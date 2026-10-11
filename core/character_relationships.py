from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping

from .knowledge_relationships import (
    ASSOCIATED_CHARACTER,
    CHARACTER_SOURCE,
    OTHER_APPEARANCE,
    SPECIES_TRAIT,
    VERSION_OF,
    KnowledgeRelationship,
    RelationshipEndpoint,
)
from .relationship_store import RelationshipStore, validate_relationships


CHARACTER_RELATIONSHIP_KINDS = (
    CHARACTER_SOURCE,
    ASSOCIATED_CHARACTER,
    OTHER_APPEARANCE,
    VERSION_OF,
    SPECIES_TRAIT,
)
SINGLE_TARGET_KINDS = (CHARACTER_SOURCE, VERSION_OF)
COMPATIBILITY_PROJECTIONS = {
    CHARACTER_SOURCE: {
        "character": "character.series_id",
        "outfit": "outfit.series_id",
    },
    ASSOCIATED_CHARACTER: {"outfit": "outfit.character_ids"},
    VERSION_OF: {"character": "character.base_character_id"},
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fingerprint(relationship: KnowledgeRelationship) -> str:
    payload = json.dumps(
        relationship.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def character_relationship_id(
    kind: str,
    source_kind: str,
    source_id: str,
    target_id: str,
) -> str:
    if kind not in CHARACTER_RELATIONSHIP_KINDS:
        raise ValueError(f"Unsupported Character relationship kind {kind!r}.")
    digest = sha256(
        f"{kind}\x1f{source_kind}\x1f{source_id}\x1f{target_id}".encode("utf-8")
    ).hexdigest()[:20]
    return f"{kind}.{digest}"


@dataclass(frozen=True)
class EffectiveCharacterRelationship:
    kind: str
    source_kind: str
    source_id: str
    target_ids: tuple[str, ...]
    represented: bool
    conflicting: bool
    relationship_ids: tuple[str, ...]
    active_relationships: tuple[KnowledgeRelationship, ...]
    compatibility_target_ids: tuple[str, ...]

    @property
    def target_id(self) -> str:
        return self.target_ids[0] if len(self.target_ids) == 1 else ""

    @property
    def details(self) -> dict[str, str]:
        if len(self.active_relationships) != 1:
            return {}
        return dict(self.active_relationships[0].details)


@dataclass(frozen=True)
class CharacterRelationshipChange:
    kind: str
    source_kind: str
    source_id: str
    target_ids: tuple[str, ...]
    source_label: str = ""
    target_labels: tuple[tuple[str, str], ...] = ()
    compatibility_target_ids: tuple[str, ...] = ()
    details: tuple[tuple[str, str], ...] = ()
    source: str = "character_record_edit"


def character_relationship_index(
    relationships: Iterable[KnowledgeRelationship],
) -> dict[tuple[str, str, str], tuple[KnowledgeRelationship, ...]]:
    index: dict[tuple[str, str, str], list[KnowledgeRelationship]] = {}
    for relationship in relationships:
        source = relationship.source_endpoint
        if (
            relationship.kind in CHARACTER_RELATIONSHIP_KINDS
            and relationship.directed
            and source is not None
        ):
            index.setdefault(
                (relationship.kind, source.kind, source.id), []
            ).append(relationship)
    return {
        key: tuple(sorted(values, key=lambda item: item.id))
        for key, values in index.items()
    }


def effective_character_relationship(
    kind: str,
    source_kind: str,
    source_id: str,
    compatibility_target_ids: Iterable[str],
    source_index: Mapping[
        tuple[str, str, str], Iterable[KnowledgeRelationship]
    ],
) -> EffectiveCharacterRelationship:
    relationships = tuple(source_index.get((kind, source_kind, source_id), ()))
    active = tuple(
        relationship
        for relationship in relationships
        if relationship.reviewed and not relationship.disabled
    )
    targets = tuple(dict.fromkeys(
        endpoint.id
        for relationship in active
        for endpoint in relationship.target_endpoints
    ))
    conflicting = kind in SINGLE_TARGET_KINDS and len(targets) > 1
    compatibility = tuple(dict.fromkeys(
        str(value) for value in compatibility_target_ids if str(value)
    ))
    effective_targets = (
        () if conflicting else targets
    ) if relationships else compatibility
    return EffectiveCharacterRelationship(
        kind=kind,
        source_kind=source_kind,
        source_id=source_id,
        target_ids=effective_targets,
        represented=bool(relationships),
        conflicting=conflicting,
        relationship_ids=tuple(item.id for item in relationships),
        active_relationships=active,
        compatibility_target_ids=compatibility,
    )


def _history_entry(relationship: KnowledgeRelationship) -> dict[str, Any]:
    return {
        "changed_at": _utc_now(),
        "relationship": relationship.to_dict(),
        "relationship_fingerprint": _fingerprint(relationship),
    }


def _target_kind(kind: str) -> str:
    return (
        "character_source"
        if kind in {CHARACTER_SOURCE, OTHER_APPEARANCE}
        else "character"
        if kind in {ASSOCIATED_CHARACTER, VERSION_OF}
        else "tag"
    )


def _revised_relationship(
    current: KnowledgeRelationship | None,
    *,
    relationship_id: str,
    change: CharacterRelationshipChange,
    target_id: str,
    disabled: bool,
) -> KnowledgeRelationship:
    labels = dict(change.target_labels)
    details = dict(current.details) if current is not None else {}
    details.update(dict(change.details))
    projection = COMPATIBILITY_PROJECTIONS.get(change.kind, {}).get(
        change.source_kind, ""
    )
    if projection:
        details["compatibility_projection"] = projection
    details["reviewed_at"] = _utc_now()
    extras = copy.deepcopy(current.extras) if current is not None else {}
    history = [
        copy.deepcopy(item)
        for item in extras.get("character_relationship_history", ())
        if isinstance(item, Mapping)
    ]
    if current is not None:
        history.append(_history_entry(current))
    if history:
        extras["character_relationship_history"] = history
    return KnowledgeRelationship(
        id=relationship_id,
        kind=change.kind,
        endpoints=(
            RelationshipEndpoint(
                change.source_kind, change.source_id, change.source_label
            ),
            RelationshipEndpoint(_target_kind(change.kind), target_id, labels.get(target_id, "")),
        ),
        directed=True,
        scope=current.scope if current is not None else (
            "same_character" if change.kind == SPECIES_TRAIT else "any"
        ),
        label=(
            current.label
            if current is not None and current.label
            else change.kind.replace("_", " ").title()
        ),
        reviewed=True,
        source=change.source,
        details=tuple((str(key), str(value)) for key, value in details.items()),
        version=(current.version + 1) if current is not None else 1,
        layer="user",
        disabled=disabled,
        extras=extras,
    )


def _store_from_payload(
    path: str | Path,
    payload: Mapping[str, Any] | None,
) -> RelationshipStore:
    store = RelationshipStore(path)
    if payload is None:
        return store
    store.schema_version = max(
        store.SCHEMA_VERSION, int(payload.get("schema_version", 0) or 0)
    )
    store.extras = {
        key: copy.deepcopy(value)
        for key, value in payload.items()
        if key not in {"schema_version", "relationships"}
    }
    store.replace(
        KnowledgeRelationship.from_dict(value)
        for value in payload.get("relationships", ())
        if isinstance(value, Mapping)
    )
    return store


def stage_character_relationship_changes(
    default_path: str | Path,
    user_path: str | Path,
    changes: Iterable[CharacterRelationshipChange],
    *,
    user_payload: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Stage canonical cross-entity authority without dropping legacy sets.

    Each change supplies the complete desired target set for one source and
    relationship kind. Existing IDs, unknown fields, and audit history are
    retained; removed defaults and compatibility targets receive disabled user
    records so stale projections cannot become authoritative again.
    """
    default_store = RelationshipStore(default_path)
    user_store = _store_from_payload(user_path, user_payload)
    defaults = dict(default_store.relationships)
    users = dict(user_store.relationships)
    changed_ids: list[str] = []

    for change in changes:
        if change.kind not in CHARACTER_RELATIONSHIP_KINDS:
            raise ValueError(
                f"Unsupported Character relationship kind {change.kind!r}."
            )
        if not change.source_id:
            raise ValueError("A Character relationship change requires a stable source ID.")
        desired = tuple(dict.fromkeys(
            str(value) for value in change.target_ids if str(value)
        ))
        if change.kind in SINGLE_TARGET_KINDS and len(desired) > 1:
            raise ValueError(
                f"{change.kind.replace('_', ' ').title()} accepts one target."
            )
        effective = dict(defaults)
        effective.update(users)
        matches = [
            relationship
            for relationship in effective.values()
            if relationship.kind == change.kind
            and relationship.source_endpoint is not None
            and relationship.source_endpoint.kind == change.source_kind
            and relationship.source_endpoint.id == change.source_id
        ]
        by_target: dict[str, list[KnowledgeRelationship]] = {}
        for relationship in matches:
            for endpoint in relationship.target_endpoints:
                by_target.setdefault(endpoint.id, []).append(relationship)

        used_ids: set[str] = set()
        for target_id in desired:
            candidates = sorted(by_target.get(target_id, ()), key=lambda item: item.id)
            current = next(
                (item for item in candidates if item.reviewed and not item.disabled),
                candidates[0] if candidates else None,
            )
            if current is None and change.kind in SINGLE_TARGET_KINDS:
                current = next(
                    (
                        item
                        for item in sorted(matches, key=lambda value: value.id)
                        if item.id not in used_ids
                    ),
                    None,
                )
            relationship_id = (
                current.id
                if current is not None
                else character_relationship_id(
                    change.kind, change.source_kind, change.source_id, target_id
                )
            )
            collision = effective.get(relationship_id)
            if collision is not None and collision not in matches:
                suffix = sha256(
                    f"{relationship_id}\x1f{target_id}".encode("utf-8")
                ).hexdigest()[:8]
                relationship_id = f"{relationship_id}.{suffix}"
                current = None
            users[relationship_id] = _revised_relationship(
                users.get(relationship_id) or current,
                relationship_id=relationship_id,
                change=change,
                target_id=target_id,
                disabled=False,
            )
            used_ids.add(relationship_id)
            changed_ids.append(relationship_id)

        stale_targets = set(change.compatibility_target_ids)
        stale_targets.update(by_target)
        for target_id in sorted(stale_targets.difference(desired)):
            candidates = sorted(by_target.get(target_id, ()), key=lambda item: item.id)
            current = next(
                (item for item in candidates if item.id not in used_ids),
                None,
            )
            relationship_id = (
                current.id
                if current is not None
                else character_relationship_id(
                    change.kind, change.source_kind, change.source_id, target_id
                )
            )
            users[relationship_id] = _revised_relationship(
                users.get(relationship_id) or current,
                relationship_id=relationship_id,
                change=change,
                target_id=target_id,
                disabled=True,
            )
            used_ids.add(relationship_id)
            changed_ids.append(relationship_id)

        for relationship in matches:
            if relationship.id in used_ids:
                continue
            target = relationship.target_endpoints[0]
            users[relationship.id] = _revised_relationship(
                users.get(relationship.id) or relationship,
                relationship_id=relationship.id,
                change=change,
                target_id=target.id,
                disabled=True,
            )
            changed_ids.append(relationship.id)

    user_store.replace(users.values())
    effective = dict(defaults)
    effective.update(user_store.relationships)
    issues = validate_relationships(effective.values())
    if issues:
        raise ValueError(" ".join(issue.message for issue in issues[:3]))
    return user_store.payload(), tuple(dict.fromkeys(changed_ids))
