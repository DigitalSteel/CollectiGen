from __future__ import annotations

import os
import re
import stat as stat_module
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable


# These attributes can be inspected without opening file content. Opening a
# matching path can ask the registered sync provider to hydrate remote bytes.
WINDOWS_FILE_ATTRIBUTE_OFFLINE = 0x00001000
WINDOWS_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
# A provider can mark content to remain present on the device. This is not a
# remote-content signal: available-offline files must retain ordinary behavior.
WINDOWS_FILE_ATTRIBUTE_PINNED = 0x00080000
WINDOWS_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
WINDOWS_REMOTE_CONTENT_ATTRIBUTES = (
    WINDOWS_FILE_ATTRIBUTE_OFFLINE
    | WINDOWS_FILE_ATTRIBUTE_RECALL_ON_OPEN
    | WINDOWS_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)

_CONFLICT_MARKERS = (
    re.compile(r"(?i)(?:^|[ ._(-])conflicted[ _-]*copy(?:[ ._)-]|$)"),
    re.compile(r"(?i)\.sync-conflict-"),
)
_CONFLICT_SPLIT = re.compile(
    r"(?i)(?:[ ._(-]+conflicted[ _-]*copy|\.sync-conflict-)"
)
_FAMILY_SUFFIXES = (".tags", ".tags.json", ".txt", ".xmp")


class LibraryFileState(str, Enum):
    AVAILABLE = "available"
    ONLINE_ONLY = "online_only"
    CONFLICT = "conflict"
    READ_ONLY = "read_only"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class LibraryFileAvailability:
    state: LibraryFileState = LibraryFileState.AVAILABLE
    detail: str = "Available locally"
    recovery: str = ""
    readable: bool = True
    writable: bool = True
    conflict_paths: tuple[Path, ...] = ()

    @property
    def ready_for_write(self) -> bool:
        return self.readable and self.writable and self.state is LibraryFileState.AVAILABLE

    @property
    def label(self) -> str:
        return {
            LibraryFileState.AVAILABLE: "Available locally",
            LibraryFileState.ONLINE_ONLY: "Online-only",
            LibraryFileState.CONFLICT: "Sync conflict held",
            LibraryFileState.READ_ONLY: "Read-only",
            LibraryFileState.UNAVAILABLE: "Unavailable",
        }[self.state]


@dataclass(frozen=True, slots=True)
class _EnumeratedFamilyState:
    attributes: dict[Path,int]
    modes: dict[Path,int]
    errors: dict[Path,OSError]
    scanned_parents: frozenset[Path]


class LibraryStorageInspector:
    """Non-hydrating availability checks for image families and destinations.

    Sync-provider state is intentionally treated as a safety signal only. This
    service never resolves a conflict, downloads a placeholder, or treats a
    provider's sync indicator as proof that a write is durable.
    """

    def __init__(
        self,
        *,
        attribute_reader: Callable[[Path], int] | None = None,
        access_checker: Callable[[Path, int], bool] | None = None,
    ) -> None:
        self._uses_default_attribute_reader = attribute_reader is None
        self._attribute_reader = attribute_reader or self._file_attributes
        self._access_checker = access_checker or os.access

    @staticmethod
    def _file_attributes(path: Path) -> int:
        result = path.stat()
        return int(getattr(result, "st_file_attributes", 0) or 0)

    def requires_remote_content(
        self,
        path: str | Path,
        *,
        enumerated_attributes: dict[Path, int] | None = None,
    ) -> bool:
        path = Path(path)
        try:
            if enumerated_attributes is not None and path in enumerated_attributes:
                attributes = int(enumerated_attributes[path] or 0)
            else:
                attributes = int(self._attribute_reader(path) or 0)
        except OSError:
            return False
        return bool(attributes & WINDOWS_REMOTE_CONTENT_ATTRIBUTES)

    def _enumerated_family_state(
        self, images: Iterable[Path]
    ) -> _EnumeratedFamilyState:
        """Capture Windows provider attributes from directory enumeration.

        Recall-on-open is an enumeration-only signal on Windows. ``DirEntry``
        keeps the metadata returned by that enumeration, so inventory can see
        the flag without opening file content or relying on a later path stat.
        """
        if not self._uses_default_attribute_reader:
            return _EnumeratedFamilyState({}, {}, {}, frozenset())
        wanted_by_parent: dict[Path, set[str]] = {}
        for image in images:
            wanted_by_parent.setdefault(image.parent, set()).update(
                path.name.casefold() for path in self.related_paths(image)
            )
        attributes: dict[Path, int] = {}
        modes: dict[Path, int] = {}
        errors: dict[Path,OSError] = {}
        scanned_parents: set[Path] = set()
        for parent, wanted_names in wanted_by_parent.items():
            try:
                entries = os.scandir(parent)
                with entries:
                    for entry in entries:
                        if entry.name.casefold() not in wanted_names:
                            continue
                        path=Path(entry.path)
                        try:
                            result = entry.stat(follow_symlinks=False)
                        except OSError as exc:
                            errors[path]=exc
                            continue
                        attributes[path] = int(
                            getattr(result, "st_file_attributes", 0) or 0
                        )
                        mode=getattr(result,"st_mode",None)
                        if mode is not None:
                            modes[path]=int(mode)
                scanned_parents.add(parent)
            except OSError:
                continue
        return _EnumeratedFamilyState(
            attributes,modes,errors,frozenset(scanned_parents),
        )

    @staticmethod
    def is_conflict_name(name: str) -> bool:
        return any(pattern.search(str(name)) for pattern in _CONFLICT_MARKERS)

    @staticmethod
    def _conflict_prefix(name: str) -> str:
        value=str(name)
        match = _CONFLICT_SPLIT.search(value)
        if match is None:
            return ""
        prefix=value[:match.start()]
        # Providers such as Dropbox and Nextcloud commonly include an actor or
        # device inside the same parenthesized conflict suffix. The family name
        # ends at that opening parenthesis, not immediately before "conflicted".
        opening=max(prefix.rfind("("),prefix.rfind("["))
        if opening>0:
            prefix=prefix[:opening]
        return prefix.rstrip(" ._(-[").casefold()

    @staticmethod
    def _matches_image_family(prefix: str, image: Path) -> bool:
        stem = image.stem.casefold()
        return prefix == stem or any(prefix == stem + suffix for suffix in _FAMILY_SUFFIXES)

    def conflict_artifacts(
        self, images: Iterable[str | Path]
    ) -> dict[Path, tuple[Path, ...]]:
        grouped: dict[Path, list[Path]] = {}
        for value in images:
            image = Path(value)
            grouped.setdefault(image.parent, []).append(image)
        conflicts: dict[Path, list[Path]] = {}
        for parent, family_images in grouped.items():
            try:
                candidates = [
                    Path(entry.path)
                    for entry in os.scandir(parent)
                    if entry.is_file(follow_symlinks=False)
                    and self.is_conflict_name(entry.name)
                ]
            except OSError:
                continue
            for candidate in candidates:
                prefix = self._conflict_prefix(candidate.name)
                for image in family_images:
                    if candidate == image or self._matches_image_family(prefix, image):
                        conflicts.setdefault(image, []).append(candidate)
        return {
            image: tuple(sorted(paths, key=lambda path: str(path).casefold()))
            for image, paths in conflicts.items()
        }

    @staticmethod
    def related_paths(image: str | Path) -> tuple[Path, ...]:
        image = Path(image)
        return (
            image,
            image.with_suffix(".txt"),
            image.with_suffix(".tags.json"),
            image.with_suffix(".xmp"),
        )

    @staticmethod
    def _mode_writable(path: Path) -> bool:
        try:
            return bool(path.stat().st_mode & stat_module.S_IWUSR)
        except OSError:
            return False

    def inspect_image(
        self,
        image: str | Path,
        *,
        conflict_paths: Iterable[str | Path] = (),
        enumerated_attributes: dict[Path, int] | None = None,
        enumerated_modes: dict[Path,int] | None = None,
        enumerated_errors: dict[Path,OSError] | None = None,
        enumerated_parents: frozenset[Path] | None = None,
    ) -> LibraryFileAvailability:
        image = Path(image)
        def exists(path: Path) -> tuple[bool,OSError|None]:
            if enumerated_parents is not None and path.parent in enumerated_parents:
                error=(enumerated_errors or {}).get(path)
                return path in (enumerated_attributes or {}),error
            try:
                path.stat(); return True,None
            except OSError as exc:
                return False,exc

        image_exists,image_error=exists(image)
        if not image_exists:
            return LibraryFileAvailability(
                LibraryFileState.UNAVAILABLE,
                f"The image cannot be reached: {image_error or 'it is no longer present'}",
                "Restore access to the library, then refresh it before trying again.",
                False,
                False,
            )

        family = []
        for path in self.related_paths(image):
            path_exists,path_error=exists(path)
            if not path_exists and (
                path_error is None or isinstance(path_error,FileNotFoundError)
            ):
                continue
            if not path_exists:
                return LibraryFileAvailability(
                    LibraryFileState.UNAVAILABLE,
                    f"{path.name} cannot be reached: {path_error}",
                    "Restore access to the library, then refresh it before trying again.",
                    False,
                    False,
                )
            family.append(path)
            if (
                int((enumerated_attributes or {}).get(path,0) or 0)
                & WINDOWS_REMOTE_CONTENT_ATTRIBUTES
                if enumerated_parents is not None and path.parent in enumerated_parents
                else self.requires_remote_content(
                    path,enumerated_attributes=enumerated_attributes,
                )
            ):
                return LibraryFileAvailability(
                    LibraryFileState.ONLINE_ONLY,
                    f"{path.name} is not fully present on this computer.",
                    "Use the sync provider to make the complete image family available offline, then refresh the library.",
                    False,
                    False,
                )

        held = tuple(Path(path) for path in conflict_paths)
        if held:
            return LibraryFileAvailability(
                LibraryFileState.CONFLICT,
                f"{len(held):,} sync-conflict copy or sidecar is beside this image.",
                "Resolve the conflicting copies in the sync folder, then refresh the library. CollectiGen will not choose a winner.",
                True,
                False,
                held,
            )

        def writable(path: Path) -> bool:
            mode=(enumerated_modes or {}).get(path)
            mode_writable=(
                bool(mode & stat_module.S_IWUSR)
                if mode is not None else self._mode_writable(path)
            )
            return self._access_checker(path,os.W_OK) and mode_writable

        if any(not writable(path) for path in family):
            return LibraryFileAvailability(
                LibraryFileState.READ_ONLY,
                "The image family is available locally but at least one current file is read-only.",
                "Restore write permission, then refresh the library before editing or moving it.",
                True,
                False,
            )
        return LibraryFileAvailability()

    def inspect_images(
        self,
        images: Iterable[str | Path],
        *,
        detect_conflicts: bool = False,
    ) -> dict[Path, LibraryFileAvailability]:
        paths = [Path(value) for value in images]
        conflicts = self.conflict_artifacts(paths) if detect_conflicts else {}
        enumerated = self._enumerated_family_state(paths)
        return {
            image: self.inspect_image(
                image,
                conflict_paths=conflicts.get(image, ()),
                enumerated_attributes=enumerated.attributes,
                enumerated_modes=enumerated.modes,
                enumerated_errors=enumerated.errors,
                enumerated_parents=enumerated.scanned_parents,
            )
            for image in paths
        }

    def assert_locally_readable(self, path: str | Path) -> None:
        path = Path(path)
        if self.requires_remote_content(path):
            raise OSError(
                f"{path.name} is online-only or not fully present locally. "
                "Make it available through the sync provider, then try again."
            )

    def assert_family_ready_for_write(self, image: str | Path) -> None:
        availability = self.inspect_image(image)
        if not availability.ready_for_write:
            raise OSError(f"{availability.label}: {availability.detail} {availability.recovery}".strip())
