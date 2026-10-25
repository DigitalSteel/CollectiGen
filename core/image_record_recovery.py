from __future__ import annotations

import hashlib
import json
import os
import stat
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .annotation import ImageAnnotation
from .image_record import ImageRecord, ImageRecordEditSnapshot, _atomic_write_text, _atomic_create_text
from .settings import normalize_plain_language_exclusion
from .tag_collection import normalize_tag


_MISSING = object()


def _copy_merge_value(value: object) -> object:
    # Deletions must retain sentinel identity so containers omit the value.
    # deepcopy(object()) creates a new object that can leak into Image Knowledge.
    return _MISSING if value is _MISSING else deepcopy(value)


class ImageRecordRecoveryError(RuntimeError):
    """The editor recovery could not proceed without risking newer data."""


def _file_fingerprint(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"exists": False}
    raw = path.read_bytes()
    stat = path.stat()
    return {
        "exists": True,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _same_content(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    if bool(left.get("exists")) != bool(right.get("exists")):
        return False
    if not left.get("exists"):
        return True
    return (
        left.get("size") == right.get("size")
        and left.get("sha256") == right.get("sha256")
    )


def _json_fingerprint(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _atomic_write_bytes(path: Path, value: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _path_key(path: tuple[str, ...]) -> str:
    encoded = json.dumps(path, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _path_label(path: tuple[str, ...]) -> str:
    if not path:
        return "Image Knowledge"
    labels = {
        "sdxl_tags": "Confirmed Tags",
        "uncategorized": "Confirmed Tags",
        "plain_language": "Descriptions",
        "confirmed_absent": "Confirmed Absent Tags",
        "export_excluded": "Training Exclusions",
        "tag_identities": "Tag Identity",
        "confirmed_tag": "Confirmed Tag",
        "global": "Global Tags",
        "scene": "Scene Tags",
        "custom_groups": "Custom Tags",
        "characters": "Character",
        "objects": "Object",
        "contexts": "Image Context",
        "source": "Review Evidence",
        "rejected_tags": "Not on This Image Tags",
        "rejected_descriptions": "Not on This Image Descriptions",
        "dismissed_findings": "Deferred Findings",
        "finding_decisions": "Finding Decisions",
        "ai_evidence": "AI Evidence",
        "generation_records": "Generator Evidence",
        "visual_style": "Visual Style",
        "style_tags": "Style Tags",
    }
    parts = []
    for value in path:
        if value.startswith("[") and value.endswith("]"):
            clean = value[1:-1]
        else:
            clean = labels.get(value, value.replace("_", " ").title())
        if not parts or parts[-1] != clean:
            parts.append(clean)
    return " › ".join(parts)


def recovery_value_text(value: object, *, exists: bool = True, limit: int = 500) -> str:
    if not exists:
        return "Removed"
    if isinstance(value, str):
        text = value or "Empty"
    elif value is None:
        text = "Empty"
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(text) > limit:
        return text[: max(1, limit - 1)] + "…"
    return text


@dataclass(frozen=True)
class ImageRecordRecoveryConflict:
    id: str
    path: tuple[str, ...]
    label: str
    base: object | None
    current: object | None
    local: object | None
    base_exists: bool
    current_exists: bool
    local_exists: bool

    @property
    def current_text(self) -> str:
        return recovery_value_text(self.current, exists=self.current_exists)

    @property
    def local_text(self) -> str:
        return recovery_value_text(self.local, exists=self.local_exists)


@dataclass(frozen=True)
class ImageRecordRecoveryPlan:
    image: Path
    base_annotation: dict[str, object]
    current_annotation: dict[str, object]
    local_annotation: dict[str, object]
    current_sidecar_fingerprint: dict[str, object]
    current_caption_fingerprint: dict[str, object]
    sidecar_changed: bool
    caption_changed: bool
    conflicts: tuple[ImageRecordRecoveryConflict, ...]

    def materialize(self, choices: Mapping[str, str] | None = None) -> dict[str, object]:
        selected = dict(choices or {})
        conflicts: list[ImageRecordRecoveryConflict] = []
        merged = _merge_value(
            self.base_annotation,
            self.current_annotation,
            self.local_annotation,
            (),
            selected,
            conflicts,
        )
        if merged is _MISSING or not isinstance(merged, dict):
            raise ImageRecordRecoveryError("The reconciled Image Knowledge record is invalid.")
        for conflict in _semantic_tag_conflicts(
            self.base_annotation,
            self.current_annotation,
            self.local_annotation,
        ):
            source = (
                self.local_annotation
                if selected.get(conflict.id) == "local"
                else self.current_annotation
            )
            key = conflict.path[-1][1:-1]
            _replace_tag_locations(merged, key, _tag_locations(source).get(key, ()))
        return merged


@dataclass(frozen=True)
class ImageRecordRecoveryResult:
    record: ImageRecord
    backup_directory: Path
    recovery_manifest: Path
    caption_changed: bool


def _conflict(
    base: object,
    current: object,
    local: object,
    path: tuple[str, ...],
    choices: Mapping[str, str],
    conflicts: list[ImageRecordRecoveryConflict],
) -> object:
    conflict_id = _path_key(path)
    conflicts.append(ImageRecordRecoveryConflict(
        id=conflict_id,
        path=path,
        label=_path_label(path),
        base=None if base is _MISSING else deepcopy(base),
        current=None if current is _MISSING else deepcopy(current),
        local=None if local is _MISSING else deepcopy(local),
        base_exists=base is not _MISSING,
        current_exists=current is not _MISSING,
        local_exists=local is not _MISSING,
    ))
    choice = choices.get(conflict_id, "current")
    return _copy_merge_value(local if choice == "local" else current)


def _entity_list(values: object, path: tuple[str, ...]) -> bool:
    if path and path[-1] in {
        "characters", "objects", "contexts", "ai_evidence", "generation_records",
        "resources", "stages",
    }:
        return True
    items = values if isinstance(values, list) else []
    return bool(items) and all(
        isinstance(item, dict) and str(item.get("id") or "").strip()
        for item in items
    )


def _tag_containers(annotation: dict[str, object]):
    for root in ("global", "scene", "custom_groups"):
        groups = annotation.get(root, {})
        if not isinstance(groups, dict):
            continue
        for group, values in groups.items():
            if isinstance(values, list):
                yield (root, str(group)), values
    for root in ("sdxl_tags", "uncategorized"):
        values = annotation.get(root, [])
        if isinstance(values, list):
            yield (root,), values
    style = annotation.get("visual_style", {})
    if isinstance(style, dict) and isinstance(style.get("style_tags"), list):
        yield ("visual_style", "style_tags"), style["style_tags"]
    for root in ("characters", "objects"):
        entities = annotation.get(root, [])
        if not isinstance(entities, list):
            continue
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            entity_id = str(entity.get("id") or "")
            if not entity_id:
                continue
            if root == "characters":
                groups = entity.get("tags", {})
                if not isinstance(groups, dict):
                    continue
                for group, values in groups.items():
                    if isinstance(values, list):
                        yield (root, f"[{entity_id}]", "tags", str(group)), values
            else:
                values = entity.get("tags", [])
                if isinstance(values, list):
                    yield (root, f"[{entity_id}]", "tags"), values
    contexts = annotation.get("contexts", [])
    if isinstance(contexts, list):
        for context in contexts:
            if not isinstance(context, dict):
                continue
            context_id = str(context.get("id") or "")
            values = context.get("tags", [])
            if context_id and isinstance(values, list):
                yield ("contexts", f"[{context_id}]", "tags"), values


def _tag_locations(annotation: dict[str, object]) -> dict[str, tuple[tuple[tuple[str, ...], str], ...]]:
    values: dict[str, list[tuple[tuple[str, ...], str]]] = {}
    for path, container in _tag_containers(annotation):
        for raw in container:
            if not isinstance(raw, str) or not (key := normalize_tag(raw)):
                continue
            values.setdefault(key, []).append((path, raw))
    return {
        key: tuple(sorted(items, key=lambda item: (item[0], item[1].casefold())))
        for key, items in values.items()
    }


def _location_text(values: tuple[tuple[tuple[str, ...], str], ...]) -> list[str]:
    return [f"{_path_label(path)}: {value}" for path, value in values]


def _semantic_tag_conflicts(
    base: dict[str, object],
    current: dict[str, object],
    local: dict[str, object],
) -> tuple[ImageRecordRecoveryConflict, ...]:
    base_values = _tag_locations(base)
    current_values = _tag_locations(current)
    local_values = _tag_locations(local)
    conflicts = []
    for key in sorted(set(base_values) | set(current_values) | set(local_values)):
        base_locations = base_values.get(key, ())
        current_locations = current_values.get(key, ())
        local_locations = local_values.get(key, ())
        base_paths = tuple(path for path, _value in base_locations)
        current_paths = tuple(path for path, _value in current_locations)
        local_paths = tuple(path for path, _value in local_locations)
        if (
            current_paths == base_paths
            or local_paths == base_paths
            or current_paths == local_paths
        ):
            continue
        display = (
            (local_locations or current_locations or base_locations)[0][1]
        )
        path = ("confirmed_tag", f"[{key}]")
        conflicts.append(ImageRecordRecoveryConflict(
            id=_path_key(path), path=path,
            label=f"Confirmed Tag › {display}",
            base=_location_text(base_locations) or None,
            current=_location_text(current_locations) or None,
            local=_location_text(local_locations) or None,
            base_exists=bool(base_locations),
            current_exists=bool(current_locations),
            local_exists=bool(local_locations),
        ))
    return tuple(conflicts)


def _container_at(annotation: dict[str, object], path: tuple[str, ...]) -> list[object] | None:
    value: object = annotation
    for token in path:
        if token.startswith("[") and token.endswith("]"):
            if not isinstance(value, list):
                return None
            entity_id = token[1:-1]
            value = next((
                item for item in value
                if isinstance(item, dict) and str(item.get("id") or "") == entity_id
            ), None)
        elif isinstance(value, dict):
            value = value.get(token)
        else:
            return None
    return value if isinstance(value, list) else None


def _replace_tag_locations(
    annotation: dict[str, object],
    key: str,
    locations: tuple[tuple[tuple[str, ...], str], ...],
) -> None:
    for _path, container in _tag_containers(annotation):
        container[:] = [
            value for value in container
            if not isinstance(value, str) or normalize_tag(value) != key
        ]
    for path, value in locations:
        container = _container_at(annotation, path)
        if container is not None and not any(
            isinstance(item, str) and normalize_tag(item) == key
            for item in container
        ):
            container.append(value)


def _list_identity(value: object, path: tuple[str, ...]) -> str:
    root = path[0] if path else ""
    leaf = path[-1] if path else ""
    tag_list = (
        root in {
            "sdxl_tags", "uncategorized", "confirmed_absent",
            "export_excluded", "global", "scene", "custom_groups", "contexts",
        }
        or leaf in {"tags", "style_tags", "rejected_tags"}
    )
    description_list = root == "plain_language" or leaf == "rejected_descriptions"
    if isinstance(value, str) and tag_list:
        return f"tag:{normalize_tag(value)}"
    if isinstance(value, str) and description_list:
        return f"description:{normalize_plain_language_exclusion(value)}"
    return "value:" + json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _merge_keyed_list(
    base: list[object],
    current: list[object],
    local: list[object],
    path: tuple[str, ...],
    choices: Mapping[str, str],
    conflicts: list[ImageRecordRecoveryConflict],
) -> list[object]:
    base_map = {_list_identity(value, path): value for value in base}
    current_map = {_list_identity(value, path): value for value in current}
    local_map = {_list_identity(value, path): value for value in local}
    order = [*current_map, *(key for key in local_map if key not in current_map)]
    result = []
    for key in order:
        merged = _merge_value(
            base_map.get(key, _MISSING),
            current_map.get(key, _MISSING),
            local_map.get(key, _MISSING),
            (*path, f"[{key.split(':', 1)[-1]}]"),
            choices,
            conflicts,
        )
        if merged is not _MISSING:
            result.append(merged)
    return result


def _overlapping_description_replacement(
    base: list[object], current: list[object], local: list[object],
) -> bool:
    """Identify two different rewrites of the same confirmed sentence."""

    def keyed(values: list[object]) -> set[str]:
        return {
            normalize_plain_language_exclusion(value)
            for value in values
            if isinstance(value, str)
            and normalize_plain_language_exclusion(value)
        }

    base_keys, current_keys, local_keys = keyed(base), keyed(current), keyed(local)
    removed_by_both = (base_keys - current_keys) & (base_keys - local_keys)
    current_additions = current_keys - base_keys
    local_additions = local_keys - base_keys
    return bool(
        removed_by_both
        and current_additions
        and local_additions
        and current_additions != local_additions
    )


def _merge_entity_values(
    base: list[object],
    current: list[object],
    local: list[object],
    path: tuple[str, ...],
    choices: Mapping[str, str],
    conflicts: list[ImageRecordRecoveryConflict],
) -> list[object]:
    def keyed(values: list[object]) -> dict[str, object]:
        return {
            str(item.get("id")): item
            for item in values
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        }

    base_map, current_map, local_map = keyed(base), keyed(current), keyed(local)
    order = [*current_map, *(key for key in local_map if key not in current_map)]
    result = []
    for key in order:
        merged = _merge_value(
            base_map.get(key, _MISSING),
            current_map.get(key, _MISSING),
            local_map.get(key, _MISSING),
            (*path, f"[{key}]"),
            choices,
            conflicts,
        )
        if merged is not _MISSING:
            result.append(merged)
    return result


def _merge_value(
    base: object,
    current: object,
    local: object,
    path: tuple[str, ...],
    choices: Mapping[str, str],
    conflicts: list[ImageRecordRecoveryConflict],
) -> object:
    if local == base:
        return _copy_merge_value(current)
    if current == base:
        return _copy_merge_value(local)
    if current == local:
        return _copy_merge_value(current)
    if all(value is not _MISSING and isinstance(value, dict) for value in (base, current, local)):
        result: dict[str, object] = {}
        keys = [
            *current,
            *(key for key in local if key not in current),
            *(key for key in base if key not in current and key not in local),
        ]
        for key in keys:
            merged = _merge_value(
                base.get(key, _MISSING),
                current.get(key, _MISSING),
                local.get(key, _MISSING),
                (*path, str(key)),
                choices,
                conflicts,
            )
            if merged is not _MISSING:
                result[str(key)] = merged
        return result
    if all(value is not _MISSING and isinstance(value, list) for value in (base, current, local)):
        if path == ("plain_language",) and _overlapping_description_replacement(
            base, current, local,
        ):
            return _conflict(base, current, local, path, choices, conflicts)
        if any(_entity_list(value, path) for value in (base, current, local)):
            return _merge_entity_values(base, current, local, path, choices, conflicts)
        return _merge_keyed_list(base, current, local, path, choices, conflicts)
    return _conflict(base, current, local, path, choices, conflicts)


class ImageRecordRecoveryService:
    """Reconcile one stale editor record without using last-writer-wins."""

    def __init__(self, *, backup_root: str | Path | None = None, history_context=None) -> None:
        self.backup_root = Path(backup_root) if backup_root is not None else None
        self.history_context = history_context
        if history_context is not None and self.backup_root is not None:
            from .history_versions import HistoryError, _reject_reparse
            _reject_reparse(self.backup_root.absolute())
            if not self.backup_root.resolve().is_relative_to(history_context.store.scope.zone_root):
                raise HistoryError("History recovery backups must remain in their authorized zone.")

    def plan(
        self,
        snapshot: ImageRecordEditSnapshot,
        edited: ImageRecord,
    ) -> ImageRecordRecoveryPlan:
        image = edited.image_path
        sidecar = edited.sidecar_path
        current_sidecar_fingerprint = _file_fingerprint(sidecar)
        if snapshot.sidecar_fingerprint.get("exists") and not sidecar.exists():
            raise ImageRecordRecoveryError(
                "The current Image Knowledge record is missing. Your pending edits remain open; restore or recreate the record before retrying."
            )
        if sidecar.exists():
            try:
                raw = json.loads(sidecar.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("the JSON root is not an object")
                current = ImageAnnotation.from_dict(raw, image.name).to_dict()
            except Exception as exc:
                raise ImageRecordRecoveryError(
                    f"The newest Image Knowledge record is invalid and was not replaced: {exc}"
                ) from exc
        else:
            current = deepcopy(snapshot.annotation)
        base = deepcopy(snapshot.annotation)
        local = deepcopy(edited.annotation.to_dict())
        conflicts: list[ImageRecordRecoveryConflict] = []
        _merge_value(base, current, local, (), {}, conflicts)
        known_conflicts = {item.id for item in conflicts}
        conflicts.extend(
            item for item in _semantic_tag_conflicts(base, current, local)
            if item.id not in known_conflicts
        )
        current_caption_fingerprint = _file_fingerprint(edited.caption_path)
        return ImageRecordRecoveryPlan(
            image=image,
            base_annotation=base,
            current_annotation=current,
            local_annotation=local,
            current_sidecar_fingerprint=current_sidecar_fingerprint,
            current_caption_fingerprint=current_caption_fingerprint,
            sidecar_changed=(
                current_sidecar_fingerprint != snapshot.sidecar_fingerprint
            ),
            caption_changed=(
                current_caption_fingerprint != snapshot.caption_fingerprint
            ),
            conflicts=tuple(conflicts),
        )

    def apply(
        self,
        plan: ImageRecordRecoveryPlan,
        *,
        choices: Mapping[str, str] | None = None,
        knowledge=None,
    ) -> ImageRecordRecoveryResult:
        if self.history_context is not None:
            with self.history_context.locked(plan.image):
                return self._apply(plan, choices=choices, knowledge=knowledge)
        return self._apply(plan, choices=choices, knowledge=knowledge)

    def _apply(self, plan, *, choices=None, knowledge=None) -> ImageRecordRecoveryResult:
        selected = dict(choices or {})
        missing = [item.id for item in plan.conflicts if selected.get(item.id) not in {"current", "local"}]
        if missing:
            raise ImageRecordRecoveryError(
                "Choose Use My Change or Keep Current for every displayed conflict."
            )
        if _file_fingerprint(plan.image.with_suffix(".tags.json")) != plan.current_sidecar_fingerprint:
            raise ImageRecordRecoveryError(
                "Image Knowledge changed again while the recovery was open. Nothing was overwritten; review the newest changes and try again."
            )
        merged = plan.materialize(selected)
        annotation = ImageAnnotation.from_dict(merged, plan.image.name)
        candidate = ImageRecord(plan.image, auto_load=False, known_tags=knowledge)
        candidate.annotation = annotation
        candidate.loaded = True
        candidate.modified = True
        try:
            candidate._assert_storage_write_ready()
        except OSError as exc:
            raise ImageRecordRecoveryError(str(exc)) from exc
        if (
            candidate.sidecar_path.exists()
            and not candidate.sidecar_path.stat().st_mode & (
                stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
            )
        ):
            raise ImageRecordRecoveryError(
                "The newest Image Knowledge record is read-only. Your pending edits remain open; restore write access before retrying."
            )
        if knowledge is not None:
            from .confirmed_tag_identity import ConfirmedTagIdentityService
            ConfirmedTagIdentityService(knowledge).synchronize_annotation(annotation)
        intended = annotation.to_dict()
        intended_text = json.dumps(intended, indent=2, ensure_ascii=False) + "\n"
        try:
            directory, manifest = self._create_backup(plan, intended)
        except ImageRecordRecoveryError:
            raise
        except Exception as exc:
            raise ImageRecordRecoveryError(
                f"The newest Image Knowledge backup could not be created and verified: {exc}"
            ) from exc
        if _file_fingerprint(candidate.sidecar_path) != plan.current_sidecar_fingerprint:
            try:
                self._set_manifest_status(manifest, "abandoned_after_concurrent_change")
            except Exception as exc:
                raise ImageRecordRecoveryError(
                    "Image Knowledge changed again before saving. Nothing was "
                    "overwritten, but the unused recovery journal could not be "
                    f"closed: {exc}"
                ) from exc
            raise ImageRecordRecoveryError(
                "Image Knowledge changed again before saving. Nothing was overwritten; "
                "review the newest changes and try again."
            )
        def persist():
            if _file_fingerprint(candidate.sidecar_path) != plan.current_sidecar_fingerprint:
                raise ImageRecordRecoveryError("Image Knowledge changed during history capture; nothing was overwritten.")
            try:
                _atomic_write_text(candidate.sidecar_path, intended_text)
                raw = json.loads(candidate.sidecar_path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict) or _json_fingerprint(raw) != _json_fingerprint(intended):
                    raise RuntimeError("Image Knowledge failed semantic read-back verification.")
                self._set_manifest_status(manifest, "complete")
            except Exception as exc:
                try:
                    self._restore_manifest(manifest, status="restored_after_failure")
                except Exception as recovery_exc:
                    raise ImageRecordRecoveryError(
                        f"The recovery save failed and automatic rollback also stopped safely: {exc}; {recovery_exc}"
                    ) from exc
                raise ImageRecordRecoveryError(
                    f"The recovery save failed and the newest Image Knowledge record was restored: {exc}"
                ) from exc
            return True

        # Commit history outside the specialized rollback handler: a journal
        # failure after verified storage must leave that exact write recoverable.
        if self.history_context is not None:
            changed = self.history_context.write(plan.image, intended_text.encode("utf-8"), persist)
            if changed is False:
                self._set_manifest_status(manifest, "unchanged")
        else:
            persist()
        return ImageRecordRecoveryResult(
            record=ImageRecord(plan.image, known_tags=knowledge, history_context=self.history_context),
            backup_directory=directory,
            recovery_manifest=manifest,
            caption_changed=plan.caption_changed,
        )

    def recover_pending(self, image: str | Path) -> tuple[Path, ...]:
        if self.history_context is not None:
            with self.history_context.locked(Path(image)):
                recovered = self._recover_pending(image)
                self.history_context.store.reconcile(self.history_context.entity)
                return recovered
        return self._recover_pending(image)

    def _recover_pending(self, image: str | Path) -> tuple[Path, ...]:
        image = Path(image)
        root = self._root(image)
        if not root.exists():
            return ()
        recovered = []
        # Recovery directories are named for the image stem. Limit inspection
        # before parsing so a damaged transaction for an unrelated image cannot
        # block this editor from opening. The manifest's resolved image path is
        # still the final authority because two image formats may share a stem.
        for manifest in sorted(root.glob(f"{image.stem}_*/recovery.json")):
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
            except Exception as exc:
                raise ImageRecordRecoveryError(
                    f"An interrupted Image Knowledge recovery manifest is invalid: {manifest.name}: {exc}"
                ) from exc
            if payload.get("status") != "pending":
                continue
            if Path(str(payload.get("image") or "")).resolve() != image.resolve():
                continue
            if payload.get("transaction_kind") == "saved-record-restore":
                self._recover_saved_manifest(manifest, payload, image)
                recovered.append(manifest)
                continue
            target = self._validated_target(manifest, payload, image)
            intended_fingerprint = str(payload.get("intended_fingerprint") or "")
            if target.exists():
                try:
                    raw = json.loads(target.read_text(encoding="utf-8"))
                except Exception:
                    raw = None
                if isinstance(raw, dict) and _json_fingerprint(raw) == intended_fingerprint:
                    self._set_manifest_status(manifest, "complete_after_interruption")
                    recovered.append(manifest)
                    continue
            self._restore_manifest(manifest, status="restored_after_interruption")
            recovered.append(manifest)
        return tuple(recovered)

    @staticmethod
    def _rename_saved_no_replace(source: Path, target: Path) -> None:
        """Atomic no-clobber quarantine; never emulate removal with blind unlink."""
        if os.name == "nt":
            os.rename(source, target)
            return
        import ctypes
        import errno
        import sys
        if not sys.platform.startswith("linux"):
            raise ImageRecordRecoveryError("Atomic no-clobber quarantine is unavailable on this platform.")
        rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
        if rename is None:
            raise ImageRecordRecoveryError("Atomic no-clobber quarantine is unavailable on this platform.")
        if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1) != 0:
            code = ctypes.get_errno()
            if code == errno.EEXIST:
                raise FileExistsError(str(target))
            raise OSError(code, os.strerror(code), str(target))

    def restore_saved_record(self, image: str | Path, *, expected: bytes | None, intended: bytes | None) -> Path | None:
        """Restore a complete admitted saved record (or absence), never a patch.

        The history service validates domain meaning. This canonical writer also
        requires the exact target snapshot and protects current raw bytes.
        """
        from .history_versions import HistoryError, read_record, record_token, _reject_reparse
        from .annotation import SCHEMA_VERSION
        context = self.history_context
        if context is None or not context.restored_from:
            raise HistoryError("Saved-record restoration requires exact scoped history authority.")
        image = Path(image)
        with context.locked(image):
            version = context.store.version(context.entity, context.restored_from)
            if context.store.payload(context.entity, version.snapshot) != intended:
                raise HistoryError("Intended record is not the exact selected historical version.")
            if intended is not None:
                value = json.loads(intended)
                if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION or ImageAnnotation.from_dict(value, image.name).to_dict() != value:
                    raise HistoryError("Whole-record restoration cannot coerce unsupported saved facts.")
            target = image.with_suffix(".tags.json")
            if read_record(target) != expected:
                raise HistoryError("Current Image Knowledge changed before whole-record restoration.")
            manifest_result = None

            def persist(intent):
                nonlocal manifest_result
                directory = self._root(image) / f"{image.stem}_{uuid4().hex}"
                directory.mkdir(parents=True, exist_ok=False)
                backup = directory / "before.bin"
                if expected is not None:
                    _atomic_write_bytes(backup, expected)
                    if read_record(backup) != expected:
                        raise HistoryError("Saved-record backup failed exact verification.")
                quarantine = image.with_name(f".{image.name}.{uuid4().hex}.history-quarantine")
                _reject_reparse(quarantine)
                manifest = directory / "recovery.json"
                manifest_result = manifest
                payload = {"schema_version": 2, "transaction_kind": "saved-record-restore", "status": "pending",
                           "image": str(image.resolve()), "target": str(target.resolve()), "entity_id": context.entity.entity_id,
                           "source_version": version.version_id, "source_snapshot": version.snapshot,
                           "before_token": record_token(expected), "after_token": record_token(intended),
                           "backup": backup.name, "quarantine": str(quarantine.resolve())}
                _atomic_write_text(manifest, json.dumps(payload, indent=2) + "\n")
                context.store.attach_recovery_receipt(intent, manifest)
                if read_record(target) != expected:
                    raise HistoryError("Image Knowledge changed after backup; restoration held.")
                if intended is None:
                    if expected is not None:
                        self._rename_saved_no_replace(target, quarantine)
                        if read_record(quarantine) != expected:
                            # Restore a competing file only into an empty name;
                            # retain both files if another writer appeared again.
                            try:
                                self._rename_saved_no_replace(quarantine, target)
                            except FileExistsError:
                                pass
                            raise HistoryError("Quarantined bytes changed; originals retained for exact recovery.")
                elif expected is None:
                    _atomic_create_text(target, intended.decode("utf-8"))
                else:
                    _atomic_write_bytes(target, intended)
                if read_record(target) != intended:
                    raise HistoryError("Saved-record read-back differs; preserve current and recover explicitly.")
                self._set_manifest_status(manifest, "complete")
                return True

            context.write(image, intended, persist, with_intent=True)
            return manifest_result

    def _recover_saved_manifest(self, manifest, payload, image):
        from .history_versions import HistoryError, read_record, record_token, _reject_reparse
        context = self.history_context
        if context is None or payload.get("schema_version") != 2 or payload.get("entity_id") != context.entity.entity_id:
            raise HistoryError("Saved-record recovery lacks exact scope/entity authority.")
        target = context.store.validate_entity(context.entity).with_suffix(".tags.json")
        if target != Path(payload.get("target", "")).resolve() or target != image.with_suffix(".tags.json").resolve():
            raise HistoryError("Saved-record recovery target is outside its entity.")
        backup = manifest.parent / str(payload.get("backup", ""))
        quarantine = Path(payload.get("quarantine", ""))
        for path in (backup, quarantine):
            _reject_reparse(path.absolute())
        if backup.resolve().parent != manifest.parent.resolve() or quarantine.parent != image.parent or not quarantine.name.startswith(f".{image.name}.") or not quarantine.name.endswith(".history-quarantine"):
            raise HistoryError("Saved-record recovery paths are outside their exact boundaries.")
        before = read_record(backup) if payload.get("before_token") != "missing" else None
        if record_token(before) != payload.get("before_token"):
            raise HistoryError("Saved-record recovery backup integrity failed.")
        version = context.store.version(context.entity, payload.get("source_version", ""))
        if version.snapshot != payload.get("source_snapshot"):
            raise HistoryError("Saved-record recovery target history changed.")
        intended = context.store.payload(context.entity, version.snapshot)
        if record_token(intended) != payload.get("after_token"):
            raise HistoryError("Saved-record recovery intention integrity failed.")
        current = read_record(target)
        if current == intended:
            if intended is None and before is not None and read_record(quarantine) != before:
                raise HistoryError("Absence recovery requires the exact retained quarantine.")
            self._set_manifest_status(manifest, "complete_after_interruption")
        elif current == before and not quarantine.exists():
            self._set_manifest_status(manifest, "aborted_before_write")
        else:
            raise HistoryError("Saved-record recovery diverged; current and protected copies were preserved.")

    def _root(self, image: Path) -> Path:
        root = self.backup_root or image.parent / ".tagmanager-backups" / "image-record-recovery"
        if self.history_context is not None:
            from .history_versions import HistoryError, _reject_reparse
            _reject_reparse(root.absolute())
            if not root.resolve().is_relative_to(self.history_context.store.scope.zone_root):
                raise HistoryError("History recovery backups escaped their authorized zone.")
        return root

    def _create_backup(
        self,
        plan: ImageRecordRecoveryPlan,
        intended: dict[str, object],
    ) -> tuple[Path, Path]:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        directory = self._root(plan.image) / f"{plan.image.stem}_{stamp}_{uuid4().hex[:8]}"
        directory.mkdir(parents=True, exist_ok=False)
        target = plan.image.with_suffix(".tags.json")
        existed = target.exists()
        backup = directory / f"0_{target.name}"
        if existed:
            _atomic_write_bytes(backup, target.read_bytes())
            if not _same_content(_file_fingerprint(backup), plan.current_sidecar_fingerprint):
                raise ImageRecordRecoveryError("The newest Image Knowledge backup failed verification.")
        manifest = directory / "recovery.json"
        payload = {
            "schema_version": 1,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "image": str(plan.image.resolve()),
            "target": str(target.resolve()),
            "backup": backup.name,
            "existed": existed,
            "before_fingerprint": plan.current_sidecar_fingerprint,
            "intended_fingerprint": _json_fingerprint(intended),
            "caption_changed": plan.caption_changed,
        }
        _atomic_write_text(
            manifest,
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        )
        return directory, manifest

    @staticmethod
    def _validated_target(manifest: Path, payload: dict[str, object], image: Path) -> Path:
        target = Path(str(payload.get("target") or "")).resolve()
        expected = image.with_suffix(".tags.json").resolve()
        backup = (manifest.parent / str(payload.get("backup") or "")).resolve()
        if target != expected:
            raise ImageRecordRecoveryError("Image Knowledge recovery contains an unsafe target path.")
        if backup.parent != manifest.parent.resolve():
            raise ImageRecordRecoveryError("Image Knowledge recovery contains an unsafe backup path.")
        return target

    def _restore_manifest(self, manifest: Path, *, status: str) -> None:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        image = Path(str(payload.get("image") or ""))
        target = self._validated_target(manifest, payload, image)
        current = _file_fingerprint(target)
        before = payload.get("before_fingerprint", {})
        before = before if isinstance(before, dict) else {}
        intended_fingerprint = str(payload.get("intended_fingerprint") or "")
        current_is_intended = False
        if target.exists():
            try:
                raw = json.loads(target.read_text(encoding="utf-8"))
            except Exception:
                raw = None
            current_is_intended = (
                isinstance(raw, dict)
                and _json_fingerprint(raw) == intended_fingerprint
            )
        if not (_same_content(current, before) or current_is_intended):
            raise ImageRecordRecoveryError(
                "Image Knowledge changed again after the interrupted write; automatic rollback did not overwrite it."
            )
        if payload.get("existed"):
            backup = (manifest.parent / str(payload.get("backup") or "")).resolve()
            if not backup.is_file():
                raise ImageRecordRecoveryError("The verified Image Knowledge recovery copy is missing.")
            _atomic_write_bytes(target, backup.read_bytes())
        elif target.exists():
            target.unlink()
        if not _same_content(_file_fingerprint(target), before):
            raise ImageRecordRecoveryError("Image Knowledge rollback failed exact content verification.")
        self._set_manifest_status(manifest, status)

    @staticmethod
    def _set_manifest_status(manifest: Path, status: str) -> None:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["status"] = status
        payload["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _atomic_write_text(
            manifest,
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        )
