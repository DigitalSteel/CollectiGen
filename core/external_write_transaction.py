from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass,field
from pathlib import Path
from uuid import uuid4

from .external_write_policy import ExternalWriteBoundary,ExternalWritePolicy


@dataclass(frozen=True)
class ExternalWriteExecution:
    """Runtime approval bound to one frozen outward-write preview."""

    decision_fingerprint: str
    current_policy_fingerprint: str=""
    privacy_confirmed: bool=False


@dataclass(frozen=True)
class ExternalWriteDecision:
    """Frozen policy, protection, and privacy decision for one outward write."""

    policy_fingerprint: str=""
    backup_enabled: bool=True
    backup_directory: Path|None=None
    boundary: ExternalWriteBoundary=field(default_factory=ExternalWriteBoundary)

    @classmethod
    def from_values(
        cls,*,policy_fingerprint: str="",backup_enabled: bool=True,
        backup_directory: str|Path|None=None,
        privacy_confirmation_required: bool=False,privacy_message: str="",
    ) -> "ExternalWriteDecision":
        directory=(
            Path(backup_directory).expanduser().absolute()
            if backup_directory is not None else None
        )
        return cls(
            str(policy_fingerprint or ""),bool(backup_enabled),directory,
            ExternalWriteBoundary(
                bool(privacy_confirmation_required),str(privacy_message or ""),
            ),
        )

    @classmethod
    def from_policy(
        cls,policy: ExternalWritePolicy,boundary: ExternalWriteBoundary,*,
        ask_choice: bool|None=None,
    ) -> "ExternalWriteDecision":
        enabled=policy.backup_enabled(ask_choice)
        return cls(
            policy.fingerprint(),enabled,
            policy.configured_backup_directory if enabled else None,boundary,
        )

    @property
    def privacy_confirmation_required(self) -> bool:
        return self.boundary.confirmation_required

    @property
    def privacy_message(self) -> str:
        return self.boundary.message

    def fingerprint(self) -> str:
        payload={
            "policy_fingerprint":self.policy_fingerprint,
            "backup_enabled":self.backup_enabled,
            "backup_directory":str(self.backup_directory or ""),
            "privacy_confirmation_required":self.privacy_confirmation_required,
            "privacy_message":self.privacy_message,
            "source_roles":list(self.boundary.source_roles),
            "destination_roles":list(self.boundary.destination_roles),
        }
        return hashlib.sha256(json.dumps(
            payload,sort_keys=True,separators=(",",":"),ensure_ascii=False,
        ).encode("utf-8")).hexdigest()

    def execution(
        self,*,current_policy_fingerprint: str="",
        privacy_confirmed: bool=False,
    ) -> ExternalWriteExecution:
        return ExternalWriteExecution(
            self.fingerprint(),str(current_policy_fingerprint or ""),
            bool(privacy_confirmed),
        )


def external_write_validation_issue(
    decision: ExternalWriteDecision,execution: ExternalWriteExecution,
) -> str:
    """Return a stable failure code, or an empty string when execution is valid."""
    if execution.decision_fingerprint!=decision.fingerprint():
        return "stale_decision"
    if (
        decision.policy_fingerprint
        and execution.current_policy_fingerprint!=decision.policy_fingerprint
    ):
        return "stale_policy"
    if decision.privacy_confirmation_required and not execution.privacy_confirmed:
        return "privacy_confirmation_required"
    return ""


def file_fingerprint(path: str|Path) -> dict[str,object]:
    """Return the shared exact-byte fingerprint used by outward-write plans."""
    target=Path(path)
    if not target.exists():
        return {"exists":False}
    digest=hashlib.sha256()
    with target.open("rb") as stream:
        for block in iter(lambda:stream.read(1024*1024),b""):
            digest.update(block)
    stat=target.stat()
    return {
        "exists":True,"size":stat.st_size,"mtime_ns":stat.st_mtime_ns,
        "sha256":digest.hexdigest(),
    }


def content_fingerprint(payload: bytes) -> dict[str,object]:
    return {
        "exists":True,"size":len(payload),
        "sha256":hashlib.sha256(payload).hexdigest(),
    }


def same_content(
    first: dict[str,object],second: dict[str,object],
) -> bool:
    return (
        bool(first.get("exists"))==bool(second.get("exists"))
        and first.get("size")==second.get("size")
        and first.get("sha256")==second.get("sha256")
    )


def atomic_verified_bytes(
    path: str|Path,payload: bytes,*,label: str="External write",
) -> None:
    target=Path(path)
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload); handle.flush(); os.fsync(handle.fileno())
        if not same_content(
            file_fingerprint(temporary),content_fingerprint(payload),
        ):
            raise RuntimeError(f"{label} staged-write verification failed.")
        os.replace(temporary,target)
        if target.read_bytes()!=payload:
            raise RuntimeError(f"{label} failed exact read-back verification.")
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_verified_text(
    path: str|Path,text: str,*,label: str="External write",
) -> None:
    atomic_verified_bytes(path,text.encode("utf-8"),label=label)


def write_verified_json(
    path: str|Path,payload: dict[str,object],*,label: str="Recovery record",
) -> None:
    target=Path(path)
    content=(
        json.dumps(payload,indent=2,ensure_ascii=False,sort_keys=True)+"\n"
    ).encode("utf-8")
    atomic_verified_bytes(target,content,label=label)
    if json.loads(target.read_text(encoding="utf-8"))!=payload:
        raise RuntimeError(f"{label} failed structured read-back verification.")


def copy_verified(
    source: str|Path,destination: str|Path,*,
    expected: dict[str,object]|None=None,label: str="Verified backup",
) -> None:
    source_path=Path(source); destination_path=Path(destination)
    if destination_path.exists():
        raise FileExistsError(f"{label} target already exists: {destination_path}")
    shutil.copy2(source_path,destination_path)
    wanted=expected or file_fingerprint(source_path)
    if not same_content(file_fingerprint(destination_path),wanted):
        raise RuntimeError(f"{label} did not match {source_path.name}.")


def validated_recovery_root(
    directory: str|Path|None,family: str,*,workflow_label: str,
) -> Path:
    if directory is None:
        raise RuntimeError(
            f"{workflow_label} requires the shared external-write backup folder."
        )
    relative=Path(str(family or ""))
    if (
        not str(family or "").strip() or relative.is_absolute()
        or relative==Path(".") or len(relative.parts)!=1
        or ".." in relative.parts
    ):
        raise RuntimeError("The recovery family name is unsafe.")
    base=Path(directory).expanduser().absolute()
    if base.is_symlink():
        raise RuntimeError(
            "The shared external-write backup folder cannot be a symbolic link."
        )
    if base.exists() and not base.is_dir():
        raise RuntimeError(
            "The shared external-write backup destination must be a folder."
        )
    resolved=base.resolve()
    candidate=resolved/relative
    if candidate.is_symlink():
        raise RuntimeError(
            f"The {workflow_label.lower()} recovery folder cannot be a symbolic link."
        )
    root=candidate.resolve()
    if root.parent!=resolved:
        raise RuntimeError(
            f"The {workflow_label.lower()} recovery folder escapes the configured backup destination."
        )
    return root
