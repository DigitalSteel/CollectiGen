from __future__ import annotations

import hashlib
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .dataset_scope import IMG_EXTENSIONS
from .library_storage import LibraryFileState,LibraryStorageInspector


COPY_CHUNK_BYTES = 1024 * 1024
LIBRARY_NAMING_KEEP = "keep"
LIBRARY_NAMING_IMAGE_HASH = "image_hash"
LIBRARY_NAMING_POLICIES = (LIBRARY_NAMING_KEEP, LIBRARY_NAMING_IMAGE_HASH)


def normalize_library_naming_policy(value: object) -> str:
    policy = str(value or "").strip().casefold()
    return policy if policy in LIBRARY_NAMING_POLICIES else LIBRARY_NAMING_KEEP


class TransferRecoveryRequired(RuntimeError):
    """The transaction could not prove a safe terminal state automatically."""


@dataclass(frozen=True)
class TransferMember:
    source: Path
    target: Path
    size: int
    mtime_ns: int

    def signature(self) -> tuple[int, int]:
        return self.size, self.mtime_ns


@dataclass(frozen=True)
class VerifiedTransferPlan:
    image: Path
    destination: Path
    members: tuple[TransferMember, ...]
    mode: str
    collision_policy: str
    required_bytes: int
    available_bytes: int
    errors: tuple[str, ...] = ()
    preserve_source: bool = False
    naming_policy: str = LIBRARY_NAMING_KEEP
    image_sha256: str = ""
    duplicate_target: Path | None = None
    expected_hashes: tuple[tuple[str, str], ...] = ()

    @property
    def ready(self) -> bool:
        return bool(self.members) and not self.errors and self.duplicate_target is None

    @property
    def total_bytes(self) -> int:
        return sum(member.size for member in self.members)

    def journal_payload(self, hashes: dict[str, str]) -> dict[str, Any]:
        return {
            "engine_version": 2,
            "image": str(self.image),
            "destination": str(self.destination),
            "mode": self.mode,
            "collision_policy": self.collision_policy,
            "preserve_source": self.preserve_source,
            "naming_policy": self.naming_policy,
            "image_sha256": self.image_sha256,
            "original_image_name": self.image.name,
            "original_image_path": str(self.image),
            "destination_image_name": (
                self.members[0].target.name if self.members else ""
            ),
            "files": [
                {
                    "source": str(member.source),
                    "target": str(member.target),
                    "size": member.size,
                    "mtime_ns": member.mtime_ns,
                    "sha256": hashes[str(member.source)],
                }
                for member in self.members
            ],
        }


@dataclass(frozen=True)
class TransferRecoveryOutcome:
    status: str
    message: str


class VerifiedTransferEngine:
    """One no-overwrite engine for complete media-family moves and recovery."""

    def __init__(self,storage_inspector: LibraryStorageInspector | None=None) -> None:
        self.storage=storage_inspector or LibraryStorageInspector()

    @staticmethod
    def related_files(image: str | Path) -> tuple[Path, ...]:
        image = Path(image)
        if image.suffix.casefold() == ".mp4":
            # Video has a separate temporal record. Never borrow image sidecars
            # or transfer only half of a registered video family.
            candidates = (image, image.with_name(image.name + ".video.json"))
            for path in candidates:
                if not path.is_file() or VerifiedTransferEngine._redirected(path):
                    return ()
            return candidates
        candidates = (
            image,
            image.with_suffix(".txt"),
            image.with_suffix(".tags.json"),
            image.with_suffix(".xmp"),
        )
        return tuple(path for path in candidates if path.is_file())

    @staticmethod
    def _redirected(path: Path) -> bool:
        for member in (path, *path.parents):
            try:
                info = member.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                return True
        return False

    @staticmethod
    def _signature(path: Path) -> tuple[int, int]:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns

    @staticmethod
    def _nearest_existing_directory(path: Path) -> Path:
        current = path
        while not current.exists() and current.parent != current:
            current = current.parent
        if not current.exists() or not current.is_dir():
            raise FileNotFoundError(f"No available parent folder exists for {path}.")
        return current

    def _offline_placeholder(self,path: Path) -> bool:
        return self.storage.requires_remote_content(path)

    def _assert_plan_storage_ready(self,plan: VerifiedTransferPlan) -> None:
        if plan.image.suffix.casefold() == ".mp4":
            if self.related_files(plan.image) != tuple(member.source for member in plan.members):
                raise OSError("The complete video family changed or is redirected; preview again.")
            if any(self._redirected(member.target) for member in plan.members):
                raise OSError("The video destination is redirected; choose a local regular folder.")
        conflicts=self.storage.conflict_artifacts((plan.image,)).get(plan.image,())
        availability=self.storage.inspect_image(
            plan.image,conflict_paths=conflicts,
        )
        blocked=(
            not availability.readable
            or availability.state is LibraryFileState.CONFLICT
            or (not plan.preserve_source and not availability.writable)
        )
        if blocked:
            raise OSError(
                f"{availability.label}: {availability.detail} {availability.recovery}".strip()
            )
        if plan.members:
            target_image=plan.members[0].target
            target_conflicts=self.storage.conflict_artifacts((target_image,)).get(
                target_image,(),
            )
            if target_conflicts:
                raise OSError(
                    "Sync conflict held: a conflict-copy file appeared at the destination. "
                    "Resolve it in the sync folder, then preview again."
                )

    @staticmethod
    def _same_volume(source: Path, destination_parent: Path) -> bool:
        try:
            return source.stat().st_dev == destination_parent.stat().st_dev
        except OSError:
            return False

    @staticmethod
    def _family_suffixes(image: Path, sources: Iterable[Path]) -> tuple[str, ...]:
        return tuple(source.name[len(image.stem):] for source in sources)

    @staticmethod
    def _hash_family_paths(destination: Path, stem: str) -> tuple[Path, ...]:
        if not destination.is_dir():
            return ()
        stem_key = stem.casefold()
        sidecar_names = {
            f"{stem}.txt".casefold(),
            f"{stem}.tags.json".casefold(),
            f"{stem}.xmp".casefold(),
        }
        matches = []
        for path in destination.iterdir():
            if not path.is_file():
                continue
            name_key = path.name.casefold()
            if name_key in sidecar_names or (
                path.suffix.casefold() in IMG_EXTENSIONS
                and path.stem.casefold() == stem_key
            ):
                matches.append(path)
        return tuple(matches)

    def _classify_hash_family(
        self,
        image: Path,
        sources: tuple[Path, ...],
        targets: tuple[Path, ...],
        image_sha256: str,
        *,
        should_cancel: Callable[[], bool] | None = None,
    ) -> tuple[Path | None, str]:
        """Return an exact duplicate target or a blocking hash-family conflict."""
        if not targets:
            return None, ""
        destination = targets[0].parent
        existing = self._hash_family_paths(destination, image_sha256)
        if not existing:
            return None, ""
        images = [path for path in existing if path.suffix.casefold() in IMG_EXTENSIONS]
        if len(images) != 1:
            return None, (
                "Conflicting hash-named family: the destination contains "
                f"{len(images):,} image files for {image_sha256}. Resolve it before moving."
            )
        existing_image = images[0]
        if self._offline_placeholder(existing_image):
            return None, (
                f"Conflicting hash-named family: {existing_image.name} is offline or placeholder-only."
            )
        if self._sha256(existing_image, should_cancel) != image_sha256:
            return None, (
                "Image-hash name collision: "
                f"{existing_image.name} contains different bytes. Nothing can overwrite it."
            )

        expected = {target.name.casefold(): (source, target) for source, target in zip(sources, targets)}
        actual = {path.name.casefold(): path for path in existing}
        if len(actual) != len(existing) or set(actual) != set(expected):
            return None, (
                "Conflicting sidecar family: the identical destination image does not have "
                "the same complete recognized family. Review the duplicate before moving."
            )
        for key, (source, _target) in expected.items():
            candidate = actual[key]
            if self._offline_placeholder(candidate):
                return None, (
                    f"Conflicting sidecar family: {candidate.name} is offline or placeholder-only."
                )
            if self._sha256(source, should_cancel) != self._sha256(candidate, should_cancel):
                return None, (
                    "Conflicting sidecar family: the identical destination image has "
                    f"different {candidate.name} content. Review it before moving."
                )
        return existing_image, ""

    def preview(
        self,
        image: str | Path,
        destination: str | Path,
        *,
        relative_to: str | Path | None = None,
        collision_policy: str = "increment",
        force_copy: bool = False,
        preserve_source: bool = False,
        naming_policy: str = LIBRARY_NAMING_KEEP,
        should_cancel: Callable[[], bool] | None = None,
    ) -> VerifiedTransferPlan:
        image = Path(image)
        destination = Path(destination)
        naming_policy = normalize_library_naming_policy(naming_policy)
        if relative_to is not None:
            try:
                destination = destination / image.parent.relative_to(Path(relative_to))
            except ValueError:
                pass
        sources = self.related_files(image)
        errors: list[str] = []
        if image.suffix.casefold() == ".mp4":
            if naming_policy != LIBRARY_NAMING_KEEP:
                errors.append("Video uses its own asset identity; image-hash naming is unsupported.")
            if self._redirected(destination):
                errors.append("The video destination is redirected; choose a local regular folder.")
        if collision_policy not in {"error", "increment"}:
            errors.append(f"Unsupported collision policy: {collision_policy}.")
        if naming_policy == LIBRARY_NAMING_IMAGE_HASH and collision_policy != "error":
            errors.append("Image-hash naming requires no-overwrite collision handling.")
        if not sources:
            errors.append("The selected image family is no longer available.")
        else:
            conflicts=self.storage.conflict_artifacts((image,)).get(image,())
            availability=self.storage.inspect_image(image,conflict_paths=conflicts)
            if not availability.readable:
                errors.append(
                    f"{availability.label}: {availability.detail} {availability.recovery}".strip()
                )
            elif availability.state is LibraryFileState.CONFLICT:
                errors.append(
                    f"{availability.label}: {availability.detail} {availability.recovery}".strip()
                )
            elif not preserve_source and not availability.writable:
                errors.append(
                    f"{availability.label}: {availability.detail} {availability.recovery}".strip()
                )

        suffixes = self._family_suffixes(image, sources)
        image_sha256 = ""
        candidate = image.stem
        if naming_policy == LIBRARY_NAMING_IMAGE_HASH and image.is_file():
            try:
                if self._offline_placeholder(image):
                    errors.append(f"{image.name} is offline or placeholder-only.")
                else:
                    image_sha256 = self._sha256(image, should_cancel)
                    candidate = image_sha256
            except InterruptedError:
                raise
            except OSError as exc:
                errors.append(f"Could not calculate the image hash for {image.name}: {exc}")
        if collision_policy == "increment":
            number = 2
            while any((destination / f"{candidate}{suffix}").exists() for suffix in suffixes):
                candidate = f"{image.stem}_{number}"
                number += 1
        targets = tuple(destination / f"{candidate}{suffix}" for suffix in suffixes)
        if targets:
            target_conflicts=self.storage.conflict_artifacts((targets[0],)).get(
                targets[0],(),
            )
            if target_conflicts:
                errors.append(
                    "Sync conflict held: a conflict-copy file already exists for the destination family. "
                    "Resolve it in the sync folder, then preview again."
                )
        duplicate_target = None
        if naming_policy == LIBRARY_NAMING_IMAGE_HASH and image_sha256:
            try:
                duplicate_target, conflict = self._classify_hash_family(
                    image, sources, targets, image_sha256,
                    should_cancel=should_cancel,
                )
                if conflict:
                    errors.append(conflict)
            except InterruptedError:
                raise
            except OSError as exc:
                errors.append(f"Could not inspect the hash-named destination family: {exc}")
        elif collision_policy == "error":
            occupied = [target for target in targets if target.exists()]
            if occupied:
                errors.append(
                    f"Destination collision: {occupied[0].name} already exists. Choose another destination or resolve the duplicate before moving."
                )

        members: list[TransferMember] = []
        for source, target in zip(sources, targets):
            try:
                size, mtime_ns = self._signature(source)
                if self._offline_placeholder(source):
                    errors.append(f"{source.name} is offline or placeholder-only.")
                if source.resolve() == target.resolve():
                    errors.append(f"{source.name} is already at the proposed destination.")
                members.append(TransferMember(source, target, size, mtime_ns))
            except OSError as exc:
                errors.append(f"Could not inspect {source.name}: {exc}")

        mode = "copy" if preserve_source else "copy_remove"
        required_bytes = sum(member.size for member in members)
        available_bytes = 0
        try:
            ancestor = self._nearest_existing_directory(destination)
            if self.storage.requires_remote_content(ancestor):
                errors.append(
                    "The destination folder is online-only or not fully present locally. "
                    "Make it available through the sync provider, then preview again."
                )
            if not os.access(ancestor, os.W_OK):
                errors.append(f"The destination is not writable: {ancestor}")
            same_volume = bool(members) and self._same_volume(members[0].source, ancestor)
            if same_volume and not force_copy and not preserve_source:
                mode = "rename"
                required_bytes = 0
            available_bytes = int(shutil.disk_usage(ancestor).free)
            if required_bytes > available_bytes:
                errors.append(
                    f"The destination has insufficient free space ({required_bytes:,} bytes required; {available_bytes:,} available)."
                )
        except OSError as exc:
            errors.append(f"The destination is unavailable: {exc}")

        return VerifiedTransferPlan(
            image=image,
            destination=destination,
            members=tuple(members),
            mode=mode,
            collision_policy=collision_policy,
            required_bytes=required_bytes,
            available_bytes=available_bytes,
            errors=tuple(dict.fromkeys(errors)),
            preserve_source=preserve_source,
            naming_policy=naming_policy,
            image_sha256=image_sha256,
            duplicate_target=duplicate_target,
        )

    def plan_exact(
        self,
        pairs: Iterable[tuple[str | Path, str | Path]],
        *,
        image: str | Path,
        force_copy: bool = False,
    ) -> VerifiedTransferPlan:
        pairs = tuple((Path(source), Path(target)) for source, target in pairs)
        destination = pairs[0][1].parent if pairs else Path(image).parent
        errors: list[str] = []
        members: list[TransferMember] = []
        source_image=Path(image)
        conflicts=self.storage.conflict_artifacts((source_image,)).get(source_image,())
        availability=self.storage.inspect_image(
            source_image,conflict_paths=conflicts,
        )
        if not availability.ready_for_write:
            errors.append(
                f"{availability.label}: {availability.detail} {availability.recovery}".strip()
            )
        for source, target in pairs:
            try:
                size, mtime_ns = self._signature(source)
                if target.exists():
                    errors.append(f"The original location is already occupied: {target}")
                members.append(TransferMember(source, target, size, mtime_ns))
            except OSError as exc:
                errors.append(f"Could not inspect {source}: {exc}")
        mode = "copy_remove"
        required_bytes = sum(member.size for member in members)
        available_bytes = 0
        try:
            ancestor = self._nearest_existing_directory(destination)
            if self.storage.requires_remote_content(ancestor):
                errors.append(
                    "The destination folder is online-only or not fully present locally. "
                    "Make it available through the sync provider, then try again."
                )
            if not os.access(ancestor, os.W_OK):
                errors.append(f"The destination is not writable: {ancestor}")
            if members and self._same_volume(members[0].source, ancestor) and not force_copy:
                mode = "rename"
                required_bytes = 0
            available_bytes = int(shutil.disk_usage(ancestor).free)
            if required_bytes > available_bytes:
                errors.append(
                    f"The destination has insufficient free space ({required_bytes:,} bytes required; {available_bytes:,} available)."
                )
        except OSError as exc:
            errors.append(f"The destination is unavailable: {exc}")
        return VerifiedTransferPlan(
            Path(image), destination, tuple(members), mode, "error",
            required_bytes, available_bytes, tuple(dict.fromkeys(errors)),
        )

    def _sha256(
        self,
        path: Path,
        should_cancel: Callable[[], bool] | None = None,
    ) -> str:
        self.storage.assert_locally_readable(path)
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(COPY_CHUNK_BYTES):
                if should_cancel is not None and should_cancel():
                    raise InterruptedError("Transfer cancelled safely while verifying file content.")
                digest.update(chunk)
        return digest.hexdigest()

    def prepare_hashes(
        self,
        plan: VerifiedTransferPlan,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, str]:
        if not plan.ready:
            raise ValueError("Transfer preview is not ready: " + " ".join(plan.errors))
        self._assert_plan_storage_ready(plan)
        hashes: dict[str, str] = {}
        expected = dict(plan.expected_hashes)
        if len(expected) != len(plan.expected_hashes) or not set(expected).issubset(
            {str(member.source) for member in plan.members}
        ):
            raise ValueError("The pinned transfer hashes do not match the exact family.")
        for member in plan.members:
            self.storage.assert_locally_readable(member.source)
            if self._signature(member.source) != member.signature():
                raise RuntimeError(
                    f"{member.source.name} changed after the transfer preview; preview again."
                )
            digest = self._sha256(member.source, should_cancel)
            if str(member.source) in expected and digest != expected[str(member.source)]:
                raise RuntimeError(f"{member.source.name} no longer matches its verified output identity; preview again.")
            if self._signature(member.source) != member.signature():
                raise RuntimeError(
                    f"{member.source.name} changed while it was being verified; preview again."
                )
            if member.target.exists():
                raise FileExistsError(
                    f"The destination changed after preview: {member.target} now exists."
                )
            hashes[str(member.source)] = digest
        if plan.naming_policy == LIBRARY_NAMING_IMAGE_HASH:
            current = hashes.get(str(plan.image), "")
            if not plan.image_sha256 or current != plan.image_sha256:
                raise RuntimeError(
                    "The image bytes no longer match the previewed hash name; preview again."
                )
        return hashes

    @staticmethod
    def _temporary_target(target: Path, operation_id: str) -> Path:
        return target.with_name(f".{target.name}.collectigen-{operation_id}.tmp")

    @staticmethod
    def _rename_no_replace(source: Path, target: Path) -> None:
        """Move on one volume while failing if a concurrent target appears."""
        if target.exists():
            raise FileExistsError(f"The destination is now occupied: {target}")
        if os.name == "nt":
            # Windows rename is no-replace and therefore closes the final
            # exists-check race without weakening collision safety.
            os.rename(source,target)
            return
        # POSIX rename may replace an existing path. Link creation is atomic
        # and no-replace; unlinking the original completes the same-volume move.
        os.link(source,target)
        try:
            source.unlink()
        except Exception:
            target.unlink(missing_ok=True)
            raise

    def _copy_verified(self, source: Path, target: Path, expected_hash: str):
        digest = hashlib.sha256()
        with source.open("rb") as reader, target.open("xb") as writer:
            while chunk := reader.read(COPY_CHUNK_BYTES):
                writer.write(chunk)
                digest.update(chunk)
            writer.flush()
            os.fsync(writer.fileno())
            created=os.fstat(writer.fileno())
            identity=(created.st_dev,created.st_ino,created.st_size,created.st_mtime_ns,
                getattr(created,"st_birthtime_ns",created.st_ctime_ns))
        if digest.hexdigest() != expected_hash or self._sha256(target) != expected_hash:
            raise OSError(f"Read-back verification failed for {target.name}.")
        return identity

    @staticmethod
    def _remove_empty_parents(paths: Iterable[Path], stop: Path) -> None:
        candidates = sorted({path.parent for path in paths}, key=lambda value: len(value.parts), reverse=True)
        for parent in candidates:
            current = parent
            while current != stop and current.parent != current:
                try:
                    current.rmdir()
                except OSError:
                    break
                current = current.parent

    def execute(
        self,
        plan: VerifiedTransferPlan,
        hashes: dict[str, str],
        operation_id: str,
        *,
        should_cancel: Callable[[], bool] | None = None,
        identity_observer=None,
    ) -> None:
        should_cancel = should_cancel or (lambda: False)
        if should_cancel():
            raise InterruptedError("Transfer cancelled before any files changed.")
        self._assert_plan_storage_ready(plan)
        for member in plan.members:
            self.storage.assert_locally_readable(member.source)
            if self._signature(member.source) != member.signature():
                raise RuntimeError(
                    f"{member.source.name} changed after verification; preview again."
                )
            if member.target.exists():
                raise FileExistsError(f"The destination is now occupied: {member.target}")
            ancestor=self._nearest_existing_directory(member.target.parent)
            if self.storage.requires_remote_content(ancestor):
                raise OSError(
                    "The destination folder is online-only or not fully present locally. "
                    "Make it available through the sync provider, then preview again."
                )
        created_targets = [member.target for member in plan.members]
        for target in created_targets:
            target.parent.mkdir(parents=True, exist_ok=True)
        if plan.mode == "rename":
            self._execute_rename(plan, hashes, should_cancel,**({"identity_observer":identity_observer} if identity_observer is not None else {}))
        elif plan.mode == "copy":
            self._execute_copy_remove(
                plan, hashes, operation_id, should_cancel,
                preserve_source=True,
                **({"identity_observer":identity_observer} if identity_observer is not None else {}),
            )
        else:
            self._execute_copy_remove(plan, hashes, operation_id, should_cancel,**({"identity_observer":identity_observer} if identity_observer is not None else {}))

    def _execute_rename(
        self,
        plan: VerifiedTransferPlan,
        hashes: dict[str, str],
        should_cancel: Callable[[], bool],
        *,identity_observer=None,
    ) -> None:
        moved: list[TransferMember] = []
        try:
            for member in plan.members:
                if should_cancel():
                    raise InterruptedError("Transfer cancelled safely before the next family member.")
                if identity_observer is not None:identity_observer("publish",member.target,member.source)
                self._rename_no_replace(member.source, member.target)
                moved.append(member)
            for member in plan.members:
                if self._sha256(member.target) != hashes[str(member.source)]:
                    raise OSError(f"Read-back verification failed for {member.target.name}.")
        except Exception as exc:
            rollback_errors: list[str] = []
            for member in reversed(moved):
                try:
                    if not member.target.exists():
                        raise FileNotFoundError("the moved file disappeared before rollback")
                    if member.source.exists():
                        raise FileExistsError("the source reappeared before rollback")
                    if self._sha256(member.target) != hashes[str(member.source)]:
                        raise OSError("the moved file changed before rollback")
                    member.source.parent.mkdir(parents=True, exist_ok=True)
                    if identity_observer is not None:identity_observer("rollback",member.source,member.target)
                    self._rename_no_replace(member.target, member.source)
                except Exception as rollback_exc:
                    rollback_errors.append(f"{member.source.name}: {rollback_exc}")
            if rollback_errors:
                raise TransferRecoveryRequired(
                    f"The move stopped and automatic rollback needs attention: {'; '.join(rollback_errors)}"
                ) from exc
            self._remove_empty_parents((member.target for member in plan.members), plan.destination)
            raise

    def _execute_copy_remove(
        self,
        plan: VerifiedTransferPlan,
        hashes: dict[str, str],
        operation_id: str,
        should_cancel: Callable[[], bool],
        *,
        preserve_source: bool = False,
        identity_observer=None,
    ) -> None:
        temps = {
            member: self._temporary_target(member.target, operation_id)
            for member in plan.members
        }
        if identity_observer is not None and any(path.exists() for path in temps.values()):
            raise FileExistsError("A native move staging path is already occupied; preserve it for recovery.")
        promoted: list[TransferMember] = []
        try:
            for member in plan.members:
                if should_cancel():
                    raise InterruptedError("Transfer cancelled safely during verified copy.")
                temporary = temps[member]
                if temporary.exists():
                    if identity_observer is not None:
                        raise FileExistsError("Native move staging became occupied; preserve it for recovery.")
                    temporary.unlink()
                identity=self._copy_verified(member.source, temporary, hashes[str(member.source)])
                if identity_observer is not None:identity_observer("staged",member.target,temporary,identity)
            for member in plan.members:
                if self._signature(member.source) != member.signature():
                    raise RuntimeError(
                        f"{member.source.name} changed while the destination copy was prepared."
                    )
                if self._sha256(member.source) != hashes[str(member.source)]:
                    raise RuntimeError(
                        f"{member.source.name} changed while the destination copy was prepared."
                    )
                if member.target.exists():
                    raise FileExistsError(f"The destination is now occupied: {member.target}")
            for member in plan.members:
                if identity_observer is not None:identity_observer("publish",member.target,temps[member])
                self._rename_no_replace(temps[member], member.target)
                promoted.append(member)
            for member in plan.members:
                if self._sha256(member.target) != hashes[str(member.source)]:
                    raise OSError(f"Read-back verification failed for {member.target.name}.")
            if should_cancel():
                raise InterruptedError(
                    "Transfer cancelled safely before the verified copy was committed."
                    if preserve_source else
                    "Transfer cancelled safely before source removal."
                )
            if not preserve_source:
                for member in plan.members:
                    if identity_observer is not None:identity_observer("remove-source",member.source,member.source)
                    member.source.unlink()
        except Exception as exc:
            rollback_errors: list[str] = []
            for member,temporary in temps.items():
                try:
                    if identity_observer is not None and temporary.exists():
                        identity_observer("discard-staged",member.target,temporary)
                    temporary.unlink(missing_ok=True)
                except Exception as cleanup_exc:
                    rollback_errors.append(f"{temporary.name}: {cleanup_exc}")
            for member in plan.members:
                expected = hashes[str(member.source)]
                try:
                    if not member.source.exists() and member.target.exists():
                        member.source.parent.mkdir(parents=True, exist_ok=True)
                        restore = member.source.with_name(
                            f".{member.source.name}.collectigen-restore-{operation_id}.tmp"
                        )
                        if identity_observer is not None and restore.exists():
                            raise FileExistsError("Native move rollback staging is occupied; preserve it for recovery.")
                        if identity_observer is None:restore.unlink(missing_ok=True)
                        identity=self._copy_verified(member.target, restore, expected)
                        if identity_observer is not None:identity_observer("rollback",member.source,restore,identity)
                        self._rename_no_replace(restore, member.source)
                    if member.source.exists() and self._sha256(member.source) != expected:
                        raise OSError("the source no longer matches the verified family")
                except Exception as rollback_exc:
                    rollback_errors.append(f"{member.source.name}: {rollback_exc}")
            if not rollback_errors:
                for member in promoted:
                    try:
                        expected = hashes[str(member.source)]
                        if not member.target.exists():
                            raise FileNotFoundError("the published destination disappeared during rollback")
                        if self._sha256(member.target) != expected:
                            raise OSError("the published destination changed during rollback")
                        if identity_observer is not None:identity_observer("discard",member.target,member.target)
                        member.target.unlink()
                    except Exception as rollback_exc:
                        rollback_errors.append(f"{member.target.name}: {rollback_exc}")
            if rollback_errors:
                raise TransferRecoveryRequired(
                    f"The verified copy stopped and automatic recovery needs attention: {'; '.join(rollback_errors)}"
                ) from exc
            self._remove_empty_parents((member.target for member in plan.members), plan.destination)
            raise

    def recover(self, payload: dict[str, Any], operation_id: str,*,identity_observer=None) -> TransferRecoveryOutcome:
        files = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(files, list) or not files:
            return TransferRecoveryOutcome("rolled_back", "No file changes were recorded.")
        mode = str(payload.get("mode") or "")
        entries = []
        for value in files:
            if not isinstance(value, dict):
                return TransferRecoveryOutcome("attention", "The recovery plan is incomplete.")
            entries.append((
                Path(str(value.get("source") or "")),
                Path(str(value.get("target") or "")),
                str(value.get("sha256") or ""),
            ))
        for _, target, _ in entries:
            temporary = self._temporary_target(target, operation_id)
            try:
                if identity_observer is not None and temporary.exists():
                    identity_observer("discard-staged",target,temporary)
                temporary.unlink(missing_ok=True)
            except OSError:
                if identity_observer is not None:raise
                pass
        if identity_observer is not None:
            for source,_,_ in entries:
                restore=source.with_name(f".{source.name}.collectigen-restore-{operation_id}.tmp")
                if restore.exists():
                    identity_observer("discard-restore",source,restore)
                    restore.unlink()

        states: list[tuple[bool, bool]] = []
        for source, target, expected in entries:
            source_ok = source.is_file() and self._sha256(source) == expected
            target_ok = target.is_file() and self._sha256(target) == expected
            if source.exists() and not source_ok:
                return TransferRecoveryOutcome(
                    "attention", f"Recovery stopped because {source} changed externally."
                )
            if target.exists() and not target_ok:
                return TransferRecoveryOutcome(
                    "attention", f"Recovery stopped because {target} changed externally."
                )
            if not source.exists() and not target.exists():
                return TransferRecoveryOutcome(
                    "attention", f"Recovery stopped because both copies of {source.name} are missing."
                )
            states.append((source_ok, target_ok))

        if mode == "copy":
            if not all(source for source, _ in states):
                return TransferRecoveryOutcome(
                    "attention",
                    "Recovery stopped because a source from a source-preserving copy is missing.",
                )
            if all(target for _, target in states):
                return TransferRecoveryOutcome(
                    "completed",
                    "The verified source and imported copy families are complete.",
                )
            try:
                for (_, target, _), (_, target_ok) in zip(entries, states):
                    if target_ok:
                        if identity_observer is not None:identity_observer("discard",target,target)
                        target.unlink()
            except OSError as exc:
                return TransferRecoveryOutcome(
                    "attention", f"Source-preserving copy rollback failed: {exc}",
                )
            return TransferRecoveryOutcome(
                "rolled_back",
                "The source family was preserved and partial imported copies were removed.",
            )

        if all(target and not source for source, target in states):
            return TransferRecoveryOutcome("completed", "The verified destination family was already complete.")
        if all(source and not target for source, target in states):
            return TransferRecoveryOutcome("rolled_back", "The source family was preserved.")

        if mode == "rename":
            if any(source and target for source, target in states):
                return TransferRecoveryOutcome(
                    "attention", "Recovery found both source and destination copies after a same-volume move."
                )
            try:
                for (source, target, _), (source_ok, target_ok) in reversed(list(zip(entries, states))):
                    if target_ok and not source_ok:
                        source.parent.mkdir(parents=True, exist_ok=True)
                        if identity_observer is not None:identity_observer("rollback",source,target)
                        self._rename_no_replace(target, source)
            except OSError as exc:
                return TransferRecoveryOutcome("attention", f"Same-volume rollback failed: {exc}")
            return TransferRecoveryOutcome("rolled_back", "The interrupted same-volume family move was rolled back.")

        if all(source for source, _ in states):
            try:
                for (_, target, _), (_, target_ok) in zip(entries, states):
                    if target_ok:
                        if identity_observer is not None:identity_observer("discard",target,target)
                        target.unlink()
            except OSError as exc:
                return TransferRecoveryOutcome("attention", f"Verified-copy rollback failed: {exc}")
            return TransferRecoveryOutcome("rolled_back", "The source family was preserved and duplicate copies were removed.")

        if not all(target for _, target in states):
            return TransferRecoveryOutcome(
                "attention", "Recovery cannot finish because source removal began before every verified destination member was present."
            )
        try:
            for (source, _, _), (source_ok, _) in zip(entries, states):
                if source_ok:
                    if identity_observer is not None:identity_observer("remove-source",source,source)
                    source.unlink()
        except OSError as exc:
            return TransferRecoveryOutcome("attention", f"Verified source cleanup could not finish: {exc}")
        return TransferRecoveryOutcome("completed", "The interrupted verified copy-and-remove transaction was completed.")

    @staticmethod
    def _copy_undo_temporary(target: Path, operation_id: str) -> Path:
        return target.with_name(
            f".{target.name}.collectigen-copy-undo-{operation_id}.tmp"
        )

    @staticmethod
    def _copy_undo_entries(payload: dict[str, Any]) -> list[tuple[Path, Path, str]]:
        files = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(files, list) or not files:
            raise ValueError("Copy Undo recovery evidence is incomplete.")
        entries = []
        for value in files:
            if not isinstance(value, dict):
                raise ValueError("Copy Undo recovery evidence is incomplete.")
            source = Path(str(value.get("source") or ""))
            target = Path(str(value.get("target") or ""))
            expected = str(value.get("sha256") or "")
            if not expected:
                raise ValueError("Copy Undo is missing a verified file hash.")
            entries.append((source, target, expected))
        return entries

    def execute_copy_undo(
        self,
        payload: dict[str, Any],
        operation_id: str,
        *,
        should_cancel: Callable[[], bool] | None = None,
        identity_observer=None,
    ) -> None:
        """Remove an unchanged imported copy while its original remains verified."""
        should_cancel = should_cancel or (lambda: False)
        entries = self._copy_undo_entries(payload)
        if should_cancel():
            raise InterruptedError("Copy Undo cancelled before any files changed.")
        for source, target, expected in entries:
            if not source.is_file() or self._sha256(source) != expected:
                raise RuntimeError(
                    f"The original source is missing or changed: {source}"
                )
            if not target.is_file() or self._sha256(target) != expected:
                raise RuntimeError(
                    f"The imported copy is missing or changed: {target}"
                )
        staged: list[tuple[Path, Path, str]] = []
        try:
            for _, target, expected in entries:
                if should_cancel():
                    raise InterruptedError(
                        "Copy Undo cancelled safely before the next family member."
                    )
                temporary = self._copy_undo_temporary(target, operation_id)
                if identity_observer is not None:
                    if temporary.exists():raise FileExistsError("Native Copy Undo staging became occupied.")
                    identity_observer("undo-stage",target,target)
                else:temporary.unlink(missing_ok=True)
                self._rename_no_replace(target, temporary)
                staged.append((target, temporary, expected))
            for _, temporary, expected in staged:
                if self._sha256(temporary) != expected:
                    raise OSError(
                        f"Copy Undo verification failed for {temporary.name}."
                    )
            for source, _, expected in entries:
                if identity_observer is not None:identity_observer("undo-source",source,source)
                if not source.is_file() or self._sha256(source) != expected:
                    raise RuntimeError(
                        f"The original source changed during Copy Undo: {source}"
                    )
            if should_cancel():
                raise InterruptedError("Copy Undo cancelled safely before deletion.")
        except Exception as exc:
            rollback_errors = []
            for target, temporary, expected in reversed(staged):
                try:
                    if temporary.exists() and not target.exists():
                        if self._sha256(temporary) != expected:
                            raise OSError("the staged imported copy changed")
                        if identity_observer is not None:identity_observer("undo-restore",target,temporary)
                        self._rename_no_replace(temporary, target)
                except Exception as rollback_exc:
                    rollback_errors.append(f"{target.name}: {rollback_exc}")
            if rollback_errors:
                raise TransferRecoveryRequired(
                    "Copy Undo stopped and rollback needs attention: "
                    +"; ".join(rollback_errors)
                ) from exc
            raise
        try:
            for target, temporary, _ in staged:
                if identity_observer is not None:identity_observer("undo-discard",target,temporary)
                temporary.unlink()
        except Exception as exc:
            raise TransferRecoveryRequired(
                "Copy Undo committed but temporary cleanup needs restart recovery: "
                f"{exc}"
            ) from exc

    def recover_copy_undo(
        self, payload: dict[str, Any], operation_id: str,
        *,identity_observer=None,
    ) -> TransferRecoveryOutcome:
        try:
            entries = self._copy_undo_entries(payload)
        except ValueError as exc:
            return TransferRecoveryOutcome("attention", str(exc))
        states = []
        for source, target, expected in entries:
            temporary = self._copy_undo_temporary(target, operation_id)
            if identity_observer is not None:identity_observer("undo-source",source,source)
            source_ok = source.is_file() and self._sha256(source) == expected
            target_ok = target.is_file() and self._sha256(target) == expected
            temporary_ok = (
                temporary.is_file() and self._sha256(temporary) == expected
            )
            if not source_ok:
                return TransferRecoveryOutcome(
                    "attention",
                    f"Copy Undo recovery stopped because {source} changed or is missing.",
                )
            if target.exists() and not target_ok:
                return TransferRecoveryOutcome(
                    "attention",
                    f"Copy Undo recovery stopped because {target} changed.",
                )
            if temporary.exists() and not temporary_ok:
                return TransferRecoveryOutcome(
                    "attention",
                    f"Copy Undo recovery stopped because {temporary} changed.",
                )
            if target_ok and temporary_ok:
                return TransferRecoveryOutcome(
                    "attention",
                    f"Copy Undo recovery found duplicate staged authority for {target.name}.",
                )
            states.append((source, target, temporary, target_ok, temporary_ok))

        deletion_began = any(
            not target_ok and not temporary_ok
            for _, _, _, target_ok, temporary_ok in states
        )
        if deletion_began:
            try:
                for _, target, temporary, target_ok, temporary_ok in states:
                    if target_ok:
                        if identity_observer is not None:identity_observer("undo-discard",target,target)
                        target.unlink()
                    if temporary_ok:
                        if identity_observer is not None:identity_observer("undo-discard",target,temporary)
                        temporary.unlink()
            except OSError as exc:
                return TransferRecoveryOutcome(
                    "attention", f"Copy Undo cleanup could not finish: {exc}",
                )
            return TransferRecoveryOutcome(
                "completed", "The interrupted Copy Undo was completed safely.",
            )

        staged = [value for value in states if value[4]]
        if staged:
            try:
                for _, target, temporary, _, _ in reversed(staged):
                    if identity_observer is not None:identity_observer("undo-restore",target,temporary)
                    self._rename_no_replace(temporary, target)
            except OSError as exc:
                return TransferRecoveryOutcome(
                    "attention", f"Copy Undo rollback could not finish: {exc}",
                )
            return TransferRecoveryOutcome(
                "rolled_back", "The interrupted Copy Undo was rolled back.",
            )
        return TransferRecoveryOutcome(
            "rolled_back", "The imported copy remains unchanged.",
        )
