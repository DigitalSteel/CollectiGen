"""Read-only Tag public-field review; never exports a pack or grants clearance.

Reference: tools/public_source.py::sanitize_public_default. This separate review
does not replace that sanitizer. Unresolved values remain in the provisional
in-memory projection; the CLI prints only aggregate, fixed-schema field categories.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
from typing import Any
from urllib.parse import urlsplit


# Deliberately explicit, matching the existing sanitizer's private-key boundary.
PRIVATE_KEYS = frozenset({
    "backup", "backup_path", "candidate_ids", "example_images", "image_paths",
    "local_path", "operator", "operator_notes", "dataset_path", "image_path",
    "prompt", "promoted_at", "raw_evidence", "raw_payload", "reviewed_at",
    "seen_count", "first_seen", "last_seen", "migrated_at", "confirmed_at",
    "pack_created_at",
})
PROVENANCE_HISTORY = frozenset({"action", "previous_category", "previous_display"})
FIELDS = {
    "document": {"schema_version", "source", "tags", "merge_rules", "removed_tag_ids",
                 "removed_tag_archives", "retired_tag_redirects", "merged_tag_ids", "knowledge_lifecycle"},
    "tag": {"id", "display", "category", "concept_id", "properties", "notes",
            "provenance", "content_sensitivity", "ordering", "relations", "aliases",
            "promotion", "retirement_guidance"},
    "properties": {"subject_kind", "count", "color", "state", "pattern", "material", "size", "value"},
    "relations": {"parent", "attributes", "implies", "conflicts", "allowed_modifier_groups"},
    "sensitivity": {"minimum_rating", "content_flags", "classification_confidence",
                    "classification_status", "provenance"},
    "ordering": {"group", "priority", "reviewed", "provenance"},
    "promotion": {"reviewed"},
    "provenance": {"source", "source_version", "research_source", "research_version", "reason",
                   "research_kind", "research_title", "research_url", "research_summary",
                   "reviewed_fields", "classification_confidence", "community_category",
                   "community_count", "tag_groups", "reviewed_relationship", "meaning_reviewed"},
    "guidance": {"schema_version", "reviewed", "reason", "alternatives", "source", "evidence_url"},
}
# Evidence: native serializers/readers, not inferred snapshot key names.
# core/category_registry.py::CategoryRecord.from_dict / CategoryRegistry.load
# core/character_database.py::SeriesRecord / CharacterRecord / OutfitRecord
# core/knowledge_engine.py::FoundationKnowledge.load
# core/knowledge_relationships.py::KnowledgeRelationship.from_dict
# core/tag_alias_relationships.py::load_tag_alias_source
# core/knowledge_lifecycle.py::LifecycleEnvelope.to_bytes / parse_envelope
# core/default_sync.py::sync_promoted_defaults (tracking, not pack authority)
FIELDS.update({
    "category": {"id", "name", "parent_id", "description", "ordering", "color", "color_policy",
                 "base_tag_id", "base_tag_policy", "sensitivity_rating", "sensitivity_policy"},
    "series": {"id", "name", "aliases", "original_style_tags", "knowledge_tag"},
    "character": {"id", "name", "series_id", "aliases", "default_tags", "sdxl_tags", "plain_language",
                  "original_style_tags", "identity_type", "origin_label", "creator", "tag_associations",
                  "base_character_id", "variant_type", "knowledge_tag"},
    "outfit": {"id", "name", "character_ids", "series_id", "aliases", "sdxl_tags", "plain_language",
               "style_tags", "notes", "knowledge_tag"},
    "concept": {"id", "display", "category", "aliases", "allowed_modifiers"},
    "modifier": {"id", "display", "aliases"},
    "relationship": {"id", "version", "kind", "endpoints", "directed", "scope", "label", "reviewed",
                     "source", "details", "layer", "disabled"},
    "endpoint": {"kind", "id", "label"},
    "alias": {"tag_id", "aliases"},
    "merge_rule": {"id", "source_tags", "target_tag", "scope", "enabled", "require_review", "description"},
    "lifecycle": {"schema_version", "namespace_id", "enrollment_id", "entity_kind", "incarnations"},
    "incarnation": {"record_id", "state", "origin", "transition_operation_id", "retirements", "successor"},
    "identity_reference": {"namespace_id", "entity_kind", "record_id", "incarnation_id"},
    "retirement_proof": {"operation_id", "archive_id", "archive_sha256"},
    "tracking": {"schema_version", "records", "tag_lifecycle", "last_sync", "input_fingerprint",
                 "tag_appearance_default_hash"},
    "tag_lifecycle": {"removed_tag_ids", "merged_tag_ids"},
})
FAMILIES = {
    "categories": ("categories", "category"), "characters": ("characters", "character"),
    "series": ("series", "series"), "outfits": ("outfits", "outfit"),
    "concepts": ("concepts", "concept"), "relationships": ("relationships", "relationship"),
    "tag_aliases": ("tags", "alias"), "modifiers": ("groups", "modifier"),
    "default_knowledge_state": (None, "tracking"),
}
for family, (collection, _) in FAMILIES.items():
    if collection:
        FIELDS[family + "_document"] = {"schema_version", collection}
FIELDS["categories_document"].update({"disabled_default_ids", "knowledge_lifecycle"})
FIELDS["modifiers_document"].add("group_priorities")
# These are links/association evidence, not full Tag records. See
# entity_tag_relationships.EffectiveEntityTagLink and KnowledgeRelationship adapters.
FIELDS["entity_tag_link"] = {"tag_id", "tag_ids", "status", "source", "basis"}
FIELDS["entity_tag_link"].add("source_version")
FIELDS["association"] = {"tag", "relationship_kind", "kind", "status", "source",
                         "observations", "sample_count", "rate", "lift", "confidence"}
FIELDS["character"].add("notes")
FIELDS["category"].update({"protected", "semantic_role", "reusable_modifier_family", "promotion"})
FIELDS["character"].update({"promotion", "relationship_review"})
FIELDS["outfit"].update({"promotion", "relationship_review", "variant_type", "legacy_character_fields"})
FIELDS["series"].add("promotion")
FIELDS["merge_rule"].update({"intent", "promotion"})
FIELDS["provenance"].update({"rule", "alias", "basis", "scope", "group", "concept_id", "sources"})
FIELDS["reviewed_relationship"] = {"base_tag_id", "variant_type"}
FIELDS["relationship_review"] = {"status", "source", "source_version", "qualifiers"}
FIELDS["citation_source"] = {"name", "version"}
FIELDS["guidance_alternative"] = {"label", "tag_id"}
FIELDS["alias_authority"] = {"reason"}
for _family in ("characters", "outfits", "series", "tag_aliases"):
    FIELDS[_family + "_document"].add("source")
# Conservative, per-kind detail vocabulary from native adapters. Recognizing a
# key does not validate its value, resolve endpoints or clear its public scope.
RELATIONSHIP_DETAILS = {
    "identity_alias": set(), "specificity": set(), "entailment": set(),
    "modifier_role": set(), "modifier_value": set(),
    "allowed_dimension": {"policy_mode"},
    "category_base": {"policy"},
    "scoped_constraint": {"legacy_rule_id", "canonical_relationship_id", "relation", "severity"},
    "entity_tag": {"status", "basis"}, "character_source": set(),
    "associated_character": {"compatibility_order"}, "other_appearance": set(),
    "version_of": {"variant_type"},
    "species_trait": {"relationship_kind", "kind", "status"},
    "advisory_association": {"relationship_kind", "kind", "status"},
}
for _kind in ("entity_tag", "character_source", "associated_character", "version_of"):
    RELATIONSHIP_DETAILS[_kind].update({"authority_relocation", "compatibility_projection"})
# Schemas emitted/consumed by reviewed_tag_semantics, tag_alias_relationships,
# allowed_dimension_policy, modifier_composition and category_base_relationships.
for _kind in ("specificity", "entailment", "allowed_dimension", "modifier_value", "scoped_constraint"):
    RELATIONSHIP_DETAILS[_kind].update({"default_compiler", "audit_cohort_id", "audit_cohort_ids",
                                      "review_source", "reviewed_authority", "compatibility_projection"})
for _kind in ("specificity", "entailment", "allowed_dimension", "scoped_constraint", "identity_alias", "category_base"):
    RELATIONSHIP_DETAILS[_kind].add("reason")
RELATIONSHIP_DETAILS["identity_alias"].update({"default_compiler", "identity_resolution", "donor_tag_id",
                                             "resolution_cohort_id", "alias_order", "compatibility_projection"})
RELATIONSHIP_DETAILS["allowed_dimension"].update({"canonical_family_id", "modifier_tag_id", "reviewed_family_id",
    "operation", "pattern", "policy_order", "value_scope", "retirement_cohort_id", "retirement_reason"})
RELATIONSHIP_DETAILS["category_base"].update({"compatibility_projection", "member_scope", "reviewed_authority"})
RELATIONSHIP_DETAILS["modifier_role"].update({"decision", "legacy_candidate_fingerprint"})
RELATIONSHIP_DETAILS["modifier_value"].update({"base_tag_id", "modifier_dimension_id", "operation", "tag_name"})
RELATIONSHIP_DETAILS["specificity"].add("dimension")
RELATIONSHIP_DETAILS["scoped_constraint"].add("dimension")
for _kind, _keys in RELATIONSHIP_DETAILS.items():
    FIELDS["details_" + _kind] = _keys

FIELDS["relationship"].update({"compatibility_projection_payload", "alias_authority", "subject_matching", "allow_when", "promotion"})
FIELDS["endpoint"].add("allowed_scope_role")
FIELDS["subject_matching"] = {"schema_version", "single_tags", "count_tags", "multiple_tags"}
FIELDS["allow_when"] = {"logic", "conditions"}
FIELDS["allow_condition"] = {"kind", "tag_ids", "tag_logic", "minimum_subjects", "maximum_subjects"}
# These known native containers are original recovery evidence, not public graph
# authority. Preserve them privately/in memory without recursively reporting old
# records as unexplained active semantics. Never use this as a generic extras rule.
PRIVATE_EVIDENCE = {
    "document": {"removed_tag_archives"},
    "relationship": {"alias_history", "character_relationship_history", "allowed_dimension_history",
        "category_base_history", "modifier_composition_history", "modifier_role_history", "semantic_link_history",
        "identity_merge", "legacy_compatibility", "legacy_modifier_review"},
    "entity_tag_link": {"identity_merge", "previous_reviewed_link"},
    "outfit": {"legacy_character_fields"},
    "merge_rule": {"intent_migration", "overlap_repair"},
}
# Three legacy annotations have no named reader/writer in the inspected current
# core/Qt/knowledge surfaces. Recognition is not permission to discard their text.
# Generic fingerprints/roundtrip still include them: retain exact values.
LEGACY_ANNOTATIONS = {"details_allowed_dimension": {"value_policy"},
                      "details_scoped_constraint": {"members_are"},
                      "relations": {"semantic_provenance"}}
PROVENANCE_OPERATOR_FIELDS = {"category", "before_id", "after_id", "batch_size", "raw_value",
                             "donor_ids", "donor_names", "survivor_id"}
AGGREGATE_FIELDS = {"observations", "sample_count", "rate", "lift", "confidence"}

CONTEXT_PRIVATE = {
    # Entire local sync baselines are operator state; retain shipped lifecycle facts separately.
    "tracking": {"records", "last_sync", "input_fingerprint", "tag_appearance_default_hash"},
    "provenance": PROVENANCE_OPERATOR_FIELDS,
    "alias_authority": {"created_at", "changed_at"},
    "details_allowed_dimension": {"retired_at"},
}
CONTEXT = {"tags": "tag", "properties": "properties", "relations": "relations",
           "content_sensitivity": "sensitivity", "ordering": "ordering",
           "promotion": "promotion", "provenance": "provenance",
           "retirement_guidance": "guidance", "knowledge_tag": "entity_tag_link", "endpoints": "endpoint",
           "tag_associations": "association",
           "reviewed_relationship": "reviewed_relationship", "relationship_review": "relationship_review",
           "alias_authority": "alias_authority", "subject_matching": "subject_matching",
           "allow_when": "allow_when", "conditions": "allow_condition",
           "merge_rules": "merge_rule", "knowledge_lifecycle": "lifecycle",
           "incarnations": "map_incarnations", "retirements": "retirement_proof",
           "successor": "identity_reference", "merged_tag_ids": "map_redirects",
           "group_priorities": "map_priorities", "tag_lifecycle": "tag_lifecycle"}
PROSE = {"notes", "research_summary", "reason", "description", "plain_language", "basis", "retirement_reason"}
CITATIONS = {"source", "source_version", "research_source", "research_version", "research_title", "research_url", "evidence_url"}
OPAQUE_SEMANTICS = {"removed_tag_archives", "knowledge_lifecycle", "tag_lifecycle", "details", "tag_associations"}
SEMANTIC_REVIEW = {"disabled_default_ids", "removed_tag_ids", "merged_tag_ids", "retired_tag_redirects",
                   "sensitivity_rating", "sensitivity_policy", "minimum_rating", "classification_status",
                   "base_tag_id", "base_tag_policy", "parent_id", "base_character_id", "variant_type"}
PRIVATE_TEXT = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Z]:[\\/])|file://|/Users/|/home/|\\\\[A-Za-z]"
    r"|\b[^\s@]+@[^\s@]+\.[a-z]{2,}\b"
    r"|\b(?:api[_ -]?key|password|access[_ -]?token|secret)\s*[:=]", re.I
)
SNAPSHOT_FILES = frozenset(
    [f"data/defaults/{name}.json" for name in
     ("categories", "characters", "community_tags", "concepts", "foundation_tags",
      "modifiers", "outfits", "promoted_tags", "relationships", "series", "tag_aliases")]
    + [f"data/user/{name}.json" for name in
       ("categories", "characters", "default_knowledge_state", "outfits", "relationships", "series", "tags")]
)
TAG_FILES = frozenset({"data/defaults/community_tags.json", "data/defaults/foundation_tags.json",
                       "data/defaults/promoted_tags.json", "data/user/tags.json"})


class ReviewHold(ValueError):
    """Fixed technical diagnostics only: never echo supplied values or prose."""


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReviewHold("duplicate_json_key")
        result[key] = value
    return result


def _decode(raw):
    try:
        return json.loads(raw, object_pairs_hook=_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ReviewHold("nonfinite_json")))
    except (ValueError, UnicodeError) as error:
        raise ReviewHold("invalid_json") from error


@dataclass
class ProjectionReview:
    provisional_projection: dict = field(repr=False)
    records: int
    classifications: Counter
    fields: dict[str, Counter]
    prose_record_counts: dict[str, int] = field(default_factory=dict)

    def summary(self):
        unresolved = Counter()
        for path, counts in self.fields.items():
            if counts["unknown_semantics_review"]:
                scope = next((name for name in ("details", "knowledge_tag", "tag_associations",
                                               "ordering", "provenance") if "." + name in path),
                             "other_metadata")
                unresolved[scope] += counts["unknown_semantics_review"]
        return {"status": "review_required", "publication_clearance": False,
                "classification_contract": "typed_public_field_review_v1",
                "derivative_ready": False,
                "core_holds": {name: self.classifications[name] for name in
                               ("unknown_core_field_hold", "unsupported_relationship_kind_hold",
                                "invalid_typed_semantics_hold") if self.classifications[name]},
                "scope_reviews": {name: self.classifications[name] for name in
                                  ("legacy_annotation_scope_review", "retain_private_recovery_evidence",
                                   "retain_aggregate_semantics_pending_scope_review", "citation_query_review",
                                   "citation_privacy_review", "sensitive_text_review", "prose_authorship_rights_review")
                                  if self.classifications[name]},
                "projection_exported": False, "records": self.records,
                "classifications": dict(sorted(self.classifications.items())),
                "field_categories": {k: dict(sorted(v.items())) for k, v in sorted(self.fields.items())},
                "unresolved_known_containers": dict(sorted(unresolved.items())),
                "prose_candidate_record_counts": dict(sorted(self.prose_record_counts.items())),
                "limits": ["Unresolved values retained in memory, never printed or exported",
                           "Unknown-field holds are conservative pending classification, not proven unsupported runtime semantics",
                           "Prose remains reviewable; no optional-content exclusion is approved or executed",
                           "Not native semantic validation, infringement detection or legal clearance",
                           "Ratings remain exact; source dates/versions are distinct from operator history"]}


def review_document(payload: Any, family: str = "tags") -> ProjectionReview:
    if family != "tags" and family not in FAMILIES:
        raise ReviewHold("unsupported_review_family")
    if not isinstance(payload, dict):
        raise ReviewHold("document_object_required")
    collection, record_context = ("tags", "tag") if family == "tags" else FAMILIES[family]
    document_context = "document" if family == "tags" else family + "_document"
    if collection is None:
        document_context, records = "tracking", 0
    elif family == "modifiers":
        groups = payload.get(collection)
        if not isinstance(groups, dict) or any(not isinstance(v, list) for v in groups.values()):
            raise ReviewHold("modifier_groups_required")
        if any(not isinstance(row, (str, dict)) for rows in groups.values() for row in rows):
            raise ReviewHold("modifier_record_required")
        records = sum(len(rows) for rows in groups.values())
    else:
        rows = payload.get(collection)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ReviewHold("record_collection_required")
        records = len(rows)
    totals, fields, prose_owners = Counter(), {}, {}

    def mark(path, category):
        totals[category] += 1
        fields.setdefault(path, Counter())[category] += 1

    def scan_legacy_text(value, path):
        # The legacy envelope is retained exactly, never interpreted as a rule.
        # Detect sensitive strings without using arbitrary keys as report labels.
        if isinstance(value, str) and PRIVATE_TEXT.search(value):
            mark(path, "sensitive_text_review")
        elif isinstance(value, list):
            for item in value:
                scan_legacy_text(item, path + "[]")
        elif isinstance(value, dict):
            for key, item in value.items():
                scan_legacy_text(key, path + ".$opaque")
                scan_legacy_text(item, path + ".$opaque")

    def visit(value, context, path, owner=None):
        if isinstance(value, list):
            return [visit(v, context, path + "[]", owner) for v in value]
        if not isinstance(value, dict):
            if isinstance(value, str) and PRIVATE_TEXT.search(value):
                mark(path, "sensitive_text_review")
            return deepcopy(value)
        result = {}
        if collection and path == "$." + collection + "[]":
            owner = id(value)  # Internal only: aggregate unique records per field.
        if context == "relationship":
            kind = value.get("kind")
            if not isinstance(kind, str) or kind not in RELATIONSHIP_DETAILS:
                mark(path + ".kind", "unsupported_relationship_kind_hold")
        if context in {"map_modifiers", "map_priorities", "map_incarnations", "map_redirects"}:
            # Native ID/group map keys are meaningful but never safe report labels.
            child = {"map_modifiers": "modifier", "map_incarnations": "incarnation"}.get(context, "opaque")
            for key, item in value.items():
                mark(path + ".$key", "retain_semantic_map_key")
                if PRIVATE_TEXT.search(key):
                    mark(path + ".$key", "sensitive_text_review")
                result[key] = visit(item, child, path + ".$entry", owner)
            return result
        for key, item in value.items():
            known = key in FIELDS.get(context, set())
            private_evidence = key in PRIVATE_EVIDENCE.get(context, set())
            legacy_annotation = key in LEGACY_ANNOTATIONS.get(context, set())
            # Never print dynamic/unknown keys: they themselves can contain private text.
            private = key in PRIVATE_KEYS or key in CONTEXT_PRIVATE.get(context, set())
            public_path = path + "." + (key if known or private or private_evidence or legacy_annotation or key in PROVENANCE_HISTORY else "$unknown")
            if private or (context == "provenance" and key in PROVENANCE_HISTORY):
                mark(public_path, "exclude_observation_or_operator_history")
                continue
            if private_evidence:
                mark(public_path, "retain_private_recovery_evidence")
                result[key] = deepcopy(item)
                continue
            if legacy_annotation:
                mark(public_path, "legacy_annotation_scope_review")
                valid_shape = (isinstance(item, list) and all(isinstance(v, dict) for v in item)
                               if key == "semantic_provenance" else isinstance(item, str))
                if not valid_shape:
                    mark(public_path, "invalid_typed_semantics_hold")
                scan_legacy_text(item, public_path)
                result[key] = deepcopy(item)
                continue
            if not known:
                mark(public_path, "unknown_semantics_review")
                mark(public_path, "unknown_core_field_hold")
            elif key in PROSE and item:
                mark(public_path, "prose_authorship_rights_review")
                if owner is not None:
                    prose_owners.setdefault(public_path, set()).add(owner)
            elif context == "association" and key in AGGREGATE_FIELDS:
                mark(public_path, "retain_aggregate_semantics_pending_scope_review")
            elif key in CITATIONS:
                mark(public_path, "retain_citation_pending_scope_review")
            elif key in OPAQUE_SEMANTICS and item:
                mark(public_path, "native_semantics_review")
            elif key in SEMANTIC_REVIEW:
                mark(public_path, "rating_lifecycle_replacement_review")
            else:
                mark(public_path, "retain_semantics")
            if known and (context in {"entity_tag_link", "association"} or context.startswith("details_")):
                if key == "tag_ids":
                    valid_shape = isinstance(item, list) and all(isinstance(v, str) for v in item)
                elif context == "association" and key in AGGREGATE_FIELDS:
                    try:
                        valid_shape = type(item) in {int, float} and math.isfinite(item) and item >= 0
                    except OverflowError:
                        valid_shape = False
                    if key in {"observations", "sample_count"}:
                        valid_shape = valid_shape and type(item) is int
                    elif key in {"rate", "confidence"}:
                        valid_shape = valid_shape and item <= 1
                else:
                    valid_shape = isinstance(item, str) or (
                        key in {"compatibility_order", "alias_order", "policy_order"} and type(item) is int)
                if not valid_shape:
                    mark(public_path, "invalid_typed_semantics_hold")
            if key in {"research_url", "evidence_url"} and isinstance(item, str) and item:
                try:
                    url = urlsplit(item)
                    if url.query:
                        mark(public_path, "citation_query_review")
                    if url.username or url.password or url.scheme not in {"https", "http"}:
                        mark(public_path, "citation_privacy_review")
                except ValueError:
                    mark(public_path, "citation_privacy_review")
            child = CONTEXT.get(key, "opaque")
            if (key == "source" and isinstance(item, dict)) or key == "sources":
                child = "citation_source"
            if context == "guidance" and key == "alternatives":
                child = "guidance_alternative"
            if context == "relationship" and key == "compatibility_projection_payload":
                child = "entity_tag_link" if value.get("kind") == "entity_tag" else "opaque"
            if context == "relationship" and key == "details":
                kind = value.get("kind")
                child = "details_" + kind if isinstance(kind, str) and kind in RELATIONSHIP_DETAILS else "opaque"
                if not isinstance(item, dict):
                    mark(public_path, "invalid_typed_semantics_hold")
            if key == "knowledge_tag" and not isinstance(item, dict):
                mark(public_path, "invalid_typed_semantics_hold")
            if key == "tag_associations" and (
                    not isinstance(item, list) or any(not isinstance(v, dict) for v in item)):
                mark(public_path, "invalid_typed_semantics_hold")
            if context == document_context and key == collection:
                child = "map_modifiers" if family == "modifiers" else record_context
            result[key] = visit(item, child, public_path, owner)
        return result

    projection = visit(payload, document_context, "$")
    return ProjectionReview(projection, records, totals, fields,
                            {path: len(owners) for path, owners in prose_owners.items()})


class ProjectionReviewV2(ProjectionReview):
    def summary(self):
        result = super().summary()
        result['classification_contract'] = 'typed_public_field_review_v2'
        return result


def review_document_v2(payload: Any, family: str = "tags") -> ProjectionReview:
    """Separate typed recursive privacy review; never compile patterns or stores.

    Legacy families retain v1 behavior. Unknown/new-family values remain exact in
    memory and hold export, including nested extras rather than generic approval.
    """
    if family == "tags" or family in FAMILIES:
        return review_document(payload, family)
    schemas = {
        'consumer_policies': ('policies', {'schema_version', 'layer', 'policies', 'policy_version', 'roadmap_id', 'source_policy_version'},
            {'id', 'version', 'selector', 'consumer', 'mode', 'explanation', 'layer', 'reviewed', 'disabled', 'source', 'conditions'}),
        'patterns': ('patterns', {'schema_version', 'patterns'},
            {'id', 'name', 'regex', 'category', 'enabled', 'auto_apply', 'confidence', 'examples', 'concept_id', 'promotion'}),
        'tag_appearance': ('rules', {'schema_version', 'revision', 'categories', 'tags', 'rules'},
            {'id', 'enabled', 'condition', 'style'}),
    }
    if family not in schemas or type(payload) is not dict:
        raise ReviewHold('unsupported_review_family')
    collection, root_keys, record_keys = schemas[family]
    totals, fields = Counter(), {}
    def mark(path, category):
        totals[category] += 1
        fields.setdefault(path, Counter())[category] += 1
    style = {'accent', 'border', 'background', 'text'}
    nested = {'selector': {'type', 'id'}, 'conditions': {'content_ratings'},
              'condition': {'kind', 'value'}, 'style': style, 'promotion': {'reviewed'}}
    def visit(value, allowed, path, depth=0, expected=None, map_style=False):
        if depth > 64:
            raise ReviewHold('review_depth')
        if expected is not None and not isinstance(value, expected):
            mark(path, 'invalid_typed_semantics_hold')
        if isinstance(value, str):
            if PRIVATE_TEXT.search(value): mark(path, 'sensitive_text_review')
            if value.startswith(('http://', 'https://')):
                parsed = urlsplit(value)
                if parsed.username or parsed.password: mark(path, 'citation_privacy_review')
                if parsed.query: mark(path, 'citation_query_review')
        elif isinstance(value, list):
            for item in value:
                visit(item, allowed, path + '[]', depth + 1, expected=dict if path == '$.' + collection else None)
        elif isinstance(value, dict):
            for key, item in value.items():
                known = key in allowed if allowed is not None else False
                public_path = path + '.' + (key if known else '$unknown')
                if PRIVATE_TEXT.search(key) or PRIVATE_TEXT.search(key + ':'):
                    mark(public_path, 'sensitive_text_review')
                semantic_pattern_category = (family == 'patterns' and path == '$.patterns[]'
                                             and key == 'category' and type(item) is str)
                if key in PRIVATE_KEYS or key in PROVENANCE_OPERATOR_FIELDS and not semantic_pattern_category:
                    mark(public_path, 'exclude_observation_or_operator_history')
                elif not known and not map_style:
                    mark(public_path, 'unknown_core_field_hold')
                else: mark(public_path, 'retain_semantics')
                if key in {'explanation', 'name', 'examples'} and item:
                    mark(public_path, 'prose_authorship_rights_review')
                if known and key in {'id', 'name', 'regex', 'category', 'consumer', 'mode', 'explanation', 'layer', 'source', 'type', 'kind'} and type(item) is not str:
                    mark(public_path, 'invalid_typed_semantics_hold')
                if known and key in {'schema_version', 'revision', 'version', 'policy_version'} and type(item) is not int:
                    mark(public_path, 'invalid_typed_semantics_hold')
                if known and key in {'enabled', 'auto_apply', 'reviewed', 'disabled'} and type(item) is not bool:
                    mark(public_path, 'invalid_typed_semantics_hold')
                if known and key in {'examples', 'content_ratings'} and (type(item) is not list or any(type(x) is not str for x in item)):
                    mark(public_path, 'invalid_typed_semantics_hold')
                if map_style:
                    visit(item, style, path + '.$entry', depth + 1, expected=dict)
                elif path == '$' and key == collection:
                    visit(item, record_keys, public_path, depth + 1, expected=list)
                elif family == 'tag_appearance' and path == '$' and key in {'categories', 'tags'}:
                    visit(item, set(), public_path, depth + 1, expected=dict, map_style=True)
                elif key in nested:
                    visit(item, nested[key], public_path, depth + 1, expected=dict)
                else:
                    # Unknown containers still receive full recursion. They do
                    # not inherit record permissions or become opaque bypasses.
                    visit(item, set(), public_path, depth + 1)
        elif value is not None and type(value) not in (bool, int, float):
            mark(path, 'invalid_typed_semantics_hold')
        if type(value) is float and not math.isfinite(value): mark(path, 'invalid_typed_semantics_hold')
    visit(payload, root_keys, '$')
    rows = payload.get(collection)
    if type(rows) is not list: mark('$.' + collection, 'invalid_typed_semantics_hold')
    return ProjectionReviewV2(deepcopy(payload), len(rows) if type(rows) is list else 0, totals, fields)


def audit_snapshot(root: Path, expected_receipt_sha256: str) -> dict:
    """Inspect only the exact selected eighteen-file capture, never a live catalog."""
    if not re.fullmatch(r"[0-9a-f]{64}", expected_receipt_sha256):
        raise ReviewHold("explicit_receipt_hash_required")
    root = root.absolute()
    for parent in (root, *root.parents):
        if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
            raise ReviewHold("redirected_snapshot_root")
    receipt_path = root / "CATALOG_CAPTURE_RECEIPT.json"
    if receipt_path.is_symlink() or getattr(receipt_path, "is_junction", lambda: False)():
        raise ReviewHold("redirected_receipt")
    receipt_bytes = receipt_path.read_bytes()
    if hashlib.sha256(receipt_bytes).hexdigest() != expected_receipt_sha256:
        raise ReviewHold("receipt_hash_mismatch")
    receipt = _decode(receipt_bytes)
    inputs = receipt.get("inputs") if isinstance(receipt, dict) else None
    if not isinstance(inputs, dict) or set(inputs) != SNAPSHOT_FILES:
        raise ReviewHold("exact_eighteen_file_capture_required")
    results, total_bytes, identities = [], 0, {}
    for relative in sorted(inputs):
        path = root.joinpath(*PurePosixPath(relative).parts)
        for parent in (path, *path.parents):
            if parent == root:
                break
            if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
                raise ReviewHold("redirected_capture_member")
        expected = inputs[relative]
        if not isinstance(expected, dict) or type(expected.get("bytes")) is not int:
            raise ReviewHold("invalid_member_inventory")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        if size != expected["bytes"] or digest.hexdigest() != expected.get("sha256"):
            raise ReviewHold("capture_member_hash_or_size_mismatch")
        total_bytes += size
        raw = path.read_bytes()
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest.hexdigest():
            raise ReviewHold("capture_changed_before_review")
        family = "tags" if relative in TAG_FILES else Path(relative).stem
        payload = _decode(raw)
        result = review_document(payload, family).summary()
        result.update({"file": relative, "family": family})
        collection = "tags" if family == "tags" else FAMILIES[family][0]
        if collection and family != "modifiers":
            key = "tag_id" if family == "tag_aliases" else "id"
            values = [row.get(key) for row in payload[collection]]
            valid = [value for value in values if isinstance(value, str) and value.strip()]
            identities[relative] = set(valid)
            result["identity_review"] = {
                "missing_or_invalid": len(values) - len(valid),
                "duplicate_occurrences": len(valid) - len(set(valid)),
                "not_native_identity_validation": True,
            }
        results.append(result)
    overlaps = []
    for family in ("categories", "characters", "outfits", "series", "relationships"):
        defaults, user = f"data/defaults/{family}.json", f"data/user/{family}.json"
        overlaps.append({"family": family, "same_id_overlap": len(identities[defaults] & identities[user])})
    return {"status": "review_required", "publication_clearance": False,
            "source_files_verified": len(inputs), "source_bytes_verified": total_bytes,
            "tag_layers": [r for r in results if r["family"] == "tags"],
            "other_family_reviews": [r for r in results if r["family"] != "tags"],
            "other_family_files": len(inputs) - len(TAG_FILES),
            "field_review_files": len(results),
            "overlay_review": {"same_id_overlaps": overlaps, "effective_catalog_computed": False,
                "native_rules": [
                    "default_sync.load_effective_tag_defaults: foundation, community, promoted; later identity/name match retains first ID; alias collisions require review",
                    "default_sync._with_reviewed_tag_aliases: reviewed alias source supplies complete alias lists",
                    "default_sync.sync_promoted_defaults: field-wise local override/baseline tracking; user corrections preserved",
                    "category_registry.CategoryRegistry.load: disabled defaults filtered with protected-role exceptions; user IDs override, protected identity retained",
                    "relationship_store.load_effective_relationship_registry: user ID replaces default; Tag endpoints resolve historical redirects without rewriting evidence",
                ],
                "remaining": "Native precedence, reference closure, lifecycle proof and recipient compatibility qualification required"},
            "layer_counts_are_not_effective_counts": True, "source_writes": False,
            "pack_exported": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--expected-receipt-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        report = audit_snapshot(args.snapshot, args.expected_receipt_sha256)
    except (OSError, ReviewHold) as error:
        print(json.dumps({"status": "held", "reason": str(error) if isinstance(error, ReviewHold) else "input_unavailable"}))
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 0  # inspection succeeded, explicitly not a publication pass


if __name__ == "__main__":
    raise SystemExit(main())
