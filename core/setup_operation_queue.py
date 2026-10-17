from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import urllib.error
from dataclasses import asdict,dataclass,field,replace
from datetime import datetime,timezone
from pathlib import Path
from threading import RLock
from typing import Any,Callable
from uuid import uuid4

from .ai_interrogation import (
    JOYCAPTION_MODEL,MODEL_ACQUISITIONS,PLAIN_LANGUAGE_MODELS,
    WD_EVA02_MODEL,WD_TAGGER_MODELS,
)
from .background_jobs import BackgroundJobManager,JobProgress
from .dependencies import (
    MODEL_PACK_FREE_SPACE_RESERVE,OPTIONAL_COMPONENT_ROOT,PRIVATE_EXIFTOOL_DIR,
    DependencyService,
)


SETUP_OPERATION_QUEUE_SCHEMA_VERSION=1
SETUP_OPERATION_STATES={"waiting","running","complete","failed","cancelled"}
SETUP_OPERATION_KINDS={
    "setup_application","setup_wd","setup_joy","setup_exiftool","setup_trainer_runtime",
}


@dataclass
class SetupOperation:
    id: str
    kind: str
    label: str
    params: dict[str,Any]
    key: str
    sequence: int
    state: str="waiting"
    detail: str="Waiting for its turn."
    error: str=""
    result: str=""
    attempts: int=0
    created_at: str=field(
        default_factory=lambda:datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        ),
    )
    job_id: str=""


@dataclass(frozen=True)
class SetupEnqueueResult:
    operation: SetupOperation
    accepted: bool
    message: str


class SetupOperationQueue:
    """Durable, confirmed, serial Setup work coordinated with app-wide jobs."""

    def __init__(
        self,settings,jobs: BackgroundJobManager,events,queue_path: Path,*,
        trainer_executor=None,
        before_component_mutation: Callable[[SetupOperation],None]|None=None,
        after_component_mutation: Callable[[SetupOperation],None]|None=None,
    ) -> None:
        if trainer_executor is not None and not callable(trainer_executor):
            raise ValueError("The reviewed trainer Setup executor must be callable.")
        self.trainer_executor=trainer_executor
        self.before_component_mutation=before_component_mutation
        self.after_component_mutation=after_component_mutation
        self.settings=settings
        self.jobs=jobs
        self.events=events
        self.queue_path=Path(queue_path)
        self._lock=RLock()
        self._operations: list[SetupOperation]=[]
        self._dispatched_id=""
        self.load_error=""
        self._load()

    def _load(self) -> None:
        if not self.queue_path.is_file():
            return
        try:
            payload=json.loads(self.queue_path.read_text(encoding="utf-8"))
            if (
                not isinstance(payload,dict)
                or payload.get("schema_version")!=SETUP_OPERATION_QUEUE_SCHEMA_VERSION
                or not isinstance(payload.get("operations"),list)
            ):
                raise ValueError("unsupported queue format")
            loaded=[]
            for raw in payload["operations"]:
                if not isinstance(raw,dict):
                    raise ValueError("invalid queue entry")
                operation=SetupOperation(**raw)
                if (
                    operation.kind not in SETUP_OPERATION_KINDS
                    or operation.state not in SETUP_OPERATION_STATES
                    or not isinstance(operation.params,dict)
                ):
                    raise ValueError("unsupported queue entry")
                if operation.kind=="setup_trainer_runtime":
                    expected=self._trainer_params(operation.params)
                    if expected!=operation.params or operation.key!=self._request_key(operation.kind,expected):
                        raise ValueError("the trainer review reference changed")
                operation.job_id=""
                if operation.state=="running":
                    operation.state="waiting"
                    operation.detail=(
                        "Recovered after CollectiGen closed; prerequisites will "
                        "be checked again before setup resumes."
                    )
                loaded.append(operation)
            self._operations=sorted(loaded,key=lambda value:value.sequence)
        except (OSError,TypeError,ValueError,json.JSONDecodeError) as exc:
            self.load_error=(
                "The saved Setup queue is unreadable and was left unchanged: "
                f"{exc}"
            )

    def _save_locked(self) -> None:
        if self.load_error:
            raise RuntimeError(self.load_error)
        active=[
            value for value in self._operations
            if value.state in {"waiting","running"}
        ]
        terminal=[
            value for value in self._operations
            if value.state not in {"waiting","running"}
        ][-40:]
        self._operations=sorted(
            [*terminal,*active],key=lambda value:value.sequence,
        )
        self.queue_path.parent.mkdir(parents=True,exist_ok=True)
        payload={
            "schema_version":SETUP_OPERATION_QUEUE_SCHEMA_VERSION,
            "operations":[
                {
                    **asdict(value),
                    # Runtime coordinator IDs are process-local and never
                    # authorize work after a restart.
                    "job_id":"",
                }
                for value in self._operations
            ],
        }
        temporary=self.queue_path.with_name(self.queue_path.name+".tmp")
        temporary.write_text(
            json.dumps(payload,indent=2,sort_keys=True)+"\n",encoding="utf-8",
        )
        temporary.replace(self.queue_path)
        verified=json.loads(self.queue_path.read_text(encoding="utf-8"))
        if verified.get("schema_version")!=SETUP_OPERATION_QUEUE_SCHEMA_VERSION:
            raise RuntimeError("The saved Setup queue failed read-back verification.")

    def snapshot(self) -> tuple[SetupOperation,...]:
        with self._lock:
            return tuple(
                replace(value,params=dict(value.params))
                for value in sorted(
                    self._operations,key=lambda item:item.sequence,
                )
            )

    def _publish(self) -> None:
        operations=self.snapshot()
        self.events.publish(
            "setup_queue_changed",operations=operations,
            waiting=sum(value.state=="waiting" for value in operations),
            running=sum(value.state=="running" for value in operations),
        )

    @staticmethod
    def _request_key(kind: str,params: dict[str,Any]) -> str:
        encoded=json.dumps(
            {"kind":kind,"params":params},sort_keys=True,separators=(",",":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _profile_identity(self,capability: str) -> dict[str,str]:
        service=DependencyService(self.settings)
        profile_id,profile=service.optional_profile(capability)
        return {
            "profile_id":profile_id,
            "profile_sha256":service._profile_sha256(profile),
        }

    @staticmethod
    def _trainer_params(params: dict[str,Any]) -> dict[str,str]:
        # The complete reviewed plan and recovery evidence belong to the trainer
        # service. Keep the shared queue schema small and its references flat.
        if (type(params) is not dict or set(params)!={"review_path","review_sha256"}
                or type(params["review_path"]) is not str
                or not 0<len(params["review_path"])<=4096 or "\0" in params["review_path"]
                or not Path(params["review_path"]).is_absolute()
                or type(params["review_sha256"]) is not str
                or re.fullmatch(r"[0-9a-f]{64}",params["review_sha256"]) is None):
            raise ValueError("Trainer Setup requires the exact saved review and its SHA-256.")
        return dict(params)

    def _confirmed_params(
        self,kind: str,params: dict[str,Any],
    ) -> dict[str,Any]:
        if kind=="setup_trainer_runtime":
            return self._trainer_params(params)
        selected=dict(params)
        selected["ai_models_directory"]=str(
            Path(self.settings.ai_models_directory).resolve(strict=False),
        )
        if kind=="setup_wd":
            runtime_only=bool(selected.get("runtime_only",False))
            model_ids=list(dict.fromkeys(
                str(value) for value in selected.get("model_ids",[])
            ))
            if not model_ids and not runtime_only:
                model_ids=[WD_EVA02_MODEL]
            if any(value not in WD_TAGGER_MODELS for value in model_ids):
                raise ValueError("The confirmed AI Tag setup names an unsupported model.")
            selected={
                "model_ids":model_ids,
                "runtime_only":runtime_only,
                "ai_models_directory":selected["ai_models_directory"],
                "execution_provider":str(
                    getattr(self.settings,"wd_execution_provider","cpu"),
                ),
                "model_revisions":{
                    value:MODEL_ACQUISITIONS[value].revision for value in model_ids
                },
                **self._profile_identity("wd"),
            }
        elif kind=="setup_joy":
            runtime_only=bool(selected.get("runtime_only",False))
            model_id=str(selected.get("model_id") or JOYCAPTION_MODEL)
            if model_id not in PLAIN_LANGUAGE_MODELS:
                raise ValueError(
                    "The confirmed AI Description setup names an unsupported model.",
                )
            selected={
                "model_id":model_id,
                "runtime_only":runtime_only,
                "require_selected_model":bool(
                    selected.get("require_selected_model",not runtime_only)
                ),
                "ai_models_directory":selected["ai_models_directory"],
                "quantization":str(
                    getattr(self.settings,"joycaption_quantization","bf16"),
                ),
                "model_revision":MODEL_ACQUISITIONS[model_id].revision,
                **self._profile_identity("joy"),
            }
        elif kind=="setup_application":
            packages=list(dict.fromkeys(
                str(value) for value in selected.get("packages",[]) if value
            ))
            selected={
                "packages":packages,
                "install_exiftool":bool(selected.get("install_exiftool",False)),
                "runtime_directory":str(
                    Path(sys.executable).resolve(strict=False).parent,
                ),
                "exiftool_directory":str(
                    PRIVATE_EXIFTOOL_DIR.resolve(strict=False),
                ),
            }
        elif kind=="setup_exiftool":
            selected={
                "exiftool_directory":str(
                    PRIVATE_EXIFTOOL_DIR.resolve(strict=False),
                ),
            }
        else:
            raise ValueError("This Setup operation cannot be queued.")
        return selected

    def enqueue_confirmed(
        self,kind: str,label: str,params: dict[str,Any]|None=None,
    ) -> SetupEnqueueResult:
        """Queue only work whose complete download/install review was accepted."""
        kind=str(kind)
        if kind not in SETUP_OPERATION_KINDS:
            raise ValueError("This Setup operation cannot be queued.")
        confirmed=self._confirmed_params(kind,params or {})
        service=DependencyService(self.settings)
        if kind=="setup_joy":
            hardware=service.component(DependencyService.JOY_HARDWARE_COMPONENT)
            if hardware is None or not hardware.installed:
                raise RuntimeError(
                    "The reviewed AI Description setup requires a compatible "
                    "NVIDIA GPU. Nothing was queued.",
                )
        if kind in {"setup_wd","setup_joy"}:
            self._storage_preflight(
                Path(self.settings.ai_models_directory),"AI model",
            )
            self._storage_preflight(
                OPTIONAL_COMPONENT_ROOT.parent,"optional component",
            )
        elif kind=="setup_application":
            if confirmed.get("packages"):
                self._storage_preflight(
                    Path(confirmed["runtime_directory"]),"application runtime",
                )
            if confirmed.get("install_exiftool"):
                self._storage_preflight(
                    Path(confirmed["exiftool_directory"]).parent,
                    "Metadata component",
                )
        elif kind=="setup_exiftool":
            self._storage_preflight(
                Path(confirmed["exiftool_directory"]).parent,
                "Metadata component",
            )
        key=self._request_key(kind,confirmed)
        with self._lock:
            if self.load_error:
                raise RuntimeError(self.load_error)
            duplicate=next((
                value for value in self._operations
                if value.key==key and value.state in {"waiting","running"}
            ),None)
            if duplicate is not None:
                return SetupEnqueueResult(
                    replace(duplicate,params=dict(duplicate.params)),False,
                    f"{duplicate.label} is already {duplicate.state}.",
                )
            if kind=="setup_wd":
                requested=set(confirmed.get("model_ids",[]))
                covering=next((
                    value for value in self._operations
                    if value.state in {"waiting","running"}
                    and value.kind=="setup_wd"
                    and requested.issubset(set(value.params.get("model_ids",[])))
                    and all(
                        value.params.get(name)==confirmed.get(name)
                        for name in (
                            "ai_models_directory","execution_provider",
                            "profile_id","profile_sha256",
                        )
                    )
                ),None)
                if covering is not None:
                    return SetupEnqueueResult(
                        replace(covering,params=dict(covering.params)),False,
                        f"{covering.label} already covers these AI Tag models.",
                    )
            completed=next((
                value for value in reversed(self._operations)
                if value.kind==kind and value.state=="complete"
            ),None)
            if self._is_satisfied(service,kind,confirmed):
                completed=completed or SetupOperation(
                    "already-current",kind,str(label),confirmed,key,0,
                    state="complete",detail="Setup is already complete and current.",
                )
                return SetupEnqueueResult(
                    replace(completed,params=dict(completed.params)),False,
                    f"{label} is already complete and still current.",
                )
            if kind=="setup_exiftool":
                overlap=next((
                    value for value in self._operations
                    if value.state in {"waiting","running"}
                    and value.kind=="setup_application"
                    and value.params.get("install_exiftool")
                ),None)
                if overlap is not None:
                    return SetupEnqueueResult(
                        replace(overlap,params=dict(overlap.params)),False,
                        "ExifTool is already included in the queued application repair.",
                    )
            if kind=="setup_application" and confirmed.get("install_exiftool"):
                overlap=next((
                    value for value in self._operations
                    if value.state in {"waiting","running"}
                    and value.kind=="setup_exiftool"
                ),None)
                if overlap is not None:
                    return SetupEnqueueResult(
                        replace(overlap,params=dict(overlap.params)),False,
                        "ExifTool already has a confirmed Setup operation in the queue.",
                    )
            sequence=max((value.sequence for value in self._operations),default=0)+1
            ahead=[
                value for value in self._operations
                if value.state in {"waiting","running"}
            ]
            detail=(
                f"Waiting behind {ahead[-1].label} in confirmed order."
                if ahead else "Ready to start when app resources are available."
            )
            operation=SetupOperation(
                uuid4().hex,kind,str(label),confirmed,key,sequence,detail=detail,
            )
            self._operations.append(operation)
            self._save_locked()
        self._publish(); self._submit_next()
        return SetupEnqueueResult(
            replace(operation,params=dict(operation.params)),True,
            f"Queued: {operation.label}",
        )

    @staticmethod
    def _is_satisfied(
        service: DependencyService,kind: str,params: dict[str,Any],
    ) -> bool:
        if kind=="setup_wd":
            if service.missing_packages(DependencyService.WD_PACKAGES):
                return False
            if params.get("runtime_only"):
                return True
            return all(
                (service.wd_model_dir_for(model_id)/"model.onnx").is_file()
                and (service.wd_model_dir_for(model_id)/"selected_tags.csv").is_file()
                for model_id in params.get("model_ids",[])
            )
        if kind=="setup_joy":
            model_id=str(params.get("model_id") or "")
            wanted=set(DependencyService.JOY_BASE_PACKAGES)
            if (
                model_id==JOYCAPTION_MODEL
                and params.get("quantization")=="nf4"
            ):
                wanted.add("BitsAndBytes")
            if service.missing_packages(wanted):
                return False
            if params.get("runtime_only"):
                return True
            return (service.plain_language_model_dir_for(model_id)/"config.json").is_file()
        if kind=="setup_application":
            return not any(
                item.required and not item.installed
                for item in service.components(include_sizes=False)
            )
        if kind=="setup_exiftool":
            component=service.component("ExifTool")
            return component is not None and component.installed
        return False

    def start_pending(self) -> None:
        """Resume the first confirmed incomplete operation after startup."""
        if self.load_error:
            self._publish()
            return
        self._publish(); self._submit_next()

    def _waiting_detail(self,job_id: str) -> str:
        blockers=[
            value.name for value in self.jobs.active()
            if value.id!=job_id and value.state in {"running","paused"}
        ]
        if blockers:
            return (
                "Waiting for exclusive access to local components while "
                f"{blockers[0]} is running."
            )
        return "Waiting for exclusive access to local components."

    def _submit_next(self) -> None:
        with self._lock:
            if self._dispatched_id:
                return
            operation=next((
                value for value in self._operations if value.state=="waiting"
            ),None)
            if operation is None:
                return
            self._dispatched_id=operation.id

            def worker(job: JobProgress,operation_id=operation.id):
                return self._execute(operation_id,job)

            def completed(job: JobProgress,operation_id=operation.id):
                self._job_finished(operation_id,job)

            job,_token=self.jobs.submit_observed(
                operation.label,worker,completed,resource="io",
                key=f"setup:{operation.key}",exclusive=operation.kind!="setup_trainer_runtime",
                recover_on_shutdown=True,
            )
            operation.job_id=job.id
            operation.detail=("Waiting for trainer setup resources." if operation.kind=="setup_trainer_runtime"
                              else self._waiting_detail(job.id))
            self._save_locked()
        self._publish()

    def _operation_locked(self,operation_id: str) -> SetupOperation:
        operation=next((
            value for value in self._operations if value.id==operation_id
        ),None)
        if operation is None:
            raise InterruptedError("The queued Setup operation is unavailable.")
        return operation

    def _execute(self,operation_id: str,job: JobProgress) -> str:
        with self._lock:
            operation=self._operation_locked(operation_id)
            if operation.state=="cancelled" or job.cancel_requested:
                raise InterruptedError("The waiting Setup operation was cancelled.")
            operation.state="running"
            operation.detail="Checking prerequisites and available space…"
            operation.attempts+=1
            self._save_locked()
            label=operation.label
        self._publish()
        self.events.publish(
            "long_operation_changed",active=True,owner="dependencies",
            label=label,current=0,total=0,
            message="Checking prerequisites and available space…",
            resource="io",job_id=job.id,
        )
        return self._run_operation(operation_id,job)

    def _progress(
        self,operation_id: str,job_id: str,current: int,total: int,message: str,
    ) -> None:
        detail=str(message or "Working on local component setup…")
        with self._lock:
            operation=self._operation_locked(operation_id)
            operation.detail=detail
            self._save_locked()
            label=operation.label
        self.jobs.update(job_id,current,total,detail)
        self.events.publish(
            "long_operation_changed",active=True,owner="dependencies",
            label=label,current=max(0,int(current)),total=max(0,int(total)),
            message=detail,resource="io",job_id=job_id,
        )
        self._publish()

    @staticmethod
    def _storage_preflight(path: Path,label: str) -> None:
        selected=Path(path).resolve(strict=False)
        probe=selected
        while not probe.exists() and probe!=probe.parent:
            probe=probe.parent
        if not probe.exists() or not probe.is_dir():
            raise RuntimeError(f"The {label} location is unavailable: {selected}")
        if probe.is_symlink() or not os.access(probe,os.R_OK|os.W_OK):
            raise RuntimeError(f"The {label} location is read-only or unsafe: {selected}")
        try:
            available=int(shutil.disk_usage(probe).free)
        except OSError as exc:
            raise RuntimeError(
                f"Free space at the {label} location could not be checked.",
            ) from exc
        if available<MODEL_PACK_FREE_SPACE_RESERVE:
            raise RuntimeError(
                f"The {label} location needs at least "
                f"{DependencyService.format_size(MODEL_PACK_FREE_SPACE_RESERVE)} "
                "free for verified staging before setup can start; only "
                f"{DependencyService.format_size(available)} is available.",
            )

    def _revalidate_common(self,operation: SetupOperation) -> DependencyService:
        confirmed=operation.params.get("ai_models_directory")
        current=str(Path(self.settings.ai_models_directory).resolve(strict=False))
        if confirmed is not None and current!=confirmed:
            raise RuntimeError(
                "The AI model location changed while this operation was waiting. "
                "Review and confirm the setup request again.",
            )
        return DependencyService(self.settings)

    def _revalidate_profile(
        self,service: DependencyService,operation: SetupOperation,capability: str,
    ) -> None:
        profile_id,profile=service.optional_profile(capability)
        if (
            profile_id!=operation.params.get("profile_id")
            or service._profile_sha256(profile)!=operation.params.get(
                "profile_sha256",
            )
        ):
            raise RuntimeError(
                "The reviewed component profile changed while this operation "
                "was waiting. Review and confirm it again.",
            )

    def _component_mutation(
        self,operation: SetupOperation,action: Callable[[],str],
    ) -> str:
        """Bracket one actual component write, after all safe preflight."""
        if self.before_component_mutation is not None:
            self.before_component_mutation(operation)
        try:
            return action()
        finally:
            # An interrupted installer can still replace shared model/runtime
            # files, so invalidate after every attempted mutation.
            if self.after_component_mutation is not None:
                self.after_component_mutation(operation)

    def _run_operation(self,operation_id: str,job: JobProgress) -> str:
        with self._lock:
            operation=replace(
                self._operation_locked(operation_id),
                params=dict(self._operation_locked(operation_id).params),
            )
        progress=lambda current,total,message:self._progress(
            operation_id,job.id,current,total,message,
        )
        if operation.kind=="setup_trainer_runtime":
            self._trainer_params(operation.params)
            if self.trainer_executor is None:
                raise RuntimeError("The reviewed trainer Setup service is unavailable. Retain this operation and use a manual training package.")
            # Separate immutable trainer storage uses its own OS lease. Holding
            # an exclusive app job here would deadlock its contained GPU health
            # check. The shared queue still preserves confirmed serial order.
            return self.trainer_executor(operation,
                should_cancel=lambda:job.cancel_requested or self.jobs.shutting_down,
                progress=progress)
        service=self._revalidate_common(operation)
        try:
            if operation.kind=="setup_wd":
                self._revalidate_profile(service,operation,"wd")
                if str(getattr(self.settings,"wd_execution_provider","cpu"))!=(
                    operation.params.get("execution_provider")
                ):
                    raise RuntimeError(
                        "The AI Tag execution profile changed while setup was "
                        "waiting. Review and confirm it again.",
                    )
                model_ids=list(operation.params["model_ids"])
                if any(
                    MODEL_ACQUISITIONS[value].revision
                    !=operation.params["model_revisions"].get(value)
                    for value in model_ids
                ):
                    raise RuntimeError(
                        "A reviewed AI Tag model revision changed while setup "
                        "was waiting. Review and confirm it again.",
                    )
                self._storage_preflight(
                    Path(self.settings.ai_models_directory),"AI model",
                )
                self._storage_preflight(
                    OPTIONAL_COMPONENT_ROOT.parent,"optional component",
                )
                if operation.params.get("runtime_only"):
                    if not service.missing_packages(DependencyService.WD_PACKAGES):
                        return "The shared AI Tag runtime is already installed."
                    return self._component_mutation(
                        operation,lambda:service.install_profile("wd",progress=progress),
                    )
                return self._component_mutation(
                    operation,lambda:service.setup_wd(
                        None,download_model=True,model_ids=model_ids,
                        progress=progress,
                    ),
                )
            if operation.kind=="setup_joy":
                self._revalidate_profile(service,operation,"joy")
                model_id=str(operation.params["model_id"])
                if operation.params.get("require_selected_model",True) and str(
                    getattr(self.settings,"plain_language_model",model_id)
                )!=model_id:
                    raise RuntimeError(
                        "The selected AI Description model changed while setup "
                        "was waiting. Review and confirm it again.",
                    )
                if str(getattr(self.settings,"joycaption_quantization","bf16"))!=(
                    operation.params.get("quantization")
                ):
                    raise RuntimeError(
                        "The AI Description memory mode changed while setup was "
                        "waiting. Review and confirm it again.",
                    )
                if MODEL_ACQUISITIONS[model_id].revision!=operation.params.get(
                    "model_revision",
                ):
                    raise RuntimeError(
                        "The reviewed AI Description model revision changed while "
                        "setup was waiting. Review and confirm it again.",
                    )
                hardware=service.component(DependencyService.JOY_HARDWARE_COMPONENT)
                if hardware is None or not hardware.installed:
                    raise RuntimeError(
                        "The reviewed AI Description profile no longer has a "
                        "compatible NVIDIA GPU. Nothing was installed.",
                    )
                self._storage_preflight(
                    Path(self.settings.ai_models_directory),"AI model",
                )
                self._storage_preflight(
                    OPTIONAL_COMPONENT_ROOT.parent,"optional component",
                )
                if operation.params.get("runtime_only"):
                    wanted=set(DependencyService.JOY_BASE_PACKAGES)
                    if (
                        model_id==JOYCAPTION_MODEL
                        and operation.params.get("quantization")=="nf4"
                    ):
                        wanted.add("BitsAndBytes")
                    if not service.missing_packages(wanted):
                        return "The shared AI Description runtime is already installed."
                    return self._component_mutation(
                        operation,lambda:service.install_profile("joy",progress=progress),
                    )
                return self._component_mutation(
                    operation,lambda:service.setup_joycaption(
                        None,download_model=True,model_id=model_id,progress=progress,
                    ),
                )
            if operation.kind=="setup_application":
                runtime=str(Path(sys.executable).resolve(strict=False).parent)
                exiftool=str(PRIVATE_EXIFTOOL_DIR.resolve(strict=False))
                if runtime!=operation.params.get("runtime_directory") or (
                    exiftool!=operation.params.get("exiftool_directory")
                ):
                    raise RuntimeError(
                        "The application component destination changed while "
                        "setup was waiting. Review and confirm it again.",
                    )
                components=service.components(include_sizes=False)
                missing_packages={
                    item.package for item in components
                    if item.required and not item.installed and item.package
                }
                packages=[
                    value for value in operation.params.get("packages",[])
                    if value in missing_packages
                ]
                install_exif=bool(operation.params.get("install_exiftool")) and any(
                    item.name=="ExifTool" and not item.installed
                    for item in components
                )
                if not packages and not install_exif:
                    return "Application components are already ready."
                results=[]
                if packages:
                    self._storage_preflight(
                        Path(runtime),"application runtime",
                    )
                    progress(1,0,"Installing confirmed application components…")
                    results.append(self._component_mutation(
                        operation,lambda:service.install_packages(packages),
                    ))
                if install_exif:
                    self._storage_preflight(
                        Path(exiftool).parent,"Metadata component",
                    )
                    progress(1,0,"Downloading and verifying ExifTool…")
                    results.append(self._component_mutation(
                        operation,service.download_exiftool,
                    ))
                return "\n".join(results)
            if operation.kind=="setup_exiftool":
                exiftool=str(PRIVATE_EXIFTOOL_DIR.resolve(strict=False))
                if exiftool!=operation.params.get("exiftool_directory"):
                    raise RuntimeError(
                        "The Metadata component destination changed while setup "
                        "was waiting. Review and confirm it again.",
                    )
                component=service.component("ExifTool")
                if component is not None and component.installed:
                    return "Metadata support is already ready."
                self._storage_preflight(
                    Path(exiftool).parent,"Metadata component",
                )
                progress(1,0,"Downloading and verifying ExifTool…")
                return self._component_mutation(
                    operation,service.download_exiftool,
                )
        except urllib.error.URLError as exc:
            raise RuntimeError(
                "Setup could not reach the reviewed download source. Check the "
                "internet connection, then retry this operation from the queue.",
            ) from exc
        raise RuntimeError("This queued Setup operation is unsupported.")

    def _job_finished(self,operation_id: str,job: JobProgress) -> None:
        with self._lock:
            operation=self._operation_locked(operation_id)
            was_running=operation.state=="running"
            if job.state=="complete":
                operation.state="complete"
                operation.result=str(job.result or "Setup completed.")
                operation.detail=operation.result.splitlines()[0]
                operation.error=""
            elif job.state=="cancelled":
                operation.state="cancelled"
                operation.detail=("Setup cancelled; retained recovery evidence is available." if was_running
                                  else "Cancelled before setup started.")
                operation.error=""
            else:
                operation.state="failed"
                operation.error=str(job.error or "Setup failed.")
                operation.detail=operation.error.splitlines()[0]
            operation.job_id=""
            self._dispatched_id=""
            self._save_locked()
            label=operation.label
        if was_running:
            self.events.publish(
                "long_operation_changed",active=False,owner="dependencies",
                label=label,current=0,total=0,message="",resource="io",
                job_id=job.id,
            )
        self._publish(); self._submit_next()

    def cancel_waiting(self,operation_id: str) -> tuple[bool,str]:
        with self._lock:
            operation=self._operation_locked(operation_id)
            if operation.state!="waiting":
                return False,"Only a waiting Setup operation can be cancelled here."
            operation.state="cancelled"
            operation.detail="Cancelled before setup started."
            job_id=operation.job_id
            if not job_id and self._dispatched_id==operation.id:
                self._dispatched_id=""
            self._save_locked()
        if job_id:
            self.jobs.cancel(job_id)
        else:
            self._publish(); self._submit_next()
        return True,"The waiting Setup operation was cancelled."

    def cancel_operation(self,operation_id: str) -> tuple[bool,str]:
        """Request cancellation; only the coordinated completion releases it."""
        with self._lock:
            operation=self._operation_locked(operation_id)
            waiting=operation.state=="waiting"
            if not waiting and (operation.state!="running" or not operation.job_id):
                return False,"Only a waiting or running Setup operation can be cancelled."
            job_id=operation.job_id
        if waiting:
            return self.cancel_waiting(operation_id)
        self.jobs.cancel(job_id)
        return True,"Cancellation requested; waiting for setup to retain its recovery state."

    def retry(self,operation_id: str) -> tuple[bool,str]:
        with self._lock:
            operation=self._operation_locked(operation_id)
            if operation.state not in {"failed","cancelled"}:
                return False,"Only a failed or cancelled Setup operation can be retried."
            duplicate=next((
                value for value in self._operations
                if value.id!=operation.id and value.key==operation.key
                and value.state in {"waiting","running"}
            ),None)
            if duplicate is not None:
                return False,f"{duplicate.label} is already {duplicate.state}."
            operation.sequence=max(
                (value.sequence for value in self._operations),default=0,
            )+1
            operation.state="waiting"
            operation.detail="Waiting for prerequisites to be checked again."
            operation.error=""; operation.result=""; operation.job_id=""
            self._save_locked()
        self._publish(); self._submit_next()
        return True,f"Queued again: {operation.label}"
