from __future__ import annotations

import copy
import json
import shutil
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Mapping
from uuid import uuid4

from .automation_rule_conversion import RulePairAdmission
from .category_registry import CategoryRegistry
from .character_database import CharacterDatabase
from .character_projection_storage import character_record_storage_payloads
from .knowledge_engine import FoundationKnowledge
from .knowledge_relationships import (
    CATEGORY_BASE,
    KnowledgeRelationship,
    RelationshipEndpoint,
)
from .relationship_store import RelationshipStore, validate_relationships
from .constraint_resolver import (
    constraint_pairs,
    owns_inline_constraint_projection,
)
from .tag_collection import normalize_tag
from .tag_database import TagDatabase, TagRecord
from .tag_alias_relationships import TagAliasChange,stage_tag_alias_changes
from .tag_semantic_relationships import publish_tag_semantic_authority
from .tag_alias_relationships import publish_tag_alias_storage_authority
from .reusable_modifier_projection import (
    publish_reusable_modifier_storage_authority,
)
from .scoped_constraint_projection import (
    publish_scoped_constraint_storage_authority,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_bytes(
        path,
        (json.dumps(dict(payload), indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
    )


def _signature(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _display_value(value: Any) -> str:
    if value in (None, "", [], {}, ()):
        return "Not set"
    if isinstance(value, dict):
        text = ", ".join(f"{key}: {item}" for key, item in sorted(value.items()))
    elif isinstance(value, (list, tuple, set)):
        text = ", ".join(str(item) for item in value)
    else:
        text = str(value)
    return text if len(text) <= 160 else text[:157] + "..."


@dataclass(frozen=True)
class TagMergeChoice:
    key: str
    label: str


@dataclass(frozen=True)
class TagMergeConflict:
    key: str
    label: str
    choices: tuple[TagMergeChoice, ...]


@dataclass(frozen=True)
class TagIdentityMergePlan:
    survivor_id: str
    donor_ids: tuple[str, ...]
    survivor_name: str
    donor_names: tuple[str, ...]
    aliases_added: tuple[str, ...]
    affected_tag_ids: tuple[str, ...]
    category_base_ids: tuple[str, ...]
    character_link_count: int
    canonical_relationship_ids: tuple[str, ...]
    retired_relationship_ids: tuple[str, ...]
    conflicts: tuple[TagMergeConflict, ...]


@dataclass(frozen=True)
class TagIdentityMergeResult:
    plan: TagIdentityMergePlan
    backup_directory: Path


class TagIdentityMergeService:
    """Merge explicitly selected duplicate tag identities without editing images."""

    def __init__(
        self,
        tags: TagDatabase,
        categories: CategoryRegistry,
        characters: CharacterDatabase | None = None,
        *,
        default_relationship_path: str | Path | None = None,
        user_relationship_path: str | Path | None = None,
    ) -> None:
        self.tags = tags
        self.categories = categories
        self.characters = characters
        self.default_relationship_path = Path(
            default_relationship_path
            or categories.default_path.with_name("relationships.json")
        )
        self.user_relationship_path = Path(
            user_relationship_path or tags.path.with_name("relationships.json")
        )
        self.default_relationships = RelationshipStore(self.default_relationship_path)
        self.user_relationships = RelationshipStore(self.user_relationship_path)
        self._modifier_foundation = FoundationKnowledge(
            categories.default_path.with_name("modifiers.json"),
            categories.default_path.with_name("concepts.json"),
        )
        self._refresh_semantic_authority()
        self._initial_files = self._file_snapshot()

    def _paths(self) -> tuple[Path, ...]:
        paths = [self.tags.path, self.user_relationship_path, self.categories.user_path]
        if self.characters is not None:
            paths.extend((
                self.characters.series_path,
                self.characters.characters_path,
                self.characters.outfits_path,
            ))
        return tuple(paths)

    def _watched_paths(self) -> tuple[Path, ...]:
        return tuple(dict.fromkeys((*self._paths(), self.default_relationship_path)))

    def _file_snapshot(self) -> tuple[bytes | None, ...]:
        return tuple(
            path.read_bytes() if path.exists() else None
            for path in self._watched_paths()
        )

    def _effective_relationships(self) -> dict[str, KnowledgeRelationship]:
        relationships = dict(self.default_relationships.relationships)
        relationships.update(self.user_relationships.relationships)
        return relationships

    def _refresh_semantic_authority(self) -> None:
        effective = self._effective_relationships()
        publish_tag_alias_storage_authority(self.tags,effective.values())
        publish_tag_semantic_authority(self.tags,effective.values())
        publish_reusable_modifier_storage_authority(
            self.tags,
            effective.values(),
            categories=self.categories,
            foundation=self._modifier_foundation,
        )
        publish_scoped_constraint_storage_authority(
            self.tags,effective.values(),
        )

    def _records(self, survivor_id: str, donor_ids) -> tuple[TagRecord, tuple[TagRecord, ...]]:
        survivor = self.tags.tags.get(str(survivor_id))
        donors = tuple(
            self.tags.tags.get(value)
            for value in dict.fromkeys(str(item) for item in donor_ids)
            if value != survivor_id
        )
        if survivor is None:
            raise ValueError("Choose one existing SDXL Tag as the canonical survivor.")
        if not donors or any(record is None for record in donors):
            raise ValueError("Choose at least one other existing SDXL Tag to merge.")
        return survivor, tuple(record for record in donors if record is not None)

    @staticmethod
    def _field_conflict(
        key: str,
        label: str,
        records: tuple[TagRecord, ...],
        getter,
        *,
        include_empty: bool = False,
    ) -> TagMergeConflict | None:
        options = []
        seen = set()
        for record in records:
            value = getter(record)
            if not include_empty and value in (None, "", [], {}, ()):
                continue
            signature = _signature(value)
            if signature in seen:
                continue
            seen.add(signature)
            options.append(TagMergeChoice(record.id, f"{record.display}: {_display_value(value)}"))
        if len(options) <= 1:
            return None
        return TagMergeConflict(key, label, tuple(options))

    def _character_links(self, selected_ids: set[str]):
        if self.characters is None:
            return {}
        result = {}
        for kind, record_kind, records in self._character_record_sets(self.characters):
            linked = [
                record for record in records
                if self.characters.effective_knowledge_tag(record_kind, record.id).get("status") == "reviewed"
                and self.characters.entity_tag_id(record_kind, record.id) in selected_ids
            ]
            if linked:
                result[kind] = linked
        return result

    @staticmethod
    def _character_record_sets(database: CharacterDatabase):
        return (
            ("Character Source", "series", database.series.values()),
            ("Character Identity", "character", database.characters.values()),
            ("Character Outfit", "outfit", database.outfits.values()),
        )

    def _character_reference_count(self, selected_ids: set[str]) -> int:
        if self.characters is None:
            return 0
        return sum(
            1
            for _kind, record_kind, records in self._character_record_sets(self.characters)
            for record in records
            if (
                self.characters.entity_tag_id(record_kind, record.id) in selected_ids
                or selected_ids.intersection(
                    str(value)
                    for value in self.characters.effective_knowledge_tag(
                        record_kind, record.id,
                    ).get("excluded_tag_ids", ())
                    if str(value)
                )
            )
        )

    @staticmethod
    def _relationship_endpoint_key(relationship: KnowledgeRelationship):
        endpoints = tuple((item.kind, item.id) for item in relationship.endpoints)
        if not relationship.directed:
            endpoints = tuple(sorted(endpoints))
        return (
            relationship.kind,
            relationship.directed,
            relationship.scope,
            endpoints,
        )

    @classmethod
    def _relationship_semantic_key(cls, relationship: KnowledgeRelationship):
        details = {
            key: value
            for key, value in relationship.details
            if key not in {
                "canonical_relationship_id",
                "compatibility_projection",
                "migrated_from",
            }
        }
        extras = {
            key: value
            for key, value in relationship.extras.items()
            if key not in {"identity_merge", "legacy_compatibility"}
        }
        return (
            cls._relationship_endpoint_key(relationship),
            _signature(details),
            _signature(extras),
        )

    @staticmethod
    def _merge_audit_extras(
        relationship: KnowledgeRelationship,
        *,
        survivor_id: str,
        donor_ids: tuple[str, ...],
        previous_endpoints: tuple[RelationshipEndpoint, ...],
        action: str,
        superseded_by: str = "",
    ) -> dict[str, Any]:
        extras = copy.deepcopy(relationship.extras)
        previous = extras.get("identity_merge")
        audit: dict[str, Any] = {
            "merged_at": _now(),
            "survivor_id": survivor_id,
            "donor_ids": list(donor_ids),
            "action": action,
            "previous_endpoints": [item.to_dict() for item in previous_endpoints],
        }
        if superseded_by:
            audit["superseded_by_relationship_id"] = superseded_by
        if previous is not None:
            audit["previous_identity_merge"] = copy.deepcopy(previous)
        extras["identity_merge"] = audit
        return extras

    def _remap_relationship(
        self,
        relationship: KnowledgeRelationship,
        *,
        survivor_id: str,
        donor_ids: tuple[str, ...],
        survivor_label: str,
    ) -> tuple[KnowledgeRelationship, bool]:
        donor_set = set(donor_ids)
        if not any(
            endpoint.kind == "tag" and endpoint.id in donor_set
            for endpoint in relationship.endpoints
        ):
            return relationship, False

        original = relationship.endpoints
        remapped = [
            replace(endpoint, id=survivor_id, label=survivor_label)
            if endpoint.kind == "tag" and endpoint.id in donor_set
            else endpoint
            for endpoint in original
        ]
        indexes_by_key: dict[tuple[str, str], list[int]] = {}
        for index, endpoint in enumerate(remapped):
            indexes_by_key.setdefault((endpoint.kind, endpoint.id), []).append(index)
        has_duplicate_endpoints = any(
            len(indexes) > 1 for indexes in indexes_by_key.values()
        )
        retired = len(indexes_by_key) < 2

        if retired:
            for indexes in indexes_by_key.values():
                if len(indexes) <= 1:
                    continue
                keeper = next(
                    (
                        index
                        for index in indexes
                        if original[index].kind == "tag"
                        and original[index].id == survivor_id
                    ),
                    indexes[0],
                )
                for index in indexes:
                    if index == keeper:
                        continue
                    previous = original[index]
                    if previous.kind != "tag" or previous.id not in donor_set:
                        raise ValueError(
                            "Merging would collapse an unsupported relationship endpoint. "
                            "Review that canonical relationship before merging."
                        )
                    donor = self.tags.tags.get(previous.id)
                    remapped[index] = replace(
                        previous,
                        kind="merged_tag",
                        label=previous.label or (donor.display if donor is not None else previous.id),
                    )
        elif has_duplicate_endpoints:
            remapped = [
                endpoint
                for index, endpoint in enumerate(remapped)
                if indexes_by_key[(endpoint.kind, endpoint.id)][0] == index
            ]

        return replace(
            relationship,
            endpoints=tuple(remapped),
            version=relationship.version + 1,
            layer="user",
            source="tag_identity_merge",
            disabled=relationship.disabled or retired,
            extras=self._merge_audit_extras(
                relationship,
                survivor_id=survivor_id,
                donor_ids=donor_ids,
                previous_endpoints=original,
                action=(
                    "retired_collapsed_relationship"
                    if retired
                    else "deduplicated_remapped_endpoints"
                    if has_duplicate_endpoints
                    else "remapped_tag_endpoints"
                ),
            ),
        ), True

    def _candidate_relationships(
        self,
        plan: TagIdentityMergePlan,
    ) -> tuple[
        dict[str, KnowledgeRelationship],
        dict[str, KnowledgeRelationship],
    ]:
        original_effective = self._effective_relationships()
        candidate_user = dict(self.user_relationships.relationships)
        candidate_effective = dict(original_effective)
        changed_ids: set[str] = set()
        for relationship_id, relationship in original_effective.items():
            updated, changed = self._remap_relationship(
                relationship,
                survivor_id=plan.survivor_id,
                donor_ids=plan.donor_ids,
                survivor_label=plan.survivor_name,
            )
            if not changed:
                continue
            candidate_user[relationship_id] = updated
            candidate_effective[relationship_id] = updated
            changed_ids.add(relationship_id)

        active_groups: dict[tuple[Any, ...], list[KnowledgeRelationship]] = {}
        for relationship in candidate_effective.values():
            if relationship.disabled or not relationship.reviewed:
                continue
            active_groups.setdefault(
                self._relationship_endpoint_key(relationship), []
            ).append(relationship)
        for relationships in active_groups.values():
            if len(relationships) <= 1 or not changed_ids.intersection(
                item.id for item in relationships
            ):
                continue
            if len({
                self._relationship_semantic_key(item)
                for item in relationships
            }) > 1:
                ids = ", ".join(sorted(item.id for item in relationships))
                raise ValueError(
                    "Merging would make canonical relationships "
                    f"{ids} overlap while preserving different reviewed details or "
                    "conditions. Review those records in Manage Relationships before merging."
                )
            winner = sorted(
                relationships,
                key=lambda item: (
                    not owns_inline_constraint_projection(item),
                    item.layer != "user",
                    item.id in changed_ids,
                    item.id,
                ),
            )[0]
            for relationship in relationships:
                if relationship.id == winner.id:
                    continue
                original = original_effective[relationship.id]
                retired = replace(
                    relationship,
                    version=original.version + 1,
                    layer="user",
                    source="tag_identity_merge",
                    disabled=True,
                    extras=self._merge_audit_extras(
                        relationship,
                        survivor_id=plan.survivor_id,
                        donor_ids=plan.donor_ids,
                        previous_endpoints=original.endpoints,
                        action="retired_duplicate_relationship",
                        superseded_by=winner.id,
                    ),
                )
                candidate_user[retired.id] = retired
                candidate_effective[retired.id] = retired
                changed_ids.add(retired.id)

        issues = validate_relationships(candidate_effective.values())
        if issues:
            raise ValueError(" ".join(issue.message for issue in issues[:3]))
        return candidate_user, candidate_effective

    @staticmethod
    def _project_remapped_constraints(
        candidate_tags: dict[str, TagRecord],
        *,
        original_relationships: Mapping[str, KnowledgeRelationship],
        candidate_relationships: Mapping[str, KnowledgeRelationship],
        affected_relationship_ids: set[str],
        survivor_id: str,
        donor_ids: tuple[str, ...],
    ) -> None:
        selected_ids = {survivor_id, *donor_ids}
        removed_pairs: set[tuple[str, str]] = set()
        for relationship_id in affected_relationship_ids:
            relationship = original_relationships.get(relationship_id)
            if relationship is None or not owns_inline_constraint_projection(relationship):
                continue
            for first, second in constraint_pairs(relationship):
                mapped = {
                    survivor_id if value in selected_ids else value
                    for value in (first, second)
                }
                if len(mapped) == 2:
                    removed_pairs.add(tuple(sorted(mapped)))
        for first, second in removed_pairs:
            if first in candidate_tags:
                candidate_tags[first].conflicts = [
                    value for value in candidate_tags[first].conflicts if value != second
                ]
            if second in candidate_tags:
                candidate_tags[second].conflicts = [
                    value for value in candidate_tags[second].conflicts if value != first
                ]

        for relationship in candidate_relationships.values():
            pairs = constraint_pairs(relationship)
            if (
                relationship.disabled
                or not relationship.reviewed
                or not owns_inline_constraint_projection(relationship)
                or (
                    relationship.id not in affected_relationship_ids
                    and not pairs.intersection(removed_pairs)
                )
            ):
                continue
            for first, second in pairs:
                if first not in candidate_tags or second not in candidate_tags:
                    raise ValueError(
                        "A remapped Contrary Values relationship references an unavailable SDXL Tag."
                    )

    def _relationship_payload(
        self,
        relationships: Mapping[str, KnowledgeRelationship],
    ) -> dict[str, Any]:
        candidate = RelationshipStore(self.user_relationship_path)
        candidate.schema_version = self.user_relationships.schema_version
        candidate.extras = copy.deepcopy(self.user_relationships.extras)
        candidate.replace(relationships.values())
        return candidate.payload()

    def preview(self, survivor_id: str, donor_ids) -> TagIdentityMergePlan:
        survivor, donors = self._records(str(survivor_id), donor_ids)
        records = (survivor, *donors)
        selected_ids = {record.id for record in records}
        roles = {
            self.categories.effective_semantic_role(record.category) or "ordinary"
            for record in records
        }
        if len(roles) > 1:
            raise ValueError(
                "The selected SDXL Tags have incompatible Character Source, "
                "Character Identity, or Character Outfit roles and cannot share one identity."
            )

        merged_names = [
            value for record in records for value in self.tags.identity_names(record)
            if normalize_tag(value)
        ]
        for other in self.tags.tags.values():
            if other.id in selected_ids:
                continue
            other_names = {
                normalize_tag(value) for value in self.tags.identity_names(other)
            }
            collision = next(
                (value for value in merged_names if normalize_tag(value) in other_names),
                None,
            )
            if collision:
                raise ValueError(
                    f'"{collision}" is also owned by unselected SDXL Tag '
                    f'"{other.display}" ({other.id}). Include or resolve that identity first.'
                )

        conflicts = []
        field_specs = (
            ("category", "Category", lambda record: record.category, True),
            ("concept_id", "Foundation concept", lambda record: record.concept_id, False),
            ("notes", "Notes", lambda record: record.notes.strip(), False),
            (
                "parent_id","Base SDXL Tag",
                lambda record:self.tags.effective_parent_id(record),False,
            ),
        )
        for key, label, getter, include_empty in field_specs:
            conflict = self._field_conflict(
                key, label, records, getter, include_empty=include_empty,
            )
            if conflict:
                conflicts.append(conflict)

        reviewed_sensitivity = tuple(
            record for record in records
            if record.content_sensitivity.classification_status
            in {"reviewed", "intentionally_unclassified"}
        )
        conflict = self._field_conflict(
            "content_sensitivity", "Content sensitivity", reviewed_sensitivity,
            lambda record: record.content_sensitivity.to_dict(),
        )
        if conflict:
            conflicts.append(conflict)
        reviewed_ordering = tuple(record for record in records if record.ordering.reviewed)
        conflict = self._field_conflict(
            "ordering", "Caption ordering", reviewed_ordering,
            lambda record: record.ordering.to_dict(),
        )
        if conflict:
            conflicts.append(conflict)

        for field_name, getter in (
            ("property", lambda record: record.properties),
            ("future field", lambda record: record.extras),
            ("future relationship field", lambda record: record.relation_extras),
        ):
            keys = sorted({key for record in records for key in getter(record)})
            for key in keys:
                conflict = self._field_conflict(
                    f"{field_name}:{key}",
                    f"{field_name.title()} — {key}",
                    records,
                    lambda record, key=key, getter=getter: getter(record).get(key),
                )
                if conflict:
                    conflicts.append(conflict)

        mapped_implies = {
            survivor.id if value in selected_ids else value
            for record in records for value in self.tags.effective_implies(record)
        } - selected_ids
        mapped_conflicts = {
            survivor.id if value in selected_ids else value
            for record in records for value in record.conflicts
        } - selected_ids
        for related_id in sorted(mapped_implies & mapped_conflicts):
            related = self.tags.tags.get(related_id)
            label = related.display if related is not None else related_id
            conflicts.append(TagMergeConflict(
                f"relationship:{related_id}",
                f"Relationship to {label}",
                (
                    TagMergeChoice("implies", "Keep as an additional implied tag"),
                    TagMergeChoice("conflicts", "Keep as a conflicting tag"),
                    TagMergeChoice("omit", "Keep neither relationship"),
                ),
            ))

        character_links = self._character_links(selected_ids)
        for kind, linked in character_links.items():
            if len(linked) > 1:
                conflicts.append(TagMergeConflict(
                    f"character_owner:{kind}",
                    f"{kind} reviewed owner",
                    tuple(
                        TagMergeChoice(record.id, f"Keep {record.name} ({record.id})")
                        for record in sorted(linked, key=lambda item: (item.name.casefold(), item.id))
                    ),
                ))

        affected_tag_ids = tuple(sorted(
            record.id for record in self.tags.tags.values()
            if record.id not in selected_ids and (
                record.parent_id in selected_ids
                or selected_ids.intersection(record.implies)
                or selected_ids.intersection(record.conflicts)
            )
        ))
        effective_relationships = self._effective_relationships()
        category_ids = tuple(sorted({
            *(
                record.id for record in self.categories.categories.values()
                if record.base_tag_id in selected_ids
            ),
            *(
                relationship.source_endpoint.id
                for relationship in effective_relationships.values()
                if (
                    relationship.kind == CATEGORY_BASE
                    and relationship.source_endpoint is not None
                    and relationship.source_endpoint.kind == "category"
                    and any(
                        endpoint.kind == "tag" and endpoint.id in selected_ids
                        for endpoint in relationship.target_endpoints
                    )
                )
            ),
        }))
        canonical_relationship_ids = tuple(sorted(
            relationship.id
            for relationship in effective_relationships.values()
            if any(
                endpoint.kind == "tag" and endpoint.id in selected_ids
                for endpoint in relationship.endpoints
            )
        ))
        aliases = []
        existing = {normalize_tag(survivor.display)}
        survivor_aliases=self.tags.effective_aliases(survivor)
        donor_names=(
            value for donor in donors for value in self.tags.identity_names(donor)
        )
        for value in (*survivor_aliases,*donor_names):
            key = normalize_tag(value)
            if key and key not in existing:
                existing.add(key)
                aliases.append(value)
        plan = TagIdentityMergePlan(
            survivor.id,
            tuple(record.id for record in donors),
            survivor.display,
            tuple(record.display for record in donors),
            tuple(aliases),
            affected_tag_ids,
            category_ids,
            self._character_reference_count(selected_ids),
            canonical_relationship_ids,
            (),
            tuple(conflicts),
        )
        _candidate_user, candidate_effective = self._candidate_relationships(plan)
        original_effective = self._effective_relationships()
        retired_relationship_ids = tuple(sorted(
            relationship_id
            for relationship_id, relationship in candidate_effective.items()
            if relationship.disabled
            and not original_effective[relationship_id].disabled
        ))
        if retired_relationship_ids:
            plan = replace(
                plan,
                canonical_relationship_ids=tuple(sorted({
                    *plan.canonical_relationship_ids,
                    *retired_relationship_ids,
                })),
                retired_relationship_ids=retired_relationship_ids,
            )
        return plan

    @staticmethod
    def _chosen_record(
        conflict_key: str,
        records: tuple[TagRecord, ...],
        getter,
        resolutions: Mapping[str, str],
        *,
        fallback: TagRecord,
    ) -> TagRecord:
        choice = str(resolutions.get(conflict_key) or "")
        if choice:
            selected = next((record for record in records if record.id == choice), None)
            if selected is None:
                raise ValueError(f"The reviewed choice for {conflict_key} is no longer available.")
            return selected
        populated = [record for record in records if getter(record) not in (None, "", [], {}, ())]
        signatures = {_signature(getter(record)) for record in populated}
        if len(signatures) > 1:
            raise ValueError(f"Choose how to resolve {conflict_key} before merging.")
        return populated[0] if populated else fallback

    def _merged_record(
        self,
        plan: TagIdentityMergePlan,
        resolutions: Mapping[str, str],
        tag_records: dict[str, TagRecord],
    ) -> TagRecord:
        survivor = tag_records[plan.survivor_id]
        donors = tuple(tag_records[value] for value in plan.donor_ids)
        records = (survivor, *donors)
        selected_ids = {record.id for record in records}
        merged = copy.deepcopy(survivor)

        category_record = self._chosen_record(
            "category", records, lambda record: record.category, resolutions,
            fallback=survivor,
        )
        merged.category = category_record.category
        concept_record = self._chosen_record(
            "concept_id", records, lambda record: record.concept_id, resolutions,
            fallback=survivor,
        )
        merged.concept_id = concept_record.concept_id
        notes_record = self._chosen_record(
            "notes", records, lambda record: record.notes.strip(), resolutions,
            fallback=survivor,
        )
        merged.notes = notes_record.notes
        parent_record = self._chosen_record(
            "parent_id",records,
            lambda record:self.tags.effective_parent_id(record),resolutions,
            fallback=survivor,
        )
        merged.parent_id = self.tags.effective_parent_id(parent_record) or None
        if merged.parent_id in selected_ids:
            merged.parent_id = None

        sensitivity_record = self._chosen_record(
            "content_sensitivity", records,
            lambda record: (
                record.content_sensitivity.to_dict()
                if record.content_sensitivity.classification_status
                in {"reviewed", "intentionally_unclassified"}
                else {}
            ),
            resolutions,
            fallback=survivor,
        )
        merged.content_sensitivity = copy.deepcopy(sensitivity_record.content_sensitivity)
        ordering_record = self._chosen_record(
            "ordering", records,
            lambda record: record.ordering.to_dict() if record.ordering.reviewed else {},
            resolutions,
            fallback=survivor,
        )
        merged.ordering = copy.deepcopy(ordering_record.ordering)

        aliases = []
        seen_names = {normalize_tag(merged.display)}
        survivor_aliases=self.tags.effective_aliases(survivor)
        donor_names=(
            value for record in donors for value in self.tags.identity_names(record)
        )
        for value in (*survivor_aliases,*donor_names):
            key = normalize_tag(value)
            if key and key not in seen_names:
                seen_names.add(key)
                aliases.append(value)
        merged.aliases = aliases
        merged.seen_count = sum(record.seen_count for record in records)
        first_seen = sorted(record.first_seen for record in records if record.first_seen)
        last_seen = sorted(record.last_seen for record in records if record.last_seen)
        merged.first_seen = first_seen[0] if first_seen else None
        merged.last_seen = last_seen[-1] if last_seen else None
        merged.attributes = list(dict.fromkeys(
            value for record in records for value in record.attributes
        ))

        for field_name, getter, setter in (
            ("property", lambda record: record.properties, lambda value: setattr(merged, "properties", value)),
            ("future field", lambda record: record.extras, lambda value: setattr(merged, "extras", value)),
            ("future relationship field", lambda record: record.relation_extras, lambda value: setattr(merged, "relation_extras", value)),
        ):
            result = {}
            keys = sorted({key for record in records for key in getter(record)})
            for key in keys:
                chosen = self._chosen_record(
                    f"{field_name}:{key}", records,
                    lambda record, key=key, getter=getter: getter(record).get(key),
                    resolutions,
                    fallback=survivor,
                )
                if key in getter(chosen):
                    result[key] = copy.deepcopy(getter(chosen)[key])
            setter(result)

        def remap(values):
            return [
                plan.survivor_id if value in selected_ids else value
                for value in values
                if value not in selected_ids
            ]

        implies = list(dict.fromkeys(
            value for record in records
            for value in remap(self.tags.effective_implies(record))
        ))
        conflicts = list(dict.fromkeys(value for record in records for value in remap(record.conflicts)))
        overlap = set(implies).intersection(conflicts)
        for related_id in sorted(overlap):
            choice = str(resolutions.get(f"relationship:{related_id}") or "")
            if choice not in {"implies", "conflicts", "omit"}:
                raise ValueError(f"Choose how to resolve the relationship to {related_id}.")
            if choice != "implies":
                implies = [value for value in implies if value != related_id]
            if choice != "conflicts":
                conflicts = [value for value in conflicts if value != related_id]
        if merged.parent_id:
            implies = [value for value in implies if value != merged.parent_id]
        if merged.parent_id in conflicts:
            raise ValueError("The selected Base SDXL Tag is also a conflict. Resolve that relationship before merging.")
        merged.implies = implies
        merged.conflicts = conflicts
        merged.provenance = list(dict.fromkeys(
            _signature(value) for record in records for value in record.provenance
        ))
        merged.provenance = [json.loads(value) for value in merged.provenance]
        merged.provenance.append({
            "source": "tag_identity_merge",
            "reviewed_at": _now(),
            "action": "merge_duplicate_tags",
            "survivor_id": plan.survivor_id,
            "donor_ids": list(plan.donor_ids),
            "donor_names": list(plan.donor_names),
        })
        return merged

    @staticmethod
    def _character_payloads(
        database: CharacterDatabase,
        *,
        default_relationships: Iterable[KnowledgeRelationship],
        user_relationships: Iterable[KnowledgeRelationship],
    ) -> dict[Path, dict[str, Any]]:
        default_relationships = tuple(default_relationships)
        user_relationships = tuple(user_relationships)
        return {
            database.series_path: {
                **database._series_extras,
                "schema_version": database._series_schema_version,
                "series": character_record_storage_payloads(
                    "series",
                    (record.to_dict() for record in database.series.values()),
                    default_relationships,
                    user_relationships,
                    projection_fields_by_id=database._projection_fields["series"],
                ),
            },
            database.characters_path: {
                **database._characters_extras,
                "schema_version": database._characters_schema_version,
                "characters": character_record_storage_payloads(
                    "character",
                    (record.to_dict() for record in database.characters.values()),
                    default_relationships,
                    user_relationships,
                    projection_fields_by_id=database._projection_fields["character"],
                ),
            },
            database.outfits_path: {
                **database._outfits_extras,
                "schema_version": database._outfits_schema_version,
                "outfits": character_record_storage_payloads(
                    "outfit",
                    (record.to_dict() for record in database.outfits.values()),
                    default_relationships,
                    user_relationships,
                    projection_fields_by_id=database._projection_fields["outfit"],
                ),
            },
        }

    @staticmethod
    def _category_payload(registry: CategoryRegistry) -> dict[str, Any]:
        records = [record for record in registry.categories.values() if record.source == "user"]
        return {
            **registry.user_extras,
            "schema_version": CategoryRegistry.SCHEMA_VERSION,
            "categories": [
                record.to_dict(
                    include_category_base_projection=not bool(
                        registry._category_base_source_index.get(record.id)
                    ),
                )
                for record in sorted(records, key=lambda item: registry.path_label(item.id).casefold())
            ],
            "disabled_default_ids": sorted(registry.disabled_default_ids),
        }

    def apply(
        self,
        survivor_id: str,
        donor_ids,
        *,
        resolutions: Mapping[str, str],
    ) -> TagIdentityMergeResult:
        admission = self.tags.rule_pair_admission
        with (admission.admit() if admission is not None else nullcontext()):
            return self._apply_admitted(survivor_id, donor_ids, resolutions=resolutions)

    def _apply_admitted(
        self,
        survivor_id: str,
        donor_ids,
        *,
        resolutions: Mapping[str, str],
    ) -> TagIdentityMergeResult:
        plan = self.preview(survivor_id, donor_ids)
        required = {conflict.key for conflict in plan.conflicts}
        missing = sorted(required - {str(key) for key in resolutions})
        if missing:
            raise ValueError("Resolve every reviewed field conflict before merging.")
        if self._file_snapshot() != self._initial_files:
            raise RuntimeError(
                "System Knowledge changed while the duplicate merge was open. "
                "Reopen the preview and review the current records."
            )

        original_effective_relationships = self._effective_relationships()
        candidate_user_relationships, candidate_effective_relationships = (
            self._candidate_relationships(plan)
        )
        candidate_tags = copy.deepcopy(self.tags.tags)
        candidate_categories = copy.deepcopy(self.categories.categories)
        candidate_characters = copy.deepcopy(self.characters) if self.characters is not None else None
        merged = self._merged_record(plan, resolutions, candidate_tags)
        selected_ids = {plan.survivor_id, *plan.donor_ids}
        candidate_tags[plan.survivor_id] = merged
        for donor_id in plan.donor_ids:
            candidate_tags.pop(donor_id, None)

        for record in candidate_tags.values():
            if record.id == plan.survivor_id:
                continue
            if record.id not in plan.affected_tag_ids:
                continue
            if record.parent_id in selected_ids:
                record.parent_id = plan.survivor_id
            record.implies = list(dict.fromkeys(
                plan.survivor_id if value in selected_ids else value
                for value in record.implies
            ))
            record.conflicts = list(dict.fromkeys(
                plan.survivor_id if value in selected_ids else value
                for value in record.conflicts
            ))
            record.implies = [value for value in record.implies if value != record.id]
            record.conflicts = [value for value in record.conflicts if value != record.id]
            if record.parent_id:
                record.implies = [value for value in record.implies if value != record.parent_id]
            if set(record.implies).intersection(record.conflicts):
                raise ValueError(
                    f'Merging would make "{record.display}" both imply and conflict '
                    "with the canonical survivor. Review that record first."
                )
        for category in candidate_categories.values():
            if category.base_tag_id in selected_ids:
                category.base_tag_id = plan.survivor_id
                category.source = "user"

        candidate_db = TagDatabase(self.tags.path)
        candidate_db.tags = candidate_tags
        candidate_db.merge_rules = copy.deepcopy(self.tags.merge_rules)
        candidate_db.removed_tag_ids = set(self.tags.removed_tag_ids).union(plan.donor_ids)
        candidate_db.merged_tag_ids = dict(self.tags.merged_tag_ids)
        for donor_id in plan.donor_ids:
            candidate_db.merged_tag_ids[donor_id] = plan.survivor_id
        for source, target in tuple(candidate_db.merged_tag_ids.items()):
            if target in plan.donor_ids:
                candidate_db.merged_tag_ids[source] = plan.survivor_id
        candidate_db._payload_extras = copy.deepcopy(self.tags._payload_extras)
        candidate_db._rebuild_lookup()
        publish_tag_semantic_authority(
            candidate_db,candidate_effective_relationships.values(),
        )
        candidate_registry = copy.deepcopy(self.categories)
        candidate_registry.categories = candidate_categories
        self._project_remapped_constraints(
            candidate_tags,
            original_relationships=original_effective_relationships,
            candidate_relationships=candidate_effective_relationships,
            affected_relationship_ids=set(plan.canonical_relationship_ids),
            survivor_id=plan.survivor_id,
            donor_ids=plan.donor_ids,
        )
        candidate_alias_store=RelationshipStore(self.user_relationship_path)
        candidate_alias_store.replace(candidate_user_relationships.values())
        candidate_user_relationships,_alias_ids,_alias_keys=stage_tag_alias_changes(
            candidate_db,
            SimpleNamespace(
                default_store=self.default_relationships,
                user_store=candidate_alias_store,
            ),
            (TagAliasChange(
                plan.survivor_id,tuple(merged.aliases),
                source="tag_identity_merge",
                reason="Preserve donor names on the canonical survivor",
            ),),
        )
        candidate_tags=candidate_db.tags
        candidate_effective_relationships = dict(
            self.default_relationships.relationships,
        )
        candidate_effective_relationships.update(candidate_user_relationships)
        publish_tag_alias_storage_authority(
            candidate_db,candidate_effective_relationships.values(),
        )
        candidate_registry.configure_category_base_authority(
            candidate_effective_relationships.values(),
        )
        publish_reusable_modifier_storage_authority(
            candidate_db,
            candidate_effective_relationships.values(),
            categories=candidate_registry,
            foundation=self._modifier_foundation,
        )
        publish_scoped_constraint_storage_authority(
            candidate_db,candidate_effective_relationships.values(),
        )
        changed_tag_relationship_ids = {
            plan.survivor_id,
            *plan.affected_tag_ids,
        }
        for record_id in sorted(changed_tag_relationship_ids):
            record = candidate_tags[record_id]
            candidate_db.validate_tag_relationships(
                record.id,
                parent_id=candidate_db.effective_parent_id(record),
                implies=candidate_db.effective_implies(record),
                conflicts=record.conflicts,
            )

        if candidate_characters is not None:
            reviewed_owners = self._character_links(selected_ids)
            for kind, record_kind, candidate_records in self._character_record_sets(candidate_characters):
                records = reviewed_owners.get(kind, ())
                chosen_id = str(resolutions.get(f"character_owner:{kind}") or "")
                if len(records) == 1:
                    chosen_id = records[0].id
                if len(records) > 1 and chosen_id not in {record.id for record in records}:
                    raise ValueError(f"Choose which {kind} record keeps the reviewed tag link.")
                for record in candidate_records:
                    compatibility_link = record.knowledge_tag
                    link = copy.deepcopy(
                        self.characters.effective_knowledge_tag(record_kind, record.id)
                    )
                    tag_id = str(link.get("tag_id") or "")
                    compatibility_ids = {
                        str(compatibility_link.get("tag_id") or ""),
                        *(
                            str(value)
                            for value in compatibility_link.get("excluded_tag_ids", ())
                            if str(value)
                        ),
                    }
                    exclusions = [
                        plan.survivor_id if str(value) in selected_ids else str(value)
                        for value in link.get("excluded_tag_ids", ())
                        if str(value)
                    ]
                    if (
                        tag_id not in selected_ids
                        and not selected_ids.intersection(
                            str(value)
                            for value in link.get("excluded_tag_ids", ())
                            if str(value)
                        )
                        and not selected_ids.intersection(compatibility_ids)
                    ):
                        continue
                    record.knowledge_tag = link
                    if exclusions:
                        link["excluded_tag_ids"] = list(dict.fromkeys(exclusions))
                    if tag_id not in selected_ids:
                        continue
                    if link.get("status") != "reviewed" or record.id == chosen_id:
                        link["identity_merge"] = {
                            "previous_tag_id": tag_id,
                            "previous_basis": link.get("basis"),
                            "merged_at": _now(),
                        }
                        link["tag_id"] = plan.survivor_id
                        link["basis"] = "tag_identity_merge"
                        link["reviewed_at"] = _now()
                        if plan.survivor_id in exclusions:
                            exclusions = [
                                value for value in exclusions
                                if value != plan.survivor_id
                            ]
                            if exclusions:
                                link["excluded_tag_ids"] = list(dict.fromkeys(exclusions))
                            else:
                                link.pop("excluded_tag_ids", None)
                    else:
                        record.knowledge_tag = {
                            "status": "reviewed_exclusions",
                            "excluded_tag_ids": list(dict.fromkeys((
                                *exclusions, plan.survivor_id,
                            ))),
                            "basis": "tag_identity_merge_conflict",
                            "reviewed_at": _now(),
                            "previous_reviewed_link": copy.deepcopy(link),
                        }

        relationship_payload = self._relationship_payload(
            candidate_user_relationships
        )
        if self._file_snapshot() != self._initial_files:
            raise RuntimeError(
                "System Knowledge changed while the duplicate merge was being prepared. "
                "Reopen the preview and review the current records."
            )

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = self.tags.path.parent / "backups" / "tag-identity-merge" / f"{stamp}_{uuid4().hex[:8]}"
        backup.mkdir(parents=True, exist_ok=False)
        entries = []
        for index, path in enumerate(self._paths()):
            name = f"{index}_{path.name}"
            if path.exists():
                shutil.copy2(path, backup / name)
            entries.append({"target": str(path.resolve()), "backup": name, "existed": path.exists()})
        manifest_path = backup / "recovery.json"
        manifest = {
            "status": "pending",
            "created_at": _now(),
            "survivor_id": plan.survivor_id,
            "donor_ids": list(plan.donor_ids),
            "resolutions": dict(resolutions),
            "files": entries,
        }
        _atomic_json(manifest_path, manifest)
        try:
            _atomic_json(self.tags.path, candidate_db._payload())
            _atomic_json(self.user_relationship_path, relationship_payload)
            _atomic_json(self.categories.user_path, self._category_payload(candidate_registry))
            if candidate_characters is not None:
                for path, payload in self._character_payloads(
                    candidate_characters,
                    default_relationships=self.default_relationships.relationships.values(),
                    user_relationships=candidate_user_relationships.values(),
                ).items():
                    _atomic_json(path, payload)
            reloaded_tags = TagDatabase(self.tags.path)
            reloaded_user_relationships = RelationshipStore(self.user_relationship_path)
            reloaded_categories = CategoryRegistry(
                self.categories.default_path, self.categories.user_path,
            )
            if reloaded_user_relationships.payload() != relationship_payload:
                raise RuntimeError(
                    "Canonical relationships failed semantic read-back verification."
                )
            reloaded_effective_relationships = dict(
                self.default_relationships.relationships
            )
            reloaded_effective_relationships.update(
                reloaded_user_relationships.relationships
            )
            reloaded_categories.configure_category_base_authority(
                reloaded_effective_relationships.values(),
            )
            for relationship in reloaded_effective_relationships.values():
                if any(
                    endpoint.kind == "tag" and endpoint.id in plan.donor_ids
                    for endpoint in relationship.endpoints
                ):
                    raise RuntimeError(
                        "A canonical relationship retained a donor Tag endpoint."
                    )
            if reloaded_tags.resolve_tag_id(plan.survivor_id) != plan.survivor_id:
                raise RuntimeError("The canonical survivor was missing during read-back verification.")
            if any(reloaded_tags.resolve_tag_id(value) != plan.survivor_id for value in plan.donor_ids):
                raise RuntimeError("A donor tombstone failed read-back verification.")
            if any(
                category.base_tag_id in plan.donor_ids
                for category in reloaded_categories.categories.values()
            ):
                raise RuntimeError("A category retained a donor tag reference.")
            if candidate_characters is not None:
                reloaded_characters = CharacterDatabase(candidate_characters.data_dir)
                for record_kind, records in (
                    ("series", reloaded_characters.series.values()),
                    ("character", reloaded_characters.characters.values()),
                    ("outfit", reloaded_characters.outfits.values()),
                ):
                    for record in records:
                        link = reloaded_characters.effective_knowledge_tag(
                            record_kind, record.id,
                        )
                        if str(link.get("tag_id") or "") in plan.donor_ids:
                            raise RuntimeError("A Character Knowledge record retained a donor tag reference.")
                        if set(
                            str(value)
                            for value in link.get("excluded_tag_ids", ())
                            if str(value)
                        ).intersection(plan.donor_ids):
                            raise RuntimeError("A Character Knowledge exclusion retained a donor tag reference.")
        except BaseException:
            for entry in entries:
                target = Path(entry["target"])
                saved = backup / entry["backup"]
                if entry["existed"]:
                    _atomic_bytes(target, saved.read_bytes())
                elif target.exists():
                    target.unlink()
            self.tags.load()
            self.categories.load()
            self.default_relationships.load()
            self.user_relationships.load()
            self._refresh_semantic_authority()
            if self.characters is not None:
                self.characters.load()
            manifest["status"] = "restored"
            manifest["restored_at"] = _now()
            _atomic_json(manifest_path, manifest)
            raise
        manifest["status"] = "complete"
        manifest["completed_at"] = _now()
        _atomic_json(manifest_path, manifest)
        self.tags.load()
        self.categories.load()
        self.default_relationships.load()
        self.user_relationships.load()
        self._refresh_semantic_authority()
        if self.characters is not None:
            self.characters.load()
        return TagIdentityMergeResult(plan, backup)

    @classmethod
    def recover_interrupted(cls, directory: str | Path) -> list[Path]:
        data_root = Path(directory).resolve()
        # This recovery writes tags.json without calling TagDatabase.save.
        with RulePairAdmission(data_root).admit():
            return cls._recover_interrupted_admitted(data_root)

    @classmethod
    def _recover_interrupted_admitted(cls, data_root: Path) -> list[Path]:
        backup_root = data_root / "backups" / "tag-identity-merge"
        restored = []
        if not backup_root.exists():
            return restored
        allowed = {
            "tags.json",
            "relationships.json",
            "categories.json",
            "series.json",
            "characters.json",
            "outfits.json",
        }
        for manifest_path in sorted(backup_root.glob("*/recovery.json")):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("status") != "pending":
                continue
            for entry in manifest.get("files", ()):
                target = Path(entry["target"]).resolve()
                backup = (manifest_path.parent / entry["backup"]).resolve()
                if target.parent != data_root or target.name not in allowed:
                    raise RuntimeError("Tag merge recovery manifest contains an unsafe target.")
                if backup.parent != manifest_path.parent.resolve():
                    raise RuntimeError("Tag merge recovery manifest contains an unsafe backup path.")
                if entry.get("existed"):
                    _atomic_bytes(target, backup.read_bytes())
                elif target.exists():
                    target.unlink()
            manifest["status"] = "restored"
            manifest["restored_at"] = _now()
            _atomic_json(manifest_path, manifest)
            restored.append(manifest_path.parent)
        return restored
