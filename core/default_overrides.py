from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from .automation_rule_conversion import RulePairAdmission
from .default_sync import load_effective_tag_defaults, sync_promoted_defaults


@dataclass(frozen=True)
class DefaultOverrideItem:
    key: str
    kind: str
    kind_label: str
    stable_id: str
    label: str
    field: str
    field_label: str
    current_value: str
    default_value: str
    disabled: bool = False
    local_id: str = ""
    difference_kind: str = "different"
    difference_label: str = "Different values"
    difference_summary: str = "The current working value and shipped value differ."
    current_detail: str = ""
    default_detail: str = ""

    @property
    def can_reset_setting(self) -> bool:
        """Return whether this row has a concrete shipped value to restore."""
        return self.disabled or self.difference_kind != "current_only"


@dataclass(frozen=True)
class DefaultOverrideSnapshot:
    items: tuple[DefaultOverrideItem, ...]
    authority_fingerprint: str

    @property
    def reset_items(self) -> tuple[DefaultOverrideItem, ...]:
        """Meaningful ordinary reset candidates, excluding current-only history."""
        return tuple(item for item in self.items if item.can_reset_setting)

    @property
    def audit_items(self) -> tuple[DefaultOverrideItem, ...]:
        """Preserved differences that remain visible only through advanced audit."""
        return tuple(item for item in self.items if not item.can_reset_setting)


@dataclass(frozen=True)
class DefaultOverrideRestoreResult:
    restored_fields: int
    restored_records: int
    backup: str


@dataclass(frozen=True)
class _RecordSpec:
    kind: str
    label: str
    default_files: tuple[str, ...]
    user_file: str
    key: str
    state_key: str
    disabled_key: str
    materialized: bool
    local_only_fields: frozenset[str] = frozenset()


_SPECS = (
    _RecordSpec(
        "tag", "Tag",
        ("foundation_tags.json", "community_tags.json", "promoted_tags.json"),
        "tags.json", "tags", "tags", "", True,
        frozenset({"seen_count", "first_seen", "last_seen", "provenance"}),
    ),
    _RecordSpec(
        "merge_rule", "Knowledge rule", ("promoted_tags.json",),
        "tags.json", "merge_rules", "merge_rules",
        "disabled_default_merge_rule_ids", True,
    ),
    _RecordSpec(
        "category", "Category", ("categories.json",),
        "categories.json", "categories", "categories", "disabled_default_ids", False,
    ),
    _RecordSpec(
        "pattern", "Reusable pattern", ("patterns.json",),
        "patterns.json", "patterns", "patterns", "disabled_default_ids", False,
    ),
    _RecordSpec(
        "series", "Character Source", ("series.json",),
        "series.json", "series", "series", "disabled_default_ids", True,
    ),
    _RecordSpec(
        "character", "Character", ("characters.json",),
        "characters.json", "characters", "characters", "disabled_default_ids", True,
        frozenset({"notes"}),
    ),
    _RecordSpec(
        "outfit", "Outfit", ("outfits.json",),
        "outfits.json", "outfits", "outfits", "disabled_default_ids", True,
        frozenset({"notes"}),
    ),
)
_SPEC_BY_KIND = {item.kind: item for item in _SPECS}
_SAFE_USER_FILES = {
    "tags.json", "categories.json", "patterns.json", "series.json",
    "characters.json", "outfits.json", "tag_appearance.json",
    "default_knowledge_state.json",
}
_FIELD_LABELS = {
    "display": "Name", "name": "Name", "aliases": "Other Names",
    "category": "Category", "parent_id": "Parent Category",
    "description": "Description", "notes": "Notes", "regex": "Pattern",
    "enabled": "Enabled", "auto_apply": "Automatic Use",
    "confidence": "Confidence", "examples": "Examples",
    "concept_id": "Reusable Subject", "properties": "Properties",
    "relations": "Legacy Relationships", "content_sensitivity": "Content Rating",
    "ordering": "Training Order", "series_id": "Character Source",
    "character_ids": "Associated Characters", "sdxl_tags": "Tags",
    "plain_language": "Descriptions", "original_style_tags": "Original Style Tags",
    "style_tags": "Style Tags", "tag_associations": "Tag Associations",
    "base_character_id": "Broader Character", "variant_type": "Version Type",
    "knowledge_tag": "Representative Tag", "source_tags": "Source Tags",
    "target_tag": "Result Tag", "scope": "Scope", "require_review": "Review Policy",
    "color": "Color", "color_policy": "Color Policy",
    "base_tag_id": "Base Tag", "base_tag_policy": "Base Tag Policy",
    "sensitivity_rating": "Content Rating", "sensitivity_policy": "Rating Policy",
}

_REFERENCE_KINDS = {
    "category": "category",
    "parent_id": "category",
    "base_tag_id": "tag",
    "series_id": "series",
    "base_character_id": "character",
    "character_ids": "character",
}


def _read_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object.")
    return value


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_bytes(
        path,
        (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
    )


def _file_fingerprint(path: Path) -> str:
    if not path.exists():
        return "missing"
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _paths_fingerprint(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256(b"collectigen-default-overrides-v1\0")
    for path in sorted({Path(value) for value in paths}, key=lambda value: str(value).casefold()):
        digest.update(str(path.resolve()).casefold().encode("utf-8", errors="surrogatepass"))
        digest.update(b"\0")
        digest.update(_file_fingerprint(path).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _detail(value: Any) -> str:
    if value is _MISSING or value in (None, ""):
        return "Not set"
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def _resolved_label(
    value: Any,
    kind: str,
    labels: dict[str, dict[str, str]],
) -> str:
    stable_id = str(value or "")
    label = labels.get(kind, {}).get(stable_id, stable_id)
    if label and label != stable_id:
        return f"{label} ({stable_id})"
    return label


def _preview(
    value: Any,
    *,
    field: str = "",
    labels: dict[str, dict[str, str]] | None = None,
) -> str:
    labels = labels or {}
    if value is _MISSING:
        return "Not set"
    if value in (None, ""):
        return "Not set"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if field == "knowledge_tag" and isinstance(value, dict):
        tag = _resolved_label(value.get("tag_id"), "tag", labels) or "unknown Tag"
        status = str(value.get("status") or "").replace("_", " ").strip()
        basis = str(value.get("basis") or "").replace("_", " ").strip()
        parts = [f"{status.title()} representative Tag: {tag}" if status else f"Representative Tag: {tag}"]
        if basis:
            parts.append(f"{basis} match")
        return " (".join(parts) + ("" if len(parts) == 1 else ")")
    if field == "relationship_review" and isinstance(value, dict):
        qualifiers = [str(item).replace("_", " ") for item in value.get("qualifiers", ())]
        source = str(value.get("source") or "").replace("_", " ").strip()
        parts = ["Reviewed relationship"]
        if qualifiers:
            parts.append(", ".join(qualifiers))
        if source:
            parts.append(f"source: {source}")
        return " — ".join(parts)
    reference_kind = _REFERENCE_KINDS.get(field)
    if reference_kind and isinstance(value, list):
        resolved = [_resolved_label(item, reference_kind, labels) for item in value]
        text = ", ".join(item for item in resolved if item)
        return text if len(text) <= 180 else text[:177] + "..."
    if reference_kind and not isinstance(value, (dict, list)):
        return _resolved_label(value, reference_kind, labels) or "Not set"
    if isinstance(value, list):
        if not value:
            return "None"
        if all(not isinstance(item, (dict, list)) for item in value):
            text = ", ".join(str(item) for item in value)
            return text if len(text) <= 180 else text[:177] + "..."
        return f"Structured list ({len(value):,} items)"
    if isinstance(value, dict):
        if not value:
            return "None"
        return f"Structured details ({len(value):,} settings)"
    text = str(value)
    return text if len(text) <= 180 else text[:177] + "..."


_MISSING = object()


def _record_label(record: dict[str, Any], stable_id: str) -> str:
    return str(
        record.get("display")
        or record.get("name")
        or record.get("target_tag")
        or stable_id
    )


class DefaultOverrideService:
    """Inspect and clear local fields layered over current shipped defaults."""

    def __init__(self, app_root: str | Path) -> None:
        self.root = Path(app_root).absolute()
        self.defaults = self.root / "data" / "defaults"
        self.user = self.root / "data" / "user"
        self.state_path = self.user / "default_knowledge_state.json"
        self.rule_pair_admission = RulePairAdmission(self.user)

    def _authority_paths(self) -> tuple[Path, ...]:
        paths = {self.state_path, self.user / "tag_appearance.json"}
        for spec in _SPECS:
            paths.add(self.user / spec.user_file)
            paths.update(self.defaults / name for name in spec.default_files)
        return tuple(sorted(paths, key=lambda value: str(value).casefold()))

    def _default_records(self, spec: _RecordSpec) -> list[dict[str, Any]]:
        if spec.kind == "tag":
            return [json.loads(json.dumps(item)) for item in load_effective_tag_defaults(self.defaults).tags]
        records: list[dict[str, Any]] = []
        for name in spec.default_files:
            records.extend(
                json.loads(json.dumps(item))
                for item in _read_payload(self.defaults / name).get(spec.key, ())
                if isinstance(item, dict)
            )
        return records

    def _state_record(
        self, state: dict[str, Any], spec: _RecordSpec, stable_id: str
    ) -> dict[str, Any]:
        records = state.get("records") if isinstance(state.get("records"), dict) else {}
        kind_records = records.get(spec.state_key) if isinstance(records.get(spec.state_key), dict) else {}
        value = kind_records.get(stable_id)
        return value if isinstance(value, dict) else {}

    def scan(self, *, refresh: bool = True) -> DefaultOverrideSnapshot:
        with self.rule_pair_admission.admit():
            return self._scan_admitted(refresh=refresh)

    def _scan_admitted(self, *, refresh: bool = True) -> DefaultOverrideSnapshot:
        if refresh:
            sync_promoted_defaults(self.defaults, self.user)
        state = _read_payload(self.state_path)
        items: list[DefaultOverrideItem] = []
        defaults_by_kind = {spec.kind: self._default_records(spec) for spec in _SPECS}
        local_payloads = {
            spec.kind: _read_payload(self.user / spec.user_file) for spec in _SPECS
        }
        labels: dict[str, dict[str, str]] = {}
        for spec in _SPECS:
            records = [
                *defaults_by_kind[spec.kind],
                *[
                    item for item in local_payloads[spec.kind].get(spec.key, ())
                    if isinstance(item, dict)
                ],
            ]
            labels[spec.kind] = {
                str(record.get("id") or ""): _record_label(
                    record, str(record.get("id") or "")
                )
                for record in records if str(record.get("id") or "")
            }
        for spec in _SPECS:
            defaults = {
                str(item.get("id") or ""): item
                for item in defaults_by_kind[spec.kind]
                if str(item.get("id") or "")
            }
            user_payload = local_payloads[spec.kind]
            local_records = {
                str(item.get("id") or ""): item
                for item in user_payload.get(spec.key, ())
                if isinstance(item, dict) and str(item.get("id") or "")
            }
            disabled = (
                {
                    str(value) for value in user_payload.get(spec.disabled_key, ())
                    if str(value).strip()
                }
                if spec.disabled_key else set()
            )
            for stable_id, default in defaults.items():
                state_record = self._state_record(state, spec, stable_id)
                local_id = str(state_record.get("local_id") or stable_id)
                local = local_records.get(local_id)
                label = _record_label(local or default, local_id)
                if stable_id in disabled or local_id in disabled:
                    key = f"{spec.kind}\x1f{stable_id}\x1f__disabled__"
                    items.append(DefaultOverrideItem(
                        key, spec.kind, spec.label, stable_id, label,
                        "__disabled__", "Removed Default", "Removed", "Available", True,
                        local_id, "hidden_locally", "Hidden locally",
                        "This shipped record is hidden in the current working knowledge. "
                        "Keeping current leaves it hidden; using shipped makes it available again.",
                        "Removed", "Available",
                    ))
                    continue
                if local is None:
                    continue
                override_fields = {
                    str(value) for value in state_record.get("override_fields", ())
                    if str(value).strip()
                }
                if not override_fields:
                    continue
                for field in sorted(override_fields):
                    if field in {"id", "promotion", *spec.local_only_fields}:
                        continue
                    current = local.get(field, _MISSING)
                    shipped = default.get(field, _MISSING)
                    if current == shipped:
                        continue
                    key = f"{spec.kind}\x1f{stable_id}\x1f{field}"
                    if current is _MISSING or current in (None, ""):
                        difference_kind = "current_blank"
                        difference_label = "Current value is blank"
                        difference_summary = (
                            "The current working knowledge leaves this setting blank while "
                            "the shipped default has a value."
                        )
                    elif shipped is _MISSING or shipped in (None, ""):
                        difference_kind = "current_only"
                        difference_label = "Current value only"
                        difference_summary = (
                            "The current working knowledge adds a value that is not present "
                            "in the shipped default."
                        )
                    else:
                        difference_kind = "different"
                        difference_label = "Different values"
                        difference_summary = (
                            "The current working knowledge and shipped default use different values."
                        )
                    items.append(DefaultOverrideItem(
                        key, spec.kind, spec.label, stable_id, label, field,
                        _FIELD_LABELS.get(field, field.replace("_", " ").title()),
                        _preview(current, field=field, labels=labels),
                        _preview(shipped, field=field, labels=labels), False,
                        local_id, difference_kind, difference_label,
                        difference_summary, _detail(current), _detail(shipped),
                    ))
        return DefaultOverrideSnapshot(
            tuple(sorted(items, key=lambda item: (
                item.kind_label.casefold(), item.label.casefold(),
                item.stable_id, item.field_label.casefold(),
            ))),
            _paths_fingerprint(self._authority_paths()),
        )

    def restore(
        self,
        item_keys: Iterable[str],
        *,
        authority_fingerprint: str,
        complete_records: bool = False,
    ) -> DefaultOverrideRestoreResult:
        with self.rule_pair_admission.admit():
            return self._restore_admitted(
                item_keys,
                authority_fingerprint=authority_fingerprint,
                complete_records=complete_records,
            )

    def _restore_admitted(
        self,
        item_keys: Iterable[str],
        *,
        authority_fingerprint: str,
        complete_records: bool = False,
    ) -> DefaultOverrideRestoreResult:
        wanted = {str(value) for value in item_keys if str(value)}
        if not wanted:
            raise ValueError("Choose at least one default override to restore.")
        current = self._scan_admitted(refresh=False)
        if current.authority_fingerprint != authority_fingerprint:
            raise RuntimeError(
                "System Knowledge changed after this default review opened. "
                "Refresh the review and choose the current fields again."
            )
        by_key = {item.key: item for item in current.items}
        missing = sorted(wanted - set(by_key))
        if missing:
            raise RuntimeError(
                "One or more selected default overrides are no longer current. Refresh and retry."
            )
        selected = [by_key[key] for key in sorted(wanted)]
        if complete_records:
            record_keys = {(item.kind, item.stable_id) for item in selected}
            selected = [
                item for item in current.items
                if (item.kind, item.stable_id) in record_keys
            ]

        state = _read_payload(self.state_path)
        payloads: dict[Path, dict[str, Any]] = {}
        restored_records = {(item.kind, item.stable_id) for item in selected}
        for spec in _SPECS:
            spec_items = [item for item in selected if item.kind == spec.kind]
            if not spec_items:
                continue
            path = self.user / spec.user_file
            payload = payloads.setdefault(path, _read_payload(path))
            records = [
                json.loads(json.dumps(item))
                for item in payload.get(spec.key, ()) if isinstance(item, dict)
            ]
            by_id = {
                str(item.get("id") or ""): index
                for index, item in enumerate(records) if str(item.get("id") or "")
            }
            defaults = {
                str(item.get("id") or ""): item for item in self._default_records(spec)
                if str(item.get("id") or "")
            }
            disabled = (
                {
                    str(value) for value in payload.get(spec.disabled_key, ())
                    if str(value).strip()
                }
                if spec.disabled_key else set()
            )
            for stable_id in sorted({item.stable_id for item in spec_items}):
                default = defaults[stable_id]
                state_record = self._state_record(state, spec, stable_id)
                local_id = str(state_record.get("local_id") or stable_id)
                record_items = [item for item in spec_items if item.stable_id == stable_id]
                if any(item.disabled for item in record_items):
                    disabled.discard(stable_id)
                    disabled.discard(local_id)
                fields = {
                    item.field for item in record_items if not item.disabled
                }
                if complete_records:
                    fields = {
                        str(value) for value in state_record.get("override_fields", ())
                        if str(value).strip()
                    }
                index = by_id.get(local_id)
                if index is None and spec.materialized:
                    restored = json.loads(json.dumps(default))
                    restored["id"] = local_id
                    records.append(restored)
                    index = len(records) - 1
                    by_id[local_id] = index
                if index is None:
                    continue
                local = records[index]
                for field in fields:
                    if field in default:
                        local[field] = json.loads(json.dumps(default[field]))
                    else:
                        local.pop(field, None)
                if complete_records and spec.materialized:
                    retained = {
                        field: json.loads(json.dumps(local[field]))
                        for field in spec.local_only_fields if field in local
                    }
                    local = json.loads(json.dumps(default))
                    local["id"] = local_id
                    local.update(retained)
                    records[index] = local
                if not spec.materialized:
                    comparable_local = {
                        key: value for key, value in local.items()
                        if key not in {"promotion"}
                    }
                    comparable_default = {
                        key: value for key, value in default.items()
                        if key not in {"promotion"}
                    }
                    comparable_default["id"] = local_id
                    if comparable_local == comparable_default:
                        records.pop(index)
                        by_id = {
                            str(item.get("id") or ""): value
                            for value, item in enumerate(records)
                            if str(item.get("id") or "")
                        }
            payload[spec.key] = records
            if spec.disabled_key:
                payload[spec.disabled_key] = sorted(disabled)

        paths = tuple(self.user / name for name in sorted(_SAFE_USER_FILES))
        before = {path: path.read_bytes() if path.exists() else None for path in paths}
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = self.user / "backups" / "default-overrides" / f"{stamp}_{uuid4().hex[:8]}"
        backup.mkdir(parents=True, exist_ok=False)
        entries = []
        for index, path in enumerate(paths):
            name = f"{index}_{path.name}"
            content = before[path]
            if content is not None:
                (backup / name).write_bytes(content)
            entries.append({"target": str(path.resolve()), "backup": name, "existed": content is not None})
        manifest = {
            "schema_version": 1,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "files": entries,
        }
        manifest_path = backup / "recovery.json"
        _atomic_json(manifest_path, manifest)
        try:
            for path, payload in payloads.items():
                _atomic_json(path, payload)
            sync_promoted_defaults(self.defaults, self.user)
            after = self._scan_admitted(refresh=False)
            remaining = {item.key for item in after.items}
            if complete_records:
                remaining_records=tuple(
                    item for item in after.items
                    if (item.kind,item.stable_id) in restored_records
                )
                if remaining_records:
                    summary=", ".join(
                        f"{item.kind}:{item.stable_id}.{item.field}"
                        for item in remaining_records[:5]
                    )
                    raise RuntimeError(
                        "Restored default records failed semantic read-back verification: "
                        +summary
                    )
            elif wanted.intersection(remaining):
                raise RuntimeError("Restored default fields failed semantic read-back verification.")
        except BaseException:
            for path, content in before.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic_bytes(path, content)
            manifest["status"] = "restored"
            _atomic_json(manifest_path, manifest)
            raise
        manifest["status"] = "complete"
        _atomic_json(manifest_path, manifest)
        return DefaultOverrideRestoreResult(
            len(selected), len(restored_records), str(backup)
        )


def recover_interrupted_default_override_restores(
    user_dir: str | Path,
) -> list[Path]:
    with RulePairAdmission(Path(user_dir).absolute()).admit():
        return _recover_default_override_restores_admitted(user_dir)


def _recover_default_override_restores_admitted(user_dir: str | Path) -> list[Path]:
    user = Path(user_dir).resolve()
    root = user / "backups" / "default-overrides"
    if not root.exists():
        return []
    restored: list[Path] = []
    for manifest_path in sorted(root.glob("*/recovery.json")):
        manifest = _read_payload(manifest_path)
        if manifest.get("status") != "pending":
            continue
        for entry in manifest.get("files", ()):
            target = Path(str(entry.get("target") or "")).resolve()
            backup = (manifest_path.parent / str(entry.get("backup") or "")).resolve()
            if target.parent != user or target.name not in _SAFE_USER_FILES:
                raise RuntimeError(
                    "Default-override recovery manifest contains an unsafe target."
                )
            if backup.parent != manifest_path.parent.resolve():
                raise RuntimeError(
                    "Default-override recovery manifest contains an unsafe backup path."
                )
            if entry.get("existed"):
                _atomic_bytes(target, backup.read_bytes())
            else:
                target.unlink(missing_ok=True)
        manifest["status"] = "restored"
        _atomic_json(manifest_path, manifest)
        restored.append(manifest_path.parent)
    return restored
