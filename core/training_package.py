from __future__ import annotations

import ctypes
import errno
import hashlib
import io
import json
import os
import re
import shutil
import stat
import sys
from dataclasses import dataclass,field
from pathlib import Path,PurePosixPath
from typing import Callable,Iterable,Mapping
from uuid import uuid4

from PIL import Image

from .external_write_transaction import (
    ExternalWriteDecision,ExternalWriteExecution,content_fingerprint,
    external_write_validation_issue,file_fingerprint,same_content,
    write_verified_json,
)
from .federated_dataset import LibraryImageIdentity
from .training_candidate_selection import (
    CandidateOverride,TrainingCandidate,TrainingCandidatePlan,
    TrainingCandidateSelectionService,
)
from .training_configuration import (
    TrainingConfiguration,estimate_training_resources,package_caption,
    validate_training_configuration,
)
from .training_image_transform import (
    TrainingImageTransformPlan,plan_training_image_transform,
    render_training_image,
)
from .training_plan_drafts import validate_training_plan_manifest


ProgressCallback=Callable[[int,int,str],None]
CancelCallback=Callable[[],bool]
MANIFEST_NAME="collectigen-training-manifest.json"
CONFIG_PATH="config/manual-training-package-v1.json"
README_PATH="README.txt"
PACKAGE_SCHEMA_VERSION=1
BOUND_PACKAGE_SCHEMA_VERSION=2
RECOVERY_SCHEMA_VERSION=1
RECOVERY_ACTIVE_STATUSES={
    "prepared","staging","staged_complete","promoted","attention_required",
}
RECOVERY_TERMINAL_STATUSES={"complete","cancelled","rolled_back"}

MANIFEST_FIELDS={
    "schema_version","kind","intent_fingerprint","package_plan","adapter",
    "candidate_plan","training_configuration","counts","excluded","artifacts",
    "expected_output","lineage","limitations","package_fingerprint",
}
PACKAGE_PLAN_FIELDS={
    "schema_version","adapter","candidate_plan_fingerprint",
    "training_configuration","items","generated_files",
}
PACKAGE_ITEM_FIELDS={
    "stable_id","role","artifact_id","image_path","caption_path","caption",
    "caption_sha256","caption_variant_policy","effective_exposures","transform",
}
TRANSFORM_PLAN_FIELDS={
    "stable_id","source_state","output_extension","output_format","byte_copy",
    "operations","source_dimensions","expected_dimensions","expected_mode",
    "engine","engine_version","resize_applied","crop_applied",
}
IMAGE_TRANSFORM_FIELDS=TRANSFORM_PLAN_FIELDS|{
    "output_sha256","output_size","output_dimensions","output_mode",
}
IMAGE_ARTIFACT_FIELDS={
    "path","kind","role","stable_id","artifact_id","size","sha256","transform",
}
CAPTION_ARTIFACT_FIELDS={
    "path","kind","role","stable_id","artifact_id","size","sha256",
    "canonical_caption","canonical_caption_sha256","package_caption",
    "caption_policy","variant_policy","tokenizer_status",
    "requested_token_limit","character_count","whitespace_piece_count",
}
CONTROL_ARTIFACT_FIELDS={"path","kind","role","size","sha256"}
RECOVERY_FIELDS={
    "schema_version","kind","operation_id","status","destination_name",
    "staging_name","intent_fingerprint","candidate_plan_fingerprint",
    "manifest_sha256","result","destination_parent_state",
}


def _stable_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value,ensure_ascii=False,sort_keys=True,separators=(",",":"),
    ).encode("utf-8")).hexdigest()


def _is_digest(value: object) -> bool:
    return bool(re.fullmatch(r"[a-f0-9]{64}",str(value or "")))


def _require_fields(value: object,fields: set[str],label: str) -> dict[str,object]:
    if not isinstance(value,dict) or set(value)!=fields:
        raise ValueError(f"{label} has missing or unsupported fields.")
    return value


def _json_bytes(value: object) -> bytes:
    return (json.dumps(
        value,ensure_ascii=False,sort_keys=True,indent=2,
    )+"\n").encode("utf-8")


def _safe_relative(value: str|Path) -> str:
    text=str(value).replace("\\","/")
    path=PurePosixPath(text)
    if (
        not text or text=="." or path.is_absolute() or ".." in path.parts
        or not path.parts or ":" in path.parts[0]
    ):
        raise ValueError(f"Unsafe training package path: {value}")
    return path.as_posix()


def _safe_name(value: object) -> str:
    text=str(value or "")
    if (
        not text or text in {".",".."} or Path(text).name!=text
        or re.search(r"[\\/:*?\"<>|\x00-\x1f]",text)
    ):
        raise ValueError("A training package directory name is unsafe.")
    return text


def _looks_machine_local(value: str) -> bool:
    raw=str(value or "")
    if (
        re.search(r"(?<![a-zA-Z0-9])[a-zA-Z]:[\\/]",raw)
        or re.search(r"\\\\[^\\\s]+[\\/]",raw)
    ):
        return True
    without_urls=re.sub(r"https?://\S+","",raw,flags=re.IGNORECASE)
    return bool(re.search(r"(?<![a-zA-Z0-9])/(?:[^/\s]+/)+",without_urls))


def _assert_source_neutral(
    value: object,location: str="manifest",*,should_cancel: CancelCallback|None=None,
) -> None:
    visited=0
    def visit(item: object,position: str) -> None:
        nonlocal visited
        if visited%128==0 and should_cancel is not None and should_cancel():
            raise InterruptedError("Training package verification was cancelled.")
        visited+=1
        if isinstance(item,dict):
            for key,child in item.items():
                visit(child,f"{position}.{key}")
        elif isinstance(item,list):
            for index,child in enumerate(item):
                visit(child,f"{position}[{index}]")
        elif isinstance(item,str) and _looks_machine_local(item):
            raise ValueError(f"The package manifest contains a machine-local path at {position}.")
    visit(value,location)


def _is_redirected(path: Path) -> bool:
    try:
        stat=path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(
        int(getattr(stat,"st_file_attributes",0))&0x400
    )


def _lexists(path: Path) -> bool:
    return os.path.lexists(str(path))


def _has_redirected_component(path: Path) -> bool:
    current=path.absolute()
    while True:
        if current.exists() and _is_redirected(current):
            return True
        if current.parent==current:
            return False
        current=current.parent


def _child(root: Path,relative: str) -> Path:
    safe=_safe_relative(relative)
    target=(root/Path(*PurePosixPath(safe).parts)).absolute()
    if target.parent!=root.absolute() and not target.is_relative_to(root.absolute()):
        raise ValueError("A package artifact escapes its destination.")
    current=root.absolute()
    if _is_redirected(current):
        raise ValueError("The package root is redirected.")
    for part in PurePosixPath(safe).parts:
        current=current/part
        if _lexists(current) and _is_redirected(current):
            raise ValueError(f"The package path is redirected at {part}.")
    return target


def _directory_state(path: Path) -> dict[str,object]:
    stat=path.stat(follow_symlinks=False)
    if not path.is_dir() or _is_redirected(path):
        raise ValueError("The package parent is missing, not a directory, or redirected.")
    return {
        "resolved_sha256":hashlib.sha256(
            str(path.resolve(strict=True)).casefold().encode("utf-8")
        ).hexdigest(),
        "device":int(getattr(stat,"st_dev",0)),
        "inode":int(getattr(stat,"st_ino",0)),
        "mode":int(getattr(stat,"st_mode",0)),
        "attributes":int(getattr(stat,"st_file_attributes",0)),
    }


def _mkdir_package_parent(root: Path,relative_parent: str) -> Path:
    safe=_safe_relative(relative_parent)
    current=root.absolute()
    if _is_redirected(current):
        raise RuntimeError("The package staging root is redirected.")
    for part in PurePosixPath(safe).parts:
        current=current/part
        if _lexists(current):
            if not current.is_dir() or _is_redirected(current):
                raise RuntimeError(f"The package directory is unsafe at {part}.")
        else:
            current.mkdir()
        if _is_redirected(current):
            raise RuntimeError(f"The package directory became redirected at {part}.")
    return current


def _write_new_package_bytes(root: Path,relative: str,payload: bytes) -> Path:
    safe=_safe_relative(relative)
    parent=PurePosixPath(safe).parent.as_posix()
    if parent!=".":
        _mkdir_package_parent(root,parent)
    target=_child(root,safe)
    with target.open("xb") as handle:
        handle.write(payload); handle.flush(); os.fsync(handle.fileno())
    if not same_content(file_fingerprint(target),content_fingerprint(payload)):
        raise RuntimeError(f"{safe} failed staged byte verification.")
    return target


@dataclass(frozen=True)
class TrainingPackageVerificationLimits:
    """Opt-in limits for inspecting a user-selected reference, not publication."""
    manifest_bytes: int = 16 * 1024 * 1024
    artifact_bytes: int = 64 * 1024 * 1024
    total_bytes: int = 4 * 1024 * 1024 * 1024
    entries: int = 25_000
    depth: int = 8
    artifacts: int = 20_000
    candidates: int = 10_000
    image_pixels: int = 32_000_000

    def __post_init__(self):
        if any(type(value) is not int or value < 1 for value in vars(self).values()):
            raise ValueError("Package inspection limits must be positive integers.")


def _inspection_signature(path):
    from .training_source_io import _assert_unredirected, _identity
    _assert_unredirected(path)
    value = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(value.st_mode):
        raise ValueError("Package inspection requires regular files.")
    return (*_identity(value), value.st_ctime_ns)


def _inspection_read(path, limit, cancel):
    """Bounded stable read; growth after stat never permits an unbounded read."""
    from .training_source_io import _identity
    if cancel(): raise InterruptedError("Training package verification was cancelled.")
    before = _inspection_signature(path)
    if before[2] > limit:
        raise ValueError("Package inspection byte limit exceeded.")
    raw = bytearray(); total = 0
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        # On Windows path stat ctime is creation time while fstat can report
        # change time. Reuse the established handle identity; keep path ctime
        # only for path-to-path freshness checks.
        if _identity(opened) != before[:5]:
            raise ValueError("Package file changed before inspection.")
        while True:
            if cancel():
                raise InterruptedError("Training package verification was cancelled.")
            chunk = stream.read(min(1024 * 1024, limit - total + 1))
            if not chunk: break
            total += len(chunk)
            if total > limit:
                raise ValueError("Package inspection byte limit exceeded during reading.")
            raw.extend(chunk)
        if _identity(os.fstat(stream.fileno())) != before[:5]:
            raise ValueError("Package file changed during inspection.")
    if total != before[2] or _inspection_signature(path) != before:
        raise ValueError("Package file changed during inspection.")
    return bytes(raw), before


def _package_files(root: Path, *, limits=None, should_cancel=None) -> tuple[tuple[str,...],tuple[str,...]]:
    files=[]; redirects=[]; stack=[root]; count=0; total=0
    while stack:
        if should_cancel and should_cancel():
            raise InterruptedError("Training package verification was cancelled.")
        current=stack.pop()
        if limits and len(current.relative_to(root).parts) > limits.depth:
            raise ValueError("Package inspection depth limit exceeded.")
        if limits and _has_redirected_component(current):
            raise ValueError("Package directory changed or is redirected.")
        with os.scandir(current) as entries:
            for entry in entries:
                if should_cancel and should_cancel():
                    raise InterruptedError("Training package verification was cancelled.")
                count += 1
                if limits and count > limits.entries:
                    raise ValueError("Package inspection entry limit exceeded.")
                path=Path(entry.path)
                relative=path.relative_to(root).as_posix()
                if _is_redirected(path):
                    redirects.append(relative)
                elif entry.is_dir(follow_symlinks=False):
                    stack.append(path)
                elif entry.is_file(follow_symlinks=False):
                    if limits:
                        size=entry.stat(follow_symlinks=False).st_size
                        total += size
                        if size > max(limits.manifest_bytes, limits.artifact_bytes) or total > limits.total_bytes:
                            raise ValueError("Package inspection inventory byte limit exceeded.")
                    files.append(relative)
                else:
                    redirects.append(relative)
    return (
        tuple(sorted(files,key=str.casefold)),
        tuple(sorted(redirects,key=str.casefold)),
    )


def _rename_directory_no_replace(source: Path,target: Path) -> None:
    """Atomically publish one directory without replacing a concurrent target."""
    if target.exists():
        raise FileExistsError(f"The package destination is now occupied: {target}")
    if os.name=="nt":
        os.rename(source,target)
        return
    if sys.platform.startswith("linux"):
        libc=ctypes.CDLL(None,use_errno=True)
        renameat2=getattr(libc,"renameat2",None)
        if renameat2 is None:
            raise RuntimeError("Atomic no-replace directory publication is unavailable.")
        result=renameat2(
            -100,os.fsencode(source),-100,os.fsencode(target),1,
        )
        if result!=0:
            code=ctypes.get_errno()
            if code==errno.EEXIST:
                raise FileExistsError(f"The package destination is now occupied: {target}")
            raise OSError(code,os.strerror(code),str(target))
        return
    raise RuntimeError("Atomic no-replace directory publication is unavailable on this platform.")


@dataclass(frozen=True)
class TrainingPackageIssue:
    code: str
    level: str
    summary: str
    reason: str
    direct_fix: str=""

    def to_dict(self) -> dict[str,str]:
        return {
            "code":self.code,"level":self.level,"summary":self.summary,
            "reason":self.reason,"direct_fix":self.direct_fix,
        }


@dataclass(frozen=True)
class TrainingPackageItem:
    candidate: TrainingCandidate
    role: str
    artifact_id: str
    image_path: str
    caption_path: str
    caption: str
    caption_sha256: str
    transform: TrainingImageTransformPlan
    effective_exposures: int

    def to_preview_dict(self) -> dict[str,object]:
        return {
            "stable_id":self.candidate.stable_id,"role":self.role,
            "artifact_id":self.artifact_id,"image_path":self.image_path,
            "caption_path":self.caption_path,"caption":self.caption,
            "caption_sha256":self.caption_sha256,
            "caption_variant_policy":"canonical_only",
            "effective_exposures":self.effective_exposures,
            "transform":self.transform.to_dict(),
        }


@dataclass(frozen=True)
class TrainingCollectionBinding:
    version_id: str
    collection_id: str
    collection_store_revision: str
    candidate_plan_fingerprint: str
    schema_version: int=1

    def to_dict(self):
        value={"schema_version":self.schema_version,"version_id":self.version_id,
            "collection_id":self.collection_id,"collection_store_revision":self.collection_store_revision,
            "candidate_plan_fingerprint":self.candidate_plan_fingerprint}
        self.from_dict(value)
        return value

    @classmethod
    def from_dict(cls,value):
        value=_require_fields(value,{"schema_version","version_id","collection_id",
            "collection_store_revision","candidate_plan_fingerprint"},"The Collection binding")
        if (type(value["schema_version"]) is not int or value["schema_version"]!=1
                or not isinstance(value["collection_id"],str)
                or not re.fullmatch(r"concept\.[0-9a-f]{32}",value["collection_id"])
                or any(not isinstance(value[key],str) or not _is_digest(value[key]) for key in (
                    "version_id","collection_store_revision","candidate_plan_fingerprint"))):
            raise ValueError("The Collection binding has invalid identity or revision evidence.")
        return cls(**value)


@dataclass(frozen=True)
class TrainingPackagePlan:
    candidate_plan: TrainingCandidatePlan
    candidate_manifest: dict[str,object]
    destination: Path
    destination_disposition: str
    items: tuple[TrainingPackageItem,...]
    generated_files: tuple[str,...]
    intent_fingerprint: str
    estimate: object
    issues: tuple[TrainingPackageIssue,...]=()
    decision: ExternalWriteDecision=field(default_factory=lambda:ExternalWriteDecision.from_values(backup_enabled=False))
    cancelled: bool=False
    destination_parent_state: dict[str,object]=field(default_factory=dict)
    forbidden_roots: tuple[Path,...]=()
    collection_version: TrainingCollectionBinding|None=None
    collection_guard: Callable|None=field(default=None,compare=False,repr=False)

    @property
    def ready(self) -> bool:
        return bool(
            self.candidate_plan.package_ready and not self.cancelled
            and self.destination_disposition in {"create","current"}
            and not any(issue.level=="blocking" for issue in self.issues)
        )

    def to_preview_dict(self,*,item_limit: int|None=None) -> dict[str,object]:
        selected=self.items if item_limit is None else self.items[:item_limit]
        return {
            "schema_version":BOUND_PACKAGE_SCHEMA_VERSION if self.collection_version else PACKAGE_SCHEMA_VERSION,
            "kind":"collectigen_training_package_preview",
            "destination_name":self.destination.name,
            "destination_disposition":self.destination_disposition,
            "intent_fingerprint":self.intent_fingerprint,
            "candidate_plan_fingerprint":self.candidate_manifest.get("plan_fingerprint"),
            **({"collection_version":self.collection_version.to_dict()} if self.collection_version else {}),
            "training_configuration":self.candidate_plan.configuration.to_dict(),
            "items":[item.to_preview_dict() for item in selected],
            "preview_truncated":len(selected)!=len(self.items),
            "item_count":len(self.items),"generated_files":list(self.generated_files),
            "resource_estimate":self.estimate.to_dict(),
            "issues":[issue.to_dict() for issue in self.issues],
            "privacy":{
                "confirmation_required":self.decision.privacy_confirmation_required,
                "message":self.decision.privacy_message,
                "source_roles":list(self.decision.boundary.source_roles),
                "destination_roles":list(self.decision.boundary.destination_roles),
            },
            "ready":self.ready,
        }


@dataclass(frozen=True)
class TrainingPackageVerification:
    destination: Path
    valid: bool
    package_fingerprint: str=""
    intent_fingerprint: str=""
    artifact_count: int=0
    manifest: dict[str,object]=field(default_factory=dict)
    issues: tuple[str,...]=()
    inspection_sources: tuple=()


@dataclass(frozen=True)
class TrainingPackageResult:
    status: str
    destination: Path
    created_files: int=0
    manifest_path: Path|None=None
    recovery_receipt: Path|None=None
    verification: TrainingPackageVerification|None=None
    issues: tuple[str,...]=()
    recovery_phase: str=""


@dataclass(frozen=True)
class TrainingPackageComparison:
    identical: bool
    first: TrainingPackageVerification
    second: TrainingPackageVerification
    differences: tuple[str,...]=()


@dataclass(frozen=True)
class TrainingRecoveryChoice:
    """A validated local receipt listing, not permission to run recovery."""

    receipt: Path
    destination_name: str
    status: str


class TrainingPackageService:
    """Preview, immutably publish, verify, recover, and compare manual packages."""

    def __init__(self,knowledge=None,characters=None,categories=None) -> None:
        self.knowledge=knowledge; self.characters=characters; self.categories=categories

    @staticmethod
    def _artifact_id(stable_id: str) -> str:
        return hashlib.sha256(stable_id.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _generated_files(items: Iterable[TrainingPackageItem]) -> tuple[str,...]:
        paths=[]
        for item in items:
            paths.extend((item.image_path,item.caption_path))
        paths.extend(
            f"splits/{role}.json" for role in ("training","validation","holdout")
        )
        paths.extend((CONFIG_PATH,README_PATH,MANIFEST_NAME))
        return tuple(sorted(paths,key=str.casefold))

    @staticmethod
    def _intent_payload(
        candidate_manifest: Mapping[str,object],configuration: TrainingConfiguration,
        items: Iterable[TrainingPackageItem],generated_files: Iterable[str],
        collection_version: TrainingCollectionBinding|None=None,
    ) -> dict[str,object]:
        return {
            "schema_version":BOUND_PACKAGE_SCHEMA_VERSION if collection_version else PACKAGE_SCHEMA_VERSION,
            **({"collection_version":collection_version.to_dict()} if collection_version else {}),
            "adapter":configuration.to_dict()["adapter"],
            "candidate_plan_fingerprint":candidate_manifest.get("plan_fingerprint"),
            "training_configuration":configuration.to_dict(),
            "items":[item.to_preview_dict() for item in items],
            "generated_files":list(generated_files),
        }

    def _rebuild_candidate_plan(
        self,plan: TrainingCandidatePlan,*,should_cancel: CancelCallback|None=None,
    ) -> TrainingCandidatePlan:
        identities={}; overrides={}
        for candidate in plan.candidates:
            source=candidate.source_identity
            if source.get("kind")=="library_image":
                identities[candidate.image]=LibraryImageIdentity(
                    str(source["library_id"]),str(source["relative_image_path"]),
                )
            if candidate.user_override:
                overrides[candidate.stable_id]=CandidateOverride(
                    candidate.decision,candidate.decision_reason,candidate.role,
                )
        service=TrainingCandidateSelectionService(
            self.knowledge,self.characters,self.categories,
            duplicate_evidence_provider=(
                (lambda *_args,**_kwargs:plan.analysis.duplicate_evidence)
                if plan.analysis is not None and plan.analysis.duplicate_evidence is not None else None
            ),
        )
        return service.plan(
            (candidate.image for candidate in plan.candidates),plan.goal,plan.profile,
            identities=identities,overrides=overrides,
            source_label=plan.source_label,
            permission_disposition=plan.permission_disposition,
            configuration=plan.configuration,
            should_cancel=should_cancel,
        )

    def _candidate_plan_is_current(
        self,plan: TrainingCandidatePlan,manifest: Mapping[str,object],
        *,should_cancel: CancelCallback|None=None,
    ) -> bool:
        rebuilt=self._rebuild_candidate_plan(
            plan,should_cancel=should_cancel,
        )
        if should_cancel is not None and should_cancel():
            raise InterruptedError("Training package source revalidation was cancelled.")
        return rebuilt.to_manifest_dict()==manifest

    @staticmethod
    def _destination_guard_issues(
        plan: TrainingPackagePlan,*,allow_destination: bool=False,
    ) -> tuple[str,...]:
        issues=[]; parent=plan.destination.parent
        try:
            if _directory_state(parent)!=plan.destination_parent_state:
                issues.append("The package parent folder changed after preview.")
        except (OSError,ValueError) as exc:
            issues.append(f"The package parent folder is unsafe: {exc}")
        if _has_redirected_component(parent):
            issues.append("The package parent path is redirected.")
        if _lexists(plan.destination) and not allow_destination:
            issues.append("The package destination became occupied after preview.")
        try:
            resolved_destination=plan.destination.resolve(strict=False)
            for root in plan.forbidden_roots:
                resolved_root=root.resolve(strict=False)
                if (
                    resolved_destination==resolved_root
                    or resolved_destination.is_relative_to(resolved_root)
                    or resolved_root.is_relative_to(resolved_destination)
                ):
                    issues.append("The package destination now overlaps a named image library.")
                    break
        except (OSError,RuntimeError,ValueError) as exc:
            issues.append(f"The package destination containment could not be checked: {exc}")
        return tuple(dict.fromkeys(issues))

    def preview(
        self,candidate_plan: TrainingCandidatePlan,destination: str|Path,*,
        decision: ExternalWriteDecision|None=None,
        forbidden_roots: Iterable[str|Path]=(),progress: ProgressCallback|None=None,
        should_cancel: CancelCallback|None=None,
        collection_version: TrainingCollectionBinding|None=None,collection_guard: Callable|None=None,
    ) -> TrainingPackagePlan:
        notify=progress or (lambda *_:None); cancel=should_cancel or (lambda:False)
        destination=Path(destination).expanduser().absolute()
        selected_decision=decision or ExternalWriteDecision.from_values(backup_enabled=False)
        candidate_manifest=candidate_plan.to_manifest_dict()
        issues=[]; items=[]; cancelled=False
        if collection_version is not None:
            collection_version.to_dict()
            if (collection_version.candidate_plan_fingerprint!=candidate_manifest["plan_fingerprint"]
                    or not callable(collection_guard)):
                issues.append(TrainingPackageIssue("collection_binding_invalid","blocking",
                    "The Collection version or current-state guard is unavailable.",
                    "A bound export requires matching candidate evidence and a current Collection lease.",
                    "Rebuild through the Collection version export preparation route."))
        try:
            if not self._candidate_plan_is_current(
                candidate_plan,candidate_manifest,should_cancel=cancel,
            ):
                issues.append(TrainingPackageIssue(
                    "candidate_plan_stale","blocking","The candidate plan no longer matches current evidence.",
                    "Source bytes, Image Knowledge, captions, permissions, roles, profile, or settings changed after the plan was built.",
                    "Rebuild and review the exact candidate plan before choosing a package destination.",
                ))
        except Exception as exc:
            issues.append(TrainingPackageIssue(
                "candidate_plan_recheck","blocking","Current candidate evidence could not be rechecked.",
                str(exc),"Restore the source scope and rebuild the plan.",
            ))
        if not candidate_plan.ready:
            issues.append(TrainingPackageIssue(
                "candidate_plan_not_ready","blocking","The candidate selection is not ready.",
                "At least one training image and no blocking candidate/plan issue are required.",
                "Resolve the named candidate checks before packaging.",
            ))
        if not candidate_plan.permission_ready:
            issues.append(TrainingPackageIssue(
                "permission_not_ready","blocking","The source and permission disposition is on hold.",
                "The exact included evidence scope has not been explicitly approved for this planned use.",
                "Review Source & Permission and record a reasoned disposition.",
            ))
        configuration=candidate_plan.configuration
        if configuration is None:
            issues.append(TrainingPackageIssue(
                "configuration_missing","blocking","Training handoff settings are missing.",
                "A package must preserve the target and adapter-neutral settings.",
                "Rebuild this plan with current Training Setup settings.",
            ))
            configuration=TrainingConfiguration()
        for issue in validate_training_configuration(
            configuration,profile_family=candidate_plan.profile.target_model_family,
            profile_version=candidate_plan.profile.target_model_version,
            require_base_model=True,
        ):
            issues.append(TrainingPackageIssue(
                f"configuration_{issue.code}",issue.level,issue.summary,
                issue.reason,issue.direct_fix,
            ))
        try:
            _safe_name(destination.name)
        except ValueError as exc:
            issues.append(TrainingPackageIssue(
                "destination_name","blocking","The package destination name is unsafe.",
                str(exc),"Choose a simple new folder name.",
            ))
        parent=destination.parent
        if not parent.is_dir():
            issues.append(TrainingPackageIssue(
                "destination_parent","blocking","The package parent folder is unavailable.",
                "The manual package is created only below an existing local folder.",
                "Choose an existing writable parent folder.",
            ))
        elif _has_redirected_component(parent):
            issues.append(TrainingPackageIssue(
                "destination_redirected","blocking","The package destination is redirected.",
                "A symlink or reparse-point component makes containment and recovery ambiguous.",
                "Choose a non-redirected local destination.",
            ))
        guarded_roots=[]
        for root_value in forbidden_roots:
            if not str(root_value or "").strip():
                continue
            root=Path(root_value).expanduser().absolute()
            guarded_roots.append(root)
            root=root.resolve()
            resolved_destination=destination.resolve(strict=False)
            if (
                resolved_destination==root or resolved_destination.is_relative_to(root)
                or root.is_relative_to(resolved_destination)
            ):
                issues.append(TrainingPackageIssue(
                    "destination_library_overlap","blocking","The package overlaps a named image library.",
                    "Generated package files must not be re-ingested into or contain a source library.",
                    "Choose a separate destination outside every named library.",
                )); break
        included=tuple(
            item for item in candidate_plan.candidates if item.decision=="include"
        )
        for index,candidate in enumerate(included,1):
            if cancel():
                cancelled=True; break
            try:
                transform=plan_training_image_transform(candidate)
                artifact_id=self._artifact_id(candidate.stable_id)
                role=candidate.role
                image_path=f"images/{role}/{artifact_id}.{transform.output_extension}"
                caption_path=f"images/{role}/{artifact_id}.txt"
                caption=package_caption(candidate.caption,configuration)
                if not caption.strip():
                    raise ValueError("The exact package caption is empty.")
                items.append(TrainingPackageItem(
                    candidate,role,artifact_id,image_path,caption_path,caption,
                    hashlib.sha256(caption.encode("utf-8")).hexdigest(),transform,
                    configuration.repeats*configuration.epochs,
                ))
            except Exception as exc:
                issues.append(TrainingPackageIssue(
                    "artifact_projection","blocking",
                    f"{candidate.image.name} cannot be projected into the package.",
                    str(exc),"Review the exact source and caption, then preview again.",
                ))
            notify(index,len(included),candidate.image.name)
        paths=[]
        for item in items:
            paths.extend((item.image_path,item.caption_path))
        folded=[path.casefold() for path in paths]
        if len(folded)!=len(set(folded)):
            issues.append(TrainingPackageIssue(
                "artifact_collision","blocking","Two package artifacts have the same portable path.",
                "Stable artifact IDs must remain unique even on case-insensitive filesystems.",
                "Rebuild the plan; do not overwrite either artifact.",
            ))
        generated=self._generated_files(items)
        intent_payload=self._intent_payload(
            candidate_manifest,configuration,items,generated,collection_version,
        )
        intent=_stable_hash(intent_payload)
        disposition="create"
        if _lexists(destination):
            verification=self.verify(destination,expected_intent=intent)
            disposition="current" if verification.valid else "collision"
            if disposition=="collision":
                issues.append(TrainingPackageIssue(
                    "destination_collision","blocking","The package destination already exists and differs.",
                    "Immutable packages are never merged, replaced, or partially updated.",
                    "Choose a new versioned folder name or verify the existing package separately.",
                ))
        try:
            estimate=estimate_training_resources(
                configuration,candidate_plan.candidates,
            )
        except Exception as exc:
            issues.append(TrainingPackageIssue(
                "resource_estimate","blocking","Package resources could not be estimated.",
                str(exc),"Correct the invalid training settings and preview again.",
            ))
            estimate=estimate_training_resources(
                TrainingConfiguration(),candidate_plan.candidates,
            )
        if parent.is_dir():
            try:
                free=shutil.disk_usage(parent).free
                if free<estimate.package_staging_bytes:
                    issues.append(TrainingPackageIssue(
                        "insufficient_space","blocking","The destination has insufficient free space.",
                        f"Safe staging needs about {estimate.package_staging_bytes:,} bytes; {free:,} bytes are currently free.",
                        "Free space or choose another destination, then preview again.",
                    ))
            except OSError as exc:
                issues.append(TrainingPackageIssue(
                    "space_unavailable","blocking","Free space could not be verified.",
                    str(exc),"Choose an available local destination and retry.",
                ))
        try:
            pending_recovery=self.recovery_receipts(
                parent,destination_name=destination.name,
            )
        except (OSError,ValueError):
            pending_recovery=()
        if pending_recovery:
            issues.append(TrainingPackageIssue(
                "recovery_pending","blocking",
                "An interrupted package operation already owns this destination.",
                "Starting another publication could obscure its exact recovery state.",
                "Recover or inspect the existing receipt before previewing again.",
            ))
        try:
            parent_state=_directory_state(parent)
        except (OSError,ValueError):
            parent_state={}
        return TrainingPackagePlan(
            candidate_plan,candidate_manifest,destination,disposition,
            tuple(items),generated,intent,estimate,tuple(issues),selected_decision,
            cancelled,parent_state,tuple(guarded_roots),
            collection_version,collection_guard,
        )

    @staticmethod
    def _config_payload(plan: TrainingPackagePlan) -> dict[str,object]:
        return {
            "schema_version":1,"kind":"collectigen_manual_training_package_config",
            "intent_fingerprint":plan.intent_fingerprint,
            "training_configuration":plan.candidate_plan.configuration.to_dict(),
            "translation":{
                "status":"manual_handoff_unverified",
                "named_trainer":None,
                "unsupported_or_unverified":[
                    "trainer CLI/config translation","exact tokenizer counts",
                    "base-model compatibility","GPU/runtime compatibility",
                    "checkpoint/resume behavior","output registration",
                ],
            },
        }

    @staticmethod
    def _split_payload(plan: TrainingPackagePlan,role: str) -> dict[str,object]:
        return {
            "schema_version":1,"kind":"collectigen_training_package_split",
            "role":role,"intent_fingerprint":plan.intent_fingerprint,
            "items":[{
                "stable_id":item.candidate.stable_id,"artifact_id":item.artifact_id,
                "image":item.image_path,"caption":item.caption_path,
                "effective_exposures":item.effective_exposures,
            } for item in plan.items if item.role==role],
        }

    @staticmethod
    def _readme_from_manifest(
        candidate_manifest: Mapping[str,object],configuration: TrainingConfiguration,
    ) -> str:
        goal=candidate_manifest.get("goal")
        goal_text=(
            str(goal.get("text") or "").strip()
            if isinstance(goal,dict) else ""
        )
        return (
            "CollectiGen manual training package v1\n"
            "========================================\n\n"
            "This is a source-neutral, verified manual handoff. It did not install or "
            "run a trainer, download or load a model, upload data, or create a trained output.\n\n"
            f"Goal: {goal_text}\n"
            f"Declared target: {configuration.target_model_family} {configuration.target_model_version}\n"
            f"Declared base model: {configuration.base_model_id}\n"
            f"Expected output: {configuration.output_name} ({configuration.output_kind})\n\n"
            "Before training, use a compatible trainer to verify its image/caption layout, "
            "tokenizer, base model, settings translation, VRAM, checkpoint, and output behavior. "
            "TRN-03 owns named trainer compatibility; TRN-04 owns execution.\n\n"
            f"Verify this package from {MANIFEST_NAME}; every file except the manifest is "
            "listed there with an exact SHA-256 hash. Rebuild to a new folder and compare "
            "package fingerprints to check determinism.\n"
        )

    @staticmethod
    def _readme(plan: TrainingPackagePlan) -> str:
        return TrainingPackageService._readme_from_manifest(
            plan.candidate_manifest,plan.candidate_plan.configuration,
        )

    def _write_stage(
        self,plan: TrainingPackagePlan,stage: Path,*,progress: ProgressCallback,
        should_cancel: CancelCallback,
    ) -> tuple[dict[str,object],int]:
        artifacts=[]; total=len(plan.items)*2+5; completed=0
        for item in plan.items:
            if should_cancel():
                raise InterruptedError("Training package creation was cancelled.")
            guard=self._destination_guard_issues(plan)
            if guard:
                raise RuntimeError(" ".join(guard))
            transformed=render_training_image(
                item.candidate,item.transform,should_cancel=should_cancel,
            )
            _write_new_package_bytes(stage,item.image_path,transformed.payload)
            artifacts.append({
                "path":item.image_path,"kind":"image","role":item.role,
                "stable_id":item.candidate.stable_id,"artifact_id":item.artifact_id,
                "size":transformed.output_size,"sha256":transformed.output_sha256,
                "transform":transformed.provenance(),
            })
            completed+=1; progress(completed,total,item.image_path)
            caption_bytes=(item.caption+"\n").encode("utf-8")
            _write_new_package_bytes(stage,item.caption_path,caption_bytes)
            artifacts.append({
                "path":item.caption_path,"kind":"caption","role":item.role,
                "stable_id":item.candidate.stable_id,"artifact_id":item.artifact_id,
                "size":len(caption_bytes),"sha256":hashlib.sha256(caption_bytes).hexdigest(),
                "canonical_caption":item.candidate.caption,
                "canonical_caption_sha256":item.candidate.caption_sha256,
                "package_caption":item.caption,
                "caption_policy":plan.candidate_plan.configuration.caption_prefix_policy,
                "variant_policy":"canonical_only","tokenizer_status":"unavailable_for_manual_adapter",
                "requested_token_limit":plan.candidate_plan.configuration.max_caption_tokens,
                "character_count":len(item.caption),
                "whitespace_piece_count":len(item.caption.split()),
            })
            completed+=1; progress(completed,total,item.caption_path)
        payloads={CONFIG_PATH:_json_bytes(self._config_payload(plan))}
        for role in ("training","validation","holdout"):
            payloads[f"splits/{role}.json"]=_json_bytes(self._split_payload(plan,role))
        payloads[README_PATH]=self._readme(plan).encode("utf-8")
        for relative,payload in sorted(payloads.items(),key=lambda item:item[0].casefold()):
            if should_cancel():
                raise InterruptedError("Training package creation was cancelled.")
            guard=self._destination_guard_issues(plan)
            if guard:
                raise RuntimeError(" ".join(guard))
            _write_new_package_bytes(stage,relative,payload)
            artifacts.append({
                "path":relative,
                "kind":"configuration" if relative==CONFIG_PATH else "split" if relative.startswith("splits/") else "handoff",
                "role":PurePosixPath(relative).stem if relative.startswith("splits/") else "",
                "size":len(payload),"sha256":hashlib.sha256(payload).hexdigest(),
            })
            completed+=1; progress(completed,total,relative)
        return {"artifacts":sorted(artifacts,key=lambda item:str(item["path"]).casefold())},completed

    def _manifest_payload(
        self,plan: TrainingPackagePlan,artifacts: list[dict[str,object]],
    ) -> dict[str,object]:
        roles={role:sum(item.role==role for item in plan.items) for role in ("training","validation","holdout")}
        payload={
            "schema_version":BOUND_PACKAGE_SCHEMA_VERSION if plan.collection_version else PACKAGE_SCHEMA_VERSION,
            **({"collection_version":plan.collection_version.to_dict()} if plan.collection_version else {}),
            "kind":"collectigen_training_package",
            "intent_fingerprint":plan.intent_fingerprint,
            "package_plan":self._intent_payload(
                plan.candidate_manifest,plan.candidate_plan.configuration,
                plan.items,plan.generated_files,
                plan.collection_version,
            ),
            "adapter":plan.candidate_plan.configuration.to_dict()["adapter"],
            "candidate_plan":plan.candidate_manifest,
            "training_configuration":plan.candidate_plan.configuration.to_dict(),
            "counts":{
                "included_images":len(plan.items),"excluded_images":plan.candidate_plan.count("exclude"),
                **roles,"artifacts":len(artifacts),
            },
            "excluded":[{
                "stable_id":item.stable_id,"role":item.role,
                "reason":item.decision_reason,"user_override":item.user_override,
            } for item in plan.candidate_plan.candidates if item.decision=="exclude"],
            "artifacts":artifacts,
            "expected_output":plan.candidate_plan.configuration.to_dict()["expected_output"],
            "lineage":{
                "base_model_id":plan.candidate_plan.configuration.base_model_id,
                "candidate_plan_fingerprint":plan.candidate_manifest["plan_fingerprint"],
                "profile":plan.candidate_plan.profile.provenance(),
                "future_output_registration_required":True,
                "trained_output_created":False,
            },
            "limitations":[
                *plan.candidate_plan.limitations,
                "Named trainer translation and execution are outside this manual package.",
                "Exact tokenizer effects are unavailable; whitespace pieces are a labelled proxy only.",
                "Caption variants are not supported; each image has one canonical package caption.",
            ],
        }
        _assert_source_neutral(payload)
        return {**payload,"package_fingerprint":_stable_hash(payload)}

    @staticmethod
    def _receipt_path(destination: Path,operation_id: str) -> Path:
        return destination.parent/f".{destination.name}.collectigen-{operation_id}.recovery.json"

    @staticmethod
    def _stage_path(destination: Path,operation_id: str) -> Path:
        return destination.parent/f".{destination.name}.collectigen-{operation_id}.staging"

    @staticmethod
    def _marker_payload(operation_id: str,destination_name: str) -> dict[str,object]:
        return {
            "schema_version":1,"kind":"collectigen_training_package_staging",
            "operation_id":operation_id,"destination_name":destination_name,
        }

    @staticmethod
    def _recovery_payload(
        plan: TrainingPackagePlan,operation_id: str,stage: Path,status: str,* ,
        manifest_sha256: str="",result: str="",
    ) -> dict[str,object]:
        return {
            "schema_version":RECOVERY_SCHEMA_VERSION,
            "kind":"collectigen_training_package_recovery",
            "operation_id":operation_id,"status":status,
            "destination_name":plan.destination.name,"staging_name":stage.name,
            "intent_fingerprint":plan.intent_fingerprint,
            "candidate_plan_fingerprint":plan.candidate_manifest["plan_fingerprint"],
            "manifest_sha256":manifest_sha256,"result":result,
            "destination_parent_state":dict(plan.destination_parent_state),
        }

    @staticmethod
    def _cleanup_owned_stage(stage: Path,operation_id: str,destination_name: str) -> bool:
        if not _lexists(stage):
            return True
        expected=f".{destination_name}.collectigen-{operation_id}.staging"
        if (
            stage.name!=expected or _is_redirected(stage)
            or _has_redirected_component(stage.parent)
        ):
            return False
        marker=stage/".collectigen-owned-staging.json"
        if not _lexists(marker):
            try:
                if stage.is_dir() and not next(stage.iterdir(),None):
                    stage.rmdir()
                    return not _lexists(stage)
            except OSError:
                return False
            return False
        try:
            payload=json.loads(marker.read_text(encoding="utf-8"))
        except (OSError,UnicodeError,json.JSONDecodeError):
            return False
        if payload!=TrainingPackageService._marker_payload(operation_id,destination_name):
            return False
        try:
            if any(_is_redirected(path) for path in stage.rglob("*")):
                return False
        except OSError:
            return False
        try:
            shutil.rmtree(stage)
        except OSError:
            return False
        return not _lexists(stage)

    def apply(
        self,plan: TrainingPackagePlan,execution: ExternalWriteExecution,* ,
        progress: ProgressCallback|None=None,should_cancel: CancelCallback|None=None,
    ) -> TrainingPackageResult:
        if plan.collection_version is None:
            return self._apply_admitted(plan,execution,progress=progress,should_cancel=should_cancel)
        try:
            if (not callable(plan.collection_guard)
                    or plan.collection_version.candidate_plan_fingerprint!=plan.candidate_manifest.get("plan_fingerprint")
                    or plan.intent_fingerprint!=_stable_hash(self._intent_payload(
                        plan.candidate_manifest,plan.candidate_plan.configuration,plan.items,
                        plan.generated_files,plan.collection_version))):
                raise ValueError("The Collection binding or exact package intent changed after preview.")
            if should_cancel and should_cancel():
                return TrainingPackageResult("cancelled",plan.destination)
            with plan.collection_guard():
                return self._apply_admitted(plan,execution,progress=progress,should_cancel=should_cancel)
        except InterruptedError:
            return TrainingPackageResult("cancelled",plan.destination)
        except Exception as exc:
            return TrainingPackageResult("failed",plan.destination,issues=(str(exc),))

    def _apply_admitted(
        self,plan: TrainingPackagePlan,execution: ExternalWriteExecution,*,
        progress: ProgressCallback|None=None,should_cancel: CancelCallback|None=None,
    ) -> TrainingPackageResult:
        notify=progress or (lambda *_:None); cancel=should_cancel or (lambda:False)
        validation=external_write_validation_issue(plan.decision,execution)
        if validation:
            return TrainingPackageResult(
                "failed",plan.destination,issues=(
                    f"External-write approval is invalid ({validation}); preview again.",
                ),
            )
        if not plan.ready:
            return TrainingPackageResult(
                "failed",plan.destination,issues=tuple(
                    issue.summary for issue in plan.issues if issue.level=="blocking"
                ) or ("The package plan is not ready.",),
            )
        guard=self._destination_guard_issues(
            plan,allow_destination=plan.destination_disposition=="current",
        )
        if guard:
            return TrainingPackageResult(
                "failed",plan.destination,issues=guard,
            )
        try:
            pending_recovery=self.recovery_receipts(
                plan.destination.parent,destination_name=plan.destination.name,
            )
        except (OSError,ValueError) as exc:
            return TrainingPackageResult(
                "failed",plan.destination,issues=(
                    f"Existing package recovery state could not be checked: {exc}",
                ),
            )
        if pending_recovery:
            return TrainingPackageResult(
                "recovery_required",plan.destination,
                recovery_receipt=pending_recovery[-1],issues=(
                    "Recover or inspect the existing package operation before starting another publication.",
                ),
            )
        if plan.destination_disposition=="current":
            if cancel():
                return TrainingPackageResult("cancelled",plan.destination)
            try:
                current=self._candidate_plan_is_current(
                    plan.candidate_plan,plan.candidate_manifest,
                    should_cancel=cancel,
                )
            except InterruptedError:
                return TrainingPackageResult("cancelled",plan.destination)
            except Exception as exc:
                return TrainingPackageResult(
                    "failed",plan.destination,issues=(
                        f"Current source evidence could not be revalidated: {exc}",
                    ),
                )
            if not current:
                return TrainingPackageResult(
                    "failed",plan.destination,issues=(
                        "The source or plan evidence changed after preview.",
                    ),
                )
            try:
                verification=self.verify(
                    plan.destination,expected_intent=plan.intent_fingerprint,
                    should_cancel=cancel,
                )
            except InterruptedError:
                return TrainingPackageResult("cancelled",plan.destination)
            if not verification.valid:
                return TrainingPackageResult(
                    "failed",plan.destination,verification=verification,
                    issues=verification.issues or (
                        "The existing package failed final verification.",
                    ),
                )
            return TrainingPackageResult(
                "current",plan.destination,verification.artifact_count,
                plan.destination/MANIFEST_NAME,None,verification,
                verification.issues,
            )
        if cancel():
            return TrainingPackageResult("cancelled",plan.destination)
        try:
            if not self._candidate_plan_is_current(
                plan.candidate_plan,plan.candidate_manifest,
                should_cancel=cancel,
            ):
                return TrainingPackageResult(
                    "failed",plan.destination,issues=(
                        "The source, Image Knowledge, caption projection, permission evidence, profile, role, or settings changed after preview.",
                    ),
                )
        except InterruptedError:
            return TrainingPackageResult("cancelled",plan.destination)
        except Exception as exc:
            return TrainingPackageResult(
                "failed",plan.destination,issues=(f"Current source evidence could not be revalidated: {exc}",),
            )
        if _lexists(plan.destination):
            return TrainingPackageResult(
                "failed",plan.destination,issues=(
                    "The package destination became occupied after preview; nothing was overwritten.",
                ),
            )
        operation_id=uuid4().hex
        stage=self._stage_path(plan.destination,operation_id)
        receipt=self._receipt_path(plan.destination,operation_id)
        recovery=self._recovery_payload(plan,operation_id,stage,"prepared")
        try:
            guard=self._destination_guard_issues(plan)
            if guard:
                raise RuntimeError(" ".join(guard))
            write_verified_json(receipt,recovery,label="Training package recovery receipt")
            guard=self._destination_guard_issues(plan)
            if guard:
                raise RuntimeError(" ".join(guard))
            stage.mkdir(exist_ok=False)
            if _is_redirected(stage):
                raise RuntimeError("The package staging folder is redirected.")
            write_verified_json(
                stage/".collectigen-owned-staging.json",
                self._marker_payload(operation_id,plan.destination.name),
                label="Training package staging marker",
            )
            recovery=self._recovery_payload(plan,operation_id,stage,"staging")
            write_verified_json(receipt,recovery,label="Training package recovery receipt")
            staged,completed=self._write_stage(
                plan,stage,progress=notify,should_cancel=cancel,
            )
            manifest=self._manifest_payload(plan,staged["artifacts"])
            manifest_bytes=_json_bytes(manifest)
            guard=self._destination_guard_issues(plan)
            if guard:
                raise RuntimeError(" ".join(guard))
            manifest_path=_write_new_package_bytes(
                stage,MANIFEST_NAME,manifest_bytes,
            )
            notify(completed+1,len(plan.items)*2+6,MANIFEST_NAME)
            verification=self.verify(
                stage,expected_intent=plan.intent_fingerprint,
                allow_marker=True,should_cancel=cancel,
            )
            if not verification.valid:
                raise RuntimeError("Staged package verification failed: "+" ".join(verification.issues))
            if cancel():
                raise InterruptedError("Training package creation was cancelled.")
            if not self._candidate_plan_is_current(
                plan.candidate_plan,plan.candidate_manifest,
                should_cancel=cancel,
            ):
                raise RuntimeError("Source or plan evidence changed during package staging.")
            guard=self._destination_guard_issues(plan)
            if guard:
                raise RuntimeError(" ".join(guard))
            marker=stage/".collectigen-owned-staging.json"; marker.unlink()
            recovery=self._recovery_payload(
                plan,operation_id,stage,"staged_complete",
                manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            )
            write_verified_json(receipt,recovery,label="Training package recovery receipt")
            if cancel():
                raise InterruptedError("Training package creation was cancelled.")
            guard=self._destination_guard_issues(plan)
            if guard:
                raise RuntimeError(" ".join(guard))
            _rename_directory_no_replace(stage,plan.destination)
            recovery=self._recovery_payload(
                plan,operation_id,stage,"promoted",
                manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            )
            write_verified_json(receipt,recovery,label="Training package recovery receipt")
            verification=self.verify(
                plan.destination,expected_intent=plan.intent_fingerprint,
                should_cancel=cancel,
            )
            if not verification.valid:
                raise RuntimeError("Published package verification failed: "+" ".join(verification.issues))
            recovery=self._recovery_payload(
                plan,operation_id,stage,"complete",
                manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                result=verification.package_fingerprint,
            )
            write_verified_json(receipt,recovery,label="Training package recovery receipt")
            return TrainingPackageResult(
                "created",plan.destination,verification.artifact_count,
                plan.destination/MANIFEST_NAME,receipt,verification,
            )
        except InterruptedError as exc:
            cleaned=self._cleanup_owned_stage(stage,operation_id,plan.destination.name)
            recovery=self._recovery_payload(
                plan,operation_id,stage,"cancelled" if cleaned else "attention_required",
                result=str(exc),
            )
            try:
                write_verified_json(receipt,recovery,label="Training package recovery receipt")
            except Exception:
                pass
            return TrainingPackageResult(
                "cancelled" if cleaned else "recovery_required",plan.destination,
                recovery_receipt=receipt,issues=(str(exc),),
            )
        except Exception as exc:
            if _lexists(plan.destination):
                verification=self.verify(plan.destination,expected_intent=plan.intent_fingerprint)
                if verification.valid:
                    return TrainingPackageResult(
                        "recovery_required",plan.destination,verification.artifact_count,
                        plan.destination/MANIFEST_NAME,receipt,verification,
                        (f"The package is verified but completion bookkeeping failed: {exc}",),
                    )
            if stage.is_dir() and not _lexists(
                stage/".collectigen-owned-staging.json"
            ):
                verification=self.verify(
                    stage,expected_intent=plan.intent_fingerprint,
                )
                if verification.valid:
                    return TrainingPackageResult(
                        "recovery_required",plan.destination,
                        verification.artifact_count,stage/MANIFEST_NAME,
                        receipt,verification,(str(exc),),
                    )
            cleaned=self._cleanup_owned_stage(stage,operation_id,plan.destination.name)
            recovery=self._recovery_payload(
                plan,operation_id,stage,"rolled_back" if cleaned else "attention_required",
                result=str(exc),
            )
            try:
                write_verified_json(receipt,recovery,label="Training package recovery receipt")
            except Exception:
                pass
            return TrainingPackageResult(
                "failed" if cleaned else "recovery_required",plan.destination,
                recovery_receipt=receipt,issues=(str(exc),),
            )

    def verify(
        self,destination: str|Path,*,expected_intent: str="",
        allow_marker: bool=False,should_cancel: CancelCallback|None=None,
        limits: TrainingPackageVerificationLimits|None=None,
        expected_package_fingerprint: str="",
    ) -> TrainingPackageVerification:
        try:
            return self._verify_package(
                Path(destination).expanduser().absolute(),
                expected_intent=expected_intent,allow_marker=allow_marker,
                should_cancel=should_cancel,limits=limits,
                expected_package_fingerprint=expected_package_fingerprint,
            )
        except InterruptedError:
            raise
        except Exception as exc:
            root=Path(destination).expanduser().absolute()
            return TrainingPackageVerification(
                root,False,issues=(f"The package failed closed during verification: {exc}",),
            )

    def _verify_package(
        self,root: Path,*,expected_intent: str="",allow_marker: bool=False,
        should_cancel: CancelCallback|None=None,limits: TrainingPackageVerificationLimits|None=None,
        expected_package_fingerprint: str="",
    ) -> TrainingPackageVerification:
        cancel=should_cancel or (lambda:False)
        def checkpoint() -> None:
            if cancel():
                raise InterruptedError("Training package verification was cancelled.")
        checkpoint()
        if (
            not root.is_dir() or _is_redirected(root)
            or _has_redirected_component(root)
        ):
            raise ValueError(
                "The package directory is missing, not a directory, or redirected."
            )
        checkpoint()
        manifest_path=root/MANIFEST_NAME
        if _is_redirected(manifest_path):
            raise ValueError("The package manifest is redirected.")
        sources=[]
        if limits:
            manifest_bytes, signature = _inspection_read(manifest_path, min(limits.manifest_bytes,limits.total_bytes), cancel)
            sources.append((str(manifest_path), signature))
        else:
            manifest_bytes=manifest_path.read_bytes()
        checkpoint()
        manifest=json.loads(manifest_bytes.decode("utf-8"))
        checkpoint()
        schema=manifest.get("schema_version")
        bound=schema==BOUND_PACKAGE_SCHEMA_VERSION
        _require_fields(manifest,MANIFEST_FIELDS|({"collection_version"} if bound else set()),"The package manifest")
        if type(schema) is not int or schema not in {PACKAGE_SCHEMA_VERSION,BOUND_PACKAGE_SCHEMA_VERSION} or manifest.get(
            "kind"
        )!="collectigen_training_package":
            raise ValueError("The package manifest kind or schema version is unsupported.")
        if manifest_bytes!=_json_bytes(manifest):
            raise ValueError("The package manifest is not in its deterministic encoding.")
        checkpoint()
        recorded=manifest.get("package_fingerprint")
        if not _is_digest(recorded):
            raise ValueError("The package manifest fingerprint is invalid.")
        unsigned=dict(manifest); unsigned.pop("package_fingerprint")
        if recorded!=_stable_hash(unsigned):
            raise ValueError("The package manifest fingerprint does not match its contents.")
        if expected_package_fingerprint and recorded != expected_package_fingerprint:
            raise ValueError("The chosen package does not match the recorded package fingerprint.")
        checkpoint()
        _assert_source_neutral(manifest,should_cancel=cancel)
        checkpoint()
        intent=str(manifest.get("intent_fingerprint") or "")
        if not _is_digest(intent) or expected_intent and intent!=expected_intent:
            raise ValueError("The package intent differs from the previewed plan.")

        if limits:
            raw_candidates=manifest.get("candidate_plan",{}).get("candidates",[])
            raw_artifacts=manifest.get("artifacts",[])
            if len(raw_candidates)>limits.candidates or len(raw_artifacts)>limits.artifacts:
                raise ValueError("Package inspection candidate or artifact limit exceeded.")
        candidate_manifest=validate_training_plan_manifest(
            manifest.get("candidate_plan"),should_cancel=cancel,
        )
        checkpoint()
        if int(candidate_manifest.get("schema_version") or 0) not in {4,5,6} or set(
            candidate_manifest
        )!={
            "schema_version","kind","goal","selection_scope","profile",
            "profile_provenance","counts","candidates","issues",
            "dataset_analysis","permission_review","training_configuration",
            "training_configuration_issues","package_ready","limitations",
            "cancelled","plan_fingerprint",
        }:
            raise ValueError("The embedded candidate plan has unsupported fields or version.")
        configuration=TrainingConfiguration.from_dict(
            manifest.get("training_configuration")
        )
        configuration_payload=configuration.to_dict()
        if configuration_payload!=manifest.get("training_configuration"):
            raise ValueError("The training configuration is not canonical.")
        package_plan=_require_fields(
            manifest.get("package_plan"),PACKAGE_PLAN_FIELDS|({"collection_version"} if bound else set()),
            "The exact package plan",
        )
        if intent!=_stable_hash(package_plan):
            raise ValueError("The package intent fingerprint does not match its plan.")
        if (
            package_plan.get("schema_version")!=schema
            or package_plan.get("candidate_plan_fingerprint")
                !=candidate_manifest.get("plan_fingerprint")
            or package_plan.get("training_configuration")!=configuration_payload
            or package_plan.get("adapter")!=configuration_payload["adapter"]
            or manifest.get("adapter")!=configuration_payload["adapter"]
        ):
            raise ValueError("The package plan, candidate plan, settings, or adapter differ.")
        if bound:
            binding=TrainingCollectionBinding.from_dict(manifest.get("collection_version"))
            if (binding.to_dict()!=package_plan.get("collection_version")
                    or binding.candidate_plan_fingerprint!=candidate_manifest.get("plan_fingerprint")):
                raise ValueError("The Collection binding differs from the exact package intent.")

        source_counts=_require_fields(
            candidate_manifest.get("counts"),{
                "included","excluded","training","validation","holdout",
                "blocking","not_processed",
            },"The candidate counts",
        )
        permission=candidate_manifest.get("permission_review")
        strict_configuration=validate_training_configuration(
            configuration,
            profile_family=str(candidate_manifest["profile"].get("target_model_family") or ""),
            profile_version=str(candidate_manifest["profile"].get("target_model_version") or ""),
            require_base_model=True,
        )
        if (
            candidate_manifest.get("package_ready") is not True
            or candidate_manifest.get("cancelled") is not False
            or source_counts.get("training",0)<1 or source_counts.get("blocking")!=0
            or not isinstance(permission,dict) or permission.get("status")!="proceed"
            or not str(permission.get("reason") or "").strip()
            or any(issue.level=="blocking" for issue in strict_configuration)
        ):
            raise ValueError("The embedded candidate plan does not satisfy the package gate.")

        candidates=candidate_manifest.get("candidates")
        if not isinstance(candidates,list):
            raise ValueError("The candidate inventory is invalid.")
        candidate_by_id={
            str(item.get("stable_id") or ""):item
            for item in candidates if isinstance(item,dict)
        }
        included={
            stable_id:item for stable_id,item in candidate_by_id.items()
            if item.get("decision")=="include"
        }
        planned_items=package_plan.get("items")
        if not isinstance(planned_items,list) or len(planned_items)!=len(included):
            raise ValueError("The package plan does not preserve every included candidate.")
        planned_by_image={}; planned_by_caption={}; planned_ids=set()
        for item_value in planned_items:
            if cancel():
                raise InterruptedError("Training package verification was cancelled.")
            item=_require_fields(
                item_value,PACKAGE_ITEM_FIELDS,"A package-plan item",
            )
            stable_id=str(item.get("stable_id") or "")
            candidate=included.get(stable_id)
            role=str(item.get("role") or "")
            artifact_id=str(item.get("artifact_id") or "")
            if (
                candidate is None or stable_id in planned_ids
                or role not in {"training","validation","holdout"}
                or candidate.get("role")!=role
                or artifact_id!=self._artifact_id(stable_id)
            ):
                raise ValueError("A package-plan item has invalid identity or role linkage.")
            planned_ids.add(stable_id)
            image_path=_safe_relative(str(item.get("image_path") or ""))
            caption_path=_safe_relative(str(item.get("caption_path") or ""))
            transform=_require_fields(
                item.get("transform"),TRANSFORM_PLAN_FIELDS,
                f"The transform plan for {artifact_id}",
            )
            expected_image=f"images/{role}/{artifact_id}.{transform.get('output_extension')}"
            expected_caption=f"images/{role}/{artifact_id}.txt"
            source_state=candidate.get("source_state")
            source_file=(
                source_state.get("file") if isinstance(source_state,dict) else None
            )
            caption=str(item.get("caption") or "")
            canonical=str(candidate.get("caption") or "")
            if (
                image_path!=expected_image or caption_path!=expected_caption
                or image_path.casefold() in planned_by_image
                or caption_path.casefold() in planned_by_caption
                or item.get("caption_variant_policy")!="canonical_only"
                or not caption or caption!=package_caption(canonical,configuration)
                or item.get("caption_sha256")
                    !=hashlib.sha256(caption.encode("utf-8")).hexdigest()
                or item.get("effective_exposures")
                    !=configuration.repeats*configuration.epochs
                or transform.get("stable_id")!=stable_id
                or transform.get("source_state")!={"exists":True,**source_file}
                or transform.get("resize_applied") is not False
                or transform.get("crop_applied") is not False
            ):
                raise ValueError(f"The exact package plan is inconsistent for {artifact_id}.")
            planned_by_image[image_path.casefold()]=(image_path,item,candidate)
            planned_by_caption[caption_path.casefold()]=(caption_path,item,candidate)
        if planned_ids!=set(included):
            raise ValueError("The package plan contains the wrong included identities.")

        expected_generated=sorted(
            [value[0] for value in planned_by_image.values()]
            +[value[0] for value in planned_by_caption.values()]
            +[f"splits/{role}.json" for role in ("training","validation","holdout")]
            +[CONFIG_PATH,README_PATH,MANIFEST_NAME],
            key=str.casefold,
        )
        if package_plan.get("generated_files")!=expected_generated:
            raise ValueError("The generated-file preview differs from the exact package plan.")

        split_payloads={
            role:{
                "schema_version":1,"kind":"collectigen_training_package_split",
                "role":role,"intent_fingerprint":intent,
                "items":[{
                    "stable_id":item[1]["stable_id"],
                    "artifact_id":item[1]["artifact_id"],
                    "image":item[1]["image_path"],
                    "caption":item[1]["caption_path"],
                    "effective_exposures":item[1]["effective_exposures"],
                } for item in planned_by_image.values() if item[1]["role"]==role],
            }
            for role in ("training","validation","holdout")
        }
        config_payload={
            "schema_version":1,"kind":"collectigen_manual_training_package_config",
            "intent_fingerprint":intent,
            "training_configuration":configuration_payload,
            "translation":{
                "status":"manual_handoff_unverified","named_trainer":None,
                "unsupported_or_unverified":[
                    "trainer CLI/config translation","exact tokenizer counts",
                    "base-model compatibility","GPU/runtime compatibility",
                    "checkpoint/resume behavior","output registration",
                ],
            },
        }
        control_payloads={
            CONFIG_PATH:_json_bytes(config_payload),
            **{
                f"splits/{role}.json":_json_bytes(payload)
                for role,payload in split_payloads.items()
            },
            README_PATH:self._readme_from_manifest(
                candidate_manifest,configuration,
            ).encode("utf-8"),
        }

        artifacts=manifest.get("artifacts")
        if not isinstance(artifacts,list):
            raise ValueError("The package artifact inventory is missing.")
        artifact_by_path={}; role_members={role:[] for role in ("training","validation","holdout")}
        inspected_bytes=len(manifest_bytes)
        for artifact_value in artifacts:
            if cancel():
                raise InterruptedError("Training package verification was cancelled.")
            if not isinstance(artifact_value,dict):
                raise ValueError("A package artifact record is invalid.")
            kind=str(artifact_value.get("kind") or "")
            fields=(
                IMAGE_ARTIFACT_FIELDS if kind=="image" else
                CAPTION_ARTIFACT_FIELDS if kind=="caption" else
                CONTROL_ARTIFACT_FIELDS if kind in {"configuration","split","handoff"}
                else set()
            )
            if not fields:
                raise ValueError(f"The package has an unsupported artifact kind: {kind or 'empty'}.")
            artifact=_require_fields(artifact_value,fields,"A package artifact")
            relative=_safe_relative(str(artifact.get("path") or ""))
            if relative.casefold() in artifact_by_path:
                raise ValueError(f"The package repeats artifact path {relative}.")
            artifact_by_path[relative.casefold()]=artifact
            target=_child(root,relative)
            if not target.is_file() or _is_redirected(target):
                raise ValueError(f"Artifact {relative} is missing or redirected.")
            if limits:
                remaining=limits.total_bytes-inspected_bytes
                if remaining < 0:
                    raise ValueError("Package inspection total byte limit exceeded.")
                inspected_content,signature=_inspection_read(target,min(limits.artifact_bytes,remaining),cancel)
                inspected_bytes += len(inspected_content)
                fingerprint={"size":len(inspected_content),"sha256":hashlib.sha256(inspected_content).hexdigest()}
                sources.append((str(target),signature))
            else:
                fingerprint=file_fingerprint(target)
            if (
                fingerprint.get("size")!=artifact.get("size")
                or fingerprint.get("sha256")!=artifact.get("sha256")
                or not _is_digest(artifact.get("sha256"))
            ):
                raise ValueError(f"Artifact {relative} does not match its recorded hash and size.")
            if kind=="image":
                planned=planned_by_image.get(relative.casefold())
                if planned is None:
                    raise ValueError(f"Image {relative} was not in the package preview.")
                _path,item,candidate=planned
                transform=_require_fields(
                    artifact.get("transform"),IMAGE_TRANSFORM_FIELDS,
                    f"The result transform for {relative}",
                )
                planned_transform=item["transform"]
                if (
                    artifact.get("role")!=item["role"]
                    or artifact.get("stable_id")!=item["stable_id"]
                    or artifact.get("artifact_id")!=item["artifact_id"]
                    or any(transform.get(key)!=value for key,value in planned_transform.items())
                    or transform.get("output_sha256")!=artifact.get("sha256")
                    or transform.get("output_size")!=artifact.get("size")
                    or transform.get("output_dimensions")!=transform.get("expected_dimensions")
                    or transform.get("output_mode")!=transform.get("expected_mode")
                ):
                    raise ValueError(f"Image {relative} differs from its declared transform.")
                with Image.open(io.BytesIO(inspected_content) if limits else target) as opened:
                    if limits and opened.width * opened.height > limits.image_pixels:
                        raise ValueError("Package inspection image pixel limit exceeded.")
                    opened.load()
                    decoded=(
                        list(opened.size),opened.mode,str(opened.format or "").upper(),
                        int(getattr(opened,"n_frames",1) or 1),
                        bool(opened.info.get("icc_profile")),
                    )
                if decoded[:4]!=(
                    transform["output_dimensions"],transform["output_mode"],
                    str(transform["output_format"]).upper(),1,
                ):
                    raise ValueError(f"Image {relative} does not match its decoded declaration.")
                source_pixels=candidate.get("source_state",{}).get("pixels",{})
                operations=transform.get("operations")
                if not isinstance(operations,list):
                    raise ValueError(f"Image {relative} has invalid transform operations.")
                if (
                    source_pixels.get("has_alpha")
                    and "A" not in str(transform.get("output_mode") or "")
                ):
                    raise ValueError(f"Image {relative} lost declared alpha data.")
                if (
                    "preserve_compatible_color_profile" in operations
                    or "preserve_color_profile_in_source_bytes" in operations
                ) and not decoded[4]:
                    raise ValueError(f"Image {relative} lost its declared color profile.")
                role_members[item["role"]].append(item["stable_id"])
            elif kind=="caption":
                planned=planned_by_caption.get(relative.casefold())
                if planned is None:
                    raise ValueError(f"Caption {relative} was not in the package preview.")
                _path,item,candidate=planned
                payload=(str(item["caption"])+"\n").encode("utf-8")
                expected={
                    "path":relative,"kind":"caption","role":item["role"],
                    "stable_id":item["stable_id"],"artifact_id":item["artifact_id"],
                    "size":len(payload),"sha256":hashlib.sha256(payload).hexdigest(),
                    "canonical_caption":candidate.get("caption"),
                    "canonical_caption_sha256":candidate.get("caption_sha256"),
                    "package_caption":item["caption"],
                    "caption_policy":configuration.caption_prefix_policy,
                    "variant_policy":"canonical_only",
                    "tokenizer_status":"unavailable_for_manual_adapter",
                    "requested_token_limit":configuration.max_caption_tokens,
                    "character_count":len(str(item["caption"])),
                    "whitespace_piece_count":len(str(item["caption"]).split()),
                }
                actual = inspected_content if limits else target.read_bytes()
                if artifact!=expected or actual!=payload:
                    raise ValueError(f"Caption {relative} differs from its exact preview.")
            else:
                expected_payload=control_payloads.get(relative)
                expected_kind=(
                    "configuration" if relative==CONFIG_PATH else
                    "split" if relative.startswith("splits/") else
                    "handoff" if relative==README_PATH else ""
                )
                expected_role=(
                    PurePosixPath(relative).stem if expected_kind=="split" else ""
                )
                if (
                    expected_payload is None or kind!=expected_kind
                    or artifact.get("role")!=expected_role
                    or (inspected_content if limits else target.read_bytes())!=expected_payload
                ):
                    raise ValueError(f"Control artifact {relative} differs from its exact preview.")

        expected_artifacts={path.casefold() for path in expected_generated if path!=MANIFEST_NAME}
        if set(artifact_by_path)!=expected_artifacts:
            raise ValueError("The package artifact inventory has missing or extra paths.")
        if artifacts!=sorted(artifacts,key=lambda item:str(item["path"]).casefold()):
            raise ValueError("The package artifact inventory is not deterministically ordered.")
        counts=_require_fields(
            manifest.get("counts"),{
                "included_images","excluded_images","training","validation",
                "holdout","artifacts",
            },"The package counts",
        )
        expected_counts={
            "included_images":len(included),
            "excluded_images":int(source_counts["excluded"]),
            "training":len(role_members["training"]),
            "validation":len(role_members["validation"]),
            "holdout":len(role_members["holdout"]),
            "artifacts":len(artifacts),
        }
        if counts!=expected_counts:
            raise ValueError("The package artifact and role counts are inconsistent.")
        excluded=[{
            "stable_id":item.get("stable_id"),"role":item.get("role"),
            "reason":item.get("decision_reason"),
            "user_override":item.get("user_override"),
        } for item in candidates if item.get("decision")=="exclude"]
        expected_lineage={
            "base_model_id":configuration.base_model_id,
            "candidate_plan_fingerprint":candidate_manifest["plan_fingerprint"],
            "profile":candidate_manifest["profile_provenance"],
            "future_output_registration_required":True,
            "trained_output_created":False,
        }
        expected_limitations=[
            *candidate_manifest["limitations"],
            "Named trainer translation and execution are outside this manual package.",
            "Exact tokenizer effects are unavailable; whitespace pieces are a labelled proxy only.",
            "Caption variants are not supported; each image has one canonical package caption.",
        ]
        if (
            manifest.get("excluded")!=excluded
            or manifest.get("expected_output")!=configuration_payload["expected_output"]
            or manifest.get("lineage")!=expected_lineage
            or manifest.get("limitations")!=expected_limitations
        ):
            raise ValueError("The package lineage, exclusions, output, or limitations are inconsistent.")

        files,redirects=_package_files(root,limits=limits,should_cancel=cancel if limits else None)
        if redirects:
            raise ValueError("The package contains redirected or unsupported entries: "+", ".join(redirects))
        expected_files=set(expected_generated)
        marker_path=".collectigen-owned-staging.json"
        if allow_marker and marker_path in files:
            marker=json.loads((root/marker_path).read_text(encoding="utf-8"))
            _require_fields(
                marker,{"schema_version","kind","operation_id","destination_name"},
                "The staging marker",
            )
            if (
                marker.get("schema_version")!=1
                or marker.get("kind")!="collectigen_training_package_staging"
                or not re.fullmatch(r"[a-f0-9]{32}",str(marker.get("operation_id") or ""))
                or root.name!=f".{marker.get('destination_name')}.collectigen-{marker.get('operation_id')}.staging"
            ):
                raise ValueError("The staging marker does not own this package directory.")
            expected_files.add(marker_path)
        if set(files)!=expected_files:
            raise ValueError("The package file tree has missing or unrecorded artifacts.")
        for path,signature in sources:
            checkpoint()
            if _inspection_signature(Path(path)) != signature:
                raise ValueError("Package changed before inspection completed.")
        return TrainingPackageVerification(
            root,True,str(recorded),intent,len(artifacts),manifest,(),tuple(sources),
        )

    def compare(
        self,first: str|Path,second: str|Path,*,
        should_cancel: CancelCallback|None=None,
    ) -> TrainingPackageComparison:
        cancel=should_cancel or (lambda:False)
        before=self.verify(first,should_cancel=cancel)
        after=self.verify(second,should_cancel=cancel)
        differences=[]
        if cancel():
            raise InterruptedError("Training package comparison was cancelled.")
        if not before.valid:
            differences.append("The first package is invalid: "+" ".join(before.issues))
        if not after.valid:
            differences.append("The second package is invalid: "+" ".join(after.issues))
        if before.valid and after.valid and before.package_fingerprint!=after.package_fingerprint:
            first_manifest=before.manifest; second_manifest=after.manifest
            if first_manifest.get("intent_fingerprint")!=second_manifest.get("intent_fingerprint"):
                differences.append("The package intent fingerprint differs.")
            if first_manifest.get("candidate_plan",{}).get("plan_fingerprint")!=second_manifest.get("candidate_plan",{}).get("plan_fingerprint"):
                differences.append("The source plan, evidence, captions, roles, or permission disposition differs.")
            if first_manifest.get("training_configuration")!=second_manifest.get("training_configuration"):
                differences.append("The target or adapter-neutral training settings differ.")
            for key,label in (
                ("collection_version","The exact Collection version binding differs."),
                ("adapter","The package adapter identity or compatibility declaration differs."),
                ("expected_output","The expected output declaration differs."),
                ("lineage","The base-model, plan, profile, or output-lineage declaration differs."),
                ("excluded","The excluded-image records differ."),
                ("counts","The artifact, exclusion, or role counts differ."),
                ("limitations","The recorded package limitations differ."),
            ):
                if first_manifest.get(key)!=second_manifest.get(key):
                    differences.append(label)
            first_plan=first_manifest.get("package_plan",{})
            second_plan=second_manifest.get("package_plan",{})
            if first_plan.get("generated_files")!=second_plan.get("generated_files"):
                differences.append("The generated package-file topology differs.")
            first_items={
                str(item.get("stable_id") or ""):item
                for item in first_plan.get("items",[]) if isinstance(item,dict)
            }
            second_items={
                str(item.get("stable_id") or ""):item
                for item in second_plan.get("items",[]) if isinstance(item,dict)
            }
            for stable_id in sorted(set(first_items)|set(second_items)):
                if cancel():
                    raise InterruptedError("Training package comparison was cancelled.")
                left=first_items.get(stable_id); right=second_items.get(stable_id)
                if left is None or right is None:
                    differences.append(
                        f"Package item membership differs for {stable_id}."
                    )
                elif left!=right:
                    changed=sorted(
                        key for key in set(left)|set(right)
                        if left.get(key)!=right.get(key)
                    )
                    differences.append(
                        f"Package item {stable_id} differs in: {', '.join(changed)}."
                    )
            first_artifacts={item["path"]:item for item in first_manifest.get("artifacts",[]) if isinstance(item,dict) and "path" in item}
            second_artifacts={item["path"]:item for item in second_manifest.get("artifacts",[]) if isinstance(item,dict) and "path" in item}
            for path in sorted(set(first_artifacts)|set(second_artifacts),key=str.casefold):
                if cancel():
                    raise InterruptedError("Training package comparison was cancelled.")
                left=first_artifacts.get(path); right=second_artifacts.get(path)
                if left is None or right is None:
                    differences.append(f"Artifact topology differs at {path}.")
                elif left.get("sha256")!=right.get("sha256"):
                    detail=""
                    if left.get("kind")=="image":
                        first_engine=(left.get("transform") or {}).get("engine_version")
                        second_engine=(right.get("transform") or {}).get("engine_version")
                        if first_engine!=second_engine:
                            detail=f" (transform engine {first_engine!r} vs {second_engine!r})"
                    differences.append(f"Artifact bytes differ at {path}{detail}.")
                elif left!=right:
                    changed=sorted(
                        key for key in set(left)|set(right)
                        if left.get(key)!=right.get(key)
                    )
                    differences.append(
                        f"Artifact provenance differs at {path}: {', '.join(changed)}."
                    )
            if not differences:
                differences.append(
                    "The deterministic manifest encoding differs outside its derived fingerprints."
                )
        if cancel():
            raise InterruptedError("Training package comparison was cancelled.")
        identical=bool(
            before.valid and after.valid
            and before.package_fingerprint==after.package_fingerprint
        )
        return TrainingPackageComparison(identical,before,after,tuple(differences))

    @staticmethod
    def _read_recovery_receipt(
        receipt: Path,
    ) -> tuple[dict[str,object],Path,Path]:
        parent=receipt.parent
        if (
            not receipt.is_file() or _is_redirected(receipt)
            or _has_redirected_component(parent)
        ):
            raise ValueError("The recovery receipt is unavailable or redirected.")
        payload=json.loads(receipt.read_text(encoding="utf-8"))
        _require_fields(payload,RECOVERY_FIELDS,"The recovery receipt")
        operation_id=str(payload.get("operation_id") or "")
        status=str(payload.get("status") or "")
        if (
            payload.get("schema_version")!=RECOVERY_SCHEMA_VERSION
            or payload.get("kind")!="collectigen_training_package_recovery"
            or not re.fullmatch(r"[a-f0-9]{32}",operation_id)
            or status not in RECOVERY_ACTIVE_STATUSES|RECOVERY_TERMINAL_STATUSES
            or not _is_digest(payload.get("intent_fingerprint"))
            or not _is_digest(payload.get("candidate_plan_fingerprint"))
        ):
            raise ValueError("The recovery receipt has an unsupported identity or state.")
        destination_name=_safe_name(payload.get("destination_name"))
        staging_name=_safe_name(payload.get("staging_name"))
        expected_stage=f".{destination_name}.collectigen-{operation_id}.staging"
        expected_receipt=f".{destination_name}.collectigen-{operation_id}.recovery.json"
        if staging_name!=expected_stage or receipt.name!=expected_receipt:
            raise ValueError("The recovery names do not match the operation.")
        parent_state=_require_fields(
            payload.get("destination_parent_state"),{
                "resolved_sha256","device","inode","mode","attributes",
            },"The recovery parent identity",
        )
        if not _is_digest(parent_state.get("resolved_sha256")):
            raise ValueError("The recovery parent identity is empty.")
        manifest_sha=str(payload.get("manifest_sha256") or "")
        if status in {"staged_complete","promoted","complete"}:
            if not _is_digest(manifest_sha):
                raise ValueError("The recovery receipt has no valid manifest digest.")
        elif manifest_sha:
            raise ValueError("The recovery receipt claims a manifest in the wrong phase.")
        result=str(payload.get("result") or "")
        if status=="complete" and not _is_digest(result):
            raise ValueError("The completed recovery receipt has no package fingerprint.")
        return payload,parent/destination_name,parent/staging_name

    @staticmethod
    def recovery_receipts(
        parent: str|Path,*,destination_name: str="",statuses: Iterable[str]=(
            "prepared","staging","staged_complete","promoted","attention_required",
        ),
    ) -> tuple[Path,...]:
        return tuple(choice.receipt for choice in TrainingPackageService.recovery_choices(
            parent,destination_name=destination_name,statuses=statuses,
        ))

    @staticmethod
    def recovery_choices(
        parent: str|Path,*,destination_name: str="",statuses: Iterable[str]=(
            "prepared","staging","staged_complete","promoted","attention_required",
        ),should_cancel: CancelCallback|None=None,
    ) -> tuple[TrainingRecoveryChoice,...]:
        """List only validated active receipts in one explicitly selected parent."""
        root=Path(parent).expanduser().absolute()
        if not root.is_dir() or _is_redirected(root):
            return ()
        accepted=set(statuses); values=[]
        pattern=(
            f".{_safe_name(destination_name)}.collectigen-*.recovery.json"
            if destination_name else ".*.collectigen-*.recovery.json"
        )
        for path in root.glob(pattern):
            if should_cancel is not None and should_cancel():
                raise InterruptedError("Training package recovery search was cancelled.")
            if _is_redirected(path) or path.parent!=root:
                continue
            try:
                payload,_destination,_stage=(
                    TrainingPackageService._read_recovery_receipt(path)
                )
            except (OSError,UnicodeError,json.JSONDecodeError,ValueError):
                continue
            if (
                isinstance(payload,dict)
                and payload.get("kind")=="collectigen_training_package_recovery"
                and payload.get("status") in accepted
            ):
                values.append(TrainingRecoveryChoice(
                    path,str(payload["destination_name"]),str(payload["status"]),
                ))
        return tuple(sorted(values,key=lambda item:item.receipt.name.casefold()))

    def preview_recovery(
        self,receipt_path: str|Path,*,
        should_cancel: CancelCallback|None=None,
    ) -> TrainingPackageResult:
        """Describe one exact recovery receipt without altering its stage or destination."""
        cancel=should_cancel or (lambda:False)
        if cancel():
            raise InterruptedError("Training package recovery preview was cancelled.")
        receipt=Path(receipt_path).expanduser().absolute()
        try:
            payload,destination,stage=self._read_recovery_receipt(receipt)
            if cancel():
                raise InterruptedError("Training package recovery preview was cancelled.")
            if _directory_state(receipt.parent)!=payload["destination_parent_state"]:
                raise ValueError("The recovery parent folder changed after publication began.")
            status=str(payload["status"])
            if status in RECOVERY_TERMINAL_STATUSES:
                raise ValueError(f"The recovery receipt is already finalized ({status}).")
            if _lexists(destination) and status!="promoted":
                raise ValueError("The destination exists without a publication receipt.")
            if status=="promoted":
                if not _lexists(destination):
                    raise ValueError("The promoted destination is missing.")
                if _is_redirected(destination/MANIFEST_NAME):
                    raise ValueError("The promoted manifest is redirected.")
                verification=self.verify(
                    destination,expected_intent=str(payload["intent_fingerprint"]),
                    should_cancel=cancel,
                )
                if not verification.valid:
                    raise ValueError("The promoted package does not verify: "+" ".join(verification.issues))
                manifest_sha=hashlib.sha256((destination/MANIFEST_NAME).read_bytes()).hexdigest()
                if manifest_sha!=payload["manifest_sha256"]:
                    raise ValueError("The promoted manifest differs from the recovery receipt.")
                consequence="Recovery can reconcile the verified published package and finalize its receipt."
            elif status=="staged_complete":
                if _is_redirected(stage) or not stage.is_dir():
                    raise ValueError("The commit-ready stage is missing or redirected.")
                if _lexists(stage/".collectigen-owned-staging.json"):
                    raise ValueError("A commit-ready stage still has an ownership marker.")
                if _is_redirected(stage/MANIFEST_NAME):
                    raise ValueError("The staged manifest is redirected.")
                verification=self.verify(
                    stage,expected_intent=str(payload["intent_fingerprint"]),
                    should_cancel=cancel,
                )
                if not verification.valid:
                    raise ValueError("The staged package does not verify: "+" ".join(verification.issues))
                manifest_sha=hashlib.sha256((stage/MANIFEST_NAME).read_bytes()).hexdigest()
                if manifest_sha!=payload["manifest_sha256"]:
                    raise ValueError("The staged manifest differs from the recovery receipt.")
                consequence=(
                    "Publication requires rebuilding the exact candidate plan and checking "
                    "current source and Image Knowledge evidence before recovery."
                )
            else:
                if _lexists(stage) and (_is_redirected(stage) or not stage.is_dir()):
                    raise ValueError("The incomplete stage is not a safe directory.")
                if status=="attention_required" and not _lexists(
                    stage/".collectigen-owned-staging.json"
                ):
                    raise ValueError("The attention-required stage has no ownership marker.")
                if _lexists(stage):
                    marker=stage/".collectigen-owned-staging.json"
                    if _lexists(marker):
                        if _is_redirected(marker):
                            raise ValueError("The staging marker is redirected.")
                        marker_payload=json.loads(marker.read_text(encoding="utf-8"))
                        if marker_payload!=self._marker_payload(
                            str(payload["operation_id"]),destination.name,
                        ):
                            raise ValueError("The staging marker does not own this directory.")
                    elif any(stage.iterdir()):
                        raise ValueError("The incomplete stage has no ownership marker.")
                    for item in stage.rglob("*"):
                        if cancel():
                            raise InterruptedError("Training package recovery preview was cancelled.")
                        if _is_redirected(item):
                            raise ValueError("The incomplete stage contains a redirected entry.")
                consequence=(
                    "Recovery can roll back marker-owned incomplete staging; "
                    "preview the package again before rebuilding."
                )
            if cancel():
                raise InterruptedError("Training package recovery preview was cancelled.")
            return TrainingPackageResult(
                "recovery_required",destination,recovery_receipt=receipt,
                verification=verification if status in {"staged_complete","promoted"} else None,
                issues=(f"Recovery phase: {status}.",consequence),
                recovery_phase=status,
            )
        except InterruptedError:
            raise
        except (OSError,UnicodeError,json.JSONDecodeError,RuntimeError,ValueError) as exc:
            return TrainingPackageResult(
                "failed",receipt.parent,
                issues=(f"Recovery is unavailable: {exc}",),
            )

    def recover(
        self,receipt_path: str|Path,*,
        candidate_plan: TrainingCandidatePlan|None=None,
        forbidden_roots: Iterable[str|Path]=(),
        expected_phase: str|None=None,
        expected_intent: str="",
        collection_version: TrainingCollectionBinding|None=None,
        collection_guard: Callable|None=None,
    ) -> TrainingPackageResult:
        options=dict(candidate_plan=candidate_plan,forbidden_roots=forbidden_roots,
            expected_phase=expected_phase,expected_intent=expected_intent)
        if collection_version is None:
            return self._recover_admitted(receipt_path,**options)
        receipt=Path(receipt_path).expanduser().absolute()
        try:
            collection_version.to_dict()
            if not callable(collection_guard):
                raise ValueError("Bound package recovery requires a current Collection guard.")
            with collection_guard():
                return self._recover_admitted(receipt,admitted_collection_version=collection_version,**options)
        except Exception as exc:
            return TrainingPackageResult("recovery_required",receipt.parent,recovery_receipt=receipt,
                issues=(str(exc),))

    @staticmethod
    def _recovery_collection_issue(verification,admitted_collection_version):
        manifest=verification.manifest
        if manifest.get("schema_version")==BOUND_PACKAGE_SCHEMA_VERSION:
            if (admitted_collection_version is None
                    or admitted_collection_version.to_dict()!=manifest.get("collection_version")):
                return "Bound package recovery requires the matching Collection binding and current guard."
        elif admitted_collection_version is not None:
            return "The reviewed Collection binding does not match this unbound package."
        return ""

    def _recover_admitted(
        self,receipt_path: str|Path,*,
        candidate_plan: TrainingCandidatePlan|None=None,
        forbidden_roots: Iterable[str|Path]=(),
        expected_phase: str|None=None,
        expected_intent: str="",
        admitted_collection_version: TrainingCollectionBinding|None=None,
    ) -> TrainingPackageResult:
        receipt=Path(receipt_path).expanduser().absolute(); parent=receipt.parent
        try:
            payload,destination,stage=self._read_recovery_receipt(receipt)
        except (OSError,UnicodeError,json.JSONDecodeError,ValueError) as exc:
            return TrainingPackageResult("failed",parent,issues=(f"The recovery receipt is invalid: {exc}",))
        if expected_intent and payload["intent_fingerprint"]!=expected_intent:
            return TrainingPackageResult("failed",destination,recovery_receipt=receipt,
                issues=("The recovery receipt belongs to a different frozen package intent.",))
        if expected_phase is not None and payload["status"]!=expected_phase:
            return TrainingPackageResult(
                "recovery_required",destination,recovery_receipt=receipt,
                issues=("The recovery phase changed after review. Review this receipt again before changing files.",),
            )
        try:
            if _directory_state(parent)!=payload["destination_parent_state"]:
                raise ValueError("The recovery parent folder changed after preview.")
            resolved_destination=destination.resolve(strict=False)
            for value in forbidden_roots:
                if not str(value or "").strip():
                    continue
                root=Path(value).expanduser().resolve(strict=False)
                if (
                    resolved_destination==root
                    or resolved_destination.is_relative_to(root)
                    or root.is_relative_to(resolved_destination)
                ):
                    raise ValueError("The recovery destination overlaps a named image library.")
        except (OSError,RuntimeError,ValueError) as exc:
            return TrainingPackageResult(
                "recovery_required",destination,recovery_receipt=receipt,
                issues=(str(exc),),
            )
        expected_intent=str(payload.get("intent_fingerprint") or "")
        status=str(payload.get("status") or "")
        if _lexists(destination):
            if status not in {"promoted","complete"}:
                return TrainingPackageResult(
                    "recovery_required",destination,recovery_receipt=receipt,
                    issues=(
                        "The destination exists but the recovery receipt does not prove publication.",
                    ),
                )
            verification=self.verify(destination,expected_intent=expected_intent)
            manifest_sha=""
            if verification.valid:
                try:
                    manifest_sha=hashlib.sha256(
                        (destination/MANIFEST_NAME).read_bytes()
                    ).hexdigest()
                except OSError:
                    pass
            if (
                not verification.valid
                or manifest_sha!=payload.get("manifest_sha256")
            ):
                return TrainingPackageResult(
                    "recovery_required",destination,recovery_receipt=receipt,
                    verification=verification,issues=(
                        verification.issues or
                        ("The published manifest does not match the recovery receipt.",)
                    ),
                )
            collection_issue=self._recovery_collection_issue(verification,admitted_collection_version)
            if collection_issue:
                return TrainingPackageResult("recovery_required",destination,recovery_receipt=receipt,
                    verification=verification,issues=(collection_issue,))
            payload["status"]="complete"; payload["result"]=verification.package_fingerprint
            try:
                write_verified_json(receipt,payload,label="Training package recovery receipt")
            except Exception as exc:
                return TrainingPackageResult(
                    "recovery_required",destination,verification.artifact_count,
                    destination/MANIFEST_NAME,receipt,verification,
                    (f"The package is verified but recovery bookkeeping failed: {exc}",),
                )
            return TrainingPackageResult("recovered",destination,verification.artifact_count,destination/MANIFEST_NAME,receipt,verification)
        if status in {"prepared","staging"} or (
            status=="attention_required"
            and _lexists(stage/".collectigen-owned-staging.json")
        ):
            cleaned=(
                True if not _lexists(stage) else
                self._cleanup_owned_stage(
                    stage,str(payload["operation_id"]),destination.name,
                )
            )
            if not cleaned:
                return TrainingPackageResult(
                    "recovery_required",destination,recovery_receipt=receipt,
                    issues=(
                        "The incomplete staged package could not be safely rolled back.",
                    ),
                )
            payload["status"]="rolled_back"
            payload["result"]="Incomplete marker-owned staging was removed safely."
            try:
                write_verified_json(receipt,payload,label="Training package recovery receipt")
            except Exception as exc:
                return TrainingPackageResult(
                    "recovery_required",destination,recovery_receipt=receipt,
                    issues=(f"Rollback succeeded but recovery bookkeeping failed: {exc}",),
                )
            return TrainingPackageResult(
                "rolled_back",destination,recovery_receipt=receipt,
                issues=("Incomplete staging was removed; preview the package again.",),
            )
        if status!="staged_complete":
            return TrainingPackageResult(
                "recovery_required",destination,recovery_receipt=receipt,
                issues=(f"Recovery state {status} cannot publish a package.",),
            )
        if not stage.is_dir() or _is_redirected(stage):
            return TrainingPackageResult("recovery_required",destination,recovery_receipt=receipt,issues=("The recorded staged package is missing or redirected.",))
        marker=stage/".collectigen-owned-staging.json"
        if _lexists(marker):
            return TrainingPackageResult("recovery_required",destination,recovery_receipt=receipt,issues=("A commit-ready recovery stage must not retain an ownership marker.",))
        verification=self.verify(stage,expected_intent=expected_intent)
        if not verification.valid:
            return TrainingPackageResult("recovery_required",destination,recovery_receipt=receipt,verification=verification,issues=verification.issues)
        collection_issue=self._recovery_collection_issue(verification,admitted_collection_version)
        if collection_issue:
            return TrainingPackageResult("recovery_required",destination,recovery_receipt=receipt,
                verification=verification,issues=(collection_issue,),recovery_phase="staged_complete")
        try:
            manifest_sha=hashlib.sha256((stage/MANIFEST_NAME).read_bytes()).hexdigest()
        except OSError as exc:
            return TrainingPackageResult(
                "recovery_required",destination,recovery_receipt=receipt,
                verification=verification,
                issues=(f"The staged manifest cannot be read: {exc}",),
            )
        if manifest_sha!=payload.get("manifest_sha256"):
            return TrainingPackageResult(
                "recovery_required",destination,recovery_receipt=receipt,
                verification=verification,
                issues=("The staged manifest does not match the recovery receipt.",),
            )
        if candidate_plan is None:
            return TrainingPackageResult(
                "recovery_required",destination,recovery_receipt=receipt,
                verification=verification,issues=(
                    "Rebuild the exact candidate plan so current source and Image Knowledge evidence can be checked before publication.",
                ),
                recovery_phase="staged_complete",
            )
        try:
            current_manifest=candidate_plan.to_manifest_dict()
            if (
                current_manifest.get("plan_fingerprint")
                    !=payload.get("candidate_plan_fingerprint")
                or current_manifest!=verification.manifest.get("candidate_plan")
                or not self._candidate_plan_is_current(
                    candidate_plan,current_manifest,
                )
            ):
                raise ValueError(
                    "Current source, Image Knowledge, profile, permission, role, or settings evidence differs from the staged package."
                )
        except Exception as exc:
            return TrainingPackageResult(
                "recovery_required",destination,recovery_receipt=receipt,
                verification=verification,issues=(str(exc),),
            )
        try:
            if _lexists(destination) or _has_redirected_component(parent):
                raise RuntimeError("The recovery destination is occupied or redirected.")
            _rename_directory_no_replace(stage,destination)
        except Exception as exc:
            return TrainingPackageResult("recovery_required",destination,recovery_receipt=receipt,issues=(str(exc),))
        verification=self.verify(destination,expected_intent=expected_intent)
        if not verification.valid:
            return TrainingPackageResult("recovery_required",destination,recovery_receipt=receipt,verification=verification,issues=verification.issues)
        payload["status"]="complete"; payload["result"]=verification.package_fingerprint
        try:
            write_verified_json(receipt,payload,label="Training package recovery receipt")
        except Exception as exc:
            return TrainingPackageResult(
                "recovery_required",destination,verification.artifact_count,
                destination/MANIFEST_NAME,receipt,verification,
                (f"The package is verified but recovery bookkeeping failed: {exc}",),
            )
        return TrainingPackageResult("recovered",destination,verification.artifact_count,destination/MANIFEST_NAME,receipt,verification)
