from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass,field,replace
from datetime import datetime,timezone
from pathlib import Path
from typing import Callable,Iterable
from uuid import uuid4

from .external_write_transaction import (
    ExternalWriteDecision,external_write_validation_issue,write_verified_json,
)
from .operation_journal import OperationJournal
from .verified_transfer import VerifiedTransferEngine,VerifiedTransferPlan


def _available_bytes(path: Path) -> int|None:
    probe=path
    while not probe.exists() and probe!=probe.parent:
        probe=probe.parent
    try:
        return int(shutil.disk_usage(probe).free)
    except OSError:
        return None


def _signature(path: Path) -> tuple[int,int]:
    try:
        stat=path.stat()
        return stat.st_size,stat.st_mtime_ns
    except OSError:
        return -1,-1


def _scope_fingerprint(items: Iterable["LibraryMoveProtectionItem"]) -> str:
    payload=[
        {
            "image":str(item.image.resolve()),
            "members":[
                {
                    "source":str(member.source.resolve()),
                    "target":str(member.target.resolve()),
                    "size":member.size,
                    "mtime_ns":member.mtime_ns,
                }
                for member in item.transfer.members
            ],
        }
        for item in items
    ]
    return hashlib.sha256(json.dumps(
        payload,sort_keys=True,separators=(",",":"),ensure_ascii=False,
    ).encode("utf-8")).hexdigest()


def _write_manifest(path: Path,payload: dict[str,object]) -> None:
    write_verified_json(
        path,payload,label="Protected image-move recovery record",
    )


def _copy_verified(source: Path,destination: Path,expected_hash: str) -> None:
    if destination.exists():
        raise FileExistsError(f"Protected backup target already exists: {destination}")
    shutil.copy2(source,destination)
    digest=hashlib.sha256()
    with destination.open("rb") as stream:
        for block in iter(lambda:stream.read(1024*1024),b""):
            digest.update(block)
    if digest.hexdigest()!=expected_hash:
        raise OSError(f"Protected backup read-back did not match {source.name}.")


@dataclass(frozen=True)
class LibraryMoveProtectionItem:
    image: Path
    transfer: VerifiedTransferPlan


@dataclass(frozen=True)
class LibraryMoveProtectionPlan:
    items: tuple[LibraryMoveProtectionItem,...]=()
    backup_enabled: bool=True
    backup_directory: Path|None=None
    external_policy_fingerprint: str=""
    scope_fingerprint: str=""
    required_free_bytes: int=0
    available_free_bytes: int|None=None
    privacy_confirmation_required: bool=False
    privacy_message: str=""
    errors: tuple[str,...]=()

    @property
    def source_count(self) -> int:
        return len(self.items)

    @property
    def file_count(self) -> int:
        return sum(len(item.transfer.members) for item in self.items)

    @property
    def space_sufficient(self) -> bool:
        return (
            not self.backup_enabled
            or self.available_free_bytes is not None
            and self.available_free_bytes>=self.required_free_bytes
        )

    @property
    def ready(self) -> bool:
        return not self.errors and self.space_sufficient

    @property
    def write_decision(self) -> ExternalWriteDecision:
        return ExternalWriteDecision.from_values(
            policy_fingerprint=self.external_policy_fingerprint,
            backup_enabled=self.backup_enabled,
            backup_directory=self.backup_directory,
            privacy_confirmation_required=self.privacy_confirmation_required,
            privacy_message=self.privacy_message,
        )

    def subset(self,images: Iterable[str|Path]) -> "LibraryMoveProtectionPlan":
        wanted={str(Path(value).resolve()) for value in images}
        items=tuple(
            item for item in self.items if str(item.image.resolve()) in wanted
        )
        required=(
            sum(member.size for item in items for member in item.transfer.members)
            +max(64*1024,sum(len(item.transfer.members) for item in items)*2048)
            if self.backup_enabled and items else 0
        )
        return replace(
            self,items=items,scope_fingerprint=_scope_fingerprint(items),
            required_free_bytes=required,
        )


@dataclass
class LibraryMoveProtectionSession:
    plan: LibraryMoveProtectionPlan
    recovery_manifest: Path|None=None
    operation_id: str=""
    journal: OperationJournal|None=None
    payload: dict[str,object]=field(default_factory=dict)


class LibraryMoveProtectionService:
    """Verify complete-family originals before an organization move begins."""

    SCHEMA_VERSION=1
    RECOVERY_KIND="collectigen_library_move_protection"
    JOURNAL_KIND="library_move_protection"

    def __init__(self,engine: VerifiedTransferEngine|None=None):
        self.engine=engine or VerifiedTransferEngine()

    @staticmethod
    def recovery_root(backup_directory: str|Path) -> Path:
        return Path(backup_directory).absolute()/"image-file-moves"

    @classmethod
    def _journal(cls,backup_directory: str|Path) -> OperationJournal:
        return OperationJournal(cls.recovery_root(backup_directory)/"operations.jsonl")

    def preview(
        self,transfers: Iterable[VerifiedTransferPlan],*,backup_enabled: bool,
        backup_directory: str|Path|None=None,
        external_policy_fingerprint: str="",
    ) -> LibraryMoveProtectionPlan:
        items=[]; seen=set(); errors=[]
        for transfer in transfers:
            image=Path(transfer.image)
            key=str(image.resolve()).casefold()
            if key in seen:
                continue
            seen.add(key)
            if not transfer.ready:
                errors.extend(transfer.errors)
                continue
            if not transfer.members or transfer.duplicate_target is not None:
                continue
            items.append(LibraryMoveProtectionItem(image,transfer))
        directory=(
            Path(backup_directory).absolute()
            if backup_enabled and backup_directory is not None else None
        )
        if backup_enabled:
            if directory is None:
                errors.append("A verified external-write backup folder is required.")
            elif directory.is_symlink():
                errors.append("The external-write backup folder cannot be a symbolic link.")
            elif directory.exists() and not directory.is_dir():
                errors.append("The external-write backup destination must be a folder.")
        required=(
            sum(member.size for item in items for member in item.transfer.members)
            +max(64*1024,sum(len(item.transfer.members) for item in items)*2048)
            if backup_enabled and items else 0
        )
        return LibraryMoveProtectionPlan(
            tuple(items),bool(backup_enabled),directory,
            str(external_policy_fingerprint or ""),_scope_fingerprint(items),
            required,(_available_bytes(directory) if directory is not None else None),
            errors=tuple(dict.fromkeys(errors)),
        )

    def prepare(
        self,plan: LibraryMoveProtectionPlan,*,privacy_confirmed: bool=False,
        external_policy_fingerprint: str="",
        progress: Callable[[int,int,str],None]|None=None,
        should_cancel: Callable[[],bool]|None=None,
    ) -> LibraryMoveProtectionSession:
        decision=plan.write_decision
        validation_issue=external_write_validation_issue(
            decision,decision.execution(
                current_policy_fingerprint=external_policy_fingerprint,
                privacy_confirmed=privacy_confirmed,
            ),
        )
        if validation_issue in {"stale_decision","stale_policy"}:
            raise ValueError(
                "The shared external-write protection changed after preview."
            )
        if validation_issue=="privacy_confirmation_required":
            raise PermissionError(
                "The exact Local Only boundary move and protected backup were not confirmed."
            )
        if plan.errors:
            raise ValueError(" ".join(plan.errors))
        if not plan.backup_enabled or not plan.items:
            return LibraryMoveProtectionSession(plan)
        if plan.backup_directory is None:
            raise ValueError("A verified external-write backup folder is required.")
        if plan.backup_directory.is_symlink():
            raise ValueError("The external-write backup folder cannot be a symbolic link.")
        if plan.backup_directory.exists() and not plan.backup_directory.is_dir():
            raise ValueError("The external-write backup destination must be a folder.")
        available=_available_bytes(plan.backup_directory)
        if available is None or available<plan.required_free_bytes:
            raise OSError(
                "The configured external-write backup folder has insufficient verified free space."
            )
        hashes=[]
        for item in plan.items:
            if should_cancel is not None and should_cancel():
                raise InterruptedError(
                    "Library move cancelled safely before any originals or destinations changed."
                )
            hashes.append(self.engine.prepare_hashes(item.transfer,should_cancel))
        root=self.recovery_root(plan.backup_directory)
        if root.is_symlink():
            raise ValueError("The image-file move recovery folder cannot be a symbolic link.")
        root.mkdir(parents=True,exist_ok=True)
        stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        directory=root/f"operation_{stamp}_{uuid4().hex[:8]}"
        directory.mkdir(parents=False,exist_ok=False)
        manifest=directory/"recovery.json"
        journal=self._journal(plan.backup_directory)
        operation_id=""; payload={}
        try:
            operation_id=journal.begin(
                self.JOURNAL_KIND,expected=plan.file_count,
                reason="Protect complete image families before library organization",
                metadata={
                    "scope_fingerprint":plan.scope_fingerprint,
                    "external_policy_fingerprint":plan.external_policy_fingerprint,
                    "source_count":plan.source_count,"file_count":plan.file_count,
                },
            )
            entries=[]
            for family_index,(item,item_hashes) in enumerate(zip(plan.items,hashes)):
                members=[]
                for member_index,member in enumerate(item.transfer.members):
                    relative=f"family_{family_index:05d}_member_{member_index:02d}{''.join(member.source.suffixes)}"
                    members.append({
                        "source":str(member.source.resolve()),
                        "target":str(member.target.resolve()),
                        "backup":relative,
                        "size":member.size,"mtime_ns":member.mtime_ns,
                        "sha256":item_hashes[str(member.source)],
                    })
                entries.append({
                    "image":str(item.image.resolve()),"state":"pending",
                    "members":members,
                })
            payload={
                "schema_version":self.SCHEMA_VERSION,"kind":self.RECOVERY_KIND,
                "status":"backing_up",
                "created_at":datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "scope_fingerprint":plan.scope_fingerprint,
                "external_policy_fingerprint":plan.external_policy_fingerprint,
                "operation_id":operation_id,"source_count":plan.source_count,
                "file_count":plan.file_count,"entries":entries,
            }
            _write_manifest(manifest,payload)
            for family_index,(item,entry) in enumerate(zip(plan.items,entries),1):
                if should_cancel is not None and should_cancel():
                    raise InterruptedError(
                        "Library move cancelled safely while protecting originals; no source or destination changed."
                    )
                for member,member_entry in zip(
                    item.transfer.members,entry["members"]
                ):
                    _copy_verified(
                        member.source,directory/str(member_entry["backup"]),
                        str(member_entry["sha256"]),
                    )
                    if member.signature()!=_signature(member.source):
                        raise RuntimeError(
                            f"{member.source.name} changed while its protected copy was verified."
                        )
                    journal.append_change(
                        operation_id,item=str(member.source.resolve()),
                        before={
                            "size":member.size,"mtime_ns":member.mtime_ns,
                            "sha256":member_entry["sha256"],
                        },
                        after={
                            "backup":str(member_entry["backup"]),
                            "sha256":member_entry["sha256"],
                        },
                        reason="Verified protected original before library organization",
                        metadata={"family":family_index-1},
                    )
                entry["state"]="verified"
                _write_manifest(manifest,payload)
                if progress is not None:
                    progress(family_index,len(plan.items),item.image.name)
            for item,expected_hashes in zip(plan.items,hashes):
                current_hashes=self.engine.prepare_hashes(
                    item.transfer,should_cancel,
                )
                if current_hashes!=expected_hashes:
                    raise RuntimeError(
                        f"{item.image.name} changed after its protected copy was created."
                    )
            payload["status"]="ready"
            _write_manifest(manifest,payload)
            return LibraryMoveProtectionSession(
                plan,manifest,operation_id,journal,payload,
            )
        except Exception as exc:
            if payload:
                payload["status"]=(
                    "cancelled" if isinstance(exc,InterruptedError) else "failed"
                )
                payload["message"]=str(exc)
                try:
                    _write_manifest(manifest,payload)
                except Exception:
                    pass
            if operation_id:
                try:
                    journal.finish(
                        operation_id,
                        "cancelled" if isinstance(exc,InterruptedError) else "failed",
                        message=str(exc),
                    )
                except Exception:
                    pass
            raise

    @staticmethod
    def finish(
        session: LibraryMoveProtectionSession,*,status: str,
        result: dict[str,object]|None=None,
    ) -> None:
        if session.recovery_manifest is None or session.journal is None:
            return
        session.payload["status"]=status
        session.payload["result"]=dict(result or {})
        _write_manifest(session.recovery_manifest,session.payload)
        session.journal.finish(
            session.operation_id,status,result=dict(result or {}),
        )
        session.journal.prune()
