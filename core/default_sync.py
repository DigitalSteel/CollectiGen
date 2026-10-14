from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .character_projection_storage import (
    CHARACTER_STORAGE_SCHEMA_VERSION,
    OUTFIT_STORAGE_SCHEMA_VERSION,
    SERIES_STORAGE_SCHEMA_VERSION,
    character_record_layered_compatibility_payloads,
    character_record_storage_payloads,
)
from .relationship_store import RelationshipStore


_STATE_SCHEMA_VERSION = 6


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").replace("_", " ").casefold().split())


def _identity(kind: str, item: dict[str, Any]) -> tuple:
    if kind == "merge_rule":
        return (
            tuple(sorted(_normalized(value) for value in item.get("source_tags", []))),
            _normalized(item.get("target_tag")),
        )
    name = _normalized(item.get("display") or item.get("name") or item.get("id"))
    if kind == "character":
        return name, str(item.get("series_id") or "")
    if kind == "outfit":
        return (
            name,
            str(item.get("series_id") or ""),
            tuple(sorted(str(value) for value in item.get("character_ids", []))),
        )
    return (name,)


def _identities(kind: str, item: dict[str, Any]) -> set[tuple]:
    primary = _identity(kind, item)
    if kind == "merge_rule":
        return {primary}
    names = {_normalized(item.get("display") or item.get("name") or item.get("id"))}
    names.update(_normalized(value) for value in item.get("aliases", []) if _normalized(value))
    names.discard("")
    if kind == "character":
        return {(name, str(item.get("series_id") or "")) for name in names}
    if kind == "outfit":
        context = (
            str(item.get("series_id") or ""),
            tuple(sorted(str(value) for value in item.get("character_ids", []))),
        )
        return {(name, *context) for name in names}
    return {(name,) for name in names} or {primary}


def _comparable(item: dict[str, Any]) -> dict[str, Any]:
    # ``prune`` builds fresh nested containers, so only the top level needs to
    # be detached before transient fields are removed. Deep-copying every
    # shipped Tag here dominated first-run default synchronization.
    result = dict(item)
    result.pop("promotion", None)
    for key in ("seen_count", "first_seen", "last_seen", "provenance"):
        result.pop(key, None)
    sensitivity = result.get("content_sensitivity")
    if (
        isinstance(sensitivity, dict)
        and not sensitivity.get("minimum_rating")
        and not sensitivity.get("content_flags")
        and str(sensitivity.get("classification_status") or "unclassified") == "unclassified"
    ):
        result.pop("content_sensitivity", None)
    ordering = result.get("ordering")
    if isinstance(ordering, dict) and not ordering.get("reviewed"):
        result.pop("ordering", None)

    def prune(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: clean
                for key, item in value.items()
                if (clean := prune(item)) not in (None, "", [], {})
            }
        if isinstance(value, list):
            return [clean for item in value if (clean := prune(item)) not in (None, "", [], {})]
        return value

    return prune(result)


def _appearance_comparable(item: dict[str, Any]) -> dict[str, Any]:
    result = dict(item) if isinstance(item, dict) else {}
    result.pop("revision", None)
    return result


def _content_hash(item: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(item, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


_MISSING_FIELD = object()
_LOCAL_ONLY_FIELDS = {
    "tag": {"seen_count", "first_seen", "last_seen", "provenance"},
    "character": {"notes"},
    "outfit": {"notes"},
}


def _alias_values(value: Any) -> set[str] | None:
    """Return comparable alias values without iterating missing/invalid fields."""
    if value is _MISSING_FIELD or value is None:
        return set()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return None
    return set(value)


def _field_hash(item: dict[str, Any], field: str) -> str:
    value = item[field] if field in item else {"__collectigen_missing_field__": True}
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _managed_fields(
    kind: str, default: dict[str, Any], local: dict[str, Any]
) -> tuple[str, ...]:
    excluded = {"id", "promotion", *_LOCAL_ONLY_FIELDS.get(kind, set())}
    return tuple(sorted((set(default) | set(local)) - excluded))


def _tag_initial_override_fields(
    local: dict[str, Any], default: dict[str, Any]
) -> set[str]:
    """Conservatively distinguish pre-ledger Tag choices from empty legacy data."""
    overrides: set[str] = set()
    for field in _managed_fields("tag", default, local):
        current = local.get(field, _MISSING_FIELD)
        shipped = default.get(field, _MISSING_FIELD)
        if current == shipped:
            continue
        if field == "category" and str(current or "uncategorized") == "uncategorized":
            continue
        if field == "concept_id" and current in (_MISSING_FIELD, None, ""):
            continue
        if field == "aliases":
            current_aliases = _alias_values(current)
            shipped_aliases = _alias_values(shipped)
            if (
                current_aliases is not None
                and shipped_aliases is not None
                and current_aliases.issubset(shipped_aliases)
            ):
                continue
        if field == "properties" and isinstance(current, dict) and isinstance(shipped, dict):
            if all(key in shipped and shipped[key] == value for key, value in current.items()):
                continue
        if field == "notes" and current in (_MISSING_FIELD, None, ""):
            continue
        if field == "relations" and isinstance(current, dict) and isinstance(shipped, dict):
            covered = True
            for key, value in current.items():
                if value in (None, "", [], {}):
                    continue
                if isinstance(value, list):
                    if not set(value).issubset(set(shipped.get(key, ()) or ())):
                        covered = False
                        break
                elif shipped.get(key) != value:
                    covered = False
                    break
            if covered:
                continue
        if field == "content_sensitivity" and isinstance(current, dict):
            if (
                not current.get("minimum_rating")
                and not current.get("content_flags")
                and str(current.get("classification_status") or "unclassified")
                == "unclassified"
            ):
                continue
        if field == "ordering" and isinstance(current, dict) and not current.get("reviewed"):
            continue
        overrides.add(field)
    return overrides


def _initial_override_fields(
    kind: str, local: dict[str, Any], default: dict[str, Any]
) -> set[str]:
    if kind == "tag":
        return _tag_initial_override_fields(local, default)
    return {
        field
        for field in _managed_fields(kind, default, local)
        if _field_hash(local, field) != _field_hash(default, field)
    }


def _merge_default_fields(
    kind: str,
    default: dict[str, Any],
    local: dict[str, Any],
    override_fields: set[str],
) -> dict[str, Any]:
    merged = copy.deepcopy(default)
    local_id = str(local.get("id") or default.get("id") or "")
    if local_id:
        merged["id"] = local_id
    for field in override_fields:
        if field in local:
            merged[field] = copy.deepcopy(local[field])
        else:
            merged.pop(field, None)
    for field in _LOCAL_ONLY_FIELDS.get(kind, set()):
        if field in local:
            merged[field] = copy.deepcopy(local[field])
    return merged


@dataclass(frozen=True)
class EffectiveTagDefaults:
    """One deterministic Tag vocabulary after reviewed default layers apply."""

    tags: tuple[dict[str, Any], ...]
    id_remap: dict[str, str]
    removed_tag_ids: frozenset[str]
    merged_tag_ids: dict[str, str]
    findings: tuple[str, ...]

    def canonical_id(self, stable_id: Any) -> str:
        current = str(stable_id or "").strip()
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            replacement = str(
                self.merged_tag_ids.get(current)
                or self.id_remap.get(current)
                or current
            )
            if replacement == current:
                return current
            current = replacement
        return current


def resolve_effective_tag_defaults(
    layers: Iterable[Iterable[dict[str, Any]]],
    *,
    removed_tag_ids: Iterable[Any] = (),
    merged_tag_ids: dict[str, Any] | None = None,
) -> EffectiveTagDefaults:
    """Resolve reviewed Tag layers without collapsing distinct alias claims.

    A later record with the same stable ID or exact normalized canonical display
    replaces the earlier semantic record while retaining the first stable ID.
    Alias-only overlap is never enough to choose one identity: collisions remain
    explicit release-blocking findings.
    """

    removed = frozenset(
        str(value).strip() for value in removed_tag_ids if str(value).strip()
    )
    records: list[dict[str, Any]] = []
    by_id: dict[str, int] = {}
    by_display: dict[str, int] = {}
    id_remap: dict[str, str] = {}
    findings: list[str] = []

    for layer in layers:
        for source in layer:
            if not isinstance(source, dict):
                continue
            stable_id = str(source.get("id") or "").strip()
            display = str(source.get("display") or stable_id).strip()
            canonical = _normalized(display)
            if not stable_id or not canonical:
                findings.append("A shipped Tag is missing a stable ID or canonical name.")
                continue
            if stable_id in removed:
                continue

            id_index = by_id.get(stable_id)
            display_index = by_display.get(canonical)
            if id_index is not None and display_index is not None and id_index != display_index:
                findings.append(
                    f"Tag '{display}' reuses stable ID '{stable_id}' and the canonical "
                    "name of a different shipped Tag."
                )
                index = id_index
            elif id_index is not None:
                index = id_index
            elif display_index is not None:
                index = display_index
            else:
                index = len(records)
                records.append({})

            previous = records[index]
            canonical_id = str(previous.get("id") or stable_id)
            old_display = _normalized(previous.get("display") or previous.get("id"))
            if old_display and by_display.get(old_display) == index and old_display != canonical:
                by_display.pop(old_display, None)

            replacement = copy.deepcopy(source)
            replacement["id"] = canonical_id
            records[index] = replacement
            by_id[canonical_id] = index
            by_id[stable_id] = index
            if canonical in by_display and by_display[canonical] != index:
                findings.append(
                    f"Canonical Tag name '{display}' resolves to more than one stable identity."
                )
            else:
                by_display[canonical] = index
            id_remap[stable_id] = canonical_id
            id_remap.setdefault(canonical_id, canonical_id)

    raw_redirects = {
        str(source).strip(): str(target).strip()
        for source, target in dict(merged_tag_ids or {}).items()
        if str(source).strip()
        and str(target).strip()
        and str(source).strip() != str(target).strip()
    }

    def canonical_id(value: Any) -> str:
        current = str(value or "").strip()
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            replacement = str(
                raw_redirects.get(current)
                or id_remap.get(current)
                or current
            )
            if replacement == current:
                return current
            current = replacement
        return current

    for record in records:
        relations = record.get("relations")
        if not isinstance(relations, dict):
            continue
        parent = str(relations.get("parent") or "").strip()
        if parent in id_remap or parent in raw_redirects:
            relations["parent"] = canonical_id(parent)
        for field in ("attributes", "implies", "conflicts"):
            values = relations.get(field)
            if not isinstance(values, list):
                continue
            relations[field] = [
                canonical_id(value)
                if str(value) in id_remap or str(value) in raw_redirects
                else value
                for value in values
            ]

    claims: dict[str, dict[str, set[str]]] = {}
    labels = {
        str(record.get("id") or ""): str(record.get("display") or record.get("id") or "Tag")
        for record in records
    }
    for record in records:
        stable_id = str(record.get("id") or "")
        forms = [(record.get("display"), "canonical name")]
        forms.extend((value, "alias") for value in record.get("aliases", []))
        for value, form_kind in forms:
            normalized = _normalized(value)
            if not normalized:
                continue
            claims.setdefault(normalized, {}).setdefault(stable_id, set()).add(form_kind)
    for normalized, owners in sorted(claims.items()):
        if len(owners) < 2:
            continue
        owner_labels = ", ".join(
            f"'{labels.get(stable_id, stable_id)}'"
            for stable_id in sorted(owners, key=lambda value: labels.get(value, value).casefold())
        )
        findings.append(
            f"Tag name or alias '{normalized}' is claimed by multiple Tags: {owner_labels}."
        )

    redirects = {
        str(source): canonical_id(target)
        for source, target in raw_redirects.items()
        if str(source).strip() and str(target).strip() and str(source) != str(target)
    }
    return EffectiveTagDefaults(
        tuple(records),
        dict(sorted(id_remap.items())),
        removed,
        dict(sorted(redirects.items())),
        tuple(sorted(set(findings))),
    )


def load_effective_tag_defaults(defaults_dir: str | Path) -> EffectiveTagDefaults:
    defaults = Path(defaults_dir)
    payloads = [
        _read_payload(defaults / name)
        for name in ("foundation_tags.json", "community_tags.json", "promoted_tags.json")
    ]
    promoted = payloads[-1]
    resolved=resolve_effective_tag_defaults(
        (
            (item for item in payload.get("tags", []) if isinstance(item, dict))
            for payload in payloads
        ),
        removed_tag_ids=promoted.get("removed_tag_ids", []),
        merged_tag_ids=dict(promoted.get("merged_tag_ids") or {}),
    )
    return _with_reviewed_tag_aliases(defaults,resolved)


def _with_reviewed_tag_aliases(
    defaults: Path,
    resolved: EffectiveTagDefaults,
) -> EffectiveTagDefaults:
    """Materialize reviewed compiler aliases for explicit default consumers."""

    from .tag_alias_relationships import load_tag_alias_source

    path=defaults/"tag_aliases.json"
    if not path.exists():
        return resolved
    aliases=load_tag_alias_source(path)
    records=[]
    for item in resolved.tags:
        record=copy.deepcopy(item)
        values=aliases.get(str(record.get("id") or ""),())
        if values:
            record["aliases"]=list(values)
        else:
            record.pop("aliases",None)
        records.append(record)
    return EffectiveTagDefaults(
        tuple(records),resolved.id_remap,resolved.removed_tag_ids,
        resolved.merged_tag_ids,resolved.findings,
    )


def load_tag_default_compilation_source(
    defaults_dir: str | Path,
    *,
    include_removed_tag_ids: Iterable[Any] = (),
) -> EffectiveTagDefaults:
    """Load all shipped Tag identities before reviewed lifecycle redirects apply.

    Reviewed semantic audit files remain historical evidence for donor identities
    after an approved merge and for exact retired identities. Compilers and
    recovery readers use this source view only when they explicitly name the
    historical stable IDs. Runtime readers must continue to use
    :func:`load_effective_tag_defaults`.
    """
    defaults = Path(defaults_dir)
    payloads = [
        _read_payload(defaults / name)
        for name in ("foundation_tags.json", "community_tags.json", "promoted_tags.json")
    ]
    promoted = payloads[-1]
    included = {
        str(value).strip()
        for value in include_removed_tag_ids
        if str(value).strip()
    }
    removed = {
        str(value).strip()
        for value in promoted.get("removed_tag_ids", ())
        if str(value).strip() and str(value).strip() not in included
    }
    redirects = {
        str(source): str(target)
        for source, target in dict(promoted.get("merged_tag_ids") or {}).items()
        if str(source).strip() in removed
    }
    resolved=resolve_effective_tag_defaults(
        (
            (item for item in payload.get("tags", []) if isinstance(item, dict))
            for payload in payloads
        ),
        removed_tag_ids=removed,
        merged_tag_ids=redirects,
    )
    return _with_reviewed_tag_aliases(defaults,resolved)


def _read_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_payload(
    path: Path, payload: dict[str, Any], *, compact: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if compact else json.dumps(payload, indent=2, ensure_ascii=False)
    )
    temporary.write_text(
        encoded + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _files_fingerprint(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256(b"tag-manager-default-sync-v3\0")
    for path in sorted({Path(value) for value in paths}, key=lambda value: str(value).casefold()):
        digest.update(str(path).casefold().encode("utf-8", errors="surrogatepass"))
        digest.update(b"\0")
        try:
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
        except OSError as exc:
            digest.update(f"<unreadable:{getattr(exc, 'errno', None)}>".encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _resolve_effective_category(
    category_id: Any,
    *,
    default_categories: dict[str, dict[str, Any]],
    user_categories: dict[str, dict[str, Any]],
    active_category_ids: set[str],
) -> str:
    """Respect category tombstones without orphaning later shipped records.

    Nested Category deletion already promotes direct members to the semantic
    parent. A later shipped Tag must follow the same effective hierarchy
    instead of reviving the deleted Category or retaining a dangling ID.
    """
    requested = str(category_id or "uncategorized")
    if requested in active_category_ids:
        return requested
    seen: set[str] = set()
    current = requested
    while current and current not in seen:
        seen.add(current)
        record = user_categories.get(current) or default_categories.get(current)
        if record is None:
            break
        parent_id = str(record.get("parent_id") or "")
        if not parent_id:
            break
        if parent_id in active_category_ids:
            return parent_id
        current = parent_id
    return "uncategorized" if "uncategorized" in active_category_ids else requested


def _effective_category_context(
    defaults_dir: Path, user_dir: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], set[str]]:
    default_payload = _read_payload(defaults_dir / "categories.json")
    user_payload = _read_payload(user_dir / "categories.json")
    default_categories = {
        str(item.get("id") or ""): item
        for item in default_payload.get("categories", ())
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    user_categories = {
        str(item.get("id") or ""): item
        for item in user_payload.get("categories", ())
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    disabled = {
        str(value) for value in user_payload.get("disabled_default_ids", ())
        if str(value).strip()
    }
    active = (set(default_categories) - disabled) | set(user_categories)
    return default_categories, user_categories, active


def _enrich_local_tag(
    local: dict[str, Any],
    default: dict[str, Any],
    *,
    id_remap: dict[str, str] | None = None,
) -> bool:
    changed = False
    if (
        str(local.get("category") or "uncategorized") == "uncategorized"
        and str(default.get("category") or "uncategorized") != "uncategorized"
    ):
        local["category"] = default["category"]
        changed = True
    if not local.get("concept_id") and default.get("concept_id"):
        local["concept_id"] = default["concept_id"]
        changed = True
    aliases = list(local.get("aliases", []))
    seen = {_normalized(value) for value in aliases}
    for value in default.get("aliases", []):
        if _normalized(value) and _normalized(value) not in seen:
            aliases.append(value)
            seen.add(_normalized(value))
            changed = True
    if aliases != local.get("aliases", []):
        local["aliases"] = aliases
    properties = local.setdefault("properties", {})
    for key, value in default.get("properties", {}).items():
        if key not in properties:
            properties[key] = value
            changed = True
    if not local.get("notes") and default.get("notes"):
        local["notes"] = default["notes"]
        changed = True
    local_relations = local.setdefault("relations", {})
    default_relations = default.get("relations") if isinstance(default.get("relations"), dict) else {}
    if id_remap:
        parent=str(local_relations.get("parent") or "")
        if parent in id_remap and id_remap[parent]!=parent:
            local_relations["parent"]=id_remap[parent]
            changed=True
        for key in ("attributes","implies","conflicts"):
            current=local_relations.get(key)
            if not isinstance(current,list):continue
            remapped=[]; known=set()
            for value in current:
                replacement=id_remap.get(str(value),value)
                marker=str(replacement)
                if marker in known:continue
                known.add(marker); remapped.append(replacement)
            if remapped!=current:
                local_relations[key]=remapped
                changed=True
    if not local_relations.get("parent") and default_relations.get("parent"):
        local_relations["parent"] = default_relations["parent"]
        changed = True
    for key in ("attributes", "implies", "conflicts"):
        values = list(local_relations.get(key, []) or [])
        known = {_normalized(value) for value in values}
        for value in default_relations.get(key, []) or []:
            if _normalized(value) not in known:
                values.append(value)
                known.add(_normalized(value))
                changed = True
        if values:
            local_relations[key] = values
    current = local.get("content_sensitivity") if isinstance(local.get("content_sensitivity"), dict) else {}
    shipped = default.get("content_sensitivity") if isinstance(default.get("content_sensitivity"), dict) else {}
    if (
        str(current.get("classification_status") or "unclassified") == "unclassified"
        and str(shipped.get("classification_status") or "unclassified") != "unclassified"
    ):
        local["content_sensitivity"] = copy.deepcopy(shipped)
        changed = True
    current_ordering = local.get("ordering") if isinstance(local.get("ordering"), dict) else {}
    shipped_ordering = default.get("ordering") if isinstance(default.get("ordering"), dict) else {}
    if shipped_ordering.get("reviewed") and not current_ordering.get("reviewed"):
        local["ordering"] = copy.deepcopy(shipped_ordering)
        changed = True
    return changed


def shipped_tag_records(
    defaults_dir: str | Path,
    stable_ids: Iterable[Any],
) -> dict[str, Any]:
    """Batch live-canonical and exact-retired defaults for tombstone recovery."""
    from .tag_database import TagRecord

    targets = {
        str(stable_id).strip()
        for stable_id in stable_ids
        if str(stable_id).strip()
    }
    if not targets:
        return {}
    effective = load_effective_tag_defaults(defaults_dir)
    live = {
        str(item.get("id") or ""): item
        for item in effective.tags
        if str(item.get("id") or "")
    }
    recovered: dict[str, dict[str, Any]] = {}
    retired_targets: set[str] = set()
    for target in targets:
        canonical = effective.canonical_id(target)
        if canonical in live:
            recovered[target] = live[canonical]
        elif (
            target in effective.removed_tag_ids
            and target not in effective.merged_tag_ids
        ):
            retired_targets.add(target)
    if retired_targets:
        historical_ids = set(retired_targets)
        changed = True
        while changed:
            changed = False
            for source, target in effective.merged_tag_ids.items():
                if str(target) in historical_ids and str(source) not in historical_ids:
                    historical_ids.add(str(source))
                    changed = True
        historical = load_tag_default_compilation_source(
            defaults_dir,
            include_removed_tag_ids=historical_ids,
        )
        for item in historical.tags:
            stable_id = str(item.get("id") or "")
            if stable_id in retired_targets:
                recovered[stable_id] = item
    return {
        stable_id: TagRecord.from_dict(record)
        for stable_id, record in recovered.items()
    }


def shipped_tag_record(defaults_dir: str | Path, stable_id: str):
    """Return one live canonical or exact retired default for recovery."""
    target = str(stable_id).strip()
    return shipped_tag_records(defaults_dir, (target,)).get(target) if target else None


def sync_promoted_defaults(defaults_dir: str | Path, user_dir: str | Path) -> dict[str, int]:
    """Synchronize reviewed shipped defaults without replacing local choices."""
    defaults = Path(defaults_dir)
    user = Path(user_dir)
    counts: dict[str, int] = {}
    state_path = user / "default_knowledge_state.json"
    from .default_tag_identity import _payload as identity_payload, DefaultTagIdentityHold
    tracking_raw = state_path.read_bytes() if state_path.exists() else None
    state = identity_payload(tracking_raw)
    sources = {
        "tags": (
            [
                defaults / "foundation_tags.json",
                defaults / "community_tags.json",
                defaults / "promoted_tags.json",
            ],
            user / "tags.json",
            "tags",
            "tag",
            True,
            "removed_tag_ids",
        ),
        "merge_rules": (
            [defaults / "promoted_tags.json"],
            user / "tags.json",
            "merge_rules",
            "merge_rule",
            True,
            "disabled_default_merge_rule_ids",
        ),
        "categories": (
            [defaults / "categories.json"],
            user / "categories.json",
            "categories",
            "category",
            False,
            "disabled_default_ids",
        ),
        "patterns": (
            [defaults / "patterns.json"],
            user / "patterns.json",
            "patterns",
            "pattern",
            False,
            "disabled_default_ids",
        ),
        "series": (
            [defaults / "series.json"],
            user / "series.json",
            "series",
            "series",
            True,
            "disabled_default_ids",
        ),
        "characters": (
            [defaults / "characters.json"],
            user / "characters.json",
            "characters",
            "character",
            True,
            "disabled_default_ids",
        ),
        "outfits": (
            [defaults / "outfits.json"],
            user / "outfits.json",
            "outfits",
            "outfit",
            True,
            "disabled_default_ids",
        ),
    }
    appearance_default = defaults / "tag_appearance.json"
    appearance_local = user / "tag_appearance.json"
    input_paths = [
        path
        for paths, target, _key, _kind, _add_missing, _disabled_key in sources.values()
        for path in [*paths, target]
    ]
    input_paths.extend((appearance_default, appearance_local))
    default_relationships_path = defaults / "relationships.json"
    user_relationships_path = user / "relationships.json"
    input_paths.extend((default_relationships_path, user_relationships_path))
    fingerprint = _files_fingerprint(input_paths)
    if (
        state.get("input_fingerprint") == fingerprint
        and int(state.get("schema_version", 0) or 0) == _STATE_SCHEMA_VERSION
    ):
        return {}
    # Explicit source inputs, validated before the first synchronization write.
    from .default_tag_identity import admit_default_tag_identity, resolve_tag_identity
    from .relationship_store import canonicalize_relationship_tag_endpoints
    tags_path = user / "tags.json"
    tags_raw = tags_path.read_bytes() if tags_path.exists() else None
    before_state = json.dumps(state, sort_keys=True, ensure_ascii=False)
    tracked = state.setdefault("records", {})
    promoted_payload = _read_payload(defaults / "promoted_tags.json")
    default_categories, user_categories, active_category_ids = (
        _effective_category_context(defaults, user)
    )
    shipped_removed_tag_ids = {
        str(value)
        for value in promoted_payload.get("removed_tag_ids", [])
        if str(value).strip()
    }
    shipped_tag_redirects = {
        str(source): str(target)
        for source, target in dict(promoted_payload.get("merged_tag_ids") or {}).items()
        if str(source).strip() and str(target).strip() and str(source) != str(target)
    }
    effective_tags = load_effective_tag_defaults(defaults)
    shipped_tag_redirects = effective_tags.merged_tag_ids
    default_relationships = tuple(
        RelationshipStore(default_relationships_path).relationships.values()
    )
    user_relationships = tuple(
        RelationshipStore(user_relationships_path).relationships.values()
    )
    current_payload = _read_payload(tags_path)
    tracked_tags = tracked.get("tags", {})
    shipped_ids = {str(row.get("id") or "") for row in effective_tags.tags}
    relevant_ids = set(shipped_ids) | set(shipped_tag_redirects) | set(shipped_tag_redirects.values())
    previous_lifecycle = state.get("tag_lifecycle", {})
    if isinstance(previous_lifecycle, dict):
        previous_redirects = dict(previous_lifecycle.get("merged_tag_ids") or {})
        current_redirects = dict(current_payload.get("merged_tag_ids") or {})
        current_ids = {row.get("id") for row in current_payload.get("tags", []) if isinstance(row, dict)}
        for source in shipped_tag_redirects:
            prior_target = previous_redirects.get(source)
            # Only a present, changed current default redirect needs the
            # resolved historical baseline to establish continued ownership.
            # Absent or obsolete redirects remain local/dormant evidence.
            if (source not in current_ids and source in current_redirects
                and current_redirects[source] != prior_target
                and prior_target in (set(tracked_tags) | set(current_redirects))):
                relevant_ids.add(prior_target)
    for row in (*default_relationships, *user_relationships):
        relevant_ids.update(endpoint.id for endpoint in row.endpoints if endpoint.kind == "tag"
            and (endpoint.id in shipped_ids or endpoint.id in tracked_tags))
    equivalent = {}
    for identity in relevant_ids:
        equivalent.setdefault(effective_tags.canonical_id(identity), set()).add(identity)
    prospective = shipped_ids - set(current_payload.get("removed_tag_ids", []))
    identity_context = admit_default_tag_identity(tags_raw, tracking_raw,
        tags_identity=str(tags_path.absolute()), tracking_identity=str(state_path.absolute()),
        required_ids=relevant_ids, prospective_ids=prospective,
        equivalent_ids=equivalent.values(), prospective_redirects=shipped_tag_redirects)

    class IdentityProjection:
        def __init__(self, view, context):
            self.tags = view.tags
            self.view, self.context = view, context

        def resolve_tag_id(self, identity):
            return resolve_tag_identity(identity, active_ids=self.tags,
                removed_ids=self.view.removed_tag_ids, redirects=self.view.merged_tag_ids,
                context=self.context)

    write_started = False
    def write_sync_payload(path, payload, *, compact=False):
        nonlocal write_started
        if not write_started:
            current_tracking = state_path.read_bytes() if state_path.exists() else None
            if current_tracking != tracking_raw or _files_fingerprint(input_paths) != fingerprint:
                raise DefaultTagIdentityHold("Synchronization identity inputs changed before the first write")
            write_started = True
        _write_payload(path, payload, compact=compact)

    character_schema_versions = {
        "series": SERIES_STORAGE_SCHEMA_VERSION,
        "character": CHARACTER_STORAGE_SCHEMA_VERSION,
        "outfit": OUTFIT_STORAGE_SCHEMA_VERSION,
    }
    for label, (paths, target, key, kind, add_missing, disabled_key) in sources.items():
        shipped = []
        tag_storage_payloads=None
        shipped_schema_version = 1
        for path in paths:
            source_payload = _read_payload(path)
            shipped_schema_version = max(
                shipped_schema_version,
                int(source_payload.get("schema_version", 1) or 1),
            )
            shipped.extend(item for item in source_payload.get(key, []) if isinstance(item, dict))
        if kind == "tag":
            shipped = list(effective_tags.tags)
            reconciled = []
            for source_item in shipped:
                item = copy.deepcopy(source_item)
                item["category"] = _resolve_effective_category(
                    item.get("category"),
                    default_categories=default_categories,
                    user_categories=user_categories,
                    active_category_ids=active_category_ids,
                )
                reconciled.append(item)
            shipped = reconciled
            from .allowed_dimension_policy import publish_allowed_dimension_storage_authority
            from .category_registry import CategoryRegistry
            from .knowledge_engine import FoundationKnowledge
            from .modifier_composition import publish_modifier_composition_storage_authority
            from .scoped_constraint_projection import publish_scoped_constraint_storage_authority
            from .tag_alias_relationships import publish_tag_alias_storage_authority
            from .tag_database import TagDatabase,TagRecord
            from .tag_semantic_relationships import publish_tag_semantic_authority

            effective_relationships={item.id:item for item in default_relationships}
            effective_relationships.update((item.id,item) for item in user_relationships)
            tag_view=TagDatabase(target)
            tag_view.tags={
                str(item.get("id") or ""):TagRecord.from_dict(item)
                for item in shipped if str(item.get("id") or "")
            }
            tag_view._rebuild_lookup()
            category_view=CategoryRegistry(
                defaults/"categories.json",user/"categories.json",
            )
            category_view.configure_category_base_authority(
                effective_relationships.values(),
            )
            foundation=FoundationKnowledge(
                defaults/"modifiers.json",defaults/"concepts.json",
            )
            publish_tag_alias_storage_authority(
                tag_view,effective_relationships.values(),
            )
            publish_tag_semantic_authority(
                tag_view,effective_relationships.values(),
            )
            publish_modifier_composition_storage_authority(
                tag_view,effective_relationships.values(),
                categories=category_view,foundation=foundation,
            )
            publish_allowed_dimension_storage_authority(
                tag_view,effective_relationships.values(),
            )
            publish_scoped_constraint_storage_authority(
                tag_view,effective_relationships.values(),
            )

            def tag_storage_payloads(records, context=identity_context):
                tag_view.tags={
                    str(item.get("id") or ""):TagRecord.from_dict(item)
                    for item in records if str(item.get("id") or "")
                }
                tag_view._rebuild_lookup()
                projected_relationships = canonicalize_relationship_tag_endpoints(
                    effective_relationships.values(), IdentityProjection(tag_view, context))
                publish_tag_alias_storage_authority(tag_view, projected_relationships)
                publish_tag_semantic_authority(tag_view, projected_relationships)
                result=[]
                for item in records:
                    tag_id=str(item.get("id") or "")
                    if tag_id not in tag_view.tags:
                        continue
                    normalized=tag_view._record_payload(tag_view.tags[tag_id])
                    storage=copy.deepcopy(item)
                    if "aliases" in normalized:
                        if "aliases" in storage or normalized["aliases"]:
                            storage["aliases"]=normalized["aliases"]
                    else:
                        storage.pop("aliases",None)
                    relation_fields=(
                        "parent","attributes","implies",
                        "allowed_modifier_groups","conflicts",
                    )
                    original_relations=storage.get("relations")
                    had_relations=isinstance(original_relations,dict)
                    relations=(
                        dict(original_relations) if had_relations else {}
                    )
                    normalized_relations=dict(normalized.get("relations") or {})
                    for field in relation_fields:
                        if field in normalized_relations:
                            if field in relations or normalized_relations[field]:
                                relations[field]=normalized_relations[field]
                        else:
                            relations.pop(field,None)
                    if had_relations or relations:
                        storage["relations"]=relations
                    else:
                        storage.pop("relations",None)
                    result.append(storage)
                return result

            shipped=tag_storage_payloads(shipped)
        elif kind in {"series", "character", "outfit"}:
            shipped = character_record_layered_compatibility_payloads(
                kind, shipped, default_relationships,
            )
            remapped = []
            for source_item in shipped:
                item = copy.deepcopy(source_item)
                link = item.get("knowledge_tag")
                if isinstance(link, dict):
                    tag_id = str(link.get("tag_id") or "")
                    if tag_id in effective_tags.id_remap:
                        link["tag_id"] = effective_tags.canonical_id(tag_id)
                remapped.append(item)
            shipped = remapped
        shipped_by_id = {
            str(item.get("id") or ""): item
            for item in shipped
            if str(item.get("id") or "")
        }
        if not shipped and kind != "tag":
            continue
        payload = _read_payload(target)
        raw_local = [
            item for item in payload.get(key, []) if isinstance(item, dict)
        ]
        raw_projection_fields_by_id = {
            str(item.get("id") or ""): tuple(item.keys())
            for item in raw_local
            if str(item.get("id") or "")
        }
        local = raw_local
        if kind=="tag":
            local=tag_storage_payloads(raw_local)
        elif kind in {"series", "character", "outfit"}:
            local = character_record_layered_compatibility_payloads(
                kind,
                raw_local,
                default_relationships,
                user_relationships,
            )
        disabled_ids = {
            str(value)
            for value in payload.get(disabled_key, [])
            if str(value).strip()
        }
        removed_ids = {
            str(value) for value in payload.get("removed_tag_ids", []) if str(value).strip()
        } if kind == "tag" else set()
        if kind == "tag":
            previous_lifecycle = (
                state.get("tag_lifecycle")
                if isinstance(state.get("tag_lifecycle"), dict)
                else {}
            )
            previous_removed = {
                str(value) for value in previous_lifecycle.get("removed_tag_ids", [])
            }
            previous_redirects = {
                str(source): str(value)
                for source, value in dict(
                    previous_lifecycle.get("merged_tag_ids") or {}
                ).items()
            }
            local_redirects = {
                str(source): str(value)
                for source, value in dict(payload.get("merged_tag_ids") or {}).items()
            }
            prior_tag_state = tracked.setdefault(label, {})
            kept_local = []
            for record in local:
                tag_id = str(record.get("id") or "")
                if tag_id not in shipped_removed_tag_ids:
                    kept_local.append(record)
                    continue
                current_hash = _content_hash(_comparable(record))
                previous_hash = str(prior_tag_state.get(tag_id, {}).get("hash") or "")
                shipped_record = shipped_by_id.get(tag_id)
                shipped_hash = (
                    _content_hash(_comparable(shipped_record)) if shipped_record else ""
                )
                was_untouched = bool(
                    current_hash
                    and (current_hash == previous_hash or current_hash == shipped_hash)
                )
                if tag_id not in previous_removed and was_untouched:
                    removed_ids.add(tag_id)
                    continue
                # A restored or locally changed Tag is an explicit correction.
                removed_ids.discard(tag_id)
                kept_local.append(record)
            local = kept_local
            local_ids = {str(item.get("id") or "") for item in local}
            for tag_id in shipped_removed_tag_ids:
                if tag_id in local_ids:
                    continue
                if tag_id in previous_removed and tag_id not in removed_ids:
                    # The user deliberately cleared a previously shipped tombstone.
                    continue
                removed_ids.add(tag_id)
            for source, target_id in shipped_tag_redirects.items():
                if source in local_ids:
                    continue
                prior_target = previous_redirects.get(source)
                current_target = local_redirects.get(source)
                if prior_target and source not in local_redirects:
                    # Explicit absence is a local choice even when the old
                    # target is retired or cannot resolve anymore.
                    continue
                if prior_target and current_target != prior_target:
                    resolved_prior = resolve_tag_identity(prior_target, active_ids=local_ids,
                        removed_ids=removed_ids, redirects=local_redirects, context=identity_context)
                    if resolved_prior is None or current_target != resolved_prior:
                        continue
                resolved_target = resolve_tag_identity(target_id, active_ids=local_ids,
                    removed_ids=removed_ids, redirects=local_redirects, context=identity_context)
                local_redirects[source] = resolved_target or target_id
            payload["removed_tag_ids"] = sorted(removed_ids)
            payload["merged_tag_ids"] = dict(sorted(local_redirects.items()))
        ids = {str(item.get("id") or ""): index for index, item in enumerate(local)}
        identities: dict[tuple, set[int]] = {}

        def add_identity_index(index: int, record: dict[str, Any]) -> None:
            for identity in _identities(kind, record):
                identities.setdefault(identity, set()).add(index)

        def remove_identity_index(index: int, record: dict[str, Any]) -> None:
            for identity in _identities(kind, record):
                matches = identities.get(identity)
                if matches is None:
                    continue
                matches.discard(index)
                if not matches:
                    identities.pop(identity, None)

        for local_index, local_item in enumerate(local):
            add_identity_index(local_index, local_item)
        added = updated = 0
        kind_state = tracked.setdefault(label, {})
        for item in shipped:
            item_id = str(item.get("id") or "")
            previous = kind_state.get(item_id, {}) if item_id else {}
            previous_local_id = str(previous.get("local_id") or "")
            if item_id and (
                item_id in removed_ids
                or previous_local_id in removed_ids
                or item_id in disabled_ids
                or previous_local_id in disabled_ids
            ):
                continue
            item_identities = _identities(kind, item)
            index = ids.get(item_id)
            if kind == "tag" and index is None:
                preserved = identity_context.mappings.get(item_id)
                if preserved is not None:
                    index = ids.get(preserved)
            if index is None:
                primary_matches = identities.get(_identity(kind, item), set())
                if len(primary_matches) == 1:
                    index = next(iter(primary_matches))
                else:
                    alias_matches = {
                        candidate
                        for value in item_identities
                        for candidate in identities.get(value, set())
                    }
                    if len(alias_matches) == 1:
                        index = next(iter(alias_matches))
            default_hash = _content_hash(_comparable(item))
            added_now = False
            if index is None and add_missing:
                local.append(copy.deepcopy(item))
                index = len(local) - 1
                ids[item_id] = index
                add_identity_index(index, local[index])
                added += 1
                added_now = True
            if index is None:
                continue

            prior_local = (
                local[index] if added_now else copy.deepcopy(local[index])
            )
            previous_effective_hashes = (
                previous.get("effective_field_hashes")
                if isinstance(previous.get("effective_field_hashes"), dict)
                else {}
            )
            override_fields = {
                str(value)
                for value in previous.get("override_fields", [])
                if str(value).strip()
            }
            managed_fields = _managed_fields(kind, item, prior_local)
            if added_now:
                override_fields.clear()
            elif previous_effective_hashes:
                for field in managed_fields:
                    current_hash = _field_hash(prior_local, field)
                    if field not in previous_effective_hashes:
                        if field not in prior_local or current_hash == _field_hash(item, field):
                            override_fields.discard(field)
                        else:
                            override_fields.add(field)
                        continue
                    prior_hash = str(previous_effective_hashes.get(field) or "")
                    if current_hash == prior_hash:
                        continue
                    if current_hash == _field_hash(item, field):
                        override_fields.discard(field)
                    else:
                        override_fields.add(field)
            elif previous and str(previous.get("hash") or "") == _content_hash(
                _comparable(prior_local)
            ):
                override_fields.clear()
            else:
                override_fields = _initial_override_fields(kind, prior_local, item)

            merged = (
                prior_local if added_now else
                _merge_default_fields(kind, item, prior_local, override_fields)
            )
            if merged != prior_local:
                local[index] = merged
                remove_identity_index(index, prior_local)
                add_identity_index(index, merged)
                updated += 1
            if item_id:
                local_id = str(local[index].get("id") or item_id)
                effective_fields = _managed_fields(kind, item, local[index])
                effective_field_hashes = {
                    field: _field_hash(local[index], field)
                    for field in effective_fields
                }
                kind_state[item_id] = {
                    "hash": (
                        default_hash if local_id == item_id else
                        _content_hash(_comparable({**item,"id":local_id}))
                    ),
                    "local_id": local_id,
                    "override_fields": sorted(override_fields),
                    "effective_field_hashes": effective_field_hashes,
                }
        storage_changed = False
        if kind=="tag":
            planned_payload = {**payload, "tags": local}
            final_context = admit_default_tag_identity(
                json.dumps(planned_payload).encode(), json.dumps(state).encode(),
                tags_identity=str(tags_path.absolute()), tracking_identity=str(state_path.absolute()),
                required_ids=relevant_ids, equivalent_ids=equivalent.values())
            storage_local=tag_storage_payloads(local, final_context)
            storage_changed=storage_local!=raw_local
            local=storage_local
        elif kind in {"series", "character", "outfit"}:
            storage_local = character_record_storage_payloads(
                kind,
                local,
                default_relationships,
                user_relationships,
                projection_fields_by_id=raw_projection_fields_by_id,
            )
            storage_changed = storage_local != raw_local
            local = storage_local
        if added or updated or storage_changed:
            payload["schema_version"] = max(
                shipped_schema_version,
                TagDatabase.SCHEMA_VERSION if kind=="tag" else 1,
                character_schema_versions.get(kind, 1),
                int(payload.get("schema_version", 1) or 1),
            )
            payload[key] = local
        if kind == "tag" and (
            payload.get("removed_tag_ids") != _read_payload(target).get("removed_tag_ids")
            or payload.get("merged_tag_ids") != _read_payload(target).get("merged_tag_ids")
        ):
            payload[key] = local
        if payload != _read_payload(target):
            write_sync_payload(target, payload)
        counts[label] = added
        if updated:
            counts[f"{label}_updated"] = updated
    state["tag_lifecycle"] = {
        "removed_tag_ids": sorted(shipped_removed_tag_ids),
        "merged_tag_ids": dict(sorted(shipped_tag_redirects.items())),
    }
    if appearance_default.is_file():
        shipped_appearance = _read_payload(appearance_default)
        shipped_tag_styles=shipped_appearance.get("tags")
        if isinstance(shipped_tag_styles,dict):
            remapped_styles={}
            for stable_id,style in shipped_tag_styles.items():
                canonical=effective_tags.canonical_id(stable_id)
                remapped_styles.setdefault(canonical,style)
            shipped_appearance["tags"]=remapped_styles
        shipped_hash = _content_hash(_appearance_comparable(shipped_appearance))
        previous_hash = str(state.get("tag_appearance_default_hash") or "")
        local_appearance = _read_payload(appearance_local)
        local_hash = _content_hash(_appearance_comparable(local_appearance))
        if not appearance_local.is_file():
            write_sync_payload(appearance_local, shipped_appearance)
            counts["tag_appearance"] = 1
        elif previous_hash and local_hash == previous_hash and local_hash != shipped_hash:
            write_sync_payload(appearance_local, shipped_appearance)
            counts["tag_appearance_updated"] = 1
        state["tag_appearance_default_hash"] = shipped_hash
    state["schema_version"] = _STATE_SCHEMA_VERSION
    state["input_fingerprint"] = _files_fingerprint(input_paths)
    comparable_state = dict(state)
    comparable_state.pop("last_sync", None)
    previous_state = _read_payload(state_path)
    previous_state.pop("last_sync", None)
    if (
        json.dumps(comparable_state, sort_keys=True, ensure_ascii=False)
        != json.dumps(previous_state, sort_keys=True, ensure_ascii=False)
        or before_state == "{}"
    ):
        state["last_sync"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        write_sync_payload(state_path, state, compact=True)
    return counts
