from __future__ import annotations

from dataclasses import dataclass, field
import json
from hashlib import sha256
from typing import Any, Iterable

from .tag_collection import normalize_tag


IDENTITY_ALIAS = "identity_alias"
SPECIFICITY = "specificity"
ENTAILMENT = "entailment"
ADVISORY_ASSOCIATION = "advisory_association"
MODIFIER_VALUE = "modifier_value"
MODIFIER_VALUE_STATE = "modifier_value_state"
NOT_SET_MODIFIER_VALUE = "not_set"
MODIFIER_ROLE = "modifier_role"
MODIFIER_ROLE_STATE = "modifier_role_state"
ORDINARY_MODIFIER_ROLE = "ordinary"
ALLOWED_DIMENSION = "allowed_dimension"
ALLOWED_DIMENSION_STATE = "allowed_dimension_state"
NO_ALLOWED_DIMENSIONS = "none"
CATEGORY_BASE = "category_base"
SCOPED_CONSTRAINT = "scoped_constraint"
ENTITY_TAG = "entity_tag"
CHARACTER_SOURCE = "character_source"
ASSOCIATED_CHARACTER = "associated_character"
OTHER_APPEARANCE = "other_appearance"
VERSION_OF = "version_of"
SPECIES_TRAIT = "species_trait"


# Current relationship kinds are declared once so storage, Knowledge Packs,
# audits, and future compatibility work cannot silently disagree about the
# supported typed surface. Unknown records still round-trip through the generic
# store for forward compatibility; adding a newly supported kind requires an
# explicit entry here and an authority-matrix decision.
CURRENT_RELATIONSHIP_KINDS = frozenset(
    {
        IDENTITY_ALIAS,
        SPECIFICITY,
        ENTAILMENT,
        ADVISORY_ASSOCIATION,
        MODIFIER_ROLE,
        MODIFIER_VALUE,
        ALLOWED_DIMENSION,
        CATEGORY_BASE,
        SCOPED_CONSTRAINT,
        ENTITY_TAG,
        CHARACTER_SOURCE,
        ASSOCIATED_CHARACTER,
        OTHER_APPEARANCE,
        VERSION_OF,
        SPECIES_TRAIT,
    }
)

# These kinds have an old-reader field or compatibility view that must be
# reconstructed deliberately at portable/history boundaries. The remaining
# current kinds are canonical-only: they must never manufacture an inline
# projection merely because an older reader has no equivalent concept.
EXPLICIT_COMPATIBILITY_ADAPTER_KINDS = frozenset(
    {
        IDENTITY_ALIAS,
        SPECIFICITY,
        ENTAILMENT,
        MODIFIER_VALUE,
        ALLOWED_DIMENSION,
        CATEGORY_BASE,
        SCOPED_CONSTRAINT,
        ENTITY_TAG,
        CHARACTER_SOURCE,
        ASSOCIATED_CHARACTER,
        VERSION_OF,
    }
)
CANONICAL_ONLY_RELATIONSHIP_KINDS = (
    CURRENT_RELATIONSHIP_KINDS - EXPLICIT_COMPATIBILITY_ADAPTER_KINDS
)


@dataclass(frozen=True, order=True)
class RelationshipEndpoint:
    """One typed endpoint in the compatibility relationship view."""

    kind: str
    id: str
    label: str = field(default="", compare=False)
    extras: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.extras)
        payload.update({"kind": self.kind, "id": self.id})
        if self.label:
            payload["label"] = self.label
        else:
            payload.pop("label", None)
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RelationshipEndpoint":
        known = {"kind", "id", "label"}
        return cls(
            kind=str(value.get("kind") or ""),
            id=str(value.get("id") or ""),
            label=str(value.get("label") or ""),
            extras={key: item for key, item in value.items() if key not in known},
        )


@dataclass(frozen=True)
class KnowledgeRelationship:
    """A read-only typed view of one existing System Knowledge relationship."""

    id: str
    kind: str
    endpoints: tuple[RelationshipEndpoint, ...]
    directed: bool = True
    scope: str = "any"
    label: str = ""
    reviewed: bool = True
    source: str = "legacy"
    details: tuple[tuple[str, str], ...] = ()
    version: int = 1
    layer: str = "legacy"
    disabled: bool = False
    extras: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def source_endpoint(self) -> RelationshipEndpoint | None:
        return self.endpoints[0] if self.directed and self.endpoints else None

    @property
    def target_endpoints(self) -> tuple[RelationshipEndpoint, ...]:
        return self.endpoints[1:] if self.directed else self.endpoints

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.extras)
        payload.update(
            {
                "id": self.id,
                "version": self.version,
                "kind": self.kind,
                "endpoints": [endpoint.to_dict() for endpoint in self.endpoints],
                "directed": self.directed,
                "scope": self.scope,
                "reviewed": self.reviewed,
                "source": self.source,
                "layer": self.layer,
                "disabled": self.disabled,
            }
        )
        if self.label:
            payload["label"] = self.label
        else:
            payload.pop("label", None)
        if self.details:
            payload["details"] = dict(self.details)
        else:
            payload.pop("details", None)
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "KnowledgeRelationship":
        known = {
            "id",
            "version",
            "kind",
            "endpoints",
            "directed",
            "scope",
            "label",
            "reviewed",
            "source",
            "details",
            "layer",
            "disabled",
        }
        details = value.get("details") if isinstance(value.get("details"), dict) else {}
        return cls(
            id=str(value.get("id") or ""),
            version=max(1, int(value.get("version", 1) or 1)),
            kind=str(value.get("kind") or ""),
            endpoints=tuple(
                RelationshipEndpoint.from_dict(endpoint)
                for endpoint in value.get("endpoints", ())
                if isinstance(endpoint, dict)
            ),
            directed=bool(value.get("directed", True)),
            scope=str(value.get("scope") or "any"),
            label=str(value.get("label") or ""),
            reviewed=bool(value.get("reviewed", True)),
            source=str(value.get("source") or "legacy"),
            details=tuple((str(key), str(item)) for key, item in details.items()),
            layer=str(value.get("layer") or "legacy"),
            disabled=bool(value.get("disabled", False)),
            extras={key: item for key, item in value.items() if key not in known},
        )


def _relationship_id(
    kind: str,
    endpoints: Iterable[RelationshipEndpoint],
    *,
    source_key: str = "",
) -> str:
    parts = [kind, source_key]
    parts.extend(f"{endpoint.kind}:{endpoint.id}" for endpoint in endpoints)
    digest = sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:20]
    return f"legacy.{kind}.{digest}"


class KnowledgeRelationshipRegistry:
    """Indexed compatibility view over today's scattered relationship stores.

    The registry deliberately has no save method. It lets consumers move to one
    typed query boundary before any schema migration changes persisted data.
    ``build_tag_name_index=False`` is for relationship-only inventory consumers;
    fallback alias lookup is unavailable without the index. Query consumers
    retain the default complete name index.
    """

    def __init__(
        self,
        relationships: Iterable[KnowledgeRelationship] = (),
        *,
        tags: Any = None,
        build_tag_name_index: bool = True,
    ) -> None:
        by_id: dict[str, KnowledgeRelationship] = {}
        for relationship in relationships:
            previous = by_id.get(relationship.id)
            if previous is not None and previous != relationship:
                raise ValueError(
                    f"Relationship ID {relationship.id!r} identifies two different records."
                )
            by_id[relationship.id] = relationship
        self._relationships = tuple(by_id.values())
        self._tags = tags
        self._fingerprint_cache=""
        self._tag_names: dict[str, str] = {}
        if tags is not None and build_tag_name_index:
            for record in getattr(tags, "tags", {}).values():
                values=(
                    tags.identity_names(record)
                    if hasattr(tags,"identity_names")
                    else (record.display,*record.aliases)
                )
                for value in values:
                    key = normalize_tag(str(value))
                    if key:
                        self._tag_names.setdefault(key, record.id)
        for relationship in self._relationships:
            for endpoint in relationship.endpoints:
                if endpoint.kind != "tag":
                    continue
                for value in (endpoint.id, endpoint.label):
                    key = normalize_tag(str(value))
                    if key:
                        self._tag_names.setdefault(key, endpoint.id)
        self._by_endpoint: dict[tuple[str, str], list[KnowledgeRelationship]] = {}
        for relationship in self._relationships:
            for endpoint in relationship.endpoints:
                self._by_endpoint.setdefault((endpoint.kind, endpoint.id), []).append(
                    relationship
                )

    @property
    def relationships(self) -> tuple[KnowledgeRelationship, ...]:
        return self._relationships

    def fingerprint(self) -> str:
        """Identify the exact effective relationship contract deterministically."""
        if self._fingerprint_cache:
            return self._fingerprint_cache
        payload = [
            relationship.to_dict()
            for relationship in sorted(self._relationships, key=lambda item: item.id)
        ]
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self._fingerprint_cache=sha256(encoded).hexdigest()
        return self._fingerprint_cache

    def for_endpoint(self, kind: str, endpoint_id: str) -> tuple[KnowledgeRelationship, ...]:
        return tuple(self._by_endpoint.get((str(kind), str(endpoint_id)), ()))

    def resolve_tag_id(self, value: str) -> str | None:
        if self._tags is not None and value in getattr(self._tags, "tags", {}):
            return str(value)
        if self._tags is not None and hasattr(self._tags,"find"):
            record=self._tags.find(str(value))
            if record is not None:return str(record.id)
        return self._tag_names.get(normalize_tag(str(value)))

    def tag_display(self, tag_id: str) -> str:
        """Return the current preferred name for one stable Tag identity."""
        record = getattr(self._tags, "tags", {}).get(str(tag_id)) if self._tags is not None else None
        if record is not None and str(getattr(record, "display", "")).strip():
            return str(record.display)
        for relationship in self.for_endpoint("tag", str(tag_id)):
            for endpoint in relationship.endpoints:
                if endpoint.kind == "tag" and endpoint.id == str(tag_id) and endpoint.label:
                    return endpoint.label
        return str(tag_id)

    def for_tag(self, value: str) -> tuple[KnowledgeRelationship, ...]:
        tag_id = self.resolve_tag_id(value)
        return self.for_endpoint("tag", tag_id) if tag_id else ()

    def outgoing(
        self,
        endpoint: RelationshipEndpoint,
        *,
        kinds: Iterable[str] = (),
    ) -> tuple[KnowledgeRelationship, ...]:
        allowed = {str(kind) for kind in kinds}
        return tuple(
            relationship
            for relationship in self.for_endpoint(endpoint.kind, endpoint.id)
            if relationship.directed
            and relationship.source_endpoint == endpoint
            and (not allowed or relationship.kind in allowed)
        )

    @classmethod
    def from_legacy(
        cls,
        tags: Any,
        *,
        categories: Any = None,
        conflicts: Any = None,
        characters: Any = None,
        include_advisory: bool = False,
        kinds: Iterable[str] = (),
        build_tag_name_index: bool = True,
    ) -> "KnowledgeRelationshipRegistry":
        records: list[KnowledgeRelationship] = []
        allowed_kinds = {str(kind) for kind in kinds if str(kind)}

        def wanted(kind: str) -> bool:
            return not allowed_kinds or kind in allowed_kinds

        def tag_endpoint(tag_id: str) -> RelationshipEndpoint:
            raw_id = str(tag_id)
            record = tags.tags.get(raw_id)
            if record is None and hasattr(tags, "find"):
                record = tags.find(raw_id)
            return RelationshipEndpoint(
                "tag",
                str(getattr(record, "id", "") or raw_id),
                str(getattr(record, "display", "") or raw_id),
            )

        def known_tag(value: str) -> RelationshipEndpoint:
            record = tags.find(str(value))
            if record is not None:
                return tag_endpoint(record.id)
            return RelationshipEndpoint("unresolved_tag", normalize_tag(str(value)), str(value))

        def append(
            kind: str,
            endpoints: Iterable[RelationshipEndpoint],
            *,
            directed: bool = True,
            scope: str = "any",
            label: str = "",
            reviewed: bool = True,
            source: str,
            source_key: str = "",
            details: Iterable[tuple[str, str]] = (),
        ) -> None:
            if not wanted(kind):
                return
            endpoint_tuple = tuple(endpoints)
            if not directed:
                endpoint_tuple = tuple(sorted(endpoint_tuple))
            records.append(
                KnowledgeRelationship(
                    _relationship_id(kind, endpoint_tuple, source_key=source_key),
                    kind,
                    endpoint_tuple,
                    directed=directed,
                    scope=scope,
                    label=label,
                    reviewed=reviewed,
                    source=source,
                    details=tuple((str(key), str(value)) for key, value in details),
                    layer="legacy",
                )
            )

        inline_constraint_keys: set[tuple[str, str]] = set()
        for record in sorted(tags.tags.values(), key=lambda item: item.id):
            current = tag_endpoint(record.id)
            if wanted(IDENTITY_ALIAS):
                alias_keys: set[str] = set()
                for alias in record.aliases:
                    alias_key = normalize_tag(alias)
                    if (
                        not alias_key
                        or alias_key == normalize_tag(record.display)
                        or alias_key in alias_keys
                    ):
                        continue
                    alias_keys.add(alias_key)
                    append(
                        IDENTITY_ALIAS,
                        (
                            RelationshipEndpoint("alias_text", alias_key, alias),
                            current,
                        ),
                        source="tag.aliases",
                        source_key=f"{record.id}:{alias_key}",
                    )
            if wanted(SPECIFICITY) and record.parent_id:
                append(
                    SPECIFICITY,
                    (current, tag_endpoint(record.parent_id)),
                    source="tag.relations.parent",
                )
            if wanted(ENTAILMENT):
                for target_id in record.implies:
                    append(
                        ENTAILMENT,
                        (current, tag_endpoint(target_id)),
                        source="tag.relations.implies",
                    )
            if wanted(MODIFIER_VALUE):
                for value_id in record.attributes:
                    endpoint = (
                        tag_endpoint(value_id)
                        if value_id in tags.tags
                        else RelationshipEndpoint("modifier_value", str(value_id), str(value_id))
                    )
                    append(
                        MODIFIER_VALUE,
                        (current, endpoint),
                        source="tag.relations.attributes",
                    )
            if wanted(ALLOWED_DIMENSION) and record.allowed_modifier_groups is not None:
                if record.allowed_modifier_groups:
                    for family_id in record.allowed_modifier_groups:
                        append(
                            ALLOWED_DIMENSION,
                            (
                                current,
                                RelationshipEndpoint(
                                    "modifier_dimension",
                                    str(family_id),
                                    str(family_id).replace("_", " ").title(),
                                ),
                            ),
                            source="tag.relations.allowed_modifier_groups",
                            source_key=f"{record.id}:{family_id}",
                        )
                else:
                    append(
                        ALLOWED_DIMENSION,
                        (
                            current,
                            RelationshipEndpoint(
                                ALLOWED_DIMENSION_STATE,
                                NO_ALLOWED_DIMENSIONS,
                                "No allowed details",
                            ),
                        ),
                        source="tag.relations.allowed_modifier_groups",
                        source_key=f"{record.id}:none",
                        details=(("policy_mode", "explicit_none"),),
                    )
            if wanted(SCOPED_CONSTRAINT):
                for target_id in record.conflicts:
                    key = tuple(sorted((record.id, str(target_id))))
                    if key in inline_constraint_keys:
                        continue
                    inline_constraint_keys.add(key)
                    append(
                        SCOPED_CONSTRAINT,
                        (current, tag_endpoint(target_id)),
                        directed=False,
                        scope="legacy_unspecified",
                        label="Conflicting Tags",
                        source="tag.relations.conflicts",
                        source_key=":".join(key),
                    )

        if categories is not None and wanted(CATEGORY_BASE):
            for category in sorted(
                getattr(categories, "categories", {}).values(), key=lambda item: item.id
            ):
                base_tag_id = str(getattr(category, "base_tag_id", "") or "")
                if not base_tag_id:
                    continue
                append(
                    CATEGORY_BASE,
                    (
                        RelationshipEndpoint("category", category.id, category.name),
                        tag_endpoint(base_tag_id),
                    ),
                    scope="organization",
                    source="category.base_tag_id",
                    details=(("policy", getattr(category, "base_tag_policy", "")),),
                )

        if conflicts is not None and wanted(SCOPED_CONSTRAINT):
            for rule in getattr(conflicts, "rules", ()):
                # A schema-v3 shipped rule is a stable presentation shim over
                # canonical authority, not a remaining legacy member
                # projection to inventory or migrate.
                if not getattr(rule, "tags", ()):
                    continue
                append(
                    SCOPED_CONSTRAINT,
                    tuple(known_tag(value) for value in rule.tags),
                    directed=False,
                    scope=str(rule.scope),
                    label=str(rule.label),
                    source="conflict_rule",
                    source_key=str(rule.id),
                    details=(
                        ("legacy_rule_id", rule.id),
                        ("canonical_relationship_id", getattr(rule, "relationship_id", "")),
                        ("relation", rule.relation),
                        ("severity", rule.severity),
                    ),
                )

        if characters is not None and any(
            wanted(kind)
            for kind in (
                ENTITY_TAG,
                CHARACTER_SOURCE,
                ASSOCIATED_CHARACTER,
                VERSION_OF,
                SPECIES_TRAIT,
                ADVISORY_ASSOCIATION,
            )
        ):
            entity_groups = (
                ("character_source", getattr(characters, "series", {})),
                ("character", getattr(characters, "characters", {})),
                ("outfit", getattr(characters, "outfits", {})),
            )
            for endpoint_kind, values in entity_groups:
                for entity in sorted(values.values(), key=lambda item: item.id):
                    entity_endpoint = RelationshipEndpoint(
                        endpoint_kind, entity.id, entity.name
                    )
                    link = getattr(entity, "knowledge_tag", {}) or {}
                    tag_id = str(link.get("tag_id") or "")
                    if wanted(ENTITY_TAG) and link.get("status") == "reviewed" and tag_id:
                        append(
                            ENTITY_TAG,
                            (entity_endpoint, tag_endpoint(tag_id)),
                            source=f"{endpoint_kind}.knowledge_tag",
                            details=(("status", "reviewed"),),
                        )

            for character in sorted(
                getattr(characters, "characters", {}).values(), key=lambda item: item.id
            ):
                source_id = str(getattr(character, "series_id", "") or "")
                if wanted(CHARACTER_SOURCE) and source_id:
                    source_record = getattr(characters, "series", {}).get(source_id)
                    append(
                        CHARACTER_SOURCE,
                        (
                            RelationshipEndpoint("character", character.id, character.name),
                            RelationshipEndpoint(
                                "character_source",
                                source_id,
                                getattr(source_record, "name", source_id),
                            ),
                        ),
                        source="character.series_id",
                    )
                base_character_id = str(
                    getattr(character, "base_character_id", "") or ""
                )
                if wanted(VERSION_OF) and base_character_id:
                    base = getattr(characters, "characters", {}).get(base_character_id)
                    append(
                        VERSION_OF,
                        (
                            RelationshipEndpoint("character", character.id, character.name),
                            RelationshipEndpoint(
                                "character",
                                base_character_id,
                                getattr(base, "name", base_character_id),
                            ),
                        ),
                        source="character.base_character_id",
                        details=((
                            "variant_type",
                            str(getattr(character, "variant_type", "") or ""),
                        ),),
                    )
                if wanted(SPECIES_TRAIT):
                    for index, association in enumerate(character.tag_associations):
                        relationship_kind = str(
                            association.get("relationship_kind")
                            or association.get("kind")
                            or ""
                        )
                        if (
                            relationship_kind != SPECIES_TRAIT
                            or str(association.get("status") or "") != "reviewed"
                        ):
                            continue
                        target = known_tag(str(association.get("tag") or ""))
                        if target.kind != "tag":
                            continue
                        append(
                            SPECIES_TRAIT,
                            (
                                RelationshipEndpoint("character", character.id, character.name),
                                target,
                            ),
                            scope="same_character",
                            source="character.tag_associations",
                            source_key=f"{character.id}:{index}:{target.id}",
                            details=tuple(
                                (key, value)
                                for key, value in association.items()
                                if key != "tag"
                            ),
                        )
                if wanted(ADVISORY_ASSOCIATION) and include_advisory:
                    for index, association in enumerate(character.tag_associations):
                        tag_name = str(association.get("tag") or "")
                        if not tag_name:
                            continue
                        status = str(association.get("status") or "observed")
                        append(
                            ADVISORY_ASSOCIATION,
                            (
                                RelationshipEndpoint("character", character.id, character.name),
                                known_tag(tag_name),
                            ),
                            source="character.tag_associations",
                            source_key=f"{character.id}:{index}:{normalize_tag(tag_name)}",
                            reviewed=status == "reviewed",
                            details=tuple(
                                (key, value)
                                for key, value in association.items()
                                if key != "tag"
                            ),
                        )

            for outfit in sorted(
                getattr(characters, "outfits", {}).values(), key=lambda item: item.id
            ):
                source_id = str(getattr(outfit, "series_id", "") or "")
                if wanted(CHARACTER_SOURCE) and source_id:
                    source_record = getattr(characters, "series", {}).get(source_id)
                    append(
                        CHARACTER_SOURCE,
                        (
                            RelationshipEndpoint("outfit", outfit.id, outfit.name),
                            RelationshipEndpoint(
                                "character_source",
                                source_id,
                                getattr(source_record, "name", source_id),
                            ),
                        ),
                        source="outfit.series_id",
                    )
                if wanted(ASSOCIATED_CHARACTER):
                    for character_id in outfit.character_ids:
                        character = getattr(characters, "characters", {}).get(character_id)
                        append(
                            ASSOCIATED_CHARACTER,
                            (
                                RelationshipEndpoint("outfit", outfit.id, outfit.name),
                                RelationshipEndpoint(
                                    "character",
                                    character_id,
                                    getattr(character, "name", character_id),
                                ),
                            ),
                            source="outfit.character_ids",
                        )

        return cls(records, tags=tags, build_tag_name_index=build_tag_name_index)
