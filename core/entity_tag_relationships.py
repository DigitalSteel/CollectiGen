from __future__ import annotations

import copy
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping

from .knowledge_relationships import (
    ENTITY_TAG,
    KnowledgeRelationship,
    RelationshipEndpoint,
)
from .relationship_store import RelationshipStore, validate_relationships


ENTITY_TAG_SOURCE_KINDS = ("character_source", "character", "outfit")
RECORD_KIND_TO_ENDPOINT_KIND = {
    "series": "character_source",
    "character": "character",
    "outfit": "outfit",
}
ENDPOINT_KIND_TO_PROJECTION = {
    "character_source": "series.knowledge_tag",
    "character": "character.knowledge_tag",
    "outfit": "outfit.knowledge_tag",
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


def entity_tag_relationship_id(entity_kind: str, entity_id: str) -> str:
    if entity_kind not in ENTITY_TAG_SOURCE_KINDS:
        raise ValueError(f"Unsupported Entity Tag source kind {entity_kind!r}.")
    digest = sha256(f"{entity_kind}\x1f{entity_id}".encode("utf-8")).hexdigest()[:20]
    return f"entity_tag.{digest}"


@dataclass(frozen=True)
class EffectiveEntityTagLink:
    entity_kind: str
    entity_id: str
    tag_id: str
    represented: bool
    relationship_ids: tuple[str, ...]
    active_tag_ids: tuple[str, ...]
    compatibility_link: dict[str, Any]

    @property
    def conflicting(self) -> bool:
        return len(self.active_tag_ids) > 1

    @property
    def effective_link(self) -> dict[str, Any]:
        if not self.represented:
            return copy.deepcopy(self.compatibility_link)
        if not self.tag_id:
            result = {
                key: copy.deepcopy(value)
                for key, value in self.compatibility_link.items()
                if key not in {"tag_id", "status", "basis", "reviewed_at", "source"}
            }
            if self.conflicting:
                result.update(
                    {
                        "status": "canonical_conflict",
                        "tag_ids": list(self.active_tag_ids),
                        "source": "canonical_relationship",
                    }
                )
            return result
        result = copy.deepcopy(self.compatibility_link)
        result.update(
            {
                "tag_id": self.tag_id,
                "status": "reviewed",
                "source": "canonical_relationship",
            }
        )
        result.pop("tag_ids", None)
        return result


@dataclass(frozen=True)
class EntityTagChange:
    entity_kind: str
    entity_id: str
    tag_id: str
    entity_label: str = ""
    tag_label: str = ""
    source: str = "user_review"
    basis: str = ""
    reviewed_at: str = ""
    compatibility_link: Mapping[str, Any] | None = None


def entity_tag_relationship_index(
    relationships: Iterable[KnowledgeRelationship],
) -> dict[tuple[str, str], tuple[KnowledgeRelationship, ...]]:
    index: dict[tuple[str, str], list[KnowledgeRelationship]] = {}
    for relationship in relationships:
        source = relationship.source_endpoint
        if (
            relationship.kind == ENTITY_TAG
            and relationship.directed
            and source is not None
            and source.kind in ENTITY_TAG_SOURCE_KINDS
        ):
            index.setdefault((source.kind, source.id), []).append(relationship)
    return {
        key: tuple(sorted(values, key=lambda item: item.id))
        for key, values in index.items()
    }


def load_entity_tag_relationship_index(
    default_path: str | Path,
    user_path: str | Path,
) -> dict[tuple[str, str], tuple[KnowledgeRelationship, ...]]:
    effective: dict[str, KnowledgeRelationship] = {}
    paths = tuple(dict.fromkeys((Path(default_path), Path(user_path))))
    for path in paths:
        effective.update(RelationshipStore(path).relationships)
    issues = validate_relationships(effective.values())
    if issues:
        raise ValueError(" ".join(issue.message for issue in issues[:3]))
    return entity_tag_relationship_index(effective.values())


def effective_entity_tag_link(
    entity_kind: str,
    entity_id: str,
    compatibility_link: Mapping[str, Any] | None,
    source_index: Mapping[
        tuple[str, str], Iterable[KnowledgeRelationship]
    ],
) -> EffectiveEntityTagLink:
    relationships = tuple(source_index.get((entity_kind, entity_id), ()))
    targets: list[str] = []
    for relationship in relationships:
        if relationship.disabled or not relationship.reviewed:
            continue
        for endpoint in relationship.target_endpoints:
            if endpoint.kind == "tag" and endpoint.id not in targets:
                targets.append(endpoint.id)
    compatibility = copy.deepcopy(dict(compatibility_link or {}))
    compatibility_tag_id = (
        str(compatibility.get("tag_id") or "")
        if compatibility.get("status") == "reviewed" else ""
    )
    return EffectiveEntityTagLink(
        entity_kind=entity_kind,
        entity_id=entity_id,
        tag_id=(
            targets[0]
            if len(targets) == 1
            else compatibility_tag_id
            if not relationships
            else ""
        ),
        represented=bool(relationships),
        relationship_ids=tuple(item.id for item in relationships),
        active_tag_ids=tuple(targets),
        compatibility_link=compatibility,
    )


def _history_entry(relationship: KnowledgeRelationship) -> dict[str, Any]:
    return {
        "changed_at": _utc_now(),
        "relationship": relationship.to_dict(),
        "relationship_fingerprint": _fingerprint(relationship),
    }


def _revised_relationship(
    current: KnowledgeRelationship | None,
    *,
    relationship_id: str,
    change: EntityTagChange,
    disabled: bool,
) -> KnowledgeRelationship:
    details = dict(current.details) if current is not None else {}
    details.update(
        {
            "reviewed_at": change.reviewed_at or _utc_now(),
            "compatibility_projection": ENDPOINT_KIND_TO_PROJECTION[change.entity_kind],
        }
    )
    if change.basis:
        details["basis"] = change.basis
    extras = copy.deepcopy(current.extras) if current is not None else {}
    if change.compatibility_link is not None:
        extras["compatibility_projection_payload"] = copy.deepcopy(
            dict(change.compatibility_link)
        )
    history = [
        copy.deepcopy(item)
        for item in extras.get("entity_tag_history", ())
        if isinstance(item, Mapping)
    ]
    if current is not None:
        history.append(_history_entry(current))
    if history:
        extras["entity_tag_history"] = history
    source_endpoint = RelationshipEndpoint(
        change.entity_kind,
        change.entity_id,
        change.entity_label,
    )
    target = RelationshipEndpoint("tag", change.tag_id, change.tag_label)
    return KnowledgeRelationship(
        id=relationship_id,
        kind=ENTITY_TAG,
        endpoints=(source_endpoint, target),
        directed=True,
        scope=current.scope if current is not None else "any",
        label=(
            current.label
            if current is not None and current.label
            else f"{change.entity_label or change.entity_id} Representative Tag"
        ),
        reviewed=True,
        source=change.source,
        details=tuple((str(key), str(value)) for key, value in details.items()),
        version=(current.version + 1) if current is not None else 1,
        layer="user",
        disabled=disabled,
        extras=extras,
    )


def stage_entity_tag_changes(
    default_path: str | Path,
    user_path: str | Path,
    changes: Iterable[EntityTagChange],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Build a user-store payload that makes each supplied Entity Tag decisive.

    Existing canonical records keep their IDs and history. Competing user records
    are disabled, and defaults are suppressed through same-ID user overrides.
    """
    default_store = RelationshipStore(default_path)
    user_store = RelationshipStore(user_path)
    defaults = dict(default_store.relationships)
    users = dict(user_store.relationships)
    changed_ids: list[str] = []

    for change in changes:
        if change.entity_kind not in ENTITY_TAG_SOURCE_KINDS:
            raise ValueError(f"Unsupported Entity Tag source kind {change.entity_kind!r}.")
        if not change.entity_id or not change.tag_id:
            raise ValueError("An Entity Tag change requires stable entity and Tag IDs.")
        effective = dict(defaults)
        effective.update(users)
        matches = [
            relationship
            for relationship in effective.values()
            if relationship.kind == ENTITY_TAG
            and relationship.source_endpoint is not None
            and relationship.source_endpoint.kind == change.entity_kind
            and relationship.source_endpoint.id == change.entity_id
        ]
        preferred_id = entity_tag_relationship_id(change.entity_kind, change.entity_id)
        preferred = next((item for item in matches if item.id == preferred_id), None)
        primary = preferred or next(
            (
                item
                for item in sorted(matches, key=lambda value: value.id)
                if not item.disabled and item.reviewed
            ),
            None,
        ) or (sorted(matches, key=lambda value: value.id)[0] if matches else None)
        primary_id = primary.id if primary is not None else preferred_id
        collision = effective.get(primary_id)
        if collision is not None and collision not in matches:
            suffix = sha256(
                f"{change.entity_kind}\x1f{change.entity_id}\x1f{change.tag_id}".encode("utf-8")
            ).hexdigest()[:8]
            primary_id = f"{preferred_id}.{suffix}"
            primary = None

        users[primary_id] = _revised_relationship(
            users.get(primary_id) or primary,
            relationship_id=primary_id,
            change=change,
            disabled=False,
        )
        changed_ids.append(primary_id)
        for relationship in matches:
            if relationship.id == primary_id:
                continue
            disabled_change = replace(
                change,
                tag_id=(
                    relationship.target_endpoints[0].id
                    if relationship.target_endpoints else change.tag_id
                ),
                tag_label=(
                    relationship.target_endpoints[0].label
                    if relationship.target_endpoints else ""
                ),
            )
            users[relationship.id] = _revised_relationship(
                users.get(relationship.id) or relationship,
                relationship_id=relationship.id,
                change=disabled_change,
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
