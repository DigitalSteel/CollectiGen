from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from .share_safety import (
    SHARE_SAFETY_REVIEWED_ONLY,SHARE_SAFETY_THROUGH_QUESTIONABLE,
    normalize_share_safety_policy,
)


LIBRARY_PROFILE_SCHEMA_VERSION = 1


class LibraryRole(str, Enum):
    """User-declared storage behavior for a peer image library."""

    LOCAL_ONLY = "local_only"
    NETWORK_SHARED = "network_shared"
    CLOUD_SYNCED = "cloud_synced"
    REMOVABLE_ARCHIVE = "removable_archive"


@dataclass(frozen=True)
class LibraryRoleDetails:
    label: str
    summary: str
    consequences: str


LIBRARY_ROLE_DETAILS: dict[LibraryRole, LibraryRoleDetails] = {
    LibraryRole.LOCAL_ONLY: LibraryRoleDetails(
        "Local Only",
        "Private image work that stays inside the explicitly configured local boundary.",
        "Ordinary work remains available offline. Image content, names, paths, Metadata, "
        "Image Knowledge, thumbnails, and derived results are denied to network or hosted "
        "adapters unless a separate exact export or move is confirmed.",
    ),
    LibraryRole.NETWORK_SHARED: LibraryRoleDetails(
        "Network / Shared",
        "A library on SMB, DFS, NAS, or another user-managed shared filesystem.",
        "Availability and speed depend on the shared storage. Cached browsing stays local, "
        "writes require fresh remote checks, and disconnects or read-only access must preserve "
        "the portable source files unchanged.",
    ),
    LibraryRole.CLOUD_SYNCED: LibraryRoleDetails(
        "Cloud Drive / Synced Folder",
        "A folder or virtual drive managed by a separate cloud desktop app, such as Google Drive, OneDrive, or Dropbox.",
        "Choosing this type does not restrict ordinary work or change the cloud app's settings. "
        "Files available offline work normally. Online-only files remain visible without bulk "
        "downloads; make the complete image family available offline and refresh before reading, "
        "editing, analyzing, or moving it. CollectiGen caches stay local, and cloud sync status "
        "is never treated as write verification.",
    ),
    LibraryRole.REMOVABLE_ARCHIVE: LibraryRoleDetails(
        "Removable / Archive",
        "A library on removable media or storage that is not always connected.",
        "Browsing can use an explicitly stale local cache while the device is absent. Writes "
        "require the exact device and current files to be available, writable, and revalidated.",
    ),
}


_ROLE_ALIASES = {
    "local": LibraryRole.LOCAL_ONLY.value,
    "private": LibraryRole.LOCAL_ONLY.value,
    "shared": LibraryRole.NETWORK_SHARED.value,
    "network": LibraryRole.NETWORK_SHARED.value,
    "cloud": LibraryRole.CLOUD_SYNCED.value,
    "removable": LibraryRole.REMOVABLE_ARCHIVE.value,
    "archive": LibraryRole.REMOVABLE_ARCHIVE.value,
}


def _path_key(value: str | Path) -> str:
    raw = str(Path(value).expanduser())
    return os.path.normcase(os.path.normpath(os.path.abspath(raw)))


def _stable_library_id(root: str | Path, name: str = "") -> str:
    material = f"{_path_key(root)}\0{str(name).strip().casefold()}".encode("utf-8")
    return "library-" + hashlib.sha256(material).hexdigest()[:20]


def _is_nested(first: str | Path, second: str | Path) -> bool:
    first_key = _path_key(first)
    second_key = _path_key(second)
    try:
        common = os.path.commonpath((first_key, second_key))
    except ValueError:
        return False
    return common == first_key or common == second_key


def _is_within(path: str | Path, root: str | Path) -> bool:
    path_key = _path_key(path)
    root_key = _path_key(root)
    try:
        return os.path.commonpath((path_key, root_key)) == root_key
    except ValueError:
        return False


@dataclass(frozen=True)
class LibraryProfile:
    stable_id: str
    name: str
    root: str
    role: str = LibraryRole.LOCAL_ONLY.value
    schema_version: int = LIBRARY_PROFILE_SCHEMA_VERSION
    extra: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def create(
        cls,
        name: str,
        root: str | Path,
        role: str | LibraryRole = LibraryRole.LOCAL_ONLY,
        *,
        stable_id: str = "",
    ) -> "LibraryProfile":
        role_value = role.value if isinstance(role, LibraryRole) else str(role).strip()
        role_value = _ROLE_ALIASES.get(role_value, role_value)
        clean_name = " ".join(str(name).split())
        clean_root = str(Path(root).expanduser()) if str(root).strip() else ""
        return cls(
            str(stable_id).strip() or _stable_library_id(clean_root, clean_name),
            clean_name,
            clean_root,
            role_value,
        )

    @classmethod
    def from_dict(cls, value: Any) -> "LibraryProfile":
        if not isinstance(value, dict):
            raise ValueError("A named library record must be an object.")
        known = {"schema_version", "stable_id", "id", "name", "root", "role"}
        root = str(value.get("root") or "").strip()
        name = " ".join(str(value.get("name") or "").split())
        role = str(value.get("role") or LibraryRole.LOCAL_ONLY.value).strip()
        role = _ROLE_ALIASES.get(role, role)
        stable_id = str(value.get("stable_id") or value.get("id") or "").strip()
        try:
            schema_version = max(1, int(value.get("schema_version") or 1))
        except (TypeError, ValueError):
            schema_version = 1
        return cls(
            stable_id or _stable_library_id(root, name),
            name,
            root,
            role,
            schema_version,
            {key: item for key, item in value.items() if key not in known},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.extra,
            "schema_version": int(self.schema_version),
            "stable_id": self.stable_id,
            "name": self.name,
            "root": self.root,
            "role": self.role,
        }

    @property
    def recognized_role(self) -> LibraryRole | None:
        try:
            return LibraryRole(self.role)
        except ValueError:
            return None

    @property
    def role_details(self) -> LibraryRoleDetails | None:
        role = self.recognized_role
        return LIBRARY_ROLE_DETAILS.get(role) if role is not None else None

    @property
    def display_role(self) -> str:
        details = self.role_details
        return details.label if details is not None else f"Unsupported ({self.role})"

    @property
    def share_safety_policy(self) -> str:
        configured=self.extra.get("share_safety_policy")
        if configured:
            return normalize_share_safety_policy(configured)
        return (
            SHARE_SAFETY_REVIEWED_ONLY
            if self.recognized_role is LibraryRole.LOCAL_ONLY else
            SHARE_SAFETY_THROUGH_QUESTIONABLE
        )

    def with_share_safety_policy(self, policy_id: str) -> "LibraryProfile":
        extra=dict(self.extra)
        extra["share_safety_policy"]=normalize_share_safety_policy(policy_id)
        return replace(self,extra=extra)

    @property
    def require_reviewed_image_knowledge(self) -> bool:
        configured=self.extra.get("require_reviewed_image_knowledge")
        return configured if isinstance(configured,bool) else True

    def with_reviewed_image_knowledge_required(
        self,required: bool,
    ) -> "LibraryProfile":
        extra=dict(self.extra)
        extra["require_reviewed_image_knowledge"]=bool(required)
        return replace(self,extra=extra)

    def with_changes(self, **changes: Any) -> "LibraryProfile":
        if "role" in changes and isinstance(changes["role"], LibraryRole):
            changes["role"] = changes["role"].value
        return replace(self, **changes)


@dataclass(frozen=True)
class LibraryProfileIssue:
    code: str
    message: str
    profile_id: str = ""


class LibraryProfileRegistry:
    """Versioned, stable-ID registry for the small set of configured libraries."""

    def __init__(self, profiles: Iterable[LibraryProfile] = ()) -> None:
        self._profiles = list(profiles)

    @classmethod
    def from_dicts(cls, values: Any) -> "LibraryProfileRegistry":
        profiles: list[LibraryProfile] = []
        for value in values if isinstance(values, list) else []:
            try:
                profiles.append(LibraryProfile.from_dict(value))
            except (TypeError, ValueError):
                # Invalid records remain in AppSettings' recovery snapshots. They are not
                # activated or rewritten into a partially understood library profile.
                continue
        return cls(profiles)

    @property
    def profiles(self) -> tuple[LibraryProfile, ...]:
        return tuple(self._profiles)

    def to_dicts(self) -> list[dict[str, Any]]:
        return [profile.to_dict() for profile in self._profiles]

    def get(self, stable_id: str) -> LibraryProfile | None:
        return next(
            (profile for profile in self._profiles if profile.stable_id == stable_id),
            None,
        )

    def find_root(self, root: str | Path) -> LibraryProfile | None:
        if not str(root).strip():
            return None
        wanted = _path_key(root)
        return next(
            (profile for profile in self._profiles if profile.root and _path_key(profile.root) == wanted),
            None,
        )

    def profile_for_path(self, path: str | Path) -> LibraryProfile | None:
        candidates = [
            profile for profile in self._profiles
            if profile.root and _is_within(path, profile.root)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda profile: len(_path_key(profile.root)))

    def validate(
        self,
        profile: LibraryProfile,
        *,
        replacing_id: str = "",
    ) -> tuple[LibraryProfileIssue, ...]:
        issues: list[LibraryProfileIssue] = []
        if not profile.stable_id:
            issues.append(LibraryProfileIssue("missing_id", "The library needs a stable identity."))
        if not profile.name:
            issues.append(LibraryProfileIssue("missing_name", "Enter a short library name."))
        elif len(profile.name) > 80:
            issues.append(LibraryProfileIssue("long_name", "Library names can contain at most 80 characters."))
        if not profile.root:
            issues.append(LibraryProfileIssue("missing_root", "Choose the library folder."))
        elif not Path(profile.root).expanduser().is_absolute():
            issues.append(LibraryProfileIssue("relative_root", "Choose an absolute library folder."))
        if profile.recognized_role is None:
            issues.append(LibraryProfileIssue("unknown_role", "Choose a supported library type."))

        for existing in self._profiles:
            if existing.stable_id == replacing_id:
                continue
            if existing.stable_id == profile.stable_id:
                issues.append(LibraryProfileIssue(
                    "duplicate_id", f"{existing.name} already uses this library identity.", existing.stable_id,
                ))
            if existing.name.casefold() == profile.name.casefold():
                issues.append(LibraryProfileIssue(
                    "duplicate_name", f'A library named "{existing.name}" already exists.', existing.stable_id,
                ))
            if profile.root and existing.root and _path_key(existing.root) == _path_key(profile.root):
                issues.append(LibraryProfileIssue(
                    "duplicate_root", f'{existing.name} already uses this folder.', existing.stable_id,
                ))
            elif profile.root and existing.root and _is_nested(existing.root, profile.root):
                issues.append(LibraryProfileIssue(
                    "overlapping_root",
                    f'{profile.name or "This library"} overlaps {existing.name}. Named library roots cannot contain one another.',
                    existing.stable_id,
                ))
        return tuple(issues)

    def add(self, profile: LibraryProfile) -> None:
        issues = self.validate(profile)
        if issues:
            raise ValueError(" ".join(issue.message for issue in issues))
        self._profiles.append(profile)

    def replace(self, profile: LibraryProfile) -> None:
        if self.get(profile.stable_id) is None:
            raise KeyError(profile.stable_id)
        issues = self.validate(profile, replacing_id=profile.stable_id)
        if issues:
            raise ValueError(" ".join(issue.message for issue in issues))
        self._profiles = [
            profile if existing.stable_id == profile.stable_id else existing
            for existing in self._profiles
        ]

    def remove(self, stable_id: str) -> LibraryProfile:
        profile = self.get(stable_id)
        if profile is None:
            raise KeyError(stable_id)
        self._profiles = [item for item in self._profiles if item.stable_id != stable_id]
        return profile

    def unique_name(self, preferred: str) -> str:
        base = " ".join(str(preferred).split()) or "Working Library"
        used = {profile.name.casefold() for profile in self._profiles}
        if base.casefold() not in used:
            return base
        number = 2
        while f"{base} {number}".casefold() in used:
            number += 1
        return f"{base} {number}"


@dataclass(frozen=True)
class LibraryBoundaryDecision:
    requires_explicit_confirmation: bool
    blocked_by_default: bool
    message: str


class LibraryPrivacyPolicy:
    """Central deny-by-default boundary used by future network and movement adapters."""

    @staticmethod
    def permits_derived_network_content(profile: LibraryProfile) -> bool:
        return profile.recognized_role in {
            LibraryRole.NETWORK_SHARED,
            LibraryRole.CLOUD_SYNCED,
            LibraryRole.REMOVABLE_ARCHIVE,
        }

    @classmethod
    def require_derived_network_content(cls, profile: LibraryProfile) -> None:
        if not cls.permits_derived_network_content(profile):
            boundary = (
                "Local Only"
                if profile.recognized_role is LibraryRole.LOCAL_ONLY
                else "an unsupported fail-closed storage role"
            )
            raise PermissionError(
                f'{profile.name or "This library"} uses {boundary}. Its content, names, paths, '
                "Metadata, Image Knowledge, thumbnails, prompts, and derived results are denied "
                "to network or hosted adapters without a separate exact export confirmation."
            )

    @classmethod
    def permits_path_network_content(
        cls, registry: LibraryProfileRegistry, path: str | Path,
    ) -> bool:
        profile = registry.profile_for_path(path)
        return bool(profile and cls.permits_derived_network_content(profile))

    @classmethod
    def require_path_network_content(
        cls, registry: LibraryProfileRegistry, path: str | Path,
    ) -> LibraryProfile:
        profile = registry.profile_for_path(path)
        if profile is None:
            raise PermissionError(
                "This path is not inside a recognized named library. Unclassified content is "
                "denied to network or hosted adapters."
            )
        cls.require_derived_network_content(profile)
        return profile

    @staticmethod
    def movement(source: LibraryProfile, destination: LibraryProfile) -> LibraryBoundaryDecision:
        source_protected = source.recognized_role in {None, LibraryRole.LOCAL_ONLY}
        destination_protected = destination.recognized_role in {None, LibraryRole.LOCAL_ONLY}
        if source_protected == destination_protected:
            return LibraryBoundaryDecision(False, False, "The move stays within the same declared privacy class.")
        return LibraryBoundaryDecision(
            True,
            True,
            "This move crosses the Local Only boundary. Preview and separately confirm every "
            "image-family member, path consequence, and destination before any bytes are copied.",
        )

    @staticmethod
    def requires_verified_copy(
        source: LibraryProfile | None,
        destination: LibraryProfile | None,
    ) -> bool:
        """Return whether a move must avoid same-volume rename optimization.

        A local filesystem device identity is not a durability guarantee for a
        network share, cloud-mounted drive, removable device, or unsupported
        fail-closed role. Any explicitly classified non-local endpoint therefore
        uses copy, read-back verification, and only then source removal.
        Unclassified paths retain the existing Local Only treatment.
        """
        return any(
            profile is not None
            and profile.recognized_role is not LibraryRole.LOCAL_ONLY
            for profile in (source, destination)
        )

    @classmethod
    def movement_for_paths(
        cls,
        registry: LibraryProfileRegistry,
        paths: Iterable[str | Path],
        destination: LibraryProfile,
    ) -> LibraryBoundaryDecision:
        source_profiles = tuple(registry.profile_for_path(path) for path in paths)
        source_protected = tuple(
            profile is None
            or profile.recognized_role in {None, LibraryRole.LOCAL_ONLY}
            for profile in source_profiles
        )
        destination_protected = destination.recognized_role in {
            None,LibraryRole.LOCAL_ONLY,
        }
        if not any(value != destination_protected for value in source_protected):
            return LibraryBoundaryDecision(
                False,False,
                "The transfer stays within the same declared privacy class.",
            )
        unclassified = sum(profile is None for profile in source_profiles)
        source_note = (
            f" {unclassified:,} selected source(s) are outside any named library and are treated as Local Only."
            if unclassified else ""
        )
        return LibraryBoundaryDecision(
            True,True,
            "This transfer crosses the Local Only boundary. The exact sources, complete owned "
            "families, destination role, routes, and collision results must be previewed and "
            f"separately confirmed before any bytes are copied.{source_note}",
        )
