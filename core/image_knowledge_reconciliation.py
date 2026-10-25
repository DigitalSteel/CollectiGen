from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable
from uuid import uuid4

from .caption_import import parse_generated_caption
from .annotation import GenerationRecord
from .automation_templates import concrete_sdxl_tags
from .export_profile import DEFAULT_EXPORT_PROFILE, ExportProfile
from .image_record import ImageRecord, _atomic_write_text, annotation_knowledge_fingerprint
from .metadata_sync import (
    MetadataSyncService,
    portable_xmp_text,
    xmp_incompatible_character_count,
)
from .tag_collection import TagCollection, normalize_tag
from .character_database import CharacterDatabase
from .tag_database import TagDatabase
from .history_versions import HistoryError,_reject_reparse
from .image_knowledge_history import HistoryWriteInterrupted


def _fingerprint(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"exists": False}
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    stat = path.stat()
    return {
        "exists": True,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": digest.hexdigest(),
    }


def _value_fingerprint(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def knowledge_fingerprint(record: ImageRecord) -> str:
    return annotation_knowledge_fingerprint(record.annotation)


def _differences(expected: Iterable[str], current: Iterable[str], *, tags: bool) -> tuple[list[str], list[str]]:
    key = normalize_tag if tags else lambda value: " ".join(str(value).casefold().split())
    expected_values = TagCollection(expected).to_list() if tags else list(dict.fromkeys(expected))
    current_values = TagCollection(current).to_list() if tags else list(dict.fromkeys(current))
    expected_keys = {key(value) for value in expected_values}
    current_keys = {key(value) for value in current_values}
    return (
        [value for value in expected_values if key(value) not in current_keys],
        [value for value in current_values if key(value) not in expected_keys],
    )


def _managed_xmp_subject(values: object) -> list[str]:
    """Return only the XMP Subject values this application owns and writes."""
    if not isinstance(values, dict):
        return []
    fields = values.get("tag_fields", {})
    if not isinstance(fields, dict) or "Subject" not in fields:
        return []
    subject = fields.get("Subject")
    if isinstance(subject, (list, tuple)):
        return [str(value) for value in subject if str(value).strip()]
    if subject is None or not str(subject).strip():
        return []
    return [str(subject)]


@dataclass
class ProjectionDifference:
    target: str
    available: bool
    will_write: bool
    added_tags: list[str] = field(default_factory=list)
    removed_tags: list[str] = field(default_factory=list)
    added_descriptions: list[str] = field(default_factory=list)
    removed_descriptions: list[str] = field(default_factory=list)
    error: str = ""
    order_changed: bool = False
    ambiguous_fragments: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.error:
            return f"{self.target}: {self.error}"
        changes = (
            len(self.added_tags) + len(self.removed_tags)
            + len(self.added_descriptions) + len(self.removed_descriptions)
            + int(self.order_changed)
            + len(self.ambiguous_fragments)
        )
        return f"{self.target}: {changes} difference(s)"


@dataclass
class ReconciliationPreview:
    image: Path
    authority_fingerprint: dict[str, object]
    txt_fingerprint: dict[str, object]
    image_fingerprint: dict[str, object]
    knowledge_fingerprint: str
    expected_txt: str
    expected_txt_tags: list[str]
    expected_metadata_tags: list[str]
    expected_descriptions: list[str]
    expected_metadata_descriptions: list[str]
    metadata_portability_replacements: int = 0
    differences: list[ProjectionDifference] = field(default_factory=list)
    embedded_generation_fields: dict[str, object] = field(default_factory=dict)
    embedded_generation_records: list[dict[str, object]] = field(default_factory=list)
    embedded_standard_metadata: dict[str, object] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def can_apply(self) -> bool:
        return not self.errors and any(item.will_write for item in self.differences)

    def difference(self, target: str) -> ProjectionDifference | None:
        return next((item for item in self.differences if item.target == target), None)


@dataclass
class ReconciliationResult:
    image: Path
    changed_targets: list[str] = field(default_factory=list)
    verified_targets: list[str] = field(default_factory=list)
    backup_directory: Path | None = None
    recovery_manifest: Path | None = None
    errors: list[str] = field(default_factory=list)
    recovered: bool = False
    history_operation_id: str = ""
    history_interrupted: bool = False
    history_held: bool = False

    @property
    def success(self) -> bool:
        return not self.errors and set(self.changed_targets) == set(self.verified_targets)


@dataclass
class ReconciliationBatchResult:
    results: list[ReconciliationResult] = field(default_factory=list)
    cancelled: bool = False

    @property
    def succeeded(self) -> int:
        return sum(1 for item in self.results if item.success)

    @property
    def failed(self) -> int:
        return sum(1 for item in self.results if not item.success)


class ImageKnowledgeReconciliationService:
    """Preview and safely project authoritative JSON into TXT and metadata."""

    def __init__(
        self,
        metadata: MetadataSyncService | None = None,
        *,
        profile: ExportProfile | None = None,
        knowledge: TagDatabase | None = None,
        characters: CharacterDatabase | None = None,
    ) -> None:
        self.metadata = metadata or MetadataSyncService()
        self.profile = profile or DEFAULT_EXPORT_PROFILE
        self.knowledge = knowledge
        self.characters = characters

    def preview(
        self,image: str | Path,*,include_metadata: bool=True,
        record: ImageRecord|None=None,
    ) -> ReconciliationPreview:
        image = Path(image)
        record = record if record is not None else ImageRecord(image)
        if record.image_path != image:
            raise ValueError("The in-memory ImageRecord does not match the preview image.")
        if not record.sidecar_path.exists():
            raise ValueError("Authoritative .tags.json record is required before reconciliation.")

        all_present = TagCollection(record.annotation.all_structured_tags()).to_list()
        present = concrete_sdxl_tags(all_present)
        absent = TagCollection(record.annotation.confirmed_absent).normalized_set()
        contradictions = [tag for tag in all_present if normalize_tag(tag) in absent]
        confirmed_metadata_tags = [
            tag for tag in present if normalize_tag(tag) not in absent
        ]
        expected_projection = record.output_projection(self.profile,self.characters,self.knowledge)
        expected_descriptions = list(expected_projection.plain_language)
        expected_metadata_tags = [
            portable_xmp_text(tag) for tag in confirmed_metadata_tags
        ]
        expected_metadata_descriptions = [
            portable_xmp_text(value) for value in expected_descriptions
        ]
        metadata_portability_replacements = sum(
            xmp_incompatible_character_count(value)
            for value in (*confirmed_metadata_tags,*expected_descriptions)
        )
        expected_txt = expected_projection.render(self.profile.separator)
        authority_knowledge_fingerprint = knowledge_fingerprint(record)
        preview = ReconciliationPreview(
            image=image,
            authority_fingerprint=_fingerprint(record.sidecar_path),
            txt_fingerprint=_fingerprint(record.caption_path),
            image_fingerprint=_fingerprint(image),
            knowledge_fingerprint=authority_knowledge_fingerprint,
            expected_txt=expected_txt,
            expected_txt_tags=list(expected_projection.sdxl_tags),
            expected_metadata_tags=expected_metadata_tags,
            expected_descriptions=expected_descriptions,
            expected_metadata_descriptions=expected_metadata_descriptions,
            metadata_portability_replacements=metadata_portability_replacements,
        )
        if contradictions:
            preview.errors.append(
                "Confirmed-present and confirmed-absent tags conflict: "
                + ", ".join(contradictions)
            )

        current_txt_tags: list[str] = []
        current_txt_prose: list[str] = []
        txt_error = ""
        current_txt = ""
        if record.caption_path.exists():
            try:
                current_txt = record.caption_path.read_bytes().decode("utf-8")
                known = (lambda value: self.knowledge.find(value) is not None) if self.knowledge is not None else None
                parsed = parse_generated_caption(
                    current_txt, known_tags=known,
                    projection_state=record.annotation.projection_state.get("txt"),
                )
                current_txt_tags = parsed.sdxl_tags
                current_txt_prose = parsed.plain_language
                current_txt_ambiguous = parsed.ambiguous_fragments
            except Exception as exc:
                txt_error = str(exc)
                current_txt_ambiguous = []
        else:
            current_txt_ambiguous = []
        add_tags, remove_tags = _differences(expected_projection.sdxl_tags, current_txt_tags, tags=True)
        add_prose, remove_prose = _differences(
            expected_projection.plain_language, current_txt_prose, tags=False
        )
        order_changed=not add_tags and not remove_tags and [normalize_tag(value) for value in expected_projection.sdxl_tags]!=[normalize_tag(value) for value in current_txt_tags]
        preview.differences.append(ProjectionDifference(
            target="txt", available=True, will_write=current_txt!=expected_txt,
            added_tags=add_tags, removed_tags=remove_tags,
            added_descriptions=add_prose, removed_descriptions=remove_prose,
            error=txt_error, order_changed=order_changed,
            ambiguous_fragments=current_txt_ambiguous,
        ))

        if include_metadata:
            available = bool(getattr(self.metadata, "is_available", lambda: True)())
            metadata_error = ""
            values: dict[str, object] = {}
            if available:
                try:
                    values = self.metadata.read_embedded(image)
                except Exception as exc:
                    metadata_error = str(exc)
            else:
                metadata_error = "Embedded metadata support is unavailable."
            metadata_tags = _managed_xmp_subject(values)
            metadata_prose = values.get("plain_language", []) if isinstance(values, dict) else []
            if isinstance(values, dict) and values.get("description_fields", {}).get("Description") is not None:
                description = str(values["description_fields"]["Description"] or "")
                metadata_prose = [part for part in description.split("\n\n") if part.strip()]
            add_tags, remove_tags = _differences(expected_metadata_tags, metadata_tags, tags=True)
            add_prose, remove_prose = _differences(
                preview.expected_metadata_descriptions, metadata_prose, tags=False
            )
            preview.embedded_generation_fields = dict(values.get("raw_generation_fields", {}))
            preview.embedded_generation_records = [
                dict(item) for item in values.get("generation_records", [])
                if isinstance(item, dict)
            ]
            preview.embedded_standard_metadata = dict(values.get("standard_metadata", {}))
            preview.differences.append(ProjectionDifference(
                "metadata", available and not metadata_error,
                available and not metadata_error and bool(
                    add_tags or remove_tags or add_prose or remove_prose
                ),
                add_tags, remove_tags, add_prose, remove_prose, metadata_error,
            ))
        return preview

    def apply(
        self,
        preview: ReconciliationPreview,
        *,
        targets: Iterable[str] = ("txt", "metadata"),
        backup_root: str | Path | None = None,
        record: ImageRecord | None = None,
        durable_backup: bool = True,
        history_operation=None,
    ) -> ReconciliationResult:
        context=getattr(record,"history_context",None)
        if context is None and history_operation is None:
            return self._apply_unlocked(preview,targets=targets,backup_root=backup_root,
                record=record,durable_backup=durable_backup)
        try:
            if history_operation is not None:
                context=history_operation.context_for(preview.image)
                if context is None or context.operation_id!=history_operation.operation_id:
                    raise HistoryError("Projection lacks its exact admitted history operation.")
            with context.locked(preview.image):
                if context.store.has_pending(context.entity):
                    raise HistoryWriteInterrupted("Reopen this image for saved-history recovery before synchronizing related files.")
                selected=tuple(targets)
                private=context.store.scope.history_root/"projection-recovery"
                _reject_reparse(private)
                if not private.resolve().is_relative_to(context.store.scope.history_root):
                    raise HistoryError("Projection recovery escaped its private history storage.")
                working=record if record is not None else ImageRecord(preview.image,history_context=context)
                if working.image_path!=preview.image:
                    raise HistoryError("Projection record belongs to a different image.")
                working.history_context=context
                return self._apply_unlocked(preview,targets=selected,backup_root=private,
                    record=working,durable_backup=True,history_operation=history_operation)
        except Exception as exc:
            return ReconciliationResult(preview.image,errors=[str(exc)],
                history_operation_id=getattr(context,"operation_id",getattr(history_operation,"operation_id","")),
                history_interrupted=isinstance(exc,HistoryWriteInterrupted),history_held=True)

    def _apply_unlocked(
        self,preview: ReconciliationPreview,*,targets=("txt","metadata"),
        backup_root=None,record=None,durable_backup=True,history_operation=None,
    ) -> ReconciliationResult:
        result = ReconciliationResult(preview.image)
        context=getattr(record,"history_context",None)
        result.history_operation_id=getattr(context,"operation_id","")
        sidecar_started=False
        metadata_published=False
        selected = {str(value) for value in targets}
        if preview.errors:
            result.errors.extend(preview.errors)
            return result
        if _fingerprint(preview.image.with_suffix(".tags.json")) != preview.authority_fingerprint:
            result.errors.append("The authoritative JSON record changed after preview; preview again.")
            return result
        if "txt" in selected and _fingerprint(preview.image.with_suffix(".txt")) != preview.txt_fingerprint:
            result.errors.append("The TXT caption changed after preview; preview again.")
            return result
        if "metadata" in selected and _fingerprint(preview.image) != preview.image_fingerprint:
            result.errors.append("The image or embedded metadata changed after preview; preview again.")
            return result

        target_errors = [
            item.summary for item in preview.differences
            if item.target in selected and item.error
        ]
        if target_errors:
            result.errors.extend(target_errors)
            return result

        changes = [
            item.target for item in preview.differences
            if item.target in selected and item.will_write
        ]
        if not changes:
            return result
        unavailable = [
            item.summary for item in preview.differences
            if item.target in changes and not item.available
        ]
        if unavailable:
            result.errors.extend(unavailable)
            return result

        temporary_recovery = None
        if durable_backup:
            transaction = self._create_backup(preview, changes, backup_root)
            result.backup_directory, result.recovery_manifest = transaction
            recovery_manifest=result.recovery_manifest
        else:
            # Backup-Off workflows still need operation-local rollback when a
            # write or read-back fails. The temporary copy is removed after the
            # verified transaction and is never offered as durable recovery.
            temporary_recovery=tempfile.TemporaryDirectory(
                prefix="collectigen-metadata-rollback-"
            )
            _directory,recovery_manifest=self._create_backup(
                preview,changes,temporary_recovery.name,
            )
        try:
            working_record = record if record is not None else ImageRecord(preview.image)
            metadata_after: dict[str, object] = {}
            if context is not None and "metadata" in changes:
                from .history_projection import prepare_projection,publish_projection
                manifest=json.loads(Path(recovery_manifest).read_text(encoding="utf-8"))
                image_backup=next(Path(item["backup"]) for item in manifest["files"] if Path(item["original"])==preview.image.resolve())
                prepared=prepare_projection(context,preview.image,self.metadata,preview,image_backup,
                    cancelled=getattr(history_operation,"cancelled",None))
                previous_context=context
                context=publish_projection(context,preview.image,prepared,
                    cancelled=getattr(history_operation,"cancelled",None))
                metadata_published=True
                working_record.history_context=context
                if history_operation is not None:
                    history_operation.replace_context(preview.image,previous_context,context)
                metadata_after=prepared.metadata_after
                result.changed_targets.append("metadata")
            if "txt" in changes:
                _atomic_write_text(working_record.caption_path, preview.expected_txt)
                result.changed_targets.append("txt")
                if working_record.caption_path.read_text(encoding="utf-8") != preview.expected_txt:
                    raise RuntimeError("TXT read-back did not match the previewed projection.")
                result.verified_targets.append("txt")

            if "metadata" in changes:
                if context is None:
                    self.metadata.write_xmp(
                        preview.image,
                        preview.expected_metadata_tags,
                        "\n\n".join(preview.expected_metadata_descriptions),
                    )
                    result.changed_targets.append("metadata")
                    metadata_after = self.metadata.read_embedded(preview.image)
                subject_values = _managed_xmp_subject(metadata_after)
                if TagCollection(subject_values).normalized_set() != TagCollection(
                    preview.expected_metadata_tags
                ).normalized_set():
                    raise RuntimeError("The image's embedded confirmed tags did not match after saving; the original metadata was restored.")
                expected_description = "\n\n".join(
                    preview.expected_metadata_descriptions
                )
                actual_description = metadata_after.get("description_fields", {}).get(
                    "Description", expected_description if not expected_description else None
                )
                if str(actual_description or "") != expected_description:
                    raise RuntimeError("Embedded description failed read-back verification.")
                if metadata_after.get("raw_generation_fields", {}) != preview.embedded_generation_fields:
                    raise RuntimeError("Embedded generator data changed during synchronization.")
                if metadata_after.get("standard_metadata", {}) != preview.embedded_standard_metadata:
                    raise RuntimeError("Standard image Metadata changed during synchronization.")
                result.verified_targets.append("metadata")

            verified_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if "metadata" in changes:
                working_record.annotation.standard_metadata = dict(
                    metadata_after.get("standard_metadata", preview.embedded_standard_metadata)
                )
                working_record.annotation.file_identity = _fingerprint(preview.image)
                generation_values = metadata_after.get(
                    "generation_records", preview.embedded_generation_records
                )
                for item in generation_values:
                    if isinstance(item, dict):
                        working_record.annotation.add_generation_record(GenerationRecord.from_dict(item))
            for target in result.verified_targets:
                path = working_record.caption_path if target == "txt" else preview.image
                working_record.annotation.projection_state[target] = {
                    **(
                        working_record.annotation.projection_state.get(target,{})
                        if isinstance(working_record.annotation.projection_state.get(target),dict)
                        else {}
                    ),
                    "knowledge_fingerprint": preview.knowledge_fingerprint,
                    "projection_fingerprint": _fingerprint(path),
                    "verified_at": verified_at,
                    "status": "verified",
                }
                if target == "txt":
                    working_record.annotation.projection_state[target].update({
                        "writer": "collectigen",
                        "format": "sdxl_tags_then_plain_language_v1",
                        **self.profile.provenance(),
                        "profile": self.profile.to_dict(),
                        "content_sha256": hashlib.sha256(preview.expected_txt.encode("utf-8")).hexdigest(),
                        "sdxl_tags": list(preview.expected_txt_tags),
                        "plain_language": list(preview.expected_descriptions),
                    })
            working_record.modified = True
            sidecar_started=True
            working_record.save_sidecar(force=True)
            reloaded = ImageRecord(preview.image)
            for target in result.verified_targets:
                if reloaded.annotation.projection_state.get(target, {}).get("status") != "verified":
                    raise RuntimeError("Projection state failed sidecar read-back verification.")
        except Exception as exc:
            result.errors.append(str(exc))
            if context is not None:
                result.history_interrupted=metadata_published or sidecar_started or isinstance(exc,HistoryWriteInterrupted)
                result.history_held=not result.history_interrupted
                result.errors.append("Related files may already have changed. Current data and private recovery evidence were retained; review the exact image's history before retrying.")
            else:
                try:
                    self.restore(recovery_manifest)
                    result.recovered = True
                    result.verified_targets.clear()
                except Exception as recovery_exc:
                    result.errors.append(f"Automatic recovery failed: {recovery_exc}")
        finally:
            if temporary_recovery is not None:
                temporary_recovery.cleanup()
        return result

    def apply_many(
        self,
        previews: Iterable[ReconciliationPreview],
        *,
        targets: Iterable[str] = ("txt", "metadata"),
        backup_root: str | Path | None = None,
        should_cancel: Callable[[], bool] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> ReconciliationBatchResult:
        items = list(previews)
        batch = ReconciliationBatchResult()
        for index, preview in enumerate(items, start=1):
            if should_cancel and should_cancel():
                batch.cancelled = True
                break
            batch.results.append(self.apply(
                preview, targets=targets, backup_root=backup_root
            ))
            if progress:
                progress(index, len(items), preview.image.name)
        return batch

    @staticmethod
    def restore(manifest_path: str | Path | None) -> None:
        if manifest_path is None:
            raise ValueError("No recovery manifest is available.")
        manifest = Path(manifest_path)
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        for item in payload.get("files", []):
            original = Path(item["original"])
            backup = Path(item["backup"])
            if item.get("existed"):
                original.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, original)
            elif original.exists():
                original.unlink()

    @staticmethod
    def _create_backup(
        preview: ReconciliationPreview,
        changes: list[str],
        backup_root: str | Path | None,
    ) -> tuple[Path, Path]:
        root = Path(backup_root) if backup_root else preview.image.parent / ".tagmanager-backups"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        directory = root / f"{preview.image.stem}_{stamp}_{uuid4().hex[:8]}"
        directory.mkdir(parents=True, exist_ok=False)
        paths = [preview.image.with_suffix(".tags.json")]
        if "txt" in changes:
            paths.append(preview.image.with_suffix(".txt"))
        if "metadata" in changes:
            paths.append(preview.image)
        entries = []
        for index, original in enumerate(paths):
            backup = directory / f"{index:02d}_{original.name}"
            existed = original.exists()
            if existed:
                shutil.copy2(original, backup)
            entries.append({
                "original": str(original.resolve()),
                "backup": str(backup.resolve()),
                "existed": existed,
            })
        manifest = directory / "recovery.json"
        _atomic_write_text(manifest, json.dumps({
            "schema_version": 1,
            "image": str(preview.image.resolve()),
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "knowledge_fingerprint": preview.knowledge_fingerprint,
            "changes": changes,
            "files": entries,
        }, indent=2, ensure_ascii=False) + "\n")
        return directory, manifest
