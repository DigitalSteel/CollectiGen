from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable, Literal

from .dataset_scope import DatasetScope, IMG_EXTENSIONS
from .library_profiles import (
    LibraryBoundaryDecision,
    LibraryPrivacyPolicy,
    LibraryProfile,
    LibraryProfileRegistry,
    LibraryRole,
)
from .library_storage import LibraryFileState,LibraryStorageInspector
from .verified_transfer import VerifiedTransferEngine, VerifiedTransferPlan
from .media_inventory import inspect_media
from .image_derivatives import DerivativeIngestHandoff


IngestImportAction = Literal["copy", "move"]


def _path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))


def _paths_overlap(first: Path, second: Path) -> bool:
    try:
        common = os.path.commonpath((_path_key(first), _path_key(second)))
    except ValueError:
        return False
    return common in {_path_key(first), _path_key(second)}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class IngestImportItem:
    source: Path
    destination: Path
    copy_plan: VerifiedTransferPlan | None = None
    move_plan: VerifiedTransferPlan | None = None
    source_profile: LibraryProfile | None = None
    duplicate_of: Path | None = None
    errors: list[str] = field(default_factory=list)

    def transfer_for(self, action: IngestImportAction) -> VerifiedTransferPlan | None:
        return self.copy_plan if action == "copy" else self.move_plan

    def ready_for(self, action: IngestImportAction) -> bool:
        transfer = self.transfer_for(action)
        return bool(
            self.duplicate_of is None
            and not self.errors
            and transfer is not None
            and transfer.ready
        )

    @property
    def source_role(self) -> str:
        return self.source_profile.display_role if self.source_profile else "Local Only (unclassified)"


@dataclass
class IngestImportPlan:
    ingest_root: Path
    source_root: Path | None = None
    preserves_subfolders: bool = False
    items: list[IngestImportItem] = field(default_factory=list)
    unsupported: tuple[Path, ...] = ()
    destination_profile: LibraryProfile | None = None
    boundary_decision: LibraryBoundaryDecision | None = None
    errors: tuple[str, ...] = ()
    copy_errors: tuple[str, ...] = ()
    move_errors: tuple[str, ...] = ()

    def action_errors(self, action: IngestImportAction) -> tuple[str, ...]:
        return self.copy_errors if action == "copy" else self.move_errors

    def ready(self, action: IngestImportAction) -> list[IngestImportItem]:
        if self.errors or self.action_errors(action):
            return []
        return [item for item in self.items if item.ready_for(action)]

    def blocked(self, action: IngestImportAction) -> list[IngestImportItem]:
        return [
            item for item in self.items
            if item.duplicate_of is None and not item.ready_for(action)
        ]

    @property
    def duplicates(self) -> list[IngestImportItem]:
        return [item for item in self.items if item.duplicate_of is not None]

    def required_bytes(self, action: IngestImportAction) -> int:
        return sum(
            transfer.required_bytes
            for item in self.items
            if item.ready_for(action)
            for transfer in (item.transfer_for(action),)
            if transfer is not None
        )

    def available_bytes(self, action: IngestImportAction) -> int:
        values = [
            transfer.available_bytes
            for item in self.items
            if item.ready_for(action)
            for transfer in (item.transfer_for(action),)
            if transfer is not None
        ]
        return min(values, default=0)

    @property
    def destination_role(self) -> str:
        profile = self.destination_profile
        return profile.display_role if profile else "Local Only (unclassified)"

    @property
    def subfolder_consequence(self) -> str:
        if self.preserves_subfolders:
            return "The chosen folder's subfolders will be preserved inside Ingest."
        return "Selected image families will be added directly to the Ingest folder."


class IngestImportPlanningService:
    """Build a cancellable, no-write Add Images preview for Ingest."""

    def __init__(
        self,
        ingest_root: str | Path,
        *,
        library_registry: LibraryProfileRegistry | None = None,
        destination_profile: LibraryProfile | None = None,
        transfers=None,
        preserve_source: bool = False,
        verified_versions: tuple[DerivativeIngestHandoff, ...] = (),
    ) -> None:
        self.ingest_root = Path(ingest_root).expanduser().resolve()
        self.library_registry = library_registry or LibraryProfileRegistry()
        self.destination_profile = (
            destination_profile
            or self.library_registry.profile_for_path(self.ingest_root)
            or LibraryProfile.create(
                "Ingest folder",
                self.ingest_root,
                LibraryRole.LOCAL_ONLY,
                stable_id="ingest-unclassified-local",
            )
        )
        if transfers is None:
            raise ValueError("Add Images requires the shared verified transfer service.")
        self.transfers = transfers
        self.preserve_source = preserve_source
        self.verified_versions = tuple(verified_versions)
        if self.verified_versions and not preserve_source:
            raise ValueError("Verified saved versions may only be copied into Ingest.")
        engine=getattr(transfers,"_engine",None)
        self.storage=getattr(engine,"storage",None) or LibraryStorageInspector()

    def preview_files(
        self,
        paths: Iterable[str | Path],
        *,
        progress: Callable[[int, int, str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> IngestImportPlan:
        paths = tuple(paths)
        pinned_families = {}
        if self.verified_versions:
            chosen = {Path(path).absolute() for path in paths}
            expected = {version.output for version in self.verified_versions}
            if chosen != expected or len(expected) != len(self.verified_versions):
                return IngestImportPlan(self.ingest_root, destination_profile=self.destination_profile,
                    errors=("Saved version selection changed; return to Saved Versions and review it again.",))
            try:
                pinned_families = {version.output: version.verify()
                                   for version in self.verified_versions}
            except (ValueError, OSError, RuntimeError) as exc:
                return IngestImportPlan(self.ingest_root, destination_profile=self.destination_profile,
                    errors=(str(exc),))
        supported: list[Path] = []
        unsupported: list[Path] = []
        seen: set[str] = set()
        for raw in paths:
            path = Path(raw).expanduser()
            key = _path_key(path)
            if key in seen:
                continue
            seen.add(key)
            if path.is_file() and path.suffix.lower() in IMG_EXTENSIONS:
                supported.append(path.absolute())
            else:
                unsupported.append(path)
        return self._preview(
            sorted(supported, key=lambda value: str(value).casefold()),
            unsupported=tuple(unsupported),
            source_root=None,
            preserves_subfolders=False,
            progress=progress,
            should_cancel=should_cancel,
            pinned_families=pinned_families,
        )

    def preview_folder(
        self,
        root: str | Path,
        *,
        progress: Callable[[int, int, str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> IngestImportPlan:
        if self.verified_versions:
            return IngestImportPlan(self.ingest_root, destination_profile=self.destination_profile,
                errors=("Choose the exact saved version files, not a folder, for this Ingest return.",))
        source_root = Path(root).expanduser().absolute()
        if VerifiedTransferEngine._redirected(source_root):
            return IngestImportPlan(self.ingest_root, source_root=source_root,
                errors=("Choose an original source folder, not a redirected folder.",))
        if _paths_overlap(source_root, self.ingest_root):
            return IngestImportPlan(
                self.ingest_root,
                source_root=source_root,
                preserves_subfolders=True,
                destination_profile=self.destination_profile,
                errors=(
                    "The chosen folder and Ingest folder overlap. Choose a separate source folder.",
                ),
            )
        scope = DatasetScope(source_root, include_subfolders=True)
        images = list(scope.iter_images())
        recognized = {
            _path_key(member)
            for image in images
            for member in VerifiedTransferEngine.related_files(image)
        }
        unsupported = tuple(
            path
            for path in self._iter_folder_files(scope)
            if _path_key(path) not in recognized
        )
        return self._preview(
            images,
            unsupported=unsupported,
            source_root=source_root,
            preserves_subfolders=True,
            progress=progress,
            should_cancel=should_cancel,
        )

    def preview_exact_selection(
        self,
        paths: Iterable[str | Path],
        *,
        source_root: str | Path | None,
        preserves_subfolders: bool,
        progress: Callable[[int, int, str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> IngestImportPlan:
        """Recheck only retained Add Images paths, preserving their folder layout.

        Unlike preview_folder this never discovers newly added files. Unlike
        preview_files it never flattens a saved folder selection. A caller must
        still compare the result with its sealed batch before authorizing work.
        """
        root = Path(source_root) if source_root is not None else None
        chosen = [Path(path) for path in paths]
        error = ""
        if self.preserve_source or self.verified_versions:
            error = "Protected saved results need their original exact handoff review."
        elif type(preserves_subfolders) is not bool or (root is None) != (not preserves_subfolders):
            error = "The saved Add Images folder policy is incomplete."
        elif root is not None and (not root.is_absolute() or VerifiedTransferEngine._redirected(root)
                                    or _paths_overlap(root, self.ingest_root)):
            error = "The saved Add Images source folder is unavailable or overlaps Ingest."
        elif not chosen or any(not path.is_absolute() or path.suffix.lower() not in IMG_EXTENSIONS
                               for path in chosen):
            error = "The saved Add Images selection contains no exact supported image paths."
        elif len({_path_key(path) for path in chosen}) != len(chosen):
            error = "The saved Add Images selection contains duplicate image paths."
        elif root is not None:
            try:
                resolved_root = root.resolve(strict=False)
                for path in chosen:
                    if path != Path(os.path.normpath(str(path))):
                        raise ValueError("noncanonical path")
                    path.resolve(strict=False).relative_to(resolved_root)
                    path.relative_to(root)
            except ValueError:
                error = "A saved image is outside the chosen source folder."
        if error:
            return IngestImportPlan(self.ingest_root, source_root=root,
                preserves_subfolders=preserves_subfolders,
                destination_profile=self.destination_profile, errors=(error,))
        return self._preview(chosen, unsupported=(), source_root=root,
            preserves_subfolders=preserves_subfolders, progress=progress,
            should_cancel=should_cancel)

    @staticmethod
    def _iter_folder_files(scope: DatasetScope) -> Iterable[Path]:
        ignored = {name.casefold() for name in scope.ignored_directory_names}
        for current, directories, files in os.walk(scope.root):
            current_path = Path(current)
            directories[:] = [
                name for name in directories
                if name.casefold() not in ignored
                and not scope._is_excluded(current_path / name)
            ]
            if scope._is_excluded(current_path):
                continue
            for name in files:
                yield current_path / name

    def _preview(
        self,
        images: list[Path],
        *,
        unsupported: tuple[Path, ...],
        source_root: Path | None,
        preserves_subfolders: bool,
        progress: Callable[[int, int, str], None] | None,
        should_cancel: Callable[[], bool] | None,
        pinned_families: dict[Path, tuple[tuple[str, str], ...]] | None = None,
    ) -> IngestImportPlan:
        pinned_families = pinned_families or {}
        should_cancel = should_cancel or (lambda: False)
        if not self.ingest_root.is_dir():
            return IngestImportPlan(
                self.ingest_root,
                source_root=source_root,
                preserves_subfolders=preserves_subfolders,
                unsupported=unsupported,
                destination_profile=self.destination_profile,
                errors=("The configured Ingest folder is unavailable.",),
            )
        if should_cancel():
            raise InterruptedError("Add Images preview cancelled before any files changed.")

        existing = list(DatasetScope(self.ingest_root, True).iter_images())
        duplicate_map, hash_errors = self._duplicates(
            images,
            existing,
            progress=progress,
            should_cancel=should_cancel,
        )
        boundary = LibraryPrivacyPolicy.movement_for_paths(
            self.library_registry, images, self.destination_profile,
        )
        plan = IngestImportPlan(
            self.ingest_root,
            source_root=source_root,
            preserves_subfolders=preserves_subfolders,
            unsupported=unsupported,
            destination_profile=self.destination_profile,
            boundary_decision=boundary,
        )
        total = len(images)
        for index, image in enumerate(images, start=1):
            if should_cancel():
                raise InterruptedError(
                    "Add Images preview cancelled before any files changed."
                )
            destination = self.ingest_root
            if source_root is not None:
                destination = self.ingest_root / image.relative_to(source_root).parent
            errors = list(hash_errors.get(image, ()))
            copy_plan = move_plan = None
            source_profile=self.library_registry.profile_for_path(image)
            if errors:
                plan.items.append(IngestImportItem(source=image, destination=destination,
                    source_profile=source_profile, errors=errors))
                if progress:
                    progress(index, total, f"Blocked {image.name}")
                continue
            force_copy=LibraryPrivacyPolicy.requires_verified_copy(
                source_profile,self.destination_profile,
            )
            try:
                copy_plan = self.transfers.preview(
                    image,
                    destination,
                    collision_policy="error",
                    force_copy=force_copy,
                    preserve_source=True,
                )
                if image in pinned_families:
                    pins = pinned_families[image]
                    if {str(member.source) for member in copy_plan.members} != {path for path, _ in pins}:
                        raise ValueError("Saved version family changed; return to Saved Versions and review it again.")
                    copy_plan = replace(copy_plan, expected_hashes=pins)
                    self.transfers._engine.prepare_hashes(copy_plan, should_cancel)
            except Exception as exc:
                errors.append(f"Copy preview: {exc}")
            try:
                move_plan = self.transfers.preview(
                    image,
                    destination,
                    collision_policy="error",
                    force_copy=force_copy,
                )
            except Exception as exc:
                errors.append(f"Move preview: {exc}")
            plan.items.append(IngestImportItem(
                source=image,
                destination=destination,
                copy_plan=copy_plan,
                move_plan=move_plan,
                source_profile=source_profile,
                duplicate_of=duplicate_map.get(image),
                errors=errors,
            ))
            if progress:
                progress(index, total, f"Planning {image.name}")

        plan.copy_errors = self._combined_space_errors(plan, "copy")
        plan.move_errors = self._combined_space_errors(plan, "move")
        if self.preserve_source:
            plan.move_errors += ("Choose Copy to Ingest: the saved result must remain available for its history and recovery.",)
        return plan

    @staticmethod
    def _combined_space_errors(
        plan: IngestImportPlan, action: IngestImportAction,
    ) -> tuple[str, ...]:
        required = plan.required_bytes(action)
        available = plan.available_bytes(action)
        if required > available:
            return (
                f"The full {action} needs {required:,} bytes, but only "
                f"{available:,} bytes are currently available at Ingest.",
            )
        return ()

    def _duplicates(
        self,
        images: list[Path],
        existing: list[Path],
        *,
        progress: Callable[[int, int, str], None] | None,
        should_cancel: Callable[[], bool],
    ) -> tuple[dict[Path, Path], dict[Path, tuple[str, ...]]]:
        duplicates: dict[Path, Path] = {}
        errors: dict[Path, tuple[str, ...]] = {}
        sizes: dict[Path, int] = {}
        selected_conflicts=self.storage.conflict_artifacts(images)
        for image in images:
            if should_cancel():
                raise InterruptedError("Add Images preview cancelled before any files changed.")
            availability=self.storage.inspect_image(
                image,conflict_paths=selected_conflicts.get(image,()),
            )
            if (
                not availability.readable
                or availability.state is LibraryFileState.CONFLICT
            ):
                errors[image]=(
                    f"{availability.label}: {availability.detail} {availability.recovery}".strip(),
                )
                continue
            try:
                media = inspect_media(image, verify_pixels=True)
                if not media.image_ready:
                    errors[image] = (f"{media.status}: {media.detail}",)
                    continue
                sizes[image] = image.stat().st_size
            except OSError as exc:
                errors[image] = (f"Could not inspect the selected image: {exc}",)
        candidate_sizes = set(sizes.values())
        existing_by_size: dict[int, list[Path]] = {}
        for path in existing:
            if self.storage.requires_remote_content(path):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size in candidate_sizes:
                existing_by_size.setdefault(size, []).append(path)

        existing_hashes: dict[tuple[int, str], Path] = {}
        candidates_to_hash = sum(len(value) for value in existing_by_size.values())
        total = candidates_to_hash + len(images)
        current = 0
        for size, paths in existing_by_size.items():
            for path in paths:
                if should_cancel():
                    raise InterruptedError(
                        "Add Images preview cancelled before any files changed."
                    )
                current += 1
                try:
                    self.storage.assert_locally_readable(path)
                    existing_hashes.setdefault((size, _sha256(path)), path)
                except OSError:
                    pass
                if progress:
                    progress(current, total, f"Checking Ingest duplicate {path.name}")

        selected_hashes: dict[tuple[int, str], Path] = {}
        for image in images:
            if should_cancel():
                raise InterruptedError(
                    "Add Images preview cancelled before any files changed."
                )
            current += 1
            if image not in sizes:
                continue
            try:
                self.storage.assert_locally_readable(image)
                key = (sizes[image], _sha256(image))
            except OSError as exc:
                errors[image] = (f"Could not verify the selected image: {exc}",)
                continue
            duplicate = existing_hashes.get(key) or selected_hashes.get(key)
            if duplicate is not None:
                duplicates[image] = duplicate
            else:
                selected_hashes[key] = image
            if progress:
                progress(current, total, f"Checking selected image {image.name}")
        return duplicates, errors
