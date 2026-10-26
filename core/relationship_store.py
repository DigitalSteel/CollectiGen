from __future__ import annotations

from .catalog_admission import native_store_api

import json
import os
import shutil
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import Any, Iterable
from uuid import uuid4

from .knowledge_relationships import (
    ALLOWED_DIMENSION,
    ALLOWED_DIMENSION_STATE,
    ADVISORY_ASSOCIATION,
    ASSOCIATED_CHARACTER,
    CATEGORY_BASE,
    CHARACTER_SOURCE,
    ENTAILMENT,
    ENTITY_TAG,
    IDENTITY_ALIAS,
    MODIFIER_VALUE,
    MODIFIER_VALUE_STATE,
    NOT_SET_MODIFIER_VALUE,
    NO_ALLOWED_DIMENSIONS,
    MODIFIER_ROLE,
    MODIFIER_ROLE_STATE,
    OTHER_APPEARANCE,
    ORDINARY_MODIFIER_ROLE,
    SCOPED_CONSTRAINT,
    SPECIES_TRAIT,
    SPECIFICITY,
    VERSION_OF,
    KnowledgeRelationship,
    KnowledgeRelationshipRegistry,
    RelationshipEndpoint,
)
from .tag_collection import normalize_tag


_PARSED_STORE_CACHE_LOCK = RLock()
_PARSED_STORE_CACHE_LIMIT = 4
_PARSED_STORE_CACHE: OrderedDict[
    Path,
    tuple[str, bytes | None, int, dict[str, Any], tuple[KnowledgeRelationship, ...]],
] = OrderedDict()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fingerprint(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest() if path.exists() else "missing"


@dataclass(frozen=True)
class RelationshipStoreIssue:
    code: str
    relationship_id: str
    message: str


@dataclass(frozen=True)
class RelationshipMigrationPlan:
    relationships: tuple[KnowledgeRelationship, ...]
    source_fingerprints: tuple[tuple[str, str], ...]
    issues: tuple[RelationshipStoreIssue, ...]

    @property
    def can_apply(self) -> bool:
        return not self.issues


class RelationshipStore:
    """Versioned standalone store for cross-entity relationship records."""

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path, *, cache_parsed: bool = False) -> None:
        self.path = Path(path)
        self.cache_parsed = bool(cache_parsed)
        self.relationships: dict[str, KnowledgeRelationship] = {}
        self.extras: dict[str, Any] = {}
        self.schema_version = self.SCHEMA_VERSION
        self._loaded_bytes: bytes | None = None
        self.load()

    @native_store_api("path")
    def load(self) -> None:
        raw = self.path.read_bytes() if self.path.exists() else None
        cache_key = self.path.resolve()
        raw_fingerprint = sha256(raw).hexdigest() if raw is not None else "missing"
        if self.cache_parsed:
            with _PARSED_STORE_CACHE_LOCK:
                cached = _PARSED_STORE_CACHE.get(cache_key)
                if cached is not None:_PARSED_STORE_CACHE.move_to_end(cache_key)
            if cached is not None and cached[0] == raw_fingerprint:
                _fingerprint_value,cached_raw,version,extras,relationships = cached
                self.schema_version=version
                self.extras=dict(extras)
                self.relationships={item.id:item for item in relationships}
                self._loaded_bytes=cached_raw
                return
        payload = json.loads(raw.decode("utf-8")) if raw is not None else {}
        if not isinstance(payload, dict):
            raise ValueError("The relationship store must contain a JSON object.")
        version = int(payload.get("schema_version", self.SCHEMA_VERSION) or 0)
        if version < 1:
            raise ValueError("The relationship store schema version is invalid.")
        self.schema_version = max(self.SCHEMA_VERSION, version)
        self.extras = {
            key: value
            for key, value in payload.items()
            if key not in {"schema_version", "relationships"}
        }
        relationships: dict[str, KnowledgeRelationship] = {}
        for value in payload.get("relationships", ()):
            if not isinstance(value, dict):
                continue
            relationship = KnowledgeRelationship.from_dict(value)
            if relationship.id in relationships:
                raise ValueError(
                    f"The relationship store contains duplicate ID {relationship.id!r}."
                )
            relationships[relationship.id] = relationship
        issues = validate_relationships(relationships.values())
        if issues:
            raise ValueError(" ".join(issue.message for issue in issues[:3]))
        self.relationships = relationships
        self._loaded_bytes = raw
        if self.cache_parsed:
            with _PARSED_STORE_CACHE_LOCK:
                _PARSED_STORE_CACHE[cache_key]=(
                    raw_fingerprint,raw,self.schema_version,dict(self.extras),
                    tuple(self.relationships.values()),
                )
                _PARSED_STORE_CACHE.move_to_end(cache_key)
                while len(_PARSED_STORE_CACHE)>_PARSED_STORE_CACHE_LIMIT:
                    _PARSED_STORE_CACHE.popitem(last=False)

    def payload(self) -> dict[str, Any]:
        payload = dict(self.extras)
        payload.update(
            {
                "schema_version": self.schema_version,
                "relationships": [
                    relationship.to_dict()
                    for relationship in sorted(
                        self.relationships.values(), key=lambda item: item.id
                    )
                ],
            }
        )
        return payload

    def replace(self, relationships: Iterable[KnowledgeRelationship]) -> None:
        values = tuple(relationships)
        issues = validate_relationships(values)
        if issues:
            raise ValueError(" ".join(issue.message for issue in issues[:3]))
        self.relationships = {item.id: item for item in values}

    @native_store_api("path")
    def save_verified(self, *, backup_root: str | Path | None = None) -> Path:
        current = self.path.read_bytes() if self.path.exists() else None
        if current != self._loaded_bytes:
            raise RuntimeError(
                "Relationships changed after this store was opened. Reload and review before saving."
            )
        root = (
            Path(backup_root)
            if backup_root is not None
            else self.path.parent / "backups" / "relationship-migration"
        )
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = root / f"{stamp}_{uuid4().hex[:8]}"
        backup.mkdir(parents=True, exist_ok=False)
        backup_name = f"0_{self.path.name}"
        if current is not None:
            shutil.copy2(self.path, backup / backup_name)
        manifest_path = backup / "recovery.json"
        manifest = {
            "schema_version": 1,
            "status": "pending",
            "created_at": _utc_now(),
            "files": [
                {
                    "target": str(self.path.resolve()),
                    "backup": backup_name,
                    "existed": current is not None,
                }
            ],
        }
        _atomic_json(manifest_path, manifest)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_json(self.path, self.payload())
            # The verified read-back is also the authoritative parsed snapshot
            # for immediate consumers.  The cache remains fingerprint-guarded,
            # so an external edit still forces a fresh parse on the next load.
            saved = RelationshipStore(self.path,cache_parsed=True)
            if saved.payload() != self.payload():
                raise RuntimeError("Relationship save failed semantic read-back verification.")
            manifest["status"] = "complete"
            manifest["completed_at"] = _utc_now()
            _atomic_json(manifest_path, manifest)
            self._loaded_bytes = self.path.read_bytes()
            return manifest_path
        except Exception:
            if current is None:
                self.path.unlink(missing_ok=True)
            else:
                temporary = self.path.with_suffix(self.path.suffix + ".restore")
                temporary.write_bytes(current)
                os.replace(temporary, self.path)
            manifest["status"] = "restored"
            manifest["restored_at"] = _utc_now()
            _atomic_json(manifest_path, manifest)
            self._loaded_bytes = current
            raise


def validate_relationships(
    relationships: Iterable[KnowledgeRelationship],
) -> tuple[RelationshipStoreIssue, ...]:
    values = tuple(relationships)
    issues: list[RelationshipStoreIssue] = []
    by_id: set[str] = set()
    directed_edges: dict[str, dict[str, set[str]]] = {
        SPECIFICITY: {},
        ENTAILMENT: {},
    }
    semantic_pairs: set[tuple[str, str]] = set()
    constraint_pairs: set[tuple[str, str]] = set()
    modifier_role_tags: set[str] = set()
    modifier_value_slots: set[tuple[str, str]] = set()
    allowed_dimension_slots: set[tuple[str, str]] = set()
    allowed_dimension_states: set[str] = set()
    alias_targets: dict[str, str] = {}
    category_base_sources: set[str] = set()

    for relationship in values:
        if not relationship.id:
            issues.append(RelationshipStoreIssue("missing_id", "", "A relationship is missing its stable ID."))
            continue
        if relationship.id in by_id:
            issues.append(
                RelationshipStoreIssue(
                    "duplicate_id",
                    relationship.id,
                    f"Relationship ID {relationship.id!r} is duplicated.",
                )
            )
        by_id.add(relationship.id)
        if not relationship.kind:
            issues.append(
                RelationshipStoreIssue(
                    "missing_kind", relationship.id, "A relationship is missing its type."
                )
            )
        if len(relationship.endpoints) < 2:
            issues.append(
                RelationshipStoreIssue(
                    "too_few_endpoints",
                    relationship.id,
                    f"Relationship {relationship.id!r} needs at least two endpoints.",
                )
            )
            continue
        endpoint_keys = [(item.kind, item.id) for item in relationship.endpoints]
        if any(not kind or not endpoint_id for kind, endpoint_id in endpoint_keys):
            issues.append(
                RelationshipStoreIssue(
                    "invalid_endpoint",
                    relationship.id,
                    f"Relationship {relationship.id!r} has an incomplete endpoint.",
                )
            )
        if len(endpoint_keys) != len(set(endpoint_keys)):
            issues.append(
                RelationshipStoreIssue(
                    "duplicate_endpoint",
                    relationship.id,
                    f"Relationship {relationship.id!r} repeats an endpoint.",
                )
            )
        def is_tag_endpoint(endpoint) -> bool:
            return endpoint.kind == "tag" or (
                relationship.disabled
                and endpoint.kind in {"merged_tag", "removed_tag"}
            )

        if relationship.kind == IDENTITY_ALIAS:
            if (
                not relationship.directed
                or len(relationship.endpoints) != 2
                or relationship.endpoints[0].kind != "alias_text"
                or (
                    relationship.endpoints[1].kind != "tag"
                    and not (
                        relationship.disabled
                        and relationship.endpoints[1].kind
                        in {"merged_tag", "removed_tag"}
                    )
                )
            ):
                issues.append(
                    RelationshipStoreIssue(
                        "invalid_alias",
                        relationship.id,
                        "Other Names must link alias text to one canonical Tag.",
                    )
                )
            elif not relationship.disabled and relationship.reviewed:
                alias=normalize_tag(
                    relationship.endpoints[0].id
                    or relationship.endpoints[0].label
                )
                target=relationship.endpoints[1].id
                previous=alias_targets.get(alias)
                if alias and previous is not None and previous!=target:
                    issues.append(RelationshipStoreIssue(
                        "alias_collision",relationship.id,
                        "One Other Name cannot identify two live SDXL Tags.",
                    ))
                elif alias:
                    alias_targets[alias]=target
        if relationship.kind in {SPECIFICITY, ENTAILMENT}:
            if (
                not relationship.directed
                or len(relationship.endpoints) < 2
                or any(not is_tag_endpoint(item) for item in relationship.endpoints)
            ):
                issues.append(
                    RelationshipStoreIssue(
                        "invalid_semantic_link",
                        relationship.id,
                        "Broader Tag and Also Means records must link one Tag to one or more Tag targets.",
                    )
                )
        if relationship.kind == ADVISORY_ASSOCIATION:
            if len(relationship.endpoints) != 2:
                issues.append(
                    RelationshipStoreIssue(
                        "invalid_advisory_association",
                        relationship.id,
                        "Usually Related must link exactly two reviewed endpoints.",
                    )
                )
        if relationship.kind == CATEGORY_BASE:
            if (
                not relationship.directed
                or len(relationship.endpoints) != 2
                or relationship.endpoints[0].kind != "category"
                or not is_tag_endpoint(relationship.endpoints[1])
            ):
                issues.append(
                    RelationshipStoreIssue(
                        "invalid_category_base",
                        relationship.id,
                        "A Category Base relationship must link one Category to one Tag.",
                    )
                )
            elif relationship.reviewed and not relationship.disabled:
                source_id = relationship.endpoints[0].id
                if source_id in category_base_sources:
                    issues.append(
                        RelationshipStoreIssue(
                            "duplicate_category_base",
                            relationship.id,
                            f"Category {source_id!r} has more than one active Category Base Tag.",
                        )
                    )
                category_base_sources.add(source_id)
        if relationship.kind == ENTITY_TAG:
            if (
                not relationship.directed
                or len(relationship.endpoints) != 2
                or relationship.endpoints[0].kind
                not in {"character_source", "character", "outfit"}
                or not is_tag_endpoint(relationship.endpoints[1])
            ):
                issues.append(
                    RelationshipStoreIssue(
                        "invalid_entity_tag",
                        relationship.id,
                        "An Entity Tag must link one Character Source, character, or outfit to one Tag.",
                    )
                )
        if relationship.kind == CHARACTER_SOURCE:
            if (
                not relationship.directed
                or len(relationship.endpoints) != 2
                or relationship.endpoints[0].kind not in {"character", "outfit"}
                or relationship.endpoints[1].kind != "character_source"
            ):
                issues.append(
                    RelationshipStoreIssue(
                        "invalid_character_source",
                        relationship.id,
                        "A Character Source relationship must link one character or outfit to one Character Source.",
                    )
                )
        if relationship.kind == ASSOCIATED_CHARACTER:
            if (
                not relationship.directed
                or len(relationship.endpoints) != 2
                or relationship.endpoints[0].kind != "outfit"
                or relationship.endpoints[1].kind != "character"
            ):
                issues.append(
                    RelationshipStoreIssue(
                        "invalid_associated_character",
                        relationship.id,
                        "Associated Characters must link one outfit to one character.",
                    )
                )
        if relationship.kind == OTHER_APPEARANCE:
            if (
                not relationship.directed
                or len(relationship.endpoints) != 2
                or relationship.endpoints[0].kind not in {"character", "outfit"}
                or relationship.endpoints[1].kind != "character_source"
            ):
                issues.append(
                    RelationshipStoreIssue(
                        "invalid_other_appearance",
                        relationship.id,
                        "Other Appearances must link one character or outfit to one additional Character Source.",
                    )
                )
        if relationship.kind == VERSION_OF:
            if (
                not relationship.directed
                or len(relationship.endpoints) != 2
                or any(item.kind != "character" for item in relationship.endpoints)
            ):
                issues.append(
                    RelationshipStoreIssue(
                        "invalid_version_of",
                        relationship.id,
                        "Version Of must link one alternate character to one base character.",
                    )
                )
        if relationship.kind == SPECIES_TRAIT:
            if (
                not relationship.directed
                or len(relationship.endpoints) != 2
                or not (
                    relationship.endpoints[0].kind == "character"
                    or is_tag_endpoint(relationship.endpoints[0])
                )
                or not (
                    relationship.endpoints[1].kind == "species_concept"
                    or is_tag_endpoint(relationship.endpoints[1])
                )
            ):
                issues.append(
                    RelationshipStoreIssue(
                        "invalid_species_trait",
                        relationship.id,
                        "Species / Traits must link one character or character-owned Tag to one Tag or species concept.",
                    )
                )
        if relationship.kind == MODIFIER_ROLE:
            details = dict(relationship.details)
            decision = details.get("decision", "")
            valid_shape = (
                relationship.directed
                and relationship.reviewed
                and len(relationship.endpoints) == 2
                and is_tag_endpoint(relationship.endpoints[0])
                and (
                    (
                        relationship.endpoints[1].kind == "category"
                        and decision in {"retain_modifier_role", "change_family"}
                    )
                    or (
                        relationship.endpoints[1].kind == MODIFIER_ROLE_STATE
                        and relationship.endpoints[1].id == ORDINARY_MODIFIER_ROLE
                        and decision == "keep_ordinary"
                    )
                )
            )
            if not valid_shape:
                issues.append(
                    RelationshipStoreIssue(
                        "invalid_modifier_role",
                        relationship.id,
                        "A Modifier Role decision must link one Tag to a reusable-family Category or the explicit Ordinary state.",
                    )
                )
            elif relationship.endpoints[0].id in modifier_role_tags:
                issues.append(
                    RelationshipStoreIssue(
                        "duplicate_modifier_role",
                        relationship.id,
                        f"Tag {relationship.endpoints[0].id!r} has more than one Modifier Role decision.",
                    )
                )
            else:
                modifier_role_tags.add(relationship.endpoints[0].id)
        if relationship.kind == MODIFIER_VALUE:
            details = dict(relationship.details)
            family_id = details.get("modifier_dimension_id", "")
            valid_target = (
                len(relationship.endpoints) == 2
                and (
                    is_tag_endpoint(relationship.endpoints[1])
                    or relationship.endpoints[1].kind == "modifier_value"
                )
            ) or (
                len(relationship.endpoints) == 2
                and relationship.endpoints[1].kind == MODIFIER_VALUE_STATE
                and relationship.endpoints[1].id == NOT_SET_MODIFIER_VALUE
                and bool(family_id)
            )
            if (
                not relationship.directed
                or not relationship.reviewed
                or len(relationship.endpoints) != 2
                or not is_tag_endpoint(relationship.endpoints[0])
                or not valid_target
            ):
                issues.append(
                    RelationshipStoreIssue(
                        "invalid_modifier_value",
                        relationship.id,
                        "A Modifier Value must link one composed Tag to one reviewed Tag/value or a family-scoped Not set state.",
                    )
                )
            elif family_id:
                slot = (relationship.endpoints[0].id, family_id)
                if slot in modifier_value_slots:
                    issues.append(
                        RelationshipStoreIssue(
                            "duplicate_modifier_value_slot",
                            relationship.id,
                            f"Tag {slot[0]!r} has more than one Modifier Value record for family {slot[1]!r}.",
                        )
                    )
                else:
                    modifier_value_slots.add(slot)
        if relationship.kind == ALLOWED_DIMENSION:
            source = relationship.endpoints[0]
            targets = relationship.endpoints[1:]
            known_targets = all(
                target.kind in {
                    "modifier_dimension",ALLOWED_DIMENSION_STATE,"category","tag",
                }
                for target in targets
            )
            valid_state = (
                len(targets) == 1
                and targets[0].kind == ALLOWED_DIMENSION_STATE
                and targets[0].id == NO_ALLOWED_DIMENSIONS
            )
            value_scope=str(dict(relationship.details).get("value_scope") or "")
            scoped_endpoints=targets[1:]
            scoped_keys=tuple(
                (target.kind,target.id) for target in scoped_endpoints
            )
            scoped_roles=tuple(
                str(target.extras.get("allowed_scope_role") or "selection")
                for target in scoped_endpoints
            )
            valid_scoped_values=(
                value_scope in {"only","all_except"}
                or (value_scope in {"all","unreviewed"} and not scoped_endpoints)
            ) and (
                len(scoped_keys)==len(set(scoped_keys))
                and all(role in {"selection","exception"} for role in scoped_roles)
            )
            valid_dimensions = bool(targets) and (
                (not value_scope and all(target.kind == "modifier_dimension" for target in targets))
                or (
                    targets[0].kind=="modifier_dimension"
                    and all(
                        target.kind in {"category","tag"}
                        for target in targets[1:]
                    )
                    and valid_scoped_values
                )
            )
            if (
                not relationship.directed
                or not relationship.reviewed
                or not is_tag_endpoint(source)
                or (known_targets and not (valid_state or valid_dimensions))
            ):
                issues.append(
                    RelationshipStoreIssue(
                        "invalid_allowed_dimension",
                        relationship.id,
                        "Allowed Details must link one Base Tag to reviewed modifier dimensions or the explicit no-details state.",
                    )
                )
            elif not relationship.disabled and known_targets:
                if valid_state:
                    if source.id in allowed_dimension_states:
                        issues.append(
                            RelationshipStoreIssue(
                                "duplicate_allowed_dimension_state",
                                relationship.id,
                                f"Tag {source.id!r} has more than one active explicit no-details record.",
                            )
                        )
                    allowed_dimension_states.add(source.id)
                else:
                    for target in targets:
                        if target.kind!="modifier_dimension":continue
                        slot = (source.id, target.id)
                        if slot in allowed_dimension_slots:
                            issues.append(
                                RelationshipStoreIssue(
                                    "duplicate_allowed_dimension_slot",
                                    relationship.id,
                                    f"Tag {source.id!r} has more than one active Allowed Details record for family {target.id!r}.",
                                )
                            )
                        allowed_dimension_slots.add(slot)
        if (
            relationship.kind in directed_edges
            and relationship.directed
            and relationship.reviewed
            and not relationship.disabled
        ):
            source = relationship.endpoints[0]
            for target in relationship.endpoints[1:]:
                if source.kind == target.kind == "tag":
                    directed_edges[relationship.kind].setdefault(source.id, set()).add(target.id)
                    semantic_pairs.add(tuple(sorted((source.id, target.id))))
        if relationship.kind == SCOPED_CONSTRAINT:
            from .conflict_policy import ConflictSubjectPolicy
            try:
                ConflictSubjectPolicy.from_payload(relationship.extras.get("subject_matching"))
            except ValueError as exc:
                issues.append(RelationshipStoreIssue(
                    "invalid_subject_matching", relationship.id, str(exc),
                ))
            invalid_constraint = relationship.directed or any(
                not is_tag_endpoint(endpoint) for endpoint in relationship.endpoints
            )
            if invalid_constraint:
                issues.append(
                    RelationshipStoreIssue(
                        "invalid_constraint",
                        relationship.id,
                        "Contrary Values must be one symmetric Tag relationship family.",
                    )
                )
            elif relationship.reviewed and not relationship.disabled:
                tag_ids = sorted(endpoint.id for endpoint in relationship.endpoints)
                for index, source in enumerate(tag_ids):
                    for target in tag_ids[index + 1 :]:
                        constraint_pairs.add((source, target))

    for pair in sorted(semantic_pairs.intersection(constraint_pairs)):
        issues.append(
            RelationshipStoreIssue(
                "semantic_constraint_overlap",
                "",
                f"Tags {pair[0]!r} and {pair[1]!r} cannot be both hierarchical and contrary.",
            )
        )

    for tag_id in sorted(allowed_dimension_states):
        if any(source_id == tag_id for source_id, _family_id in allowed_dimension_slots):
            issues.append(
                RelationshipStoreIssue(
                    "allowed_dimension_state_conflict",
                    "",
                    f"Tag {tag_id!r} cannot actively allow modifier dimensions and explicitly allow none at the same time.",
                )
            )

    for kind, edges in directed_edges.items():
        for root in sorted(edges):
            pending = list(edges.get(root, ()))
            visited: set[str] = set()
            while pending:
                current = pending.pop()
                if current == root:
                    issues.append(
                        RelationshipStoreIssue(
                            "cycle",
                            "",
                            f"The {kind} relationships contain a cycle through {root!r}.",
                        )
                    )
                    break
                if current in visited:
                    continue
                visited.add(current)
                pending.extend(edges.get(current, ()))
    return tuple(issues)


def plan_legacy_relationship_migration(
    registry: KnowledgeRelationshipRegistry,
    *,
    source_paths: Iterable[str | Path] = (),
    destination_layer: str = "user",
) -> RelationshipMigrationPlan:
    migrated = tuple(
        KnowledgeRelationship(
            id=item.id,
            kind=item.kind,
            endpoints=item.endpoints,
            directed=item.directed,
            scope=item.scope,
            label=item.label,
            reviewed=item.reviewed,
            source=item.source,
            details=item.details,
            version=item.version,
            layer=destination_layer,
            disabled=item.disabled,
            extras=dict(item.extras),
        )
        for item in registry.relationships
    )
    fingerprints = tuple(
        (str(Path(path).resolve()), _fingerprint(Path(path))) for path in source_paths
    )
    return RelationshipMigrationPlan(
        migrated,
        fingerprints,
        validate_relationships(migrated),
    )


def apply_relationship_migration(
    plan: RelationshipMigrationPlan,
    store: RelationshipStore,
    *,
    backup_root: str | Path | None = None,
) -> Path:
    if not plan.can_apply:
        raise ValueError("Resolve relationship migration issues before applying the plan.")
    for source_path, expected in plan.source_fingerprints:
        if _fingerprint(Path(source_path)) != expected:
            raise RuntimeError(
                "A relationship migration source changed after preview. Rebuild the plan."
            )
    store.replace(plan.relationships)
    return store.save_verified(backup_root=backup_root)


def load_effective_relationship_registry(
    *,
    tags: Any = None,
    default_path: str | Path | None = None,
    user_path: str | Path | None = None,
) -> KnowledgeRelationshipRegistry:
    """Overlay user records on reviewed defaults without mutating either layer."""
    if default_path is None or user_path is None:
        from .paths import DEFAULTS_DIR, RELATIONSHIPS_PATH

        default_path = default_path or DEFAULTS_DIR / "relationships.json"
        user_path = user_path or RELATIONSHIPS_PATH

    effective: dict[str, KnowledgeRelationship] = {}
    for index,path in enumerate((Path(default_path), Path(user_path))):
        store = RelationshipStore(path,cache_parsed=index==0)
        effective.update(store.relationships)
    relationships = tuple(
        effective[key] for key in sorted(effective)
    )
    if tags is not None:
        relationships = canonicalize_relationship_tag_endpoints(
            relationships, tags,
        )
    issues = validate_relationships(relationships)
    if issues:
        raise ValueError(" ".join(issue.message for issue in issues[:3]))
    return KnowledgeRelationshipRegistry(relationships, tags=tags)


def canonicalize_relationship_tag_endpoints(
    relationships: Iterable[KnowledgeRelationship],
    tags: Any,
) -> tuple[KnowledgeRelationship, ...]:
    """Resolve historical merged Tag IDs without rewriting relationship evidence."""
    result: list[KnowledgeRelationship] = []
    for relationship in relationships:
        endpoints: list[RelationshipEndpoint] = []
        changed = False
        for endpoint in relationship.endpoints:
            if endpoint.kind != "tag":
                endpoints.append(endpoint)
                continue
            resolved = tags.resolve_tag_id(endpoint.id)
            record = tags.tags.get(str(resolved or ""))
            if record is None or record.id == endpoint.id:
                endpoints.append(endpoint)
                continue
            endpoints.append(
                replace(endpoint, id=record.id, label=record.display)
            )
            changed = True
        if not changed:
            result.append(relationship)
            continue

        keys = [(endpoint.kind, endpoint.id) for endpoint in endpoints]
        collapsed = len(set(keys)) != len(keys)
        if collapsed:
            details = dict(relationship.details)
            details["runtime_redirect_state"] = "collapsed_after_identity_merge"
            result.append(
                replace(
                    relationship,
                    disabled=True,
                    details=tuple(details.items()),
                )
            )
            continue
        result.append(replace(relationship, endpoints=tuple(endpoints)))
    return tuple(result)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)
