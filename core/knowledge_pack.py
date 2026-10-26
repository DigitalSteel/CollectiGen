from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
import json
import os
import shutil
import stat
import tempfile
import zipfile
from datetime import datetime,timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4
from .automation_rule_conversion import RulePairAdmission
from .default_catalog_release import ApprovedDefaultPackRelease, require_previous, stage_release_state, release_records
import sys
from .catalog_admission import (
    ENROLLED_PACK_RECEIPT, catalog_admission, require_catalog_admission,
    checkpoint_catalog_admission,
)
from suite_core.knowledge.pack_container import (
    checked_archive_entries as _shared_checked_archive_entries,
    extract_pack_safely as _shared_extract_pack_safely,
    validate_manifest as _shared_validate_manifest,
    is_record_list as _shared_is_record_list,
    validate_json_depth, FOUNDATION_PACK_SCHEMA_VERSION, FOUNDATION_PACK_SCOPE,
    MAX_FOUNDATION_MANIFEST_BYTES,
)

from .knowledge_relationships import (
    ALLOWED_DIMENSION,
    ALLOWED_DIMENSION_STATE,
    ASSOCIATED_CHARACTER,
    CHARACTER_SOURCE,
    CURRENT_RELATIONSHIP_KINDS,
    ENTITY_TAG,
    IDENTITY_ALIAS,
    MODIFIER_ROLE,
    MODIFIER_VALUE,
    NO_ALLOWED_DIMENSIONS,
    VERSION_OF,
    KnowledgeRelationship,
    RelationshipEndpoint,
)
from .relationship_workspace import relationship_fingerprint
from .relationship_store import RelationshipStore, validate_relationships
from .category_registry import CategoryRecord, CategoryRegistry
from .category_base_relationships import (
    category_base_compatibility_payload,
    category_base_relationship_index,
)
from .allowed_dimension_policy import (
    publish_allowed_dimension_storage_authority,
)
from .knowledge_engine import FoundationKnowledge
from .modifier_composition import (
    publish_modifier_composition_storage_authority,
)
from .reusable_modifier_projection import reusable_modifier_compatibility_index
from .tag_semantic_relationships import (
    publish_tag_semantic_authority,
    tag_semantic_relationship_index,
)
from .scoped_constraint_projection import (
    publish_scoped_constraint_storage_authority,
    scoped_constraint_compatibility_index,
)
from .tag_relationship_compatibility import tag_relationship_compatibility_payload
from .character_database import CharacterRecord, OutfitRecord, SeriesRecord
from .character_projection_storage import (
    CHARACTER_STORAGE_SCHEMA_VERSION,
    OUTFIT_STORAGE_SCHEMA_VERSION,
    SERIES_STORAGE_SCHEMA_VERSION,
    character_record_compatibility_payloads,
    character_record_layered_compatibility_payloads,
    character_record_storage_payloads,
)
from .pattern_registry import PatternRecord
from .tag_database import CONTENT_RATINGS, MergeRule, TagDatabase, TagRecord
from .tag_alias_relationships import (
    publish_tag_alias_storage_authority,
    tag_alias_compatibility_index, tag_alias_compatibility_payload,
)
from .default_sync import load_effective_tag_defaults
from .removed_tag_pack import (
    REMOVAL_HOLD,
    KnowledgePackRemovalDecision,
    parse_portable_removed_archives,
    portable_removed_archive_payload,
    prepare_removed_archive_import,
)
from .consumer_policies import (
    COMPATIBILITY_TERM,
    CONSUMER_DEFAULT,
    EXACT_RELATIONSHIP,
    EXACT_TAG,
    RELATIONSHIP_KIND,
    ConsumerPolicyRecord,
    ConsumerPolicyStore,
    consumer_policy_record_id,
)


MAX_PACK_FILES = 64
MAX_PACK_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
# Explicit Foundation defaults have their own bounded envelope; ordinary user
# archives remain50MiB. The schema10 manifest has a separate128KiB metadata cap.
MAX_FOUNDATION_BYTES = 64 * 1024 * 1024
MAX_PACK_RELATIONSHIPS = 40_000
MAX_PACK_RECORDS = 20_000
PACK_RECORDS = (
    ("tags", "data/user/tags.json", "data/tags.json", "tags"),
    ("merge_rules", "data/user/tags.json", "data/tags.json", "merge_rules"),
    ("characters", "data/user/characters.json", "data/characters.json", "characters"),
    ("series", "data/user/series.json", "data/series.json", "series"),
    ("outfits", "data/user/outfits.json", "data/outfits.json", "outfits"),
    ("categories", "data/user/categories.json", None, "categories"),
    ("patterns", "data/user/patterns.json", None, "patterns"),
    ("consumer_policies", "data/user/consumer_policies.json", None, "policies"),
)
RECORD_KIND_BY_LABEL = {
    "tags": "tag",
    "merge_rules": "merge_rule",
    "categories": "category",
    "patterns": "pattern",
    "series": "series",
    "characters": "character",
    "outfits": "outfit",
    "consumer_policies": "consumer_policy",
    "policies": "consumer_policy",
}
RECORD_STATE_KEY = {
    "tag": "tags", "merge_rule": "merge_rules", "category": "categories",
    "pattern": "patterns", "series": "series", "character": "characters",
    "outfit": "outfits",
    "consumer_policy": "consumer_policies",
}
RECORD_DISABLED_KEY = {
    "merge_rule": "disabled_default_merge_rule_ids",
    "category": "disabled_default_ids",
    "pattern": "disabled_default_ids",
    "series": "disabled_default_ids",
    "character": "disabled_default_ids",
    "outfit": "disabled_default_ids",
}
RELATIONSHIPS_RELATIVE = "data/user/relationships.json"
DEFAULT_RELATIONSHIPS_RELATIVE = "data/defaults/relationships.json"
SUPPORTED_PACK_SCHEMA_VERSION = 9
RELATIONSHIP_HOLD = "hold"
SCHEMA_5_RELATIONSHIP_KINDS = frozenset(
    {MODIFIER_ROLE, MODIFIER_VALUE, ALLOWED_DIMENSION}
)
PORTABLE_RELATIONSHIP_KINDS = CURRENT_RELATIONSHIP_KINDS
CONTENT_RATING_BOUNDARIES = tuple(CONTENT_RATINGS)
CONTENT_RATING_ORDER = {
    rating: index for index, rating in enumerate(CONTENT_RATING_BOUNDARIES)
}


@dataclass(frozen=True)
class KnowledgePackRelationshipDecision:
    stable_id: str
    kind: str
    label: str
    outcome: str
    reason: str


@dataclass(frozen=True)
class KnowledgePackRecordDecision:
    stable_id: str
    kind: str
    label: str
    outcome: str
    reason: str
    consequence: str = ""


@dataclass(frozen=True)
class KnowledgePackContentFinding:
    stable_id: str
    label: str
    rating: str
    basis: str


@dataclass(frozen=True)
class KnowledgePackRuleEffect:
    stable_id: str
    label: str
    action: str
    consequence: str


@dataclass(frozen=True)
class KnowledgePackExportPlan:
    authority_fingerprint: str
    content_rating_boundary: str
    summary: tuple[tuple[str, int], ...]
    content_findings: tuple[KnowledgePackContentFinding, ...] = ()
    rule_effects: tuple[KnowledgePackRuleEffect, ...] = ()
    issues: tuple[str, ...] = ()

    @property
    def can_export(self) -> bool:
        return not self.issues and bool(sum(value for _key, value in self.summary))

    def summary_dict(self) -> dict[str, int]:
        return dict(self.summary)


@dataclass(frozen=True)
class KnowledgePackRecordPatch:
    kind: str
    filename: str
    key: str
    stable_id: str
    local_id: str
    payload: dict[str, Any] | None
    disabled_key: str = ""
    source_id: str = ""


@dataclass(frozen=True)
class FoundationProposal:
    """Explicit complete source inventory; never inferred from Pack filenames.

    Payloads are exact approved default bytes, not portable user patches. The
    native review binds them to recipient identity, epoch and before evidence.
    """
    version: int
    source_reference: str
    source_inventory: tuple[tuple[str, str], ...]
    payloads: tuple[tuple[str, bytes], ...]

    def __post_init__(self):
        if type(self.version) is not int or self.version != 1:
            raise ValueError("Unsupported Foundation proposal version")
        if type(self.source_reference) is not str or not self.source_reference.strip() or len(self.source_reference) > 8192:
            raise ValueError("Explicit bounded Foundation source reference required")
        if type(self.payloads) is not tuple or not 0 < len(self.payloads) <= MAX_PACK_FILES:
            raise ValueError("Bounded immutable Foundation payload inventory required")
        names, inventory, total = set(), [], 0
        for row in self.payloads:
            if type(row) is not tuple or len(row) != 2:
                raise ValueError("Malformed Foundation payload")
            name, raw = row
            _foundation_relative(name)
            if name.casefold() in names or type(raw) is not bytes:
                raise ValueError("Duplicate or mutable Foundation payload")
            if len(raw) > MAX_PACK_UNCOMPRESSED_BYTES:
                raise ValueError("Foundation participant exceeds native recovery byte budget")
            names.add(name.casefold())
            total += len(raw)
            if total > MAX_FOUNDATION_BYTES:
                raise ValueError("Foundation proposal exceeds native byte budget")
            _foundation_json(raw)
            inventory.append((name, sha256(raw).hexdigest()))
        if type(self.source_inventory) is not tuple or self.source_inventory != tuple(sorted(inventory)):
            raise ValueError("Foundation bytes differ from the exact source inventory")


def _foundation_relative(name):
    if (type(name) is not str or not name.startswith("data/defaults/")
            or name != "data/defaults/" + Path(name).name
            or not name.endswith(".json") or any(c in name for c in "\\:")):
        raise ValueError("Foundation requires a canonical default JSON participant")
    return name


def _foundation_json(raw):
    validate_json_depth(raw)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate Foundation JSON field")
            result[key] = value
        return result
    def nonfinite(_value):
        raise ValueError("Nonfinite Foundation JSON value")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=nonfinite)
    if type(value) is not dict:
        raise ValueError("Foundation participant must contain a JSON object")
    return value


@dataclass(frozen=True)
class FoundationPack:
    """Decoded input, not public clearance or recipient mutation approval."""
    proposal: FoundationProposal
    approved_release: ApprovedDefaultPackRelease
    display_version: str
    content_rating_boundary: str


def _foundation_display(value):
    from .component_release_identity import ComponentReleaseIdentity
    if type(value) is not str:
        raise ValueError("Explicit Foundation display identity required")
    prefix = value[:2]
    maturity = {"a.": "alpha", "b.": "beta"}.get(prefix, "stable")
    identity = ComponentReleaseIdentity(value[2:] if maturity != "stable" else value, maturity)
    if identity.display_version != value:
        raise ValueError("Noncanonical Foundation display identity")
    return identity


def _pack_input_manifest(pack_path):
    _check_export_path(Path(pack_path))
    if Path(pack_path).stat().st_size > MAX_FOUNDATION_BYTES + MAX_FOUNDATION_MANIFEST_BYTES + 2 * 1024 * 1024:
        raise ValueError("Foundation archive exceeds its input byte bound")
    with zipfile.ZipFile(pack_path) as archive:
        _shared_checked_archive_entries(archive, max_files=MAX_PACK_FILES,
            max_uncompressed_bytes=MAX_FOUNDATION_BYTES + MAX_FOUNDATION_MANIFEST_BYTES,
            max_member_bytes=MAX_PACK_UNCOMPRESSED_BYTES)
        rows = [item for item in archive.infolist() if item.filename == "manifest.json"]
        if len(rows) != 1 or rows[0].file_size > MAX_FOUNDATION_MANIFEST_BYTES:
            raise ValueError("Foundation/Knowledge Pack requires one bounded manifest")
        with archive.open(rows[0]) as handle:
            raw = handle.read(MAX_FOUNDATION_MANIFEST_BYTES + 1)
        if len(raw) > MAX_FOUNDATION_MANIFEST_BYTES:
            raise ValueError("Knowledge Pack manifest exceeds its byte bound")
        return _foundation_json(raw)


def load_foundation_pack(pack_path) -> FoundationPack:
    """Reconstruct typed Foundation input through the existing bounded ZIP reader.

    Release metadata is input evidence. The planner still requires the caller's
    explicitly approved descriptor, enrolled recipient and reviewed before state.
    """
    manifest = _pack_input_manifest(pack_path)
    _shared_validate_manifest(manifest, allow_foundation=True)
    if manifest.get("schema_version") != FOUNDATION_PACK_SCHEMA_VERSION or manifest.get("scope") != FOUNDATION_PACK_SCOPE:
        raise ValueError("Explicit Foundation mode required")
    source_state = _export_path_state(Path(pack_path))
    if set(manifest) != {"format", "schema_version", "scope", "files", "content_rating_boundary", "foundation"}:
        raise ValueError("Unknown/missing Foundation manifest authority fields")
    mode = manifest["foundation"]
    if (type(mode) is not dict or set(mode) != {"version", "source_reference", "source_inventory", "approved_release", "display_version"}
            or type(mode["version"]) is not int or mode["version"] != 1):
        raise ValueError("Unsupported Foundation wire mode")
    _foundation_display(mode["display_version"])
    if manifest["content_rating_boundary"] not in CONTENT_RATING_BOUNDARIES:
        raise ValueError("Foundation requires an explicit content-rating boundary")
    inventory = mode["source_inventory"]
    if (type(inventory) is not list or not inventory or len(inventory) > MAX_PACK_FILES - 1
            or any(type(row) is not list or len(row) != 2 for row in inventory)):
        raise ValueError("Malformed Foundation wire inventory")
    inventory = tuple(tuple(row) for row in inventory)
    for name, digest in inventory:
        _foundation_relative(name)
        if type(digest) is not str or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("Malformed Foundation payload fingerprint")
    if manifest["files"] != [name for name, _digest in inventory]:
        raise ValueError("Foundation manifest membership differs from its source inventory")
    approved = ApprovedDefaultPackRelease.decode(json.dumps(mode["approved_release"]))
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        with zipfile.ZipFile(pack_path) as archive:
            expected = {"manifest.json", *manifest["files"]}
            if len(archive.infolist()) != len(expected) or set(archive.namelist()) != expected:
                raise ValueError("Foundation archive membership differs from its manifest")
            _extract_pack_safely(archive, work, foundation=True)
        if _foundation_json((work / "manifest.json").read_bytes()) != manifest:
            raise RuntimeError("Foundation manifest changed during loading")
        rows = tuple((name, (work / name).read_bytes()) for name, _digest in inventory)
        proposal = FoundationProposal(mode["version"], mode["source_reference"], inventory, rows)
    if _export_path_state(Path(pack_path)) != source_state:
        raise RuntimeError("Foundation archive changed during loading")
    return FoundationPack(proposal, approved, mode["display_version"], manifest["content_rating_boundary"])


def _maybe_foundation_pack(pack_path):
    """Probe only the existing manifest; ordinary packs keep their native route."""
    with zipfile.ZipFile(pack_path) as archive:
        rows = [row for row in archive.infolist() if row.filename == "manifest.json"]
        if len(rows) != 1 or rows[0].file_size > MAX_FOUNDATION_MANIFEST_BYTES:
            return None  # Ordinary validation still rejects unsupported schema/mode.
        raw = archive.read(rows[0])
        validate_json_depth(raw)
        manifest = json.loads(raw)
    if type(manifest) is dict and (manifest.get("schema_version") == FOUNDATION_PACK_SCHEMA_VERSION
            or manifest.get("scope") == FOUNDATION_PACK_SCOPE or "foundation" in manifest):
        return load_foundation_pack(pack_path)
    return None


def _same_foundation_release_approval(left, right):
    """Wire release/source approval is immutable; predecessor is recipient state.

    The native planner still requires the exact installed predecessor (or the
    already committed release/source for repeat). Never use a wire predecessor
    as authority to replace that admission or change source/metadata bytes.
    """
    return replace(left, previous_release_fingerprint=None) == replace(right, previous_release_fingerprint=None)


def export_foundation_pack(output, *, proposal, approved_release, display_version, content_rating_boundary="explicit"):
    """Produce a local candidate from explicitly supplied reviewed/synthetic input.

    No classifier, publication, signing or allocation decision is inferred here.
    This refuses replacement and verifies a loader round-trip before publishing.
    """
    if type(proposal) is not FoundationProposal or type(approved_release) is not ApprovedDefaultPackRelease:
        raise TypeError("Typed Foundation proposal and approved release required")
    proposal.__post_init__()
    _foundation_display(display_version)
    if content_rating_boundary not in CONTENT_RATING_BOUNDARIES:
        raise ValueError("Explicit Foundation content boundary required")
    manifest = {"format": "tagmanager_knowledge_pack", "schema_version": FOUNDATION_PACK_SCHEMA_VERSION,
        "scope": FOUNDATION_PACK_SCOPE, "files": [name for name, _digest in proposal.source_inventory],
        "content_rating_boundary": content_rating_boundary, "foundation": {"version": proposal.version,
        "source_reference": proposal.source_reference, "source_inventory": proposal.source_inventory,
        "approved_release": json.loads(approved_release.encode()), "display_version": display_version}}
    raw = (json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    if len(raw) > MAX_FOUNDATION_MANIFEST_BYTES or len(proposal.payloads) >= MAX_PACK_FILES:
        raise ValueError("Foundation manifest/file count exceeds its byte bound")
    output = Path(output).absolute()
    _check_export_path(output)
    if _export_path_state(output) is not None:
        raise FileExistsError("Foundation candidate destination already exists")
    parent = _export_parent_identity(output.parent)
    with tempfile.TemporaryDirectory(prefix=".foundation-", dir=output.parent) as temporary:
        staged = Path(temporary) / "candidate.zip"
        with zipfile.ZipFile(staged, "x", zipfile.ZIP_DEFLATED) as archive:
            for name, payload in (("manifest.json", raw), *tuple(sorted(proposal.payloads))):
                entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, payload)
        with staged.open("r+b") as handle:
            os.fsync(handle.fileno())
        expected = FoundationPack(replace(proposal, payloads=tuple(sorted(proposal.payloads))),
                                  approved_release, display_version, content_rating_boundary)
        if load_foundation_pack(staged) != expected:
            raise RuntimeError("Foundation candidate failed exact loader round-trip")
        if _export_parent_identity(output.parent) != parent or _export_path_state(output) is not None:
            raise RuntimeError("Foundation candidate destination changed during production")
        if os.name == "nt":
            os.rename(staged, output)
        else:
            os.link(staged, output)
    return output


@dataclass(frozen=True)
class KnowledgePackImportPlan:
    pack_path: str
    pack_fingerprint: str
    authority_fingerprint: str
    schema_version: int
    content_rating_boundary: str
    summary: tuple[tuple[str, int], ...]
    relationship_decisions: tuple[KnowledgePackRelationshipDecision, ...]
    record_decisions: tuple[KnowledgePackRecordDecision, ...] = ()
    removal_decisions: tuple[KnowledgePackRemovalDecision, ...] = ()
    record_patches: tuple[KnowledgePackRecordPatch, ...] = ()
    imported_relationships: tuple[KnowledgeRelationship, ...] = ()
    imported_removal_archives: tuple[dict[str, Any], ...] = ()
    removal_tag_remap: tuple[tuple[str, str], ...] = ()
    removal_category_remap: tuple[tuple[str, str], ...] = ()
    authority_fingerprints: tuple[tuple[str, str], ...] = ()
    issues: tuple[str, ...] = ()
    content_findings: tuple[KnowledgePackContentFinding, ...] = ()
    consumer_policy_extras: tuple[tuple[str, Any], ...] = ()
    catalog_identity_binding: tuple[tuple[str, Any], ...] = ()
    catalog_receipt_epoch: str = ""
    approved_default_release_json: str = ""
    foundation_proposal: FoundationProposal | None = None
    foundation_default_tags: tuple[tuple[Any, ...], ...] = ()
    foundation_default_categories: tuple[tuple[str, str, str], ...] = ()
    foundation_effective_tags: tuple[tuple[Any, ...], ...] = ()

    @property
    def can_apply(self) -> bool:
        return not self.issues and all(
            item.outcome != RELATIONSHIP_HOLD
            for item in self.relationship_decisions
        ) and all(
            item.outcome != RELATIONSHIP_HOLD for item in self.record_decisions
        ) and all(
            item.outcome != REMOVAL_HOLD for item in self.removal_decisions
        )

    def summary_dict(self) -> dict[str, int]:
        return dict(self.summary)


def _file_fingerprint(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest() if path.exists() else "missing"


def _paths_fingerprint(paths) -> str:
    digest = sha256(b"collectigen-knowledge-pack-authority-v1\0")
    for path in sorted(
        {Path(value) for value in paths}, key=lambda value: str(value).casefold()
    ):
        digest.update(
            str(path.resolve()).casefold().encode("utf-8", errors="surrogatepass")
        )
        digest.update(b"\0")
        digest.update(_file_fingerprint(path).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _relationship_fingerprint(
    value: dict[str, Any] | KnowledgeRelationship,
) -> str:
    relationship = (
        value
        if isinstance(value, KnowledgeRelationship)
        else KnowledgeRelationship.from_dict(value)
    )
    payload = relationship.to_dict()
    payload.pop("layer", None)
    return sha256(
        json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _relationship_semantic_key(
    relationship: KnowledgeRelationship,
) -> tuple[Any, ...]:
    """Return the stable meaning boundary used to detect duplicate authority."""
    endpoints = tuple((item.kind, item.id) for item in relationship.endpoints)
    if not relationship.directed:
        endpoints = tuple(sorted(endpoints))
    return (
        relationship.kind,
        relationship.directed,
        relationship.scope,
        endpoints,
    )


def _relationship_content_fingerprint(
    relationship: KnowledgeRelationship,
) -> str:
    """Compare exact reviewed content while ignoring portable record identity."""
    payload = relationship.to_dict()
    payload.pop("id", None)
    payload.pop("layer", None)
    payload.pop("label", None)
    for endpoint in payload.get("endpoints", ()):
        if isinstance(endpoint, dict):
            endpoint.pop("label", None)
    if not relationship.directed:
        payload["endpoints"] = sorted(
            payload.get("endpoints", ()),
            key=lambda item: (str(item.get("kind") or ""), str(item.get("id") or "")),
        )
    return sha256(
        json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _atomic_bytes(path: Path, payload: bytes) -> None:
    checkpoint_catalog_admission()
    _check_export_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _check_export_path(path)
        os.replace(temporary, path)
        checkpoint_catalog_admission()
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_bytes(
        path,
        (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _extract_pack_safely(archive: zipfile.ZipFile, target: Path, *, foundation=False) -> None:
    """Native wrapper retains caller limits and extraction behavior."""
    _shared_extract_pack_safely(archive, target, max_files=MAX_PACK_FILES,
        max_uncompressed_bytes=MAX_FOUNDATION_BYTES + MAX_FOUNDATION_MANIFEST_BYTES if foundation else MAX_PACK_UNCOMPRESSED_BYTES,
        max_member_bytes=MAX_PACK_UNCOMPRESSED_BYTES if foundation else None)


def _load(path: Path, key: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [item for item in data.get(key, []) if isinstance(item, dict)]


def _default_tag_identities(app_root: Path) -> tuple[set[str], set[str], set[str]]:
    """Return shipped tag/rule IDs and tag names that must not enter user packs."""
    tag_ids: set[str] = set()
    tag_names: set[str] = set()
    rule_ids: set[str] = set()
    defaults = app_root / "data" / "defaults"
    if not defaults.exists():
        return tag_ids, tag_names, rule_ids
    for path in defaults.glob("*tags.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for item in payload.get("tags", []):
            if not isinstance(item, dict):
                continue
            if item.get("id"):
                tag_ids.add(str(item["id"]))
            if item.get("display"):
                tag_names.add(" ".join(str(item["display"]).casefold().split()))
            tag_names.update(" ".join(str(value).casefold().split()) for value in item.get("aliases",[]) if str(value).strip())
        for item in payload.get("merge_rules", []):
            if isinstance(item, dict) and item.get("id"):
                rule_ids.add(str(item["id"]))
    return tag_ids, tag_names, rule_ids


def _portable_record(kind: str, item: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(json.dumps(item))
    value.pop("promotion", None)
    value.pop("import_alternatives", None)
    if kind == "tag":
        value["seen_count"] = 0
        value["first_seen"] = None
        value["last_seen"] = None
        value["provenance"] = []
    return value


def _canonical_record(kind: str, item: dict[str, Any]) -> dict[str, Any]:
    """Expand schema defaults so serialized and compact records compare equally."""
    if kind == "tag":
        return TagRecord.from_dict(item).to_dict()
    if kind == "merge_rule":
        return MergeRule.from_dict(item).to_dict()
    if kind == "category":
        return CategoryRecord.from_dict(item, source="comparison").to_dict()
    if kind == "pattern":
        return PatternRecord.from_dict(item, source="comparison").to_dict()
    if kind == "series":
        known = {"id", "name", "aliases", "original_style_tags", "knowledge_tag"}
        return SeriesRecord(
            id=str(item.get("id") or ""),
            name=str(item.get("name") or item.get("id") or ""),
            aliases=[str(value) for value in item.get("aliases", ())],
            original_style_tags=[
                str(value) for value in item.get("original_style_tags", ())
            ],
            knowledge_tag=(
                dict(item.get("knowledge_tag") or {})
                if isinstance(item.get("knowledge_tag"), dict) else {}
            ),
            extras={key: value for key, value in item.items() if key not in known},
        ).to_dict()
    if kind == "character":
        known = {
            "id", "name", "series_id", "aliases", "default_tags", "sdxl_tags",
            "plain_language", "original_style_tags", "identity_type", "origin_label",
            "creator", "tag_associations", "base_character_id", "variant_type",
            "knowledge_tag",
        }
        default_tags = [str(value) for value in item.get("default_tags", ())]
        return CharacterRecord(
            id=str(item.get("id") or ""),
            name=str(item.get("name") or item.get("id") or ""),
            series_id=str(item["series_id"]) if item.get("series_id") else None,
            aliases=[str(value) for value in item.get("aliases", ())],
            default_tags=default_tags,
            sdxl_tags=[
                str(value) for value in item.get("sdxl_tags", default_tags)
            ],
            plain_language=[str(value) for value in item.get("plain_language", ())],
            original_style_tags=[
                str(value) for value in item.get("original_style_tags", ())
            ],
            identity_type=str(
                item.get("identity_type")
                or ("canonical" if item.get("series_id") else "unknown")
            ),
            origin_label=str(item.get("origin_label") or ""),
            creator=str(item.get("creator") or ""),
            tag_associations=[
                dict(value) for value in item.get("tag_associations", ())
                if isinstance(value, dict)
            ],
            base_character_id=(
                str(item["base_character_id"])
                if item.get("base_character_id") else None
            ),
            variant_type=str(item.get("variant_type") or ""),
            knowledge_tag=(
                dict(item.get("knowledge_tag") or {})
                if isinstance(item.get("knowledge_tag"), dict) else {}
            ),
            extras={key: value for key, value in item.items() if key not in known},
        ).to_dict()
    if kind == "outfit":
        known = {
            "id", "name", "character_ids", "series_id", "aliases", "sdxl_tags",
            "plain_language", "style_tags", "notes", "knowledge_tag",
        }
        return OutfitRecord(
            id=str(item.get("id") or ""),
            name=str(item.get("name") or item.get("id") or ""),
            character_ids=[str(value) for value in item.get("character_ids", ())],
            series_id=str(item["series_id"]) if item.get("series_id") else None,
            aliases=[str(value) for value in item.get("aliases", ())],
            sdxl_tags=[str(value) for value in item.get("sdxl_tags", ())],
            plain_language=[str(value) for value in item.get("plain_language", ())],
            style_tags=[str(value) for value in item.get("style_tags", ())],
            notes=str(item.get("notes") or ""),
            knowledge_tag=(
                dict(item.get("knowledge_tag") or {})
                if isinstance(item.get("knowledge_tag"), dict) else {}
            ),
            extras={key: value for key, value in item.items() if key not in known},
        ).to_dict()
    if kind == "consumer_policy":
        return ConsumerPolicyRecord.from_dict(item).to_dict()
    return json.loads(json.dumps(item))


def _record_comparable(
    kind: str, item: dict[str, Any], *, ignore_id: bool = False
) -> dict[str, Any]:
    value = _canonical_record(kind, _portable_record(kind, item))
    if ignore_id:
        value.pop("id", None)

    def prune(current: Any) -> Any:
        if isinstance(current, dict):
            return {
                key: clean
                for key, child in current.items()
                if (clean := prune(child)) not in (None, "", [], {})
            }
        if isinstance(current, list):
            return [
                clean for child in current
                if (clean := prune(child)) not in (None, "", [], {})
            ]
        return current

    return prune(value)


def _record_fingerprint(kind: str, item: dict[str, Any]) -> str:
    return sha256(
        json.dumps(
            _record_comparable(kind, item),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _default_records_for_kind(app_root: Path, kind: str) -> list[dict[str, Any]]:
    defaults = app_root / "data" / "defaults"
    if kind == "tag":
        return [
            json.loads(json.dumps(item))
            for item in load_effective_tag_defaults(defaults).tags
        ]
    paths_and_keys = {
        "merge_rule": (("promoted_tags.json", "merge_rules"),),
        "category": (("categories.json", "categories"),),
        "pattern": (("patterns.json", "patterns"),),
        "series": (("series.json", "series"),),
        "character": (("characters.json", "characters"),),
        "outfit": (("outfits.json", "outfits"),),
        "consumer_policy": (("consumer_policies.json", "policies"),),
    }
    return [
        json.loads(json.dumps(item))
        for filename, key in paths_and_keys.get(kind, ())
        for item in _load(defaults / filename, key)
    ]


def _matching_default_record(
    item: dict[str, Any], defaults: list[dict[str, Any]], key: str
) -> dict[str, Any] | None:
    stable_id = str(item.get("id") or "")
    exact = next(
        (value for value in defaults if str(value.get("id") or "") == stable_id),
        None,
    )
    if exact is not None:
        return exact
    identities = _record_identities(item, key)
    matches = [
        value for value in defaults
        if identities.intersection(_record_identities(value, key))
    ]
    return matches[0] if len(matches) == 1 else None


def _portable_record_collection(
    app_root: Path,
    *,
    filename: str,
    key: str,
    kind: str,
) -> dict[str, Any]:
    user_path = app_root / "data" / "user" / filename
    payload = json.loads(user_path.read_text(encoding="utf-8")) if user_path.exists() else {}
    defaults = _default_records_for_kind(app_root, kind)
    lifecycle_owned_ids: set[str] = set()
    tags_path = app_root / "data" / "user" / "tags.json"
    if kind in {"tag", "category"} and tags_path.exists():
        tags_payload = _read_payload_checked(tags_path)
        lifecycle_key = "related_tags_after" if kind == "tag" else "categories_after"
        lifecycle_owned_ids = {
            str(stable_id)
            for archive in tags_payload.get("removed_tag_archives", ())
            if isinstance(archive, dict) and not str(archive.get("restored_at") or "")
            for stable_id in (
                archive.get(lifecycle_key, {})
                if isinstance(archive.get(lifecycle_key), dict) else {}
            )
        }
    records: list[dict[str, Any]] = []
    base_fingerprints: dict[str, str] = {}
    for item in payload.get(key, ()):
        if not isinstance(item, dict) or not str(item.get("id") or ""):
            continue
        portable = _portable_record(kind, item)
        shipped = _matching_default_record(portable, defaults, key)
        if shipped is not None and _record_comparable(
            kind, portable, ignore_id=True
        ) == _record_comparable(kind, shipped, ignore_id=True):
            continue
        stable_id = str(portable["id"])
        records.append(portable)
        base_fingerprints[stable_id] = (
            _record_fingerprint(kind, shipped) if shipped is not None else "missing"
        )
    result: dict[str, Any] = {
        "schema_version": payload.get("schema_version", 1),
        key: records,
        "base_fingerprints": dict(sorted(base_fingerprints.items())),
        "lifecycle_owned_ids": sorted(
            stable_id for stable_id in lifecycle_owned_ids
            if stable_id in {str(item.get("id") or "") for item in records}
        ),
    }
    if kind == "consumer_policy":
        result["policy_version"] = int(payload.get("policy_version", 1) or 1)
        result["layer"] = "user"
        for extra_key, extra_value in payload.items():
            if extra_key not in {
                "schema_version", "policy_version", "layer", key,
                "base_fingerprints", "lifecycle_owned_ids",
            }:
                result.setdefault(extra_key, json.loads(json.dumps(extra_value)))
    disabled_key = RECORD_DISABLED_KEY.get(kind, "")
    if disabled_key:
        default_ids = {
            str(item.get("id") or "") for item in defaults if str(item.get("id") or "")
        }
        disabled = sorted({
            str(value) for value in payload.get(disabled_key, ())
            if str(value) in default_ids
        })
        result[disabled_key] = disabled
        for stable_id in disabled:
            shipped = next(
                item for item in defaults if str(item.get("id") or "") == stable_id
            )
            result["base_fingerprints"].setdefault(
                stable_id, _record_fingerprint(kind, shipped)
            )
    return result


def _portable_user_tags(app_root: Path) -> dict[str, Any]:
    source = app_root / "data" / "user" / "tags.json"
    payload = json.loads(source.read_text(encoding="utf-8")) if source.exists() else {}
    tag_patch = _portable_tag_records(app_root)
    rule_patch = _portable_record_collection(
        app_root, filename="tags.json", key="merge_rules", kind="merge_rule"
    )
    removed_archives,legacy_removed_ids=portable_removed_archive_payload(app_root)
    return {
        "schema_version": payload.get("schema_version", TagDatabase.SCHEMA_VERSION),
        "tags": tag_patch["tags"],"merge_rules": rule_patch["merge_rules"],
        "record_base_fingerprints": {
            "tags": tag_patch["base_fingerprints"],
            "merge_rules": rule_patch["base_fingerprints"],
        },
        "record_lifecycle_owned_ids": {
            "tags": tag_patch.get("lifecycle_owned_ids", []),
            "merge_rules": rule_patch.get("lifecycle_owned_ids", []),
        },
        "disabled_default_merge_rule_ids": rule_patch.get(
            "disabled_default_merge_rule_ids", []
        ),
        "removed_tag_ids":[str(value) for value in payload.get("removed_tag_ids",[]) if str(value).strip()],
        "removed_tag_archives":[
            item for item in removed_archives
        ],
        "legacy_removed_tag_ids":list(legacy_removed_ids),
        "merged_tag_ids":{
            str(source):str(target)
            for source,target in dict(payload.get("merged_tag_ids") or {}).items()
            if str(source).strip() and str(target).strip()
        },
    }


def _portable_tag_records(app_root: Path) -> dict[str, Any]:
    """Export schema-v10 Tags with explicit relationship compatibility views."""

    payload = _portable_record_collection(
        app_root, filename="tags.json", key="tags", kind="tag",
    )
    effective = dict(
        RelationshipStore(
            app_root / DEFAULT_RELATIONSHIPS_RELATIVE
        ).relationships
    )
    effective.update(
        RelationshipStore(app_root / RELATIONSHIPS_RELATIVE).relationships
    )
    semantic_index = tag_semantic_relationship_index(effective.values())
    alias_index = tag_alias_compatibility_index(effective.values())
    constraint_index = scoped_constraint_compatibility_index(effective.values())
    tags = TagDatabase(app_root / "data" / "user" / "tags.json")
    categories = CategoryRegistry(
        app_root / "data" / "defaults" / "categories.json",
        app_root / "data" / "user" / "categories.json",
    )
    foundation = FoundationKnowledge(
        app_root / "data" / "defaults" / "modifiers.json",
        app_root / "data" / "defaults" / "concepts.json",
    )
    categories.configure_category_base_authority(effective.values())
    modifier_index = reusable_modifier_compatibility_index(
        tags,
        effective.values(),
        categories=categories,
        foundation=foundation,
    )
    defaults = _default_records_for_kind(app_root, "tag")
    projected_records = []
    for item in payload.get("tags", ()):
        if not isinstance(item, dict):
            continue
        projected = json.loads(json.dumps(item))
        record = TagRecord.from_dict(projected)
        compatibility = tag_relationship_compatibility_payload(
            record,
            effective.values(),
            tags=tags,
            categories=categories,
            foundation=foundation,
            alias_index=alias_index,
            semantic_index=semantic_index,
            modifier_index=modifier_index,
            constraint_index=constraint_index,
        )
        projected_relations = projected.setdefault("relations", {})
        compatibility_relations = compatibility.get("relations", {})
        projected_relations.pop("parent", None)
        projected_relations.pop("attributes", None)
        projected_relations.pop("implies", None)
        projected_relations.pop("modifier_value_provenance", None)
        projected_relations.pop("allowed_modifier_groups", None)
        projected_relations.pop("allowed_modifier_groups_provenance", None)
        projected_relations.pop("conflicts", None)
        projected_relations.pop("constraint_provenance", None)
        if "parent" in compatibility_relations:
            projected_relations["parent"] = compatibility_relations["parent"]
        if "attributes" in compatibility_relations:
            projected_relations["attributes"] = compatibility_relations["attributes"]
        if "implies" in compatibility_relations:
            projected_relations["implies"] = compatibility_relations["implies"]
        if "modifier_value_provenance" in compatibility_relations:
            projected_relations["modifier_value_provenance"] = (
                compatibility_relations["modifier_value_provenance"]
            )
        if "allowed_modifier_groups" in compatibility_relations:
            projected_relations["allowed_modifier_groups"] = (
                compatibility_relations["allowed_modifier_groups"]
            )
        if "allowed_modifier_groups_provenance" in compatibility_relations:
            projected_relations["allowed_modifier_groups_provenance"] = (
                compatibility_relations["allowed_modifier_groups_provenance"]
            )
        if "conflicts" in compatibility_relations:
            projected_relations["conflicts"] = compatibility_relations["conflicts"]
        if "constraint_provenance" in compatibility_relations:
            projected_relations["constraint_provenance"] = (
                compatibility_relations["constraint_provenance"]
            )
        shipped = _matching_default_record(projected, defaults, "tags")
        if shipped is not None and _record_comparable(
            "tag", projected, ignore_id=True,
        ) == _record_comparable("tag", shipped, ignore_id=True):
            continue
        projected_records.append(projected)
    payload["tags"] = projected_records
    retained_ids = {
        str(item.get("id") or "") for item in projected_records
        if str(item.get("id") or "")
    }
    payload["base_fingerprints"] = {
        stable_id: fingerprint
        for stable_id, fingerprint in dict(
            payload.get("base_fingerprints") or {}
        ).items()
        if stable_id in retained_ids
    }
    payload["lifecycle_owned_ids"] = [
        stable_id for stable_id in payload.get("lifecycle_owned_ids", ())
        if stable_id in retained_ids
    ]
    return payload


def _portable_user_records(app_root: Path, filename: str, key: str) -> dict[str,Any]:
    kind = RECORD_KIND_BY_LABEL[key]
    return _portable_record_collection(
        app_root, filename=filename, key=key, kind=kind
    )


def _portable_character_records(
    app_root: Path, filename: str, key: str,
) -> dict[str, Any]:
    """Export Character records with explicit old-reader relationship fields."""

    kind = RECORD_KIND_BY_LABEL[key]
    payload = _portable_user_records(app_root, filename, key)
    user_path = app_root / "data" / "user" / filename
    raw_user = _read_payload_checked(user_path) if user_path.exists() else {}
    raw_by_id = {
        str(item.get("id") or ""): item
        for item in raw_user.get(key, ())
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    selected = {
        str(item.get("id") or ""): item
        for item in payload.get(key, ())
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    endpoint_kind = {
        "series": "character_source",
        "character": "character",
        "outfit": "outfit",
    }[kind]
    portable_relationship_ids = {
        relationship.id
        for relationship in RelationshipStore(
            app_root / RELATIONSHIPS_RELATIVE
        ).relationships.values()
        if relationship.source_endpoint is not None
        and relationship.source_endpoint.kind == endpoint_kind
        and relationship.kind
        in {ENTITY_TAG, CHARACTER_SOURCE, ASSOCIATED_CHARACTER, VERSION_OF}
    }
    forced_ids = {
        relationship.source_endpoint.id
        for relationship in RelationshipStore(
            app_root / RELATIONSHIPS_RELATIVE
        ).relationships.values()
        if relationship.id in portable_relationship_ids
        and relationship.source_endpoint is not None
        and relationship.source_endpoint.id in raw_by_id
    }
    for stable_id in forced_ids:
        selected.setdefault(stable_id, raw_by_id[stable_id])

    defaults = _default_records_for_kind(app_root, kind)
    default_relationships = tuple(
        RelationshipStore(
            app_root / DEFAULT_RELATIONSHIPS_RELATIVE
        ).relationships.values()
    )
    user_relationships = tuple(
        RelationshipStore(
            app_root / RELATIONSHIPS_RELATIVE
        ).relationships.values()
    )
    compatibility_by_id = {
        str(item.get("id") or ""): item
        for item in character_record_layered_compatibility_payloads(
            kind,
            selected.values(),
            default_relationships,
            user_relationships,
        )
    }
    shipped_compatibility_by_id = {
        str(item.get("id") or ""): item
        for item in character_record_compatibility_payloads(
            kind, defaults, default_relationships,
        )
    }
    projected: list[dict[str, Any]] = []
    for stable_id, item in selected.items():
        compatibility = compatibility_by_id[stable_id]
        shipped = _matching_default_record(item, defaults, key)
        if shipped is not None:
            shipped_compatibility = shipped_compatibility_by_id[
                str(shipped.get("id") or "")
            ]
            if (
                stable_id not in forced_ids
                and _record_comparable(kind, compatibility, ignore_id=True)
                == _record_comparable(kind, shipped_compatibility, ignore_id=True)
            ):
                continue
            payload.setdefault("base_fingerprints", {}).setdefault(
                stable_id, _record_fingerprint(kind, shipped),
            )
        projected.append(compatibility)
    payload[key] = sorted(
        projected, key=lambda item: str(item.get("id") or ""),
    )
    retained_ids = {
        str(item.get("id") or "") for item in projected
        if str(item.get("id") or "")
    }
    retained_ids.update(
        str(value) for value in payload.get("disabled_default_ids", ())
        if str(value).strip()
    )
    payload["base_fingerprints"] = {
        stable_id: fingerprint
        for stable_id, fingerprint in dict(
            payload.get("base_fingerprints") or {}
        ).items()
        if stable_id in retained_ids
    }
    payload["lifecycle_owned_ids"] = [
        stable_id for stable_id in payload.get("lifecycle_owned_ids", ())
        if stable_id in retained_ids
    ]
    return payload


def _portable_category_records(app_root: Path) -> dict[str, Any]:
    """Export schema-v6 Categories with the explicit pack compatibility view."""

    payload = _portable_user_records(
        app_root, "categories.json", "categories",
    )
    effective = dict(
        RelationshipStore(
            app_root / DEFAULT_RELATIONSHIPS_RELATIVE
        ).relationships
    )
    effective.update(
        RelationshipStore(app_root / RELATIONSHIPS_RELATIVE).relationships
    )
    category_base_index = category_base_relationship_index(effective.values())
    defaults = _default_records_for_kind(app_root, "category")
    projected_records = []
    for item in payload.get("categories", ()):
        if not isinstance(item, dict):
            continue
        projected = json.loads(json.dumps(item))
        record = CategoryRecord.from_dict(projected, source="pack")
        compatibility = category_base_compatibility_payload(
            record, category_base_index,
        )
        projected.pop("base_tag_id", None)
        projected.pop("base_tag_policy", None)
        if "base_tag_id" in compatibility:
            projected["base_tag_id"] = compatibility["base_tag_id"]
        if "base_tag_policy" in compatibility:
            projected["base_tag_policy"] = compatibility["base_tag_policy"]
        shipped = _matching_default_record(projected, defaults, "categories")
        if shipped is not None and _record_comparable(
            "category", projected, ignore_id=True,
        ) == _record_comparable("category", shipped, ignore_id=True):
            continue
        projected_records.append(projected)
    payload["categories"] = projected_records
    retained_ids = {
        str(item.get("id") or "") for item in projected_records
        if str(item.get("id") or "")
    }
    retained_ids.update(
        str(value)
        for value in payload.get("disabled_default_ids", ())
        if str(value).strip()
    )
    payload["base_fingerprints"] = {
        stable_id: fingerprint
        for stable_id, fingerprint in dict(
            payload.get("base_fingerprints") or {}
        ).items()
        if stable_id in retained_ids
    }
    payload["lifecycle_owned_ids"] = [
        stable_id for stable_id in payload.get("lifecycle_owned_ids", ())
        if stable_id in retained_ids
    ]
    return payload


def _portable_user_relationships(app_root: Path) -> dict[str, Any]:
    user_store = RelationshipStore(app_root / RELATIONSHIPS_RELATIVE)
    default_store = RelationshipStore(app_root / DEFAULT_RELATIONSHIPS_RELATIVE)
    removal_relationship_ids={
        stable_id
        for archive in TagDatabase(app_root/"data"/"user"/"tags.json").active_removed_archives()
        for stable_id in archive.relationships_after
    }
    records = []
    base_fingerprints = {}
    for relationship in user_store.relationships.values():
        if not relationship.id:
            continue
        if relationship.kind not in PORTABLE_RELATIONSHIP_KINDS:
            raise ValueError(
                "Knowledge Pack export cannot omit unsupported relationship "
                f"kind {relationship.kind!r}. Add an explicit registry and "
                "authority-matrix adapter decision before exporting it."
            )
        if relationship.id in removal_relationship_ids:continue
        portable=replace(relationship,layer="user").to_dict()
        shipped=default_store.relationships.get(relationship.id)
        if shipped is not None and _relationship_fingerprint(portable)==_relationship_fingerprint(shipped):
            continue
        records.append(portable)
        base_fingerprints[relationship.id]=(
            _relationship_fingerprint(shipped) if shipped is not None else "missing"
        )
    if len(records) > MAX_PACK_RELATIONSHIPS:
        raise ValueError(
            "Knowledge Packs can carry at most "
            f"{MAX_PACK_RELATIONSHIPS:,} canonical relationships."
        )
    issues=validate_relationships(
        KnowledgeRelationship.from_dict(item) for item in records
    )
    if issues:
        raise ValueError(
            "Portable relationship authority is invalid: "+
            " ".join(issue.message for issue in issues[:3])
        )
    return {
        "schema_version": user_store.schema_version,
        "relationships":records,
        "base_fingerprints":dict(sorted(base_fingerprints.items())),
    }


def knowledge_pack_summary(app_root: Path) -> dict[str, int]:
    """Count the portable user additions that a new Knowledge Pack would contain."""
    tags = _portable_user_tags(app_root)
    summary = {
        "tags": len(tags.get("tags", [])),
        "merge_rules": (
            len(tags.get("merge_rules", []))
            + len(tags.get("disabled_default_merge_rule_ids", []))
        ),
        "removed_tag_archives": len(tags.get("removed_tag_archives", [])),
        "legacy_removed_tag_ids": len(tags.get("legacy_removed_tag_ids", [])),
    }
    for filename, key in (
        ("characters.json", "characters"),
        ("series.json", "series"),
        ("outfits.json", "outfits"),
        ("categories.json", "categories"),
        ("patterns.json", "patterns"),
        ("consumer_policies.json", "policies"),
    ):
        portable = (
            _portable_character_records(app_root, filename, key)
            if key in {"characters", "series", "outfits"}
            else _portable_user_records(app_root, filename, key)
        )
        summary["consumer_policies" if key == "policies" else key] = (
            len(portable.get(key, []))
            + len(portable.get("disabled_default_ids", []))
        )
    summary["relationships"]=len(
        _portable_user_relationships(app_root).get("relationships",[])
    )
    return summary


def _rule_effect(
    item: dict[str, Any], *, action: str,
) -> KnowledgePackRuleEffect:
    stable_id = str(item.get("id") or "rule")
    sources = tuple(
        str(value).strip() for value in item.get("source_tags", ())
        if str(value).strip()
    )
    target = str(item.get("target_tag") or "").strip() or "(no target)"
    scope = str(item.get("scope") or "any").replace("_", " ")
    enabled = bool(item.get("enabled", True))
    require_review = bool(item.get("require_review", True))
    consequence = (
        f"Sources: {' + '.join(sources) if sources else '(none)'} → "
        f"Target: {target}; scope: {scope}; "
        f"{'enabled' if enabled else 'disabled'}; "
        f"review requirement: {'required' if require_review else 'not required'}."
    )
    return KnowledgePackRuleEffect(
        stable_id,
        str(item.get("description") or target or stable_id),
        action,
        consequence,
    )


def _pack_rule_effects(
    app_root: Path, tags_payload: dict[str, Any],
) -> tuple[KnowledgePackRuleEffect, ...]:
    base_fingerprints = tags_payload.get("record_base_fingerprints")
    base_fingerprints = base_fingerprints if isinstance(base_fingerprints, dict) else {}
    rule_bases = base_fingerprints.get("merge_rules")
    rule_bases = rule_bases if isinstance(rule_bases, dict) else {}
    effects = [
        _rule_effect(
            item,
            action=(
                "Add rule"
                if str(rule_bases.get(str(item.get("id") or "")) or "") == "missing"
                else "Override shipped rule"
            ),
        )
        for item in tags_payload.get("merge_rules", ())
        if isinstance(item, dict)
    ]
    defaults = {
        str(item.get("id") or ""): item
        for item in _default_records_for_kind(app_root, "merge_rule")
        if str(item.get("id") or "")
    }
    for stable_id in tags_payload.get("disabled_default_merge_rule_ids", ()):
        item = defaults.get(str(stable_id))
        if item is None:
            effects.append(KnowledgePackRuleEffect(
                str(stable_id), str(stable_id), "Disable shipped rule",
                "The referenced shipped rule is unavailable, so its exact consequence cannot be verified.",
            ))
        else:
            effects.append(_rule_effect(item, action="Disable shipped rule"))
    return tuple(sorted(effects, key=lambda item: (item.label.casefold(), item.stable_id)))


def _reviewed_tag_rating(record: TagRecord, categories: CategoryRegistry) -> tuple[str, str]:
    sensitivity = record.content_sensitivity
    if (
        sensitivity.classification_status == "reviewed"
        and sensitivity.minimum_rating in CONTENT_RATING_ORDER
    ):
        return sensitivity.minimum_rating, "reviewed Tag sensitivity"
    candidate, source_id, _policy = categories.effective_sensitivity(record.category)
    if candidate in CONTENT_RATING_ORDER:
        return candidate, f"reviewed Category sensitivity ({source_id or record.category})"
    return "explicit", "no reviewed Tag or Category content rating; conservative Explicit boundary"


def _content_review(
    app_root: Path,
    *,
    tags_payload: dict[str, Any],
    record_payloads: dict[str, dict[str, Any]],
    relationships_payload: dict[str, Any],
) -> tuple[str, tuple[KnowledgePackContentFinding, ...]]:
    """Derive the lowest boundary that does not understate portable contents."""
    categories = CategoryRegistry(
        app_root / "data" / "defaults" / "categories.json",
        app_root / "data" / "user" / "categories.json",
    )
    for item in record_payloads.get("categories", {}).get("categories", ()):
        if not isinstance(item, dict) or not str(item.get("id") or ""):
            continue
        record = CategoryRecord.from_dict(item, source="pack")
        categories.categories[record.id] = record

    tag_records: dict[str, TagRecord] = {
        str(item.get("id") or ""): TagRecord.from_dict(item)
        for item in load_effective_tag_defaults(app_root / "data" / "defaults").tags
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    user_tags = TagDatabase(app_root / "data" / "user" / "tags.json")
    tag_records.update(user_tags.tags)
    portable_tags = [
        item for item in tags_payload.get("tags", ()) if isinstance(item, dict)
    ]
    for item in portable_tags:
        record = TagRecord.from_dict(item)
        tag_records[record.id] = record

    names: dict[str, set[str]] = {}
    for record in tag_records.values():
        for value in (record.display, *record.aliases):
            key = _normalized(value)
            if key:
                names.setdefault(key, set()).add(record.id)

    findings: dict[tuple[str, str], KnowledgePackContentFinding] = {}

    def add_tag(record: TagRecord, *, stable_id: str = "", label: str = "") -> None:
        rating, basis = _reviewed_tag_rating(record, categories)
        key = ("tag", record.id)
        findings[key] = KnowledgePackContentFinding(
            stable_id or record.id,
            label or record.display,
            rating,
            basis,
        )

    def add_unknown(stable_id: str, label: str, basis: str) -> None:
        key = ("unknown", str(stable_id))
        findings[key] = KnowledgePackContentFinding(
            str(stable_id), str(label or stable_id), "explicit", basis
        )

    def add_tag_reference(value: str, *, owner_id: str, owner_label: str) -> None:
        text = str(value).strip()
        matches = names.get(_normalized(text), set())
        if text in tag_records:
            matches = {text}
        if len(matches) != 1:
            add_unknown(
                f"{owner_id}:{_normalized(text) or text}",
                text or owner_label,
                f'Tag reference "{text}" has no single reviewed content-rating owner; conservative Explicit boundary',
            )
            return
        add_tag(tag_records[next(iter(matches))])

    for item in portable_tags:
        add_tag(TagRecord.from_dict(item))

    for archive in tags_payload.get("removed_tag_archives", ()):
        if not isinstance(archive, dict) or str(archive.get("restored_at") or ""):
            continue
        for item in archive.get("removed_tags", ()):
            if isinstance(item, dict):
                add_tag(TagRecord.from_dict(item))
    for stable_id in tags_payload.get("legacy_removed_tag_ids", ()):
        add_unknown(
            f"legacy-removal:{stable_id}",
            str(stable_id),
            "Legacy removal has no complete reviewed record; conservative Explicit boundary",
        )

    for item in tags_payload.get("merge_rules", ()):
        if not isinstance(item, dict):
            continue
        effect = _rule_effect(item, action="Review rule")
        for value in (*item.get("source_tags", ()), item.get("target_tag", "")):
            if str(value).strip():
                add_tag_reference(
                    str(value), owner_id=f"rule:{effect.stable_id}", owner_label=effect.label
                )

    structural_endpoint_kinds = {
        "alias_text", "modifier_dimension", "modifier_value",
        "modifier_role_state", "modifier_value_state", "allowed_dimension_state",
    }
    for item in relationships_payload.get("relationships", ()):
        if not isinstance(item, dict):
            continue
        relationship = KnowledgeRelationship.from_dict(item)
        if relationship.disabled:
            continue
        for endpoint in relationship.endpoints:
            if endpoint.kind == "tag":
                record = tag_records.get(endpoint.id)
                if record is None:
                    add_unknown(
                        f"relationship:{relationship.id}:tag:{endpoint.id}",
                        endpoint.label or endpoint.id,
                        f"Relationship Tag endpoint {endpoint.id!r} has no reviewed content rating; conservative Explicit boundary",
                    )
                else:
                    add_tag(record)
            elif endpoint.kind == "category":
                candidate, source_id, _policy = categories.effective_sensitivity(endpoint.id)
                if candidate in CONTENT_RATING_ORDER:
                    findings[("category", endpoint.id)] = KnowledgePackContentFinding(
                        endpoint.id,
                        endpoint.label or endpoint.id,
                        candidate,
                        f"reviewed Category sensitivity ({source_id or endpoint.id})",
                    )
                else:
                    add_unknown(
                        f"relationship:{relationship.id}:{endpoint.id}",
                        endpoint.label or endpoint.id,
                        "Relationship Category endpoint has no reviewed content rating; conservative Explicit boundary",
                    )
            elif endpoint.kind not in structural_endpoint_kinds:
                add_unknown(
                    f"relationship:{relationship.id}:{endpoint.kind}:{endpoint.id}",
                    endpoint.label or endpoint.id,
                    f"{endpoint.kind.replace('_', ' ').title()} content has no reviewed rating model; conservative Explicit boundary",
                )

    for key, label in (
        ("categories", "Category"),
        ("patterns", "Reusable pattern"),
        ("series", "Character Source"),
        ("characters", "Character"),
        ("outfits", "Outfit"),
    ):
        payload = record_payloads.get(key, {})
        for item in payload.get(key, ()):
            if not isinstance(item, dict):
                continue
            stable_id = str(item.get("id") or key)
            if key == "categories":
                candidate, source_id, _policy = categories.effective_sensitivity(stable_id)
                if candidate in CONTENT_RATING_ORDER:
                    findings[("category", stable_id)] = KnowledgePackContentFinding(
                        stable_id,
                        str(item.get("name") or stable_id),
                        candidate,
                        f"reviewed Category sensitivity ({source_id or stable_id})",
                    )
                    continue
            add_unknown(
                f"{key}:{stable_id}",
                str(item.get("name") or stable_id),
                f"{label} content has no complete reviewed content rating; conservative Explicit boundary",
            )

    ordered = tuple(sorted(
        findings.values(), key=lambda item: (
            CONTENT_RATING_ORDER[item.rating], item.label.casefold(), item.stable_id
        )
    ))
    boundary = max(
        (item.rating for item in ordered),
        key=CONTENT_RATING_ORDER.get,
        default="general",
    )
    return boundary, ordered


def _portable_export_payloads(
    app_root: Path,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, Any]]:
    tags_payload = _portable_user_tags(app_root)
    record_payloads = {
        key: (
            _portable_character_records(app_root, filename, key)
            if key in {"characters", "series", "outfits"}
            else _portable_user_records(app_root, filename, key)
        )
        for filename, key in (
            ("characters.json", "characters"),
            ("series.json", "series"),
            ("outfits.json", "outfits"),
            ("patterns.json", "patterns"),
            ("consumer_policies.json", "policies"),
        )
    }
    record_payloads["categories"] = _portable_category_records(app_root)
    return tags_payload, record_payloads, _portable_user_relationships(app_root)


def _export_summary_from_payloads(
    tags_payload: dict[str, Any],
    record_payloads: dict[str, dict[str, Any]],
    relationships: dict[str, Any],
) -> dict[str, int]:
    summary = {
        "tags": len(tags_payload.get("tags", ())),
        "merge_rules": (
            len(tags_payload.get("merge_rules", ()))
            + len(tags_payload.get("disabled_default_merge_rule_ids", ()))
        ),
        "removed_tag_archives": len(tags_payload.get("removed_tag_archives", ())),
        "legacy_removed_tag_ids": len(tags_payload.get("legacy_removed_tag_ids", ())),
    }
    for key, payload in record_payloads.items():
        summary["consumer_policies" if key == "policies" else key] = (
            len(payload.get(key, ()))
            + len(payload.get("disabled_default_ids", ()))
        )
    summary["relationships"] = len(relationships.get("relationships", ()))
    return summary


def _portable_export_snapshot(
    app_root: Path,
) -> tuple[
    str,
    dict[str, Any],
    dict[str, dict[str, Any]],
    dict[str, Any],
    str,
]:
    authority_paths = _pack_authority_paths(app_root)
    before = _paths_fingerprint(authority_paths)
    tags_payload, record_payloads, relationships = _portable_export_payloads(app_root)
    default_relationship_fingerprint = _file_fingerprint(
        app_root / "data" / "defaults" / "relationships.json"
    )
    after = _paths_fingerprint(authority_paths)
    if before != after:
        raise RuntimeError(
            "System Knowledge changed while the export review was being built. Review it again."
        )
    return (
        after,
        tags_payload,
        record_payloads,
        relationships,
        default_relationship_fingerprint,
    )


def _export_plan_from_snapshot(
    app_root: Path,
    authority_fingerprint: str,
    tags_payload: dict[str, Any],
    record_payloads: dict[str, dict[str, Any]],
    relationships: dict[str, Any],
) -> KnowledgePackExportPlan:
    summary = _export_summary_from_payloads(
        tags_payload, record_payloads, relationships
    )
    boundary, findings = _content_review(
        app_root,
        tags_payload=tags_payload,
        record_payloads=record_payloads,
        relationships_payload=relationships,
    )
    return KnowledgePackExportPlan(
        authority_fingerprint,
        boundary,
        tuple(summary.items()),
        findings,
        _pack_rule_effects(app_root, tags_payload),
    )


def _verify_export_snapshot_current(
    app_root: Path, authority_fingerprint: str,
) -> None:
    if _paths_fingerprint(_pack_authority_paths(app_root)) != authority_fingerprint:
        raise RuntimeError(
            "System Knowledge changed while the export review was being built. Review it again."
        )


def plan_knowledge_pack_export(app_root: Path) -> KnowledgePackExportPlan:
    """Review the exact portable boundary before authoring a lower-rated pack."""
    with catalog_admission(Path(app_root).absolute(), __import__(__name__, fromlist=["*"]), role="export_plan"):
        return _plan_knowledge_pack_export_admitted(app_root)


def _plan_knowledge_pack_export_admitted(app_root: Path) -> KnowledgePackExportPlan:
    app_root = Path(app_root)
    fingerprint, tags_payload, record_payloads, relationships, _default = (
        _portable_export_snapshot(app_root)
    )
    plan = _export_plan_from_snapshot(
        app_root, fingerprint, tags_payload, record_payloads, relationships
    )
    _verify_export_snapshot_current(app_root, fingerprint)
    return plan


def inspect_knowledge_pack(pack_path: Path) -> dict[str, int]:
    """Safely inspect a Knowledge Pack and report its portable record counts."""
    with tempfile.TemporaryDirectory() as temp:
        work = Path(temp)
        with zipfile.ZipFile(pack_path, "r") as archive:
            _extract_pack_safely(archive, work)
        _load_manifest(work)
        return _pack_summary_from_work(work)


def export_knowledge_pack(
    app_root: Path,
    output: Path | None = None,
    *,
    plan: KnowledgePackExportPlan | None = None,
) -> Path:
    with catalog_admission(Path(app_root).absolute(), __import__(__name__, fromlist=["*"]), role="export"):
        return _export_knowledge_pack_admitted(app_root,output,plan=plan)


def _export_path_state(path: Path) -> tuple | None:
    """Stream an exact destination snapshot without following redirects."""
    _check_export_path(path)
    try:
        before = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode):
        raise RuntimeError("Choose a regular ZIP file as the export destination.")
    def identity(value):
        return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
    digest = sha256()
    with path.open("rb") as handle:
        if identity(os.fstat(handle.fileno())) != identity(before):
            raise RuntimeError("The export destination changed. Choose it again.")
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        if identity(os.fstat(handle.fileno())) != identity(before):
            raise RuntimeError("The export destination changed while being checked.")
    _check_export_path(path)
    if identity(path.stat(follow_symlinks=False)) != identity(before):
        raise RuntimeError("The export destination changed. Choose it again.")
    return (*identity(before), digest.hexdigest())


def _check_export_path(path: Path) -> None:
    for current in (path, *path.parents):
        try:
            value = current.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(value.st_mode) or getattr(value, "st_file_attributes", 0) & 0x400:
            raise RuntimeError("Choose an export destination without redirected folders or files.")


def _export_parent_identity(path: Path) -> tuple:
    _check_export_path(path)
    value = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(value.st_mode):
        raise RuntimeError("The export destination folder is unavailable.")
    # Creating our sibling stage changes folder timestamps, not its identity.
    return (value.st_dev, value.st_ino, getattr(value, "st_file_attributes", 0))


def _verify_export_archive(staged: Path, work: Path, manifest: dict) -> None:
    expected = {"manifest.json", *manifest["files"]}
    with zipfile.ZipFile(staged) as archive:
        _shared_checked_archive_entries(archive, max_files=MAX_PACK_FILES,
                                       max_uncompressed_bytes=MAX_PACK_UNCOMPRESSED_BYTES)
        names = archive.namelist()
        if len(names) != len(expected) or set(names) != expected or archive.testzip() is not None:
            raise RuntimeError("The staged Knowledge Pack failed archive verification.")
        for name in expected:
            if archive.read(name) != (work / name).read_bytes():
                raise RuntimeError("The staged Knowledge Pack does not match its reviewed contents.")


def _export_knowledge_pack_admitted(
    app_root: Path,
    output: Path | None = None,
    *,
    plan: KnowledgePackExportPlan | None = None,
) -> Path:
    app_root = Path(app_root)
    (
        fingerprint,
        tags_payload,
        record_payloads,
        relationships,
        default_relationship_fingerprint,
    ) = _portable_export_snapshot(app_root)
    reviewed_boundary = "explicit"
    reviewed_findings: tuple[KnowledgePackContentFinding, ...] = ()
    review_method = "conservative_explicit"
    if plan is not None:
        fresh = _export_plan_from_snapshot(
            app_root, fingerprint, tags_payload, record_payloads, relationships
        )
        _verify_export_snapshot_current(app_root, fingerprint)
        if fresh.authority_fingerprint != plan.authority_fingerprint:
            raise RuntimeError(
                "System Knowledge changed after the export review. Review the Knowledge Pack again."
            )
        if (
            fresh.content_rating_boundary != plan.content_rating_boundary
            or fresh.summary != plan.summary
            or fresh.rule_effects != plan.rule_effects
            or fresh.content_findings != plan.content_findings
        ):
            raise RuntimeError(
                "The reviewed Knowledge Pack no longer matches current System Knowledge. Review it again."
            )
        if not fresh.can_export:
            raise ValueError("The reviewed Knowledge Pack is not ready to export.")
        reviewed_boundary = fresh.content_rating_boundary
        reviewed_findings = fresh.content_findings
        review_method = "exact_portable_content"
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    output = Path(os.path.abspath(output or app_root / f"CollectiGen_KnowledgePack_{stamp}.zip"))
    for protected in (app_root / "data" / "user", app_root / "data" / "defaults", app_root / "backups"):
        if output.is_relative_to(Path(os.path.abspath(protected))):
            raise RuntimeError("Export outside the application's Knowledge and recovery folders.")
    parent_identity = _export_parent_identity(output.parent)
    destination_state = _export_path_state(output)
    if destination_state is not None:
        for source in _pack_authority_paths(app_root):
            if source.exists() and os.path.samefile(output, source):
                raise RuntimeError("The export destination is also a Knowledge source file.")
    files = [
        ("characters.json","characters"),("series.json","series"),("outfits.json","outfits"),
        ("categories.json","categories"),("patterns.json","patterns"),
        ("consumer_policies.json","policies"),
    ]
    manifest = {
        "format": "tagmanager_knowledge_pack",
        "schema_version": (
            SUPPORTED_PACK_SCHEMA_VERSION
            if record_payloads.get("policies", {}).get("policies")
            else 8
        ),
        "scope": "user_layer_patch",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "content_rating_boundary":reviewed_boundary,
        "content_rating_review":{
            "version":1,
            "method":review_method,
            "finding_count":len(reviewed_findings),
            "conservative_unknown_count":sum(
                "conservative Explicit boundary" in item.basis
                for item in reviewed_findings
            ),
        },
        "default_relationship_fingerprint":default_relationship_fingerprint,
        "files": [],
    }
    # Cleanup cannot turn a completed publication into a reported failed export.
    with tempfile.TemporaryDirectory(prefix=".collectigen-pack-", dir=output.parent,
                                     ignore_cleanup_errors=True) as temp:
        work = Path(temp) / "contents"
        portable_record_count = 0
        tags_target = work / "data" / "user" / "tags.json"
        tags_target.parent.mkdir(parents=True, exist_ok=True)
        portable_record_count += (
            len(tags_payload.get("tags", ()))
            + len(tags_payload.get("merge_rules", ()))
            + len(tags_payload.get("disabled_default_merge_rule_ids", ()))
        )
        tags_target.write_text(
            json.dumps(tags_payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        manifest["files"].append("data/user/tags.json")
        for filename,key in files:
            relative=f"data/user/{filename}"
            portable = record_payloads[key]
            if portable.get(key) or portable.get("disabled_default_ids"):
                target = work / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                portable_record_count += (
                    len(portable.get(key, ()))
                    + len(portable.get("disabled_default_ids", ()))
                )
                target.write_text(json.dumps(portable,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
                manifest["files"].append(relative)
        if portable_record_count > MAX_PACK_RECORDS:
            raise ValueError(
                "Knowledge Packs can carry at most "
                f"{MAX_PACK_RECORDS:,} non-relationship record decisions."
            )
        if relationships.get("relationships"):
            relative=RELATIONSHIPS_RELATIVE; target=work/relative
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text(
                json.dumps(relationships,indent=2,ensure_ascii=False)+"\n",
                encoding="utf-8",
            )
            manifest["files"].append(relative)
        (work / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        staged = Path(temp) / "pack.zip"
        with zipfile.ZipFile(staged, "x", zipfile.ZIP_DEFLATED) as archive:
            for file in work.rglob("*"):
                if file.is_file():
                    archive.write(file, file.relative_to(work))
        with staged.open("r+b") as handle:
            os.fsync(handle.fileno())
        _verify_export_archive(staged, work, manifest)
        _verify_export_snapshot_current(app_root, fingerprint)
        if _export_parent_identity(output.parent) != parent_identity or _export_path_state(output) != destination_state:
            raise RuntimeError("The export destination changed. Choose it again before exporting.")
        if destination_state is not None:
            # Intentional replacement, not an external-writer compare-and-swap.
            os.replace(staged, output)
        elif os.name == "nt":
            os.rename(staged, output)  # Windows refuses an existing destination.
        else:
            os.link(staged, output)  # Exclusive publication; stage cleanup follows.
    return output


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _record_identities(item: dict[str, Any], key: str) -> set[tuple[Any, ...]]:
    """Return semantic identities used to avoid cross-install duplicate records."""
    if key == "policies":
        selector = item.get("selector") if isinstance(item.get("selector"), dict) else {}
        conditions = item.get("conditions") if isinstance(item.get("conditions"), dict) else {}
        return {
            (
                str(selector.get("type") or ""),
                str(selector.get("id") or ""),
                str(item.get("consumer") or ""),
                json.dumps(
                    conditions,
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        }
    if key == "merge_rules":
        return {(
            tuple(sorted(_normalized(value) for value in item.get("source_tags", ()))),
            _normalized(item.get("target_tag")),
        )}
    names = {_normalized(item.get("display") or item.get("name"))}
    names.update(_normalized(value) for value in item.get("aliases", []) if _normalized(value))
    names.discard("")
    if key == "characters":
        return {(name, str(item.get("series_id") or "")) for name in names}
    if key == "outfits":
        context = (str(item.get("series_id") or ""), tuple(sorted(str(v) for v in item.get("character_ids", []))))
        return {(name, *context) for name in names}
    if key == "categories":
        return {(name, str(item.get("parent_id") or "")) for name in names}
    if key in {"tags", "series", "patterns"}:
        return {(name,) for name in names}
    return set()


def _pack_summary_from_work(work: Path) -> dict[str, int]:
    summary = {label: 0 for label, *_ in PACK_RECORDS}
    summary["relationships"] = 0
    summary["removed_tag_archives"] = 0
    summary["legacy_removed_tag_ids"] = 0
    for label, relative, legacy_relative, key in PACK_RECORDS:
        imported = work / relative
        if not imported.exists() and legacy_relative:
            imported = work / legacy_relative
        if imported.exists():
            payload=json.loads(imported.read_text(encoding="utf-8"))
            summary[label] = len(_load(imported, key))
            if isinstance(payload,dict):
                disabled_key = (
                    "disabled_default_merge_rule_ids"
                    if label == "merge_rules"
                    else "disabled_default_ids"
                )
                if isinstance(payload.get(disabled_key), list):
                    summary[label] += len(payload[disabled_key])
            if label=="tags":
                if isinstance(payload,dict) and isinstance(payload.get("removed_tag_archives"),list):
                    summary["removed_tag_archives"]=len(payload["removed_tag_archives"])
                if isinstance(payload,dict) and isinstance(payload.get("legacy_removed_tag_ids"),list):
                    summary["legacy_removed_tag_ids"]=len(payload["legacy_removed_tag_ids"])
    relationships = work / RELATIONSHIPS_RELATIVE
    if relationships.exists():
        summary["relationships"] = len(_load(relationships, "relationships"))
    return summary


def _pack_target_paths(app_root: Path) -> tuple[Path, ...]:
    relatives = {relative for _label, relative, _legacy, _key in PACK_RECORDS}
    relatives.add(RELATIONSHIPS_RELATIVE)
    relatives.add("data/user/default_knowledge_state.json")
    return tuple(app_root / relative for relative in sorted(relatives))


def _pack_authority_paths(app_root: Path) -> tuple[Path, ...]:
    defaults = app_root / "data" / "defaults"
    paths = set(_pack_target_paths(app_root))
    paths.add(app_root / DEFAULT_RELATIONSHIPS_RELATIVE)
    paths.update(
        defaults / Path(relative).name
        for _label, relative, _legacy, _key in PACK_RECORDS
    )
    paths.update(defaults.glob("*tags.json"))
    paths.add(app_root / "data" / "user" / "default_knowledge_state.json")
    if (app_root / "data/user/catalog_identity.json").exists():
        paths.add(app_root / "data/user/catalog_identity.json")
    return tuple(sorted(paths, key=lambda value: str(value).casefold()))


def _authority_fingerprints(app_root: Path) -> tuple[tuple[str, str], ...]:
    return tuple(
        (str(path.resolve()), _file_fingerprint(path))
        for path in _pack_authority_paths(app_root)
    )


def _verify_pack_authority(
    app_root: Path, plan: KnowledgePackImportPlan, *, message: str
) -> None:
    current_paths = _foundation_plan_paths(app_root, plan.foundation_proposal)
    if _paths_fingerprint(current_paths) != plan.authority_fingerprint:
        raise RuntimeError(message)
    if not plan.authority_fingerprints:
        return
    current = {
        str(path.resolve()).casefold(): _file_fingerprint(path)
        for path in current_paths
    }
    expected = {
        str(Path(path).resolve()).casefold(): fingerprint
        for path, fingerprint in plan.authority_fingerprints
    }
    if current != expected:
        raise RuntimeError(message)


def _read_records(path: Path, key: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object.")
    return [item for item in payload.get(key, ()) if isinstance(item, dict)]


def _existing_tag_records(app_root: Path) -> list[dict[str, Any]]:
    defaults = app_root / "data" / "defaults"
    records = [
        item
        for path in sorted(defaults.glob("*tags.json"))
        for item in _read_records(path, "tags")
    ]
    records.extend(_read_records(app_root / "data" / "user" / "tags.json", "tags"))
    return records


def _existing_category_records(app_root: Path) -> list[dict[str, Any]]:
    records = _read_records(
        app_root / "data" / "defaults" / "categories.json", "categories"
    )
    records.extend(
        _read_records(app_root / "data" / "user" / "categories.json", "categories")
    )
    return records


def _layered_character_records(
    app_root: Path,
    record_kind: str,
    records: list[dict[str, Any]],
    *,
    include_user_relationships: bool,
    additional_user_relationships: Iterable[KnowledgeRelationship] = (),
) -> list[dict[str, Any]]:
    """Return old-reader views for identity and record comparisons."""

    default_relationships = tuple(
        RelationshipStore(
            app_root / DEFAULT_RELATIONSHIPS_RELATIVE
        ).relationships.values()
    )
    user_by_id = (
        dict(RelationshipStore(app_root / RELATIONSHIPS_RELATIVE).relationships)
        if include_user_relationships else {}
    )
    user_by_id.update(
        (relationship.id, relationship)
        for relationship in additional_user_relationships
    )
    user_relationships = tuple(user_by_id.values())
    return character_record_layered_compatibility_payloads(
        record_kind,
        records,
        default_relationships,
        user_relationships,
    )


def _existing_collection_records(
    app_root: Path, filename: str, key: str
) -> list[dict[str, Any]]:
    default_records = _read_records(
        app_root / "data" / "defaults" / filename, key
    )
    user_records = _read_records(
        app_root / "data" / "user" / filename, key
    )
    record_kind = {
        ("series.json", "series"): "series",
        ("characters.json", "characters"): "character",
        ("outfits.json", "outfits"): "outfit",
    }.get((filename, key))
    if record_kind is None:
        return [*default_records, *user_records]
    return [
        *_layered_character_records(
            app_root,
            record_kind,
            default_records,
            include_user_relationships=False,
        ),
        *_layered_character_records(
            app_root,
            record_kind,
            user_records,
            include_user_relationships=True,
        ),
    ]


def _remap_collection_context(
    records: list[dict[str, Any]],
    *,
    source_ids: dict[str, str] | None = None,
    character_ids: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Map contextual identity fields before matching cross-install records."""
    source_ids = source_ids or {}
    character_ids = character_ids or {}
    remapped: list[dict[str, Any]] = []
    for item in records:
        value = dict(item)
        source_id = str(value.get("series_id") or "")
        if source_id:
            value["series_id"] = source_ids.get(source_id, source_id)
        if isinstance(value.get("character_ids"), list):
            value["character_ids"] = [
                character_ids.get(str(character_id), str(character_id))
                for character_id in value["character_ids"]
            ]
        remapped.append(value)
    return remapped


def _known_mapped_ids(
    existing: list[dict[str, Any]],
    imported: list[dict[str, Any]],
    remap: dict[str, str],
) -> set[str]:
    values = {
        str(item.get("id") or "") for item in (*existing, *imported)
    }
    return {remap.get(value, value) for value in values if value}


def _tag_identity_owners(
    records: list[dict[str, Any]], tag_ids: dict[str, str]
) -> dict[str, set[str]]:
    owners: dict[str, set[str]] = {}
    for item in records:
        item_id = str(item.get("id") or "")
        if not item_id:
            continue
        owner_id = tag_ids.get(item_id, item_id)
        for value in (item.get("display"), *(item.get("aliases") or ())):
            identity = _normalized(value)
            if identity:
                owners.setdefault(identity, set()).add(owner_id)
    return owners


def _semantic_id_remap(
    imported: list[dict[str, Any]],
    existing: list[dict[str, Any]],
    key: str,
) -> tuple[dict[str, str], set[str]]:
    existing_ids = {
        str(item.get("id") or "") for item in existing if str(item.get("id") or "")
    }
    identities: dict[tuple[Any, ...], set[str]] = {}
    for item in existing:
        item_id = str(item.get("id") or "")
        if not item_id:
            continue
        for identity in _record_identities(item, key):
            identities.setdefault(identity, set()).add(item_id)
    remap: dict[str, str] = {}
    ambiguous: set[str] = set()
    for item in imported:
        item_id = str(item.get("id") or "")
        if not item_id:
            continue
        if item_id in existing_ids:
            remap[item_id] = item_id
            continue
        matches = {
            candidate
            for identity in _record_identities(item, key)
            for candidate in identities.get(identity, ())
        }
        if len(matches) == 1:
            remap[item_id] = next(iter(matches))
        elif len(matches) > 1:
            ambiguous.add(item_id)
        else:
            remap[item_id] = item_id
    return remap, ambiguous


_PORTABLE_RECORD_SPECS = (
    ("tag", "tags.json", "tags", "SDXL Tag", ""),
    (
        "merge_rule", "tags.json", "merge_rules", "Knowledge rule",
        "disabled_default_merge_rule_ids",
    ),
    ("category", "categories.json", "categories", "Category", "disabled_default_ids"),
    ("pattern", "patterns.json", "patterns", "Reusable pattern", "disabled_default_ids"),
    ("series", "series.json", "series", "Character Source", "disabled_default_ids"),
    ("character", "characters.json", "characters", "Character", "disabled_default_ids"),
    ("outfit", "outfits.json", "outfits", "Outfit", "disabled_default_ids"),
    (
        "consumer_policy", "consumer_policies.json", "policies",
        "Consumer policy", "",
    ),
)


def _record_label(item: dict[str, Any], stable_id: str) -> str:
    if isinstance(item.get("selector"), dict):
        selector = item["selector"]
        return (
            f"{str(selector.get('type') or 'policy').replace('_', ' ').title()} "
            f"{str(selector.get('id') or stable_id)} — "
            f"{str(item.get('consumer') or 'consumer').replace('_', ' ').title()}"
        )
    return str(
        item.get("display")
        or item.get("name")
        or item.get("target_tag")
        or stable_id
    )


def _remap_record_payload(
    kind: str,
    item: dict[str, Any],
    *,
    tag_ids: dict[str, str],
    category_ids: dict[str, str],
    source_ids: dict[str, str],
    character_ids: dict[str, str],
    outfit_ids: dict[str, str],
    pattern_ids: dict[str, str],
    merge_rule_ids: dict[str, str],
    relationship_ids: dict[str, str],
) -> dict[str, Any]:
    value = json.loads(json.dumps(item))
    remap_by_kind = {
        "tag": tag_ids, "category": category_ids, "series": source_ids,
        "character": character_ids, "outfit": outfit_ids,
        "pattern": pattern_ids, "merge_rule": merge_rule_ids,
    }
    stable_id = str(value.get("id") or "")
    if stable_id and kind in remap_by_kind:
        value["id"] = remap_by_kind[kind].get(stable_id, stable_id)
    if kind == "tag":
        category_id = str(value.get("category") or "")
        if category_id:
            value["category"] = category_ids.get(category_id, category_id)
        relations = value.get("relations")
        if isinstance(relations, dict):
            parent = str(relations.get("parent") or "")
            if parent:
                relations["parent"] = tag_ids.get(parent, parent)
            for field in ("attributes", "implies", "conflicts"):
                if isinstance(relations.get(field), list):
                    relations[field] = [
                        tag_ids.get(str(record_id), str(record_id))
                        for record_id in relations[field]
                    ]
    elif kind == "category":
        parent = str(value.get("parent_id") or "")
        if parent:
            value["parent_id"] = category_ids.get(parent, parent)
        base_tag = str(value.get("base_tag_id") or "")
        if base_tag:
            value["base_tag_id"] = tag_ids.get(base_tag, base_tag)
    elif kind == "pattern":
        category_id = str(value.get("category") or "")
        if category_id:
            value["category"] = category_ids.get(category_id, category_id)
    elif kind in {"series", "character", "outfit"}:
        source_id = str(value.get("series_id") or "")
        if source_id:
            value["series_id"] = source_ids.get(source_id, source_id)
        link = value.get("knowledge_tag")
        if isinstance(link, dict) and str(link.get("tag_id") or ""):
            link["tag_id"] = tag_ids.get(str(link["tag_id"]), str(link["tag_id"]))
        if kind == "character":
            base_id = str(value.get("base_character_id") or "")
            if base_id:
                value["base_character_id"] = character_ids.get(base_id, base_id)
        if kind == "outfit" and isinstance(value.get("character_ids"), list):
            value["character_ids"] = [
                character_ids.get(str(record_id), str(record_id))
                for record_id in value["character_ids"]
            ]
    elif kind == "consumer_policy":
        selector = value.get("selector")
        if isinstance(selector, dict):
            selector_type = str(selector.get("type") or "")
            selector_id = str(selector.get("id") or "")
            if selector_type == EXACT_TAG:
                selector_id = tag_ids.get(selector_id, selector_id)
            elif selector_type == EXACT_RELATIONSHIP:
                selector_id = relationship_ids.get(selector_id, selector_id)
            selector["id"] = selector_id
            value["id"] = consumer_policy_record_id(
                selector_type,
                selector_id,
                str(value.get("consumer") or ""),
                value.get("conditions")
                if isinstance(value.get("conditions"), dict)
                else None,
            )
        value["layer"] = "user"
    return value


def _record_dependency_issue(
    kind: str,
    item: dict[str, Any],
    *,
    known_tag_ids: set[str],
    known_tag_names: dict[str, set[str]],
    known_category_ids: set[str],
    known_source_ids: set[str],
    known_character_ids: set[str],
    known_relationship_ids: set[str],
    ambiguous_tag_ids: set[str],
    ambiguous_category_ids: set[str],
    ambiguous_source_ids: set[str],
    ambiguous_character_ids: set[str],
) -> str:
    label = _record_label(item, str(item.get("id") or "record"))
    if kind == "tag":
        category_id = str(item.get("category") or "")
        if category_id in ambiguous_category_ids:
            return f"{label} references an ambiguous Category identity."
        if category_id and category_id != "uncategorized" and category_id not in known_category_ids:
            return f"{label} references missing Category {category_id!r}."
        relations = item.get("relations") if isinstance(item.get("relations"), dict) else {}
        values: list[str] = []
        parent = str(relations.get("parent") or "")
        if parent:
            values.append(parent)
        for field in ("attributes", "implies", "conflicts"):
            values.extend(str(value) for value in relations.get(field, ()) or ())
        ambiguous = next((value for value in values if value in ambiguous_tag_ids), "")
        if ambiguous:
            return f"{label} references an ambiguous Tag identity."
        missing = next((value for value in values if value not in known_tag_ids), "")
        if missing:
            return f"{label} references missing Tag {missing!r}."
        owners = set(known_tag_names.get(_normalized(item.get("display")), set()))
        owners.update(
            owner
            for alias in item.get("aliases", ())
            for owner in known_tag_names.get(_normalized(alias), set())
        )
        owners.discard(str(item.get("id") or ""))
        if owners:
            return f"{label} claims a canonical name or alias already owned by another Tag."
    elif kind == "category":
        parent = str(item.get("parent_id") or "")
        if parent in ambiguous_category_ids:
            return f"{label} references an ambiguous parent Category identity."
        if parent and parent not in known_category_ids:
            return f"{label} references missing parent Category {parent!r}."
        base_tag = str(item.get("base_tag_id") or "")
        if base_tag in ambiguous_tag_ids:
            return f"{label} references an ambiguous base Tag identity."
        if base_tag and base_tag not in known_tag_ids:
            return f"{label} references missing base Tag {base_tag!r}."
    elif kind == "pattern":
        category_id = str(item.get("category") or "")
        if category_id in ambiguous_category_ids:
            return f"{label} references an ambiguous Category identity."
        if category_id and category_id not in known_category_ids:
            return f"{label} references missing Category {category_id!r}."
    elif kind in {"series", "character", "outfit"}:
        source_id = str(item.get("series_id") or "")
        if source_id in ambiguous_source_ids:
            return f"{label} references an ambiguous Character Source identity."
        if source_id and source_id not in known_source_ids:
            return f"{label} references missing Character Source {source_id!r}."
        link = item.get("knowledge_tag") if isinstance(item.get("knowledge_tag"), dict) else {}
        tag_id = str(link.get("tag_id") or "")
        if tag_id in ambiguous_tag_ids:
            return f"{label} references an ambiguous representative Tag identity."
        if tag_id and tag_id not in known_tag_ids:
            return f"{label} references missing representative Tag {tag_id!r}."
        if kind == "character":
            base_id = str(item.get("base_character_id") or "")
            if base_id in ambiguous_character_ids:
                return f"{label} references an ambiguous broader Character identity."
            if base_id and base_id not in known_character_ids:
                return f"{label} references missing broader Character {base_id!r}."
        if kind == "outfit":
            if any(
                str(value) in ambiguous_character_ids
                for value in item.get("character_ids", ())
            ):
                return f"{label} references an ambiguous Character identity."
            missing = next(
                (
                    str(value) for value in item.get("character_ids", ())
                    if str(value) not in known_character_ids
                ),
                "",
            )
            if missing:
                return f"{label} references missing Character {missing!r}."
    elif kind == "merge_rule":
        concrete = [
            str(value) for value in (*item.get("source_tags", ()), item.get("target_tag", ""))
            if str(value).strip() and "{" not in str(value)
        ]
        missing = next(
            (value for value in concrete if _normalized(value) not in known_tag_names),
            "",
        )
        if missing:
            return f"{label} references missing concrete Tag {missing!r}."
    elif kind == "consumer_policy":
        record = ConsumerPolicyRecord.from_dict(item)
        errors = record.validation_errors()
        if errors:
            return " ".join(errors)
        if record.selector_type == EXACT_TAG:
            if record.selector_id in ambiguous_tag_ids:
                return f"{label} references an ambiguous Tag identity."
            if record.selector_id not in known_tag_ids:
                return f"{label} references missing Tag {record.selector_id!r}."
        elif (
            record.selector_type == EXACT_RELATIONSHIP
            and record.selector_id not in known_relationship_ids
        ):
            return f"{label} references missing relationship {record.selector_id!r}."
        elif (
            record.selector_type == RELATIONSHIP_KIND
            and record.selector_id not in CURRENT_RELATIONSHIP_KINDS
        ):
            return f"{label} references unknown relationship kind {record.selector_id!r}."
        elif record.selector_type not in {
            EXACT_TAG,
            EXACT_RELATIONSHIP,
            RELATIONSHIP_KIND,
            COMPATIBILITY_TERM,
            CONSUMER_DEFAULT,
        }:
            return f"{label} has an unsupported selector."
    return ""


def _record_import_plan(
    app_root: Path,
    work: Path,
    manifest: dict[str, Any],
    *,
    additional_user_relationships: Iterable[KnowledgeRelationship] = (),
    relationship_ids: dict[str, str] | None = None,
) -> tuple[
    tuple[KnowledgePackRecordDecision, ...],
    tuple[KnowledgePackRecordPatch, ...],
    tuple[str, ...],
]:
    schema_version = int(manifest.get("schema_version", 1) or 1)
    if schema_version < 8:
        return (), (), ()
    relationship_ids = dict(relationship_ids or {})
    policy_path = work / "data" / "user" / "consumer_policies.json"
    if policy_path.exists() and schema_version < 9:
        return (), (), (
            "Consumer-policy patches require Knowledge Pack schema 9.",
        )
    if policy_path.exists():
        try:
            policy_store = ConsumerPolicyStore(policy_path, expected_layer="user")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            return (), (), (f"The consumer-policy patch is malformed: {error}",)
        if not policy_store.runtime_supported:
            return (), (), (
                "The consumer-policy patch uses a newer unsupported schema.",
            )
        policy_errors = [
            error
            for record in policy_store.records
            for error in record.validation_errors()
        ]
        if policy_errors:
            return (), (), (
                "The consumer-policy patch is not safe to apply: "
                + " ".join(policy_errors[:3]),
            )
    local_policy_path = app_root / "data" / "user" / "consumer_policies.json"
    if local_policy_path.exists():
        try:
            local_policy_store = ConsumerPolicyStore(
                local_policy_path, expected_layer="user"
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            return (), (), (f"Local consumer-policy authority is malformed: {error}",)
        if not local_policy_store.runtime_supported:
            return (), (), (
                "Local consumer-policy authority uses a newer unsupported schema.",
            )

    imported: dict[str, list[dict[str, Any]]] = {}
    payloads: dict[str, dict[str, Any]] = {}
    for kind, filename, key, _label, _disabled_key in _PORTABLE_RECORD_SPECS:
        path = work / "data" / "user" / filename
        if filename not in payloads:
            payloads[filename] = _read_payload_checked(path) if path.exists() else {}
        payload = payloads[filename]
        values = payload.get(key, [])
        if not _shared_is_record_list(values):
            return (), (), (f"The {key} patch contains an invalid record list.",)
        imported[kind] = [json.loads(json.dumps(item)) for item in values]
    disabled_record_count = sum(
        len(payloads[filename].get(disabled_key, ()))
        for _kind, filename, _key, _label, disabled_key in _PORTABLE_RECORD_SPECS
        if disabled_key and isinstance(payloads[filename].get(disabled_key, ()), list)
    )
    if (
        sum(len(values) for values in imported.values()) + disabled_record_count
        > MAX_PACK_RECORDS
    ):
        return (), (), (
            "Knowledge Pack non-relationship patches are limited to "
            f"{MAX_PACK_RECORDS:,} records.",
        )
    tags_payload = payloads.get("tags.json", {})
    for field in ("removed_tag_ids", "legacy_removed_tag_ids"):
        if not isinstance(tags_payload.get(field, []), list):
            return (), (), (f"The Tag patch contains an invalid {field} list.",)
    if not isinstance(tags_payload.get("merged_tag_ids", {}), dict):
        return (), (), ("The Tag patch contains an invalid merged-ID map.",)

    existing = {
        "tag": _existing_tag_records(app_root),
        "category": _existing_category_records(app_root),
        "series": _existing_collection_records(app_root, "series.json", "series"),
        "character": _existing_collection_records(app_root, "characters.json", "characters"),
        "outfit": _existing_collection_records(app_root, "outfits.json", "outfits"),
        "pattern": _existing_collection_records(app_root, "patterns.json", "patterns"),
        "merge_rule": [
            *_default_records_for_kind(app_root, "merge_rule"),
            *_read_records(app_root / "data" / "user" / "tags.json", "merge_rules"),
        ],
        "consumer_policy": [
            *_default_records_for_kind(app_root, "consumer_policy"),
            *_read_records(
                app_root / "data" / "user" / "consumer_policies.json", "policies"
            ),
        ],
    }
    tag_remap, ambiguous_tags = _semantic_id_remap(imported["tag"], existing["tag"], "tags")
    category_remap, ambiguous_categories = _semantic_id_remap(
        imported["category"], existing["category"], "categories"
    )
    source_remap, ambiguous_sources = _semantic_id_remap(
        imported["series"], existing["series"], "series"
    )
    contextual_characters = _remap_collection_context(
        imported["character"], source_ids=source_remap
    )
    character_remap, ambiguous_characters = _semantic_id_remap(
        contextual_characters, existing["character"], "characters"
    )
    ambiguous_characters.update(
        str(item.get("id") or "") for item in imported["character"]
        if str(item.get("series_id") or "") in ambiguous_sources
    )
    for stable_id in ambiguous_characters:
        character_remap.pop(stable_id, None)
    contextual_outfits = _remap_collection_context(
        imported["outfit"], source_ids=source_remap, character_ids=character_remap
    )
    outfit_remap, ambiguous_outfits = _semantic_id_remap(
        contextual_outfits, existing["outfit"], "outfits"
    )
    ambiguous_outfits.update(
        str(item.get("id") or "") for item in imported["outfit"]
        if (
            str(item.get("series_id") or "") in ambiguous_sources
            or any(
                str(value) in ambiguous_characters
                for value in (item.get("character_ids") or ())
            )
        )
    )
    for stable_id in ambiguous_outfits:
        outfit_remap.pop(stable_id, None)
    pattern_remap, ambiguous_patterns = _semantic_id_remap(
        imported["pattern"], existing["pattern"], "patterns"
    )
    rule_remap, ambiguous_rules = _semantic_id_remap(
        imported["merge_rule"], existing["merge_rule"], "merge_rules"
    )
    policy_remap = {
        str(item.get("id") or ""): str(item.get("id") or "")
        for item in imported["consumer_policy"]
        if str(item.get("id") or "")
    }
    remaps = {
        "tag": tag_remap, "category": category_remap, "series": source_remap,
        "character": character_remap, "outfit": outfit_remap,
        "pattern": pattern_remap, "merge_rule": rule_remap,
        "consumer_policy": policy_remap,
    }
    ambiguous = {
        "tag": ambiguous_tags, "category": ambiguous_categories,
        "series": ambiguous_sources, "character": ambiguous_characters,
        "outfit": ambiguous_outfits, "pattern": ambiguous_patterns,
        "merge_rule": ambiguous_rules,
        "consumer_policy": set(),
    }

    mapped: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for kind, _filename, _key, _label, _disabled_key in _PORTABLE_RECORD_SPECS:
        mapped[kind] = [
            (
                original,
                _remap_record_payload(
                    kind, original, tag_ids=tag_remap,
                    category_ids=category_remap, source_ids=source_remap,
                    character_ids=character_remap, outfit_ids=outfit_remap,
                    pattern_ids=pattern_remap, merge_rule_ids=rule_remap,
                    relationship_ids=relationship_ids,
                ),
            )
            for original in imported[kind]
        ]

    known_tag_ids = _known_mapped_ids(existing["tag"], imported["tag"], tag_remap)
    known_category_ids = _known_mapped_ids(
        existing["category"], imported["category"], category_remap
    )
    known_source_ids = _known_mapped_ids(existing["series"], imported["series"], source_remap)
    known_character_ids = _known_mapped_ids(
        existing["character"], contextual_characters, character_remap
    )
    known_tag_names = _tag_identity_owners(
        [*existing["tag"], *(item for _original, item in mapped["tag"])], tag_remap
    )
    known_relationship_ids = {
        relationship.id
        for relationship in (
            *RelationshipStore(
                app_root / DEFAULT_RELATIONSHIPS_RELATIVE
            ).relationships.values(),
            *RelationshipStore(
                app_root / RELATIONSHIPS_RELATIVE
            ).relationships.values(),
            *additional_user_relationships,
        )
    }
    known_relationship_ids.update(relationship_ids.values())

    state_path = app_root / "data" / "user" / "default_knowledge_state.json"
    state = _read_payload_checked(state_path) if state_path.exists() else {}
    state_records = state.get("records") if isinstance(state.get("records"), dict) else {}
    decisions: list[KnowledgePackRecordDecision] = []
    patches: list[KnowledgePackRecordPatch] = []
    seen: dict[str, set[str]] = {kind: set() for kind in imported}

    for kind, filename, key, kind_label, disabled_key in _PORTABLE_RECORD_SPECS:
        user_payload = _read_payload_checked(app_root / "data" / "user" / filename)
        local_records = [
            item for item in user_payload.get(key, ()) if isinstance(item, dict)
        ]
        defaults = _default_records_for_kind(app_root, kind)
        if kind in {"series", "character", "outfit"}:
            defaults = _layered_character_records(
                app_root,
                kind,
                defaults,
                include_user_relationships=False,
            )
            local_records = _layered_character_records(
                app_root,
                kind,
                local_records,
                include_user_relationships=True,
                additional_user_relationships=additional_user_relationships,
            )
        default_by_id = {
            str(item.get("id") or ""): item for item in defaults if str(item.get("id") or "")
        }
        local_by_id = {
            str(item.get("id") or ""): item for item in local_records if str(item.get("id") or "")
        }
        local_disabled = {
            str(value) for value in user_payload.get(disabled_key, ())
            if disabled_key and str(value).strip()
        }
        kind_state = state_records.get(RECORD_STATE_KEY[kind])
        kind_state = kind_state if isinstance(kind_state, dict) else {}
        base_payload = payloads[filename]
        if filename == "tags.json":
            grouped = base_payload.get("record_base_fingerprints")
            grouped = grouped if isinstance(grouped, dict) else {}
            base_fingerprints = grouped.get(key)
            grouped_lifecycle = base_payload.get("record_lifecycle_owned_ids")
            grouped_lifecycle = (
                grouped_lifecycle if isinstance(grouped_lifecycle, dict) else {}
            )
            lifecycle_owned = grouped_lifecycle.get(key, [])
        else:
            base_fingerprints = base_payload.get("base_fingerprints")
            lifecycle_owned = base_payload.get("lifecycle_owned_ids", [])
        if not isinstance(base_fingerprints, dict):
            base_fingerprints = {}
        if not isinstance(lifecycle_owned, list):
            return (), (), (f"The {kind_label} lifecycle ownership list is invalid.",)
        lifecycle_owned_ids = {
            str(value) for value in lifecycle_owned if str(value).strip()
        }
        if kind in {"tag", "category"}:
            lifecycle_key = (
                "related_tags_after" if kind == "tag" else "categories_after"
            )
            tags_payload = payloads.get("tags.json", {})
            archive_owned_ids = {
                str(stable_id)
                for archive in tags_payload.get("removed_tag_archives", ())
                if isinstance(archive, dict) and not str(archive.get("restored_at") or "")
                for stable_id in (
                    archive.get(lifecycle_key, {})
                    if isinstance(archive.get(lifecycle_key), dict) else {}
                )
            }
            imported_ids = {
                str(item.get("id") or "") for item in imported[kind]
                if str(item.get("id") or "")
            }
            expected_lifecycle_ids = archive_owned_ids.intersection(imported_ids)
            if lifecycle_owned_ids != expected_lifecycle_ids:
                return (), (), (
                    f"The {kind_label} lifecycle ownership does not match its removal archive.",
                )

        for original, record in mapped[kind]:
            original_id = str(original.get("id") or "")
            stable_id = str(record.get("id") or "")
            if original_id in lifecycle_owned_ids:
                continue
            outcome = "add"
            reason = "This is a new reviewed user record."
            if not original_id or not stable_id:
                outcome = RELATIONSHIP_HOLD
                reason = "A portable record is missing its stable ID."
            elif original_id in ambiguous[kind]:
                outcome = RELATIONSHIP_HOLD
                reason = f"{kind_label} identity matches multiple local records."
            elif stable_id in seen[kind]:
                outcome = RELATIONSHIP_HOLD
                reason = f"Two imported {kind_label} records resolve to the same identity."
            else:
                dependency = _record_dependency_issue(
                    kind, record, known_tag_ids=known_tag_ids,
                    known_tag_names=known_tag_names,
                    known_category_ids=known_category_ids,
                    known_source_ids=known_source_ids,
                    known_character_ids=known_character_ids,
                    known_relationship_ids=known_relationship_ids,
                    ambiguous_tag_ids=ambiguous_tags,
                    ambiguous_category_ids=ambiguous_categories,
                    ambiguous_source_ids=ambiguous_sources,
                    ambiguous_character_ids=ambiguous_characters,
                )
                if dependency:
                    outcome = RELATIONSHIP_HOLD
                    reason = dependency
            seen[kind].add(stable_id)
            current_default = default_by_id.get(stable_id)
            existing_local = local_by_id.get(stable_id)
            state_record = kind_state.get(
                str(current_default.get("id") or "") if current_default else stable_id
            )
            state_record = state_record if isinstance(state_record, dict) else {}
            override_fields = {
                str(value) for value in state_record.get("override_fields", ())
                if str(value).strip()
            }
            local_is_materialized_default = bool(
                current_default is not None
                and existing_local is not None
                and not override_fields
                and _record_comparable(kind, existing_local, ignore_id=True)
                == _record_comparable(kind, current_default, ignore_id=True)
            )
            if outcome != RELATIONSHIP_HOLD:
                if stable_id in local_disabled:
                    outcome = RELATIONSHIP_HOLD
                    reason = f"The matching local {kind_label} default is disabled."
                elif existing_local is not None and _record_comparable(
                    kind, existing_local, ignore_id=True
                ) == _record_comparable(kind, record, ignore_id=True):
                    outcome = "already_present"
                    reason = "The same reviewed local record is already present."
                elif existing_local is not None and not local_is_materialized_default:
                    outcome = RELATIONSHIP_HOLD
                    reason = f"A different local {kind_label} already owns this identity."
                elif current_default is not None and _record_comparable(
                    kind, current_default, ignore_id=True
                ) == _record_comparable(kind, record, ignore_id=True):
                    outcome = "already_present"
                    reason = "The current shipped default already provides this record."
                else:
                    expected_base = str(base_fingerprints.get(original_id) or "")
                    if expected_base == "missing" and current_default is None:
                        outcome = "add"
                    elif not expected_base:
                        outcome = RELATIONSHIP_HOLD
                        reason = "The pack does not record the default base for this record."
                    elif current_default is None:
                        outcome = RELATIONSHIP_HOLD
                        reason = "The default record used by this patch is no longer present."
                    elif _record_fingerprint(kind, current_default) != expected_base:
                        outcome = RELATIONSHIP_HOLD
                        reason = "The shipped default changed after this record patch was created."
                    else:
                        outcome = "override"
                        reason = "This reviewed record overrides the matching current default."
            consequence = (
                _rule_effect(record, action=outcome.replace("_", " ").title()).consequence
                if kind == "merge_rule" else ""
            )
            decisions.append(KnowledgePackRecordDecision(
                stable_id, kind, _record_label(record, stable_id), outcome, reason,
                consequence,
            ))
            patches.append(KnowledgePackRecordPatch(
                kind, filename, key, stable_id, stable_id, record,
                source_id=original_id,
            ))

        if disabled_key:
            disabled_values = base_payload.get(disabled_key, [])
            if not isinstance(disabled_values, list):
                return (), (), (f"The {kind_label} disabled-default list is invalid.",)
            for original_id in (str(value) for value in disabled_values if str(value).strip()):
                stable_id = remaps[kind].get(original_id, original_id)
                current_default = default_by_id.get(stable_id)
                existing_local = local_by_id.get(stable_id)
                state_record = kind_state.get(stable_id)
                state_record = state_record if isinstance(state_record, dict) else {}
                override_fields = {
                    str(value) for value in state_record.get("override_fields", ())
                    if str(value).strip()
                }
                local_is_materialized_default = bool(
                    current_default is not None
                    and existing_local is not None
                    and not override_fields
                    and _record_comparable(kind, existing_local, ignore_id=True)
                    == _record_comparable(kind, current_default, ignore_id=True)
                )
                expected_base = str(base_fingerprints.get(original_id) or "")
                outcome = "disable_default"
                reason = "This pack disables the matching current default record."
                if stable_id in seen[kind]:
                    outcome = RELATIONSHIP_HOLD
                    reason = (
                        f"The pack contains more than one action for this {kind_label}."
                    )
                elif stable_id in local_disabled:
                    outcome = "already_present"
                    reason = "This default record is already disabled locally."
                elif current_default is None:
                    outcome = RELATIONSHIP_HOLD
                    reason = "The default record to disable is no longer present."
                elif not expected_base or _record_fingerprint(kind, current_default) != expected_base:
                    outcome = RELATIONSHIP_HOLD
                    reason = "The shipped default changed after this disable decision was created."
                elif existing_local is not None and not local_is_materialized_default:
                    outcome = RELATIONSHIP_HOLD
                    reason = "A local override must be reconciled before disabling this default."
                consequence = (
                    _rule_effect(
                        current_default or {"id": stable_id},
                        action=outcome.replace("_", " ").title(),
                    ).consequence
                    if kind == "merge_rule" else ""
                )
                decisions.append(KnowledgePackRecordDecision(
                    stable_id, kind, _record_label(current_default or {}, stable_id),
                    outcome, reason, consequence,
                ))
                patches.append(KnowledgePackRecordPatch(
                    kind, filename, key, stable_id, stable_id, None, disabled_key,
                    original_id,
                ))
                seen[kind].add(stable_id)
    return tuple(decisions), tuple(patches), ()


def _read_payload_checked(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object.")
    return payload


def _remap_relationship(
    relationship: KnowledgeRelationship,
    *,
    tag_ids: dict[str, str],
    category_ids: dict[str, str],
    source_ids: dict[str, str],
    character_ids: dict[str, str],
    outfit_ids: dict[str, str],
) -> KnowledgeRelationship:
    endpoints: list[RelationshipEndpoint] = []
    changed = False
    for endpoint in relationship.endpoints:
        endpoint_id = endpoint.id
        if endpoint.kind == "tag":
            endpoint_id = tag_ids.get(endpoint_id, endpoint_id)
        elif endpoint.kind in {"category", "modifier_dimension"}:
            endpoint_id = category_ids.get(endpoint_id, endpoint_id)
        elif endpoint.kind == "character_source":
            endpoint_id = source_ids.get(endpoint_id, endpoint_id)
        elif endpoint.kind == "character":
            endpoint_id = character_ids.get(endpoint_id, endpoint_id)
        elif endpoint.kind == "outfit":
            endpoint_id = outfit_ids.get(endpoint_id, endpoint_id)
        changed = changed or endpoint_id != endpoint.id
        endpoints.append(replace(endpoint, id=endpoint_id))

    details = dict(relationship.details)
    family_id = details.get("modifier_dimension_id", "")
    mapped_family_id = category_ids.get(family_id, family_id)
    if mapped_family_id != family_id:
        details["modifier_dimension_id"] = mapped_family_id
        changed = True

    stable_id = relationship.id
    if changed and endpoints:
        source_id = endpoints[0].id
        if relationship.kind == MODIFIER_ROLE:
            from .modifier_role_decisions import modifier_role_relationship_id

            stable_id = modifier_role_relationship_id(source_id)
        elif relationship.kind == MODIFIER_VALUE and mapped_family_id:
            from .modifier_composition import modifier_composition_relationship_id

            stable_id = modifier_composition_relationship_id(source_id, mapped_family_id)
        elif relationship.kind == ALLOWED_DIMENSION:
            from .allowed_dimension_policy import (
                allowed_dimension_none_relationship_id,
                allowed_dimension_relationship_id,
            )

            targets = endpoints[1:]
            if (
                len(targets) == 1
                and targets[0].kind == ALLOWED_DIMENSION_STATE
                and targets[0].id == NO_ALLOWED_DIMENSIONS
            ):
                stable_id = allowed_dimension_none_relationship_id(source_id)
            elif targets and targets[0].kind == "modifier_dimension":
                stable_id = allowed_dimension_relationship_id(source_id, targets[0].id)
            else:
                encoded = "\x1f".join(
                    [source_id, *(f"{item.kind}:{item.id}" for item in targets)]
                ).encode("utf-8")
                stable_id = f"allowed_dimension.{sha256(encoded).hexdigest()[:20]}"
    return replace(
        relationship,
        id=stable_id,
        endpoints=tuple(endpoints),
        details=tuple((str(key), str(value)) for key, value in details.items()),
        layer="user",
    )


def _relationship_label(relationship: KnowledgeRelationship) -> str:
    if relationship.label:
        return relationship.label
    if relationship.endpoints:
        source = relationship.endpoints[0]
        return source.label or source.id
    return relationship.id


def _relationship_dependency_issue(
    relationship: KnowledgeRelationship,
    *,
    known_tag_ids: set[str],
    known_category_ids: set[str],
    known_source_ids: set[str],
    known_character_ids: set[str],
    known_outfit_ids: set[str],
    ambiguous_tag_ids: set[str],
    ambiguous_category_ids: set[str],
    ambiguous_source_ids: set[str],
    ambiguous_character_ids: set[str],
    ambiguous_outfit_ids: set[str],
    alias_owners: dict[str, set[str]],
) -> str:
    for endpoint in relationship.endpoints:
        if endpoint.kind == "tag":
            if endpoint.id in ambiguous_tag_ids:
                return f"Tag dependency {endpoint.id!r} matches multiple local Tags."
            if endpoint.id not in known_tag_ids:
                return f"Tag dependency {endpoint.id!r} is missing from this pack and installation."
        if endpoint.kind in {"category", "modifier_dimension"}:
            if endpoint.id in ambiguous_category_ids:
                return f"Category dependency {endpoint.id!r} matches multiple local Categories."
            if endpoint.id not in known_category_ids:
                return f"Category dependency {endpoint.id!r} is missing from this pack and installation."
        if endpoint.kind == "character_source":
            if endpoint.id in ambiguous_source_ids:
                return f"Character Source dependency {endpoint.id!r} matches multiple local records."
            if endpoint.id not in known_source_ids:
                return f"Character Source dependency {endpoint.id!r} is missing from this pack and installation."
        if endpoint.kind == "character":
            if endpoint.id in ambiguous_character_ids:
                return f"Character dependency {endpoint.id!r} matches multiple local records."
            if endpoint.id not in known_character_ids:
                return f"Character dependency {endpoint.id!r} is missing from this pack and installation."
        if endpoint.kind == "outfit":
            if endpoint.id in ambiguous_outfit_ids:
                return f"Outfit dependency {endpoint.id!r} matches multiple local records."
            if endpoint.id not in known_outfit_ids:
                return f"Outfit dependency {endpoint.id!r} is missing from this pack and installation."
    if relationship.kind == IDENTITY_ALIAS and len(relationship.endpoints) == 2:
        alias = _normalized(relationship.endpoints[0].id)
        target_id = relationship.endpoints[1].id
        conflicting = alias_owners.get(alias, set()) - {target_id}
        if conflicting:
            return (
                f"Other Name {relationship.endpoints[0].id!r} is already owned by "
                f"another Tag identity."
            )
    return ""


def _load_manifest(work: Path, *, foundation=False) -> dict[str, Any]:
    manifest_path = work / "manifest.json"
    if not manifest_path.exists():
        raise ValueError(
            "This ZIP does not contain a CollectiGen Knowledge Pack manifest."
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return _shared_validate_manifest(manifest, supported_schema=SUPPORTED_PACK_SCHEMA_VERSION, allow_foundation=foundation)


def _relationship_import_plan(
    app_root: Path,
    work: Path,
    manifest: dict[str, Any],
) -> tuple[
    tuple[KnowledgePackRelationshipDecision, ...],
    tuple[KnowledgeRelationship, ...],
    tuple[str, ...],
]:
    relationship_path = work / RELATIONSHIPS_RELATIVE
    if not relationship_path.exists():
        return (), (), ()
    schema_version = int(manifest.get("schema_version", 1) or 1)
    if schema_version < 5:
        return (), (), (
            "Legacy Knowledge Packs cannot carry typed relationship patches.",
        )
    payload = json.loads(relationship_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return (), (), ("The relationship patch must contain a JSON object.",)
    relationship_values = payload.get("relationships", ())
    if not _shared_is_record_list(relationship_values):
        return (), (), (
            "The relationship patch contains an invalid relationship record list.",
        )
    raw_relationships = list(relationship_values)
    if len(raw_relationships) > MAX_PACK_RELATIONSHIPS:
        return (), (), (
            "Knowledge Pack relationship patches are limited to "
            f"{MAX_PACK_RELATIONSHIPS:,} records.",
        )
    raw_base_fingerprints = payload.get("base_fingerprints") or {}
    if not isinstance(raw_base_fingerprints, dict):
        return (), (), (
            "The relationship patch contains an invalid base-fingerprint map.",
        )
    base_fingerprints = {
        str(key): str(value)
        for key, value in raw_base_fingerprints.items()
    }

    imported_tags_path = work / "data" / "user" / "tags.json"
    if not imported_tags_path.exists():
        imported_tags_path = work / "data" / "tags.json"
    imported_tags = _read_records(imported_tags_path, "tags")
    imported_categories = _read_records(
        work / "data" / "user" / "categories.json", "categories"
    )
    imported_sources = _read_records(
        work / "data" / "user" / "series.json", "series"
    )
    imported_characters = _read_records(
        work / "data" / "user" / "characters.json", "characters"
    )
    imported_outfits = _read_records(
        work / "data" / "user" / "outfits.json", "outfits"
    )
    existing_tags = _existing_tag_records(app_root)
    existing_categories = _existing_category_records(app_root)
    existing_sources = _existing_collection_records(
        app_root, "series.json", "series"
    )
    existing_characters = _existing_collection_records(
        app_root, "characters.json", "characters"
    )
    existing_outfits = _existing_collection_records(
        app_root, "outfits.json", "outfits"
    )
    tag_remap, ambiguous_tags = _semantic_id_remap(
        imported_tags, existing_tags, "tags"
    )
    category_remap, ambiguous_categories = _semantic_id_remap(
        imported_categories, existing_categories, "categories"
    )
    source_remap, ambiguous_sources = _semantic_id_remap(
        imported_sources, existing_sources, "series"
    )
    contextual_characters = _remap_collection_context(
        imported_characters, source_ids=source_remap
    )
    character_remap, ambiguous_characters = _semantic_id_remap(
        contextual_characters, existing_characters, "characters"
    )
    ambiguous_characters.update(
        str(item.get("id") or "")
        for item in imported_characters
        if str(item.get("series_id") or "") in ambiguous_sources
    )
    for character_id in ambiguous_characters:
        character_remap.pop(character_id, None)
    contextual_outfits = _remap_collection_context(
        imported_outfits,
        source_ids=source_remap,
        character_ids=character_remap,
    )
    outfit_remap, ambiguous_outfits = _semantic_id_remap(
        contextual_outfits, existing_outfits, "outfits"
    )
    ambiguous_outfits.update(
        str(item.get("id") or "")
        for item in imported_outfits
        if (
            str(item.get("series_id") or "") in ambiguous_sources
            or any(
                str(value) in ambiguous_characters
                for value in (item.get("character_ids") or ())
            )
        )
    )
    for outfit_id in ambiguous_outfits:
        outfit_remap.pop(outfit_id, None)
    known_tag_ids = _known_mapped_ids(existing_tags, imported_tags, tag_remap)
    known_category_ids = _known_mapped_ids(
        existing_categories, imported_categories, category_remap
    )
    known_source_ids = _known_mapped_ids(
        existing_sources, imported_sources, source_remap
    )
    known_character_ids = _known_mapped_ids(
        existing_characters, contextual_characters, character_remap
    )
    known_outfit_ids = _known_mapped_ids(
        existing_outfits, contextual_outfits, outfit_remap
    )

    defaults = dict(
        RelationshipStore(
            app_root / DEFAULT_RELATIONSHIPS_RELATIVE
        ).relationships
    )
    users = dict(RelationshipStore(app_root / RELATIONSHIPS_RELATIVE).relationships)

    mapped: list[tuple[KnowledgeRelationship, KnowledgeRelationship]] = []
    for raw in raw_relationships:
        original = KnowledgeRelationship.from_dict(raw)
        mapped.append(
            (
                original,
                _remap_relationship(
                    original,
                    tag_ids=tag_remap,
                    category_ids=category_remap,
                    source_ids=source_remap,
                    character_ids=character_remap,
                    outfit_ids=outfit_remap,
                ),
            )
        )
    alias_owners = _tag_identity_owners(
        [*existing_tags, *imported_tags], tag_remap
    )
    mapped_relationships = tuple(item for _, item in mapped)
    for relationship in (
        *defaults.values(),
        *users.values(),
        *mapped_relationships,
    ):
        if relationship.kind != IDENTITY_ALIAS or len(relationship.endpoints) != 2:
            continue
        alias = _normalized(relationship.endpoints[0].id)
        target = relationship.endpoints[1]
        if alias and target.kind == "tag":
            alias_owners.setdefault(alias, set()).add(target.id)

    supported_kinds = (
        PORTABLE_RELATIONSHIP_KINDS
        if schema_version >= 7
        else SCHEMA_5_RELATIONSHIP_KINDS
    )
    local_by_semantic_key: dict[tuple[Any, ...], list[KnowledgeRelationship]] = {}
    for relationship in (*defaults.values(), *users.values()):
        local_by_semantic_key.setdefault(
            _relationship_semantic_key(relationship), []
        ).append(relationship)

    decisions: list[KnowledgePackRelationshipDecision] = []
    imported: list[KnowledgeRelationship] = []
    seen_ids: set[str] = set()
    seen_semantic_keys: set[tuple[Any, ...]] = set()
    for original, relationship in mapped:
        imported.append(relationship)
        reason = ""
        outcome = "add"
        semantic_key = _relationship_semantic_key(relationship)
        if original.kind not in supported_kinds:
            outcome = RELATIONSHIP_HOLD
            reason = (
                f"Relationship type {original.kind!r} is not portable in "
                f"Knowledge Pack schema {schema_version}."
            )
        elif relationship.id in seen_ids:
            outcome = RELATIONSHIP_HOLD
            reason = "Two imported relationship records resolve to the same stable ID."
        elif semantic_key in seen_semantic_keys:
            outcome = RELATIONSHIP_HOLD
            reason = "Two imported relationship records define the same typed endpoints."
        else:
            reason = _relationship_dependency_issue(
                relationship,
                known_tag_ids=known_tag_ids,
                known_category_ids=known_category_ids,
                known_source_ids=known_source_ids,
                known_character_ids=known_character_ids,
                known_outfit_ids=known_outfit_ids,
                ambiguous_tag_ids=ambiguous_tags,
                ambiguous_category_ids=ambiguous_categories,
                ambiguous_source_ids=ambiguous_sources,
                ambiguous_character_ids=ambiguous_characters,
                ambiguous_outfit_ids=ambiguous_outfits,
                alias_owners=alias_owners,
            )
            if reason:
                outcome = RELATIONSHIP_HOLD
        seen_ids.add(relationship.id)
        seen_semantic_keys.add(semantic_key)

        if outcome != RELATIONSHIP_HOLD:
            existing_user = users.get(relationship.id)
            current_default = defaults.get(relationship.id)
            expected_base = base_fingerprints.get(original.id, "")
            if existing_user is not None:
                if _relationship_fingerprint(existing_user) == _relationship_fingerprint(
                    relationship
                ):
                    outcome = "already_present"
                    reason = "The same reviewed user relationship is already present."
                else:
                    outcome = RELATIONSHIP_HOLD
                    reason = "A different local user relationship has this stable ID."
            elif current_default is not None and _relationship_fingerprint(
                current_default
            ) == _relationship_fingerprint(relationship):
                outcome = "already_present"
                reason = "The current shipped default already provides this relationship."
            elif (
                current_default is None
                and existing_user is None
                and (
                    semantic_matches := local_by_semantic_key.get(semantic_key, [])
                )
            ):
                exact_match = next(
                    (
                        item
                        for item in semantic_matches
                        if _relationship_content_fingerprint(item)
                        == _relationship_content_fingerprint(relationship)
                    ),
                    None,
                )
                if exact_match is not None:
                    outcome = "already_present"
                    reason = (
                        "The same reviewed relationship is already present under "
                        "its local stable ID."
                    )
                else:
                    outcome = RELATIONSHIP_HOLD
                    reason = (
                        "A local relationship already defines these typed endpoints "
                        "with different reviewed content."
                    )
            elif expected_base == "missing" and current_default is None:
                if relationship.disabled:
                    outcome = RELATIONSHIP_HOLD
                    reason = "A disabled relationship has no default record to disable."
                else:
                    outcome = "add"
                    reason = "This is a new reviewed user relationship."
            elif not expected_base:
                outcome = RELATIONSHIP_HOLD
                reason = "The pack does not record the default base for this relationship."
            elif current_default is None:
                outcome = RELATIONSHIP_HOLD
                reason = "The default relationship used by this patch is no longer present."
            elif _relationship_fingerprint(current_default) != expected_base:
                outcome = RELATIONSHIP_HOLD
                reason = "The shipped default changed after this patch was created."
            elif relationship.disabled:
                outcome = "disable_default"
                reason = "The pack disables the matching current default relationship."
            else:
                outcome = "override"
                reason = "The pack overrides the matching current default relationship."
        decisions.append(
            KnowledgePackRelationshipDecision(
                relationship.id,
                relationship.kind,
                _relationship_label(relationship),
                outcome,
                reason,
            )
        )

    initial_issues = validate_relationships(imported)
    issue_by_id = {
        issue.relationship_id: issue.message
        for issue in initial_issues
        if issue.relationship_id
    }
    broad_issue = next(
        (issue.message for issue in initial_issues if not issue.relationship_id), ""
    )
    decisions = [
        replace(
            decision,
            outcome=RELATIONSHIP_HOLD,
            reason=issue_by_id.get(decision.stable_id, broad_issue),
        )
        if issue_by_id.get(decision.stable_id) or broad_issue
        else decision
        for decision in decisions
    ]

    active_imports = {
        relationship.id: relationship
        for relationship, decision in zip(imported, decisions)
        if decision.outcome not in {RELATIONSHIP_HOLD, "already_present"}
    }
    effective = dict(defaults)
    effective.update(users)
    effective.update(active_imports)
    effective_issues = validate_relationships(effective.values())
    if effective_issues:
        messages = " ".join(issue.message for issue in effective_issues[:3])
        affected = {
            issue.relationship_id
            for issue in effective_issues
            if issue.relationship_id in active_imports
        }
        decisions = [
            replace(decision, outcome=RELATIONSHIP_HOLD, reason=messages)
            if decision.stable_id in affected or (not affected and decision.stable_id in active_imports)
            else decision
            for decision in decisions
        ]
    return tuple(decisions), tuple(imported), ()


def _relationship_selector_remap(
    app_root: Path,
    work: Path,
    relationships: tuple[KnowledgeRelationship, ...],
    decisions: tuple[KnowledgePackRelationshipDecision, ...],
) -> dict[str, str]:
    """Map exact imported relationship selectors to destination authority."""

    path = work / RELATIONSHIPS_RELATIVE
    if not path.exists():
        return {}
    payload = _read_payload_checked(path)
    raw = [
        item for item in payload.get("relationships", ()) if isinstance(item, dict)
    ]
    if len(raw) != len(relationships) or len(raw) != len(decisions):
        return {}
    local = tuple(
        [
            *RelationshipStore(
                app_root / DEFAULT_RELATIONSHIPS_RELATIVE
            ).relationships.values(),
            *RelationshipStore(
                app_root / RELATIONSHIPS_RELATIVE
            ).relationships.values(),
        ]
    )
    local_by_content: dict[tuple[Any, ...], list[KnowledgeRelationship]] = {}
    for item in local:
        key = (_relationship_semantic_key(item), _relationship_content_fingerprint(item))
        local_by_content.setdefault(key, []).append(item)
    result: dict[str, str] = {}
    for original, relationship, decision in zip(raw, relationships, decisions):
        source_id = str(original.get("id") or "")
        if not source_id:
            continue
        target_id = relationship.id
        if decision.outcome == "already_present":
            key = (_relationship_semantic_key(relationship),
                   _relationship_content_fingerprint(relationship))
            matches = local_by_content.get(key, [])
            if len(matches) == 1:
                target_id = matches[0].id
        result[source_id] = target_id
    return result


def _work_content_payloads(
    work: Path,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, Any]]:
    def payload(relative: str, legacy_relative: str = "") -> dict[str, Any]:
        path = work / relative
        if not path.exists() and legacy_relative:
            path = work / legacy_relative
        return _read_payload_checked(path) if path.exists() else {}

    tags_payload = payload("data/user/tags.json", "data/tags.json")
    record_payloads = {
        key: payload(f"data/user/{filename}", f"data/{filename}")
        for filename, key in (
            ("characters.json", "characters"),
            ("series.json", "series"),
            ("outfits.json", "outfits"),
            ("categories.json", "categories"),
            ("patterns.json", "patterns"),
            ("consumer_policies.json", "policies"),
        )
    }
    relationships = payload(RELATIONSHIPS_RELATIVE)
    return tags_payload, record_payloads, relationships


def _stage_category_base_storage_retirement(
    app_root: Path,
    stage: Path,
    staged_targets: dict[Path, Path],
) -> None:
    """Keep imported canonical Category Bases out of schema-v6 live storage."""

    local_categories = app_root / "data" / "user" / "categories.json"
    staged_categories = staged_targets.get(local_categories)
    source_categories = staged_categories or local_categories
    if not source_categories.exists():
        return
    local_relationships = app_root / RELATIONSHIPS_RELATIVE
    source_relationships = staged_targets.get(
        local_relationships, local_relationships,
    )
    effective = dict(
        RelationshipStore(
            app_root / DEFAULT_RELATIONSHIPS_RELATIVE
        ).relationships
    )
    if source_relationships.exists():
        effective.update(RelationshipStore(source_relationships).relationships)
    category_base_index = category_base_relationship_index(effective.values())
    payload = _read_payload_checked(source_categories)
    original_payload = json.loads(json.dumps(payload))
    payload["schema_version"] = max(
        CategoryRegistry.SCHEMA_VERSION,
        int(payload.get("schema_version", 1) or 1),
    )
    for item in payload.get("categories", ()):
        if (
            isinstance(item, dict)
            and category_base_index.get(str(item.get("id") or ""))
        ):
            item.pop("base_tag_id", None)
            item.pop("base_tag_policy", None)
    if payload == original_payload:
        return
    if staged_categories is None:
        staged_categories = stage / local_categories.relative_to(app_root)
        staged_categories.parent.mkdir(parents=True, exist_ok=True)
        staged_targets[local_categories] = staged_categories
    staged_categories.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _stage_tag_relationship_storage_retirement(
    app_root: Path,
    stage: Path,
    staged_targets: dict[Path, Path],
) -> None:
    """Keep represented canonical links out of schema-v10 Tag storage."""

    local_tags = app_root / "data" / "user" / "tags.json"
    staged_tags = staged_targets.get(local_tags)
    source_tags = staged_tags or local_tags
    if not source_tags.exists():
        return
    local_relationships = app_root / RELATIONSHIPS_RELATIVE
    source_relationships = staged_targets.get(
        local_relationships, local_relationships,
    )
    effective = dict(
        RelationshipStore(
            app_root / DEFAULT_RELATIONSHIPS_RELATIVE
        ).relationships
    )
    if source_relationships.exists():
        effective.update(RelationshipStore(source_relationships).relationships)
    tags = TagDatabase(source_tags)
    local_categories = app_root / "data" / "user" / "categories.json"
    source_categories = staged_targets.get(local_categories, local_categories)
    default_categories = app_root / "data" / "defaults" / "categories.json"
    if source_categories != local_categories:
        # A staged projection has its own root. Keep the strict enrolled-store
        # admission intact by using a private copy of the admitted defaults.
        staged_defaults = stage / default_categories.relative_to(app_root)
        if default_categories.exists():
            staged_defaults.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(default_categories, staged_defaults)
        default_categories = staged_defaults
    categories = CategoryRegistry(
        default_categories,
        source_categories,
    )
    categories.configure_category_base_authority(effective.values())
    foundation = FoundationKnowledge(
        app_root / "data" / "defaults" / "modifiers.json",
        app_root / "data" / "defaults" / "concepts.json",
    )
    publish_tag_semantic_authority(tags, effective.values())
    publish_tag_alias_storage_authority(tags,effective.values())
    publish_modifier_composition_storage_authority(
        tags,
        effective.values(),
        categories=categories,
        foundation=foundation,
    )
    publish_allowed_dimension_storage_authority(tags, effective.values())
    publish_scoped_constraint_storage_authority(tags, effective.values())
    storage_by_id = {
        str(item.get("id") or ""): item
        for item in tags._payload().get("tags", ())
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    payload = _read_payload_checked(source_tags)
    original_payload = json.loads(json.dumps(payload))
    payload["schema_version"] = max(
        TagDatabase.SCHEMA_VERSION,
        int(payload.get("schema_version", 1) or 1),
    )
    for item in payload.get("tags", ()):
        if not isinstance(item, dict):
            continue
        stored = storage_by_id.get(str(item.get("id") or ""))
        if stored is not None and isinstance(stored.get("relations"), dict):
            item["relations"] = json.loads(json.dumps(stored["relations"]))
    if payload == original_payload:
        return
    if staged_tags is None:
        staged_tags = stage / local_tags.relative_to(app_root)
        staged_tags.parent.mkdir(parents=True, exist_ok=True)
        staged_targets[local_tags] = staged_tags
    staged_tags.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _stage_character_relationship_storage_retirement(
    app_root: Path,
    stage: Path,
    staged_targets: dict[Path, Path],
) -> None:
    """Keep imported canonical cross-entity links out of live record storage."""

    local_relationships = app_root / RELATIONSHIPS_RELATIVE
    source_relationships = staged_targets.get(
        local_relationships, local_relationships,
    )
    default_relationships = tuple(
        RelationshipStore(
            app_root / DEFAULT_RELATIONSHIPS_RELATIVE
        ).relationships.values()
    )
    user_relationships = (
        tuple(RelationshipStore(source_relationships).relationships.values())
        if source_relationships.exists() else ()
    )
    specs = (
        (
            "series", "series.json", "series",
            SERIES_STORAGE_SCHEMA_VERSION,
        ),
        (
            "character", "characters.json", "characters",
            CHARACTER_STORAGE_SCHEMA_VERSION,
        ),
        (
            "outfit", "outfits.json", "outfits",
            OUTFIT_STORAGE_SCHEMA_VERSION,
        ),
    )
    for kind, filename, key, schema_version in specs:
        local = app_root / "data" / "user" / filename
        staged = staged_targets.get(local)
        source = staged or local
        if not source.exists():
            continue
        payload = _read_payload_checked(source)
        original = json.loads(json.dumps(payload))
        payload["schema_version"] = max(
            schema_version, int(payload.get("schema_version", 1) or 1),
        )
        values = payload.get(key, ())
        if not isinstance(values, list):
            raise RuntimeError(f"{filename} does not contain a valid {key} collection.")
        record_values = [item for item in values if isinstance(item, dict)]
        normalized_records = iter(character_record_storage_payloads(
            kind,
            record_values,
            default_relationships,
            user_relationships,
        ))
        normalized = [
            next(normalized_records) if isinstance(item, dict) else item
            for item in values
        ]
        payload[key] = normalized
        if payload == original:
            continue
        if staged is None:
            staged = stage / local.relative_to(app_root)
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged_targets[local] = staged
        staged.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def plan_knowledge_pack_import(
    app_root: Path, pack_path: Path, *, approved_default_release: ApprovedDefaultPackRelease | None = None,
    foundation_proposal: FoundationProposal | None = None,
) -> KnowledgePackImportPlan:
    """Build a preview admitted by persisted identity and operation history."""
    _check_export_path(Path(pack_path))
    input_fingerprint = _file_fingerprint(Path(pack_path))
    wire = _maybe_foundation_pack(pack_path)
    wire_fingerprint = input_fingerprint if wire is not None else None
    if wire is not None:
        if (type(approved_default_release) is not ApprovedDefaultPackRelease
                or not _same_foundation_release_approval(approved_default_release, wire.approved_release)):
            raise ValueError("Foundation wire release requires the exact explicitly approved descriptor")
        if foundation_proposal is not None and foundation_proposal != wire.proposal:
            raise ValueError("Foundation wire input differs from the supplied source proposal")
        foundation_proposal = wire.proposal
    with catalog_admission(app_root, sys.modules[__name__], role="plan") as admission:
        if foundation_proposal is not None:
            if not admission.enrolled or approved_default_release is None:
                raise ValueError("Foundation review requires enrolled identity and approved release tracking")
            plan = _plan_foundation_import(Path(app_root), Path(pack_path), foundation_proposal, wire=wire)
            if wire is not None and plan.pack_fingerprint != wire_fingerprint:
                raise RuntimeError("Foundation archive changed during planning")
        else:
            plan = _plan_knowledge_pack_import_admitted(app_root, pack_path)
        if approved_default_release is not None:
            if type(approved_default_release) is not ApprovedDefaultPackRelease:
                raise TypeError("Supply an explicitly approved immutable default Pack release")
            state_path = Path(app_root) / "data/user/default_knowledge_state.json"
            state = _read_payload_checked(state_path) if state_path.exists() else {}
            repeat_foundation = foundation_proposal is not None and _foundation_release_matches(state, approved_default_release)
            if repeat_foundation:
                _require_installed_foundation_source(Path(app_root), approved_default_release, foundation_proposal)
            prior = release_records(state).get(approved_default_release.pack_id) if repeat_foundation else require_previous(state, approved_default_release)
            if prior is not None:
                observe_approved_default_pack_release(app_root, approved_default_release.pack_id)
            if foundation_proposal is None and (plan.schema_version < 8 or plan.imported_relationships or plan.removal_decisions
                    or any(p.kind not in {"tag", "category"} for p in plan.record_patches)
                    or any(d.kind == "category" and d.outcome != "already_present" for d in plan.record_decisions)
                    or any(p.kind == "tag" and (p.source_id != p.stable_id or p.local_id != p.stable_id)
                           for p in plan.record_patches)):
                raise ValueError("Approved default Tag state requires a bounded Tag Pack")
            plan = replace(plan, approved_default_release_json=approved_default_release.encode())
        if admission.enrolled:
            plan = replace(plan, catalog_identity_binding=tuple(sorted(admission.binding.items())),
                           catalog_receipt_epoch=admission.receipt_epoch)
        result = plan
    return review_foundation_import_plan(app_root, result, capture_effective=False) if foundation_proposal is not None else result


def _plan_knowledge_pack_import_admitted(
    app_root: Path, pack_path: Path
) -> KnowledgePackImportPlan:
    require_catalog_admission(app_root, sys.modules[__name__])
    app_root = Path(app_root)
    pack_path = Path(pack_path)
    pack_fingerprint = _file_fingerprint(pack_path)
    if pack_fingerprint == "missing":
        raise FileNotFoundError(pack_path)
    with tempfile.TemporaryDirectory() as temp:
        work = Path(temp)
        with zipfile.ZipFile(pack_path, "r") as archive:
            _extract_pack_safely(archive, work)
        manifest = _load_manifest(work)
        schema_version = int(manifest.get("schema_version", 1) or 1)
        boundary = str(manifest.get("content_rating_boundary") or "")
        issues: list[str] = []
        if (
            schema_version < 9
            and (work / "data" / "user" / "consumer_policies.json").exists()
        ):
            issues.append(
                "Consumer-policy patches require Knowledge Pack schema 9."
            )
        content_findings: tuple[KnowledgePackContentFinding, ...] = ()
        if schema_version >= 5 and boundary not in CONTENT_RATING_BOUNDARIES:
            issues.append(
                "This Knowledge Pack does not declare a supported content-rating boundary."
            )
        elif schema_version >= 5:
            try:
                tags_payload, record_payloads, relationships_payload = (
                    _work_content_payloads(work)
                )
                verified_boundary, content_findings = _content_review(
                    app_root,
                    tags_payload=tags_payload,
                    record_payloads=record_payloads,
                    relationships_payload=relationships_payload,
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                verified_boundary = "explicit"
                if boundary != "explicit":
                    issues.append(
                        "The lower content-rating declaration could not be verified: "
                        + str(error)
                    )
            if (
                boundary in CONTENT_RATING_ORDER
                and CONTENT_RATING_ORDER[boundary]
                < CONTENT_RATING_ORDER[verified_boundary]
            ):
                strongest = next(
                    (
                        item for item in reversed(content_findings)
                        if item.rating == verified_boundary
                    ),
                    None,
                )
                detail = (
                    f" {strongest.label}: {strongest.basis}." if strongest else ""
                )
                issues.append(
                    f"The declared {boundary.title()} content-rating boundary understates "
                    f"the reviewed pack contents; at least {verified_boundary.title()} is required."
                    + detail
                )
        if schema_version < 5:
            boundary = "legacy_unrated"
        decisions, imported, relationship_issues = _relationship_import_plan(
            app_root, work, manifest
        )
        issues.extend(relationship_issues)
        active_imported_relationships = tuple(
            relationship
            for relationship, decision in zip(imported, decisions)
            if decision.outcome not in {RELATIONSHIP_HOLD, "already_present"}
        )
        relationship_selector_ids = _relationship_selector_remap(
            app_root, work, imported, decisions
        )
        record_decisions, record_patches, record_issues = _record_import_plan(
            app_root,
            work,
            manifest,
            additional_user_relationships=active_imported_relationships,
            relationship_ids=relationship_selector_ids,
        )
        issues.extend(record_issues)
        consumer_policy_extras=()
        imported_policy_path=work/"data"/"user"/"consumer_policies.json"
        if schema_version>=9 and imported_policy_path.exists():
            imported_policy_payload=_read_payload_checked(imported_policy_path)
            consumer_policy_extras=tuple(
                (str(key),json.loads(json.dumps(value)))
                for key,value in imported_policy_payload.items()
                if key not in {
                    "schema_version","policy_version","layer","policies",
                    "base_fingerprints","lifecycle_owned_ids",
                }
            )
        removal_decisions=()
        imported_removal_archives=()
        removal_tag_remap=()
        removal_category_remap=()
        if schema_version>=6:
            imported_tags_path=work/"data"/"user"/"tags.json"
            if not imported_tags_path.exists():
                issues.append("Knowledge Pack schema 6 requires a user Tag patch.")
            else:
                tags_payload=json.loads(imported_tags_path.read_text(encoding="utf-8"))
                if not isinstance(tags_payload,dict):
                    raise ValueError("The Knowledge Pack Tag patch must contain a JSON object.")
                imported_removal_archives=parse_portable_removed_archives(tags_payload)
                incoming_tags=_read_records(imported_tags_path,"tags")
                incoming_categories=_read_records(
                    work/"data"/"user"/"categories.json","categories"
                )
                tag_remap,ambiguous_tags=_semantic_id_remap(
                    incoming_tags,_existing_tag_records(app_root),"tags"
                )
                category_remap,ambiguous_categories=_semantic_id_remap(
                    incoming_categories,_existing_category_records(app_root),"categories"
                )
                preparation=prepare_removed_archive_import(
                    app_root,imported_removal_archives,
                    incoming_tags=tuple(incoming_tags),
                    incoming_categories=tuple(incoming_categories),
                    tag_id_remap=tag_remap,category_id_remap=category_remap,
                    ambiguous_tag_ids=ambiguous_tags,
                    ambiguous_category_ids=ambiguous_categories,
                )
                removal_decisions=preparation.decisions
                removal_tag_remap=tuple(sorted(tag_remap.items()))
                removal_category_remap=tuple(sorted(category_remap.items()))
        summary = _pack_summary_from_work(work)
    authority_fingerprints = _authority_fingerprints(app_root)
    return KnowledgePackImportPlan(
        str(pack_path.resolve()),
        pack_fingerprint,
        _paths_fingerprint(Path(path) for path, _fingerprint in authority_fingerprints),
        schema_version,
        boundary,
        tuple(summary.items()),
        decisions,
        record_decisions=record_decisions,
        removal_decisions=removal_decisions,
        record_patches=record_patches,
        imported_relationships=imported,
        imported_removal_archives=imported_removal_archives,
        removal_tag_remap=removal_tag_remap,
        removal_category_remap=removal_category_remap,
        authority_fingerprints=authority_fingerprints,
        issues=tuple(issues),
        content_findings=content_findings,
        consumer_policy_extras=consumer_policy_extras,
    )


def _merge_records(local_path: Path, imported_path: Path, key: str, id_key: str = "id") -> dict[str, int]:
    local_payload = json.loads(local_path.read_text(encoding="utf-8")) if local_path.exists() else {"schema_version": 1, key: []}
    imported_payload = json.loads(imported_path.read_text(encoding="utf-8"))
    local_items = [item for item in local_payload.get(key, []) if isinstance(item, dict)]
    imported_items = [item for item in imported_payload.get(key, []) if isinstance(item, dict)]
    by_id = {str(item.get(id_key) or ""): item for item in local_items}
    by_identity: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in local_items:
        for identity in _record_identities(item, key):
            by_identity.setdefault(identity, item)
    added = conflicts = same = 0
    for item in imported_items:
        item_id = str(item.get(id_key) or "")
        if not item_id:
            continue
        existing = by_id.get(item_id)
        if existing is None:
            existing = next(
                (by_identity[identity] for identity in _record_identities(item, key) if identity in by_identity),
                None,
            )
        if existing is None:
            local_items.append(item)
            by_id[item_id] = item
            for identity in _record_identities(item, key):
                by_identity.setdefault(identity, item)
            added += 1
        elif existing == item:
            same += 1
        else:
            # Preserve local data. Record imported alternative for later review.
            alternatives = existing.setdefault("import_alternatives", [])
            if item not in alternatives:
                alternatives.append(item)
            conflicts += 1
    local_payload[key] = local_items
    disabled_added=0; removed_tags_added=0
    if key=="categories":
        local_disabled={str(value) for value in local_payload.get("disabled_default_ids",[]) if str(value).strip()}
        imported_disabled={str(value) for value in imported_payload.get("disabled_default_ids",[]) if str(value).strip()}
        disabled_added=len(imported_disabled-local_disabled)
        local_payload["disabled_default_ids"]=sorted(local_disabled|imported_disabled)
    if key=="tags":
        local_removed={str(value) for value in local_payload.get("removed_tag_ids",[]) if str(value).strip()}
        imported_removed={str(value) for value in imported_payload.get("removed_tag_ids",[]) if str(value).strip()}
        removed_tags_added=len(imported_removed-local_removed)
        local_payload["removed_tag_ids"]=sorted(local_removed|imported_removed)
        local_redirects={
            str(source):str(target)
            for source,target in dict(local_payload.get("merged_tag_ids") or {}).items()
            if str(source).strip() and str(target).strip()
        }
        imported_redirects={
            str(source):str(target)
            for source,target in dict(imported_payload.get("merged_tag_ids") or {}).items()
            if str(source).strip() and str(target).strip()
        }
        redirect_conflicts={
            source for source,target in imported_redirects.items()
            if (
                source in by_id
                or (source in local_redirects and local_redirects[source]!=target)
            )
        }
        for source,target in imported_redirects.items():
            if source not in redirect_conflicts:
                local_redirects.setdefault(source,target)
        live_ids=set(by_id)
        for source in imported_redirects:
            if source in redirect_conflicts:
                continue
            current=source; seen=set()
            while current not in live_ids and current not in seen:
                seen.add(current)
                current=str(local_redirects.get(current) or "")
                if not current:
                    break
            if current not in live_ids:
                redirect_conflicts.add(source)
                if source not in dict(local_payload.get("merged_tag_ids") or {}):
                    local_redirects.pop(source,None)
        local_payload["merged_tag_ids"]=dict(sorted(local_redirects.items()))
    local_path.parent.mkdir(parents=True, exist_ok=True)
    temporary=local_path.with_suffix(local_path.suffix+".tmp")
    temporary.write_text(json.dumps(local_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(local_path)
    result={"added": added, "conflicts": conflicts, "already_present": same}
    if key=="categories":result["disabled_defaults_added"]=disabled_added
    if key=="tags":result["removed_tags_added"]=removed_tags_added
    if key=="tags":result["merged_tag_redirect_conflicts"]=len(redirect_conflicts)
    return result


def _stage_record_import(
    app_root: Path,
    stage: Path,
    plan: KnowledgePackImportPlan,
) -> tuple[dict[str, dict[str, int]], dict[Path, Path]]:
    """Stage every schema-v8 record decision without weakening local authority."""
    if len(plan.record_patches) != len(plan.record_decisions):
        raise RuntimeError("The Knowledge Pack record preview is incomplete.")

    payloads: dict[str, dict[str, Any]] = {}
    staged_paths: dict[str, Path] = {}
    target_paths: dict[Path, Path] = {}
    results: dict[str, dict[str, int]] = {}
    record_tables: dict[tuple[str, str], tuple[list[dict[str, Any]], dict[str, int]]] = {}
    disabled_tables: dict[tuple[str, str], set[str]] = {}
    for patch, decision in zip(plan.record_patches, plan.record_decisions):
        if patch.kind != decision.kind or patch.stable_id != decision.stable_id:
            raise RuntimeError("The Knowledge Pack record preview changed while staging.")
        if decision.outcome == RELATIONSHIP_HOLD:
            raise ValueError(
                "Record holds must be resolved before applying the Knowledge Pack."
            )
        counts = results.setdefault(
            RECORD_STATE_KEY.get(patch.kind, patch.key),
            {
                "added": 0,
                "overridden": 0,
                "disabled_defaults": 0,
                "already_present": 0,
                "conflicts": 0,
            },
        )
        if decision.outcome == "already_present":
            counts["already_present"] += 1
            continue

        if patch.filename not in payloads:
            target = app_root / "data" / "user" / patch.filename
            staged = stage / "data" / "user" / patch.filename
            payload = _read_payload_checked(target)
            payload.setdefault("schema_version", 1)
            if patch.kind == "consumer_policy":
                payload.setdefault("policy_version", 1)
                payload.setdefault("layer", "user")
                for key,value in plan.consumer_policy_extras:
                    payload.setdefault(key,json.loads(json.dumps(value)))
            payloads[patch.filename] = payload
            staged_paths[patch.filename] = staged
            target_paths[target] = staged
        payload = payloads[patch.filename]
        table_key = (patch.filename, patch.key)
        if table_key not in record_tables:
            records = [
                json.loads(json.dumps(item))
                for item in payload.get(patch.key, ())
                if isinstance(item, dict)
            ]
            by_id = {
                str(item.get("id") or ""): index
                for index, item in enumerate(records)
                if str(item.get("id") or "")
            }
            record_tables[table_key] = (records, by_id)
        records, by_id = record_tables[table_key]
        disabled_key = patch.disabled_key or RECORD_DISABLED_KEY.get(patch.kind, "")
        disabled_table_key = (patch.filename, disabled_key)
        if disabled_table_key not in disabled_tables:
            disabled_tables[disabled_table_key] = {
                str(value) for value in payload.get(disabled_key, ())
                if disabled_key and str(value).strip()
            }
        disabled = disabled_tables[disabled_table_key]

        if decision.outcome in {"add", "override"}:
            if patch.payload is None:
                raise RuntimeError("A Knowledge Pack record patch is missing its payload.")
            record = json.loads(json.dumps(patch.payload))
            record["id"] = patch.local_id
            index = by_id.get(patch.local_id)
            if index is None:
                by_id[patch.local_id] = len(records)
                records.append(record)
            else:
                records[index] = record
            if disabled_key:
                disabled.discard(patch.local_id)
            counts["added" if decision.outcome == "add" else "overridden"] += 1
        elif decision.outcome == "disable_default":
            if not disabled_key:
                raise RuntimeError(
                    "This Knowledge Pack record type cannot disable shipped defaults."
                )
            disabled.add(patch.local_id)
            records = [
                item for item in records
                if str(item.get("id") or "") != patch.local_id
            ]
            by_id = {
                str(item.get("id") or ""): index
                for index, item in enumerate(records)
                if str(item.get("id") or "")
            }
            record_tables[table_key] = (records, by_id)
            counts["disabled_defaults"] += 1
        else:
            raise RuntimeError(
                f"Unsupported Knowledge Pack record outcome {decision.outcome!r}."
            )
        payload[patch.key] = records

    for filename, payload in payloads.items():
        for (disabled_filename, disabled_key), disabled in disabled_tables.items():
            if disabled_filename == filename and disabled_key:
                payload[disabled_key] = sorted(disabled)
        staged = staged_paths[filename]
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        checked = _read_payload_checked(staged)
        if checked != payload:
            raise RuntimeError(
                f"Knowledge Pack staging verification failed for {filename}."
            )
        if filename == "consumer_policies.json":
            policy_store = ConsumerPolicyStore(staged, expected_layer="user")
            policy_errors = [
                error
                for record in policy_store.records
                for error in record.validation_errors()
            ]
            if not policy_store.runtime_supported or policy_errors:
                raise RuntimeError(
                    "Knowledge Pack staging produced unsupported consumer-policy authority."
                )
    return results, target_paths


def _stage_tag_lifecycle_compatibility(
    staged_path: Path,
    imported_path: Path,
    plan: KnowledgePackImportPlan,
) -> dict[str, int]:
    """Retain schema-v4 Tag tombstone/redirect compatibility beside v8 records."""
    payload = _read_payload_checked(staged_path)
    imported = _read_payload_checked(imported_path)
    tag_remap = {
        patch.source_id: patch.local_id
        for patch in plan.record_patches
        if patch.kind == "tag" and patch.source_id
    }
    local_removed = {
        str(value) for value in payload.get("removed_tag_ids", ())
        if str(value).strip()
    }
    imported_removed = {
        tag_remap.get(str(value), str(value))
        for value in (
            *imported.get("removed_tag_ids", ()),
            *imported.get("legacy_removed_tag_ids", ()),
        )
        if str(value).strip()
    }
    payload["removed_tag_ids"] = sorted(local_removed | imported_removed)

    local_redirects = {
        str(source): str(target)
        for source, target in dict(payload.get("merged_tag_ids") or {}).items()
        if str(source).strip() and str(target).strip()
    }
    imported_redirects = {
        tag_remap.get(str(source), str(source)): tag_remap.get(
            str(target), str(target)
        )
        for source, target in dict(imported.get("merged_tag_ids") or {}).items()
        if str(source).strip() and str(target).strip()
    }
    live_ids = {
        str(item.get("id") or "") for item in payload.get("tags", ())
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    conflicts = {
        source for source, target in imported_redirects.items()
        if source in live_ids or (
            source in local_redirects and local_redirects[source] != target
        )
    }
    for source, target in imported_redirects.items():
        if source not in conflicts:
            local_redirects.setdefault(source, target)
    for source in imported_redirects:
        if source in conflicts:
            continue
        current = source
        seen: set[str] = set()
        while current not in live_ids and current not in seen:
            seen.add(current)
            current = str(local_redirects.get(current) or "")
            if not current:
                break
        if current not in live_ids:
            conflicts.add(source)
            if source not in dict(payload.get("merged_tag_ids") or {}):
                local_redirects.pop(source, None)
    payload["merged_tag_ids"] = dict(sorted(local_redirects.items()))
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return {
        "removed_tags_added": len(imported_removed - local_removed),
        "merged_tag_redirect_conflicts": len(conflicts),
    }


def _stage_relationship_import(
    app_root: Path,
    staged_path: Path,
    plan: KnowledgePackImportPlan,
) -> dict[str, int]:
    local_path = app_root / RELATIONSHIPS_RELATIVE
    local_store = RelationshipStore(local_path)
    payload = local_store.payload()
    by_id = dict(local_store.relationships)
    counts = {
        "added": 0,
        "overridden": 0,
        "disabled_defaults": 0,
        "already_present": 0,
        "conflicts": 0,
    }
    for relationship, decision in zip(
        plan.imported_relationships, plan.relationship_decisions
    ):
        if decision.outcome == "already_present":
            counts["already_present"] += 1
            continue
        if decision.outcome == RELATIONSHIP_HOLD:
            raise ValueError(
                "Relationship holds must be resolved before applying the Knowledge Pack."
            )
        by_id[relationship.id] = relationship
        if decision.outcome == "add":
            counts["added"] += 1
        elif decision.outcome == "override":
            counts["overridden"] += 1
        elif decision.outcome == "disable_default":
            counts["disabled_defaults"] += 1

    user_relationships = tuple(by_id.values())
    issues = validate_relationships(user_relationships)
    if issues:
        raise ValueError(" ".join(issue.message for issue in issues[:3]))
    defaults = dict(
        RelationshipStore(
            app_root / DEFAULT_RELATIONSHIPS_RELATIVE
        ).relationships
    )
    defaults.update(by_id)
    issues = validate_relationships(defaults.values())
    if issues:
        raise ValueError(" ".join(issue.message for issue in issues[:3]))
    payload["schema_version"] = max(1, int(payload.get("schema_version", 1) or 1))
    payload["relationships"] = [
        relationship.to_dict()
        for relationship in sorted(user_relationships, key=lambda item: item.id)
    ]
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return counts


def _restore_pack_transaction(
    manifest_path: Path, manifest: dict[str, Any], app_root: Path,
    *, preflight: tuple | None = None,
) -> bool:
    admission = require_catalog_admission(app_root, sys.modules[__name__], ordinary=False, receipt=manifest_path)
    if admission is not None and hasattr(admission, "validate_restore"):
        preflight = admission.validate_restore(manifest, preflight)
    # Durable status wins over an in-memory "complete" set before receipt I/O.
    if preflight is None:
        durable, receipt_state = _read_pack_receipt(manifest_path, app_root)
        if durable["status"] in {"complete", "restored"}:
            return False
        prepared = _prepare_pack_restore(manifest_path, durable, app_root)
    else:
        durable, receipt_state, prepared = preflight
        if _export_path_state(manifest_path) != receipt_state:
            raise RuntimeError("Knowledge Pack recovery receipt changed after batch preflight. Recovery is paused.")
    for target, before, after, payload, current in prepared:
        checkpoint_catalog_admission()
        if _export_path_state(manifest_path) != receipt_state:
            raise RuntimeError("Knowledge Pack recovery receipt changed. Recovery is paused.")
        if _export_path_state(target) != current:
            raise RuntimeError("Knowledge changed during recovery. Recovery is paused; nothing newer will be overwritten.")
        digest = current[-1] if current is not None else "missing"
        if digest == before:
            continue  # Already restored, or import never reached this participant.
        if payload is None:
            target.unlink()
        else:
            _atomic_bytes(target, payload)
        restored = _export_path_state(target)
        if (restored[-1] if restored is not None else "missing") != before:
            raise RuntimeError("Knowledge Pack recovery read-back failed. Recovery remains pending.")
    # Every participant must be back at its exact before state before finalizing.
    for target, before, _after, _payload, _current in prepared:
        state = _export_path_state(target)
        if (state[-1] if state is not None else "missing") != before:
            raise RuntimeError("Knowledge changed before recovery completed. Recovery remains pending.")
    if _export_path_state(manifest_path) != receipt_state:
        raise RuntimeError("Knowledge Pack recovery receipt changed. Recovery remains pending.")
    if "foundation_transition" in durable:
        _require_foundation_authority(app_root, durable, phase="before")
    checkpoint_catalog_admission()
    finalized = dict(durable, status="restored", restored_at=_utc_now())
    try:
        _atomic_json(manifest_path, finalized)
    except Exception:
        saved, _state = _read_pack_receipt(manifest_path, app_root)
        if saved != finalized:
            raise
    saved, _state = _read_pack_receipt(manifest_path, app_root)
    if saved != finalized:
        raise RuntimeError("Knowledge Pack recovery final receipt read-back failed; retain selected evidence.")
    return True


def _recovery_bytes(path: Path) -> tuple[bytes, tuple]:
    state = _export_path_state(path)
    if state is None or state[2] > MAX_PACK_UNCOMPRESSED_BYTES:
        raise RuntimeError("Knowledge Pack recovery file is missing or exceeds the safe read limit.")
    with path.open("rb") as handle:
        payload = handle.read(MAX_PACK_UNCOMPRESSED_BYTES + 1)
    if len(payload) > MAX_PACK_UNCOMPRESSED_BYTES or sha256(payload).hexdigest() != state[-1] or _export_path_state(path) != state:
        raise RuntimeError("Knowledge Pack recovery file changed while being checked.")
    return payload, state


def _read_pack_receipt(path: Path, app_root: Path) -> tuple[dict, tuple]:
    root = Path(os.path.abspath(app_root / "backups" / "knowledge-pack-import"))
    path = Path(os.path.abspath(path))
    if path.name != "recovery.json" or path.parent.parent != root:
        raise RuntimeError("Knowledge Pack recovery receipt is outside its managed folder.")
    payload, state = _recovery_bytes(path)
    manifest = json.loads(payload)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("status"), str) or manifest["status"] not in {"pending", "complete", "restored"}:
        raise RuntimeError("Knowledge Pack recovery receipt is malformed; manual review is required.")
    return manifest, state


def _prepare_pack_restore(manifest_path: Path, manifest: dict, app_root: Path) -> list[tuple]:
    admission = require_catalog_admission(app_root, sys.modules[__name__], ordinary=False)
    if admission is not None and admission.enrolled:
        if hasattr(admission, "admit_manifest"):
            manifest = admission.admit_manifest(manifest_path, manifest)
        else:
            if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != ENROLLED_PACK_RECEIPT:
                raise RuntimeError("Enrolled recovery requires a supported identity-bound receipt.")
            from suite_core.catalog_identity_candidate import require_inverse_catalog
            require_inverse_catalog(_recovery_bytes(Path(app_root).absolute()/"data/user/catalog_identity.json")[0],
                                    manifest.get("catalog_identity_binding"))
            manifest = dict(manifest, schema_version=2)
    return _prepare_pack_restore_verified(manifest_path, manifest, app_root)


def _prepare_pack_restore_verified(manifest_path: Path, manifest: dict, app_root: Path) -> list[tuple]:
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 2:
        raise RuntimeError("This Knowledge Pack recovery receipt has no supported verified before state. Manual review is required.")
    files = manifest.get("files")
    if not isinstance(files, list) or not 0 < len(files) <= MAX_PACK_FILES:
        raise RuntimeError("Knowledge Pack recovery contains an invalid participant list.")
    allowed = {Path(os.path.abspath(path)) for path in _pack_target_paths(app_root)}
    if "foundation_transition" in manifest:
        # Receipt mode alone never grants a legacy or inverse restore default writes.
        if manifest.get("identity_bound_operation") != "pack_import" or not manifest.get("catalog_identity_binding"):
            raise RuntimeError("Foundation restore requires enrolled import authority")
        allowed.update(_require_foundation_authority(app_root, manifest))
    seen = set(); prepared = []
    def valid_hash(value):
        return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)
    for item in files:
        if not isinstance(item, dict) or type(item.get("existed")) is not bool:
            raise RuntimeError("Knowledge Pack recovery contains an invalid file entry.")
        raw_target = item.get("target")
        if not isinstance(raw_target, str) or not Path(raw_target).is_absolute():
            raise RuntimeError("Knowledge Pack recovery target is unsafe.")
        target = Path(os.path.abspath(raw_target))
        if target not in allowed or target in seen:
            raise RuntimeError("Knowledge Pack recovery contains an unsafe or duplicate target.")
        seen.add(target)
        before, after = item.get("expected_before"), item.get("expected_after")
        if not valid_hash(after) or (not valid_hash(before) if item["existed"] else before != "missing"):
            raise RuntimeError("Knowledge Pack recovery has invalid before/after evidence.")
        backup_name = item.get("backup")
        if (not isinstance(backup_name, str) or not backup_name
            or backup_name in {".", "..", "recovery.json"}
            or Path(backup_name).name != backup_name):
            raise RuntimeError("Knowledge Pack recovery backup path is unsafe.")
        backup = manifest_path.parent / backup_name
        payload = None
        if item["existed"]:
            payload, _state = _recovery_bytes(backup)
            if sha256(payload).hexdigest() != before:
                raise RuntimeError("Knowledge Pack recovery backup does not match its verified before state.")
        elif backup.exists():
            raise RuntimeError("Knowledge Pack recovery contains an unexpected backup for an absent file.")
        current = _export_path_state(target)
        digest = current[-1] if current is not None else "missing"
        if digest not in {before, after}:
            raise RuntimeError("Knowledge changed since the interrupted import. Recovery is paused; newer changes and backups were preserved.")
        prepared.append((target, before, after, payload, current))
    return prepared


def _pending_pack_receipts(app_root: Path) -> list[tuple]:
    backup_root = app_root / "backups" / "knowledge-pack-import"
    _check_export_path(backup_root)
    if not backup_root.exists():
        return []
    if not backup_root.is_dir():
        raise RuntimeError("Knowledge Pack recovery folder is unavailable. Manual review is required.")
    pending = []
    targets = set()
    for path in sorted(backup_root.glob("*/recovery.json")):
        manifest, receipt_state = _read_pack_receipt(path, app_root)
        if manifest["status"] != "pending":
            continue
        prepared = _prepare_pack_restore(path, manifest, app_root)
        current_targets = {item[0] for item in prepared}
        if targets & current_targets:
            raise RuntimeError("Overlapping Knowledge Pack recoveries require manual review. No recovery was applied.")
        targets.update(current_targets)
        pending.append((path, manifest, receipt_state, prepared))
    return pending


class KnowledgePackRecoveryHold(RuntimeError):
    """A failed verified restore; transport only diagnostics, never file contents."""

    reason_code = "knowledge-pack-recovery-held"

    def __init__(self, message: str, recovery_location: Path):
        super().__init__(message)
        self.recovery_location = Path(recovery_location)


def recover_interrupted_knowledge_pack_imports(
    app_root: Path,
) -> tuple[Path, ...]:
    """Restore any pack transaction that stopped before its verified commit."""
    with RulePairAdmission(Path(app_root).absolute()/"data"/"user").admit():
        try:
            with catalog_admission(app_root, sys.modules[__name__], role="recovery"):
                return _recover_interrupted_knowledge_pack_imports_admitted(app_root)
        except (RuntimeError, OSError, ValueError) as exc:
            raise KnowledgePackRecoveryHold(
                str(exc), Path(app_root).absolute()/"backups"/"knowledge-pack-import",
            ) from exc


def _recover_interrupted_knowledge_pack_imports_admitted(app_root: Path) -> tuple[Path, ...]:
    app_root = Path(app_root)
    restored: list[Path] = []
    # Preflight ALL pending receipts before changing ANY participant.
    for manifest_path, manifest, receipt_state, prepared in _pending_pack_receipts(app_root):
        if _restore_pack_transaction(manifest_path, manifest, app_root,
                                     preflight=(manifest, receipt_state, prepared)):
            restored.append(manifest_path)
    return tuple(restored)


def import_knowledge_pack(
    app_root: Path,
    pack_path: Path,
    *,
    plan: KnowledgePackImportPlan | None = None,
) -> dict[str, dict[str, int]]:
    # Private stage reads cannot run inside original-catalog admission. Recheck
    # supplied Foundation plans too; direct native callers retain this safety gate.
    plan = plan or plan_knowledge_pack_import(app_root, pack_path)
    if plan.schema_version == FOUNDATION_PACK_SCHEMA_VERSION and plan.foundation_proposal is None:
        raise ValueError("Foundation wire apply requires the typed reviewed source proposal")
    if plan.schema_version == FOUNDATION_PACK_SCHEMA_VERSION:
        if _file_fingerprint(Path(pack_path)) != plan.pack_fingerprint:
            raise RuntimeError("Foundation Pack changed after preview")
        wire = load_foundation_pack(pack_path)
        if (wire.proposal != plan.foundation_proposal
                or not _same_foundation_release_approval(wire.approved_release,
                    ApprovedDefaultPackRelease.decode(plan.approved_default_release_json))
                or wire.content_rating_boundary != plan.content_rating_boundary):
            raise ValueError("Reviewed Foundation authority differs from its exact wire input")
    if plan.foundation_proposal is not None:
        plan = review_foundation_import_plan(app_root, plan, capture_effective=False)
    # Bind the stores this importer actually writes, not another active profile.
    with catalog_admission(app_root, sys.modules[__name__], role="import") as admission:
        plan = plan or plan_knowledge_pack_import(app_root, pack_path)
        admission.validate_plan(plan)
        return _import_knowledge_pack_admitted(app_root,pack_path,plan=plan)


def _import_knowledge_pack_admitted(
    app_root: Path,
    pack_path: Path,
    *,
    plan: KnowledgePackImportPlan | None = None,
) -> dict[str, dict[str, int]]:
    admission = require_catalog_admission(app_root, sys.modules[__name__])
    app_root = Path(app_root)
    pack_path = Path(pack_path)
    if _pending_pack_receipts(app_root):
        raise RuntimeError("Knowledge Pack recovery is pending. Complete recovery before importing another pack.")
    plan = plan or plan_knowledge_pack_import(app_root, pack_path)
    if admission is not None:
        admission.validate_plan(plan)
    if Path(plan.pack_path).resolve() != pack_path.resolve():
        raise RuntimeError("This import preview belongs to a different Knowledge Pack.")
    if _file_fingerprint(pack_path) != plan.pack_fingerprint:
        raise RuntimeError(
            "The Knowledge Pack changed after preview. Inspect it again before importing."
        )
    _verify_pack_authority(
        app_root,
        plan,
        message=(
            "Local or shipped knowledge changed after preview. Inspect the pack again before importing."
        ),
    )
    if plan.foundation_proposal is not None and (
            admission is None or not admission.enrolled or not plan.approved_default_release_json):
        raise ValueError("Foundation apply requires reviewed enrolled release authority")
    if not plan.can_apply:
        raise ValueError("Resolve every Knowledge Pack hold before applying the import.")

    results: dict[str, dict[str, int]] = {}
    with tempfile.TemporaryDirectory() as temp:
        temporary_root = Path(temp)
        work = temporary_root / "pack"
        work.mkdir()
        with zipfile.ZipFile(pack_path, "r") as archive:
            _extract_pack_safely(archive, work, foundation=plan.schema_version == FOUNDATION_PACK_SCHEMA_VERSION)
        manifest = _load_manifest(work, foundation=plan.schema_version == FOUNDATION_PACK_SCHEMA_VERSION)
        if int(manifest.get("schema_version", 1) or 1) != plan.schema_version:
            raise RuntimeError("The Knowledge Pack schema changed after preview.")

        stage = temporary_root / "stage"
        staged_targets: dict[Path, Path] = {}
        if plan.foundation_proposal is not None:
            results["foundation"] = {"participants": len(plan.foundation_proposal.payloads)}
        elif plan.schema_version >= 8:
            record_results, record_targets = _stage_record_import(
                app_root, stage, plan
            )
            results.update(record_results)
            staged_targets.update(record_targets)
            imported_tags = work / "data" / "user" / "tags.json"
            if imported_tags.exists():
                imported_tag_payload = _read_payload_checked(imported_tags)
                local_tags = app_root / "data" / "user" / "tags.json"
                has_legacy_lifecycle = bool(
                    imported_tag_payload.get("removed_tag_ids")
                    or imported_tag_payload.get("legacy_removed_tag_ids")
                    or imported_tag_payload.get("merged_tag_ids")
                )
                staged_tags = staged_targets.get(local_tags)
                if staged_tags is None and has_legacy_lifecycle:
                    staged_tags = stage / "data" / "user" / "tags.json"
                    staged_tags.parent.mkdir(parents=True, exist_ok=True)
                    if local_tags.exists():
                        shutil.copy2(local_tags, staged_tags)
                    else:
                        staged_tags.write_text(
                            json.dumps({
                                "schema_version": TagDatabase.SCHEMA_VERSION,
                                "tags": [], "merge_rules": [],
                                "removed_tag_ids": [], "merged_tag_ids": {},
                            }, indent=2) + "\n",
                            encoding="utf-8",
                        )
                    staged_targets[local_tags] = staged_tags
                if staged_tags is not None:
                    compatibility = _stage_tag_lifecycle_compatibility(
                        staged_tags, imported_tags, plan
                    )
                    tag_counts = results.setdefault("tags", {
                        "added": 0, "overridden": 0, "disabled_defaults": 0,
                        "already_present": 0, "conflicts": 0,
                    })
                    tag_counts.update(compatibility)
        else:
            for label, relative, legacy_relative, key in PACK_RECORDS:
                imported = work / relative
                if not imported.exists() and legacy_relative:
                    imported = work / legacy_relative
                if imported.exists():
                    local = app_root / relative
                    staged = stage / relative
                    if local not in staged_targets:
                        staged.parent.mkdir(parents=True, exist_ok=True)
                        if local.exists():
                            shutil.copy2(local, staged)
                        staged_targets[local] = staged
                    results[label] = _merge_records(staged, imported, key)

        if plan.foundation_proposal is None and (work / RELATIONSHIPS_RELATIVE).exists():
            local = app_root / RELATIONSHIPS_RELATIVE
            staged = stage / RELATIONSHIPS_RELATIVE
            staged_targets[local] = staged
            results["relationships"] = _stage_relationship_import(
                app_root, staged, plan
            )

        if plan.foundation_proposal is None and plan.schema_version>=6 and plan.imported_removal_archives:
            removal_paths={
                "tags":app_root/"data"/"user"/"tags.json",
                "categories":app_root/"data"/"user"/"categories.json",
                "relationships":app_root/RELATIONSHIPS_RELATIVE,
            }
            staged_removal={}
            defaults={
                "tags":{
                    "schema_version":TagDatabase.SCHEMA_VERSION,
                    "tags":[],"merge_rules":[],
                },
                "categories":{
                    "schema_version":CategoryRegistry.SCHEMA_VERSION,
                    "categories":[],"disabled_default_ids":[],
                },
                "relationships":{"schema_version":1,"relationships":[]},
            }
            for key,local in removal_paths.items():
                staged=staged_targets.get(local)
                if staged is None:
                    staged=stage/local.relative_to(app_root)
                    staged.parent.mkdir(parents=True,exist_ok=True)
                    if local.exists():shutil.copy2(local,staged)
                    else:staged.write_text(json.dumps(defaults[key],indent=2)+"\n",encoding="utf-8")
                    staged_targets[local]=staged
                staged_removal[key]=staged
            payloads={
                key:json.loads(path.read_text(encoding="utf-8"))
                for key,path in staged_removal.items()
            }
            preparation=prepare_removed_archive_import(
                app_root,plan.imported_removal_archives,
                tags_payload=payloads["tags"],
                categories_payload=payloads["categories"],
                relationships_payload=payloads["relationships"],
                relationship_file_existed=(
                    removal_paths["relationships"].exists()
                    or bool(payloads["relationships"].get("relationships"))
                ),
                tag_id_remap=dict(plan.removal_tag_remap),
                category_id_remap=dict(plan.removal_category_remap),
            )
            expected=[(item.stable_id,item.outcome) for item in plan.removal_decisions]
            actual=[(item.stable_id,item.outcome) for item in preparation.decisions]
            if actual!=expected:
                raise RuntimeError(
                    "Removed-vocabulary authority changed while the Knowledge Pack was staged. Inspect it again."
                )
            for key,payload in (
                ("tags",preparation.tags_payload),
                ("categories",preparation.categories_payload),
                ("relationships",preparation.relationships_payload),
            ):
                staged_removal[key].write_text(
                    json.dumps(payload,indent=2,ensure_ascii=False)+"\n",encoding="utf-8"
                )
            results["removed_vocabulary"]={
                "added":sum(item.outcome=="add" for item in preparation.decisions),
                "already_present":sum(item.outcome=="already_present" for item in preparation.decisions),
                "conflicts":0,
            }

        if plan.foundation_proposal is not None:
            _validate_foundation_proposal(app_root, plan.foundation_proposal)
            for relative, raw in plan.foundation_proposal.payloads:
                staged = stage / relative
                staged.parent.mkdir(parents=True, exist_ok=True)
                staged.write_bytes(raw)
                staged_targets[app_root / relative] = staged
        else:
            _stage_category_base_storage_retirement(app_root, stage, staged_targets)
            _stage_character_relationship_storage_retirement(app_root, stage, staged_targets)
            _stage_tag_relationship_storage_retirement(app_root, stage, staged_targets)

        if plan.approved_default_release_json:
            approved = ApprovedDefaultPackRelease.decode(plan.approved_default_release_json)
            target = app_root / "data/user/default_knowledge_state.json"
            state = _read_payload_checked(target) if target.exists() else {}
            payload = state if plan.foundation_proposal is not None and _foundation_release_matches(state, approved) else stage_release_state(state, approved, pack_fingerprint=plan.pack_fingerprint)
            staged = stage / "data/user/default_knowledge_state.json"
            staged.parent.mkdir(parents=True, exist_ok=True)
            if plan.foundation_proposal is not None and payload is state and target.exists():
                staged.write_bytes(_recovery_bytes(target)[0])
            else:
                staged.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            if _read_payload_checked(staged) != payload:
                raise RuntimeError("Approved default release state failed native staging read-back")
            staged_targets[target] = staged

        changed = {
            target: staged
            for target, staged in staged_targets.items()
            if staged.exists()
            and staged.read_bytes()
            != (target.read_bytes() if target.exists() else None)
        }
        if not changed:
            results["backup"] = {"path": ""}
            return results
        _verify_pack_authority(
            app_root,
            plan,
            message=(
                "Local or shipped knowledge changed while the import was being staged. Inspect the pack again."
            ),
        )

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = (
            app_root
            / "backups"
            / "knowledge-pack-import"
            / f"{stamp}_{uuid4().hex[:8]}"
        )
        backup.mkdir(parents=True, exist_ok=False)
        entries: list[dict[str, Any]] = []
        expected_authority = {
            str(Path(path).resolve()).casefold(): fingerprint
            for path, fingerprint in plan.authority_fingerprints
        }
        for index, target in enumerate(
            sorted(changed, key=lambda value: str(value).casefold())
        ):
            existed = target.exists()
            backup_name = f"{index}_{target.name}"
            if existed:
                current = target.read_bytes()
                expected = expected_authority.get(str(target.resolve()).casefold())
                if expected is not None and sha256(current).hexdigest() != expected:
                    raise RuntimeError(
                        "Local knowledge changed immediately before backup. Inspect the pack again."
                    )
                (backup / backup_name).write_bytes(current)
                if (backup / backup_name).read_bytes() != current:
                    raise RuntimeError("Knowledge Pack backup verification failed before import.")
            elif expected_authority.get(str(target.resolve()).casefold(), "missing") != "missing":
                raise RuntimeError(
                    "Local knowledge changed immediately before backup. Inspect the pack again."
                )
            entries.append(
                {
                    "target": str(target.resolve()),
                    "backup": backup_name,
                    "existed": existed,
                    "expected_before": sha256(current).hexdigest() if existed else "missing",
                    "expected_after": sha256(changed[target].read_bytes()).hexdigest(),
                }
            )
        if not 0 < len(entries) <= MAX_PACK_FILES:
            raise RuntimeError("Knowledge Pack transaction exceeds its native participant budget")
        recovery_path = backup / "recovery.json"
        recovery: dict[str, Any] = {
            "schema_version": 2,
            "status": "pending",
            "created_at": _utc_now(),
            "pack_fingerprint": plan.pack_fingerprint,
            "authority_fingerprint": plan.authority_fingerprint,
            "files": entries,
        }
        if plan.approved_default_release_json:
            recovery["approved_default_release_json"] = plan.approved_default_release_json
        if admission is not None and admission.enrolled:
            recovery.update(schema_version=ENROLLED_PACK_RECEIPT,
                            catalog_identity_binding=dict(admission.binding),
                            catalog_receipt_epoch=plan.catalog_receipt_epoch,
                            identity_bound_operation="pack_import",
                            default_authority_fingerprints=_default_baseline_from_plan(app_root, plan))
            if plan.foundation_proposal is not None:
                proposal = plan.foundation_proposal
                recovery["foundation_transition"] = {
                    "version": proposal.version, "source_reference": proposal.source_reference,
                    "source_inventory": [list(row) for row in proposal.source_inventory],
                    "immutable_dependencies": [[Path(path).relative_to(app_root).as_posix(), digest]
                        for path, digest in plan.authority_fingerprints
                        if not Path(path).is_relative_to(app_root / "data/defaults") and Path(path) not in changed],
                    "changed_defaults": sorted(target.relative_to(app_root).as_posix()
                        for target in changed if target.is_relative_to(app_root / "data/defaults")),
                }
                _require_foundation_authority(app_root, recovery, phase="before")
            if plan.foundation_proposal is not None:
                _verify_pack_authority(app_root, plan, message="Knowledge changed during Foundation backup; review again")
                admission.validate_plan(plan)
                prepared = _prepare_pack_restore_verified(recovery_path, dict(recovery, schema_version=2), app_root)
                if any((row[4][-1] if row[4] is not None else "missing") != row[1] for row in prepared):
                    raise RuntimeError("Foundation participant changed before mutation")
            admission.begin(recovery_path, recovery)
        _atomic_json(recovery_path, recovery)
        try:
            for target, staged in changed.items():
                if plan.foundation_proposal is not None:
                    checkpoint_catalog_admission()
                    _require_foundation_authority(app_root, recovery)
                expected = expected_authority.get(str(target.resolve()).casefold())
                if expected is not None and _file_fingerprint(target) != expected:
                    raise RuntimeError(
                        "Local knowledge changed immediately before write. The staged import was not applied."
                    )
                _atomic_bytes(target, staged.read_bytes())
            for entry in entries:
                target = Path(str(entry["target"]))
                if _file_fingerprint(target) != entry["expected_after"]:
                    raise RuntimeError(
                        f"Knowledge Pack import read-back verification failed for {target.name}."
                    )
            if plan.foundation_proposal is not None:
                _require_foundation_authority(app_root, recovery, phase="after")
            recovery["status"] = "complete"
            recovery["completed_at"] = _utc_now()
            _atomic_json(recovery_path, recovery)
        except Exception:
            durable, _state = _read_pack_receipt(recovery_path, app_root)
            if not (
                recovery.get("status") == "complete" and durable == recovery
                and all(_file_fingerprint(Path(entry["target"])) == entry["expected_after"] for entry in entries)
            ):
                _restore_pack_transaction(recovery_path, recovery, app_root)
                raise
            # The verified commit receipt was actually published. Do not undo
            # it or report an uncommitted failure after a post-replace error.
    results["backup"] = {"path": str(backup)}
    return results


def _require_installed_foundation_source(root, approved, proposal):
    inventory = [list(row) for row in proposal.source_inventory]
    state = _read_payload_checked(root / "data/user/default_knowledge_state.json")
    installed = release_records(state).get(approved.pack_id)
    committed_pack = installed.get("native_pack_fingerprint") if type(installed) is dict else None
    for path in (root / "backups/knowledge-pack-import").glob("*/recovery.json"):
        receipt, _stamp = _read_pack_receipt(path, root)
        metadata = receipt.get("approved_default_release_json")
        if receipt["status"] != "complete" or not metadata:
            continue
        recorded = ApprovedDefaultPackRelease.decode(metadata)
        mode = receipt.get("foundation_transition")
        if (recorded.release_fingerprint == approved.release_fingerprint
                and receipt.get("pack_fingerprint") == committed_pack
                and type(mode) is dict and mode.get("source_inventory") == inventory
                and mode.get("source_reference") == proposal.source_reference):
            return
    raise RuntimeError("Installed Foundation source inventory differs; review a distinct release transition")


def _foundation_release_matches(state, approved):
    """Release identity excludes predecessor and carrier packaging observations."""
    previous = release_records(state).get(approved.pack_id)
    if type(previous) is not dict:
        return False
    fields = {key: previous.get(key) for key in ApprovedDefaultPackRelease.__dataclass_fields__}
    installed = ApprovedDefaultPackRelease.decode(json.dumps(fields))
    return installed.release_fingerprint == approved.release_fingerprint


def _foundation_plan_paths(root, proposal):
    if proposal is not None:
        _check_export_path(root / "data/defaults")
    paths = set(_pack_authority_paths(root))
    if proposal is not None:
        if type(proposal) is not FoundationProposal:
            raise TypeError("Supply an explicit typed Foundation proposal")
        paths.update((root / "data/defaults").glob("*.json"))
        paths.update(root / name for name, _raw in proposal.payloads)
    return tuple(sorted(paths, key=lambda path: str(path).casefold()))


def _validate_foundation_proposal(root, proposal):
    if type(proposal) is not FoundationProposal:
        raise TypeError("Supply an explicit typed Foundation proposal")
    proposal.__post_init__()
    _check_export_path(root / "data/defaults")
    # Complete replacement inventory, with no deletion or implicit discovery.
    present = {path.relative_to(root).as_posix() for path in (root / "data/defaults").glob("*.json")}
    if not present.issubset(dict(proposal.source_inventory)):
        raise RuntimeError("Foundation source omits an existing default participant")


def _copy_foundation_review_stage(root, staged_root, proposal, authority, *, overlay=True):
    """Private read-review input, never a native write/restore participant."""
    for path in authority:
        _check_export_path(path)
        if path.exists() and path.name != "catalog_identity.json":
            target = staged_root / path.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_recovery_bytes(path)[0])
    for name, raw in proposal.payloads if overlay else ():
        target = staged_root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)


def review_foundation_import_plan(app_root, plan, *, capture_effective=True):
    """Seal a canonical effective read surface onto an already-reviewed plan.

    Original selected admission verifies the exact before state around copying and
    after review. Private staged capture occurs after leaving original admission;
    cross-catalog nested admission remains forbidden. No receipt or user mutation.
    """
    if type(plan) is not KnowledgePackImportPlan:
        raise TypeError("Supply the existing native import plan")
    if plan.foundation_proposal is None or not plan.can_apply:
        return plan
    from .foundation_dependencies import prepare_foundation_dependencies, qualify_foundation_relationships
    from .shared_tag_snapshot import capture_tag_snapshot
    from .tag_alias_relationships import TagAliasRelationshipService
    from suite_core.knowledge import Source
    root, proposal = Path(app_root), plan.foundation_proposal
    native = sys.modules[__name__]
    with tempfile.TemporaryDirectory() as temporary:
        before_root, staged_root = Path(temporary) / "before", Path(temporary) / "after"
        with catalog_admission(root, native, role="foundation-review") as admission:
            admission.validate_plan(plan)
            _verify_pack_authority(root, plan, message="Foundation knowledge changed after preview")
            if _file_fingerprint(Path(plan.pack_path)) != plan.pack_fingerprint:
                raise RuntimeError("Foundation Pack changed after preview")
            _copy_foundation_review_stage(root, before_root, proposal, _foundation_plan_paths(root, proposal), overlay=False)
            _copy_foundation_review_stage(root, staged_root, proposal, _foundation_plan_paths(root, proposal))
            _verify_pack_authority(root, plan, message="Foundation review inputs changed during copy")
        def stage_revision(stage):
            def revision():
                values = tuple((path.relative_to(stage).as_posix(), _file_fingerprint(path))
                               for path in _foundation_plan_paths(stage, proposal))
                return sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()
            return revision
        before_revision, staged_revision = stage_revision(before_root), stage_revision(staged_root)
        from .default_tag_identity import DefaultTagIdentityHold
        try:
            before = prepare_foundation_dependencies(before_root, revision=before_revision,
                expected_revision=before_revision())
            after = prepare_foundation_dependencies(staged_root, revision=staged_revision,
                expected_revision=staged_revision())
        except DefaultTagIdentityHold as error:
            raise ValueError("Staged Foundation relationships have invalid or missing effective dependencies: "
                             "saved identity admission: " + str(error)) from error
        def relationships(stage):
            rows = dict(RelationshipStore(stage / DEFAULT_RELATIONSHIPS_RELATIVE).relationships)
            rows.update(RelationshipStore(stage / RELATIONSHIPS_RELATIVE).relationships)
            return rows
        findings = qualify_foundation_relationships(before=before, after=after,
            before_relationships=relationships(before_root), relationships=relationships(staged_root).values())
        if findings:
            raise ValueError("Staged Foundation relationships have invalid or missing effective dependencies")
        if not capture_effective:
            with catalog_admission(root, native, role="foundation-review") as admission:
                admission.validate_plan(plan)
                _verify_pack_authority(root, plan, message="Foundation knowledge changed after preview")
            return plan
        def capture_stage(stage, dependencies, revision, reference):
            observed = revision()
            reader = dependencies._reader
            categories = CategoryRegistry(stage / "data/defaults/categories.json",
                stage / "data/user/categories.json")
            aliases = TagAliasRelationshipService(reader.tags,
                default_path=stage / DEFAULT_RELATIONSHIPS_RELATIVE,
                user_path=stage / RELATIONSHIPS_RELATIVE)
            return capture_tag_snapshot(tags=reader.raw_tags, categories=categories, aliases=aliases,
                relationships=aliases.workspace.effective,
                source=Source(reference, observed, observed, tuple(sorted(categories.categories))),
                revision=revision, effective_reader=reader)

        # The source review excludes recipient overrides, but uses the same native
        # reader/alias authority as effective capture. A raw compatibility index
        # cannot resolve historical alias endpoints or canonical-name precedence.
        # This is another private read view, never a writer, receipt or enrollment.
        default_root = Path(temporary) / "defaults-only"
        _copy_foundation_review_stage(root, default_root, proposal, ())
        empty_tags = default_root / "data/user/tags.json"
        empty_tags.parent.mkdir(parents=True, exist_ok=True)
        empty_tags.write_text(json.dumps({"schema_version": 11, "tags": [], "merge_rules": []}), encoding="utf-8")
        default_revision = lambda: _paths_fingerprint(_foundation_plan_paths(default_root, proposal))
        source_dependencies = prepare_foundation_dependencies(default_root,
            revision=default_revision, expected_revision=default_revision())
        default_snapshot = capture_stage(default_root, source_dependencies, default_revision,
                                         "reviewed private Foundation defaults-only source")
        defaults = tuple((row.id, row.display, row.category_id, row.aliases,
            row.minimum_rating, row.classification_status) for row in default_snapshot.tags)
        default_categories = tuple((row.id, row.label, row.path) for row in default_snapshot.categories)
        snapshot = capture_stage(staged_root, after, staged_revision, "reviewed private Foundation stage")
        effective = tuple((row.id, row.display, row.category_id, row.aliases,
            row.minimum_rating, row.classification_status) for row in snapshot.tags)
        with catalog_admission(root, native, role="foundation-review") as admission:
            admission.validate_plan(plan)
            _verify_pack_authority(root, plan, message="Foundation knowledge changed after preview")
        return replace(plan, foundation_default_tags=defaults,
                       foundation_default_categories=default_categories, foundation_effective_tags=effective)


def _plan_foundation_import(root, pack, proposal, *, wire=None):
    _validate_foundation_proposal(root, proposal)
    authority = _foundation_plan_paths(root, proposal)
    for path in authority:
        _check_export_path(path)
    evidence = tuple((str(path.resolve()), _file_fingerprint(path)) for path in authority)
    with tempfile.TemporaryDirectory() as temporary:
        staged_root = Path(temporary)
        _copy_foundation_review_stage(root, staged_root, proposal, authority)
        carrier = pack
        if wire is not None:
            carrier = staged_root / "foundation-carrier.zip"
            with zipfile.ZipFile(carrier, "x") as archive:
                archive.writestr("manifest.json", json.dumps({"format": "tagmanager_knowledge_pack",
                    "schema_version": 9, "content_rating_boundary": wire.content_rating_boundary}))
                archive.writestr("data/user/tags.json", json.dumps({"schema_version": 11, "tags": [], "merge_rules": []}))
        plan = _plan_knowledge_pack_import_admitted(staged_root, carrier)
        # This bounded mode installs exact defaults. A user-patch carrier would
        # promote incoming defaults into user authority or normalize histories.
        with zipfile.ZipFile(carrier) as archive:
            work = staged_root / "carrier"
            _extract_pack_safely(archive, work)
        for _label, relative, _legacy, key in PACK_RECORDS:
            payload = _read_payload_checked(work / relative) if (work / relative).exists() else {}
            if payload.get(key) or any(payload.get(field) for field in (
                    "disabled_default_ids", "disabled_default_merge_rule_ids", "removed_tag_ids",
                    "legacy_removed_tag_ids", "merged_tag_ids", "removed_vocabulary", "base_fingerprints")):
                raise ValueError("Foundation carrier must not contain user record directives")
        if plan.schema_version < 8 or plan.record_patches or plan.imported_relationships or plan.removal_decisions:
            raise ValueError("Foundation requires a schema 8/9 carrier without user mutations")
        defaults = RelationshipStore(staged_root / DEFAULT_RELATIONSHIPS_RELATIVE).relationships
        users = RelationshipStore(staged_root / RELATIONSHIPS_RELATIVE).relationships
        effective = dict(defaults); effective.update(users)
        issues = validate_relationships(effective.values())
        effective_tags = load_effective_tag_defaults(staged_root / "data/defaults")
        foundation_issues = list(plan.issues)
        foundation_issues.extend("Foundation identity: " + finding for finding in effective_tags.findings)
        record_payloads = {
            label: _read_payload_checked(staged_root / "data/defaults" / Path(relative).name)
            for label, relative, _legacy, _key in PACK_RECORDS
            if label not in {"tags", "merge_rules"}
            and (staged_root / "data/defaults" / Path(relative).name).exists()
        }
        source_tags = {"tags": list(effective_tags.tags), "merge_rules": [
            row for name in ("foundation_tags.json", "community_tags.json", "promoted_tags.json")
            for row in _read_records(staged_root / "data/defaults" / name, "merge_rules")]}
        source_relationships = _read_payload_checked(staged_root / DEFAULT_RELATIONSHIPS_RELATIVE) if (
            staged_root / DEFAULT_RELATIONSHIPS_RELATIVE).exists() else {"relationships": []}
        verified_boundary, findings = _content_review(staged_root, tags_payload=source_tags,
            record_payloads=record_payloads, relationships_payload=source_relationships)
        if (plan.content_rating_boundary in CONTENT_RATING_ORDER
                and CONTENT_RATING_ORDER[plan.content_rating_boundary] < CONTENT_RATING_ORDER[verified_boundary]):
            foundation_issues.append("Foundation content understates its reviewed rating; at least "
                                     + verified_boundary.title() + " is required")
        # Raw Foundation families outside the canonical review remain preserved,
        # but cannot establish a lower content boundary through an empty carrier.
        reviewed_names = {"foundation_tags.json", "community_tags.json", "promoted_tags.json", "relationships.json"}
        reviewed_names.update(Path(relative).name for label, relative, _legacy, _key in PACK_RECORDS
                              if label != "consumer_policies")
        unsupported = sorted(name for name, _raw in proposal.payloads if Path(name).name not in reviewed_names)
        if unsupported and plan.content_rating_boundary != "explicit":
            foundation_issues.append("Foundation lower content boundary has unreviewed definition families: "
                                     + ", ".join(unsupported))
        default_aliases = tag_alias_compatibility_index(defaults.values())
        default_tags = []
        for raw in effective_tags.tags:
            record = TagRecord.from_dict(raw)
            projected = tag_alias_compatibility_payload(record, defaults.values(), index=default_aliases)
            default_tags.append((record.id, record.display, record.category, tuple(projected.get("aliases", ())),
                record.content_sensitivity.minimum_rating, record.content_sensitivity.classification_status))
        default_categories = CategoryRegistry(staged_root / "data/defaults/categories.json",
            staged_root / "data/user/foundation-review-categories.json")
        default_categories.configure_category_base_authority(defaults.values())
        category_view = tuple((row.id, row.name, default_categories.path_label(row.id))
                             for row in sorted(default_categories.categories.values(), key=lambda row: row.id))
        plan = replace(plan, issues=tuple(foundation_issues), content_findings=findings,
            foundation_default_tags=tuple(sorted(default_tags)), foundation_default_categories=category_view)
        if issues:
            raise ValueError("Staged Foundation relationships have invalid or missing effective dependencies")
    return replace(plan, pack_path=str(pack.resolve()), pack_fingerprint=_file_fingerprint(pack),
        schema_version=FOUNDATION_PACK_SCHEMA_VERSION if wire is not None else plan.schema_version,
        authority_fingerprints=evidence,
        authority_fingerprint=_paths_fingerprint(authority), foundation_proposal=proposal)


def _require_foundation_authority(app_root, value, *, phase="pending"):
    """Authorize declared default participants using the native receipt hashes.

    Immutable dependencies retain their reviewed hashes. Changed defaults use
    the same expected_before/expected_after evidence as every other participant.
    """
    root = Path(app_root).absolute()
    _check_export_path(root / "data/defaults")
    mode = value.get("foundation_transition")
    if (type(mode) is not dict or set(mode) != {"version", "source_reference", "source_inventory", "changed_defaults", "immutable_dependencies"}
            or type(mode.get("version")) is not int or mode["version"] != 1
            or type(mode.get("source_reference")) is not str or not mode["source_reference"].strip()
            or len(mode["source_reference"]) > 8192):
        raise RuntimeError("Unsupported Foundation receipt authority")
    def rows(items):
        if type(items) is not list or not 0 < len(items) <= MAX_PACK_FILES:
            raise RuntimeError("Malformed Foundation inventory")
        result = {}
        for row in items:
            if type(row) is not list or len(row) != 2:
                raise RuntimeError("Malformed Foundation inventory row")
            name, digest = row
            try: _foundation_relative(name)
            except ValueError as error: raise RuntimeError(str(error)) from error
            if name in result or type(digest) is not str or (digest != "missing" and
                    (len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest))):
                raise RuntimeError("Invalid Foundation inventory evidence")
            result[name] = digest
        return result
    source, baseline = rows(mode["source_inventory"]), rows(value.get("default_authority_fingerprints"))
    if "missing" in source.values():
        raise RuntimeError("Foundation source inventory cannot contain missing payloads")
    required = {path.relative_to(root).as_posix() for path in _pack_authority_paths(root)
                if path.is_relative_to(root / "data/defaults")} | set(source) | {
                    path.relative_to(root).as_posix() for path in (root / "data/defaults").glob("*.json")}
    if set(baseline) != required:
        raise RuntimeError("Foundation default membership drifted")
    participants = mode["changed_defaults"]
    if (type(participants) is not list or any(type(name) is not str for name in participants)
            or len(set(participants)) != len(participants) or not set(participants).issubset(source)):
        raise RuntimeError("Malformed changed Foundation participants")
    files = value.get("files")
    if type(files) is not list or not 0 < len(files) <= MAX_PACK_FILES:
        raise RuntimeError("Malformed Foundation transaction participants")
    entries = {}
    changed_user = set()
    seen_targets = set()
    for item in files:
        if type(item) is not dict or type(item.get("target")) is not str:
            raise RuntimeError("Malformed Foundation transaction participant")
        target = Path(item["target"])
        if not target.is_absolute() or ".." in target.parts or target in seen_targets:
            raise RuntimeError("Unsafe or duplicate Foundation transaction target")
        seen_targets.add(target)
        if not target.is_relative_to(root / "data/defaults"):
            if target != root / "data/user/default_knowledge_state.json":
                raise RuntimeError("Foundation carrier cannot mutate user records")
            changed_user.add(target)
            before, after = item.get("expected_before"), item.get("expected_after")
            if (type(item.get("existed")) is not bool or type(before) is not str or type(after) is not str
                    or len(after) != 64 or any(c not in "0123456789abcdef" for c in after)
                    or (before != "missing" and (len(before) != 64 or any(c not in "0123456789abcdef" for c in before)))
                    or item["existed"] != (before != "missing")):
                raise RuntimeError("Malformed Foundation tracking before/after evidence")
            allowed = {before} if phase == "before" else {after} if phase == "after" else {before, after}
            state = _export_path_state(target)
            if (state[-1] if state is not None else "missing") not in allowed:
                raise RuntimeError("Foundation tracking changed unexpectedly")
        if target.is_relative_to(root / "data/defaults"):
            name = target.relative_to(root).as_posix()
            if name not in participants or name in entries:
                raise RuntimeError("Undeclared or duplicate Foundation participant")
            entries[name] = item
    expected_dependencies = {path.relative_to(root).as_posix()
        for path in (*_pack_target_paths(root), root / "data/user/catalog_identity.json") if path not in changed_user}
    dependencies = mode["immutable_dependencies"]
    if type(dependencies) is not list or len(dependencies) != len(expected_dependencies):
        raise RuntimeError("Incomplete Foundation immutable dependencies")
    observed_dependencies = set()
    for row in dependencies:
        if type(row) is not list or len(row) != 2:
            raise RuntimeError("Malformed Foundation immutable dependency")
        name, digest = row
        if (type(name) is not str or name not in expected_dependencies or name in observed_dependencies
                or type(digest) is not str or (digest != "missing" and
                    (len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)))):
            raise RuntimeError("Unsafe Foundation immutable dependency")
        observed_dependencies.add(name)
        state = _export_path_state(root / name)
        if (state[-1] if state is not None else "missing") != digest:
            raise RuntimeError("Foundation immutable dependency changed unexpectedly")
    if set(entries) != set(participants):
        raise RuntimeError("Missing changed Foundation participant")
    for name, before in baseline.items():
        entry = entries.get(name)
        if entry is not None:
            if (entry.get("expected_before") != before or entry.get("expected_after") != source[name]
                    or type(entry.get("existed")) is not bool or entry["existed"] != (before != "missing")):
                raise RuntimeError("Foundation participant differs from native before/after evidence")
            allowed = {before} if phase == "before" else {entry["expected_after"]} if phase == "after" else {before, entry["expected_after"]}
        else:
            if name in source and source[name] != before:
                raise RuntimeError("Changed Foundation default was not declared as a participant")
            allowed = {before}
        state = _export_path_state(root / name)
        if (state[-1] if state is not None else "missing") not in allowed:
            raise RuntimeError("Foundation participant or immutable dependency changed unexpectedly")
    return {root / name for name in participants}


def _default_baseline_from_plan(app_root, plan):
    """Durable explicit shipped evidence from the already-verified prewrite plan."""
    root = Path(app_root).absolute()
    expected = {str(Path(path).resolve()).casefold(): digest for path, digest in plan.authority_fingerprints}
    rows = [[path.relative_to(root).as_posix(), expected[str(path.resolve()).casefold()]]
            for path in _foundation_plan_paths(root, plan.foundation_proposal) if path.is_relative_to(root / "data/defaults")]
    if plan.foundation_proposal is None:
        _require_default_baseline(root, {"default_authority_fingerprints": rows})
    return rows


def _require_default_baseline(app_root, value):
    if "foundation_transition" in value:
        return _require_foundation_authority(app_root, value,
            phase="before" if value.get("status") == "restored" else "pending")
    root = Path(app_root).absolute()
    rows = value.get("default_authority_fingerprints")
    required = {path.relative_to(root).as_posix() for path in _pack_authority_paths(root)
                if path.is_relative_to(root / "data/defaults")}
    if type(rows) is not list or not rows:
        raise RuntimeError("Selected recovery lacks durable historical default evidence")
    observed = {}
    for row in rows:
        if type(row) is not list or len(row) != 2 or any(type(item) is not str for item in row):
            raise RuntimeError("Malformed durable default evidence")
        name, digest = row
        if name not in required or name in observed or (digest != "missing" and
                (len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest))):
            raise RuntimeError("Unsafe or incomplete durable default evidence")
        observed[name] = digest
    if set(observed) != required:
        raise RuntimeError("Shipped default membership differs from durable evidence")
    for name, digest in observed.items():
        state = _export_path_state(root / name)  # Canonical unredirected regular-file check.
        if (state[-1] if state is not None else "missing") != digest:
            raise RuntimeError("Shipped defaults differ from the durable selected baseline")


@dataclass(frozen=True)
class CompletedPackImportReview:
    """Immutable read evidence only; grants no Undo, recovery or enrollment."""
    catalog_root: str
    receipt_relative: str
    catalog_identity_binding: tuple
    receipt_sha256: str
    receipt_epoch: str
    default_authority_fingerprints: tuple
    participants: tuple


def review_completed_knowledge_pack_import(app_root, receipt, *, catalog_identity_binding):
    """Inspect one enrolled completed user import without inverse authority.

    Later mutations must obtain their own admission and revalidate all evidence.
    Synthetic inverse, Foundation and legacy enrollment routes stay unchanged.
    """
    from .catalog_identity_codec import require_inverse_catalog
    root, path = Path(app_root), Path(receipt)
    if (not root.is_absolute() or not path.is_absolute()
            or ".." in root.parts or ".." in path.parts):
        raise ValueError("Select explicit absolute catalog and receipt paths without traversal")
    _check_export_path(root)
    _check_export_path(path)
    if (not root.is_dir() or not (root / "data/user").is_dir()
            or path.name != "recovery.json"
            or path.parent.parent != root / "backups/knowledge-pack-import"):
        raise ValueError("Select one receipt inside the existing catalog import zone")
    if type(catalog_identity_binding) is not tuple:
        raise TypeError("Explicit immutable catalog identity binding required")
    expected = dict(catalog_identity_binding)
    header = root / "data/user/catalog_identity.json"
    require_inverse_catalog(_recovery_bytes(header)[0], expected)
    with catalog_admission(root, sys.modules[__name__], role="completed-pack-review") as admission:
        if not admission.enrolled or tuple(sorted(admission.binding.items())) != catalog_identity_binding:
            raise RuntimeError("Completed import review belongs to a stale catalog identity")
        value, stamp = _read_pack_receipt(path, root)
        if (type(value.get("schema_version")) is not int
                or value["schema_version"] != ENROLLED_PACK_RECEIPT
                or value["status"] != "complete"
                or value.get("identity_bound_operation") != "pack_import"):
            raise RuntimeError("Select an exact completed identity-bound user import")
        if "foundation_transition" in value:
            raise RuntimeError("Completed Foundation Undo requires separate inverse authority qualification")
        require_inverse_catalog(_recovery_bytes(header)[0], value.get("catalog_identity_binding"))
        _require_default_baseline(root, value)
        prepared = _prepare_pack_restore(path, value, root)
        if any(row[4] is None or row[4][-1] != row[2] for row in prepared):
            raise RuntimeError("Newer or restored participant bytes block completed import review")

        def observations(rows):
            return tuple((str(row[0].relative_to(root)), row[1], row[2], row[4]) for row in rows)

        observed = observations(prepared)
        fresh, fresh_stamp = _read_pack_receipt(path, root)
        if fresh != value or fresh_stamp != stamp:
            raise RuntimeError("Selected completed receipt changed during review")
        _require_default_baseline(root, fresh)
        if observations(_prepare_pack_restore(path, fresh, root)) != observed:
            raise RuntimeError("Catalog participants changed during completed import review")
        admission.checkpoint(ordinary=True)
        return CompletedPackImportReview(str(root), path.relative_to(root).as_posix(),
            tuple(sorted(admission.binding.items())), stamp[-1], admission.receipt_epoch,
            tuple(tuple(row) for row in value["default_authority_fingerprints"]), observed)


class CompletedPackInverseFailure(RuntimeError):
    """Retain the single reserved inverse identity after an uncertain outcome."""
    def __init__(self, receipt):
        self.receipt = Path(receipt)
        super().__init__("Completed inverse did not settle; retain and inspect exact receipt: " + str(receipt))


def undo_completed_knowledge_pack_import(app_root, review):
    """Explicit native inverse of one unchanged reviewed user import; no UI wiring."""
    from .catalog_admission import COMPLETED_PACK_UNDO_RECEIPT, selected_completed_pack_inverse
    if type(review) is not CompletedPackImportReview:
        raise TypeError("Exact immutable completed import review required")
    root = Path(app_root)
    with selected_completed_pack_inverse(root, sys.modules[__name__],
            binding=review.catalog_identity_binding, review=review) as admission:
        source = root / admission.undo_of["receipt_relative"]
        original = admission.baseline
        inverse = source.parent.parent / ("completed-pack-undo-" + uuid4().hex) / "recovery.json"
        try:
            prepared = _prepare_pack_restore(source, original, root)
            if any(row[4] is None or row[4][-1] != row[2] for row in prepared):
                raise RuntimeError("Newer or restored participant bytes block completed inverse")
            inverse.parent.mkdir(exist_ok=False)
            for entry, row in zip(original["files"], prepared):
                if entry["existed"]:
                    backup = inverse.parent / entry["backup"]
                    _atomic_bytes(backup, row[3])
                    if _recovery_bytes(backup)[0] != row[3]:
                        raise RuntimeError("Completed inverse backup read-back failed")
            fresh = _prepare_pack_restore(source, original, root)
            if any(row[4] is None or row[4][-1] != row[2] for row in fresh):
                raise RuntimeError("Catalog changed before completed inverse publication")
            pending = dict(schema_version=COMPLETED_PACK_UNDO_RECEIPT,
                operation_kind="completed_pack_undo", identity_bound_operation="completed_pack_undo",
                operation_id=inverse.parent.name, status="pending", undo_of=dict(admission.undo_of),
                catalog_identity_binding=original["catalog_identity_binding"],
                catalog_receipt_epoch=admission.prior_epoch, files=original["files"],
                default_authority_fingerprints=original["default_authority_fingerprints"])
            admission.begin(inverse, pending)
            _atomic_json(inverse, pending)
            if _read_pack_receipt(inverse, root)[0] != pending:
                raise RuntimeError("Completed inverse pending receipt read-back failed")
            _restore_pack_transaction(inverse, pending, root)
            if _read_pack_receipt(inverse, root)[0]["status"] != "restored":
                raise RuntimeError("Completed inverse result is unconfirmed")
        except Exception as exc:
            raise CompletedPackInverseFailure(inverse) from exc
        return inverse


def recover_completed_knowledge_pack_inverse(app_root, receipt, *, catalog_identity_binding):
    """Resume only this exact schema-7 intent; no automatic retry or new inverse."""
    from .catalog_admission import selected_completed_pack_inverse
    root, receipt = Path(app_root), Path(receipt)
    with selected_completed_pack_inverse(root, sys.modules[__name__],
            binding=catalog_identity_binding, receipt=receipt) as admission:
        try:
            durable, _stamp = _read_pack_receipt(admission.owned_receipt, root)
            if durable["status"] == "restored":
                admission.checkpoint()
                return False
            result = _restore_pack_transaction(admission.owned_receipt, durable, root)
            if _read_pack_receipt(admission.owned_receipt, root)[0]["status"] != "restored":
                raise RuntimeError("Completed inverse recovery result is unconfirmed")
            return result
        except Exception as exc:
            raise CompletedPackInverseFailure(receipt) from exc


def _require_pack_sandbox(sandbox):
    from suite_core.completed_pack_undo_candidate import SyntheticPackUndoSandbox
    if type(sandbox) is not SyntheticPackUndoSandbox or sandbox.native is not sys.modules[__name__]:
        raise RuntimeError("Use the exact canonical engine and explicit synthetic sandbox")
    return sandbox._marker()


def _selected_pack_source(sandbox, selection):
    from suite_core.completed_pack_undo_candidate import SelectedCompletedImport
    marker = _require_pack_sandbox(sandbox)
    if (type(selection) is not SelectedCompletedImport or selection.incarnation != sandbox.incarnation
            or selection.marker_sha256 != marker):
        raise RuntimeError("Completed import selection belongs to another catalog")
    relative = Path(selection.receipt_relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError("Unsafe selected completed receipt")
    path = sandbox.root / relative
    value, stamp = _read_pack_receipt(path, sandbox.root)
    if (stamp[-1] != selection.receipt_sha256 or type(value.get("schema_version")) is not int
            or value["schema_version"] != ENROLLED_PACK_RECEIPT or value["status"] != "complete"
            or value.get("identity_bound_operation") != "pack_import"):
        raise RuntimeError("Selected completed physical import changed")
    from suite_core.catalog_identity_candidate import require_inverse_catalog
    require_inverse_catalog(_recovery_bytes(sandbox.root / "data/user/catalog_identity.json")[0], value.get("catalog_identity_binding"))
    _require_default_baseline(sandbox.root, value)
    return path, value


def select_enrolled_completed_pack(sandbox, receipt):
    from suite_core.completed_pack_undo_candidate import SelectedCompletedImport
    with sandbox._guard():
        marker = _require_pack_sandbox(sandbox)
        path = Path(receipt).absolute()
        _value, stamp = _read_pack_receipt(path, sandbox.root)
        selected = SelectedCompletedImport(sandbox.incarnation, marker, str(path.relative_to(sandbox.root)), stamp[-1])
        _selected_pack_source(sandbox, selected)
        return selected


def undo_enrolled_completed_pack(sandbox, selection):
    from dataclasses import asdict
    from .catalog_admission import ENROLLED_INVERSE_RECEIPT, selected_pack_admission
    _require_pack_sandbox(sandbox)
    with sandbox._guard():
        _source, reviewed = _selected_pack_source(sandbox, selection)
        if "foundation_transition" in reviewed:
            raise RuntimeError("Completed Foundation Undo requires separate inverse authority qualification")
    with selected_pack_admission(sandbox, sys.modules[__name__], selection=selection, operation="undo") as admission:
        source, original = _selected_pack_source(sandbox, selection)
        prepared = _prepare_pack_restore(source, original, sandbox.root)
        if any(row[4] is None or row[4][-1] != row[2] for row in prepared):
            raise RuntimeError("Newer or restored participant bytes block completed Undo")
        inverse = source.parent.parent / ("synthetic-undo-" + uuid4().hex) / "recovery.json"
        inverse.parent.mkdir(exist_ok=False)
        for entry, row in zip(original["files"], prepared):
            if entry["existed"]:
                backup = inverse.parent / entry["backup"]
                _atomic_bytes(backup, row[3])
                if _recovery_bytes(backup)[0] != row[3]:
                    raise RuntimeError("Inverse backup read-back failed")
        fresh = _prepare_pack_restore(source, original, sandbox.root)
        if any(row[4] is None or row[4][-1] != row[2] for row in fresh):
            raise RuntimeError("Catalog changed before inverse publication")
        pending = dict(schema_version=ENROLLED_INVERSE_RECEIPT, operation_kind="synthetic_pack_undo",
            operation_id=inverse.parent.name, status="pending", undo_of=asdict(selection), files=original["files"],
            catalog_identity_binding=original["catalog_identity_binding"], identity_bound_operation="pack_undo",
            default_authority_fingerprints=original["default_authority_fingerprints"])
        admission.begin(inverse, pending)
        _atomic_json(inverse, pending)
        _restore_pack_transaction(inverse, pending, sandbox.root)
        return inverse


def recover_enrolled_pack(sandbox, receipt, *, selection=None):
    from .catalog_admission import selected_pack_admission
    _require_pack_sandbox(sandbox)
    operation = "import_recovery" if selection is None else "inverse_recovery"
    with selected_pack_admission(sandbox, sys.modules[__name__], selection=selection,
                                 receipt=receipt, operation=operation) as admission:
        durable, _stamp = _read_pack_receipt(admission.owned_receipt, sandbox.root)
        if durable["status"] == "restored":
            _prepare_pack_restore(admission.owned_receipt, durable, sandbox.root)
            return False
        return _restore_pack_transaction(admission.owned_receipt, durable, sandbox.root)


def recover_selected_knowledge_pack_import(app_root, receipt, *, catalog_identity_binding):
    """Native selected enrolled import recovery; no synthetic sandbox fallback."""
    from .catalog_admission import selected_native_pack_recovery
    with selected_native_pack_recovery(Path(app_root), sys.modules[__name__], receipt=receipt,
                                       catalog_identity_binding=catalog_identity_binding) as admission:
        durable, _stamp = _read_pack_receipt(admission.owned_receipt, Path(app_root))
        if durable["status"] == "restored":
            _prepare_pack_restore(admission.owned_receipt, durable, Path(app_root))
            return False
        return _restore_pack_transaction(admission.owned_receipt, durable, Path(app_root))


def observe_approved_default_pack_release(app_root, pack_id):
    """Read native installed release evidence under ordinary catalog admission."""
    with catalog_admission(Path(app_root), sys.modules[__name__], role="default-release-observation"):
        state_path = Path(app_root) / "data/user/default_knowledge_state.json"
        state = _read_payload_checked(state_path) if state_path.exists() else {}
        value = release_records(state).get(pack_id)
        if value is None:
            return None
        if type(value) is not dict:
            raise ValueError("Malformed native approved Pack state")
        known = {key: value.get(key) for key in ApprovedDefaultPackRelease.__dataclass_fields__}
        approved = ApprovedDefaultPackRelease.decode(json.dumps(known))
        committed = False
        for path in (Path(app_root) / "backups/knowledge-pack-import").glob("*/recovery.json"):
            receipt, _stamp = _read_pack_receipt(path, Path(app_root))
            if receipt["status"] != "complete" or not receipt.get("approved_default_release_json"):
                continue
            observation = ApprovedDefaultPackRelease.decode(receipt["approved_default_release_json"])
            if (observation.encode() == approved.encode()
                    and receipt.get("pack_fingerprint") == value.get("native_pack_fingerprint")
                    and any(Path(entry.get("target", "")) == state_path.absolute()
                            for entry in receipt.get("files", ()) if type(entry) is dict)):
                committed = True
                break
        if not committed:
            raise RuntimeError("Approved default Pack state lacks its completed native commit receipt")
        return json.loads(json.dumps(value))
