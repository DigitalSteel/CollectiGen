from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass,field,replace
from pathlib import Path
from typing import Callable,Iterable

from .annotation import SCHEMA_VERSION
from .metadata_sync import MetadataSyncService
from .operation_journal import OperationJournal


EXTERNAL_EVIDENCE_SOURCES=("txt","metadata")
EXTERNAL_EVIDENCE_MODES=("changed","all")
EXTERNAL_EVIDENCE_JOB_CONTRACT_VERSION=2


@dataclass(frozen=True,slots=True)
class ExternalEvidenceImportCounts:
    """Exact evidence outcomes that can be added across images and resumes."""

    reread_images: int=0
    source_reads: int=0
    unchanged_images: int=0
    known_tags: int=0
    unknown_tags: int=0
    ambiguous_values: int=0
    conflicted_values: int=0
    descriptions: int=0
    suppressed_values: int=0

    def __add__(
        self,other: "ExternalEvidenceImportCounts",
    ) -> "ExternalEvidenceImportCounts":
        if not isinstance(other,ExternalEvidenceImportCounts):
            return NotImplemented
        return ExternalEvidenceImportCounts(**{
            name:int(getattr(self,name))+int(getattr(other,name))
            for name in self.__dataclass_fields__
        })

    def to_dict(self) -> dict[str,int]:
        return {
            name:int(getattr(self,name)) for name in self.__dataclass_fields__
        }

    @classmethod
    def from_dict(cls,value) -> "ExternalEvidenceImportCounts":
        if not isinstance(value,dict):
            raise ValueError("External-evidence outcome counts are missing.")
        missing=set(cls.__dataclass_fields__).difference(value)
        if missing:
            raise ValueError("External-evidence outcome counts are incomplete.")
        parsed={}
        for name in cls.__dataclass_fields__:
            raw=value.get(name)
            if type(raw) is not int or raw<0:
                raise ValueError("External-evidence outcome counts are invalid.")
            parsed[name]=raw
        return cls(**parsed)

    @property
    def reviewable_findings(self) -> int:
        return (
            self.known_tags+self.unknown_tags+self.ambiguous_values
            +self.descriptions
        )

    def summary_lines(
        self,*,current_reused: int=0,recovered: int=0,
        held: int=0,failed: int=0,
    ) -> tuple[str,str]:
        return (
            (
                f"Reread {self.reread_images:,} image(s) / "
                f"{self.source_reads:,} source(s); "
                f"{self.unchanged_images:,} exact reread(s) were unchanged; "
                f"reused {max(0,int(current_reused)):,} current image(s); "
                f"recovered {max(0,int(recovered)):,} verified checkpoint(s)."
            ),
            (
                "Reviewable findings: "
                f"known Tags {self.known_tags:,}; "
                f"unknown Tags {self.unknown_tags:,}; "
                f"ambiguous {self.ambiguous_values:,}; "
                f"conflicted {self.conflicted_values:,}; "
                f"Descriptions {self.descriptions:,}; "
                f"skipped {self.suppressed_values+max(0,int(held)):,} "
                f"(suppressed {self.suppressed_values:,}; held {max(0,int(held)):,}); "
                f"failed {max(0,int(failed)):,}."
            ),
        )


def _signature(path: Path) -> dict[str,object]:
    try:
        stat=path.stat()
    except OSError:
        return {"exists":False}
    return {"exists":True,"size":stat.st_size,"mtime_ns":stat.st_mtime_ns}


def _signature_matches(fingerprint: object,path: Path) -> bool:
    if not isinstance(fingerprint,dict):
        return False
    current=_signature(path)
    return all(fingerprint.get(key)==value for key,value in current.items())


def _read_sidecar(path: Path) -> tuple[dict[str,object]|None,str]:
    before=_signature(path)
    if not before.get("exists"):
        return None,""
    try:
        raw=path.read_bytes()
        after=_signature(path)
        if before!=after:
            return None,"Image Knowledge changed while the preview was being built."
        payload=json.loads(raw.decode("utf-8"))
        if not isinstance(payload,dict):
            return None,"Image Knowledge does not contain an object."
        if int(payload.get("schema_version",1))!=SCHEMA_VERSION:
            return None,"Image Knowledge uses an older schema and must be upgraded first."
        return payload,""
    except Exception as exc:
        return None,f"Image Knowledge could not be read: {exc}"


def _scope_entry(
    path: Path,*,sources: Iterable[str]=EXTERNAL_EVIDENCE_SOURCES,
) -> dict[str,object]:
    selected={str(value) for value in sources}
    entry={
        "path":str(path),
        "image":_signature(path),
        "sidecar":_signature(path.with_suffix(".tags.json")),
    }
    if "txt" in selected:
        entry["txt"]=_signature(path.with_suffix(".txt"))
    return entry


def _scope_fingerprint(
    entries: Iterable[dict[str,object]],*,scope_label: str,
    sources: Iterable[str],mode: str,
) -> str:
    payload={
        "scope_label":str(scope_label),
        "sources":sorted(str(value) for value in sources),
        "mode":str(mode),
        "paths":list(entries),
    }
    encoded=json.dumps(
        payload,sort_keys=True,separators=(",",":"),ensure_ascii=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def external_evidence_scope_fingerprint(
    paths: Iterable[str|Path],*,scope_label: str,sources: Iterable[str],mode: str,
) -> str:
    return _scope_fingerprint(
        (_scope_entry(Path(value),sources=sources) for value in paths),
        scope_label=scope_label,sources=sources,mode=mode,
    )


def external_evidence_request_fingerprint(
    paths: Iterable[str|Path],*,sources: Iterable[str],mode: str,
    rules_fingerprint: str="",
) -> str:
    payload={
        "contract_version":EXTERNAL_EVIDENCE_JOB_CONTRACT_VERSION,
        "paths":sorted(
            str(Path(value).resolve()).casefold() for value in paths
        ),
        "sources":sorted(str(value) for value in sources),
        "mode":str(mode),
        "rules_fingerprint":str(rules_fingerprint or ""),
    }
    encoded=json.dumps(
        payload,sort_keys=True,separators=(",",":"),ensure_ascii=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def external_evidence_item_fingerprint(
    path: str|Path,*,sources: Iterable[str],mode: str,
    rules_fingerprint: str="",
) -> str:
    source_fingerprint=external_evidence_scope_fingerprint(
        (Path(path),),scope_label="checkpoint",sources=sources,mode=mode,
    )
    encoded=json.dumps({
        "source_fingerprint":source_fingerprint,
        "rules_fingerprint":str(rules_fingerprint or ""),
    },sort_keys=True,separators=(",",":"),ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True,slots=True)
class ExternalEvidenceImportItem:
    image: Path
    ready: bool
    txt_action: str
    metadata_action: str
    detail: str=""

    @property
    def reads_source(self) -> bool:
        return "read" in {self.txt_action,self.metadata_action}


@dataclass(frozen=True,slots=True)
class ExternalEvidenceImportPlan:
    scope_label: str
    sources: tuple[str,...]
    mode: str
    items: tuple[ExternalEvidenceImportItem,...]
    fingerprint: str
    rules_fingerprint: str=""
    resume_operation_id: str=""
    resume_completed_paths: tuple[Path,...]=()
    resume_counts: ExternalEvidenceImportCounts=field(
        default_factory=ExternalEvidenceImportCounts,
    )

    @property
    def paths(self) -> tuple[Path,...]:
        return tuple(item.image for item in self.items)

    @property
    def ready_paths(self) -> tuple[Path,...]:
        return tuple(item.image for item in self.items if item.ready)

    @property
    def held_count(self) -> int:
        return sum(not item.ready for item in self.items)

    @property
    def txt_reads(self) -> int:
        return sum(item.ready and item.txt_action=="read" for item in self.items)

    @property
    def metadata_reads(self) -> int:
        return sum(item.ready and item.metadata_action=="read" for item in self.items)

    @property
    def reused_count(self) -> int:
        return sum(item.ready and not item.reads_source for item in self.items)

    @property
    def reused_paths(self) -> tuple[Path,...]:
        return tuple(
            item.image for item in self.items
            if item.ready and not item.reads_source
        )

    @property
    def read_paths(self) -> tuple[Path,...]:
        return tuple(
            item.image for item in self.items
            if item.ready and item.reads_source
        )

    @property
    def estimated_source_reads(self) -> int:
        return self.txt_reads+self.metadata_reads

    @property
    def resume_count(self) -> int:
        return len(self.resume_completed_paths)

    @property
    def resume_available(self) -> bool:
        return bool(self.resume_operation_id)

    @property
    def pending_ready_paths(self) -> tuple[Path,...]:
        completed=set(self.resume_completed_paths)
        return tuple(path for path in self.ready_paths if path not in completed)

    @property
    def pending_source_reads(self) -> int:
        completed=set(self.resume_completed_paths)
        return sum(
            item.ready and item.image not in completed
            and item.txt_action=="read"
            for item in self.items
        )+sum(
            item.ready and item.image not in completed
            and item.metadata_action=="read"
            for item in self.items
        )

    def is_current(self) -> bool:
        return self.fingerprint==external_evidence_scope_fingerprint(
            self.ready_paths,scope_label=self.scope_label,
            sources=self.sources,mode=self.mode,
        )


@dataclass(frozen=True,slots=True)
class ExternalEvidenceImportCheckpointSession:
    operation_id: str
    sources: tuple[str,...]
    mode: str
    rules_fingerprint: str=""
    completed_paths: tuple[Path,...]=()
    current_paths: tuple[Path,...]=()
    resumed: bool=False
    completed_counts: ExternalEvidenceImportCounts=field(
        default_factory=ExternalEvidenceImportCounts,
    )


class ExternalEvidenceImportCheckpointService:
    """Persist exact, non-authoritative per-image import completions."""

    OPERATION_KIND="external_evidence_import"
    CONTRACT_VERSION=EXTERNAL_EVIDENCE_JOB_CONTRACT_VERSION

    def __init__(
        self,journal: OperationJournal,*,rules_fingerprint_provider=None,
    ) -> None:
        self.journal=journal
        self.rules_fingerprint_provider=rules_fingerprint_provider

    def _current_rules_fingerprint(self) -> str:
        if not callable(self.rules_fingerprint_provider):
            return ""
        return str(self.rules_fingerprint_provider() or "")

    @staticmethod
    def _path_key(path: str|Path) -> str:
        return str(Path(path).resolve()).casefold()

    @staticmethod
    def _request(plan: ExternalEvidenceImportPlan) -> str:
        return external_evidence_request_fingerprint(
            plan.ready_paths,sources=plan.sources,mode=plan.mode,
            rules_fingerprint=plan.rules_fingerprint,
        )

    def _operation(self,operation_id: str):
        return next((
            operation for operation in self.journal.operations()
            if operation.id==operation_id
        ),None)

    def _valid_completed(
        self,operation,plan,
    ) -> tuple[tuple[Path,...],ExternalEvidenceImportCounts]:
        wanted={self._path_key(path):path for path in plan.ready_paths}
        checkpoints={
            self._path_key(value.get("item") or ""):dict(value.get("metadata") or {})
            for value in operation.checkpoints
            if value.get("item")
        }
        completed=[]; counts=ExternalEvidenceImportCounts()
        for key,path in wanted.items():
            metadata=checkpoints.get(key,{})
            if (
                int(metadata.get("contract_version") or 0)==self.CONTRACT_VERSION
                and str(metadata.get("fingerprint") or "")
                ==external_evidence_item_fingerprint(
                    path,sources=plan.sources,mode=plan.mode,
                    rules_fingerprint=plan.rules_fingerprint,
                )
            ):
                try:
                    outcome=ExternalEvidenceImportCounts.from_dict(
                        metadata.get("outcome")
                    )
                except ValueError:
                    continue
                completed.append(path)
                counts+=outcome
        return tuple(completed),counts

    def decorate(
        self,plan: ExternalEvidenceImportPlan,
    ) -> ExternalEvidenceImportPlan:
        plan=replace(
            plan,rules_fingerprint=self._current_rules_fingerprint(),
            resume_operation_id="",resume_completed_paths=(),
            resume_counts=ExternalEvidenceImportCounts(),
        )
        request=self._request(plan)
        matches=[
            operation for operation in self.journal.operations()
            if operation.kind==self.OPERATION_KIND
            and int(operation.metadata.get("contract_version") or 0)
                ==self.CONTRACT_VERSION
            and str(operation.metadata.get("request_fingerprint") or "")
                ==request
        ]
        if not matches:
            return replace(
                plan,resume_operation_id="",resume_completed_paths=(),
                resume_counts=ExternalEvidenceImportCounts(),
            )
        operation=matches[-1]
        if operation.status not in {"applying","cancelled"}:
            return replace(
                plan,resume_operation_id="",resume_completed_paths=(),
                resume_counts=ExternalEvidenceImportCounts(),
            )
        completed,counts=self._valid_completed(operation,plan)
        if operation.checkpoints and not completed:
            return replace(
                plan,resume_operation_id="",resume_completed_paths=(),
                resume_counts=ExternalEvidenceImportCounts(),
            )
        return replace(
            plan,resume_operation_id=operation.id,
            resume_completed_paths=completed,resume_counts=counts,
        )

    def begin(
        self,plan: ExternalEvidenceImportPlan,
    ) -> ExternalEvidenceImportCheckpointSession:
        if plan.rules_fingerprint!=self._current_rules_fingerprint():
            raise RuntimeError(
                "Automation rules changed after the external-evidence preview. "
                "Nothing was imported; build the preview again."
            )
        undecorated=replace(
            plan,resume_operation_id="",resume_completed_paths=(),
            resume_counts=ExternalEvidenceImportCounts(),
        )
        current=self.decorate(undecorated)
        if (
            current.resume_operation_id!=plan.resume_operation_id
            or current.resume_completed_paths!=plan.resume_completed_paths
            or current.resume_counts!=plan.resume_counts
        ):
            raise RuntimeError(
                "The saved external-evidence resume state changed after the preview. "
                "Nothing was imported; build the preview again."
            )
        if current.resume_operation_id:
            operation=self._operation(current.resume_operation_id)
            if operation is None:
                raise RuntimeError(
                    "The saved external-evidence resume state is no longer available. "
                    "Build the preview again."
                )
            if operation.status=="cancelled":
                self.journal.resume(
                    operation.id,message="Resuming the exact reviewed evidence scope.",
                )
            return ExternalEvidenceImportCheckpointSession(
                operation_id=operation.id,sources=plan.sources,mode=plan.mode,
                rules_fingerprint=plan.rules_fingerprint,
                completed_paths=current.resume_completed_paths,
                current_paths=plan.reused_paths,resumed=True,
                completed_counts=current.resume_counts,
            )
        paths=sorted(str(path.resolve()) for path in plan.ready_paths)
        snapshot=self.journal.store_snapshot(paths)
        operation_id=self.journal.begin(
            self.OPERATION_KIND,expected=len(plan.read_paths),
            reason="Import attributed external image evidence",
            metadata={
                "contract_version":self.CONTRACT_VERSION,
                "request_fingerprint":self._request(plan),
                "scope_snapshot":snapshot,
                "scope_label":plan.scope_label,
                "path_count":len(paths),
                "sources":list(plan.sources),"mode":plan.mode,
                "writes":"image_knowledge_evidence_only",
            },
        )
        return ExternalEvidenceImportCheckpointSession(
            operation_id=operation_id,sources=plan.sources,mode=plan.mode,
            rules_fingerprint=plan.rules_fingerprint,
            completed_paths=(),current_paths=plan.reused_paths,resumed=False,
            completed_counts=ExternalEvidenceImportCounts(),
        )

    def checkpoint(
        self,session: ExternalEvidenceImportCheckpointSession,path: str|Path,*,
        counts: ExternalEvidenceImportCounts|None=None,
    ) -> None:
        path=Path(path)
        self.journal.append_checkpoint(
            session.operation_id,item=str(path),metadata={
                "contract_version":self.CONTRACT_VERSION,
                "fingerprint":external_evidence_item_fingerprint(
                    path,sources=session.sources,mode=session.mode,
                    rules_fingerprint=session.rules_fingerprint,
                ),
                "outcome":(
                    counts or ExternalEvidenceImportCounts()
                ).to_dict(),
            },
        )

    def finish(
        self,session: ExternalEvidenceImportCheckpointSession,status: str,*,
        result: dict[str,object]|None=None,message: str="",
    ) -> None:
        self.journal.finish(
            session.operation_id,status,message=message,result=result,
        )
        self.journal.prune()


class ExternalEvidenceImportPlanningService:
    """Build a read-only exact-scope plan without opening Metadata payloads."""

    def build(
        self,paths: Iterable[str|Path],*,scope_label: str,
        sources: Iterable[str]=EXTERNAL_EVIDENCE_SOURCES,mode: str="changed",
        availability: Callable[[Path],tuple[bool,str]]|None=None,
        progress: Callable[[int,int,str],None]|None=None,
        should_cancel: Callable[[],bool]|None=None,
    ) -> ExternalEvidenceImportPlan:
        exact=tuple(dict.fromkeys(Path(value) for value in paths))
        requested_sources={str(value) for value in sources}
        selected=tuple(
            value for value in EXTERNAL_EVIDENCE_SOURCES
            if value in requested_sources
        )
        if not selected:
            raise ValueError("Choose TXT, Metadata, or both before building the preview.")
        mode=str(mode or "changed")
        if mode not in EXTERNAL_EVIDENCE_MODES:
            raise ValueError(f"Unsupported external-evidence mode: {mode}")
        availability_by_path={
            image:(availability(image) if availability else (True,""))
            for image in exact
        }
        ready_scope=tuple(
            image for image in exact if availability_by_path[image][0]
        )
        starting_entries={
            image:_scope_entry(image,sources=selected) for image in ready_scope
        }
        items=[]; total=len(exact)
        for position,image in enumerate(exact,1):
            if should_cancel and should_cancel():
                raise InterruptedError("External-evidence preview cancelled")
            ready,reason=availability_by_path[image]
            if not ready:
                items.append(ExternalEvidenceImportItem(
                    image,False,
                    "held" if "txt" in selected else "not_selected",
                    "held" if "metadata" in selected else "not_selected",
                    str(reason or "The image is not ready for a verified Image Knowledge update."),
                ))
            else:
                payload,error=_read_sidecar(image.with_suffix(".tags.json"))
                if error:
                    items.append(ExternalEvidenceImportItem(
                        image,False,
                        "held" if "txt" in selected else "not_selected",
                        "held" if "metadata" in selected else "not_selected",
                        error,
                    ))
                else:
                    fingerprints={}
                    if payload is not None:
                        source=payload.get("source",{})
                        if isinstance(source,dict):
                            fingerprints=source.get("source_fingerprints",{})
                        if not isinstance(fingerprints,dict):
                            fingerprints={}
                    txt_current=(
                        payload is not None
                        and _signature_matches(
                            fingerprints.get("txt"),image.with_suffix(".txt")
                        )
                    )
                    metadata_fingerprint=fingerprints.get("metadata",{})
                    metadata_current=bool(
                        payload is not None
                        and isinstance(metadata_fingerprint,dict)
                        and metadata_fingerprint.get("status")=="verified"
                        and int(metadata_fingerprint.get("reader_version",0) or 0)
                        ==int(getattr(MetadataSyncService,"READER_VERSION",1))
                        and _signature_matches(
                            metadata_fingerprint.get("input"),image,
                        )
                    )
                    txt_action=(
                        "not_selected" if "txt" not in selected else
                        "read" if mode=="all" or not txt_current else "reuse"
                    )
                    metadata_action=(
                        "not_selected" if "metadata" not in selected else
                        "read" if mode=="all" or not metadata_current else "reuse"
                    )
                    items.append(ExternalEvidenceImportItem(
                        image,True,txt_action,metadata_action,
                        "New Image Knowledge will be created." if payload is None else "",
                    ))
            if progress and (position==total or position%25==0):
                progress(position,total,image.name)
        final_ready=tuple(item.image for item in items if item.ready)
        starting_fingerprint=_scope_fingerprint(
            (starting_entries[image] for image in final_ready),
            scope_label=scope_label,sources=selected,mode=mode,
        )
        fingerprint=external_evidence_scope_fingerprint(
            final_ready,scope_label=scope_label,sources=selected,mode=mode,
        )
        if fingerprint!=starting_fingerprint:
            raise RuntimeError(
                "An image, TXT caption, or Image Knowledge file changed while "
                "the preview was being built. Build the preview again."
            )
        return ExternalEvidenceImportPlan(
            str(scope_label),selected,mode,tuple(items),fingerprint,
        )
