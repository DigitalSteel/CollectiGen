from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .character_relationships import (
    COMPATIBILITY_PROJECTIONS,
    character_relationship_id,
    character_relationship_index,
    effective_character_relationship,
)
from .entity_tag_relationships import (
    ENDPOINT_KIND_TO_PROJECTION,
    entity_tag_relationship_id,
    entity_tag_relationship_index,
    effective_entity_tag_link,
)
from .knowledge_relationships import (
    ASSOCIATED_CHARACTER,
    CHARACTER_SOURCE,
    ENTITY_TAG,
    VERSION_OF,
    KnowledgeRelationship,
    RelationshipEndpoint,
)


REVIEWED_CHARACTER_PROJECTION_RELOCATION = (
    "reviewed_character_projection_relocation_v1"
)
CHARACTER_PROJECTION_SOURCE = (
    "CollectiGen reviewed Character Knowledge projection relocation"
)
SERIES_STORAGE_SCHEMA_VERSION = 4
CHARACTER_STORAGE_SCHEMA_VERSION = 6
OUTFIT_STORAGE_SCHEMA_VERSION = 4

_RECORD_ENDPOINT_KINDS = {
    "series": "character_source",
    "character": "character",
    "outfit": "outfit",
}


@dataclass(frozen=True)
class DefaultCharacterProjectionCompilation:
    relationships: tuple[KnowledgeRelationship, ...]
    managed_existing_ids: frozenset[str]
    entity_tag_count: int
    character_source_count: int
    associated_character_count: int
    version_of_count: int


def _records(payload: Mapping[str, Any], key: str) -> tuple[dict[str, Any], ...]:
    values = payload.get(key, ())
    if not isinstance(values, list):
        raise ValueError(f"Shipped {key} must be one JSON list.")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise ValueError(f"Shipped {key} record {index + 1} must be one object.")
        stable_id = str(value.get("id") or "").strip()
        name = str(value.get("name") or "").strip()
        if not stable_id or not name:
            raise ValueError(f"Shipped {key} record {index + 1} needs an ID and name.")
        if stable_id in seen:
            raise ValueError(f"Duplicate shipped {key} ID: {stable_id}")
        seen.add(stable_id)
        result.append(copy.deepcopy(value))
    return tuple(result)


def _relocation_record(relationship: KnowledgeRelationship) -> bool:
    return (
        dict(relationship.details).get("authority_relocation")
        == REVIEWED_CHARACTER_PROJECTION_RELOCATION
    )


def reviewed_default_character_projection_relationships(
    relationships: Iterable[KnowledgeRelationship],
) -> tuple[KnowledgeRelationship, ...]:
    """Return independently managed records created by the reviewed relocation."""

    result = tuple(
        relationship
        for relationship in relationships
        if _relocation_record(relationship)
    )
    invalid = [
        relationship.id
        for relationship in result
        if not relationship.reviewed
        or relationship.disabled
        or relationship.layer != "default"
        or relationship.kind
        not in {ENTITY_TAG, CHARACTER_SOURCE, ASSOCIATED_CHARACTER, VERSION_OF}
    ]
    if invalid:
        raise ValueError(
            "Reviewed Character projection authority contains inactive or invalid "
            "records: " + ", ".join(sorted(invalid)[:10])
        )
    return tuple(sorted(result, key=lambda item: item.id))


def _relationship(
    relationship_id: str,
    kind: str,
    source_kind: str,
    source_id: str,
    source_label: str,
    target_kind: str,
    target_id: str,
    target_label: str,
    projection: str,
    *,
    details: Mapping[str, Any] | None = None,
    extras: Mapping[str, Any] | None = None,
) -> KnowledgeRelationship:
    relationship_details = {
        "authority_relocation": REVIEWED_CHARACTER_PROJECTION_RELOCATION,
        "compatibility_projection": projection,
    }
    relationship_details.update(
        (str(key), str(value)) for key, value in dict(details or {}).items()
        if str(value)
    )
    return KnowledgeRelationship(
        id=relationship_id,
        kind=kind,
        endpoints=(
            RelationshipEndpoint(source_kind, source_id, source_label),
            RelationshipEndpoint(target_kind, target_id, target_label),
        ),
        directed=True,
        scope="any",
        label=kind.replace("_", " ").title(),
        reviewed=True,
        source=CHARACTER_PROJECTION_SOURCE,
        details=tuple(relationship_details.items()),
        version=1,
        layer="default",
        disabled=False,
        extras=copy.deepcopy(dict(extras or {})),
    )


def _active_targets(
    kind: str,
    source_kind: str,
    source_id: str,
    index: Mapping[tuple[str, str, str], Iterable[KnowledgeRelationship]],
) -> tuple[str, ...]:
    return effective_character_relationship(
        kind, source_kind, source_id, (), index,
    ).target_ids


def compile_default_character_projection_relationships(
    series_payload: Mapping[str, Any],
    characters_payload: Mapping[str, Any],
    outfits_payload: Mapping[str, Any],
    *,
    tag_labels: Mapping[str, str],
    existing_relationships: Iterable[KnowledgeRelationship] = (),
) -> DefaultCharacterProjectionCompilation:
    """Relocate authoritative shipped fields to stable canonical relationships.

    Pre-retirement schema payloads are compiler source, including explicit empty
    values that retire a formerly projected relationship. Current schemas keep
    the canonical records independently managed and treat any reappearing field
    as compatibility input rather than silently replacing reviewed authority.
    """

    series = _records(series_payload, "series")
    characters = _records(characters_payload, "characters")
    outfits = _records(outfits_payload, "outfits")
    series_by_id = {item["id"]: item for item in series}
    characters_by_id = {item["id"]: item for item in characters}
    existing = tuple(existing_relationships)
    character_index = character_relationship_index(existing)
    entity_index = entity_tag_relationship_index(existing)
    relocated = reviewed_default_character_projection_relationships(existing)
    managed_ids: set[str] = set()
    desired: list[KnowledgeRelationship] = []

    old_series = int(series_payload.get("schema_version", 1) or 1) < (
        SERIES_STORAGE_SCHEMA_VERSION
    )
    old_characters = int(characters_payload.get("schema_version", 1) or 1) < (
        CHARACTER_STORAGE_SCHEMA_VERSION
    )
    old_outfits = int(outfits_payload.get("schema_version", 1) or 1) < (
        OUTFIT_STORAGE_SCHEMA_VERSION
    )

    def manage(kind: str, source_kind: str, source_id: str) -> None:
        managed_ids.update(
            relationship.id
            for relationship in relocated
            if relationship.kind == kind
            and relationship.source_endpoint is not None
            and relationship.source_endpoint.kind == source_kind
            and relationship.source_endpoint.id == source_id
        )

    def add_entity(record_kind: str, record: Mapping[str, Any], *, managed: bool) -> None:
        source_kind = _RECORD_ENDPOINT_KINDS[record_kind]
        source_id = str(record["id"])
        if managed:
            manage(ENTITY_TAG, source_kind, source_id)
        link = record.get("knowledge_tag")
        if not isinstance(link, dict) or not link:
            return
        if str(link.get("status") or "") != "reviewed":
            raise ValueError(
                f"Shipped {record_kind} {source_id} has a non-reviewed knowledge_tag."
            )
        tag_id = str(link.get("tag_id") or "").strip()
        if not tag_id or tag_id not in tag_labels:
            raise ValueError(
                f"Shipped {record_kind} {source_id} references missing Tag {tag_id!r}."
            )
        resolved = effective_entity_tag_link(
            source_kind, source_id, {}, entity_index,
        )
        if resolved.represented and resolved.tag_id != tag_id:
            raise ValueError(
                f"Shipped {record_kind} {source_id} knowledge_tag disagrees with "
                "existing canonical authority."
            )
        relationship_id = entity_tag_relationship_id(source_kind, source_id)
        current = next(
            (item for item in relocated if item.id == relationship_id), None,
        )
        details = {
            key: link[key]
            for key in ("basis", "reviewed_at")
            if str(link.get(key) or "")
        }
        built = _relationship(
            relationship_id,
            ENTITY_TAG,
            source_kind,
            source_id,
            str(record.get("name") or source_id),
            "tag",
            tag_id,
            str(tag_labels[tag_id]),
            ENDPOINT_KIND_TO_PROJECTION[source_kind],
            details=details,
            extras={"compatibility_projection_payload": copy.deepcopy(link)},
        )
        if current is not None:
            current_payload = current.to_dict()
            built_payload = built.to_dict()
            current_payload.pop("version", None)
            built_payload.pop("version", None)
            built = KnowledgeRelationship(
                **{
                    **built.__dict__,
                    "version": current.version + (current_payload != built_payload),
                }
            )
        desired.append(built)

    for record in series:
        add_entity("series", record, managed=old_series or "knowledge_tag" in record)
    for record in characters:
        add_entity(
            "character", record, managed=old_characters or "knowledge_tag" in record,
        )
    for record in outfits:
        add_entity("outfit", record, managed=old_outfits or "knowledge_tag" in record)

    def add_character_source(
        source_kind: str, record: Mapping[str, Any], *, managed: bool,
    ) -> None:
        source_id = str(record["id"])
        if managed:
            manage(CHARACTER_SOURCE, source_kind, source_id)
        target_id = str(record.get("series_id") or "").strip()
        if not target_id:
            return
        if target_id not in series_by_id:
            raise ValueError(
                f"Shipped {source_kind} {source_id} references missing Character "
                f"Source {target_id}."
            )
        represented = _active_targets(
            CHARACTER_SOURCE, source_kind, source_id, character_index,
        )
        if represented and represented != (target_id,):
            raise ValueError(
                f"Shipped {source_kind} {source_id} series_id disagrees with "
                "existing canonical authority."
            )
        desired.append(_relationship(
            character_relationship_id(
                CHARACTER_SOURCE, source_kind, source_id, target_id,
            ),
            CHARACTER_SOURCE,
            source_kind,
            source_id,
            str(record.get("name") or source_id),
            "character_source",
            target_id,
            str(series_by_id[target_id].get("name") or target_id),
            COMPATIBILITY_PROJECTIONS[CHARACTER_SOURCE][source_kind],
        ))

    for record in characters:
        add_character_source(
            "character", record,
            managed=old_characters or "series_id" in record,
        )
    for record in outfits:
        add_character_source(
            "outfit", record, managed=old_outfits or "series_id" in record,
        )

    for record in outfits:
        source_id = str(record["id"])
        owns_projection = old_outfits or "character_ids" in record
        if owns_projection:
            manage(ASSOCIATED_CHARACTER, "outfit", source_id)
        else:
            continue
        target_ids = tuple(dict.fromkeys(
            str(value).strip()
            for value in record.get("character_ids", ())
            if str(value).strip()
        ))
        missing = sorted(set(target_ids) - set(characters_by_id))
        if missing:
            raise ValueError(
                f"Shipped outfit {source_id} references missing character {missing[0]}."
            )
        represented = _active_targets(
            ASSOCIATED_CHARACTER, "outfit", source_id, character_index,
        )
        if represented and set(represented) != set(target_ids):
            raise ValueError(
                f"Shipped outfit {source_id} character_ids disagree with existing "
                "canonical authority."
            )
        for order, target_id in enumerate(target_ids):
            desired.append(_relationship(
                character_relationship_id(
                    ASSOCIATED_CHARACTER, "outfit", source_id, target_id,
                ),
                ASSOCIATED_CHARACTER,
                "outfit",
                source_id,
                str(record.get("name") or source_id),
                "character",
                target_id,
                str(characters_by_id[target_id].get("name") or target_id),
                COMPATIBILITY_PROJECTIONS[ASSOCIATED_CHARACTER]["outfit"],
                details={"compatibility_order": order},
            ))

    for record in characters:
        source_id = str(record["id"])
        if old_characters or "base_character_id" in record or "variant_type" in record:
            manage(VERSION_OF, "character", source_id)
        target_id = str(record.get("base_character_id") or "").strip()
        variant_type = str(record.get("variant_type") or "").strip()
        if bool(target_id) != bool(variant_type):
            raise ValueError(
                f"Shipped character {source_id} needs both base_character_id and "
                "variant_type."
            )
        if not target_id:
            continue
        if target_id not in characters_by_id:
            raise ValueError(
                f"Shipped character {source_id} references missing base character "
                f"{target_id}."
            )
        represented = _active_targets(
            VERSION_OF, "character", source_id, character_index,
        )
        if represented and represented != (target_id,):
            raise ValueError(
                f"Shipped character {source_id} base_character_id disagrees with "
                "existing canonical authority."
            )
        desired.append(_relationship(
            character_relationship_id(VERSION_OF, "character", source_id, target_id),
            VERSION_OF,
            "character",
            source_id,
            str(record.get("name") or source_id),
            "character",
            target_id,
            str(characters_by_id[target_id].get("name") or target_id),
            COMPATIBILITY_PROJECTIONS[VERSION_OF]["character"],
            details={"variant_type": variant_type},
        ))

    desired_by_id: dict[str, KnowledgeRelationship] = {}
    for relationship in desired:
        previous = desired_by_id.get(relationship.id)
        if previous is not None and previous != relationship:
            raise ValueError(
                f"Character projection compiler produced duplicate ID {relationship.id}."
            )
        desired_by_id[relationship.id] = relationship
    values = tuple(sorted(desired_by_id.values(), key=lambda item: item.id))
    return DefaultCharacterProjectionCompilation(
        relationships=values,
        managed_existing_ids=frozenset(managed_ids),
        entity_tag_count=sum(item.kind == ENTITY_TAG for item in values),
        character_source_count=sum(item.kind == CHARACTER_SOURCE for item in values),
        associated_character_count=sum(
            item.kind == ASSOCIATED_CHARACTER for item in values
        ),
        version_of_count=sum(item.kind == VERSION_OF for item in values),
    )


def _entity_projection_payload(
    source_kind: str,
    source_id: str,
    relationships: Iterable[KnowledgeRelationship],
    *,
    relationship_index: Mapping[
        tuple[str, str], Iterable[KnowledgeRelationship]
    ] | None = None,
) -> dict[str, Any]:
    index = (
        relationship_index
        if relationship_index is not None
        else entity_tag_relationship_index(relationships)
    )
    resolved = effective_entity_tag_link(source_kind, source_id, {}, index)
    if not resolved.represented or not resolved.tag_id or resolved.conflicting:
        return {}
    relationship = resolved.active_tag_ids and next(
        (
            item for item in index.get((source_kind, source_id), ())
            if item.reviewed
            and not item.disabled
            and any(
                endpoint.kind == "tag" and endpoint.id == resolved.tag_id
                for endpoint in item.target_endpoints
            )
        ),
        None,
    )
    has_projection_payload = (
        relationship is not None
        and isinstance(
            relationship.extras.get("compatibility_projection_payload"), dict,
        )
    )
    payload = (
        copy.deepcopy(
            relationship.extras.get("compatibility_projection_payload")
        )
        if has_projection_payload
        else {}
    )
    payload.update({"tag_id": resolved.tag_id, "status": "reviewed"})
    if relationship is not None:
        details = dict(relationship.details)
        if details.get("basis"):
            payload["basis"] = details["basis"]
        if details.get("reviewed_at"):
            payload["reviewed_at"] = details["reviewed_at"]
        if not has_projection_payload:
            payload.setdefault("source", relationship.source)
    return payload


def character_record_compatibility_payload(
    record_kind: str,
    record: Mapping[str, Any],
    relationships: Iterable[KnowledgeRelationship],
    *,
    _entity_index: Mapping[
        tuple[str, str], Iterable[KnowledgeRelationship]
    ] | None = None,
    _character_index: Mapping[
        tuple[str, str, str], Iterable[KnowledgeRelationship]
    ] | None = None,
) -> dict[str, Any]:
    """Materialize the explicit old-reader shape for one Character record."""

    result = copy.deepcopy(dict(record))
    all_relationships = tuple(relationships)
    source_kind = _RECORD_ENDPOINT_KINDS[record_kind]
    source_id = str(result.get("id") or "")
    entity_index = (
        _entity_index
        if _entity_index is not None
        else entity_tag_relationship_index(all_relationships)
    )
    entity = _entity_projection_payload(
        source_kind,
        source_id,
        all_relationships,
        relationship_index=entity_index,
    )
    entity_records = entity_index.get(
        (source_kind, source_id), ()
    )
    if entity_records:
        if entity:
            result["knowledge_tag"] = entity
        else:
            result.pop("knowledge_tag", None)
    index = (
        _character_index
        if _character_index is not None
        else character_relationship_index(all_relationships)
    )
    if record_kind in {"character", "outfit"}:
        source = effective_character_relationship(
            CHARACTER_SOURCE, source_kind, source_id, (), index,
        )
        if source.represented:
            if source.target_id:
                result["series_id"] = source.target_id
            else:
                result.pop("series_id", None)
        result.setdefault("series_id", None)
    if record_kind == "outfit":
        owners = effective_character_relationship(
            ASSOCIATED_CHARACTER, "outfit", source_id, (), index,
        )
        if owners.represented:
            ordered = sorted(
                owners.active_relationships,
                key=lambda item: (
                    int(dict(item.details).get("compatibility_order", 1_000_000)),
                    item.id,
                ),
            )
            result["character_ids"] = [
                endpoint.id
                for relationship in ordered
                for endpoint in relationship.target_endpoints
                if endpoint.kind == "character"
            ]
        result.setdefault("character_ids", [])
    if record_kind == "character":
        version = effective_character_relationship(
            VERSION_OF, "character", source_id, (), index,
        )
        if version.represented:
            if version.target_id:
                result["base_character_id"] = version.target_id
                variant_type = version.details.get("variant_type", "")
                if variant_type:
                    result["variant_type"] = variant_type
                else:
                    result.pop("variant_type", None)
            else:
                result.pop("base_character_id", None)
                result.pop("variant_type", None)
    return result


def character_record_compatibility_payloads(
    record_kind: str,
    records: Iterable[Mapping[str, Any]],
    relationships: Iterable[KnowledgeRelationship],
) -> list[dict[str, Any]]:
    """Materialize one collection while building relationship indexes once."""

    all_relationships = tuple(relationships)
    entity_index = entity_tag_relationship_index(all_relationships)
    character_index = character_relationship_index(all_relationships)
    return [
        character_record_compatibility_payload(
            record_kind,
            record,
            all_relationships,
            _entity_index=entity_index,
            _character_index=character_index,
        )
        for record in records
    ]


def character_record_storage_payload(
    record_kind: str,
    record: Mapping[str, Any],
    default_relationships: Iterable[KnowledgeRelationship],
    user_relationships: Iterable[KnowledgeRelationship] = (),
    *,
    projection_fields: Iterable[str] = (),
    _default_entity_index: Mapping[
        tuple[str, str], Iterable[KnowledgeRelationship]
    ] | None = None,
    _user_entity_index: Mapping[
        tuple[str, str], Iterable[KnowledgeRelationship]
    ] | None = None,
    _default_character_index: Mapping[
        tuple[str, str, str], Iterable[KnowledgeRelationship]
    ] | None = None,
    _user_character_index: Mapping[
        tuple[str, str, str], Iterable[KnowledgeRelationship]
    ] | None = None,
) -> dict[str, Any]:
    """Serialize one live record without represented mutable projections.

    A canonical user record always owns the field. A canonical default owns a
    matching or absent field, while a differing explicitly stored legacy value
    remains intact as a local compatibility override until a reviewed writer
    promotes it. Unrepresented nonempty or future values are preserved.
    """

    result = copy.deepcopy(dict(record))
    defaults = tuple(default_relationships)
    users = tuple(user_relationships)
    fields = {str(value) for value in projection_fields}
    source_kind = _RECORD_ENDPOINT_KINDS[record_kind]
    source_id = str(result.get("id") or "")
    default_entity_index = (
        _default_entity_index
        if _default_entity_index is not None
        else entity_tag_relationship_index(defaults)
    )
    user_entity_index = (
        _user_entity_index
        if _user_entity_index is not None
        else entity_tag_relationship_index(users)
    )
    default_character_index = (
        _default_character_index
        if _default_character_index is not None
        else character_relationship_index(defaults)
    )
    user_character_index = (
        _user_character_index
        if _user_character_index is not None
        else character_relationship_index(users)
    )
    default_compatibility = character_record_compatibility_payload(
        record_kind,
        {"id": source_id},
        defaults,
        _entity_index=default_entity_index,
        _character_index=default_character_index,
    )
    default_entity = default_entity_index.get(
        (source_kind, source_id), ()
    )
    user_entity = user_entity_index.get(
        (source_kind, source_id), ()
    )
    if user_entity:
        result.pop("knowledge_tag", None)
    elif default_entity:
        current = result.get("knowledge_tag")
        expected = default_compatibility.get("knowledge_tag")
        explicit = "knowledge_tag" in fields or bool(current)
        if not explicit or current == expected:
            result.pop("knowledge_tag", None)
    elif not result.get("knowledge_tag"):
        result.pop("knowledge_tag", None)

    default_index = default_character_index
    user_index = user_character_index

    def represented(
        index: Mapping[tuple[str, str, str], Iterable[KnowledgeRelationship]],
        kind: str,
    ) -> bool:
        return bool(index.get((kind, source_kind, source_id), ()))

    if record_kind in {"character", "outfit"}:
        current = result.get("series_id")
        expected = default_compatibility.get("series_id")
        if represented(user_index, CHARACTER_SOURCE):
            result.pop("series_id", None)
        elif represented(default_index, CHARACTER_SOURCE):
            explicit = "series_id" in fields or current not in (None, "")
            if not explicit or current == expected:
                result.pop("series_id", None)
        elif current in (None, ""):
            result.pop("series_id", None)
    if record_kind == "outfit":
        current = result.get("character_ids")
        expected = default_compatibility.get("character_ids")
        if represented(user_index, ASSOCIATED_CHARACTER):
            result.pop("character_ids", None)
        elif represented(default_index, ASSOCIATED_CHARACTER):
            explicit = "character_ids" in fields or bool(current)
            if not explicit or current == expected:
                result.pop("character_ids", None)
        elif not current:
            result.pop("character_ids", None)
    if record_kind == "character":
        current = (
            result.get("base_character_id"), result.get("variant_type"),
        )
        expected = (
            default_compatibility.get("base_character_id"),
            default_compatibility.get("variant_type"),
        )
        version_fields = {"base_character_id", "variant_type"}
        if represented(user_index, VERSION_OF):
            result.pop("base_character_id", None)
            result.pop("variant_type", None)
        elif represented(default_index, VERSION_OF):
            explicit = bool(version_fields.intersection(fields)) or any(current)
            if not explicit or current == expected:
                result.pop("base_character_id", None)
                result.pop("variant_type", None)
        elif not any(current):
            result.pop("base_character_id", None)
            result.pop("variant_type", None)
    return result


def character_record_storage_payloads(
    record_kind: str,
    records: Iterable[Mapping[str, Any]],
    default_relationships: Iterable[KnowledgeRelationship],
    user_relationships: Iterable[KnowledgeRelationship] = (),
    *,
    projection_fields_by_id: Mapping[str, Iterable[str]] | None = None,
) -> list[dict[str, Any]]:
    """Serialize one collection while sharing canonical relationship indexes."""

    defaults = tuple(default_relationships)
    users = tuple(user_relationships)
    default_entity_index = entity_tag_relationship_index(defaults)
    user_entity_index = entity_tag_relationship_index(users)
    default_character_index = character_relationship_index(defaults)
    user_character_index = character_relationship_index(users)
    fields_by_id = projection_fields_by_id or {}
    return [
        character_record_storage_payload(
            record_kind,
            record,
            defaults,
            users,
            projection_fields=fields_by_id.get(
                str(record.get("id") or ""), record.keys(),
            ),
            _default_entity_index=default_entity_index,
            _user_entity_index=user_entity_index,
            _default_character_index=default_character_index,
            _user_character_index=user_character_index,
        )
        for record in records
    ]


def character_record_layered_compatibility_payload(
    record_kind: str,
    record: Mapping[str, Any],
    default_relationships: Iterable[KnowledgeRelationship],
    user_relationships: Iterable[KnowledgeRelationship] = (),
    *,
    projection_fields: Iterable[str] = (),
    _default_entity_index: Mapping[
        tuple[str, str], Iterable[KnowledgeRelationship]
    ] | None = None,
    _user_entity_index: Mapping[
        tuple[str, str], Iterable[KnowledgeRelationship]
    ] | None = None,
    _effective_entity_index: Mapping[
        tuple[str, str], Iterable[KnowledgeRelationship]
    ] | None = None,
    _default_character_index: Mapping[
        tuple[str, str, str], Iterable[KnowledgeRelationship]
    ] | None = None,
    _user_character_index: Mapping[
        tuple[str, str, str], Iterable[KnowledgeRelationship]
    ] | None = None,
    _effective_character_index: Mapping[
        tuple[str, str, str], Iterable[KnowledgeRelationship]
    ] | None = None,
) -> dict[str, Any]:
    """Materialize old-reader fields while respecting legacy local overrides."""

    result = copy.deepcopy(dict(record))
    defaults = tuple(default_relationships)
    users = tuple(user_relationships)
    needs_effective_relationships = (
        _effective_entity_index is None or _effective_character_index is None
    )
    if needs_effective_relationships:
        effective_by_id = {item.id: item for item in defaults}
        effective_by_id.update((item.id, item) for item in users)
        effective = tuple(effective_by_id.values())
    else:
        # Bulk callers already built both indexes once. Rebuilding the complete
        # relationship map for every Character/Series/Outfit made a first run
        # after shipped-knowledge updates quadratic and delayed the first window.
        effective = ()
    fields = {str(value) for value in projection_fields}
    source_kind = _RECORD_ENDPOINT_KINDS[record_kind]
    source_id = str(result.get("id") or "")
    default_entity_index = (
        _default_entity_index
        if _default_entity_index is not None
        else entity_tag_relationship_index(defaults)
    )
    user_entity_index = (
        _user_entity_index
        if _user_entity_index is not None
        else entity_tag_relationship_index(users)
    )
    effective_entity_index = (
        _effective_entity_index
        if _effective_entity_index is not None
        else entity_tag_relationship_index(effective)
    )
    default_character_index = (
        _default_character_index
        if _default_character_index is not None
        else character_relationship_index(defaults)
    )
    user_character_index = (
        _user_character_index
        if _user_character_index is not None
        else character_relationship_index(users)
    )
    effective_character_index = (
        _effective_character_index
        if _effective_character_index is not None
        else character_relationship_index(effective)
    )
    default_payload = character_record_compatibility_payload(
        record_kind,
        {"id": source_id},
        defaults,
        _entity_index=default_entity_index,
        _character_index=default_character_index,
    )
    effective_payload = character_record_compatibility_payload(
        record_kind,
        {"id": source_id},
        effective,
        _entity_index=effective_entity_index,
        _character_index=effective_character_index,
    )
    default_entity = default_entity_index.get(
        (source_kind, source_id), ()
    )
    user_entity = user_entity_index.get(
        (source_kind, source_id), ()
    )
    if user_entity:
        if effective_payload.get("knowledge_tag"):
            result["knowledge_tag"] = effective_payload["knowledge_tag"]
        else:
            result.pop("knowledge_tag", None)
    elif default_entity:
        current = result.get("knowledge_tag")
        expected = default_payload.get("knowledge_tag")
        explicit = "knowledge_tag" in fields or bool(current)
        if not explicit or current == expected:
            if expected:
                result["knowledge_tag"] = expected
            else:
                result.pop("knowledge_tag", None)

    default_index = default_character_index
    user_index = user_character_index

    def represented(
        index: Mapping[tuple[str, str, str], Iterable[KnowledgeRelationship]],
        kind: str,
    ) -> bool:
        return bool(index.get((kind, source_kind, source_id), ()))

    if record_kind in {"character", "outfit"}:
        current = result.get("series_id")
        expected = default_payload.get("series_id")
        if represented(user_index, CHARACTER_SOURCE):
            result["series_id"] = effective_payload.get("series_id")
        elif represented(default_index, CHARACTER_SOURCE):
            explicit = "series_id" in fields or current not in (None, "")
            if not explicit or current == expected:
                result["series_id"] = expected
        result.setdefault("series_id", None)
    if record_kind == "outfit":
        current = result.get("character_ids")
        expected = default_payload.get("character_ids", [])
        if represented(user_index, ASSOCIATED_CHARACTER):
            result["character_ids"] = effective_payload.get("character_ids", [])
        elif represented(default_index, ASSOCIATED_CHARACTER):
            explicit = "character_ids" in fields or bool(current)
            if not explicit or current == expected:
                result["character_ids"] = expected
        result.setdefault("character_ids", [])
    if record_kind == "character":
        current = (
            result.get("base_character_id"), result.get("variant_type"),
        )
        expected = (
            default_payload.get("base_character_id"),
            default_payload.get("variant_type"),
        )
        version_fields = {"base_character_id", "variant_type"}
        if represented(user_index, VERSION_OF):
            if effective_payload.get("base_character_id"):
                result["base_character_id"] = effective_payload["base_character_id"]
                result["variant_type"] = effective_payload.get("variant_type", "")
            else:
                result.pop("base_character_id", None)
                result.pop("variant_type", None)
        elif represented(default_index, VERSION_OF):
            explicit = bool(version_fields.intersection(fields)) or any(current)
            if not explicit or current == expected:
                if expected[0]:
                    result["base_character_id"] = expected[0]
                    result["variant_type"] = expected[1]
                else:
                    result.pop("base_character_id", None)
                    result.pop("variant_type", None)
    return result


def character_record_layered_compatibility_payloads(
    record_kind: str,
    records: Iterable[Mapping[str, Any]],
    default_relationships: Iterable[KnowledgeRelationship],
    user_relationships: Iterable[KnowledgeRelationship] = (),
    *,
    projection_fields_by_id: Mapping[str, Iterable[str]] | None = None,
) -> list[dict[str, Any]]:
    """Materialize one layered collection with shared relationship indexes."""

    defaults = tuple(default_relationships)
    users = tuple(user_relationships)
    effective_by_id = {item.id: item for item in defaults}
    effective_by_id.update((item.id, item) for item in users)
    effective = tuple(effective_by_id.values())
    default_entity_index = entity_tag_relationship_index(defaults)
    user_entity_index = entity_tag_relationship_index(users)
    effective_entity_index = entity_tag_relationship_index(effective)
    default_character_index = character_relationship_index(defaults)
    user_character_index = character_relationship_index(users)
    effective_character_index = character_relationship_index(effective)
    fields_by_id = projection_fields_by_id or {}
    return [
        character_record_layered_compatibility_payload(
            record_kind,
            record,
            defaults,
            users,
            projection_fields=fields_by_id.get(
                str(record.get("id") or ""), record.keys(),
            ),
            _default_entity_index=default_entity_index,
            _user_entity_index=user_entity_index,
            _effective_entity_index=effective_entity_index,
            _default_character_index=default_character_index,
            _user_character_index=user_character_index,
            _effective_character_index=effective_character_index,
        )
        for record in records
    ]


def validate_shipped_character_projection_payloads(
    series_payload: Mapping[str, Any],
    characters_payload: Mapping[str, Any],
    outfits_payload: Mapping[str, Any],
    relationships: Iterable[KnowledgeRelationship],
    *,
    require_retired: bool = False,
) -> int:
    """Require exact canonical authority before shipped fields are removed."""

    all_relationships = tuple(relationships)
    represented = 0
    specs = (
        ("series", series_payload, "series", ("knowledge_tag",)),
        (
            "character",
            characters_payload,
            "characters",
            ("knowledge_tag", "series_id", "base_character_id", "variant_type"),
        ),
        (
            "outfit",
            outfits_payload,
            "outfits",
            ("knowledge_tag", "series_id", "character_ids"),
        ),
    )
    for record_kind, payload, key, fields in specs:
        records = _records(payload, key)
        compatibility_by_id = (
            {}
            if require_retired
            else {
                str(item.get("id") or ""): item
                for item in character_record_compatibility_payloads(
                    record_kind,
                    ({"id": record["id"]} for record in records),
                    all_relationships,
                )
            }
        )
        for record in records:
            if require_retired:
                present = sorted(field for field in fields if field in record)
                if present:
                    raise ValueError(
                        f"Shipped {record_kind} {record['id']} still stores "
                        f"projected fields: {', '.join(present)}"
                    )
                continue
            compatibility = compatibility_by_id[str(record["id"])]
            for field in fields:
                raw = record.get(field)
                expected = compatibility.get(field)
                if raw in (None, "", [], {}) and expected in (None, "", [], {}):
                    continue
                if raw != expected:
                    raise ValueError(
                        f"Shipped {record_kind} {record['id']} field {field} does "
                        "not match canonical relationship authority."
                    )
                represented += 1
    if require_retired:
        versions = (
            (series_payload, SERIES_STORAGE_SCHEMA_VERSION, "series"),
            (characters_payload, CHARACTER_STORAGE_SCHEMA_VERSION, "characters"),
            (outfits_payload, OUTFIT_STORAGE_SCHEMA_VERSION, "outfits"),
        )
        for payload, minimum, label in versions:
            if int(payload.get("schema_version", 1) or 1) < minimum:
                raise ValueError(
                    f"Projection-free shipped {label} require schema v{minimum}."
                )
    return represented


def retire_shipped_character_projection_payloads(
    series_payload: Mapping[str, Any],
    characters_payload: Mapping[str, Any],
    outfits_payload: Mapping[str, Any],
    relationships: Iterable[KnowledgeRelationship],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Remove validated cross-entity projections from shipped record stores."""

    all_relationships = tuple(relationships)
    results = [
        copy.deepcopy(dict(series_payload)),
        copy.deepcopy(dict(characters_payload)),
        copy.deepcopy(dict(outfits_payload)),
    ]
    already_retired = (
        int(results[0].get("schema_version", 1) or 1)
        >= SERIES_STORAGE_SCHEMA_VERSION
        and int(results[1].get("schema_version", 1) or 1)
        >= CHARACTER_STORAGE_SCHEMA_VERSION
        and int(results[2].get("schema_version", 1) or 1)
        >= OUTFIT_STORAGE_SCHEMA_VERSION
        and all(
            "knowledge_tag" not in item
            for item in results[0].get("series", ())
            if isinstance(item, dict)
        )
        and all(
            not {
                "knowledge_tag", "series_id", "base_character_id", "variant_type",
            }.intersection(item)
            for item in results[1].get("characters", ())
            if isinstance(item, dict)
        )
        and all(
            not {"knowledge_tag", "series_id", "character_ids"}.intersection(item)
            for item in results[2].get("outfits", ())
            if isinstance(item, dict)
        )
    )
    if already_retired:
        validate_shipped_character_projection_payloads(
            results[0], results[1], results[2], all_relationships,
            require_retired=True,
        )
        return results[0], results[1], results[2]
    validate_shipped_character_projection_payloads(
        series_payload, characters_payload, outfits_payload, all_relationships,
    )
    results[0]["schema_version"] = max(
        SERIES_STORAGE_SCHEMA_VERSION,
        int(results[0].get("schema_version", 1) or 1),
    )
    results[1]["schema_version"] = max(
        CHARACTER_STORAGE_SCHEMA_VERSION,
        int(results[1].get("schema_version", 1) or 1),
    )
    results[2]["schema_version"] = max(
        OUTFIT_STORAGE_SCHEMA_VERSION,
        int(results[2].get("schema_version", 1) or 1),
    )
    for item in results[0].get("series", ()):
        if isinstance(item, dict):
            item.pop("knowledge_tag", None)
    for item in results[1].get("characters", ()):
        if isinstance(item, dict):
            for field in (
                "knowledge_tag", "series_id", "base_character_id", "variant_type",
            ):
                item.pop(field, None)
    for item in results[2].get("outfits", ()):
        if isinstance(item, dict):
            for field in ("knowledge_tag", "series_id", "character_ids"):
                item.pop(field, None)
    validate_shipped_character_projection_payloads(
        results[0], results[1], results[2], all_relationships,
        require_retired=True,
    )
    return results[0], results[1], results[2]
