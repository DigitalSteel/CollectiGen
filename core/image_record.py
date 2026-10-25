from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .annotation import ImageAnnotation
from .automation_templates import has_automation_placeholder
from .caption_import import parse_generated_caption, text_sha256
from .character_database import CharacterDatabase
from .export_profile import DEFAULT_EXPORT_PROFILE, ExportProfile
from .image_fact_decisions import accept_confirmed_tag_decisions
from .library_storage import LibraryStorageInspector
from .confirmed_tag_identity import ConfirmedTagIdentityService
from .tag_collection import TagCollection, normalize_tag


def _atomic_write_text(path: Path, value: str) -> None:
    """Replace a text projection only after its complete contents reach disk."""
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_create_text(path: Path, value: str) -> None:
    """Publish a complete new file without ever replacing an existing target."""
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "nt":
            # Windows rename is atomic and refuses an existing destination.
            os.rename(temporary, path)
        else:
            # A same-directory hard link atomically publishes the fully written
            # inode and fails with FileExistsError if another process won.
            os.link(temporary, path)
            temporary.unlink()
    finally:
        if temporary.exists():
            temporary.unlink()


def split_tags_caption(text: str) -> tuple[list[str], list[str]]:
    """Backward-compatible tuple view of the source-aware TXT parser."""
    result = parse_generated_caption(text)
    return result.sdxl_tags, result.plain_language


def annotation_knowledge_fingerprint(annotation: ImageAnnotation) -> str:
    # Group mappings created by in-memory edits may have a different insertion
    # order from the schema-ordered mapping restored after save. Fingerprints
    # describe semantic authority, so calculate from the canonical round trip.
    canonical = ImageAnnotation.from_dict(annotation.to_dict(), annotation.image)
    value = {
        "schema_version": canonical.schema_version,
        "present": canonical.all_structured_tags(),
        "contexts": [context.to_dict() for context in canonical.contexts],
        "tag_identities": canonical.tag_identities,
        "absent": canonical.confirmed_absent,
        "export_excluded": canonical.export_excluded,
        "plain_language": canonical.plain_language,
    }
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _path_fingerprint(path: Path) -> dict[str, object]:
    try:
        raw = path.read_bytes()
        stat = path.stat()
    except FileNotFoundError:
        return {"exists": False}
    return {
        "exists": True, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _read_bytes_snapshot(path: Path) -> tuple[bytes, dict[str, object]]:
    """Read one coherent file version and return its exact write identity."""
    before = path.stat()
    raw = path.read_bytes()
    after = path.stat()
    before_identity = (before.st_size, before.st_mtime_ns)
    after_identity = (after.st_size, after.st_mtime_ns)
    if before_identity != after_identity or after.st_size != len(raw):
        raise RuntimeError(f"{path.name} changed while it was being read.")
    return raw, {
        "exists": True,
        "size": after.st_size,
        "mtime_ns": after.st_mtime_ns,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


class ImageRecordStaleWriteError(RuntimeError):
    """Image Knowledge changed after this record loaded and was not replaced."""


@dataclass(frozen=True)
class ImageRecordEditAssessment:
    """Describe whether one editor session can safely persist its record."""

    changed: bool
    stale_sidecar: bool = False
    stale_caption: bool = False

    @property
    def stale(self) -> bool:
        return self.stale_sidecar or self.stale_caption


@dataclass(frozen=True)
class ImageRecordEditSnapshot:
    """Opening state used for no-op detection and stale-file protection."""

    annotation: dict[str, object]
    sidecar_fingerprint: dict[str, object]
    caption_fingerprint: dict[str, object]

    @classmethod
    def capture(cls, record: "ImageRecord") -> "ImageRecordEditSnapshot":
        record._ensure_loaded()
        return cls(
            annotation=deepcopy(record.annotation.to_dict()),
            # The annotation and its write identity must describe the same
            # loaded version, including records prefetched before editor open.
            # Sampling disk here could hide an intervening writer from assess().
            sidecar_fingerprint=deepcopy(record._loaded_sidecar_fingerprint),
            caption_fingerprint=_path_fingerprint(record.caption_path),
        )

    def assess(self, record: "ImageRecord") -> ImageRecordEditAssessment:
        record._ensure_loaded()
        changed = record.annotation.to_dict() != self.annotation
        return ImageRecordEditAssessment(
            changed=changed,
            stale_sidecar=_path_fingerprint(record.sidecar_path) != self.sidecar_fingerprint,
            stale_caption=_path_fingerprint(record.caption_path) != self.caption_fingerprint,
        )


class ImageRecord:
    """One image and its structured annotation sidecar."""

    def __init__(
        self, image_path: str | Path, *, auto_load: bool = True, known_tags=None,
        history_context=None,
    ) -> None:
        self.image_path = Path(image_path)
        self.caption_path = self.image_path.with_suffix(".txt")
        self.sidecar_path = self.image_path.with_suffix(".tags.json")

        self.annotation = ImageAnnotation(image=self.image_path.name)
        self.raw_text = ""
        self.loaded = False
        self.modified = False
        self.imported_from_legacy = False
        self.known_tags = known_tags
        self.history_context = history_context
        self._loaded_sidecar_fingerprint: dict[str, object] | None = None

        if auto_load:
            if self.history_context is not None:
                with self.history_context.locked(self.image_path):
                    self.load()
            else:
                self.load()

    @property
    def tags(self) -> TagCollection:
        """Backward-compatible view of uncategorized legacy/flat tags."""
        self._ensure_loaded()
        return TagCollection(self.annotation.uncategorized)

    @property
    def caption(self) -> list[str]:
        self._ensure_loaded()
        return list(self.annotation.plain_language)

    def load(self, *, force: bool = False) -> None:
        if self.loaded and not force:
            return

        self.imported_from_legacy = False
        if self.sidecar_path.exists():
            LibraryStorageInspector().assert_locally_readable(self.sidecar_path)
            raw, fingerprint = _read_bytes_snapshot(self.sidecar_path)
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError(f"Invalid sidecar root in {self.sidecar_path}")
            self.annotation = ImageAnnotation.from_dict(data, self.image_path.name)
            self._loaded_sidecar_fingerprint = fingerprint
        elif self.caption_path.exists():
            LibraryStorageInspector().assert_locally_readable(self.caption_path)
            self.raw_text = self.caption_path.read_bytes().decode("utf-8")
            imported = parse_generated_caption(self.raw_text, known_tags=self.known_tags)
            self.annotation = ImageAnnotation.from_legacy(
                image_name=self.image_path.name,
                tags=imported.sdxl_tags,
                caption=imported.plain_language,
                source_file=self.caption_path.name,
                source_format=imported.format,
                ambiguous_fragments=imported.ambiguous_fragments,
                raw_text=imported.raw_text,
            )
            self.imported_from_legacy = True
            self._loaded_sidecar_fingerprint = {"exists": False}
        else:
            self.raw_text = ""
            self.annotation = ImageAnnotation(image=self.image_path.name)
            self._loaded_sidecar_fingerprint = {"exists": False}

        self.loaded = True
        self.modified = False

    def _assert_storage_write_ready(self) -> None:
        inspector=LibraryStorageInspector()
        conflicts=(self.image_path,) if inspector.is_conflict_name(self.image_path.name) else ()
        availability=inspector.inspect_image(
            self.image_path,conflict_paths=conflicts,
        )
        if not availability.ready_for_write:
            raise OSError(
                f"{availability.label}: {availability.detail} {availability.recovery}".strip()
            )

    def import_legacy_caption(self, *, force: bool = False) -> bool:
        """Import the current .txt file into structured data.

        Existing sidecar-backed data is preserved unless force=True. The import is
        conservative: flat tags go to ``uncategorized`` and prose goes to
        ``plain_language`` for later review.
        """
        self._ensure_loaded()

        if not self.caption_path.exists():
            return False
        if self.sidecar_path.exists() and not force:
            return False

        self.raw_text = self.caption_path.read_bytes().decode("utf-8")
        imported = parse_generated_caption(self.raw_text, known_tags=self.known_tags)
        self.annotation = ImageAnnotation.from_legacy(
            image_name=self.image_path.name,
            tags=imported.sdxl_tags,
            caption=imported.plain_language,
            source_file=self.caption_path.name,
            source_format=imported.format,
            ambiguous_fragments=imported.ambiguous_fragments,
            raw_text=imported.raw_text,
        )
        self.imported_from_legacy = True
        self.loaded = True
        self.modified = True
        return True

    def has_tag(self, tag: str) -> bool:
        self._ensure_loaded()
        return tag in TagCollection(self.annotation.all_structured_tags())

    def add_tag(self, tag: str) -> bool:
        self._ensure_loaded()
        if has_automation_placeholder(tag):
            return False
        if normalize_tag(tag) in TagCollection(
            self.annotation.confirmed_absent
        ).normalized_set():
            return False
        if normalize_tag(tag) in TagCollection(
            self.annotation.all_structured_tags()
        ).normalized_set():
            return False
        tags = TagCollection(self.annotation.uncategorized)
        changed = tags.add(tag)
        if changed:
            self.annotation.uncategorized = tags.to_list()
            self.modified = True
        return changed

    def add_tags(self, tags: list[str]) -> int:
        self._ensure_loaded()
        collection = TagCollection(self.annotation.uncategorized)
        absent = TagCollection(
            self.annotation.confirmed_absent
        ).normalized_set()
        present = TagCollection(
            self.annotation.all_structured_tags()
        ).normalized_set()
        added = collection.extend(
            tag for tag in tags
            if (
                not has_automation_placeholder(tag)
                and normalize_tag(tag) not in absent
                and normalize_tag(tag) not in present
            )
        )
        if added:
            self.annotation.uncategorized = collection.to_list()
            self.modified = True
        return added

    def confirm_tags(self, tags: list[str], *, knowledge=None) -> int:
        """Explicitly confirm tags and reconcile only contradictory decisions."""

        service = ConfirmedTagIdentityService(knowledge)
        resolution = service.require_existing(
            tags,
            stored_ids=self.annotation.tag_identities,
        )
        decisions_changed = accept_confirmed_tag_decisions(
            self.annotation, resolution.values, knowledge=knowledge,
        )
        added = self.add_tags(list(resolution.values))
        identities_changed = service.bind_annotation(
            self.annotation,
            resolution,
            values=self.annotation.all_structured_tags(),
        )
        self.modified = self.modified or decisions_changed or identities_changed
        return added

    def confirm_sdxl_tags(self, tags: list[str], *, knowledge=None) -> bool:
        """Explicitly confirm flat SDXL Tags without changing their storage scope."""

        self._ensure_loaded()
        unresolved=[tag for tag in tags if has_automation_placeholder(tag)]
        if unresolved:
            raise ValueError(
                "Unresolved Automation templates cannot be confirmed as SDXL Tags: "
                + ", ".join(unresolved)
            )
        service = ConfirmedTagIdentityService(knowledge)
        resolution = service.require_existing(
            tags,
            stored_ids=self.annotation.tag_identities,
        )
        collection = TagCollection(self.annotation.sdxl_tags)
        present = TagCollection(
            self.annotation.all_structured_tags()
        ).normalized_set()
        added = collection.extend(
            value for value in resolution.values
            if normalize_tag(value) not in present
        )
        if added:
            self.annotation.sdxl_tags = collection.to_list()
        decisions_changed = accept_confirmed_tag_decisions(
            self.annotation, resolution.values, knowledge=knowledge,
        )
        identities_changed = service.bind_annotation(
            self.annotation,
            resolution,
            values=self.annotation.all_structured_tags(),
        )
        self.modified = (
            self.modified or bool(added) or decisions_changed
            or identities_changed
        )
        return bool(added) or decisions_changed or identities_changed

    def remove_tag(
        self,
        tag: str,
        *,
        character_slot: int | None = None,
        object_id: str | None = None,
    ) -> bool:
        self._ensure_loaded()
        if character_slot is not None and object_id is not None:
            raise ValueError("Choose either one character or one object scope.")
        changed = False

        characters = (
            [
                character
                for character in self.annotation.characters
                if character.slot == character_slot
            ]
            if character_slot is not None
            else self.annotation.characters
            if object_id is None
            else []
        )
        for character in characters:
            for key, values in character.tags.items():
                collection = TagCollection(values)
                if collection.remove(tag):
                    character.tags[key] = collection.to_list()
                    changed = True

        objects = (
            [
                item
                for item in self.annotation.objects
                if item.id == object_id
            ]
            if object_id is not None
            else self.annotation.objects
            if character_slot is None
            else []
        )
        for item in objects:
            if item.identity_tag and tag in TagCollection([item.identity_tag]):
                item.identity_tag = None
                item.identity_tag_id = None
                changed = True
            values = TagCollection(item.tags)
            if values.remove(tag):
                item.tags = values.to_list()
                changed = True

        if character_slot is None and object_id is None:
            for groups in (
                self.annotation.global_tags,
                self.annotation.scene,
                self.annotation.custom_groups,
            ):
                for key, values in groups.items():
                    collection = TagCollection(values)
                    if collection.remove(tag):
                        groups[key] = collection.to_list()
                        changed = True

            style_tags = TagCollection(self.annotation.visual_style.style_tags)
            if style_tags.remove(tag):
                self.annotation.visual_style.style_tags = style_tags.to_list()
                changed = True

            for attr in (
                "sdxl_tags", "uncategorized", "confirmed_absent", "excluded",
            ):
                collection = TagCollection(getattr(self.annotation, attr))
                if collection.remove(tag):
                    setattr(self.annotation, attr, collection.to_list())
                    changed = True

            for context in self.annotation.contexts:
                collection = TagCollection(context.tags)
                if collection.remove(tag):
                    context.tags = collection.to_list()
                    changed = True
            self.annotation.contexts = [
                context for context in self.annotation.contexts if context.tags
            ]

        if changed:
            confirmed = TagCollection(
                self.annotation.all_structured_tags()
            ).normalized_set()
            self.annotation.export_excluded = [
                value
                for value in self.annotation.export_excluded
                if normalize_tag(value) in confirmed
            ]

        self.modified = self.modified or changed
        return changed

    def replace_tag(
        self, old: str, new: str, *, new_tag_id: str | None = None,
    ) -> bool:
        self._ensure_loaded()
        changed = False

        for groups in (self.annotation.global_tags, self.annotation.scene, self.annotation.custom_groups):
            for key, values in groups.items():
                collection = TagCollection(values)
                if collection.replace(old, new):
                    groups[key] = collection.to_list()
                    changed = True

        for character in self.annotation.characters:
            for key, values in character.tags.items():
                collection = TagCollection(values)
                if collection.replace(old, new):
                    character.tags[key] = collection.to_list()
                    changed = True

        for context in self.annotation.contexts:
            collection = TagCollection(context.tags)
            if collection.replace(old, new):
                context.tags = collection.to_list()
                changed = True

        for item in self.annotation.objects:
            if item.identity_tag and old in TagCollection([item.identity_tag]):
                item.identity_tag = str(new).strip() or None
                item.identity_tag_id = (
                    str(new_tag_id).strip() or None
                    if new_tag_id is not None
                    else None
                )
                changed = True
            collection = TagCollection(item.tags)
            if collection.replace(old, new):
                item.tags = collection.to_list()
                changed = True

        style_tags = TagCollection(self.annotation.visual_style.style_tags)
        if style_tags.replace(old, new):
            self.annotation.visual_style.style_tags = style_tags.to_list()
            changed = True

        for attr in ("sdxl_tags", "uncategorized", "confirmed_absent", "excluded"):
            collection = TagCollection(getattr(self.annotation, attr))
            if collection.replace(old, new):
                setattr(self.annotation, attr, collection.to_list())
                changed = True

        if changed:
            old_key = normalize_tag(old)
            new_key = normalize_tag(new)
            stable_id = (
                str(new_tag_id).strip()
                if new_tag_id is not None
                else str(self.annotation.tag_identities.get(old_key, "")).strip()
            )
            self.annotation.tag_identities.pop(old_key, None)
            if new_key and stable_id:
                self.annotation.tag_identities[new_key] = stable_id
        self.modified = self.modified or changed
        return changed

    def clear_tags(self) -> bool:
        self._ensure_loaded()
        changed = bool(
            self.annotation.all_structured_tags()
            or self.annotation.export_excluded
            or any(item.identity_tag_id for item in self.annotation.objects)
        )
        if not changed:
            return False

        for groups in (self.annotation.global_tags, self.annotation.scene, self.annotation.custom_groups):
            for key in groups:
                groups[key] = []
        for character in self.annotation.characters:
            for key in character.tags:
                character.tags[key] = []
        for item in self.annotation.objects:
            item.identity_tag_id = None
            item.identity_tag = None
            item.tags = []
        self.annotation.visual_style.style_tags = []
        self.annotation.sdxl_tags = []
        self.annotation.uncategorized = []
        self.annotation.contexts = []
        self.annotation.export_excluded = []
        self.annotation.tag_identities = {}
        self.modified = True
        return True

    def set_caption(self, caption: str | list[str]) -> bool:
        self._ensure_loaded()
        if isinstance(caption, str):
            values = [caption.strip()] if caption.strip() else []
        else:
            values = [value.strip() for value in caption if value.strip()]

        if values == self.annotation.plain_language:
            return False
        self.annotation.plain_language = values
        self.modified = True
        return True

    def add_character(
        self,
        *,
        character_id: str | None = None,
        role: str | None = None,
    ):
        self._ensure_loaded()
        character = self.annotation.add_character(character_id=character_id, role=role)
        self.modified = True
        return character

    def assign_character(self, slot: int, character_id: str | None) -> bool:
        self._ensure_loaded()
        character = self.annotation.get_character(slot)
        if character is None:
            raise KeyError(f"No character in slot {slot}")
        if character.character_id == character_id:
            return False
        character.character_id = character_id
        self.modified = True
        return True

    def output_text(
        self,
        profile: ExportProfile | None = None,
        character_db: CharacterDatabase | None = None,
        knowledge=None,
    ) -> str:
        self._ensure_loaded()
        return (profile or DEFAULT_EXPORT_PROFILE).render(self.annotation, character_db, knowledge)

    def output_projection(
        self,
        profile: ExportProfile | None = None,
        character_db: CharacterDatabase | None = None,
        knowledge=None,
    ):
        self._ensure_loaded()
        return (profile or DEFAULT_EXPORT_PROFILE).project(self.annotation, character_db, knowledge)

    def save_sidecar(self, *, force: bool = False, knowledge=None) -> bool:
        if self.history_context is not None:
            with self.history_context.locked(self.image_path):
                return self._save_sidecar(force=force, knowledge=knowledge)
        return self._save_sidecar(force=force, knowledge=knowledge)

    def _save_sidecar(self, *, force: bool = False, knowledge=None) -> bool:
        self._ensure_loaded()
        identities_changed = ConfirmedTagIdentityService.prune_annotation(
            self.annotation
        )
        if knowledge is not None:
            identities_changed = ConfirmedTagIdentityService.shared(
                knowledge
            ).synchronize_annotation(self.annotation) or identities_changed
        self.modified = self.modified or identities_changed
        if not self.modified and self.sidecar_path.exists() and not force:
            return False

        self._assert_storage_write_ready()
        current_fingerprint = _path_fingerprint(self.sidecar_path)
        expected_fingerprint = self._loaded_sidecar_fingerprint
        if (
            expected_fingerprint is not None
            and current_fingerprint != expected_fingerprint
        ):
            raise ImageRecordStaleWriteError(
                "Image Knowledge changed after this record was loaded; "
                "the newer sidecar was preserved. Reload before saving."
            )
        self.sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        expected = self.annotation.to_dict()
        expected_text = json.dumps(expected, indent=2, ensure_ascii=False) + "\n"
        if self.history_context is not None:
            return self.history_context.write(
                self.image_path, expected_text.encode("utf-8"),
                lambda: self._write_verified_sidecar(expected, expected_text, current_fingerprint),
            )
        return self._write_verified_sidecar(expected, expected_text, current_fingerprint)

    def _write_verified_sidecar(self, expected, expected_text, current_fingerprint) -> bool:
        # Snapshotting may take time. Repeat the stale guard immediately before
        # the canonical write, including noncooperating writers during capture.
        if _path_fingerprint(self.sidecar_path) != current_fingerprint:
            raise ImageRecordStaleWriteError("Image Knowledge changed before saving; the competing sidecar was preserved.")
        if current_fingerprint.get("exists"):
            _atomic_write_text(self.sidecar_path, expected_text)
        else:
            # A writer that appears after the final check wins; never replace it.
            try:
                _atomic_create_text(self.sidecar_path, expected_text)
            except FileExistsError as exc:
                raise ImageRecordStaleWriteError(
                    "Image Knowledge appeared while this record was being saved; "
                    "the competing sidecar was preserved. Reload before saving."
                ) from exc
        actual_raw, actual_fingerprint = _read_bytes_snapshot(self.sidecar_path)
        if actual_raw != expected_text.encode("utf-8"):
            raise RuntimeError(
                "Image Knowledge changed before read-back verification; "
                "the competing sidecar was preserved."
            )
        actual = json.loads(actual_raw.decode("utf-8"))
        if actual != expected:
            raise RuntimeError("Image Knowledge failed semantic read-back verification.")
        self._loaded_sidecar_fingerprint = actual_fingerprint
        self.imported_from_legacy = False
        return True

    def create_sidecar(self) -> bool:
        """Atomically create authority only when no sidecar already exists."""
        if self.history_context is not None:
            with self.history_context.locked(self.image_path):
                return self._create_sidecar()
        return self._create_sidecar()

    def _create_sidecar(self) -> bool:
        self._ensure_loaded()
        if self.sidecar_path.exists():
            return False
        self._assert_storage_write_ready()
        self.sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        expected=self.annotation.to_dict()
        expected_text=json.dumps(expected, indent=2, ensure_ascii=False) + "\n"
        if self.history_context is not None:
            return self.history_context.write(
                self.image_path, expected_text.encode("utf-8"),
                lambda: self._create_verified_sidecar(expected, expected_text),
            )
        return self._create_verified_sidecar(expected, expected_text)

    def _create_verified_sidecar(self, expected, expected_text) -> bool:
        _atomic_create_text(self.sidecar_path,expected_text)
        actual_text=self.sidecar_path.read_text(encoding="utf-8")
        if actual_text!=expected_text or json.loads(actual_text)!=expected:
            raise RuntimeError(
                f"The new image record changed before read-back verification: {self.sidecar_path.name}"
            )
        self._loaded_sidecar_fingerprint = _path_fingerprint(self.sidecar_path)
        self.imported_from_legacy = False
        self.modified = False
        return True

    def export_caption(
        self,
        *,
        profile: ExportProfile | None = None,
        character_db: CharacterDatabase | None = None,
        knowledge=None,
        force: bool = False,
    ) -> bool:
        self._ensure_loaded()
        identities_changed=ConfirmedTagIdentityService.prune_annotation(
            self.annotation
        )
        if knowledge is not None:
            identities_changed=(
                ConfirmedTagIdentityService.shared(knowledge)
                .synchronize_annotation(self.annotation)
                or identities_changed
            )
        self.modified=self.modified or identities_changed
        selected_profile = profile or DEFAULT_EXPORT_PROFILE
        projection = self.output_projection(selected_profile, character_db, knowledge)
        output = projection.render(selected_profile.separator)
        changed = True
        if self.caption_path.exists() and not force:
            current = self.caption_path.read_text(encoding="utf-8")
            if current == output:
                changed = False

        if changed:
            self._assert_storage_write_ready()
            self.caption_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(self.caption_path, output)
        if self.caption_path.read_text(encoding="utf-8") != output:
            raise RuntimeError("The TXT caption failed read-back verification.")
        self.raw_text = output
        existing_state = self.annotation.projection_state.get("txt", {})
        if not isinstance(existing_state, dict):
            existing_state = {}
        state = {
            **existing_state,
            "writer": "collectigen",
            "format": "sdxl_tags_then_plain_language_v1",
            **selected_profile.provenance(),
            "profile": selected_profile.to_dict(),
            "content_sha256": text_sha256(output),
            "sdxl_tags": list(projection.sdxl_tags),
            "plain_language": list(projection.plain_language),
            "knowledge_fingerprint": annotation_knowledge_fingerprint(self.annotation),
            "projection_fingerprint": _path_fingerprint(self.caption_path),
            "status": "verified",
        }
        stable_existing = {
            key: value for key, value in existing_state.items() if key != "verified_at"
        }
        stable_state = {key:value for key,value in state.items() if key!="verified_at"}
        state["verified_at"] = (
            existing_state.get("verified_at")
            if stable_existing == stable_state and existing_state.get("verified_at")
            else datetime.now(timezone.utc).isoformat(timespec="seconds")
        )
        state_changed = existing_state != state
        self.annotation.projection_state["txt"] = state
        if state_changed:
            self.modified = True
            self.save_sidecar(force=True, knowledge=knowledge)
        return changed or state_changed

    def save(
        self,
        *,
        profile: ExportProfile | None = None,
        character_db: CharacterDatabase | None = None,
        knowledge=None,
        export_txt: bool = True,
        force: bool = False,
    ) -> bool:
        self._ensure_loaded()
        sidecar_changed = self.save_sidecar(
            force=force or self.imported_from_legacy,
            knowledge=knowledge,
        )
        caption_changed = False
        if export_txt:
            caption_changed = self.export_caption(
                profile=profile,
                character_db=character_db,
                knowledge=knowledge,
                force=force,
            )
        self.modified = False
        return sidecar_changed or caption_changed

    def discard_changes(self) -> None:
        self.load(force=True)

    def _ensure_loaded(self) -> None:
        if not self.loaded:
            self.load()

    def __contains__(self, tag: object) -> bool:
        return isinstance(tag, str) and self.has_tag(tag)

    def __len__(self) -> int:
        self._ensure_loaded()
        return len(self.annotation.all_structured_tags())

    def __repr__(self) -> str:
        state = "modified" if self.modified else "clean"
        return (
            f"ImageRecord({self.image_path.name!r}, "
            f"{len(self.annotation.all_structured_tags())} tags, {state})"
        )
