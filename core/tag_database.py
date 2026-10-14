from __future__ import annotations

from .catalog_admission import native_store_api

from .record_layout import WeakReferenceableRecord

import copy
import hashlib
import json
import shutil
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

from .tag_collection import normalize_tag
from .category_registry import exact_category_anchor_ids
from .tag_ordering import ORDER_GROUP_DEFAULT_PRIORITY,TagOrdering,default_order_group
from .automation_rule_conversion import RulePairAdmission


CONTENT_RATINGS = ("general", "sensitive", "questionable", "explicit")


@dataclass(slots=True)
class ContentSensitivity(WeakReferenceableRecord):
    minimum_rating: str = ""
    content_flags: list[str] = field(default_factory=list)
    classification_confidence: float = 0.0
    classification_status: str = "unclassified"
    provenance: list[dict[str, Any]] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result=dict(self.extras)
        result.update({
            "minimum_rating": self.minimum_rating,
            "content_flags": list(self.content_flags),
            "classification_confidence": self.classification_confidence,
            "classification_status": self.classification_status,
            "provenance": list(self.provenance),
        })
        return result

    @classmethod
    def from_dict(cls, data: Any) -> "ContentSensitivity":
        payload = data if isinstance(data, dict) else {}
        known={"minimum_rating","content_flags","classification_confidence","classification_status","provenance"}
        rating = str(payload.get("minimum_rating") or "")
        if rating not in CONTENT_RATINGS:
            rating = ""
        status = str(payload.get("classification_status") or "unclassified")
        if status not in {"unclassified", "intentionally_unclassified", "suggested", "reviewed"}:
            status = "unclassified"
        try:
            confidence = min(1.0, max(0.0, float(payload.get("classification_confidence", 0) or 0)))
        except (TypeError, ValueError):
            confidence = 0.0
        return cls(
            minimum_rating=rating,
            content_flags=[str(v) for v in payload.get("content_flags", []) if str(v).strip()],
            classification_confidence=confidence,
            classification_status=status,
            provenance=[v for v in payload.get("provenance", []) if isinstance(v, dict)],
            extras={key:value for key,value in payload.items() if key not in known},
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(value: str) -> str:
    safe = "_".join(part for part in normalize_tag(value).split() if part)
    return safe or "tag"


@dataclass(slots=True)
class TagRecord(WeakReferenceableRecord):
    id: str
    display: str
    category: str = "uncategorized"
    aliases: list[str] = field(default_factory=list)
    seen_count: int = 0
    first_seen: str | None = None
    last_seen: str | None = None
    parent_id: str | None = None
    attributes: list[str] = field(default_factory=list)
    implies: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    allowed_modifier_groups: list[str] | None = None
    concept_id: str | None = None
    properties: dict[str, str] = field(default_factory=dict)
    notes: str = ""
    provenance: list[dict[str, Any]] = field(default_factory=list)
    content_sensitivity: ContentSensitivity = field(default_factory=ContentSensitivity)
    ordering: TagOrdering = field(default_factory=TagOrdering)
    extras: dict[str, Any] = field(default_factory=dict)
    relation_extras: dict[str, Any] = field(default_factory=dict)
    aliases_projection_present: bool = field(default=True, repr=False, compare=False)
    parent_projection_present: bool = field(default=True, repr=False, compare=False)
    attributes_projection_present: bool = field(default=True, repr=False, compare=False)
    implies_projection_present: bool = field(default=True, repr=False, compare=False)
    conflicts_projection_present: bool = field(default=True, repr=False, compare=False)

    def to_dict(
        self,
        *,
        include_aliases_projection: bool | None = None,
        aliases_projection_values: Iterable[str] | None = None,
        include_parent_projection: bool | None = None,
        include_attributes_projection: bool | None = None,
        attributes_projection_values: Iterable[str] | None = None,
        include_implies_projection: bool | None = None,
        include_conflicts_projection: bool | None = None,
        conflicts_projection_values: Iterable[str] | None = None,
        include_allowed_modifier_groups_projection: bool | None = None,
    ) -> dict[str, Any]:
        relations=dict(self.relation_extras)
        conflict_values = list(
            self.conflicts
            if conflicts_projection_values is None
            else conflicts_projection_values
        )
        include_conflicts = (
            self.conflicts_projection_present or bool(conflict_values)
            if include_conflicts_projection is None
            else bool(include_conflicts_projection)
        )
        if include_conflicts:
            relations["conflicts"] = conflict_values
        else:
            relations.pop("conflicts", None)
        attribute_values = list(
            self.attributes
            if attributes_projection_values is None
            else attributes_projection_values
        )
        include_attributes = (
            self.attributes_projection_present or bool(attribute_values)
            if include_attributes_projection is None
            else bool(include_attributes_projection)
        )
        if include_attributes:
            relations["attributes"] = attribute_values
        else:
            relations.pop("attributes", None)
        include_parent = (
            self.parent_projection_present or self.parent_id is not None
            if include_parent_projection is None
            else bool(include_parent_projection)
        )
        include_implies = (
            self.implies_projection_present or bool(self.implies)
            if include_implies_projection is None
            else bool(include_implies_projection)
        )
        if include_parent:
            relations["parent"] = self.parent_id
        else:
            relations.pop("parent", None)
        if include_implies:
            relations["implies"] = list(self.implies)
        else:
            relations.pop("implies", None)
        include_allowed_modifier_groups = (
            self.allowed_modifier_groups is not None
            if include_allowed_modifier_groups_projection is None
            else bool(include_allowed_modifier_groups_projection)
        )
        if not include_allowed_modifier_groups:
            relations.pop("allowed_modifier_groups", None)
        else:
            relations["allowed_modifier_groups"] = list(
                self.allowed_modifier_groups or ()
            )
        alias_values=list(
            self.aliases
            if aliases_projection_values is None
            else aliases_projection_values
        )
        include_aliases=(
            self.aliases_projection_present or bool(alias_values)
            if include_aliases_projection is None
            else bool(include_aliases_projection)
        )
        result=dict(self.extras)
        result.update({
            "id": self.id,
            "display": self.display,
            "category": self.category,
            "seen_count": self.seen_count,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "concept_id": self.concept_id,
            "properties": dict(self.properties),
            "notes": self.notes,
            "provenance": list(self.provenance),
            "content_sensitivity": self.content_sensitivity.to_dict(),
            "ordering": self.ordering.to_dict(),
            "relations": relations,
        })
        if include_aliases:
            result["aliases"]=alias_values
        else:
            result.pop("aliases",None)
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TagRecord":
        relations = data.get("relations") if isinstance(data.get("relations"), dict) else {}
        known={"id","display","category","aliases","seen_count","first_seen","last_seen","concept_id","properties","notes","provenance","content_sensitivity","ordering","relations"}
        known_relations={"parent","attributes","implies","conflicts","allowed_modifier_groups"}
        category = str(data.get("category") or "uncategorized")
        return cls(
            id=str(data.get("id") or _slug(str(data.get("display", "tag")))),
            display=str(data.get("display") or data.get("id") or "tag"),
            category=category,
            aliases=[str(v) for v in data.get("aliases", []) if str(v).strip()],
            seen_count=int(data.get("seen_count", 0) or 0),
            first_seen=data.get("first_seen"), last_seen=data.get("last_seen"),
            parent_id=relations.get("parent"),
            attributes=[str(v) for v in relations.get("attributes", []) if str(v).strip()],
            implies=[str(v) for v in relations.get("implies", []) if str(v).strip()],
            conflicts=[str(v) for v in relations.get("conflicts", []) if str(v).strip()],
            allowed_modifier_groups=(
                [str(v) for v in relations.get("allowed_modifier_groups", []) if str(v).strip()]
                if "allowed_modifier_groups" in relations else None
            ),
            concept_id=str(data.get("concept_id")) if data.get("concept_id") else None,
            properties={str(k): str(v) for k, v in (data.get("properties") or {}).items()},
            notes=str(data.get("notes") or ""),
            provenance=[v for v in data.get("provenance", []) if isinstance(v, dict)],
            content_sensitivity=ContentSensitivity.from_dict(data.get("content_sensitivity")),
            ordering=TagOrdering.from_dict(data.get("ordering"), category=category),
            extras={key:value for key,value in data.items() if key not in known},
            relation_extras={key:value for key,value in relations.items() if key not in known_relations},
            aliases_projection_present="aliases" in data,
            parent_projection_present="parent" in relations,
            attributes_projection_present="attributes" in relations,
            implies_projection_present="implies" in relations,
            conflicts_projection_present="conflicts" in relations,
        )


@dataclass
class MergeRule:
    id: str
    source_tags: list[str]
    target_tag: str
    scope: str = "any"
    enabled: bool = True
    require_review: bool = True
    description: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    def matches(self, tags: Iterable[str]) -> bool:
        available = {normalize_tag(tag) for tag in tags}
        required = {normalize_tag(tag) for tag in self.source_tags}
        return self.enabled and required.issubset(available)

    def to_dict(self) -> dict[str, Any]:
        result=dict(self.extras)
        result.update({
            "id":self.id,"source_tags":list(self.source_tags),"target_tag":self.target_tag,
            "scope":self.scope,"enabled":self.enabled,"require_review":self.require_review,
            "description":self.description,
        })
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MergeRule":
        known={"id","source_tags","target_tag","scope","enabled","require_review","description"}
        return cls(
            id=str(data.get("id") or _slug(str(data.get("target_tag", "merge")))),
            source_tags=[str(v) for v in data.get("source_tags", []) if str(v).strip()],
            target_tag=str(data.get("target_tag") or "").strip(),
            scope=str(data.get("scope") or "any"), enabled=bool(data.get("enabled", True)),
            require_review=bool(data.get("require_review", True)), description=str(data.get("description") or ""),
            extras={key:value for key,value in data.items() if key not in known},
        )


@dataclass
class RemovedTagArchive:
    """Durable reviewed recovery data for one Tag-removal transaction."""

    id: str
    removed_at: str
    source: str
    reason: str
    removed_tags: list[dict[str, Any]] = field(default_factory=list)
    recovery_kinds: dict[str, str] = field(default_factory=dict)
    related_tags_before: dict[str, dict[str, Any]] = field(default_factory=dict)
    related_tags_after: dict[str, str] = field(default_factory=dict)
    categories_before: dict[str, dict[str, Any]] = field(default_factory=dict)
    categories_after: dict[str, str] = field(default_factory=dict)
    relationships_before: dict[str, dict[str, Any] | None] = field(default_factory=dict)
    relationships_after: dict[str, str] = field(default_factory=dict)
    relationship_file_existed: bool = False
    restored_at: str = ""
    restored_by: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return not self.restored_at

    @property
    def tag_ids(self) -> tuple[str, ...]:
        return tuple(
            str(item.get("id") or "")
            for item in self.removed_tags
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        )

    def tag_record(self, stable_id: str) -> TagRecord | None:
        payload = next(
            (
                item for item in self.removed_tags
                if isinstance(item, dict)
                and str(item.get("id") or "") == str(stable_id)
            ),
            None,
        )
        return TagRecord.from_dict(payload) if payload is not None else None

    def to_dict(self) -> dict[str, Any]:
        result = copy.deepcopy(self.extras)
        result.update({
            "id": self.id,
            "removed_at": self.removed_at,
            "source": self.source,
            "reason": self.reason,
            "removed_tags": copy.deepcopy(self.removed_tags),
            "recovery_kinds": dict(sorted(self.recovery_kinds.items())),
            "related_tags_before": copy.deepcopy(self.related_tags_before),
            "related_tags_after": dict(sorted(self.related_tags_after.items())),
            "categories_before": copy.deepcopy(self.categories_before),
            "categories_after": dict(sorted(self.categories_after.items())),
            "relationships_before": copy.deepcopy(self.relationships_before),
            "relationships_after": dict(sorted(self.relationships_after.items())),
            "relationship_file_existed": self.relationship_file_existed,
            "restored_at": self.restored_at,
            "restored_by": self.restored_by,
        })
        return result

    @classmethod
    def from_dict(cls, data: Any) -> "RemovedTagArchive":
        payload = data if isinstance(data, dict) else {}
        known = {
            "id", "removed_at", "source", "reason", "removed_tags",
            "recovery_kinds", "related_tags_before", "related_tags_after",
            "categories_before", "categories_after", "relationships_before",
            "relationships_after", "relationship_file_existed", "restored_at",
            "restored_by",
        }

        def record_map(key: str) -> dict[str, dict[str, Any]]:
            value = payload.get(key)
            return {
                str(record_id): copy.deepcopy(record)
                for record_id, record in dict(value or {}).items()
                if str(record_id).strip() and isinstance(record, dict)
            }

        relationships_before: dict[str, dict[str, Any] | None] = {}
        for relationship_id, record in dict(
            payload.get("relationships_before") or {}
        ).items():
            if not str(relationship_id).strip():
                continue
            if record is None or isinstance(record, dict):
                relationships_before[str(relationship_id)] = copy.deepcopy(record)
        return cls(
            # Loading legacy recovery data must not invent a fresh identity on
            # every launch. New removal transactions supply explicit IDs.
            id=str(payload.get("id") or "legacy-"+hashlib.sha256(json.dumps(
                payload,sort_keys=True,separators=(",",":"),ensure_ascii=False,
            ).encode("utf-8")).hexdigest()),
            removed_at=str(payload.get("removed_at") or ""),
            source=str(payload.get("source") or ""),
            reason=str(payload.get("reason") or ""),
            removed_tags=[
                copy.deepcopy(item) for item in payload.get("removed_tags", [])
                if isinstance(item, dict) and str(item.get("id") or "").strip()
            ],
            recovery_kinds={
                str(tag_id): str(kind)
                for tag_id, kind in dict(payload.get("recovery_kinds") or {}).items()
                if str(tag_id).strip()
            },
            related_tags_before=record_map("related_tags_before"),
            related_tags_after={
                str(record_id): str(fingerprint)
                for record_id, fingerprint in dict(
                    payload.get("related_tags_after") or {}
                ).items() if str(record_id).strip()
            },
            categories_before=record_map("categories_before"),
            categories_after={
                str(record_id): str(fingerprint)
                for record_id, fingerprint in dict(
                    payload.get("categories_after") or {}
                ).items() if str(record_id).strip()
            },
            relationships_before=relationships_before,
            relationships_after={
                str(record_id): str(fingerprint)
                for record_id, fingerprint in dict(
                    payload.get("relationships_after") or {}
                ).items() if str(record_id).strip()
            },
            relationship_file_existed=bool(
                payload.get("relationship_file_existed",False)
            ),
            restored_at=str(payload.get("restored_at") or ""),
            restored_by=str(payload.get("restored_by") or ""),
            extras={key: copy.deepcopy(value) for key, value in payload.items() if key not in known},
        )


class TagDatabase:
    SCHEMA_VERSION = 11
    ENROLLED_SCHEMA_VERSION = 12
    def __init__(self, path: str | Path, *, rule_pair_admission: RulePairAdmission | None = None) -> None:
        self.path = Path(path); self.tags = {}; self.merge_rules = {}; self.removed_tag_ids=set(); self.removed_tag_archives={}; self.merged_tag_ids={}; self._lookup = {}; self._legacy_conflict_index={}; self._categories=None; self._payload_extras={}
        if rule_pair_admission is not None and not rule_pair_admission.owns(self.path, "knowledge"):
            raise ValueError("Rule conversion admission must bind this exact System Knowledge path.")
        self.rule_pair_admission = rule_pair_admission
        self._display_name_owners: dict[str,tuple[str,...]]={}
        self._inline_alias_owners: dict[str,tuple[str,...]]={}
        self._alias_authority_enabled=False
        self._canonical_alias_owners: dict[str, tuple[str, ...]]={}
        self._canonical_alias_labels: dict[str, str]={}
        self._canonical_aliases_by_owner: dict[str,tuple[tuple[str,str],...]]={}
        self._represented_alias_keys: set[str]=set()
        self._storage_represented_alias_keys: set[str]=set()
        self._semantic_authority_enabled=False
        self._canonical_parent_targets: dict[str,tuple[str,...]]={}
        self._canonical_implies_by_source: dict[str,tuple[str,...]]={}
        self._represented_parent_sources: set[str]=set()
        self._represented_implication_sources: set[str]=set()
        self._scoped_constraint_authority_enabled=False
        self._represented_conflict_targets: dict[str,tuple[str,...]]={}
        self._modifier_composition_authority_enabled=False
        self._modifier_compatibility_values: dict[str,tuple[str,...]]={}
        self._modifier_families_by_source: dict[str,tuple[str,...]]={}
        self._allowed_dimension_authority_enabled=False
        self._represented_allowed_dimension_sources: set[str]=set()
        self.load()

    @native_store_api("path")
    def load(self) -> None:
        self.invalidate_default_identity_context()
        raw=self.path.read_bytes() if self.path.exists() else None
        from .tag_storage_decode import KnowledgeLifecycleCodec, decode_tag_storage
        lifecycle = KnowledgeLifecycleCodec("tag", self.SCHEMA_VERSION, self.ENROLLED_SCHEMA_VERSION)
        data, parsed_tags = (decode_tag_storage(raw.decode("utf-8"), TagRecord.from_dict, lifecycle_codec=lifecycle)
                             if raw is not None else ({"tags": [], "merge_rules": []}, {}))
        data=data if isinstance(data,dict) else {}
        explicit_archive_payloads={}
        for item in data.get("removed_tag_archives", []):
            if not isinstance(item,dict) or not item.get("id"):continue
            archive_id=str(item["id"])
            encoded=json.dumps(item,sort_keys=True,separators=(",",":"),ensure_ascii=False)
            previous=explicit_archive_payloads.get(archive_id)
            if previous is not None and previous!=encoded:
                raise ValueError("Tag recovery history has conflicting duplicate archive IDs; restore a verified copy before saving.")
            explicit_archive_payloads[archive_id]=encoded
        self.tags.clear(); self.merge_rules.clear(); self.removed_tag_ids.clear(); self.removed_tag_archives.clear(); self.merged_tag_ids.clear()
        self._payload_extras={key:value for key,value in data.items() if key not in {"schema_version","tags","merge_rules","removed_tag_ids","removed_tag_archives","merged_tag_ids","knowledge_lifecycle"}}
        self.removed_tag_ids={str(value) for value in data.get("removed_tag_ids",[]) if str(value).strip()}
        legacy_archive_occurrences={}
        explicit_archive_ids=set(explicit_archive_payloads)
        for item in data.get("removed_tag_archives", []):
            if not isinstance(item, dict):
                continue
            archive = RemovedTagArchive.from_dict(item)
            if not item.get("id"):
                # Preserve repeated legacy records instead of collapsing equal
                # payloads; their occurrence order is stable across reloads.
                base=archive.id
                count=legacy_archive_occurrences.get(base,0)+1
                while True:
                    archive.id=base if count==1 else f"{base}-{count}"
                    if archive.id not in explicit_archive_ids and archive.id not in self.removed_tag_archives:break
                    count+=1
                legacy_archive_occurrences[base]=count
            if archive.id:
                self.removed_tag_archives[archive.id] = archive
        self.merged_tag_ids={
            str(source):str(target) for source,target in dict(data.get("merged_tag_ids") or {}).items()
            if str(source).strip() and str(target).strip() and str(source)!=str(target)
        }
        if parsed_tags is not None:
            self.tags.update(parsed_tags)
        else:
            for item in data.get("tags", []):
                if isinstance(item, dict):
                    record = TagRecord.from_dict(item)
                    self.tags[record.id] = record
        for item in data.get("merge_rules", []):
            if isinstance(item, dict):
                rule = MergeRule.from_dict(item)
                if rule.target_tag and rule.source_tags: self.merge_rules[rule.id] = rule
        self._rebuild_lookup()
        if self._categories is not None:self.apply_category_ordering(self._categories)
        self._loaded_bytes=raw
        lifecycle.bind_current(tuple(self.tags), removed_ids=self.removed_tag_ids, redirects=self.merged_tag_ids)
        self._lifecycle_codec = lifecycle

    @property
    def lifecycle_envelope(self):
        return self._lifecycle_codec.envelope

    @property
    def lifecycle_write_hold(self):
        return self._lifecycle_codec.write_hold

    def stage_lifecycle_transitions(self, transitions, *, operation_id, expected_fingerprint):
        """Stage explicit identity decisions for a future qualified native owner.

        This cannot enroll a legacy store, authorize canonical/history writes,
        or replace shared transaction admission and native recovery.
        """
        self._lifecycle_codec.stage(transitions, operation_id=operation_id,
            expected_fingerprint=expected_fingerprint, record_ids=tuple(record.id for record in self.tags.values()),
            removed_ids=self.removed_tag_ids, redirects=self.merged_tag_ids)

    def _payload(self) -> dict[str, Any]:
        from .knowledge_lifecycle import KnowledgeLifecycleError
        if "knowledge_lifecycle" in self._payload_extras:
            raise KnowledgeLifecycleError("Lifecycle authority cannot be introduced through unknown fields.")
        lifecycle_fields = self._lifecycle_codec.fields_for_write(
            tuple(record.id for record in self.tags.values()),
            removed_ids=self.removed_tag_ids, redirects=self.merged_tag_ids,
        )
        if self.lifecycle_envelope is not None and any(key != record.id for key, record in self.tags.items()):
            raise KnowledgeLifecycleError("Tag lookup identity changed without canonical lifecycle admission.")
        payload = dict(self._payload_extras); payload.update({**lifecycle_fields,
            "tags": [
                self._record_payload(r)
                for r in sorted(self.tags.values(), key=lambda r:r.display.casefold())
            ],
            "merge_rules": [r.to_dict() for r in sorted(self.merge_rules.values(), key=lambda r:r.id)],
            "removed_tag_ids": sorted(self.removed_tag_ids),
            "removed_tag_archives": [
                archive.to_dict()
                for archive in sorted(
                    self.removed_tag_archives.values(),
                    key=lambda item: (item.removed_at, item.id),
                )
            ],
            "merged_tag_ids": dict(sorted(self.merged_tag_ids.items()))})
        return payload

    def _record_payload(self, record: TagRecord) -> dict[str, Any]:
        alias_represented=(
            self._alias_authority_enabled
            and (
                bool(self._canonical_aliases_by_owner.get(record.id))
                or any(
                    normalize_tag(value) in self._storage_represented_alias_keys
                    for value in record.aliases
                )
            )
        )
        alias_values=(
            tuple(
                value for value in record.aliases
                if normalize_tag(value) not in self._storage_represented_alias_keys
            )
            if self._alias_authority_enabled else None
        )
        modifier_represented = (
            self._modifier_composition_authority_enabled
            and record.id in self._modifier_families_by_source
        )
        compatibility_values = (
            self._modifier_compatibility_values.get(record.id, ())
            if modifier_represented else None
        )
        allowed_represented = (
            self._allowed_dimension_authority_enabled
            and record.id in self._represented_allowed_dimension_sources
        )
        represented_conflicts = (
            set(self._represented_conflict_targets.get(record.id, ()))
            if self._scoped_constraint_authority_enabled else set()
        )
        conflict_represented = bool(represented_conflicts)
        conflict_values = (
            tuple(
                value for value in record.conflicts
                if str(value) not in represented_conflicts
            )
            if conflict_represented else None
        )
        payload = record.to_dict(
            include_aliases_projection=(
                (
                    bool(alias_values)
                    or (record.aliases_projection_present and not alias_represented)
                )
                if alias_values is not None else None
            ),
            aliases_projection_values=alias_values,
            include_parent_projection=(
                False
                if self._semantic_authority_enabled
                and record.id in self._represented_parent_sources
                else None
            ),
            include_attributes_projection=(
                bool(compatibility_values) if modifier_represented else None
            ),
            attributes_projection_values=compatibility_values,
            include_implies_projection=(
                False
                if self._semantic_authority_enabled
                and record.id in self._represented_implication_sources
                else None
            ),
            include_conflicts_projection=(
                bool(conflict_values) if conflict_represented else None
            ),
            conflicts_projection_values=conflict_values,
            include_allowed_modifier_groups_projection=(
                False if allowed_represented else None
            ),
        )
        relations = payload.get("relations")
        if not isinstance(relations, dict):
            return payload
        if modifier_represented:
            provenance = relations.get("modifier_value_provenance")
            if isinstance(provenance, dict):
                represented_families = set(
                    self._modifier_families_by_source.get(record.id, ())
                )
                retained = {
                    str(family_id): copy.deepcopy(value)
                    for family_id, value in provenance.items()
                    if str(family_id) not in represented_families
                }
                if retained:
                    relations["modifier_value_provenance"] = retained
                else:
                    relations.pop("modifier_value_provenance", None)
        if allowed_represented:
            relations.pop("allowed_modifier_groups_provenance", None)
        if conflict_represented:
            provenance = relations.get("constraint_provenance")
            if isinstance(provenance, list):
                retained = [
                    copy.deepcopy(item)
                    for item in provenance
                    if not isinstance(item, dict)
                    or str(item.get("target_id") or "") not in represented_conflicts
                ]
                if retained:
                    relations["constraint_provenance"] = retained
                else:
                    relations.pop("constraint_provenance", None)
        return payload

    @native_store_api("path")
    def save(self) -> None:
        with (self.rule_pair_admission.admit() if self.rule_pair_admission else nullcontext()):
            self._save_unadmitted()

    def _save_unadmitted(self) -> None:
        self.invalidate_default_identity_context()
        payload = self._payload()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False)+"\n", encoding="utf-8")
        temporary.replace(self.path)
        self._loaded_bytes=self.path.read_bytes()

    @staticmethod
    def _atomic_bytes(path: Path,content: bytes) -> None:
        temporary=path.with_suffix(path.suffix+".restore.tmp"); temporary.write_bytes(content); temporary.replace(path)

    @staticmethod
    def _atomic_json(path: Path,payload: dict[str, Any]) -> None:
        temporary=path.with_suffix(path.suffix+".tmp"); temporary.write_text(json.dumps(payload,indent=2,ensure_ascii=False)+"\n",encoding="utf-8"); temporary.replace(path)

    @native_store_api("path")
    def save_verified(self, *, backup_root: str | Path | None = None) -> Path:
        """Save one tags-file transaction with stale guard, backup, read-back, and rollback."""
        with (self.rule_pair_admission.admit() if self.rule_pair_admission else nullcontext()):
            return self._save_verified_unadmitted(backup_root=backup_root)

    def _save_verified_unadmitted(self, *, backup_root: str | Path | None = None) -> Path:
        self.invalidate_default_identity_context()
        expected=self._payload()
        current=self.path.read_bytes() if self.path.exists() else None
        if current!=getattr(self,"_loaded_bytes",None):
            raise RuntimeError("System Knowledge changed after this SDXL Tag was opened. Reload it and review the current record before saving again.")
        stamp=datetime.now().strftime("%Y%m%d_%H%M%S"); root=Path(backup_root) if backup_root is not None else self.path.parent/"backups"/"tag-autosave"
        backup=root/f"{stamp}_{uuid4().hex[:8]}"; backup.mkdir(parents=True,exist_ok=False)
        backup_name=f"0_{self.path.name}"
        if current is not None:shutil.copy2(self.path,backup/backup_name)
        manifest_path=backup/"recovery.json"; manifest={"status":"pending","created_at":_utc_now(),"files":[{"target":str(self.path.resolve()),"backup":backup_name,"existed":current is not None}]}
        self._atomic_json(manifest_path,manifest)
        try:
            self.save()
            actual=json.loads(self.path.read_text(encoding="utf-8"))
            if actual!=expected:raise RuntimeError("Saved SDXL Tag changes failed read-back verification.")
            manifest["status"]="complete"; self._atomic_json(manifest_path,manifest)
        except BaseException:
            if current is None:
                if self.path.exists():self.path.unlink()
            else:self._atomic_bytes(self.path,current)
            self.load(); manifest["status"]="restored"; self._atomic_json(manifest_path,manifest); raise
        return backup

    @native_store_api("path")
    def delete_merge_rules_verified(self,rule_ids: Iterable[str]) -> tuple[str,...]:
        """Delete one exact merge-rule selection with stale-safe recovery."""
        selected=[]; seen=set()
        for value in rule_ids:
            rule_id=str(value)
            if rule_id and rule_id not in seen:
                seen.add(rule_id); selected.append(rule_id)
        if not selected:return ()
        if any(rule_id not in self.merge_rules for rule_id in selected):
            raise RuntimeError(
                "System Knowledge changed after this selection was made. "
                "Reload and select the current rules."
            )
        before=copy.deepcopy(self.merge_rules)
        try:
            for rule_id in selected:self.merge_rules.pop(rule_id)
            self.save_verified()
        except BaseException:
            self.merge_rules=before
            raise
        return tuple(selected)

    @native_store_api("path")
    def add_merge_rule_verified(
        self,source_tags: Iterable[str],target_tag: str,*,description: str = "",
        require_review: bool = True,intent: str = "training_projection",
    ) -> MergeRule:
        """Create one modern rule and any needed Tags in a verified tags-file write."""
        before_state=copy.deepcopy(self.__dict__)
        try:
            rule=self.add_merge_rule(
                source_tags,target_tag,description=description,
                require_review=require_review,intent=intent,
            )
            self.save_verified()
            return rule
        except BaseException:
            try:self.load()
            except BaseException:
                # A broken/unreadable authority must not leave rejected edits live.
                # The retained loaded bytes keep a later verified write stale-held.
                self.__dict__.clear(); self.__dict__.update(before_state)
            raise

    @native_store_api("path")
    def replace_merge_rule_verified(
        self,rule_id: str,original: MergeRule,source_tags: Iterable[str],target_tag: str,
    ) -> MergeRule:
        """Replace one exact modern rule in place, retaining identity and extras."""
        current=self.merge_rules.get(rule_id)
        if current is None or current.to_dict()!=original.to_dict():
            raise RuntimeError(
                "This System Knowledge rule changed after it was opened. "
                "Reload and review the current rule before saving."
            )
        sources=[str(value).strip() for value in source_tags if str(value).strip()]
        target=str(target_tag).strip()
        if len(sources)<2 or not target:
            raise ValueError("Choose at least two matching SDXL Tags and one result Tag.")
        from .automation_rules import AutomationEngine
        active={
            "variables":{},"combine_rules":[],"wildcard_rules":[],"ignore_rules":[],
            "merge_rules":[{
                "remove":list(rule.source_tags),"replace":rule.target_tag,
                "intent":str(rule.extras.get("intent") or "training_projection"),
                "rule_id":rule.id,
            } for candidate_id,rule in self.merge_rules.items()
              if candidate_id!=rule_id and rule.enabled],
        }
        analysis=AutomationEngine(active).analyze_projection_rule(sources,target)
        if analysis.errors:raise ValueError(" ".join(analysis.errors))
        if analysis.warnings:raise ValueError(" ".join(analysis.warnings))
        if analysis.duplicate:raise ValueError("An active projection rule already covers these Tags.")
        before_state=copy.deepcopy(self.__dict__)
        try:
            for value in [*analysis.sources,target]:
                if "{" not in value and self.find(value) is None:
                    self.add_known_tag(
                        value,category="uncategorized",
                        provenance={"source":"automation_rule"},
                        rebuild_lookup=False,
                    )
            self._rebuild_lookup()
            updated=replace(current,source_tags=list(analysis.sources),target_tag=target)
            self.merge_rules[rule_id]=updated
            self.save_verified()
            return updated
        except BaseException:
            try:self.load()
            except BaseException:
                self.__dict__.clear(); self.__dict__.update(before_state)
            raise

    def _rebuild_lookup(self) -> None:
        self.invalidate_default_identity_context()
        self._relationship_signature_revision=(
            getattr(self,"_relationship_signature_revision",0)+1
        )
        conflict_index: dict[str, set[str]] = {}
        records = sorted(self.tags.values(), key=lambda item: item.id.casefold())
        display_owners: dict[str, set[str]]={}
        for record in records:
            key=normalize_tag(record.display)
            if key:display_owners.setdefault(key,set()).add(record.id)
        self._display_name_owners={
            key:tuple(sorted(owners)) for key,owners in display_owners.items()
        }
        inline_alias_owners: dict[str,set[str]]={}
        for record in records:
            for alias in record.aliases:
                key=normalize_tag(alias)
                if key:inline_alias_owners.setdefault(key,set()).add(record.id)
            for target_id in record.conflicts:
                if target_id == record.id:
                    continue
                conflict_index.setdefault(record.id, set()).add(target_id)
                conflict_index.setdefault(target_id, set()).add(record.id)
        self._inline_alias_owners={
            key:tuple(sorted(owners)) for key,owners in inline_alias_owners.items()
        }
        self._refresh_identity_lookup()
        self._legacy_conflict_index=conflict_index

    def _refresh_identity_lookup(self) -> None:
        """Rebuild name resolution from the already-current ownership indexes."""
        lookup={}
        display_owners=self._display_name_owners
        inline_alias_owners=self._inline_alias_owners
        # Ambiguous canonical displays deliberately resolve to nothing. Their
        # collision remains visible to health review instead of changing meaning
        # with file order.
        for key,owners in display_owners.items():
            if len(owners)==1:lookup[key]=owners[0]
        if self._alias_authority_enabled:
            for key,owners in self._canonical_alias_owners.items():
                if key not in display_owners and len(owners)==1:
                    lookup[key]=owners[0]
            for key,owners in inline_alias_owners.items():
                if (
                    key not in display_owners
                    and key not in self._represented_alias_keys
                    and len(owners)==1
                ):
                    lookup[key]=next(iter(owners))
        else:
            for key,owners in inline_alias_owners.items():
                if key not in display_owners and len(owners)==1:
                    lookup[key]=owners[0]
        self._lookup=lookup

    def _extend_created_record_lookup(self, record) -> None:
        """Extend current indexes for one newly inserted, verified record.

        The caller owns a private candidate Tag map with unchanged live records.
        Name-owner tuples are immutable; detach maps and mutable conflict sets
        before extending them so an abandoned candidate cannot alter live state.
        Updates/deletions continue to use the complete rebuild contract.
        """
        displays=dict(self._display_name_owners)
        aliases=dict(self._inline_alias_owners)
        conflicts={key:set(values) for key,values in self._legacy_conflict_index.items()}
        def add_owner(index,value):
            key=normalize_tag(value)
            if key:
                index[key]=tuple(sorted({*index.get(key,()),record.id}))
        add_owner(displays,record.display)
        for alias in record.aliases:
            add_owner(aliases,alias)
        for target_id in record.conflicts:
            if target_id!=record.id:
                conflicts.setdefault(record.id,set()).add(target_id)
                conflicts.setdefault(target_id,set()).add(record.id)
        self._display_name_owners=displays
        self._inline_alias_owners=aliases
        self._legacy_conflict_index=conflicts
        self._relationship_signature_revision=(
            getattr(self,"_relationship_signature_revision",0)+1
        )
        self._refresh_identity_lookup()

    def configure_alias_authority(
        self,
        alias_owners: Mapping[str, Iterable[str]],
        represented_aliases: Iterable[str],
        alias_labels: Mapping[str, str] | None = None,
        storage_represented_aliases: Iterable[str] | None = None,
    ) -> None:
        """Publish canonical alias ownership and labels to every identity consumer."""
        self._alias_authority_enabled=True
        self._canonical_alias_owners={
            normalize_tag(key):tuple(sorted({resolved for value in owners if str(value)
                                            if (resolved:=self.resolve_tag_id(str(value)))}))
            for key,owners in alias_owners.items() if normalize_tag(key)
        }
        self._canonical_alias_labels={
            normalize_tag(key):str(value)
            for key,value in dict(alias_labels or {}).items()
            if normalize_tag(key) and str(value).strip()
        }
        self._represented_alias_keys={
            normalize_tag(value) for value in represented_aliases if normalize_tag(value)
        }
        self._storage_represented_alias_keys={
            normalize_tag(value)
            for value in (
                represented_aliases
                if storage_represented_aliases is None
                else storage_represented_aliases
            )
            if normalize_tag(value)
        }
        aliases_by_owner: dict[str,list[tuple[str,str]]]={}
        for key,owners in self._canonical_alias_owners.items():
            if key not in self._represented_alias_keys or len(owners)!=1:continue
            aliases_by_owner.setdefault(owners[0],[]).append((
                key,self._canonical_alias_labels.get(key,key),
            ))
        self._canonical_aliases_by_owner={
            owner:tuple(sorted(values,key=lambda item:item[0]))
            for owner,values in aliases_by_owner.items()
        }
        self._refresh_identity_lookup()

    def clear_alias_authority(self) -> None:
        self._alias_authority_enabled=False
        self._canonical_alias_owners={}
        self._canonical_alias_labels={}
        self._canonical_aliases_by_owner={}
        self._represented_alias_keys=set()
        self._storage_represented_alias_keys=set()
        self._rebuild_lookup()

    def effective_aliases(self, record_or_id) -> tuple[str, ...]:
        """Return canonical Other Names with unrepresented inline compatibility.

        Canonical relationship presence suppresses the matching inline value even
        when the relationship is disabled or ambiguous. This keeps presentation
        and search readers from reviving stale compatibility ownership.
        """
        record=(
            self.tags.get(record_or_id)
            if isinstance(record_or_id,str) else record_or_id
        )
        if record is None:return ()
        if not self._alias_authority_enabled:return tuple(record.aliases)
        if not record.aliases and not self._canonical_aliases_by_owner.get(record.id):return ()
        canonical_labels={}
        for key,label in self._canonical_aliases_by_owner.get(record.id,()):
            displays=self._display_name_owners.get(key,())
            if displays and displays!=(record.id,):continue
            if normalize_tag(label)!=normalize_tag(record.display):
                canonical_labels[key]=label
        values=[]
        for alias in record.aliases:
            key=normalize_tag(alias)
            if not key:continue
            if key in canonical_labels:
                values.append(canonical_labels.pop(key)); continue
            if key in self._represented_alias_keys or key in self._display_name_owners:continue
            values.append(alias)
        values.extend(canonical_labels[key] for key in sorted(canonical_labels))
        result=[]; seen=set()
        for value in values:
            key=normalize_tag(value)
            if key and key not in seen:
                seen.add(key); result.append(value)
        return tuple(result)

    def identity_names(self, record_or_id) -> tuple[str, ...]:
        """Return one Tag's canonical display plus its effective Other Names."""
        record=(
            self.tags.get(record_or_id)
            if isinstance(record_or_id,str) else record_or_id
        )
        return (record.display,*self.effective_aliases(record)) if record is not None else ()

    def configure_semantic_authority(
        self,
        parent_targets: Mapping[str, Iterable[str]],
        implication_targets: Mapping[str, Iterable[str]],
        represented_parent_sources: Iterable[str],
        represented_implication_sources: Iterable[str],
    ) -> None:
        """Publish canonical Broader Tag and Also Means authority to consumers."""
        self._semantic_authority_enabled=True
        self._canonical_parent_targets={
            str(source):tuple(dict.fromkeys(
                str(target) for target in targets if str(target)
            ))
            for source,targets in parent_targets.items() if str(source)
        }
        self._canonical_implies_by_source={
            str(source):tuple(dict.fromkeys(
                str(target) for target in targets if str(target)
            ))
            for source,targets in implication_targets.items() if str(source)
        }
        self._represented_parent_sources={
            str(value) for value in represented_parent_sources if str(value)
        }
        self._represented_implication_sources={
            str(value) for value in represented_implication_sources if str(value)
        }

    def clear_semantic_authority(self) -> None:
        self._semantic_authority_enabled=False
        self._canonical_parent_targets={}
        self._canonical_implies_by_source={}
        self._represented_parent_sources=set()
        self._represented_implication_sources=set()

    def configure_scoped_constraint_authority(
        self, represented_targets: Mapping[str, Iterable[str]],
    ) -> None:
        """Publish canonical Contrary Values ownership for schema-v10 storage."""

        self._scoped_constraint_authority_enabled=True
        self._represented_conflict_targets={
            str(source):tuple(dict.fromkeys(
                str(target) for target in targets if str(target)
            ))
            for source,targets in represented_targets.items() if str(source)
        }

    def clear_scoped_constraint_authority(self) -> None:
        self._scoped_constraint_authority_enabled=False
        self._represented_conflict_targets={}

    def configure_modifier_composition_authority(
        self,
        compatibility_values: Mapping[str, Iterable[str]],
        represented_families: Mapping[str, Iterable[str]],
    ) -> None:
        """Publish canonical Modifier Value ownership for schema-v10 storage."""

        self._modifier_composition_authority_enabled=True
        self._modifier_compatibility_values={
            str(source):tuple(dict.fromkeys(
                str(value) for value in values if str(value)
            ))
            for source,values in compatibility_values.items() if str(source)
        }
        self._modifier_families_by_source={
            str(source):tuple(dict.fromkeys(
                str(family) for family in families if str(family)
            ))
            for source,families in represented_families.items() if str(source)
        }

    def clear_modifier_composition_authority(self) -> None:
        self._modifier_composition_authority_enabled=False
        self._modifier_compatibility_values={}
        self._modifier_families_by_source={}

    def configure_allowed_dimension_authority(
        self, represented_sources: Iterable[str],
    ) -> None:
        """Publish canonical Allowed Details ownership for schema-v10 storage."""

        self._allowed_dimension_authority_enabled=True
        self._represented_allowed_dimension_sources={
            str(source) for source in represented_sources if str(source)
        }

    def clear_allowed_dimension_authority(self) -> None:
        self._allowed_dimension_authority_enabled=False
        self._represented_allowed_dimension_sources=set()

    def semantic_parent_targets(self, record_or_id) -> tuple[str, ...]:
        """Return represented canonical parent targets or inline compatibility."""
        record=(
            self.tags.get(record_or_id)
            if isinstance(record_or_id,str) else record_or_id
        )
        if record is None:return ()
        if (
            self._semantic_authority_enabled
            and record.id in self._represented_parent_sources
        ):
            return self._canonical_parent_targets.get(record.id,())
        parent=str(record.parent_id or "")
        return (parent,) if parent else ()

    def semantic_parent_is_ambiguous(self, record_or_id) -> bool:
        """Return whether canonical authority exposes multiple active parents."""
        record=(
            self.tags.get(record_or_id)
            if isinstance(record_or_id,str) else record_or_id
        )
        return bool(
            record is not None
            and self._semantic_authority_enabled
            and record.id in self._represented_parent_sources
            and len(self._canonical_parent_targets.get(record.id,()))>1
        )

    def effective_parent_id(self, record_or_id) -> str:
        """Return one effective Broader Tag, failing closed on ambiguity."""
        targets=self.semantic_parent_targets(record_or_id)
        return targets[0] if len(targets)==1 else ""

    def effective_implies(self, record_or_id) -> tuple[str, ...]:
        """Return effective Also Means targets without reviving represented inline data."""
        record=(
            self.tags.get(record_or_id)
            if isinstance(record_or_id,str) else record_or_id
        )
        if record is None:return ()
        if (
            self._semantic_authority_enabled
            and record.id in self._represented_implication_sources
        ):
            values=self._canonical_implies_by_source.get(record.id,())
        else:values=tuple(str(value) for value in record.implies if str(value))
        parent=self.effective_parent_id(record)
        return tuple(dict.fromkeys(
            str(value) for value in values if str(value) and str(value)!=parent
        ))

    def effective_semantic_ids(self, record_or_id) -> tuple[str, tuple[str, ...]]:
        """Return effective Broader Tag and Also Means stable IDs."""
        return self.effective_parent_id(record_or_id),self.effective_implies(record_or_id)

    def identity_owner_ids(self, value: str) -> tuple[str, ...]:
        """Return every effective live identity owner without guessing ambiguity."""
        key=normalize_tag(value)
        if not key:return ()
        display=self._display_name_owners.get(key,())
        if display:return display
        if self._alias_authority_enabled and key in self._represented_alias_keys:
            return self._canonical_alias_owners.get(key,())
        return self._inline_alias_owners.get(key,())

    def identity_name_keys(self) -> tuple[str, ...]:
        """Return normalized names represented by the effective identity index."""
        keys={
            normalize_tag(value)
            for record in self.tags.values()
            for value in self.identity_names(record)
            if normalize_tag(value)
        }
        return tuple(sorted(keys))

    def legacy_conflict_ids(self, tag_id: str) -> tuple[str, ...]:
        """Return the symmetric compatibility view of inline conflict links."""
        tag_id = str(tag_id)
        values = set(self._legacy_conflict_index.get(tag_id, ()))
        record = self.tags.get(tag_id)
        if record is not None:
            values.update(str(value) for value in record.conflicts if str(value))
        return tuple(sorted(values))

    def name_collisions(self) -> dict[str, list[TagRecord]]:
        names: dict[str, dict[str, TagRecord]] = {}
        candidate_keys={normalize_tag(record.display) for record in self.tags.values()}
        candidate_keys.update(
            normalize_tag(alias)
            for record in self.tags.values() for alias in self.effective_aliases(record)
        )
        for key in candidate_keys:
            for owner_id in self.identity_owner_ids(key):
                record=self.tags.get(owner_id)
                if record is not None:names.setdefault(key,{})[owner_id]=record
        return {
            key: sorted(records.values(), key=lambda item: item.display.casefold())
            for key, records in names.items() if len(records) > 1
        }

    def conflicting_name(self, values: Iterable[str], *, exclude_id: str = "") -> TagRecord | None:
        for value in values:
            for owner_id in self.identity_owner_ids(value):
                if owner_id!=exclude_id and owner_id in self.tags:
                    return self.tags[owner_id]
        return None

    def active_removed_archives(self) -> tuple[RemovedTagArchive, ...]:
        return tuple(
            sorted(
                (
                    archive for archive in self.removed_tag_archives.values()
                    if archive.active
                    and set(archive.tag_ids).intersection(self.removed_tag_ids)
                ),
                key=lambda item: (item.removed_at, item.id),
                reverse=True,
            )
        )

    def removed_archive_for_tag(self, stable_id: str) -> RemovedTagArchive | None:
        target = str(stable_id or "").strip()
        return next(
            (
                archive for archive in self.active_removed_archives()
                if target in archive.tag_ids
            ),
            None,
        )

    def removed_identity_ids(self, value: str) -> tuple[str, ...]:
        """Return every active tombstone whose archived name exactly matches."""
        key = normalize_tag(value)
        if not key:
            return ()
        matches = []
        active_archives=self.active_removed_archives()
        archived_ids={
            stable_id for archive in active_archives for stable_id in archive.tag_ids
        }
        for archive in active_archives:
            for stable_id in archive.tag_ids:
                if stable_id not in self.removed_tag_ids:
                    continue
                record = archive.tag_record(stable_id)
                if record is None:
                    continue
                if key in {
                    normalize_tag(name)
                    for name in (record.display, *record.aliases)
                    if normalize_tag(name)
                }:
                    matches.append(stable_id)
        # Schema-v6 and older tombstones have no complete archived names. Keep
        # their conservative historical slug match until reviewed recovery data
        # is supplied by current shipped defaults or a Knowledge Pack/backup.
        base = _slug(value)
        matches.extend(
            candidate for candidate in sorted(self.removed_tag_ids-archived_ids)
            if candidate == base or (
                candidate.startswith(base + "_")
                and candidate[len(base) + 1:].isdigit()
            )
        )
        return tuple(dict.fromkeys(matches))

    def removed_identity_id(self, value: str) -> str | None:
        """Return one unambiguous recognizable tombstoned stable ID."""
        matches = self.removed_identity_ids(value)
        return matches[0] if len(matches) == 1 else None

    @property
    def default_identity_generation(self) -> int:
        return getattr(self, "_default_identity_generation", 0)

    def invalidate_default_identity_context(self) -> None:
        self._default_identity_context = None
        self._default_identity_generation = self.default_identity_generation + 1

    def bind_default_identity_context(self, context, *, pair_revision: str) -> None:
        """Bind explicit guarded caller evidence; never read tracking here."""
        from .default_tag_identity import DefaultTagIdentityContext, DefaultTagIdentityHold, document_revision
        self._default_identity_context = None
        if not isinstance(context, DefaultTagIdentityContext) or (
            context.tags_identity != str(self.path.absolute())
            or context.tags_revision != document_revision(getattr(self, "_loaded_bytes", None))
            or context.generation != self.default_identity_generation
            or context.revision != pair_revision
        ):
            raise DefaultTagIdentityHold("Tag/tracking identity context is stale or belongs to another participant")
        self._default_identity_context = context

    def require_default_identity_revision(self, pair_revision: str) -> None:
        """Host calls this around a mapped batch; no hidden polling/IO."""
        from .default_tag_identity import DefaultTagIdentityHold
        context = getattr(self, "_default_identity_context", None)
        if context is None or context.revision != pair_revision:
            self.invalidate_default_identity_context()
            raise DefaultTagIdentityHold("Tag/tracking identity context changed; explicitly rebind")

    def effective_identity_projection(self):
        """Detach the admitted runtime graph; never rewrite lifecycle history.

        Unresolved raw chains remain explicit so retired/dangling/cycle holds
        retain their native meaning. Context-only equivalents are included.
        The host must still guard the complete Tags/tracking pair revision.
        """
        from .default_tag_identity import DefaultTagIdentityHold, document_revision
        context = getattr(self, "_default_identity_context", None)
        if context is not None and (
            context.generation != self.default_identity_generation
            or context.tags_revision != document_revision(getattr(self,"_loaded_bytes",None))
            or context.tags_identity != str(self.path.absolute())
        ):
            raise DefaultTagIdentityHold("Effective identity projection context is stale")
        identities = set(self.merged_tag_ids)
        if context is not None:
            identities.update(context.mappings)
        result = {}
        for identity in sorted(identities - self.tags.keys()):
            resolved = self.resolve_tag_id(identity)
            if resolved is not None:
                result[identity] = resolved
            elif identity in self.merged_tag_ids:
                result[identity] = self.merged_tag_ids[identity]
        return tuple(sorted(result.items())), tuple(sorted(self.removed_tag_ids))

    def resolve_tag_id(self, stable_id: str) -> str | None:
        """Resolve native redirects and an explicitly admitted default identity."""
        from .default_tag_identity import resolve_tag_identity, DefaultTagIdentityHold
        context=getattr(self,"_default_identity_context",None)
        if context is not None and context.generation!=self.default_identity_generation:
            raise DefaultTagIdentityHold("Native identity context generation is stale")
        return resolve_tag_identity(stable_id, active_ids=self.tags,
            removed_ids=self.removed_tag_ids, redirects=self.merged_tag_ids,
            context=getattr(self, "_default_identity_context", None))

    def validate_tag_relationships(
        self,
        record_id: str,
        *,
        parent_id: str | None = None,
        implies: Iterable[str] = (),
        conflicts: Iterable[str] = (),
    ) -> tuple[str | None, list[str], list[str]]:
        """Validate stable-ID tag links without mutating reviewed knowledge."""
        if record_id not in self.tags:
            raise ValueError("The SDXL Tag being edited is no longer available.")
        parent = str(parent_id or "") or None
        implied = [str(value) for value in implies]
        conflicting = [str(value) for value in conflicts]
        for label, values in (("Also Means", implied), ("Contrary Values", conflicting)):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} contains the same SDXL Tag more than once.")
        references = [value for value in (parent, *implied, *conflicting) if value]
        missing = [value for value in references if value not in self.tags]
        if missing:
            raise ValueError("A referenced SDXL Tag is no longer available: " + ", ".join(missing[:3]))
        if record_id in references:
            raise ValueError(
                "An SDXL Tag cannot link itself as a Broader Tag, Also Means, or Contrary Value."
            )
        if parent and parent in implied:
            raise ValueError(
                "The Broader Tag is already included under Also Means and must not be added twice."
            )
        overlap = set(conflicting).intersection({value for value in (parent, *implied) if value})
        if overlap:
            names=", ".join(self.tags[value].display for value in sorted(overlap))
            raise ValueError(
                f"A Tag cannot be both Also Means and a Contrary Value: {names}."
            )

        def outgoing(tag_id: str) -> tuple[str, ...]:
            if tag_id == record_id:
                return tuple(value for value in (parent, *implied) if value)
            record=self.tags[tag_id]
            if self.semantic_parent_is_ambiguous(record):
                raise ValueError(
                    f'Existing Tag "{record.display}" has more than one active Broader Tag.'
                )
            effective_parent,effective_implies=self.effective_semantic_ids(record)
            return tuple(
                value for value in (effective_parent,*effective_implies)
                if value in self.tags
            )

        pending=list(outgoing(record_id)); visited=set()
        while pending:
            candidate=pending.pop()
            if candidate==record_id:
                raise ValueError(
                    "This Broader Tag or Also Means link would create a relationship cycle."
                )
            if candidate in visited:continue
            visited.add(candidate); pending.extend(outgoing(candidate))
        return parent,implied,conflicting

    def apply_category_ordering(self, categories) -> None:
        """Refresh inherited defaults without replacing reviewed tag overrides."""
        self._categories=categories
        for record in self.tags.values():
            if record.ordering.reviewed:
                continue
            group,priority,_,_=categories.effective_ordering(record.category)
            record.ordering.group=group; record.ordering.priority=priority

    def effective_base_tag(
        self,
        record_or_id,
        categories=None,
        *,
        derived_anchors: dict[str,str] | None = None,
    ) -> tuple[str, str | None, str]:
        """Return effective base ID, owning category, and resolution kind.

        A tag-specific parent is an explicit override. Otherwise the tag uses
        its category's effective base. Category anchor tags stop the inherited
        chain instead of becoming their own base, and malformed inherited
        cycles resolve to no base rather than entering relationship consumers.
        """
        record=(self.tags.get(record_or_id) if isinstance(record_or_id,str) else record_or_id)
        if record is None:return "",None,"none"
        registry=categories or self._categories
        anchors=derived_anchors
        if anchors is None and registry is not None:
            if hasattr(registry,"exact_category_anchor_ids"):
                anchors=registry.exact_category_anchor_ids()
            elif hasattr(registry,"categories"):
                anchors=exact_category_anchor_ids(
                    registry.categories.values(),self.tags.values(),
                )

        def next_base(item):
            if self.semantic_parent_is_ambiguous(item):
                return "",None,"ambiguous_explicit"
            parent_id=self.effective_parent_id(item)
            if parent_id:
                return parent_id,None,"explicit"
            if registry is None or not hasattr(registry,"effective_base_tag"):
                return "",None,"none"
            if anchors is None:
                base_id,source,policy=registry.effective_base_tag(item.category)
            else:
                base_id,source,policy=registry.effective_base_tag(
                    item.category,derived_anchors=anchors,
                )
            if policy=="ambiguous":
                return "",source,"ambiguous_category_base"
            base_id=str(base_id or "")
            base_id=str(self.resolve_tag_id(base_id) or base_id)
            if base_id==item.id:return "",source,"anchor"
            return base_id,source,"inherited" if base_id else "none"

        base_id,source,kind=next_base(record)
        if not base_id:return "",source,kind
        if base_id not in self.tags:return "",source,"missing_explicit" if kind=="explicit" else "missing_inherited"
        visited={record.id}; current_id=base_id
        while current_id:
            if current_id in visited:return "",source,"cycle"
            visited.add(current_id)
            current=self.tags.get(current_id)
            if current is None:break
            current_id,_,_=next_base(current)
        return base_id,source,kind

    def _default_ordering(self, category: str) -> tuple[str, int]:
        if self._categories is not None:
            group,priority,_,_=self._categories.effective_ordering(category)
            return group,priority
        group=default_order_group(category)
        return group,ORDER_GROUP_DEFAULT_PRIORITY[group]

    def find(self, value: str) -> TagRecord | None:
        rid = self._lookup.get(normalize_tag(value)); return self.tags.get(rid) if rid else None

    def add_known_tag(self, value: str, *, category: str, concept_id: str | None = None,
                      properties: dict[str, str] | None = None, seen_count: int = 0,
                      provenance: dict[str, Any] | None = None,
                      rebuild_lookup: bool = True) -> TagRecord:
        existing = self.find(value); now = _utc_now()
        if existing:
            old_category=existing.category
            existing.category = category or existing.category
            if not existing.ordering.reviewed and existing.category!=old_category:
                existing.ordering.group,existing.ordering.priority=self._default_ordering(existing.category)
            existing.concept_id = concept_id or existing.concept_id
            if properties: existing.properties.update(properties)
            existing.seen_count = max(existing.seen_count, seen_count)
            existing.last_seen = now
            if provenance and provenance not in existing.provenance: existing.provenance.append(provenance)
            return existing
        base = _slug(value); rid = base; counter = 2
        while rid in self.tags: rid = f"{base}_{counter}"; counter += 1
        group,priority=self._default_ordering(category)
        record = TagRecord(rid, value.strip(), category, seen_count=seen_count, first_seen=now, last_seen=now,
                           concept_id=concept_id, properties=dict(properties or {}),
                           provenance=[provenance] if provenance else [],
                           ordering=TagOrdering(group=group,priority=priority))
        self.removed_tag_ids.discard(rid)
        self.tags[rid] = record
        if rebuild_lookup: self._rebuild_lookup()
        return record

    def restore_removed_tag(
        self,
        value: str,
        *,
        stable_id: str,
        recovery_record: TagRecord,
        provenance: dict[str, Any] | None = None,
    ) -> TagRecord:
        """Restore one explicitly reviewed tombstone from a complete source record."""
        text=str(value).strip(); stable_id=str(stable_id).strip()
        if not text or not stable_id:
            raise ValueError("A removed SDXL Tag name and stable identity are required.")
        if stable_id not in self.removed_tag_ids:
            raise ValueError("That removed SDXL Tag identity is no longer available to restore.")
        if self.removed_archive_for_tag(stable_id) is not None:
            raise ValueError(
                "That removed identity has a complete lifecycle archive and must be "
                "restored through the shared multi-file restoration transaction."
            )
        if stable_id in self.tags or stable_id in self.merged_tag_ids:
            raise ValueError("That stable identity is already live or redirects to another tag.")
        recognized_id=self.removed_identity_id(text)
        if recognized_id is not None and recognized_id!=stable_id:
            raise ValueError("The entered name does not uniquely match that removed stable identity.")
        if self.find(text) is not None:
            raise ValueError("That SDXL Tag name already resolves to a live identity.")
        if recovery_record is None or recovery_record.id!=stable_id:
            raise ValueError("Complete reviewed recovery details are not available for that removed identity.")
        if normalize_tag(text) not in {
            normalize_tag(item) for item in (
                recovery_record.display,*recovery_record.aliases,
            )
        }:
            raise ValueError("The entered name does not match the reviewed recovery record.")
        record=copy.deepcopy(recovery_record)
        if provenance and provenance not in record.provenance:
            record.provenance.append(provenance)
        self.removed_tag_ids.remove(stable_id); self.tags[stable_id]=record
        self._rebuild_lookup(); return record

    def ensure_tag(self, value: str, *, category: str = "uncategorized", status: str = "pending") -> TagRecord:
        """Backward-compatible helper. New code should use add_known_tag or DiscoveryRegistry."""
        existing = self.find(value)
        if existing:
            existing.seen_count += 1
            existing.last_seen = _utc_now()
            return existing
        return self.add_known_tag(value, category=category, seen_count=1, provenance={"source":"legacy_ensure_tag"})

    def register_seen_tags(self, values: Iterable[str]) -> list[TagRecord]:
        unknown = []
        for value in values:
            before = self.find(value)
            record = self.ensure_tag(value)
            if before is None:
                unknown.append(record)
        return unknown

    def record_seen(self, value: str) -> TagRecord | None:
        record = self.find(value)
        if record:
            record.seen_count += 1; record.last_seen = _utc_now()
        return record

    def add_merge_rule(
        self,
        source_tags: Iterable[str],
        target_tag: str,
        *,
        description: str = "",
        require_review: bool = True,
        intent: str = "training_projection",
    ) -> MergeRule:
        sources = [v.strip() for v in source_tags if v.strip()]
        if not any("{" in value for value in (*sources,target_tag)):
            from .automation_rules import AutomationEngine
            active={
                "variables":{},"combine_rules":[],"wildcard_rules":[],"ignore_rules":[],
                "merge_rules":[{
                    "remove":list(rule.source_tags),"replace":rule.target_tag,
                    "intent":str(rule.extras.get("intent") or "training_projection"),
                    "rule_id":rule.id,
                } for rule in self.merge_rules.values() if rule.enabled],
            }
            analysis=AutomationEngine(active).analyze_projection_rule(sources,target_tag)
            if analysis.errors:raise ValueError(" ".join(analysis.errors))
            if analysis.warnings:raise ValueError(" ".join(analysis.warnings))
            sources=list(analysis.sources)
        knowledge_changed=False
        for value in [*sources,target_tag]:
            if "{" not in value and self.find(value) is None:
                self.add_known_tag(value,category="uncategorized",provenance={"source":"automation_rule"},rebuild_lookup=False); knowledge_changed=True
        if knowledge_changed: self._rebuild_lookup()
        base = _slug(target_tag); rule_id=f"merge_{base}"; counter=2
        while rule_id in self.merge_rules:
            existing=self.merge_rules[rule_id]
            if {normalize_tag(v) for v in existing.source_tags} == {normalize_tag(v) for v in sources}: return existing
            rule_id=f"merge_{base}_{counter}"; counter+=1
        rule=MergeRule(
            rule_id,sources,target_tag.strip(),description=description,
            require_review=require_review,extras={"intent":intent},
        )
        self.merge_rules[rule.id]=rule
        return rule
