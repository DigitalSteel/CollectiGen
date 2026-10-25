from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .annotation import ImageAnnotation
from .image_record import ImageRecord
from .operation_journal import JournalOperation, OperationJournal
from .tag_collection import TagCollection, normalize_tag


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def image_identity_fingerprint(image: str | Path) -> str:
    image = Path(image)
    sidecar = image.with_suffix(".tags.json")
    digest = hashlib.sha256()
    for path in (image, sidecar):
        try:
            current = path.stat()
            digest.update(
                f"{path}:{current.st_size}:{current.st_mtime_ns}:".encode("utf-8")
            )
            if path == sidecar:
                digest.update(path.read_bytes())
        except OSError:
            digest.update(f"{path}:missing:".encode("utf-8"))
    return digest.hexdigest()


@dataclass(frozen=True)
class ImageIdentityCorrectionChange:
    location: str
    before: str
    after: str
    stable_id: str
    reason: str


@dataclass
class ImageIdentityCorrectionItem:
    image: Path
    fingerprint: str
    status: str
    changes: tuple[ImageIdentityCorrectionChange, ...] = ()
    warnings: tuple[str, ...] = ()
    message: str = ""
    before: dict | None = field(default=None, repr=False)
    after: dict | None = field(default=None, repr=False)
    before_raw: str = field(default="", repr=False)
    after_raw: str = field(default="", repr=False)


@dataclass
class ImageIdentityCorrectionPreview:
    image_count: int
    candidate_count: int
    items: list[ImageIdentityCorrectionItem] = field(default_factory=list)
    current_count: int = 0
    cancelled: bool = False
    target_ids: tuple[str, ...] = ()
    source_names: tuple[str, ...] = ()
    checked_count: int = 0

    def count(self, status: str) -> int:
        return sum(item.status == status for item in self.items)

    @property
    def will_change(self) -> int:
        return self.count("will_change")

    @property
    def unresolved(self) -> int:
        return sum(
            item.status == "unresolved"
            or (item.status == "will_change" and bool(item.warnings))
            for item in self.items
        )

    @property
    def unavailable(self) -> int:
        return self.count("unavailable") + self.count("read_only")

    @property
    def failed(self) -> int:
        return self.count("failed")


@dataclass(frozen=True)
class ImageIdentityCorrectionDiscovery:
    image_count: int
    candidate_paths: tuple[Path, ...]
    uncertain_count: int = 0


@dataclass(frozen=True)
class ImageIdentityCorrectionApplyItem:
    image: Path
    status: str
    changes: tuple[ImageIdentityCorrectionChange, ...] = ()
    error: str = ""
    version_id: str = ""


@dataclass
class ImageIdentityCorrectionApplyResult:
    operation_id: str = ""
    items: list[ImageIdentityCorrectionApplyItem] = field(default_factory=list)
    cancelled: bool = False
    history_operation_id: str = ""
    history_interrupted: bool = False

    def count(self, status: str) -> int:
        return sum(item.status == status for item in self.items)

    @property
    def changed(self) -> int:
        return self.count("changed")

    @property
    def stale(self) -> int:
        return self.count("stale")

    @property
    def failed(self) -> int:
        return self.count("failed") + self.count("held") + self.count("interrupted")

    @property
    def not_processed(self) -> int:
        return self.count("not_processed")


@dataclass(frozen=True)
class ImageIdentityCorrectionRecovery:
    operation_id: str
    applied: int = 0
    untouched: int = 0
    conflicts: tuple[str, ...] = ()


class ImageIdentityCorrectionStaleError(RuntimeError):
    """The Image Knowledge no longer matches a correction or restore snapshot."""


class ImageIdentityCorrectionService:
    """Audit and normalize exact confirmed tag identities in Image Knowledge.

    The service writes only authoritative ``.tags.json`` Image Knowledge.
    Adjacent TXT, embedded Metadata, source images, and raw observations remain
    outside its authority.
    """

    OPERATION_KIND = "image_identity_correction"
    UNDO_KIND = "image_identity_correction_undo"
    CHECKPOINT_SCHEMA = 1

    def __init__(
        self,
        knowledge,
        journal: OperationJournal,
        checkpoint_path: str | Path,
    ) -> None:
        self.knowledge = knowledge
        self.journal = journal
        self.checkpoint_path = Path(checkpoint_path)
        self._names = self._name_index()

    def _name_index(self) -> dict[str, tuple]:
        names: dict[str, dict[str, object]] = {}
        for key in self.knowledge.identity_name_keys():
            for stable_id in self.knowledge.identity_owner_ids(key):
                record=self.knowledge.tags.get(stable_id)
                if record is not None:names.setdefault(key,{})[record.id]=record
        return {
            key: tuple(
                sorted(records.values(), key=lambda record: record.id.casefold())
            )
            for key, records in names.items()
        }

    def _resolved_target_ids(self, target_ids: Iterable[str]) -> set[str]:
        result = set()
        for value in target_ids:
            stable_id = str(value).strip()
            if not stable_id:
                continue
            resolved = self.knowledge.resolve_tag_id(stable_id)
            if resolved:
                result.add(resolved)
        return result

    def _target_names(
        self,
        target_ids: Iterable[str],
        source_names: Iterable[str],
    ) -> set[str]:
        result = {
            normalize_tag(value)
            for value in source_names
            if normalize_tag(value)
        }
        for stable_id in self._resolved_target_ids(target_ids):
            record = self.knowledge.tags.get(stable_id)
            if record is None:
                continue
            result.update(
                normalize_tag(value)
                for value in self.knowledge.identity_names(record)
                if normalize_tag(value)
            )
        return result

    def discover(
        self,
        cached_records: Iterable,
        *,
        target_ids: Iterable[str] = (),
        source_names: Iterable[str] = (),
    ) -> ImageIdentityCorrectionDiscovery:
        """Select candidates from the rebuildable index without opening files."""

        records = tuple(cached_records)
        target_ids = tuple(target_ids)
        source_names = tuple(source_names)
        scoped = bool(target_ids or source_names)
        target_names = self._target_names(target_ids, source_names)
        candidates = []
        uncertain = 0
        for record in records:
            if not bool(getattr(record, "sidecar_exists", False)):
                continue
            tags = tuple(getattr(record, "tags", ()) or ())
            sources = dict(getattr(record, "tag_sources", {}) or {})
            if scoped and not any(
                normalize_tag(value) in target_names for value in tags
            ):
                continue
            confirmed = False
            for value in tags:
                key = normalize_tag(value)
                tag_sources = tuple(
                    sources.get(value, sources.get(key, ())) or ()
                )
                if "Confirmed Image Knowledge" in tag_sources:
                    confirmed = True
                    break
            if not confirmed:
                uncertain += 1
            candidates.append(Path(record.path))
        return ImageIdentityCorrectionDiscovery(
            len(records), tuple(dict.fromkeys(candidates)), uncertain,
        )

    @staticmethod
    def _list_locations(annotation: ImageAnnotation):
        for group, values in annotation.global_tags.items():
            yield f"Image / {group}", values
        for character in sorted(annotation.characters, key=lambda item: item.slot):
            for group, values in character.tags.items():
                yield f"Character {character.slot} / {group}", values
        for item in sorted(annotation.objects, key=lambda value: value.slot):
            yield f"Object {item.slot}", item.tags
        for group, values in annotation.scene.items():
            yield f"Scene / {group}", values
        for context in annotation.contexts:
            yield f"Context / {context.display_label}", context.tags
        for group, values in annotation.custom_groups.items():
            yield f"Custom / {group}", values
        yield "SDXL Tags", annotation.sdxl_tags
        yield "Uncategorized", annotation.uncategorized
        yield "Visual style", annotation.visual_style.style_tags

    def _resolve_value(self, value: str, stored_ids: dict[str, str]):
        key = normalize_tag(value)
        stored_id = str(stored_ids.get(key) or "").strip()
        if stored_id:
            resolved_id = self.knowledge.resolve_tag_id(stored_id)
            record = self.knowledge.tags.get(resolved_id or "")
            if record is None:
                return None, stored_id, (
                    f'"{value}" points to removed identity "{stored_id}" without '
                    "a current canonical survivor."
                )
            return record, stored_id, ""
        matches = self._names.get(key, ())
        if len(matches) == 1:
            return matches[0], "", ""
        if len(matches) > 1:
            return None, "", (
                f'"{value}" matches more than one current System Knowledge identity: '
                + ", ".join(f"{item.display} ({item.id})" for item in matches)
                + "."
            )
        tombstones=(
            self.knowledge.removed_identity_ids(value)
            if hasattr(self.knowledge,"removed_identity_ids") else
            tuple(filter(None,(self.knowledge.removed_identity_id(value),)))
        )
        if len(tombstones)>1:
            return None,"",(
                f'"{value}" matches more than one removed System Knowledge '
                "identity: "+", ".join(tombstones)+"."
            )
        tombstone=tombstones[0] if tombstones else None
        if tombstone:
            return None, tombstone, (
                f'"{value}" matches removed identity "{tombstone}" without a '
                "reviewed replacement."
            )
        return None, "", (
            f'"{value}" has no current reusable System Knowledge identity.'
        )

    def _value_in_scope(
        self,
        value: str,
        record,
        stored_id: str,
        *,
        requested_ids: set[str],
        resolved_ids: set[str],
        source_names: set[str],
    ) -> bool:
        if not requested_ids and not source_names:
            return True
        if stored_id and stored_id in requested_ids:
            return True
        if record is not None and record.id in resolved_ids:
            return True
        return normalize_tag(value) in source_names

    def _plan_annotation(
        self,
        annotation: ImageAnnotation,
        *,
        target_ids: Iterable[str] = (),
        source_names: Iterable[str] = (),
    ) -> tuple[
        ImageAnnotation,
        tuple[ImageIdentityCorrectionChange, ...],
        tuple[str, ...],
    ]:
        updated = copy.deepcopy(annotation)
        stored_ids = {
            normalize_tag(value): str(stable_id)
            for value, stable_id in annotation.tag_identities.items()
            if normalize_tag(value) and str(stable_id).strip()
        }
        requested_ids = {str(value).strip() for value in target_ids if str(value).strip()}
        resolved_ids = self._resolved_target_ids(requested_ids)
        source_keys = {
            normalize_tag(value)
            for value in source_names
            if normalize_tag(value)
        }
        changes = []
        warnings = []
        identity_updates: dict[str, tuple[str, str]] = {}
        display_updates: dict[str, tuple[str, str]] = {}

        def normalize_values(
            location: str,
            values: list[str],
            *,
            stored_override: dict[str, str] | None = None,
        ) -> list[str]:
            active_stored_ids=dict(stored_ids)
            active_stored_ids.update(stored_override or {})
            result = []
            resolved_results = []
            seen_ids = set()
            for value in values:
                record, stored_id, error = self._resolve_value(
                    value, active_stored_ids,
                )
                in_scope = self._value_in_scope(
                    value, record, stored_id,
                    requested_ids=requested_ids,
                    resolved_ids=resolved_ids,
                    source_names=source_keys,
                )
                if not in_scope:
                    result.append(value)
                    resolved_results.append("")
                    continue
                if error or record is None:
                    warnings.append(f"{location}: {error}")
                    result.append(value)
                    resolved_results.append("")
                    continue
                if (
                    requested_ids
                    and normalize_tag(value) in source_keys
                    and stored_id not in requested_ids
                    and record.id not in resolved_ids
                ):
                    warnings.append(
                        f'{location}: "{value}" is linked to {record.id}, which '
                        "does not match the identities in this prefilled "
                        "System Knowledge handoff."
                    )
                    result.append(value)
                    resolved_results.append("")
                    continue
                canonical = record.display
                old_key = normalize_tag(value)
                canonical_key = normalize_tag(canonical)
                resolved_id = record.id
                redirected = bool(stored_id and stored_id != resolved_id)
                missing_link = not stored_id
                duplicate = resolved_id in seen_ids
                if duplicate:
                    changes.append(ImageIdentityCorrectionChange(
                        location, value, canonical, resolved_id,
                        "same_identity_duplicate",
                    ))
                    identity_updates[old_key] = (canonical_key, resolved_id)
                    display_updates[old_key] = (canonical, resolved_id)
                    continue
                seen_ids.add(resolved_id)
                result.append(canonical)
                resolved_results.append(resolved_id)
                if old_key != canonical_key:
                    changes.append(ImageIdentityCorrectionChange(
                        location, value, canonical, resolved_id,
                        "canonical_spelling",
                    ))
                    display_updates[old_key] = (canonical, resolved_id)
                if redirected:
                    changes.append(ImageIdentityCorrectionChange(
                        "Stable identity", stored_id, resolved_id, resolved_id,
                        "stable_id_redirect",
                    ))
                elif missing_link:
                    changes.append(ImageIdentityCorrectionChange(
                        "Stable identity", "Not recorded", resolved_id, resolved_id,
                        "missing_identity_link",
                    ))
                if (
                    old_key != canonical_key
                    or redirected
                    or missing_link
                    or duplicate
                ):
                    identity_updates[old_key] = (canonical_key, resolved_id)
            values[:] = result
            return resolved_results

        for location, values in self._list_locations(updated):
            normalize_values(location, values)
        for item in sorted(updated.objects, key=lambda value: value.slot):
            if not item.identity_tag:
                continue
            old_key=normalize_tag(item.identity_tag)
            values = [item.identity_tag]
            resolved=normalize_values(
                f"Object {item.slot} identity", values,
                stored_override=(
                    {old_key:str(item.identity_tag_id)}
                    if old_key and item.identity_tag_id else None
                ),
            )
            item.identity_tag = values[0] if values else None
            if resolved and resolved[0]:
                item.identity_tag_id=resolved[0]

        if display_updates:
            excluded = TagCollection()
            for value in updated.excluded:
                replacement = display_updates.get(normalize_tag(value))
                excluded.add(replacement[0] if replacement else value)
            updated.excluded = excluded.to_list()

        confirmed_keys = {
            normalize_tag(value)
            for value in updated.all_structured_tags()
            if normalize_tag(value)
        }
        identity_map = dict(stored_ids)
        for old_key, (canonical_key, stable_id) in identity_updates.items():
            if old_key != canonical_key and old_key not in confirmed_keys:
                identity_map.pop(old_key, None)
            if canonical_key in confirmed_keys:
                identity_map[canonical_key] = stable_id
        updated.tag_identities = identity_map
        return (
            updated,
            tuple(dict.fromkeys(changes)),
            tuple(dict.fromkeys(warnings)),
        )

    @staticmethod
    def _writable(sidecar: Path) -> bool:
        try:
            mode = sidecar.stat().st_mode
        except OSError:
            return False
        return bool(mode & stat.S_IWRITE) and os.access(sidecar, os.W_OK)

    def preview(
        self,
        images: Iterable[str | Path],
        *,
        image_count: int | None = None,
        target_ids: Iterable[str] = (),
        source_names: Iterable[str] = (),
        progress: Callable[[int, int, str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> ImageIdentityCorrectionPreview:
        paths = tuple(dict.fromkeys(Path(value) for value in images))
        target_ids = tuple(dict.fromkeys(str(value) for value in target_ids if str(value)))
        source_names = tuple(dict.fromkeys(str(value) for value in source_names if str(value)))
        result = ImageIdentityCorrectionPreview(
            image_count if image_count is not None else len(paths),
            len(paths),
            target_ids=target_ids,
            source_names=source_names,
        )
        should_cancel = should_cancel or (lambda: False)
        for index, image in enumerate(paths, 1):
            if should_cancel():
                result.cancelled = True
                break
            sidecar = image.with_suffix(".tags.json")
            fingerprint = image_identity_fingerprint(image)
            if not image.is_file() or not sidecar.is_file():
                result.items.append(ImageIdentityCorrectionItem(
                    image, fingerprint, "unavailable",
                    message="The image or authoritative Image Knowledge sidecar is unavailable.",
                ))
            else:
                writable = self._writable(sidecar)
                try:
                    before_raw = sidecar.read_text(encoding="utf-8")
                    record = ImageRecord(image)
                    before = copy.deepcopy(record.annotation.to_dict())
                    updated, changes, warnings = self._plan_annotation(
                        record.annotation,
                        target_ids=target_ids,
                        source_names=source_names,
                    )
                    after = updated.to_dict()
                except Exception as exc:
                    result.items.append(ImageIdentityCorrectionItem(
                        image, fingerprint, "failed", message=str(exc),
                    ))
                else:
                    if changes and after != before and not writable:
                        result.items.append(ImageIdentityCorrectionItem(
                            image, fingerprint, "read_only", changes, warnings,
                            message=(
                                "The exact correction is known, but the authoritative "
                                "Image Knowledge sidecar is read-only."
                            ),
                            before=before, after=after,
                            before_raw=before_raw,
                            after_raw=(
                                json.dumps(after, indent=2, ensure_ascii=False)
                                + "\n"
                            ),
                        ))
                    elif changes and after != before:
                        after_raw = (
                            json.dumps(after, indent=2, ensure_ascii=False) + "\n"
                        )
                        result.items.append(ImageIdentityCorrectionItem(
                            image, fingerprint, "will_change", changes, warnings,
                            before=before, after=after,
                            before_raw=before_raw, after_raw=after_raw,
                        ))
                    elif warnings:
                        result.items.append(ImageIdentityCorrectionItem(
                            image, fingerprint, "unresolved",
                            warnings=warnings,
                            message="; ".join(warnings),
                        ))
                    else:
                        result.current_count += 1
            if progress is not None:
                progress(index, len(paths), image.name)
            result.checked_count = index
        return result

    @staticmethod
    def _snapshot(annotation: dict, raw_text: str) -> dict:
        return {"annotation": copy.deepcopy(annotation), "raw_text": raw_text}

    def _write_checkpoint(self, payload: dict) -> None:
        _atomic_text(
            self.checkpoint_path,
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        )

    def _clear_checkpoint(self) -> None:
        if self.checkpoint_path.exists():
            self.checkpoint_path.unlink()

    @staticmethod
    def _read_current(image: Path) -> tuple[dict, str]:
        sidecar = image.with_suffix(".tags.json")
        raw = sidecar.read_text(encoding="utf-8")
        return ImageRecord(image).annotation.to_dict(), raw

    @staticmethod
    def _restore_snapshot(image: Path, snapshot: dict) -> None:
        raw = str(snapshot.get("raw_text") or "")
        if not raw:
            raise RuntimeError(f"No exact Image Knowledge restore snapshot exists for {image.name}.")
        _atomic_text(image.with_suffix(".tags.json"), raw)
        current = ImageRecord(image).annotation.to_dict()
        if current != snapshot.get("annotation"):
            raise RuntimeError(f"{image.name} failed Image Knowledge restore verification.")

    def apply(
        self,
        preview: ImageIdentityCorrectionPreview,
        *,
        selected_images: Iterable[str | Path] = (),
        progress: Callable[[int, int, str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        history_operation=None,
    ) -> ImageIdentityCorrectionApplyResult:
        selected = {Path(value) for value in selected_images}
        items = [
            item for item in preview.items
            if item.status == "will_change"
            and (not selected or item.image in selected)
        ]
        result = ImageIdentityCorrectionApplyResult()
        if not items:
            return result
        should_cancel = should_cancel or (lambda: False)
        if history_operation is not None:
            return self._apply_with_history(items, history_operation, progress, should_cancel)
        operation_id = self.journal.begin(
            self.OPERATION_KIND,
            expected=len(items),
            reason="Normalize exact confirmed SDXL Tag identities",
            metadata={
                "target_ids": list(preview.target_ids),
                "source_names": list(preview.source_names),
                "writes": "image_knowledge_only",
            },
        )
        result.operation_id = operation_id
        checkpoint = {
            "schema_version": self.CHECKPOINT_SCHEMA,
            "operation_id": operation_id,
            "status": "applying",
            "entries": [],
        }
        try:
            for item in items:
                before_digest = self.journal.store_snapshot(
                    self._snapshot(item.before or {}, item.before_raw)
                )
                after_digest = self.journal.store_snapshot(
                    self._snapshot(item.after or {}, item.after_raw)
                )
                checkpoint["entries"].append({
                    "image": str(item.image),
                    "fingerprint": item.fingerprint,
                    "before": before_digest,
                    "after": after_digest,
                    "changes": [
                        {
                            "location": change.location,
                            "before": change.before,
                            "after": change.after,
                            "stable_id": change.stable_id,
                            "reason": change.reason,
                        }
                        for change in item.changes
                    ],
                    "status": "pending",
                })
            self._write_checkpoint(checkpoint)
        except Exception as exc:
            self.journal.finish(
                operation_id, "failed",
                message=f"The recovery checkpoint could not be prepared: {exc}",
            )
            raise

        terminal = "complete"
        for position, (item, entry) in enumerate(
            zip(items, checkpoint["entries"], strict=True)
        ):
            if should_cancel():
                result.cancelled = True
                terminal = "cancelled"
                for remaining in items[position:]:
                    result.items.append(ImageIdentityCorrectionApplyItem(
                        remaining.image, "not_processed",
                        error="Cancelled before this image was changed.",
                    ))
                break
            try:
                if image_identity_fingerprint(item.image) != item.fingerprint:
                    result.items.append(ImageIdentityCorrectionApplyItem(
                        item.image, "stale",
                        error="Image Knowledge changed after the preview.",
                    ))
                    entry["status"] = "stale"
                    self._write_checkpoint(checkpoint)
                    if progress is not None:
                        progress(position + 1, len(items), item.image.name)
                    continue
                before_snapshot = self.journal.load_snapshot(entry["before"])
                after_snapshot = self.journal.load_snapshot(entry["after"])
                current, current_raw = self._read_current(item.image)
                if (
                    current != before_snapshot["annotation"]
                    or current_raw != before_snapshot["raw_text"]
                ):
                    raise ImageIdentityCorrectionStaleError(
                        "Image Knowledge changed after the preview."
                    )
                entry["status"] = "writing"
                self._write_checkpoint(checkpoint)
                _atomic_text(
                    item.image.with_suffix(".tags.json"),
                    str(after_snapshot["raw_text"]),
                )
                verified, verified_raw = self._read_current(item.image)
                if (
                    verified != after_snapshot["annotation"]
                    or verified_raw != after_snapshot["raw_text"]
                ):
                    raise RuntimeError(
                        "Saved Image Knowledge did not match the reviewed correction."
                    )
                self.journal.append_change(
                    operation_id,
                    item=str(item.image),
                    before=before_snapshot,
                    after=after_snapshot,
                    reason="Normalize exact confirmed SDXL Tag identity",
                    metadata={
                        "changes": entry["changes"],
                        "writes": "image_knowledge_only",
                    },
                )
                entry["status"] = "journaled"
                self._write_checkpoint(checkpoint)
                result.items.append(ImageIdentityCorrectionApplyItem(
                    item.image, "changed", item.changes,
                ))
            except ImageIdentityCorrectionStaleError as exc:
                entry["status"] = "stale"
                self._write_checkpoint(checkpoint)
                result.items.append(ImageIdentityCorrectionApplyItem(
                    item.image, "stale", error=str(exc),
                ))
            except Exception as exc:
                recovery_error = ""
                try:
                    before_snapshot = self.journal.load_snapshot(entry["before"])
                    self._restore_snapshot(item.image, before_snapshot)
                    entry["status"] = "restored"
                except Exception as restore_exc:
                    recovery_error = f" Exact restore also failed: {restore_exc}"
                    entry["status"] = "recovery_required"
                    terminal = "failed"
                self._write_checkpoint(checkpoint)
                result.items.append(ImageIdentityCorrectionApplyItem(
                    item.image, "failed", error=str(exc) + recovery_error,
                ))
            if progress is not None:
                progress(position + 1, len(items), item.image.name)

        self.journal.finish(
            operation_id, terminal,
            message=(
                "One or more images require recovery."
                if terminal == "failed"
                else "The selected Image Knowledge corrections were processed."
            ),
        )
        checkpoint["status"] = terminal
        self._write_checkpoint(checkpoint)
        if not any(
            entry["status"] in {"writing", "recovery_required"}
            for entry in checkpoint["entries"]
        ):
            self._clear_checkpoint()
        self.journal.prune()
        return result

    def _apply_with_history(self, items, operation, progress, cancelled):
        """Capture the existing exact raw writer without its legacy rollback."""
        from .history_versions import HistoryError
        from .image_knowledge_history import HistoryWriteInterrupted

        result = ImageIdentityCorrectionApplyResult(history_operation_id=operation.operation_id)
        for position, item in enumerate(items):
            if cancelled():
                result.cancelled = True
                result.items.extend(ImageIdentityCorrectionApplyItem(rest.image, "not_processed",
                    error="Cancelled before this image was changed.") for rest in items[position:])
                break
            write_started = False
            try:
                context = operation.context_for(item.image)
                if context is None or context.operation_id != operation.operation_id:
                    raise HistoryError("Identity correction requires its exact admitted history operation.")
                with context.locked(item.image):
                    if context.store.has_pending(context.entity):
                        raise HistoryWriteInterrupted("Reopen this image for saved-history recovery before correcting its links.")
                    if image_identity_fingerprint(item.image) != item.fingerprint:
                        raise ImageIdentityCorrectionStaleError("Image Knowledge changed after the preview.")
                    current, raw = self._read_current(item.image)
                    if current != item.before or raw != item.before_raw:
                        raise ImageIdentityCorrectionStaleError("Image Knowledge changed after the preview.")
                    def write():
                        _atomic_text(item.image.with_suffix(".tags.json"), item.after_raw)
                        verified, verified_raw = self._read_current(item.image)
                        if verified != item.after or verified_raw != item.after_raw:
                            raise RuntimeError("Saved Image Knowledge did not match the reviewed correction.")
                    write_started = True
                    context.write(item.image, item.after_raw.encode("utf-8"), write)
                    head = context.store.current_head(context.entity)
                    if head is None or head.operation_id != operation.operation_id:
                        raise HistoryError("The exact saved correction history could not be verified.")
                    result.items.append(ImageIdentityCorrectionApplyItem(item.image, "changed", item.changes,
                        version_id=head.version_id))
            except ImageIdentityCorrectionStaleError as exc:
                result.items.append(ImageIdentityCorrectionApplyItem(item.image, "stale", error=str(exc)))
            except Exception as exc:
                interrupted = write_started or isinstance(exc, HistoryWriteInterrupted)
                result.history_interrupted = interrupted
                result.cancelled = isinstance(exc, InterruptedError)
                result.items.append(ImageIdentityCorrectionApplyItem(item.image,
                    "interrupted" if interrupted else "not_processed" if result.cancelled else "held", error=str(exc)))
                result.items.extend(ImageIdentityCorrectionApplyItem(rest.image, "not_processed",
                    error="Stopped after an image required recovery or renewed admission.") for rest in items[position + 1:])
                break
            if progress is not None:
                progress(position + 1, len(items), item.image.name)
        return result

    def recover_interrupted(self) -> ImageIdentityCorrectionRecovery | None:
        if not self.checkpoint_path.exists():
            return None
        payload = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        if int(payload.get("schema_version") or 0) != self.CHECKPOINT_SCHEMA:
            raise RuntimeError("The matching-image recovery checkpoint uses an unsupported version.")
        operation_id = str(payload.get("operation_id") or "")
        if not operation_id:
            raise RuntimeError("The matching-image recovery checkpoint has no operation identity.")
        operation = self._operation(operation_id)
        journaled_changes = {
            (
                str(change.get("item") or ""),
                str(change.get("before") or ""),
                str(change.get("after") or ""),
            )
            for change in operation.changes
        }
        applied = 0
        untouched = 0
        conflicts = []
        for entry in payload.get("entries", ()):
            image = Path(str(entry.get("image") or ""))
            before = self.journal.load_snapshot(str(entry.get("before") or ""))
            after = self.journal.load_snapshot(str(entry.get("after") or ""))
            try:
                current, current_raw = self._read_current(image)
            except Exception as exc:
                conflicts.append(f"{image.name}: {exc}")
                continue
            if (
                current == after.get("annotation")
                and current_raw == after.get("raw_text")
            ):
                journal_key = (
                    str(image),
                    str(entry.get("before") or ""),
                    str(entry.get("after") or ""),
                )
                if journal_key not in journaled_changes:
                    self.journal.append_change(
                        operation_id,
                        item=str(image),
                        before=before,
                        after=after,
                        reason="Recover verified matching-image correction",
                        metadata={
                            "changes": list(entry.get("changes") or ()),
                            "writes": "image_knowledge_only",
                            "recovered": True,
                        },
                    )
                    journaled_changes.add(journal_key)
                entry["status"] = "journaled"
                applied += 1
            elif (
                current == before.get("annotation")
                and current_raw == before.get("raw_text")
            ):
                entry["status"] = "not_applied"
                untouched += 1
            else:
                entry["status"] = "recovery_required"
                conflicts.append(
                    f"{image.name}: Image Knowledge differs from both recovery snapshots."
                )
        payload["status"] = "failed" if conflicts else "cancelled"
        self._write_checkpoint(payload)
        if operation.status == "applying":
            self.journal.finish(
                operation_id,
                "failed" if conflicts else "cancelled",
                message=(
                    "Manual recovery is required."
                    if conflicts
                    else "Recovered verified changes and left untouched images unchanged."
                ),
            )
        if not conflicts:
            self._clear_checkpoint()
        return ImageIdentityCorrectionRecovery(
            operation_id, applied, untouched, tuple(conflicts),
        )

    def latest_undoable(self) -> JournalOperation | None:
        return self.latest_undoable_in(self.journal)

    @classmethod
    def latest_undoable_in(cls,journal: OperationJournal) -> JournalOperation | None:
        """Inspect recovery history without preparing knowledge or reading images."""
        operations = [
            operation for operation in journal.operations()
            if operation.kind == cls.OPERATION_KIND
            and operation.status in {"complete", "cancelled", "failed"}
            and operation.changes
        ]
        return operations[-1] if operations else None

    def undo(
        self,
        operation_id: str,
        *,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> ImageIdentityCorrectionApplyResult:
        operation = self._operation(operation_id)
        if (
            operation.kind != self.OPERATION_KIND
            or operation.status not in {"complete", "cancelled", "failed"}
            or not operation.changes
        ):
            raise ValueError("That matching-image correction is not available to restore.")
        prepared = []
        for change in operation.changes:
            image = Path(change["item"])
            before = self.journal.load_snapshot(change["before"])
            after = self.journal.load_snapshot(change["after"])
            current, current_raw = self._read_current(image)
            if (
                current != after.get("annotation")
                or current_raw != after.get("raw_text")
            ):
                raise ImageIdentityCorrectionStaleError(
                    f"{image.name} changed after this correction, so restore stopped "
                    "without overwriting newer work."
                )
            prepared.append((image, before, after))
        undo_id = self.journal.begin(
            self.UNDO_KIND,
            expected=len(prepared),
            reason=f"Restore matching-image correction {operation_id}",
            metadata={"restores": operation_id, "writes": "image_knowledge_only"},
        )
        result = ImageIdentityCorrectionApplyResult(operation_id=undo_id)
        restored = []
        try:
            for index, (image, before, after) in enumerate(prepared, 1):
                restored.append((image, after))
                self._restore_snapshot(image, before)
                self.journal.append_change(
                    undo_id,
                    item=str(image),
                    before=after,
                    after=before,
                    reason="Restore exact Image Knowledge before identity correction",
                    metadata={"restores": operation_id, "writes": "image_knowledge_only"},
                )
                result.items.append(ImageIdentityCorrectionApplyItem(
                    image, "changed",
                ))
                if progress is not None:
                    progress(index, len(prepared), image.name)
        except Exception as exc:
            rollback_errors = []
            for image, after in reversed(restored):
                try:
                    self._restore_snapshot(image, after)
                except Exception as rollback_exc:
                    rollback_errors.append(f"{image.name}: {rollback_exc}")
            message = str(exc)
            if rollback_errors:
                message += "; restore rollback also failed: " + "; ".join(rollback_errors)
            self.journal.finish(undo_id, "failed", message=message)
            raise RuntimeError(message) from exc
        self.journal.finish(undo_id, "complete")
        self.journal.finish(operation_id, "undone")
        self.journal.prune()
        return result

    def _operation(self, operation_id: str) -> JournalOperation:
        operation = next(
            (
                item for item in self.journal.operations()
                if item.id == operation_id
            ),
            None,
        )
        if operation is None:
            raise ValueError("The matching-image operation is no longer available.")
        return operation
