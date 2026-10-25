from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .library_profiles import LibraryProfile,LibraryProfileRegistry,LibraryRole


EXTERNAL_WRITE_BACKUP_ALWAYS="always"
EXTERNAL_WRITE_BACKUP_ASK="ask"
EXTERNAL_WRITE_BACKUP_OFF="off"
EXTERNAL_WRITE_BACKUP_MODES=(
    EXTERNAL_WRITE_BACKUP_ALWAYS,
    EXTERNAL_WRITE_BACKUP_ASK,
    EXTERNAL_WRITE_BACKUP_OFF,
)


def normalize_external_write_backup_mode(value) -> str:
    selected=str(value or "").strip().casefold()
    return (
        selected if selected in EXTERNAL_WRITE_BACKUP_MODES
        else EXTERNAL_WRITE_BACKUP_ALWAYS
    )


def _resolved(value: str|Path) -> Path:
    return Path(value).expanduser().resolve()


def _absolute(value: str|Path) -> Path:
    """Keep the selected path spelling so redirected roots remain detectable."""
    return Path(value).expanduser().absolute()


def _overlaps(first: Path,second: Path) -> bool:
    return (
        first==second
        or first.is_relative_to(second)
        or second.is_relative_to(first)
    )


def _privacy_class(profile: LibraryProfile|None) -> str:
    return (
        "local_only"
        if profile is None
        or profile.recognized_role in {None,LibraryRole.LOCAL_ONLY}
        else "shared"
    )


def _profile_label(profile: LibraryProfile|None) -> str:
    return profile.display_role if profile is not None else "Local Only (unclassified)"


@dataclass(frozen=True)
class ExternalWriteBoundary:
    confirmation_required: bool=False
    message: str="The export remains inside its declared storage privacy class."
    source_roles: tuple[str,...]=()
    destination_roles: tuple[str,...]=()


@dataclass(frozen=True)
class ExternalWritePolicy:
    """Shared Automation-owned protection policy for outward file writes."""

    backup_mode: str=EXTERNAL_WRITE_BACKUP_ALWAYS
    backup_directory: str=""

    @classmethod
    def from_settings(cls,settings) -> "ExternalWritePolicy":
        if settings is None:
            return cls()
        return cls(
            normalize_external_write_backup_mode(
                getattr(settings,"external_write_backup_mode","")
            ),
            str(getattr(settings,"external_write_backup_directory","") or ""),
        )

    @property
    def configured_backup_directory(self) -> Path|None:
        return (
            _absolute(self.backup_directory)
            if self.backup_directory.strip() else None
        )

    @property
    def asks_each_time(self) -> bool:
        return self.backup_mode==EXTERNAL_WRITE_BACKUP_ASK

    def backup_enabled(self,ask_choice: bool|None=None) -> bool:
        if self.backup_mode==EXTERNAL_WRITE_BACKUP_ALWAYS:
            return True
        if self.backup_mode==EXTERNAL_WRITE_BACKUP_OFF:
            return False
        return True if ask_choice is None else bool(ask_choice)

    def fingerprint(self) -> str:
        payload={
            "backup_mode":self.backup_mode,
            "backup_directory":str(
                self.configured_backup_directory or ""
            ),
        }
        return hashlib.sha256(json.dumps(
            payload,sort_keys=True,separators=(",",":"),ensure_ascii=False,
        ).encode("utf-8")).hexdigest()

    def backup_destination_issues(
        self,registry: LibraryProfileRegistry|None=None,
        *,enabled: bool=True,
    ) -> tuple[str,...]:
        if not enabled:
            return ()
        destination=self.configured_backup_directory
        if destination is None:
            return (
                "Choose the shared external-write backup folder in Setup > "
                "Libraries & Storage before starting a protected write.",
            )
        issues=[]
        if destination.is_symlink():
            issues.append(
                "The external-write backup folder cannot be a symbolic link."
            )
        if destination.exists() and not destination.is_dir():
            issues.append(
                "The external-write backup destination must be a folder."
            )
        resolved_destination=destination.resolve()
        for profile in (registry.profiles if registry is not None else ()):
            if not profile.root:
                continue
            root=_resolved(profile.root)
            if _overlaps(resolved_destination,root):
                issues.append(
                    "The external-write backup folder must stay outside every "
                    f'named image library; it overlaps "{profile.name}".'
                )
        return tuple(issues)

    def boundary_for_pairs(
        self,registry: LibraryProfileRegistry,
        source_destination_pairs: Iterable[tuple[str|Path,str|Path]],
        *,backup_enabled: bool,artifact_label: str="derived TXT",
        workflow_label: str="Training TXT export",
    ) -> ExternalWriteBoundary:
        source_roles=[]
        destination_roles=[]
        crossings=[]
        backup=self.configured_backup_directory if backup_enabled else None
        for source,destination in source_destination_pairs:
            source_profile=registry.profile_for_path(source)
            destination_profile=registry.profile_for_path(destination)
            source_label=_profile_label(source_profile)
            destination_label=_profile_label(destination_profile)
            source_roles.append(source_label)
            destination_roles.append(destination_label)
            if _privacy_class(source_profile)!=_privacy_class(destination_profile):
                crossings.append(
                    f"{artifact_label}: {source_label} → {destination_label}"
                )
            if backup is not None:
                backup_profile=registry.profile_for_path(backup)
                backup_label=_profile_label(backup_profile)
                destination_roles.append(backup_label)
                if _privacy_class(source_profile)!=_privacy_class(backup_profile):
                    crossings.append(
                        f"verified backup: {source_label} → {backup_label}"
                    )
        source_values=tuple(dict.fromkeys(source_roles))
        destination_values=tuple(dict.fromkeys(destination_roles))
        crossing_values=tuple(dict.fromkeys(crossings))
        if not crossing_values:
            display_label=(
                "Training TXT" if artifact_label=="derived TXT" else
                artifact_label[:1].upper()+artifact_label[1:]
            )
            return ExternalWriteBoundary(
                False,
                f"{display_label} and any verified backups remain inside the "
                "selected sources' declared storage privacy class.",
                source_values,destination_values,
            )
        return ExternalWriteBoundary(
            True,
            f"This {workflow_label} crosses the Local Only storage boundary: "
            +"; ".join(crossing_values)
            +". Confirm this exact scope and destination before writing.",
            source_values,destination_values,
        )
