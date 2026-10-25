"""Read-only Foundation endpoint qualification on an isolated native stage.

Reuse the qualified native lifecycle/saved-identity reader. This does not enroll
recipients, synchronize defaults, rewrite endpoints or grant transaction access.
The caller owns the stage/host guard; returned views detect subsequent drift.
"""
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from .automation_rule_conversion import RulePairAdmission
from .category_registry import CategoryRegistry
from .default_tag_identity import _payload
from .effective_tag_snapshot_reader import (
    EffectiveTagReaderChanged, _pin, prepare_effective_tag_snapshot_reader,
)
from .relationship_workspace import relationship_fingerprint
from .tag_database import TagDatabase
from .knowledge_engine import FoundationKnowledge
from .knowledge_relationships import NO_ALLOWED_DIMENSIONS, NOT_SET_MODIFIER_VALUE
from .allowed_dimension_policy import (
    LegacyModifierPolicyCompatibility, inspect_legacy_finite_modifier_policy,
)


@dataclass(frozen=True)
class FoundationDependencies:
    ids: object
    tag_targets: object
    _reader: object = field(repr=False, compare=False)
    _pins: tuple = field(repr=False, compare=False)
    _revision: object = field(repr=False, compare=False)
    _foundation: object = field(repr=False, compare=False)
    _categories: object = field(repr=False, compare=False)

    def verify(self):
        self._reader.verify(self._revision)
        if any(_pin(path) != digest for path, digest in self._pins):
            raise EffectiveTagReaderChanged("Foundation entity dependencies changed")

    def resolve(self, kind, identity):
        self.verify()
        return self._resolve(kind, identity)

    def legacy_modifier_policy(self, relationship):
        """Inspect a pinned finite policy without granting family semantics."""
        self.verify()
        result = self._legacy_modifier_policy(relationship)
        self.verify()
        return result

    def _legacy_modifier_policy(self, row):
        return inspect_legacy_finite_modifier_policy(row, foundation=self._foundation,
            categories=self._categories, resolve_tag=lambda identity: self._resolve("tag", identity))

    def _resolve(self, kind, identity):
        # A batch checks participants once before/after the immutable lookup loop.
        if kind == "tag":
            return self.tag_targets.get(identity)
        if kind == "modifier_dimension":
            target = self._foundation.canonical_modifier_group(identity, self._categories)
            return target if target in self.ids["category"] else None
        if kind == "modifier_value":
            matches = {(row.id, row.group) for row in self._foundation.modifiers_for(identity)}
            return identity if len(matches) == 1 else None
        family = "category" if kind == "modifier_dimension" else kind
        return identity if identity in self.ids.get(family, ()) else None


def prepare_foundation_dependencies(root, *, revision, expected_revision):
    """Compose existing initialized recipient/stage files without any writes.

    Empty initialized user stores qualify fresh recipients. Missing user Tags is
    a host initialization prerequisite, not permission to create a user store.
    Modifier dimensions retain the existing native category representation.
    """
    root = Path(root).absolute()
    defaults, user = root / "data/defaults", root / "data/user"
    if not (user / "tags.json").is_file():
        raise ValueError("Foundation dependencies require an initialized user Tag store")
    paths = tuple(directory / filename for directory in (defaults, user)
                  for filename in ("series.json", "characters.json", "outfits.json"))
    paths += (defaults / "modifiers.json", defaults / "concepts.json")
    for path in paths:
        for parent in (path, *path.parents):
            if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
                raise EffectiveTagReaderChanged("Redirected Foundation entity participant")
    pins = tuple((path, _pin(path)) for path in paths)
    reader = prepare_effective_tag_snapshot_reader(tags=TagDatabase(user / "tags.json",
        rule_pair_admission=RulePairAdmission(user)),
        defaults_dir=defaults, revision=revision, expected_revision=expected_revision)
    tags = reader.tags
    candidates = set(tags.tags) | set(tags.merged_tag_ids)
    targets = {identity: target for identity in candidates
               if (target := tags.resolve_tag_id(identity)) is not None}
    categories = CategoryRegistry(defaults / "categories.json", user / "categories.json")
    foundation = FoundationKnowledge(defaults / "modifiers.json", defaults / "concepts.json")
    ids = {"tag": frozenset(targets), "category": frozenset(categories.categories)}
    ids.update({"modifier_value": frozenset(form for values in foundation.modifiers.values()
                                            for row in values for form in row.forms()),
                "allowed_dimension_state": frozenset({NO_ALLOWED_DIMENSIONS}),
                "modifier_value_state": frozenset({NOT_SET_MODIFIER_VALUE}),
                "concept": frozenset(foundation.concepts)})
    for kind, filename, key in (("character_source", "series.json", "series"),
                               ("character", "characters.json", "characters"),
                               ("outfit", "outfits.json", "outfits")):
        def read(directory):
            path = directory / filename
            return _payload(path.read_bytes() if path.exists() else None)
        shipped, local = read(defaults), read(user)
        default_ids = {str(row.get("id")) for row in shipped.get(key, ())}
        ids[kind] = frozenset((default_ids - set(local.get("disabled_default_ids", ()))) |
                             {str(row.get("id")) for row in local.get(key, ())})
    result = FoundationDependencies(MappingProxyType(ids), MappingProxyType(targets),
                                    reader, pins, revision, foundation, categories)
    for path in (defaults / "modifiers.json", defaults / "concepts.json"):
        if foundation._loaded_source_fingerprints[path] != (() if _pin(path) is None else _pin(path)):
            raise EffectiveTagReaderChanged("Loaded Foundation vocabulary is stale")
    expected = dict(reader._pins)
    for path in (defaults / "categories.json", user / "categories.json"):
        digest = expected[path]
        if categories._loaded_source_fingerprints.get(path) != (() if digest is None else digest):
            raise EffectiveTagReaderChanged("Loaded Foundation Category reader is stale")
    result.verify()
    return result


@dataclass(frozen=True)
class FoundationDependencyFinding:
    relationship_id: str
    endpoint_kind: str
    endpoint_id: str
    reason: str


def qualify_foundation_relationships(*, before, after, before_relationships, relationships,
                                    require_supported_representation=False):
    """Exact existing history exception; fresh or changed missing edges hold.

    Strict representation reporting is for producer/fresh-source qualification,
    separate from the existing recipient's bounded membership check. It does not
    claim unknown endpoint kinds are invalid facts or silently discard them.
    """
    before.verify()
    after.verify()
    findings = []
    for row in relationships:
        if row.disabled:
            continue
        prior = before_relationships.get(row.id)
        unchanged = prior is not None and relationship_fingerprint(row) == relationship_fingerprint(prior)
        compatibility = (after._legacy_modifier_policy(row) if len(row.endpoints) > 1
            and after._resolve(row.endpoints[1].kind, row.endpoints[1].id) is None else None)
        if compatibility is not None and not compatibility.supported:
            findings.append(FoundationDependencyFinding(row.id, "modifier_dimension",
                compatibility.dimension_id, compatibility.reason))
        for endpoint in row.endpoints:
            if (compatibility is not None and endpoint is row.endpoints[1]):
                # Unsupported legacy shapes always hold, including unchanged
                # historical rows. Do not turn the legacy ID into a Category.
                continue
            family = "category" if endpoint.kind == "modifier_dimension" else endpoint.kind
            if family not in after.ids:
                if require_supported_representation and endpoint.kind != "alias_text":
                    findings.append(FoundationDependencyFinding(row.id, endpoint.kind, endpoint.id,
                                                               "unsupported_endpoint_representation"))
                continue
            if after._resolve(endpoint.kind, endpoint.id) is not None:
                continue
            if unchanged and before._resolve(endpoint.kind, endpoint.id) is None:
                continue
            findings.append(FoundationDependencyFinding(row.id, endpoint.kind, endpoint.id,
                                                       "unavailable_dependency"))
    before.verify()
    after.verify()
    return tuple(findings)


@dataclass(frozen=True)
class FoundationDependencyClosure:
    """Strict source endpoint report, explicitly separate from full graph proof."""
    findings: tuple
    unsupported_families: tuple
    retained_user_participants: tuple
    covered_endpoint_kinds: tuple
    coverage: str = field(default="relationship_endpoints", init=False)
    unqualified_reference_families: tuple = field(default=("category_record_references",
        "entity_record_references", "tag_record_references", "foundation_vocabulary_references",
        "portable_lifecycle"), init=False)
    # Embedded record references/unknown families require their canonical owners.
    # A clean endpoint report must never be advertised as full blank installation.
    full_closure_qualified: bool = field(default=False, init=False)

    @property
    def relationship_endpoints_closed(self):
        return not self.findings and not self.retained_user_participants


def inspect_foundation_dependency_closure(root, *, default_membership, revision, expected_revision):
    """Report strict isolated source closure without any historical-edge waiver.

    Caller supplies the exact inventory's relative names and an initialized blank
    read stage. Nothing is synchronized, erased or enrolled. Unsupported source
    families and retained user authority remain explicit, not silently pruned.
    This does not qualify embedded record references or certify a public pack.
    """
    from .relationship_store import RelationshipStore
    root = Path(root).absolute()
    names = tuple(default_membership)
    if (not names or len(names) > 64 or len(set(names)) != len(names)
            or any(type(name) is not str or name != "data/defaults/" + Path(name).name
                   or not name.endswith(".json") or any(c in name for c in "\\:\0") for name in names)):
        raise ValueError("Explicit canonical Foundation membership required for closure report")
    dependencies = prepare_foundation_dependencies(root, revision=revision, expected_revision=expected_revision)
    raw_user = []
    for filename in ("tags.json", "categories.json", "characters.json", "series.json", "outfits.json",
                     "patterns.json", "consumer_policies.json", "relationships.json", "default_knowledge_state.json"):
        path = root / "data/user" / filename
        payload = _payload(path.read_bytes() if path.exists() else None)
        if any(value not in (None, "", [], {}) for key, value in payload.items() if key != "schema_version"):
            raw_user.append("data/user/" + filename)
    relationships = dict(RelationshipStore(root / "data/defaults/relationships.json").relationships)
    # Source-only: user overrides cannot hide a malformed/unclosed shipped edge.
    findings = qualify_foundation_relationships(before=dependencies, after=dependencies,
        before_relationships={}, relationships=relationships.values(), require_supported_representation=True)
    represented = {"foundation_tags.json", "community_tags.json", "promoted_tags.json", "tag_aliases.json",
                   "categories.json", "characters.json", "series.json", "outfits.json", "relationships.json",
                   "concepts.json", "modifiers.json"}
    unsupported = tuple(sorted(Path(name).stem for name in names if Path(name).name not in represented))
    dependencies.verify()
    return FoundationDependencyClosure(findings, unsupported, tuple(raw_user),
        ("alias_text", "category", "character", "character_source", "concept", "modifier_dimension",
         "modifier_value", "modifier_value_state", "allowed_dimension_state", "outfit", "tag"))


@dataclass(frozen=True)
class FoundationCapabilityReport:
    """Inventory-bound preservation and execution evidence, never install authority."""
    source_inventory: tuple
    annotations: tuple
    declarations: tuple
    executable_policies: tuple
    preservation_findings: tuple
    endpoint_findings: tuple
    version: int = field(default=1, init=False)
    full_execution_qualified: bool = field(default=False, init=False)

    @property
    def preservation_supported(self):
        return not self.preservation_findings and not self.endpoint_findings

    @property
    def state(self):
        return "preservation_supported_with_legacy_declarations" if self.preservation_supported else "preservation_hold"


def inspect_foundation_capabilities(root, *, source_inventory, revision, expected_revision):
    """Preserve legacy source facts separately from actual canonical policy readiness.

    Caller supplies exact reviewed default inventory. Fresh callers initialize
    blank user participants; no matching-history exception is used for endpoints.
    This report is not a transaction receipt, publication or closure shortcut.
    """
    from .relationship_store import RelationshipStore
    from .allowed_dimension_policy import AllowedDimensionPolicyService
    root = Path(root).absolute()
    inventory = tuple(source_inventory)
    if not inventory or len(inventory) > 64 or len({name for name, _ in inventory}) != len(inventory):
        raise ValueError("Exact bounded default source inventory required")
    for name, digest in inventory:
        if (type(name) is not str or name != "data/defaults/" + Path(name).name
                or not name.endswith(".json") or any(c in name for c in "\\:\0")
                or type(digest) is not str or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)):
            raise ValueError("Canonical default source inventory required")
        pin = _pin(root / name)
        if pin is None or pin.hex() != digest:
            raise EffectiveTagReaderChanged("Capability source inventory changed")
    declared = {name for name, _ in inventory}
    def verify_membership():
        # Same top-level JSON membership as the native Foundation transaction.
        present = {path.relative_to(root).as_posix() for path in (root / "data/defaults").glob("*.json")}
        if present != declared:
            raise EffectiveTagReaderChanged("Capability source inventory membership changed")
    verify_membership()
    dependencies = prepare_foundation_dependencies(root, revision=revision, expected_revision=expected_revision)
    loaded_defaults = {path.relative_to(root).as_posix() for path, pin in
        (*dependencies._reader._pins, *dependencies._pins)
        if pin is not None and path.parent == root / "data/defaults"}
    if not loaded_defaults <= declared or "data/defaults/relationships.json" not in declared:
        raise ValueError("Capability inventory omits a loaded default participant")
    annotations, declarations, findings = [], [], []
    for row in dependencies._reader.tags.tags.values():
        if row.concept_id:
            interpreted = dependencies._foundation.interpret_value_annotation(row)
            annotations.append((row.id, interpreted))
            if interpreted.kind == "unsupported_annotation":
                findings.append((row.id, interpreted.reason))
    for concept in dependencies._foundation.concepts.values():
        for group in concept.allowed_modifiers:
            declarations.append((concept.id, dependencies._foundation.interpret_group_declaration(group, dependencies._categories)))
    rows = tuple(RelationshipStore(root / "data/defaults/relationships.json").relationships.values())
    endpoints = qualify_foundation_relationships(before=dependencies, after=dependencies,
        before_relationships={}, relationships=rows, require_supported_representation=True)
    policy = AllowedDimensionPolicyService(dependencies._reader.tags, dependencies._categories,
        dependencies._foundation, default_path=root / "data/defaults/relationships.json",
        user_path=root / "data/user/relationships.json", publish_shared_authority=False)
    owners = sorted({row.endpoints[0].id for row in rows if row.kind == "allowed_dimension"
                     and row.endpoints and not row.disabled})
    executable = tuple((identity, dependencies._foundation.interpret_validated_policy(policy, identity)) for identity in owners)
    dependencies.verify()
    verify_membership()
    for name, digest in inventory:
        pin = _pin(root / name)
        if pin is None or pin.hex() != digest:
            raise EffectiveTagReaderChanged("Capability source inventory changed")
    return FoundationCapabilityReport(inventory, tuple(annotations), tuple(declarations), executable,
        tuple(findings), endpoints)
