from __future__ import annotations

import hashlib
import json
import os
import unicodedata
from dataclasses import dataclass,field
from datetime import datetime,timezone
from pathlib import Path
from typing import Any,Callable,Iterable
from uuid import uuid4

from .dataset_scope import IMG_EXTENSIONS
from .history_configuration import path_key
from .edit_service import MoveHistoryService,MoveRecord
from .library_profiles import (
    LibraryBoundaryDecision,LibraryPrivacyPolicy,LibraryProfile,LibraryRole,
)
from .library_storage import LibraryStorageInspector
from .operation_journal import JournalOperation
from .verified_transfer import (
    COPY_CHUNK_BYTES,TransferRecoveryRequired,VerifiedTransferPlan,
    VerifiedTransferEngine,
)


RECYCLE_SCHEMA_VERSION=3
RECYCLE_ACTIVE="active"
RECYCLE_TERMINAL_STATUSES=frozenset({
    "cancelled","failed","restored","routed_ingest","routed_library","purged",
})
COMPANION_SUFFIXES=(".tags.json",".txt",".xmp")
COMMON_UNSUPPORTED_IMAGE_SUFFIXES=frozenset({
    ".avif",".bmp",".cr2",".dng",".gif",".heic",".heif",".jxl",
    ".nef",".raw",".tif",".tiff",".arw",
})
COMMON_VIDEO_SUFFIXES=frozenset({".mp4",".m4v",".mov",".webm",".mkv",".avi"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _inside(path: Path,parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError,ValueError):
        return False


def _overlaps(first: Path,second: Path) -> bool:
    return _inside(first,second) or _inside(second,first)


def _is_remote_drive(path: Path) -> bool:
    if str(path).startswith("\\\\"):
        return True
    if os.name!="nt" or not path.drive:
        return False
    try:
        import ctypes
        return int(ctypes.windll.kernel32.GetDriveTypeW(f"{path.drive}\\"))==4
    except (AttributeError,OSError,TypeError,ValueError):
        return False


def _sha256(path: Path,should_cancel: Callable[[],bool]|None=None) -> str:
    digest=hashlib.sha256()
    with path.open("rb") as handle:
        while chunk:=handle.read(COPY_CHUNK_BYTES):
            if should_cancel is not None and should_cancel():
                raise InterruptedError("Work cancelled safely while verifying current file bytes.")
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path,payload: dict[str,Any]) -> None:
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    raw=(json.dumps(payload,indent=2,ensure_ascii=False,sort_keys=True)+"\n").encode("utf-8")
    temporary=path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    backup=path.with_suffix(path.suffix+".bak")
    try:
        temporary.write_bytes(raw)
        if temporary.read_bytes()!=raw:
            raise OSError("The staged recycle record failed read-back verification.")
        if path.is_file():
            previous=path.read_bytes(); backup.write_bytes(previous)
            if backup.read_bytes()!=previous:
                raise OSError("The recycle-record recovery copy failed verification.")
        temporary.replace(path)
        if path.read_bytes()!=raw:
            raise OSError("The saved recycle record failed read-back verification.")
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True,slots=True)
class RecycleFile:
    original: str
    current: str
    sha256: str=""
    size: int=0
    mtime_ns: int=0

    def to_dict(self) -> dict[str,Any]:
        return {
            "original":self.original,"current":self.current,
            "sha256":self.sha256,"size":self.size,"mtime_ns":self.mtime_ns,
        }

    @classmethod
    def from_dict(cls,value: dict[str,Any]) -> "RecycleFile":
        return cls(
            str(value.get("original") or ""),str(value.get("current") or ""),
            str(value.get("sha256") or ""),int(value.get("size") or 0),
            int(value.get("mtime_ns") or 0),
        )


@dataclass
class RecycleRecord:
    id: str
    status: str
    kind: str
    original_path: str
    current_path: str
    source_root: str=""
    source_profile_id: str=""
    relative_path: str=""
    recycled_at: str=""
    updated_at: str=""
    move_operation_id: str=""
    routed_operation_id: str=""
    files: tuple[RecycleFile,...]=()
    recovery: str=""
    _extra: dict[str,Any]=field(default_factory=dict,repr=False,compare=False)

    def to_dict(self) -> dict[str,Any]:
        return {
            **self._extra,"schema_version":3 if "recycle_lifecycle" in self._extra else 2 if "copy_lifecycle" in self._extra else 1,
            "id":self.id,"status":self.status,"kind":self.kind,
            "original_path":self.original_path,"current_path":self.current_path,
            "source_root":self.source_root,"source_profile_id":self.source_profile_id,
            "relative_path":self.relative_path,"recycled_at":self.recycled_at,
            "updated_at":self.updated_at,"move_operation_id":self.move_operation_id,
            "routed_operation_id":self.routed_operation_id,
            "files":[value.to_dict() for value in self.files],
            "recovery":self.recovery,
        }

    @classmethod
    def from_dict(cls,value: dict[str,Any]) -> "RecycleRecord":
        known={
            "schema_version","id","status","kind","original_path","current_path",
            "source_root","source_profile_id","relative_path","recycled_at",
            "updated_at","move_operation_id","routed_operation_id","files","recovery",
        }
        if int(value.get("schema_version") or 0)>RECYCLE_SCHEMA_VERSION:
            raise ValueError("This recycle record was written by a newer CollectiGen version.")
        if (value.get("schema_version")==2)!=("copy_lifecycle" in value):
            raise ValueError("Copied-family Recycle records require their exact supported schema.")
        if (value.get("schema_version")==3)!=("recycle_lifecycle" in value) or ("copy_lifecycle" in value and "recycle_lifecycle" in value):
            raise ValueError("Ordinary Recycle lifecycle records require their exact supported schema.")
        record_id=str(value.get("id") or "")
        if not record_id or Path(record_id).name!=record_id:
            raise ValueError("The recycle record has no safe stable identity.")
        return cls(
            record_id,str(value.get("status") or "failed"),
            str(value.get("kind") or "image"),
            str(value.get("original_path") or ""),str(value.get("current_path") or ""),
            str(value.get("source_root") or ""),str(value.get("source_profile_id") or ""),
            str(value.get("relative_path") or ""),str(value.get("recycled_at") or ""),
            str(value.get("updated_at") or ""),str(value.get("move_operation_id") or ""),
            str(value.get("routed_operation_id") or ""),
            tuple(RecycleFile.from_dict(item) for item in value.get("files",()) if isinstance(item,dict)),
            str(value.get("recovery") or ""),
            {key:item for key,item in value.items() if key not in known},
        )


class RecycleRecordStore:
    """One verified record per recycled family; active state is never retention-pruned."""

    def __init__(self,root: str|Path) -> None:
        self.root=Path(root); self.issues: list[str]=[]

    def path_for(self,record_id: str) -> Path:
        if not record_id or Path(record_id).name!=record_id:
            raise ValueError("Recycle record identity is invalid.")
        return self.root/f"{record_id}.json"

    def save(self,record: RecycleRecord) -> None:
        record.updated_at=_utc_now()
        if "copy_lifecycle" in record._extra or "recycle_lifecycle" in record._extra:
            from .history_versions import _reject_reparse
            path=self.path_for(record.id)
            _reject_reparse(path.absolute());_reject_reparse(path.with_suffix(".json.bak").absolute())
        _atomic_json(self.path_for(record.id),record.to_dict())

    def load(self,record_id: str) -> RecycleRecord:
        path=self.path_for(record_id)
        try:
            value=json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value,dict) and int(value.get("schema_version") or 0)>RECYCLE_SCHEMA_VERSION:
                raise NotImplementedError("This recycle record was written by a newer CollectiGen version.")
            if not isinstance(value,dict):
                raise ValueError("Recycle record must be a JSON object.")
            return RecycleRecord.from_dict(value)
        except Exception as error:
            if isinstance(error,NotImplementedError):raise ValueError(str(error)) from error
            backup=path.with_suffix(path.suffix+".bak")
            if backup.is_file():
                try:
                    value=json.loads(backup.read_text(encoding="utf-8"))
                    record=RecycleRecord.from_dict(value)
                    record._extra["_recovered_backup"]=True
                    self.issues.append(f"{record_id}: current record is damaged; verified backup is readable")
                    return record
                except Exception:
                    pass
            raise ValueError(f"Recycle record {record_id} could not be read: {error}") from error

    def load_strict(self,record_id: str) -> RecycleRecord:
        """Current exact authority only; recovery copies never authorize writes."""
        from .history_versions import _reject_reparse
        path=self.path_for(record_id);_reject_reparse(path.absolute())
        with path.open("rb") as stream:raw=stream.read(1024*1024+1)
        if len(raw)>1024*1024:raise ValueError("Recycle record exceeds its supported bound.")
        value=json.loads(raw)
        if not isinstance(value,dict):raise ValueError("Recycle record must be a JSON object.")
        record=RecycleRecord.from_dict(value)
        if record.id!=record_id or record._extra.get("_recovered_backup"):
            raise ValueError("Recycle record has no exact current authority.")
        return record

    def records(self) -> tuple[RecycleRecord,...]:
        result=[]; self.issues=[]
        if not self.root.is_dir():
            return ()
        for path in sorted(self.root.glob("recycle_*.json")):
            try:
                result.append(self.load(path.stem))
            except ValueError as error:
                self.issues.append(str(error))
        return tuple(result)


@dataclass(frozen=True,slots=True)
class RecyclePlan:
    record: RecycleRecord
    transfer: VerifiedTransferPlan
    boundary_decision: LibraryBoundaryDecision|None=None
    force_copy: bool=False

    @property
    def ready(self) -> bool:
        return self.transfer.ready

    @property
    def errors(self) -> tuple[str,...]:
        return self.transfer.errors


@dataclass(frozen=True,slots=True)
class RecycleRoutePlan:
    record_id: str
    route: str
    destination: Path
    transfer: VerifiedTransferPlan
    boundary_decision: LibraryBoundaryDecision|None=None
    destination_profile_id: str=""
    force_copy: bool=False

    @property
    def ready(self) -> bool:
        return self.transfer.ready

    @property
    def errors(self) -> tuple[str,...]:
        return self.transfer.errors


@dataclass(frozen=True,slots=True)
class RecyclePage:
    items: tuple[RecycleRecord,...]
    total: int
    page: int
    page_size: int
    issues: tuple[str,...]=()


@dataclass(frozen=True,slots=True)
class PurgeMember:
    entry_id: str
    source: Path
    sha256: str
    size: int
    mtime_ns: int
    source_token: str=""


@dataclass(frozen=True,slots=True)
class PurgePlan:
    members: tuple[PurgeMember,...]
    entry_ids: tuple[str,...]
    fingerprint: str
    errors: tuple[str,...]=()
    linked_witnesses_json: str=""

    @property
    def ready(self) -> bool:
        return bool(self.members) and not self.errors


class RecycleBinService:
    """Recycle-domain coordinator over the existing transfer and journal authority."""

    def __init__(
        self,settings,moves: MoveHistoryService,index_root: str|Path,* ,
        storage: LibraryStorageInspector|None=None,
    ) -> None:
        self.settings=settings; self.moves=moves; self.storage=storage or LibraryStorageInspector()
        self.store=RecycleRecordStore(index_root); self.recovery_issues: list[str]=[]
        from .organization_copy_history import OrganizationCopyHistoryProvider
        self.copy_history=OrganizationCopyHistoryProvider(self)
        self.moves.copy_history_provider=self.copy_history
        from .organization_recycle_history import OrganizationRecycleHistoryProvider
        self.organization_history=OrganizationRecycleHistoryProvider(self)
        self.moves.recycle_history_provider=self.organization_history
        from .history_recycle_support import NativeRecyclePurge
        self.native_purge=NativeRecyclePurge(self)
        self._recover_recycle_records()
        self._recover_purges()
        self.copy_history.recover_pending()
        self.organization_history.recover_pending()

    def preview_history(self,record_id,*,action="restore_origin",destination=None,destination_profile=None,should_cancel=None):
        """None means legacy-only coverage; held native previews never fall back."""
        from .organization_history import OrganizationRestorePreview
        record=self.store.load_strict(record_id);cancelled=should_cancel or (lambda:False)
        if "copy_lifecycle" in record._extra:
            try:
                link,current,_=self.copy_history._current(record)
                allowed=(action in {"restore_origin","restore_placement"} and record.status=="active") or (action=="return_to_recycle" and record.status=="restored")
                if not allowed:raise ValueError("Restore this copied family through its exact retained placement before routing elsewhere.")
                return self.copy_history.preview_restore(current,member_ids=(link["member_id"],),cancelled=cancelled)
            except Exception as exc:return OrganizationRestorePreview(record.move_operation_id,provider="copy_recycle",held=(str(exc),))
        if "recycle_lifecycle" in record._extra:
            return self.organization_history.preview_record(record_id,action=action,destination=destination,destination_profile=destination_profile,cancelled=cancelled)
        if record.kind!="image" or Path(record.current_path).suffix.casefold() not in IMG_EXTENSIONS:return None
        # Exact recorded ID only; corrupt/missing claimed native evidence holds.
        try:selected=self.organization_history._history()._record(record.move_operation_id)
        except Exception as exc:
            return OrganizationRestorePreview(record.move_operation_id,provider="recycle",held=(str(exc),))
        if not selected.get("history_move"):return None
        return self.organization_history.preview_record(record_id,action=action,destination=destination,destination_profile=destination_profile,cancelled=cancelled)

    def apply_history(self,preview,*,boundary_confirmed=False,should_cancel=None):
        providers={"recycle":self.organization_history,"copy_recycle":self.copy_history}
        provider=providers.get(getattr(preview,"provider",None))
        if provider is None:raise ValueError("Choose a supported exact Recycle comparison.")
        return provider.apply_restore(preview,boundary_confirmed=boundary_confirmed,cancelled=should_cancel or (lambda:False))

    def _apply_history_record(self,preview,*,boundary_confirmed=False,should_cancel=None):
        result=self.apply_history(preview,boundary_confirmed=boundary_confirmed,should_cancel=should_cancel)[0]
        if result.status=="recovery_required":
            error=TransferRecoveryRequired(result.message);error.operation_id=result.inverse_operation_id;raise error
        if result.status=="cancelled":raise InterruptedError(result.message)
        if result.status!="complete":raise ValueError(result.message)
        return self.store.load_strict(result.recycle_record_id)

    def _native_source(self,image):
        coordinator=self.moves.history_coordinator
        if coordinator is None or Path(image).suffix.casefold() not in IMG_EXTENSIONS:return False
        from .history_versions import HistoryStore
        descriptor=coordinator.configuration.resolve(path=image)
        if descriptor.status not in {"configured","not_configured"}:raise ValueError(descriptor.reason)
        return descriptor.status=="configured" and HistoryStore(descriptor.scope()).lookup(Path(image)) is not None

    def configured_root(self) -> Path|None:
        raw=str(getattr(self.settings,"recycle_directory","") or "").strip()
        return Path(raw).expanduser() if raw else None

    def protected_roots(self) -> tuple[tuple[str,Path],...]:
        values=[]
        for field,label in (
            ("ingest_directory","Incoming images"),("problems_directory","Problems"),
            ("external_write_backup_directory","Protected image backups"),
            ("working_directory","Legacy working library"),
        ):
            raw=str(getattr(self.settings,field,"") or "").strip()
            if raw: values.append((label,Path(raw).expanduser()))
        registry=getattr(self.settings,"library_registry",lambda:None)()
        for profile in tuple(getattr(registry,"profiles",()) or ()):
            if str(getattr(profile,"root","") or "").strip():
                values.append((f'Image library "{profile.name}"',Path(profile.root)))
        return tuple(values)

    def validate_root(self,*,create: bool=False) -> Path:
        root=self.configured_root()
        if root is None:
            raise ValueError("Choose a Recycle Bin folder in Setup first.")
        if not root.is_absolute():
            raise ValueError("Recycle Bin must use an absolute machine-local folder path.")
        if _is_remote_drive(root):
            raise ValueError("Recycle Bin must use machine-local storage, not a network path.")
        if root.is_symlink():
            raise ValueError("Recycle Bin cannot use a symbolic-link folder because ownership is uncertain.")
        for label,other in self.protected_roots():
            if _overlaps(root,other):
                raise ValueError(f"Recycle Bin must be separate from {label}; the folders cannot be equal or nested.")
        if create:
            root.mkdir(parents=True,exist_ok=True)
        if not root.is_dir():
            raise ValueError("Recycle Bin is unavailable. Restore the configured folder or choose another one in Setup.")
        if not os.access(root,os.R_OK|os.W_OK):
            raise PermissionError("Recycle Bin is read-only or unavailable to this account.")
        return root.resolve()

    def preview_recycle(
        self,image: str|Path,*,source_root: str|Path|None=None,
        source_profile_id: str="",kind: str="image",
        should_cancel: Callable[[],bool]|None=None,
    ) -> RecyclePlan:
        root=self.validate_root()
        source=Path(image)
        origin_root=Path(source_root).resolve() if source_root else source.parent.resolve()
        registry=getattr(self.settings,"library_registry",lambda:None)()
        source_profile=(
            registry.get(source_profile_id)
            if registry is not None and source_profile_id else None
        )
        if source_profile_id and source_profile is None:
            raise ValueError(
                "The recorded source library profile is unavailable; refresh its ownership before moving to Recycle Bin."
            )
        recycle_profile=LibraryProfile.create(
            "Recycle Bin",root,LibraryRole.LOCAL_ONLY,stable_id="collectigen-recycle-bin",
        )
        boundary=(
            LibraryPrivacyPolicy.movement(source_profile,recycle_profile)
            if source_profile is not None else
            LibraryBoundaryDecision(False,False,"The move stays inside machine-local storage.")
        )
        force_copy=LibraryPrivacyPolicy.requires_verified_copy(source_profile,recycle_profile)
        try: relative=str(source.resolve().relative_to(origin_root))
        except ValueError: relative=source.name
        record_id=f"recycle_{uuid4().hex}"
        destination=root/("orphans" if kind=="orphan" else "items")/record_id
        transfer=self.moves.preview(
            source,destination,collision_policy="error",force_copy=force_copy,
            should_cancel=should_cancel,
        )
        record=RecycleRecord(
            record_id,"planned",kind,str(source),
            str(transfer.members[0].target) if transfer.members else "",
            str(origin_root),str(source_profile_id or ""),relative,_utc_now(),_utc_now(),
            files=tuple(
                RecycleFile(str(member.source),str(member.target),"",member.size,member.mtime_ns)
                for member in transfer.members
            ),
        )
        return RecyclePlan(record,transfer,boundary,force_copy)

    def apply_recycle(
        self,plan: RecyclePlan,*,reason: str="move to Recycle Bin",
        boundary_confirmed: bool=False,
        should_cancel: Callable[[],bool]|None=None,
    ) -> RecycleRecord:
        root=self.validate_root()
        if not plan.ready:
            raise ValueError("Recycle preview is not ready: "+"; ".join(plan.errors))
        if (
            plan.boundary_decision is not None
            and plan.boundary_decision.blocked_by_default
            and not boundary_confirmed
        ):
            raise PermissionError(
                "This move crosses the Local Only privacy boundary and requires the exact preview confirmation."
            )
        if not _inside(plan.transfer.destination,root):
            raise PermissionError("Recycle preview no longer targets the configured Recycle Bin.")
        if plan.record.kind=="image" and self._native_source(plan.transfer.image):
            preview=self.organization_history.preview_entry(plan,cancelled=should_cancel or (lambda:False))
            return self._apply_history_record(preview,boundary_confirmed=boundary_confirmed,should_cancel=should_cancel)
        record=plan.record; record.status="moving"; self.store.save(record)
        try:
            moved=self.moves.move(
                plan.transfer.image,plan.transfer.destination,preview=plan.transfer,
                collision_policy="error",reason=reason,force_copy=plan.force_copy,
                should_cancel=should_cancel,boundary_decision=plan.boundary_decision,
                boundary_confirmed=boundary_confirmed,
                source_profile_ids=(record.source_profile_id,) if record.source_profile_id else (),
            )
        except InterruptedError as error:
            record.status="cancelled"; record.recovery=str(error); self.store.save(record); raise
        except TransferRecoveryRequired as error:
            record.status="recovery_required"; record.recovery=str(error)
            record._extra["recovery_transition"]="moving"
            self.store.save(record); raise
        except Exception as error:
            record.status="failed"; record.recovery=str(error); self.store.save(record); raise
        record.status=RECYCLE_ACTIVE; record.move_operation_id=moved.operation_id
        record.current_path=moved.files[0]["to"] if moved.files else record.current_path
        original_stats={value.original:(value.size,value.mtime_ns) for value in record.files}
        record.files=tuple(
            RecycleFile(
                str(item.get("from") or ""),str(item.get("to") or ""),
                str(item.get("sha256") or ""),
                original_stats.get(str(item.get("from") or ""),(0,0))[0],
                original_stats.get(str(item.get("from") or ""),(0,0))[1],
            ) for item in moved.files
        )
        record.recovery=""; self.store.save(record); return record

    def record(self,record_id: str) -> RecycleRecord:
        return self.store.load(record_id)

    def page(
        self,*,query: str="",page: int=0,page_size: int=24,
        statuses: Iterable[str]=(RECYCLE_ACTIVE,"recovery_required"),
        record_ids: Iterable[str]|None=None,
    ) -> RecyclePage:
        wanted=set(statuses); needle=str(query or "").strip().casefold()
        exact=None if record_ids is None else tuple(dict.fromkeys(record_ids))
        if exact is not None and (len(exact)>10000 or any(not isinstance(value,str) or not value for value in exact)):
            raise ValueError("Choose a bounded exact Recycle record scope.")
        records_source=self.store.records() if exact is None else tuple(self.store.load(value) for value in exact)
        records=[
            item for item in records_source
            if item.status in wanted and (
                not needle or needle in " ".join((
                    Path(item.original_path).name,item.original_path,item.relative_path,
                    item.source_profile_id,item.status,item.recovery,
                )).casefold()
            )
        ]
        records.sort(key=lambda item:(item.recycled_at,item.id),reverse=True)
        size=max(1,min(500,int(page_size))); total=len(records)
        pages=max(1,(total+size-1)//size); current=max(0,min(int(page),pages-1))
        start=current*size
        return RecyclePage(
            tuple(records[start:start+size]),total,current,size,
            tuple((*self.store.issues,*self.recovery_issues)),
        )

    def _require_active(self,record_id: str) -> RecycleRecord:
        record=self.store.load_strict(record_id)
        if record.status!=RECYCLE_ACTIVE:
            raise ValueError(f"Recycle item is {record.status.replace('_',' ')} and cannot be moved.")
        current=Path(record.current_path)
        root=self.validate_root()
        if not _inside(current,root):
            raise PermissionError("Recycle item is outside the configured Recycle Bin.")
        if "copy_lifecycle" in record._extra:self.copy_history.validate_active(record)
        if "recycle_lifecycle" in record._extra:self.organization_history.validate_active(record)
        return record

    def restore(
        self,record_id: str,*,boundary_confirmed: bool=False,
        should_cancel: Callable[[],bool]|None=None,
    ) -> RecycleRecord:
        record=self._require_active(record_id)
        preview=self.preview_history(record_id,should_cancel=should_cancel)
        if preview is not None:return self._apply_history_record(preview,boundary_confirmed=boundary_confirmed,should_cancel=should_cancel)
        registry=getattr(self.settings,"library_registry",lambda:None)()
        if "copy_lifecycle" in record._extra:
            return self.copy_history.restore_record(record_id,boundary_confirmed=boundary_confirmed,should_cancel=should_cancel)
        destination=(
            registry.get(record.source_profile_id)
            if registry is not None and record.source_profile_id else None
        )
        if record.source_profile_id and destination is None:
            raise ValueError(
                "The recorded source library is no longer configured. Restore its profile before restoring this item."
            )
        if destination is not None and destination.recognized_role is None:
            raise ValueError(
                "The recorded source library has an unsupported fail-closed storage role."
            )
        recycle_profile=LibraryProfile.create(
            "Recycle Bin",self.validate_root(),LibraryRole.LOCAL_ONLY,
            stable_id="collectigen-recycle-bin",
        )
        boundary=(
            LibraryPrivacyPolicy.movement(recycle_profile,destination)
            if destination is not None else
            LibraryBoundaryDecision(False,False,"The restore stays inside machine-local storage.")
        )
        force_copy=LibraryPrivacyPolicy.requires_verified_copy(recycle_profile,destination)
        record.status="restoring"; record.recovery="Restore is in progress."
        self.store.save(record)
        try:
            restored=self.moves.restore_path(
                Path(record.current_path),force_copy=force_copy,
                should_cancel=should_cancel,boundary_decision=boundary,
                boundary_confirmed=boundary_confirmed,
                destination_profile_id=destination.stable_id if destination is not None else "",
            )
        except TransferRecoveryRequired as error:
            record.status="recovery_required"; record.recovery=str(error)
            record._extra["recovery_transition"]="restoring"
            self.store.save(record); raise
        except Exception as error:
            record.status=RECYCLE_ACTIVE; record.recovery=str(error)
            self.store.save(record); raise
        record.status="restored"; record.routed_operation_id=restored.operation_id
        record.current_path=record.original_path; record.recovery=""
        self.store.save(record); return record

    def preview_route(
        self,record_id: str,destination: str|Path,*,route: str,
        destination_profile: LibraryProfile|None=None,
        should_cancel: Callable[[],bool]|None=None,
    ) -> RecycleRoutePlan:
        if route not in {"ingest","library"}:
            raise ValueError("Recycle destination must be Ingest or a named library.")
        record=self._require_active(record_id); root=self.validate_root()
        if "copy_lifecycle" in record._extra:
            raise ValueError("Restore this retained copy through its exact Recycle lifecycle before routing it elsewhere.")
        destination=Path(destination)
        if _overlaps(root,destination):
            raise ValueError("The Recycle Bin and route destination cannot be equal or nested.")
        registry=getattr(self.settings,"library_registry",lambda:None)()
        if route=="library" and destination_profile is None and registry is not None:
            destination_profile=registry.profile_for_path(destination)
        if route=="library" and destination_profile is None:
            raise ValueError("Choose a recognized named-library destination.")
        if (
            route=="library" and destination_profile is not None
            and destination_profile.recognized_role is None
        ):
            raise ValueError("The selected named library has an unsupported fail-closed storage role.")
        if (
            route=="library" and destination_profile is not None
            and destination.resolve()!=Path(destination_profile.root).resolve()
        ):
            raise ValueError("The selected named-library profile does not match the route destination.")
        recycle_profile=LibraryProfile.create(
            "Recycle Bin",root,LibraryRole.LOCAL_ONLY,stable_id="collectigen-recycle-bin",
        )
        boundary=(
            LibraryPrivacyPolicy.movement(recycle_profile,destination_profile)
            if destination_profile is not None else
            LibraryBoundaryDecision(False,False,"The route stays inside machine-local storage.")
        )
        force_copy=LibraryPrivacyPolicy.requires_verified_copy(
            recycle_profile,destination_profile,
        )
        transfer=self.moves.preview(
            Path(record.current_path),destination,collision_policy="error",
            force_copy=force_copy,should_cancel=should_cancel,
        )
        return RecycleRoutePlan(
            record.id,route,destination,transfer,boundary,
            destination_profile.stable_id if destination_profile is not None else "",
            force_copy,
        )

    def route(
        self,record_id: str,destination: str|Path,*,route: str,
        preview: RecycleRoutePlan|None=None,boundary_confirmed: bool=False,
        destination_profile: LibraryProfile|None=None,
        should_cancel: Callable[[],bool]|None=None,
    ) -> RecycleRecord:
        record=self._require_active(record_id); source=Path(record.current_path)
        if getattr(preview,"provider",None) is not None:
            return self._apply_history_record(preview,boundary_confirmed=boundary_confirmed,should_cancel=should_cancel)
        native_preview=self.preview_history(record_id,action="route_"+route,destination=destination,destination_profile=destination_profile,should_cancel=should_cancel)
        if native_preview is not None:
            return self._apply_history_record(native_preview,boundary_confirmed=boundary_confirmed,should_cancel=should_cancel)
        if "copy_lifecycle" in record._extra:
            raise ValueError("Restore this retained copy through its exact Recycle lifecycle before routing it elsewhere.")
        plan=preview or self.preview_route(
            record_id,destination,route=route,destination_profile=destination_profile,
            should_cancel=should_cancel,
        )
        if plan.record_id!=record.id or plan.route!=route:
            raise ValueError("The recycle route preview does not match this item or action.")
        if not plan.ready:
            raise ValueError("Destination preview is not ready: "+"; ".join(plan.errors))
        record.status=f"routing_{route}"; record.recovery=f"Move to {route} is in progress."
        record._extra["pending_route"]={
            "route":route,"destination":str(plan.destination),
            "targets":[str(member.target) for member in plan.transfer.members],
        }
        self.store.save(record)
        try:
            moved=self.moves.move(
                source,Path(destination),preview=plan.transfer,collision_policy="error",
                force_copy=plan.force_copy,reason=f"move recycled item to {route}",
                should_cancel=should_cancel,boundary_decision=plan.boundary_decision,
                boundary_confirmed=boundary_confirmed,
                destination_profile_id=plan.destination_profile_id,
            )
        except TransferRecoveryRequired as error:
            record.status="recovery_required"; record.recovery=str(error)
            record._extra["recovery_transition"]=f"routing_{route}"
            self.store.save(record); raise
        except Exception as error:
            record.status=RECYCLE_ACTIVE; record.recovery=str(error)
            record._extra.pop("pending_route",None); self.store.save(record); raise
        record.status=f"routed_{route}"; record.routed_operation_id=moved.operation_id
        record.current_path=moved.files[0]["to"] if moved.files else str(destination)
        record.recovery=""; record._extra.pop("pending_route",None)
        self.store.save(record); return record

    def preview_purge(
        self,record_ids: Iterable[str],*,should_cancel: Callable[[],bool]|None=None,
    ) -> PurgePlan:
        root=self.validate_root(); errors=[]; members=[]; entry_ids=[];witnesses=[]
        for record_id in dict.fromkeys(str(value) for value in record_ids if str(value)):
            if should_cancel is not None and should_cancel():
                raise InterruptedError("Permanent-delete preview cancelled safely before any files changed.")
            try:
                record=self._require_active(record_id)
                if "copy_lifecycle" in record._extra:
                    witness={"schema_version":1,"provider":"copy_recycle","authority":self.copy_history.purge_witness(record)}
                else:
                    native=bool(record.move_operation_id and record.kind=="image" and Path(record.current_path).suffix.casefold() in IMG_EXTENSIONS
                        and self.organization_history._history()._record(record.move_operation_id).get("history_move"))
                    witness=({"schema_version":1,"provider":"recycle","authority":self.organization_history.purge_witness(record)}
                        if "recycle_lifecycle" in record._extra or native else None)
            except Exception as error:
                errors.append(f"{record_id}: {error}"); continue
            entry_ids.append(record.id); current=Path(record.current_path)
            if witness:witnesses.append(witness)
            tokens={path_key(item["path"]):item["source_token"] for item in witness["authority"]["files"]} if witness else {}
            expected={Path(value.current).resolve():value for value in record.files}
            actual=(
                (current,) if record.kind=="orphan" else
                VerifiedTransferEngine.related_files(current)
            )
            actual_set={path.resolve() for path in actual}
            if actual_set!=set(expected):
                errors.append(
                    f"{Path(record.original_path).name}: current family differs from the recorded recycle manifest."
                ); continue
            for path in actual:
                if not _inside(path,root):
                    errors.append(f"{path.name}: path is outside Recycle Bin."); continue
                try:
                    stat=path.stat(); digest=_sha256(path,should_cancel)
                except OSError as error:
                    errors.append(f"{path.name}: {error}"); continue
                recorded=expected[path.resolve()]
                if recorded.sha256 and digest!=recorded.sha256:
                    errors.append(f"{path.name}: file changed after it entered Recycle Bin."); continue
                members.append(PurgeMember(record.id,path,digest,stat.st_size,stat.st_mtime_ns,tokens.get(path_key(path),"")))
        payload=[(item.entry_id,str(item.source),item.sha256,item.size,item.mtime_ns) for item in members]
        typed=any(witness["provider"]=="recycle" for witness in witnesses)
        if not typed:witnesses=[witness["authority"] for witness in witnesses]
        if witnesses:payload={"members":payload,"native_witnesses" if typed else "copy_witnesses":witnesses}
        fingerprint=hashlib.sha256(json.dumps(payload,separators=(",",":"),ensure_ascii=False).encode("utf-8")).hexdigest()
        return PurgePlan(tuple(members),tuple(entry_ids),fingerprint,tuple(dict.fromkeys(errors)),
            json.dumps(witnesses,sort_keys=True,separators=(",",":")) if witnesses else "")

    @staticmethod
    def _purge_payload(plan: PurgePlan,operation_id: str,root: Path) -> dict[str,Any]:
        staging=root/".collectigen-delete"/operation_id
        witnesses=json.loads(plan.linked_witnesses_json or "[]")
        typed=bool(witnesses and "provider" in witnesses[0])
        return {
            "engine_version":1 if typed else 2 if witnesses else 1,"fingerprint":plan.fingerprint,
            **({"native_witnesses" if typed else "copy_witnesses":witnesses} if witnesses else {}),
            "entry_ids":list(plan.entry_ids),
            "files":[{
                "entry_id":item.entry_id,"source":str(item.source),
                "staged":str(staging/item.entry_id/f"{index:04d}_{item.source.name}"),
                "sha256":item.sha256,"size":item.size,"mtime_ns":item.mtime_ns,
                **({"source_token":item.source_token} if item.source_token else {}),
            } for index,item in enumerate(plan.members)],
        }

    def apply_purge(
        self,plan: PurgePlan,*,should_cancel: Callable[[],bool]|None=None,
    ) -> tuple[RecycleRecord,...]:
        if not plan.ready:
            raise ValueError("Permanent-delete preview is not ready: "+"; ".join(plan.errors))
        current=self.preview_purge(plan.entry_ids,should_cancel=should_cancel)
        if not current.ready or current.fingerprint!=plan.fingerprint:
            raise RuntimeError("Recycle contents changed after preview. Review permanent deletion again.")
        if (current.linked_witnesses_json or plan.linked_witnesses_json) and current!=plan:
            raise RuntimeError("The exact linked purge plan changed after preview.")
        witnesses=json.loads(plan.linked_witnesses_json or "[]")
        guard=self.native_purge.guard if witnesses and "provider" in witnesses[0] else self.copy_history.purge_guard
        with self.moves._lock,guard(witnesses) as stores:
            return self._apply_purge(plan,should_cancel=should_cancel,linked_stores=stores)

    @staticmethod
    def _verify_purge_instance(value,path):
        if value.get("source_token"):
            from .history_versions import _source_token,_reject_reparse
            _reject_reparse(path)
            if _source_token(path)!=value["source_token"]:
                raise ValueError("The exact retained-copy purge file instance changed.")

    def _apply_purge(self,plan,*,should_cancel=None,linked_stores=None):
        root=self.validate_root(); journal=self.moves.journal
        linked=bool(plan.linked_witnesses_json)
        witnesses=json.loads(plan.linked_witnesses_json or "[]");typed=bool(witnesses and "provider" in witnesses[0])
        operation_id=journal.begin(
            "recycle_native_permanent_delete" if typed else "recycle_copy_permanent_delete" if linked else "recycle_permanent_delete",expected=len(plan.members),
            reason="permanently delete reviewed Recycle Bin files",
            metadata={"engine_version":1 if typed else 2 if linked else 1,"entry_ids":list(plan.entry_ids)},
        )
        payload=self._purge_payload(plan,operation_id,root)
        journal.append_change(
            operation_id,item="recycle permanent delete",
            before={"files":[value["source"] for value in payload["files"]]},
            after=payload,reason="reviewed permanent deletion",
            metadata={"stage":"exact_recycle_manifest"},
        )
        staged=[]
        try:
            for value in payload["files"]:
                if should_cancel is not None and should_cancel():
                    raise InterruptedError("Permanent deletion cancelled safely before commit.")
                source=Path(value["source"]); target=Path(value["staged"])
                if source.stat().st_size!=int(value["size"]) or source.stat().st_mtime_ns!=int(value["mtime_ns"]):
                    raise RuntimeError(f"{source.name} changed after preview.")
                if _sha256(source,should_cancel)!=value["sha256"]:
                    raise RuntimeError(f"{source.name} changed after preview.")
                self._verify_purge_instance(value,source)
                if value.get("source_token"):
                    from .history_versions import _reject_reparse
                    _reject_reparse(target)
                target.parent.mkdir(parents=True,exist_ok=True)
                if target.exists(): raise FileExistsError(f"Delete staging collision: {target}")
                os.rename(source,target); staged.append((source,target,value))
            for _,target,value in staged:
                self._verify_purge_instance(value,target)
                if _sha256(target)!=value["sha256"]:
                    raise OSError(f"Delete staging verification failed for {target.name}.")
        except Exception as error:
            rollback=[]
            for source,target,value in reversed(staged):
                try:
                    self._verify_purge_instance(value,target)
                    if target.is_file() and not source.exists() and _sha256(target)==value["sha256"]:
                        if value.get("source_token"):
                            from .history_versions import _reject_reparse
                            _reject_reparse(source)
                        source.parent.mkdir(parents=True,exist_ok=True); os.rename(target,source)
                except Exception as rollback_error:
                    rollback.append(f"{source.name}: {rollback_error}")
            if rollback:
                raise TransferRecoveryRequired(
                    "Permanent deletion did not commit and rollback needs attention: "+"; ".join(rollback)
                ) from error
            journal.finish(
                operation_id,"cancelled" if isinstance(error,InterruptedError) else "failed",
                message=str(error),
            )
            raise
        journal.append_checkpoint(operation_id,item="deletion_committed")
        try:
            for value in payload["files"]:
                target=Path(value["staged"])
                if target.is_file():
                    self._verify_purge_instance(value,target)
                    if _sha256(target)!=value["sha256"]:
                        raise TransferRecoveryRequired(f"Staged file changed before deletion: {target.name}")
                    target.unlink()
                journal.append_checkpoint(operation_id,item=f"deleted:{value['staged']}")
        except Exception as error:
            self.recovery_issues.append(f"{operation_id}: {error}")
            raise TransferRecoveryRequired(
                "Permanent deletion committed, but cleanup must resume before more deletion is allowed."
            ) from error
        records=[]
        for record_id in plan.entry_ids:
            record=self.store.load_strict(record_id); record.status="purged"; record.routed_operation_id=operation_id
            record.recovery="Permanently deleted after explicit reviewed confirmation."
            self.store.save(record); records.append(record)
        if linked:
            try:
                if typed:self.native_purge.retire(payload["native_witnesses"],operation_id,linked_stores)
                else:self.copy_history.retire_purged(payload["copy_witnesses"],operation_id,linked_stores)
            except Exception as error:raise TransferRecoveryRequired("The committed copy purge needs exact native retirement: "+str(error)) from error
        journal.finish(operation_id,result={"entry_ids":list(plan.entry_ids),"fingerprint":plan.fingerprint})
        self._cleanup_staging(root/".collectigen-delete"/operation_id)
        return tuple(records)

    @staticmethod
    def _cleanup_staging(path: Path) -> None:
        current=Path(path)
        for directory in sorted(
            (value for value in current.rglob("*") if value.is_dir()),
            key=lambda value:len(value.parts),reverse=True,
        ) if current.is_dir() else ():
            try: directory.rmdir()
            except OSError: pass
        try: current.rmdir()
        except OSError: pass

    def _operation_payload(self,operation: JournalOperation) -> dict[str,Any]:
        if not operation.changes:return {}
        try:
            value=self.moves.journal.load_snapshot(operation.changes[-1]["after"])
            return value if isinstance(value,dict) else {}
        except Exception as error:
            self.recovery_issues.append(f"{operation.id}: purge evidence could not be read: {error}")
            return {}

    @staticmethod
    def _all_files_exist(paths: Iterable[str]) -> bool:
        values=tuple(Path(value) for value in paths if str(value))
        return bool(values) and all(path.is_file() for path in values)

    @staticmethod
    def _no_files_exist(paths: Iterable[str]) -> bool:
        return not any(Path(value).exists() for value in paths if str(value))

    def _recover_recycle_records(self) -> None:
        """Reconcile only transitional records after journal recovery has run."""
        for record in self.store.records():
            if "copy_lifecycle" in record._extra or "recycle_lifecycle" in record._extra or record._extra.get("_recovered_backup"):continue
            transition=(
                record.status
                if record.status in {"moving","restoring","routing_ingest","routing_library"}
                else str(record._extra.get("recovery_transition") or "")
            )
            if transition not in {"moving","restoring","routing_ingest","routing_library"}:
                continue
            originals=tuple(value.original for value in record.files)
            currents=tuple(value.current for value in record.files)
            try:
                if transition=="moving":
                    move=self.moves.record_for_path(Path(record.current_path))
                    if move is not None and self._all_files_exist(
                        item.get("to","") for item in move.files
                    ):
                        recorded={value.original:value for value in record.files}
                        record.files=tuple(
                            RecycleFile(
                                str(item.get("from") or ""),str(item.get("to") or ""),
                                str(item.get("sha256") or ""),
                                recorded.get(str(item.get("from") or ""),RecycleFile("","")).size,
                                recorded.get(str(item.get("from") or ""),RecycleFile("","")).mtime_ns,
                            ) for item in move.files
                        )
                        record.status=RECYCLE_ACTIVE; record.move_operation_id=move.operation_id
                        record.current_path=move.files[0]["to"] if move.files else record.current_path
                        record.recovery="Recovered a verified interrupted Recycle Bin move."
                    elif self._all_files_exist(originals) and self._no_files_exist(currents):
                        record.status="cancelled"
                        record.recovery="The interrupted move rolled back before Recycle Bin ownership changed."
                    else:
                        record.status="recovery_required"
                        record.recovery="Interrupted Recycle Bin move has uncertain source/destination authority."
                elif transition=="restoring":
                    if self._all_files_exist(originals) and self._no_files_exist(currents):
                        record.status="restored"; record.current_path=record.original_path
                        record.recovery="Recovered a verified interrupted restore."
                    elif self._all_files_exist(currents) and self._no_files_exist(originals):
                        record.status=RECYCLE_ACTIVE
                        record.recovery="Interrupted restore rolled back; the item remains in Recycle Bin."
                    else:
                        record.status="recovery_required"
                        record.recovery="Interrupted restore has uncertain origin/Recycle Bin authority."
                else:
                    pending=record._extra.get("pending_route")
                    targets=tuple(
                        str(value) for value in (pending or {}).get("targets",()) if str(value)
                    ) if isinstance(pending,dict) else ()
                    if self._all_files_exist(targets) and self._no_files_exist(currents):
                        expected=tuple(value.sha256 for value in record.files)
                        if len(targets)!=len(expected) or any(
                            digest and _sha256(Path(target))!=digest
                            for target,digest in zip(targets,expected)
                        ):
                            raise RuntimeError("Routed files no longer match the recorded Recycle Bin hashes.")
                        route=transition.removeprefix("routing_")
                        record.status=f"routed_{route}"; record.current_path=targets[0]
                        move=self.moves.record_for_path(Path(targets[0]))
                        record.routed_operation_id=move.operation_id if move is not None else ""
                        record.recovery="Recovered a verified interrupted Recycle Bin route."
                        record._extra.pop("pending_route",None)
                    elif self._all_files_exist(currents) and self._no_files_exist(targets):
                        record.status=RECYCLE_ACTIVE
                        record.recovery="Interrupted route rolled back; the item remains in Recycle Bin."
                        record._extra.pop("pending_route",None)
                    else:
                        record.status="recovery_required"
                        record.recovery="Interrupted route has uncertain Recycle Bin/destination authority."
                if record.status!="recovery_required":
                    record._extra.pop("recovery_transition",None)
                self.store.save(record)
            except Exception as error:
                self.recovery_issues.append(
                    f"{record.id}: recycle transition recovery needs attention: {error}"
                )

    def _recover_purges(self) -> None:
        journal=self.moves.journal
        for operation in journal.operations():
            if operation.kind not in {"recycle_permanent_delete","recycle_copy_permanent_delete","recycle_native_permanent_delete"}:continue
            if operation.kind in {"recycle_copy_permanent_delete","recycle_native_permanent_delete"}:
                try:self._recover_linked_purge(operation)
                except Exception as error:
                    self.recovery_issues.append(f"{operation.id}: exact purge authority is held: {error}")
                continue
            # Historical untyped deletion never acquires authority over a
            # subsequently qualified lifecycle, even if IDs were reused in
            # malformed journal evidence.
            evidence=operation.result if operation.status=="complete" else self._operation_payload(operation)
            try:
                for record_id in evidence.get("entry_ids",()):
                    candidate=self.store.load_strict(str(record_id))
                    if "copy_lifecycle" in candidate._extra or "recycle_lifecycle" in candidate._extra:
                        raise ValueError("Untyped purge cannot recover a qualified Recycle lifecycle.")
            except Exception as error:
                self.recovery_issues.append(f"{operation.id}: untyped purge authority is held: {error}")
                continue
            if operation.status=="complete":
                for record_id in operation.result.get("entry_ids",()):
                    try:
                        record=self.store.load_strict(str(record_id))
                        if record.status==RECYCLE_ACTIVE:
                            record.status="purged"; record.routed_operation_id=operation.id
                            record.recovery="Permanently deleted after explicit reviewed confirmation."
                            self.store.save(record)
                    except Exception as error:
                        self.recovery_issues.append(f"{operation.id}: {error}")
                continue
            if operation.status!="applying":continue
            payload=self._operation_payload(operation)
            if not payload:continue
            committed=any(item.get("item")=="deletion_committed" for item in operation.checkpoints)
            try:
                if committed:
                    for value in payload.get("files",()):
                        source=Path(value["source"]); staged=Path(value["staged"])
                        if source.exists():
                            raise RuntimeError(f"Committed deletion found restored source authority: {source}")
                        if staged.is_file():
                            if _sha256(staged)!=value["sha256"]:
                                raise RuntimeError(f"Staged delete file changed: {staged}")
                            staged.unlink()
                    for record_id in payload.get("entry_ids",()):
                        record=self.store.load_strict(str(record_id)); record.status="purged"
                        record.routed_operation_id=operation.id
                        record.recovery="Recovered and completed an explicitly committed permanent deletion."
                        self.store.save(record)
                    journal.finish(operation.id,result={
                        "entry_ids":list(payload.get("entry_ids",())),
                        "fingerprint":str(payload.get("fingerprint") or ""),"recovered":True,
                    })
                else:
                    for value in reversed(tuple(payload.get("files",()))):
                        source=Path(value["source"]); staged=Path(value["staged"])
                        if source.exists() and staged.exists():
                            raise RuntimeError(f"Uncommitted purge has duplicate authority for {source}")
                        if staged.is_file():
                            if _sha256(staged)!=value["sha256"]:
                                raise RuntimeError(f"Staged rollback file changed: {staged}")
                            source.parent.mkdir(parents=True,exist_ok=True); os.rename(staged,source)
                    journal.finish(operation.id,"cancelled",message="Interrupted purge rolled back before deletion commit.")
                root=self.configured_root()
                if root:self._cleanup_staging(root/".collectigen-delete"/operation.id)
            except Exception as error:
                self.recovery_issues.append(f"{operation.id}: permanent-delete recovery needs attention: {error}")

    def _recover_linked_purge(self,operation):
        if operation.status not in {"applying","complete"}:return
        typed=operation.kind=="recycle_native_permanent_delete";version=1 if typed else 2
        if operation.metadata.get("engine_version")!=version:raise ValueError("Unsupported retained-family purge version.")
        payload=self._operation_payload(operation)
        witnesses=payload.get("native_witnesses" if typed else "copy_witnesses")
        if payload.get("engine_version")!=version or not isinstance(witnesses,list) or not witnesses:
            raise ValueError("Linked purge has no exact native binding witness.")
        if payload.get("entry_ids")!=operation.metadata.get("entry_ids"):
            raise ValueError("Linked purge record identities disagree.")
        root=self.validate_root();staging=root/".collectigen-delete"/operation.id
        authorities=[item["authority"] for item in witnesses] if typed else witnesses
        if typed:self.native_purge.pairs(witnesses)
        witnessed={item["record_id"]:item for item in authorities}
        if len(witnessed)!=len(witnesses):raise ValueError("Duplicate native purge witnesses.")
        files=payload.get("files",[])
        for record_id in payload["entry_ids"]:
            record=self.store.load_strict(record_id)
            required="copy_lifecycle" in record._extra or "recycle_lifecycle" in record._extra
            if (required and record_id not in witnessed) or (not typed and ("copy_lifecycle" in record._extra)!=(record_id in witnessed)):
                raise ValueError("Linked purge omits a copied family's native identity.")
            selected=[item for item in files if item.get("entry_id")==record_id]
            expected={(path_key(item.current),item.sha256) for item in record.files}
            if {(path_key(item["source"]),item["sha256"]) for item in selected}!=expected or len(selected)!=len(expected):
                raise ValueError("Linked purge file family disagrees with the retained record.")
            if record_id in witnessed:
                tokens={path_key(item["path"]):item["source_token"] for item in witnessed[record_id]["files"]}
                if any(item.get("source_token")!=tokens.get(path_key(item["source"])) for item in selected):
                    raise ValueError("Linked purge instance witnesses disagree.")
        if any(item.get("entry_id") not in payload["entry_ids"] or not _inside(Path(item["source"]),root)
                or not _inside(Path(item["staged"]),staging) for item in files):
            raise ValueError("Linked purge paths escape their exact configured transaction.")
        committed=any(item.get("item")=="deletion_committed" for item in operation.checkpoints)
        if operation.status=="complete" and not committed:
            raise ValueError("Linked purge completion lacks its explicit deletion commit.")
        guard=self.native_purge.guard if typed else self.copy_history.purge_guard
        with self.moves._lock,guard(witnesses,operation_id=operation.id,recovery=True) as stores:
            if committed:
                for item in files:
                    source=Path(item["source"]);target=Path(item["staged"])
                    if source.exists():raise ValueError("Committed linked purge found a replaced retained source.")
                    if target.exists():
                        self._verify_purge_instance(item,target)
                        if _sha256(target)!=item["sha256"]:raise ValueError("Committed linked purge staging changed.")
                        target.unlink()
                for record_id in payload["entry_ids"]:
                    record=self.store.load_strict(record_id)
                    if record.status!="purged" or record.routed_operation_id!=operation.id:
                        record.status="purged";record.routed_operation_id=operation.id
                        record.recovery="Explicit copied-family deletion completed; restore coverage expired."
                        self.store.save(record)
                if typed:self.native_purge.retire(witnesses,operation.id,stores)
                else:self.copy_history.retire_purged(witnesses,operation.id,stores)
                if operation.status!="complete":
                    self.moves.journal.finish(operation.id,result={"entry_ids":payload["entry_ids"],"fingerprint":payload["fingerprint"],"recovered":True})
            else:
                for item in reversed(files):
                    source=Path(item["source"]);target=Path(item["staged"])
                    if source.exists() and target.exists():raise ValueError("Uncommitted linked purge has duplicate authority.")
                    if target.exists():
                        self._verify_purge_instance(item,target)
                        if _sha256(target)!=item["sha256"]:raise ValueError("Uncommitted linked purge staging changed.")
                        if item.get("source_token"):
                            from .history_versions import _reject_reparse
                            _reject_reparse(source)
                        source.parent.mkdir(parents=True,exist_ok=True);os.rename(target,source)
                    if not source.is_file() or _sha256(source)!=item["sha256"]:
                        raise ValueError("Uncommitted linked purge lost its exact retained family.")
                    self._verify_purge_instance(item,source)
                self.moves.journal.finish(operation.id,"cancelled",message="Linked purge rolled back before deletion commit.")
            self._cleanup_staging(staging)


@dataclass(frozen=True,slots=True)
class OrphanAuditItem:
    path: Path
    classification: str
    explanation: str
    actionable: bool
    group_key: str


@dataclass(frozen=True,slots=True)
class OrphanAuditReport:
    items: tuple[OrphanAuditItem,...]
    files_scanned: int
    directories_scanned: int
    truncated: bool=False
    errors: tuple[str,...]=()

    @property
    def actionable(self) -> tuple[OrphanAuditItem,...]:
        return tuple(item for item in self.items if item.actionable)


class OrphanCompanionAuditor:
    """Bounded report-only companion classifier. It never mutates a scanned root."""

    def __init__(
        self,*,excluded_roots: Iterable[str|Path]=(),
        storage: LibraryStorageInspector|None=None,max_files: int=100_000,
    ) -> None:
        self.excluded=tuple(Path(value).resolve() for value in excluded_roots if str(value))
        self.storage=storage or LibraryStorageInspector(); self.max_files=max(1,int(max_files))

    @staticmethod
    def _companion_stem(path: Path) -> str:
        name=path.name
        return name[:-len(".tags.json")] if name.casefold().endswith(".tags.json") else path.stem

    @staticmethod
    def _key(value: str) -> str:
        return unicodedata.normalize("NFC",str(value)).casefold()

    def _excluded(self,path: Path) -> bool:
        return any(_inside(path,root) for root in self.excluded)

    def scan(
        self,roots: Iterable[str|Path],*,
        progress: Callable[[int,int,str],None]|None=None,
        should_cancel: Callable[[],bool]|None=None,
    ) -> OrphanAuditReport:
        stack=[]; errors=[]; items=[]; files_scanned=0; directories_scanned=0; truncated=False
        for value in roots:
            root=Path(value)
            if not root.is_dir():
                errors.append(f"{root}: folder is unavailable or not a directory.")
            else:
                resolved=root.resolve()
                if self._excluded(resolved):
                    items.append(OrphanAuditItem(
                        resolved,"app_managed_excluded",
                        "This app-owned backup or recovery area is excluded from cleanup inspection.",
                        False,f"{resolved}:app-managed",
                    ))
                else:
                    stack.append(resolved)
        while stack:
            if should_cancel is not None and should_cancel():
                raise InterruptedError("Orphan-companion audit cancelled safely; no files changed.")
            directory=stack.pop(); directories_scanned+=1
            try: entries=list(os.scandir(directory))
            except OSError as error:
                errors.append(f"{directory}: {error}"); continue
            file_paths=[]; incomplete_directory=False
            for entry in entries:
                if should_cancel is not None and should_cancel():
                    raise InterruptedError("Orphan-companion audit cancelled safely; no files changed.")
                path=Path(entry.path)
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if self._excluded(path):
                            items.append(OrphanAuditItem(
                                path,"app_managed_excluded",
                                "This app-owned backup or recovery area is excluded from cleanup inspection.",
                                False,f"{path}:app-managed",
                            ))
                        else:stack.append(path)
                        continue
                    if not entry.is_file(follow_symlinks=False):continue
                except OSError as error:
                    errors.append(f"{path}: {error}"); incomplete_directory=True; continue
                files_scanned+=1; file_paths.append(path)
                if files_scanned>=self.max_files:
                    truncated=True; incomplete_directory=True; break
            images=[path for path in file_paths if path.suffix.casefold() in IMG_EXTENSIONS]
            unsupported=[path for path in file_paths if path.suffix.casefold() in COMMON_UNSUPPORTED_IMAGE_SUFFIXES]
            companions=[path for path in file_paths if any(path.name.casefold().endswith(value) for value in COMPANION_SUFFIXES)]
            exact_images={}
            for image in images: exact_images.setdefault(image.stem,[]).append(image)
            normalized_images={}
            for image in images: normalized_images.setdefault(self._key(image.stem),[]).append(image)
            unsupported_keys={self._key(path.stem) for path in unsupported}
            video_keys={self._key(path.stem) for path in file_paths
                        if path.suffix.casefold() in COMMON_VIDEO_SUFFIXES}
            for companion in companions:
                stem=self._companion_stem(companion); key=self._key(stem)
                group=f"{companion.parent}:{key}"
                if self._excluded(companion):
                    items.append(OrphanAuditItem(
                        companion,"app_managed_excluded",
                        "This companion is inside an app-owned backup or recovery area and is not cleanup scope.",False,group,
                    )); continue
                if incomplete_directory:
                    items.append(OrphanAuditItem(
                        companion,"incomplete_directory_inventory",
                        "This directory was not fully checked; a matching image or video may remain outside the checked scope. Cleanup is held.",False,group,
                    )); continue
                exact=exact_images.get(stem,[])
                if len(exact)==1:continue
                if len(exact)>1:
                    items.append(OrphanAuditItem(
                        companion,"shared_supported_stem",
                        "More than one supported image format shares this stem; the companion is retained for explicit image-family review.",False,group,
                    )); continue
                normalized=normalized_images.get(key,[])
                if normalized:
                    items.append(OrphanAuditItem(
                        companion,"case_or_normalization_mismatch",
                        "A supported image differs only by filename case or Unicode normalization; cleanup is held to avoid detaching it.",False,group,
                    )); continue
                if key in unsupported_keys:
                    items.append(OrphanAuditItem(
                        companion,"unsupported_image_present",
                        "An image format CollectiGen does not inventory shares this stem; the companion is not treated as orphaned.",False,group,
                    )); continue
                if key in video_keys:
                    items.append(OrphanAuditItem(
                        companion,"video_stem_present",
                        "A video shares this stem. This image-style companion needs explicit media-family review; it is not an orphan-cleanup candidate.",False,group,
                    )); continue
                if self.storage.requires_remote_content(companion):
                    items.append(OrphanAuditItem(
                        companion,"unavailable_placeholder",
                        "The companion is online-only or placeholder-backed; make it available locally before reviewing cleanup.",False,group,
                    )); continue
                writable=os.access(companion,os.R_OK|os.W_OK)
                items.append(OrphanAuditItem(
                    companion,"image_missing_or_moved",
                    "No current supported or known unsupported image resolves to this companion; the image may have been removed or moved externally.",
                    bool(writable),group,
                ))
            if progress:
                progress(files_scanned,self.max_files,directory.name)
            if truncated:break
        items.sort(key=lambda item:(str(item.path).casefold(),item.classification))
        return OrphanAuditReport(
            tuple(items),files_scanned,directories_scanned,truncated,tuple(errors),
        )
