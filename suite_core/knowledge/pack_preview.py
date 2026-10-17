"""Read explicit Pack input into public partial patch DTOs. Never apply or merge.

This is inspection evidence, not native import planning or effective authority.
Unsupported semantics remain visible as holds and accounting, never inferred.
"""
from dataclasses import dataclass, field
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import zipfile
from uuid import uuid4

from .pack_container import (
    MAX_PACK_FILES, MAX_PACK_UNCOMPRESSED_BYTES, MAX_PACK_RECORDS,
    MAX_PACK_RELATIONSHIPS, checked_destination, is_record_list, validate_manifest,
    validate_json_depth, FOUNDATION_PACK_SCHEMA_VERSION, FOUNDATION_PACK_SCOPE,
)
from .tag_lookup import normalize_term
from .version import PACK_PREVIEW_INTERFACE_VERSION

MAX_PACK_INPUT_BYTES = MAX_PACK_UNCOMPRESSED_BYTES + 2 * 1024 * 1024
MAX_PUBLIC_TEXT = 1000
MAX_PACK_ENDPOINTS = 160_000  # Independent aggregate bound; unchanged by row capacity.
# All native record families are accounted for, including unsupported semantics.
SPECS = (
    ("tags", "tags.json", "tags", ""),
    ("merge_rules", "tags.json", "merge_rules", "disabled_default_merge_rule_ids"),
    ("categories", "categories.json", "categories", "disabled_default_ids"),
    ("patterns", "patterns.json", "patterns", "disabled_default_ids"),
    ("series", "series.json", "series", "disabled_default_ids"),
    ("characters", "characters.json", "characters", "disabled_default_ids"),
    ("outfits", "outfits.json", "outfits", "disabled_default_ids"),
    ("consumer_policies", "consumer_policies.json", "policies", ""),
)


@dataclass(frozen=True)
class Hold:
    code: str
    family: str = "pack"
    record_id: str = ""
    reference_kind: str = ""
    reference_id: str = ""


@dataclass(frozen=True)
class Attribution:
    reference: str
    archive_sha256: str
    schema_version: int
    declared_scope: str
    content_rating_boundary: str
    default_relationship_fingerprint: str


@dataclass(frozen=True)
class TagPatch:
    id: str
    display: str
    category_id: str
    declared_aliases: tuple[str, ...]
    minimum_rating: str
    classification_status: str
    declared_base_fingerprint: str


@dataclass(frozen=True)
class CategoryPatch:
    id: str
    label: str
    parent_id: str
    declared_base_fingerprint: str


@dataclass(frozen=True)
class Endpoint:
    kind: str
    id: str


@dataclass(frozen=True)
class RelationshipPatch:
    id: str
    kind: str
    endpoints: tuple[Endpoint, ...]
    directed: bool
    reviewed: bool
    disabled: bool
    version: int
    declared_base_fingerprint: str


@dataclass(frozen=True)
class FamilyAccounting:
    family: str
    records: int
    disabled_ids: tuple[str, ...] = ()
    record_ids: tuple[str, ...] = ()
    lifecycle_owned_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PackPreview:
    attribution: Attribution
    tags: tuple[TagPatch, ...]
    categories: tuple[CategoryPatch, ...]
    relationships: tuple[RelationshipPatch, ...]
    families: tuple[FamilyAccounting, ...]
    removed_ids: tuple[str, ...]
    legacy_removed_ids: tuple[str, ...]
    redirects: tuple[tuple[str, str], ...]
    removed_archive_count: int
    unrecognized_file_count: int
    holds: tuple[Hold, ...]
    completeness: str = field(default="partial", init=False)

    @property
    def ready_to_apply(self):
        return False  # This reader cannot supply destination authority or approval.

    def unacknowledged_holds(self, acknowledged=()):
        """Filter a review UI's separate acknowledgments; source holds stay intact.

        Acknowledgment clears a visible review item, not its missing evidence or
        native apply gate. It never changes completeness or ready_to_apply.
        """
        acknowledged = frozenset(acknowledged)
        if not acknowledged.issubset(self.holds):
            raise ValueError("Acknowledgment does not belong to this preview")
        return tuple(hold for hold in self.holds if hold not in acknowledged)


def _text(value, *, blank=False):
    if not isinstance(value, str) or len(value) > MAX_PUBLIC_TEXT or (not blank and not value.strip()):
        raise ValueError("Invalid or oversized public string")
    return value


def _ids(value):
    if not isinstance(value, list):
        raise ValueError("Invalid ID list")
    result = tuple(_text(item) for item in value)
    if len(set(result)) != len(result):
        raise ValueError("Duplicate ID in lifecycle list")
    return result


def _fingerprint(value):
    if value in ("", "missing"):
        return value
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise ValueError("Invalid declared fingerprint")
    return value


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _json(raw):
    validate_json_depth(raw)
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object_pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON value")))
    if not isinstance(value, dict):
        raise ValueError("Pack payload must be a JSON object")
    return value


def _read_input(pack):
    if isinstance(pack, bytes):
        raw = pack
    elif isinstance(pack, (str, Path)):
        # Only the explicit caller file is opened. No root, catalog or services.
        with Path(pack).open("rb") as handle:
            raw = handle.read(MAX_PACK_INPUT_BYTES + 1)
    else:
        raise TypeError("Pack input must be bytes or an explicit path")
    if len(raw) > MAX_PACK_INPUT_BYTES:
        raise ValueError("Pack input exceeds preview byte bound")
    return raw


def _members(raw):
    output = {}
    with zipfile.ZipFile(BytesIO(raw), "r") as archive:
        entries = archive.infolist()
        if len(entries) > MAX_PACK_FILES:
            raise ValueError("Too many Pack entries")
        if sum(max(0, int(item.file_size)) for item in entries) > MAX_PACK_UNCOMPRESSED_BYTES:
            raise ValueError("Pack exceeds expanded byte bound")
        # A not-created virtual root permits reuse of native containment checks.
        root = Path.cwd().resolve() / ("__pack_preview_virtual_" + uuid4().hex)
        total = 0
        seen = set()
        for item in entries:
            checked_destination(item, root, root)
            name = item.filename.replace("\\", "/")
            # Additional preview-only portable path/duplicate/compression checks.
            if PureWindowsPath(name).drive or ":" in name or "\x00" in name:
                raise ValueError("Unsafe portable Pack path")
            canonical = PurePosixPath(name).as_posix().rstrip("/")
            if canonical.casefold() in seen:
                raise ValueError("Duplicate Pack entry")
            seen.add(canonical.casefold())
            if item.is_dir():
                continue
            if item.flag_bits & 1 or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise ValueError("Unsupported encrypted/compressed preview entry")
            # ZIP's CRC/header checks remain active; stream bound is independently
            # enforced rather than trusting directory file_size as the only cap.
            chunks = []
            with archive.open(item, "r") as handle:
                while True:
                    chunk = handle.read(min(65536, MAX_PACK_UNCOMPRESSED_BYTES - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_PACK_UNCOMPRESSED_BYTES:
                        raise ValueError("Pack stream exceeds expanded byte bound")
                    chunks.append(chunk)
            output[canonical] = b"".join(chunks)
    return output


def _cycle_reachable_ids(parents):
    """Iterative O(V+E) scan, including descendants whose ancestry reaches a cycle.

    Cache each completed path once. Preserve the original per-ID hold order and
    meaning without recursion or rescanning already completed ancestor chains.
    """
    resolved = {}
    for start in parents:
        if start in resolved:
            continue
        path, visiting = [], set()
        current = start
        while current and current in parents and current not in resolved and current not in visiting:
            path.append(current)
            visiting.add(current)
            current = parents[current]
        cyclic = current in visiting or resolved.get(current, False)
        for identity in path:
            resolved[identity] = cyclic
    return tuple(identity for identity in parents if resolved[identity])


def inspect_pack_preview(pack, *, source_reference: str, expected_sha256: str | None = None) -> PackPreview:
    """Inspect data-only partial patches; callers review holds without applying.

No hold-clearing API fabricates destination readiness. Review dispositions belong
to the consumer and must retain this immutable source evidence. No payload code
or imports are executed; no absent defaults or effective aliases are reconstructed.
"""
    _text(source_reference)
    raw = _read_input(pack)
    digest = sha256(raw).hexdigest()
    if expected_sha256 is not None:
        if not isinstance(expected_sha256, str) or re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256) is None:
            raise ValueError("Expected archive hash must be SHA-256")
        if digest != expected_sha256.lower():
            raise ValueError("Pack archive hash mismatch")
    members = _members(raw)
    if "manifest.json" not in members:
        raise ValueError("This ZIP does not contain a CollectiGen Knowledge Pack manifest.")
    manifest = _json(members["manifest.json"])
    if manifest.get("schema_version") == FOUNDATION_PACK_SCHEMA_VERSION or manifest.get("scope") == FOUNDATION_PACK_SCOPE or "foundation" in manifest:
        raise ValueError("Foundation default payloads require native Foundation review; partial user-patch preview is unsupported.")
    manifest = validate_manifest(manifest)
    schema = int(manifest.get("schema_version", 1) or 0)
    holds = [Hold("partial_patch_not_effective"), Hold("destination_review_required")]
    declared_scope = manifest.get("scope", "")
    if declared_scope not in {"", "user_layer_patch"}:
        holds.append(Hold("unsupported_declared_scope"))
        declared_scope = "unsupported"  # Never relay arbitrary manifest payload.
    boundary = manifest.get("content_rating_boundary", "")
    if boundary not in {"general", "sensitive", "questionable", "explicit"}:
        holds.append(Hold("missing_or_invalid_content_boundary"))
        boundary = ""
    default_fp = _fingerprint(manifest.get("default_relationship_fingerprint", ""))
    if default_fp != "missing":
        holds.append(Hold("default_baseline_unverified"))
    declared_files = manifest.get("files", None)
    if declared_files is not None:
        if not isinstance(declared_files, list) or any(not isinstance(item, str) for item in declared_files):
            raise ValueError("Invalid manifest file list")
        if len(declared_files) != len(set(declared_files)) or set(declared_files) != set(members) - {"manifest.json"}:
            holds.append(Hold("manifest_file_list_mismatch"))
    payloads, known_files = {}, {"manifest.json"}
    for _family, filename, _key, _disabled in SPECS:
        current, legacy = "data/user/" + filename, "data/" + filename
        if current in members and legacy in members:
            holds.append(Hold("competing_payload_paths", _family))
        # Preserve native fallback only for Tags/character-context families.
        allowed_legacy = filename in {"tags.json", "characters.json", "series.json", "outfits.json"}
        selected = current if current in members else legacy if allowed_legacy and legacy in members else None
        if selected:
            known_files.add(selected)
            payloads[filename] = _json(members[selected])
        else:
            payloads.setdefault(filename, {})
    family_rows, all_rows, count = [], {}, 0
    for family, filename, key, disabled_key in SPECS:
        payload = payloads[filename]
        rows = payload.get(key, [])
        if not is_record_list(rows):
            raise ValueError("Invalid " + family + " record list")
        disabled = _ids(payload.get(disabled_key, [])) if disabled_key else ()
        owned_values = payload.get("record_lifecycle_owned_ids", {}).get(key, []) if filename == "tags.json" and isinstance(payload.get("record_lifecycle_owned_ids", {}), dict) else payload.get("lifecycle_owned_ids", [])
        if filename == "tags.json" and not isinstance(payload.get("record_lifecycle_owned_ids", {}), dict):
            raise ValueError("Invalid grouped lifecycle ownership")
        owned = _ids(owned_values)
        if len(owned) > MAX_PACK_RECORDS:
            raise ValueError("Oversized lifecycle ownership")
        count += len(rows) + len(disabled)
        family_rows.append(FamilyAccounting(family, len(rows), disabled,
                                            tuple(_text(row.get("id")) for row in rows), owned))
        all_rows[family] = rows
        if rows and "schema_version" in payload:
            holds.append(Hold("payload_schema_requires_native_review", family))
        if (rows or disabled) and family not in {"tags", "categories"}:
            holds.append(Hold("unsupported_record_family", family))
        if disabled:
            holds.append(Hold("disabled_defaults_need_native_authority", family))
        if owned:
            holds.append(Hold("lifecycle_ownership_needs_native_authority", family))
        if family == "consumer_policies" and rows and schema < 9:
            holds.append(Hold("consumer_policies_require_schema_9", family))
        if schema < 8 and (rows or disabled) and family != "tags":
            holds.append(Hold("legacy_record_lifecycle_requires_native_review", family))
        ids = set()
        for row in rows:
            identity = _text(row.get("id"))
            if identity in ids:
                holds.append(Hold("duplicate_identity", family, identity))
            ids.add(identity)
    if count > MAX_PACK_RECORDS:
        raise ValueError("Pack exceeds non-relationship decision bound")

    def base(payload, key, identity):
        grouped = payload.get("record_base_fingerprints", {}) if key == "tags" else {}
        values = grouped.get(key, {}) if isinstance(grouped, dict) and key == "tags" else payload.get("base_fingerprints", {})
        if not isinstance(grouped, dict) or not isinstance(values, dict):
            raise ValueError("Invalid record base fingerprints")
        result = _fingerprint(values.get(identity, ""))
        if result != "missing":
            holds.append(Hold("record_baseline_unverified", key, identity))
        return result

    tags, categories, alias_count = [], [], 0
    for row in all_rows["tags"]:
        identity = _text(row.get("id"))
        sensitivity = row.get("content_sensitivity", {})
        if not isinstance(sensitivity, dict):
            raise ValueError("Invalid Tag sensitivity")
        rating = sensitivity.get("minimum_rating", "")
        status = sensitivity.get("classification_status", "unclassified")
        if rating not in {"", "general", "sensitive", "questionable", "explicit"}:
            raise ValueError("Invalid Tag minimum rating")
        status = _text(status)
        aliases = _ids(row.get("aliases", []))
        alias_count += len(aliases)
        if alias_count > MAX_PACK_RECORDS:
            raise ValueError("Oversized declared alias evidence")
        tags.append(TagPatch(identity, _text(row.get("display")), _text(row.get("category", ""), blank=True),
                             aliases, rating, status, base(payloads["tags.json"], "tags", identity)))
        if aliases:
            holds.append(Hold("declared_aliases_not_effective", "tags", identity))
        # Legacy semantic/projection fields are neither portable lookup authority
        # nor silently ignored semantics: report their need for native review.
        if any(row.get(key) for key in ("relations", "properties", "ordering")):
            holds.append(Hold("tag_semantics_need_native_review", "tags", identity))
    for row in all_rows["categories"]:
        identity = _text(row.get("id"))
        parent = "" if row.get("parent_id") is None else row.get("parent_id")
        categories.append(CategoryPatch(identity, _text(row.get("name")), _text(parent, blank=True),
                                        base(payloads["categories.json"], "categories", identity)))
        if any(row.get(key) for key in ("base", "semantic_role", "content_sensitivity", "properties")):
            holds.append(Hold("category_semantics_need_native_review", "categories", identity))
    tag_ids = {row.id for row in tags}
    category_ids = {row.id for row in categories}
    for row in tags:
        if row.category_id not in category_ids:
            holds.append(Hold("missing_reference", "tags", row.id, "category", row.category_id))
    for row in categories:
        if row.parent_id and row.parent_id not in category_ids:
            holds.append(Hold("missing_reference", "categories", row.id, "category", row.parent_id))
    names = {}
    for row in tags:
        for value in (row.display, *row.declared_aliases):
            key = normalize_term(value)
            previous = names.get(key)
            if previous is not None and previous != row.id:
                holds.append(Hold("name_or_alias_conflict", "tags", row.id, "tag", previous))
            names[key] = row.id
    category_names = {}
    for row in categories:
        key = (" ".join(row.label.casefold().split()), row.parent_id)
        previous = category_names.get(key)
        if previous is not None and previous != row.id:
            holds.append(Hold("category_name_conflict", "categories", row.id, "category", previous))
        category_names[key] = row.id
    parent_map = {row.id: row.parent_id for row in categories}
    for identity in _cycle_reachable_ids(parent_map):
        holds.append(Hold("category_cycle", "categories", identity))

    relation_file = "data/user/relationships.json"
    relation_payload = _json(members[relation_file]) if relation_file in members else {}
    if relation_file in members:
        known_files.add(relation_file)
    relation_rows = relation_payload.get("relationships", [])
    if not is_record_list(relation_rows) or len(relation_rows) > MAX_PACK_RELATIONSHIPS:
        raise ValueError("Invalid or oversized relationship patch list")
    relation_base = relation_payload.get("base_fingerprints", {})
    if not isinstance(relation_base, dict):
        raise ValueError("Invalid relationship base fingerprints")
    relationships, relation_ids, endpoint_count = [], set(), 0
    for row in relation_rows:
        identity, kind = _text(row.get("id")), _text(row.get("kind"))
        if identity in relation_ids:
            holds.append(Hold("duplicate_identity", "relationships", identity))
        relation_ids.add(identity)
        values = row.get("endpoints")
        if not is_record_list(values) or not 2 <= len(values) <= MAX_PACK_RECORDS:
            raise ValueError("Invalid relationship endpoints")
        endpoints = tuple(Endpoint(_text(item.get("kind")), _text(item.get("id"))) for item in values)
        endpoint_count += len(endpoints)
        if endpoint_count > MAX_PACK_ENDPOINTS:
            raise ValueError("Oversized relationship endpoint evidence")
        for field in ("directed", "reviewed", "disabled"):
            if not isinstance(row.get(field, {"directed": True, "reviewed": True, "disabled": False}[field]), bool):
                raise ValueError("Invalid relationship boolean")
        version = row.get("version", 1)
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("Invalid relationship version")
        fp = _fingerprint(relation_base.get(identity, ""))
        relationships.append(RelationshipPatch(identity, kind, endpoints, row.get("directed", True),
                                               row.get("reviewed", True), row.get("disabled", False), version, fp))
        # Preserve every relation as preview evidence. Full structural/semantic
        # validation belongs to the native validator and is never claimed here.
        holds.append(Hold("relationship_semantics_need_native_review", "relationships", identity))
        if fp != "missing":
            holds.append(Hold("relationship_baseline_unverified", "relationships", identity))
        if schema < 5:
            holds.append(Hold("typed_relationships_require_schema_5", "relationships", identity))
        for endpoint in endpoints:
            known = tag_ids if endpoint.kind == "tag" else category_ids if endpoint.kind in {"category", "modifier_dimension"} else None
            if known is not None and endpoint.id not in known:
                holds.append(Hold("missing_reference", "relationships", identity, endpoint.kind, endpoint.id))
            elif endpoint.kind not in {"tag", "category", "modifier_dimension", "alias_text"}:
                holds.append(Hold("unsupported_endpoint_semantics", "relationships", identity, endpoint.kind, endpoint.id))
    family_rows.append(FamilyAccounting("relationships", len(relationships), record_ids=tuple(row.id for row in relationships)))
    tags_payload = payloads["tags.json"]
    removed = _ids(tags_payload.get("removed_tag_ids", []))
    legacy = _ids(tags_payload.get("legacy_removed_tag_ids", []))
    redirect_values = tags_payload.get("merged_tag_ids", {})
    if not isinstance(redirect_values, dict):
        raise ValueError("Invalid redirect map")
    redirects = tuple((_text(key), _text(value)) for key, value in redirect_values.items())
    archive_rows = tags_payload.get("removed_tag_archives", [])
    if not is_record_list(archive_rows):
        raise ValueError("Invalid removed Tag archive list")
    if len(removed) + len(legacy) + len(redirects) + len(archive_rows) > MAX_PACK_RECORDS:
        raise ValueError("Oversized lifecycle evidence")
    if removed or legacy or redirects or archive_rows:
        holds.append(Hold("removal_redirect_semantics_need_native_review", "tags"))
    for identity in removed:
        if identity in tag_ids:
            holds.append(Hold("active_removed_conflict", "tags", identity))
    for donor, target in redirects:
        if donor in tag_ids:
            holds.append(Hold("active_redirect_conflict", "tags", donor))
        if target not in tag_ids:
            holds.append(Hold("missing_reference", "tags", donor, "tag", target))
    unknown = len(set(members) - known_files)
    if unknown:
        holds.append(Hold("unrecognized_files_not_interpreted"))
    # Immutable source ledger; review UI may record separate dispositions.
    return PackPreview(Attribution(source_reference, digest, schema, declared_scope, boundary, default_fp),
                       tuple(tags), tuple(categories), tuple(relationships), tuple(family_rows), removed, legacy,
                       redirects, len(archive_rows), unknown, tuple(dict.fromkeys(holds)))
