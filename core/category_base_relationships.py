from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Iterable, Mapping

from .category_registry import (
    CategoryRecord,
    CategoryRegistry,
    normalize_category_reference_policy,
)
from .knowledge_relationships import (
    CATEGORY_BASE,
    KnowledgeRelationship,
    RelationshipEndpoint,
)
from .relationship_store import validate_relationships
from .tag_database import TagDatabase


DEFAULT_CATEGORY_BASE_COMPILER = "reviewed_category_bases_v1"
REVIEWED_CATEGORY_BASE_AUTHORITY = "reviewed_category_base_relationships_v1"
DEFAULT_CATEGORY_BASE_SOURCE = "CollectiGen reviewed shipped Category Base policies"
CATEGORY_BASE_MEMBER_SCOPE_DIRECT = "direct"
CATEGORY_BASE_MEMBER_SCOPE_DESCENDANTS = "descendants"
CATEGORY_BASE_MEMBER_SCOPES = (
    CATEGORY_BASE_MEMBER_SCOPE_DIRECT,
    CATEGORY_BASE_MEMBER_SCOPE_DESCENDANTS,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def category_base_relationship_id(category_id: str) -> str:
    """Return one stable canonical relationship identity per Category."""

    digest = sha256(str(category_id).encode("utf-8")).hexdigest()[:20]
    return f"category_base.{digest}"


@dataclass(frozen=True)
class DirectCategoryBaseAuthority:
    category_id: str
    base_tag_id: str
    policy: str
    represented: bool
    ambiguous: bool
    relationship_ids: tuple[str, ...] = ()
    member_scope: str = CATEGORY_BASE_MEMBER_SCOPE_DIRECT
    scope_reviewed: bool = True


def normalize_category_base_member_scope(value: Any) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized not in CATEGORY_BASE_MEMBER_SCOPES:
        raise ValueError(
            "Choose whether the Category Base applies to direct members only "
            "or to direct and descendant members."
        )
    return normalized


def _stored_member_scope(
    relationship: KnowledgeRelationship,
) -> tuple[str, bool]:
    """Return explicit scope, preserving pre-scope records as legacy descendants."""

    value = str(dict(relationship.details).get("member_scope") or "")
    if not value:
        return CATEGORY_BASE_MEMBER_SCOPE_DESCENDANTS, False
    return normalize_category_base_member_scope(value), True


def category_base_relationship_index(
    relationships: Iterable[KnowledgeRelationship],
) -> dict[str, tuple[KnowledgeRelationship, ...]]:
    """Index canonical Category Base authority by its Category source."""

    values: dict[str, list[KnowledgeRelationship]] = {}
    for relationship in relationships:
        source = relationship.source_endpoint
        if (
            relationship.kind == CATEGORY_BASE
            and relationship.directed
            and source is not None
            and source.kind == "category"
        ):
            values.setdefault(source.id, []).append(relationship)
    return {
        category_id: tuple(sorted(records, key=lambda item: item.id))
        for category_id, records in values.items()
    }


def direct_category_base_authority(
    record: CategoryRecord,
    source_index: Mapping[str, Iterable[KnowledgeRelationship]],
) -> DirectCategoryBaseAuthority:
    """Resolve one direct policy, falling back only when it is unrepresented.

    Canonical presence owns the compatibility projection even when deliberately
    disabled. Multiple active records or malformed active authority fail closed
    instead of reviving an inline Category field.
    """

    records = tuple(source_index.get(str(record.id), ()))
    if not records:
        policy = normalize_category_reference_policy(
            record.base_tag_policy,
            parent_id=record.parent_id,
            own_value=record.base_tag_id,
        )
        return DirectCategoryBaseAuthority(
            record.id,
            str(record.base_tag_id or "") if policy == "own" else "",
            policy,
            False,
            False,
            (),
            (
                CATEGORY_BASE_MEMBER_SCOPE_DESCENDANTS
                if policy == "own"
                else CATEGORY_BASE_MEMBER_SCOPE_DIRECT
            ),
            policy != "own",
        )

    active = tuple(
        relationship
        for relationship in records
        if relationship.reviewed and not relationship.disabled
    )
    invalid_active = any(
        not relationship.disabled and not relationship.reviewed
        for relationship in records
    )
    relationship_ids = tuple(item.id for item in records)
    if invalid_active or len(active) > 1:
        return DirectCategoryBaseAuthority(
            record.id, "", "none", True, True, relationship_ids,
        )
    if active:
        targets = tuple(
            endpoint
            for endpoint in active[0].target_endpoints
            if endpoint.kind == "tag"
        )
        try:
            member_scope, scope_reviewed = _stored_member_scope(active[0])
        except ValueError:
            return DirectCategoryBaseAuthority(
                record.id, "", "none", True, True, relationship_ids,
            )
        if len(targets) != 1 or dict(active[0].details).get("policy", "own") != "own":
            return DirectCategoryBaseAuthority(
                record.id, "", "none", True, True, relationship_ids,
            )
        return DirectCategoryBaseAuthority(
            record.id, targets[0].id, "own", True, False, relationship_ids,
            member_scope, scope_reviewed,
        )

    policies = {
        str(dict(relationship.details).get("policy") or "")
        for relationship in records
    }
    policies.discard("")
    if len(policies) == 1 and next(iter(policies)) in {"inherit", "none"}:
        return DirectCategoryBaseAuthority(
            record.id, "", next(iter(policies)), True, False, relationship_ids,
        )
    return DirectCategoryBaseAuthority(
        record.id, "", "none", True, True, relationship_ids,
    )


def category_base_compatibility_payload(
    record: CategoryRecord,
    source_index: Mapping[str, Iterable[KnowledgeRelationship]],
) -> dict[str, Any]:
    """Serialize one exact compatibility view without changing ``record``."""

    projected = copy.deepcopy(record)
    direct = direct_category_base_authority(projected, source_index)
    if direct.represented:
        projected.base_tag_id = (
            direct.base_tag_id if direct.policy == "own" else ""
        )
        projected.base_tag_policy = (
            "none" if direct.ambiguous else direct.policy
        )
    return projected.to_dict(include_category_base_projection=True)


def _history_entry(relationship: KnowledgeRelationship) -> dict[str, Any]:
    return {
        "changed_at": _utc_now(),
        "relationship": relationship.to_dict(),
    }


def _updated_category_base(
    current: KnowledgeRelationship | None,
    *,
    relationship_id: str,
    category: CategoryRecord,
    target_id: str,
    target_label: str,
    policy: str,
    member_scope: str,
    source: str,
    reason: str,
) -> KnowledgeRelationship:
    details = dict(current.details) if current is not None else {}
    details.update({
        "policy": policy,
        "member_scope": normalize_category_base_member_scope(member_scope),
        "compatibility_projection": "category.base_tag_id",
        "reviewed_at": _utc_now(),
    })
    if reason:
        details["reason"] = reason
    extras = copy.deepcopy(current.extras) if current is not None else {}
    history = [
        dict(item)
        for item in extras.get("category_base_history", ())
        if isinstance(item, Mapping)
    ]
    if current is not None:
        history.append(_history_entry(current))
    if history:
        extras["category_base_history"] = history
    return KnowledgeRelationship(
        id=relationship_id,
        kind=CATEGORY_BASE,
        endpoints=(
            RelationshipEndpoint("category", category.id, category.name),
            RelationshipEndpoint("tag", target_id, target_label),
        ),
        directed=True,
        scope="organization",
        label=f"{category.name} Category Base Tag",
        reviewed=True,
        source=str(source or "category_workspace"),
        details=tuple((str(key), str(value)) for key, value in details.items()),
        version=(current.version + 1) if current is not None else 1,
        layer="user",
        disabled=policy != "own",
        extras=extras,
    )


def stage_category_base_change(
    category: CategoryRecord,
    tags: Mapping[str, Any],
    default_relationships: Mapping[str, KnowledgeRelationship],
    user_relationships: Mapping[str, KnowledgeRelationship],
    *,
    policy: str,
    base_tag_id: str = "",
    member_scope: str = CATEGORY_BASE_MEMBER_SCOPE_DIRECT,
    previous_base_tag_id: str = "",
    source: str = "category_workspace",
    reason: str = "",
) -> tuple[dict[str, KnowledgeRelationship], tuple[str, ...]]:
    """Stage canonical authority for one Category and its old-reader policy."""

    desired_policy = normalize_category_reference_policy(
        policy,
        parent_id=category.parent_id,
        own_value=base_tag_id,
    )
    desired_base = str(base_tag_id or "") if desired_policy == "own" else ""
    desired_member_scope = normalize_category_base_member_scope(member_scope)
    if desired_policy == "own" and desired_base not in tags:
        raise ValueError("The selected Category Base Tag is no longer available.")

    effective = dict(default_relationships)
    effective.update(user_relationships)
    source_index = category_base_relationship_index(effective.values())
    current_records = tuple(source_index.get(category.id, ()))
    stable_id = category_base_relationship_id(category.id)
    primary = effective.get(stable_id) or (current_records[0] if current_records else None)
    historical_target = ""
    historical_label = ""
    if primary is not None:
        historical = next(
            (item for item in primary.target_endpoints if item.kind == "tag"),
            None,
        )
        if historical is not None:
            historical_target = historical.id
            historical_label = historical.label
    target_id = desired_base or historical_target or str(previous_base_tag_id or "")
    if not target_id:
        return dict(user_relationships), ()
    target = tags.get(target_id)
    target_label = str(getattr(target, "display", "") or historical_label or target_id)

    updated = _updated_category_base(
        primary,
        relationship_id=stable_id,
        category=category,
        target_id=target_id,
        target_label=target_label,
        policy=desired_policy,
        member_scope=desired_member_scope,
        source=source,
        reason=reason,
    )
    user_values = dict(user_relationships)
    user_values[stable_id] = updated
    changed_ids = [stable_id]
    for extra in current_records:
        if extra.id == stable_id:
            continue
        disabled = _updated_category_base(
            extra,
            relationship_id=extra.id,
            category=category,
            target_id=next(
                (item.id for item in extra.target_endpoints if item.kind == "tag"),
                target_id,
            ),
            target_label=next(
                (item.label for item in extra.target_endpoints if item.kind == "tag"),
                target_label,
            ),
            policy=desired_policy if desired_policy != "own" else "none",
            member_scope=desired_member_scope,
            source=source,
            reason="Superseded by the stable Category Base authority.",
        )
        user_values[extra.id] = disabled
        changed_ids.append(extra.id)

    candidate = dict(default_relationships)
    candidate.update(user_values)
    issues = validate_relationships(candidate.values())
    if issues:
        raise ValueError(" ".join(issue.message for issue in issues[:3]))
    return user_values, tuple(dict.fromkeys(changed_ids))


def stage_category_base_restore(
    category_id: str,
    default_relationships: Mapping[str, KnowledgeRelationship],
    user_relationships: Mapping[str, KnowledgeRelationship],
) -> tuple[dict[str, KnowledgeRelationship], tuple[str, ...]]:
    """Clear personal Category Base authority so the shipped default is visible."""

    user_values = dict(user_relationships)
    removed = []
    for relationship_id, relationship in tuple(user_values.items()):
        source = relationship.source_endpoint
        if (
            relationship.kind == CATEGORY_BASE
            and source is not None
            and source.kind == "category"
            and source.id == str(category_id)
        ):
            user_values.pop(relationship_id, None)
            removed.append(relationship_id)
    candidate = dict(default_relationships)
    candidate.update(user_values)
    issues = validate_relationships(candidate.values())
    if issues:
        raise ValueError(" ".join(issue.message for issue in issues[:3]))
    return user_values, tuple(removed)


def load_reviewed_default_category_base_relationships(
    categories: CategoryRegistry,
    tags: TagDatabase,
    relationships: Iterable[KnowledgeRelationship],
) -> tuple[KnowledgeRelationship, ...]:
    """Validate and normalize shipped canonical Category Base authority.

    A Category Base is organization and reusable-Base policy, not evidence that
    every member semantically implies the selected Tag. Canonical reviewed
    relationships are the compiler input. When a Category still carries an
    old-reader projection, it is checked against that canonical authority.
    """

    category_records = categories.categories
    categories_with_descendants = {
        str(record.parent_id)
        for record in category_records.values()
        if record.parent_id
    }
    normalized: list[KnowledgeRelationship] = []
    represented: set[str] = set()
    for relationship in sorted(relationships, key=lambda item: item.id):
        if relationship.kind != CATEGORY_BASE or relationship.layer != "default":
            continue
        source = relationship.source_endpoint
        targets = relationship.target_endpoints
        category = category_records.get(source.id) if source is not None else None
        tag = tags.tags.get(targets[0].id) if len(targets) == 1 else None
        details = dict(relationship.details)
        category_has_descendants = bool(
            category is not None
            and category.id in categories_with_descendants
        )
        try:
            member_scope = (
                normalize_category_base_member_scope(details["member_scope"])
                if details.get("member_scope")
                else (
                    CATEGORY_BASE_MEMBER_SCOPE_DESCENDANTS
                    if category_has_descendants
                    else CATEGORY_BASE_MEMBER_SCOPE_DIRECT
                )
            )
            if (
                member_scope == CATEGORY_BASE_MEMBER_SCOPE_DESCENDANTS
                and not category_has_descendants
            ):
                # A shipped leaf has no descendant decision to preserve. Keep
                # it direct so adding a future child cannot broaden authority
                # without a new reviewed default change.
                member_scope = CATEGORY_BASE_MEMBER_SCOPE_DIRECT
        except ValueError as error:
            raise ValueError(
                f"Shipped Category Base {relationship.id!r} has invalid member "
                "scope."
            ) from error
        if (
            not relationship.directed
            or not relationship.reviewed
            or relationship.disabled
            or source is None
            or source.kind != "category"
            or category is None
            or len(targets) != 1
            or targets[0].kind != "tag"
            or tag is None
            or details.get("policy") != "own"
            or relationship.scope != "organization"
        ):
            raise ValueError(
                f"Shipped Category Base {relationship.id!r} is not one valid "
                "reviewed Category-to-Tag authority record."
            )
        if relationship.id != category_base_relationship_id(category.id):
            raise ValueError(
                f"Shipped Category Base for {category.id!r} has unstable ID "
                f"{relationship.id!r}."
            )
        if category.id in represented:
            raise ValueError(
                f"Shipped Category {category.id!r} has duplicate Category Base "
                "authority."
            )
        represented.add(category.id)
        if category.base_tag_policy == "own" and not category.base_tag_id:
            raise ValueError(
                f"Shipped Category {category.id!r} compatibility projection "
                "has an own policy without a Base Tag."
            )
        if category.base_tag_id and (
            category.base_tag_policy != "own" or category.base_tag_id != tag.id
        ):
            raise ValueError(
                f"Shipped Category {category.id!r} compatibility projection "
                f"does not match canonical Base Tag {tag.id!r}."
            )
        marker = details.pop("default_compiler", "")
        if marker and marker != DEFAULT_CATEGORY_BASE_COMPILER:
            raise ValueError(
                f"Shipped Category Base {relationship.id!r} has unknown legacy "
                f"compiler marker {marker!r}."
            )
        details["reviewed_authority"] = REVIEWED_CATEGORY_BASE_AUTHORITY
        details["member_scope"] = member_scope
        normalized.append(replace(
            relationship,
            endpoints=(
                RelationshipEndpoint("category", category.id, category.name),
                RelationshipEndpoint("tag", tag.id, tag.display),
            ),
            label=f"{category.name} Category Base Tag",
            source=DEFAULT_CATEGORY_BASE_SOURCE,
            details=tuple((str(key), str(value)) for key, value in details.items()),
        ))

    projected = {
        category.id
        for category in category_records.values()
        if category.base_tag_policy == "own" or category.base_tag_id
    }
    missing = sorted(projected - represented)
    if missing:
        raise ValueError(
            "Shipped Category Base compatibility projections lack canonical "
            "authority: " + ", ".join(missing[:10])
        )
    return tuple(normalized)
