from __future__ import annotations

import copy
import json
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping

from .allowed_dimension_policy import (
    SCOPE_ROLE_KEY,
    SCOPE_ROLE_SELECTION,
    allowed_dimension_none_relationship_id,
    allowed_dimension_relationship_id,
)
from .category_registry import CategoryRegistry
from .default_sync import (
    load_tag_default_compilation_source,
    resolve_effective_tag_defaults,
)
from .knowledge_relationships import (
    ALLOWED_DIMENSION,
    ALLOWED_DIMENSION_STATE,
    ENTAILMENT,
    IDENTITY_ALIAS,
    MODIFIER_VALUE,
    NO_ALLOWED_DIMENSIONS,
    SCOPED_CONSTRAINT,
    SPECIFICITY,
    KnowledgeRelationship,
    RelationshipEndpoint,
)
from .modifier_composition import modifier_composition_relationship_id
from .tag_collection import normalize_tag
from .tag_alias_relationships import tag_alias_relationship_id
from .tag_database import CONTENT_RATINGS, TagDatabase
from .tag_semantic_relationships import tag_semantic_relationship_id


REVIEWED_TAG_SEMANTICS_COMPILER = "reviewed_tag_semantic_audit_v1"
REVIEWED_TAG_SEMANTICS_SOURCE = "CollectiGen reviewed Tag semantic audit"
REVIEWED_TAG_IDENTITY_MERGE_COMPILER = "reviewed_tag_identity_merges_v1"
REVIEWED_TAG_IDENTITY_MERGE_SOURCE = "CollectiGen reviewed Tag identity reconciliation"
REVIEWED_TAG_IDENTITY_RESOLUTION_SCHEMA = 1
PLACEHOLDER_NOTES = frozenset(
    (
        "",
        "Community-confirmed reusable vocabulary.",
        "Community-confirmed reusable character identity.",
    )
)


@dataclass(frozen=True)
class ReviewedTagSemanticCompilation:
    payload: dict[str, Any]
    cohort_ids: tuple[str, ...]
    decision_count: int
    addition_count: int
    note_change_count: int
    category_change_count: int
    hold_count: int
    identity_review_count: int
    merge_candidate_count: int
    approved_merge_count: int
    retirement_count: int


@dataclass(frozen=True)
class RankedTagSemanticCandidate:
    tag: str
    observation_count: int


def rank_reviewed_tag_semantic_candidates(
    tags: Iterable[Mapping[str, Any]],
    audits: Iterable[Mapping[str, Any]],
    wiki_items: Iterable[Mapping[str, Any]],
    *,
    limit: int | None = 50,
) -> tuple[RankedTagSemanticCandidate, ...]:
    """Rank unresolved exact-source Tags for the next bounded review cohort."""
    if limit is not None and limit < 1:
        raise ValueError("Reviewed Tag semantic candidate limit must be positive.")
    reviewed = {
        normalize_tag(str(decision.get("tag") or ""))
        for audit in audits
        for decision in audit.get("decisions", ())
        if isinstance(decision, Mapping)
    }
    exact: dict[str, int] = {}
    for item in wiki_items:
        title = normalize_tag(str(item.get("title") or ""))
        if (
            not title
            or item.get("is_deleted")
            or not str(item.get("body") or "").strip()
        ):
            continue
        if title in exact:
            raise ValueError(
                f"Reviewed Tag semantic ranking found duplicate exact wiki page {title!r}."
            )
        exact[title] = int(item.get("post_count") or 0)
    candidates: dict[str, RankedTagSemanticCandidate] = {}
    for item in tags:
        name = normalize_tag(str(item.get("display") or ""))
        if (
            not name
            or name in reviewed
            or str(item.get("category") or "uncategorized") != "uncategorized"
            or str(item.get("notes") or "").strip() not in PLACEHOLDER_NOTES
            or name not in exact
        ):
            continue
        if name in candidates:
            raise ValueError(
                f"Reviewed Tag semantic ranking found duplicate effective Tag {name!r}."
            )
        candidates[name] = RankedTagSemanticCandidate(name, exact[name])
    ranked = tuple(
        sorted(
            candidates.values(),
            key=lambda candidate: (-candidate.observation_count, candidate.tag),
        )
    )
    return ranked if limit is None else ranked[:limit]


def _read_payload(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object.")
    return value


def load_reviewed_tag_identity_resolutions(
    path: str | Path,
) -> tuple[dict[str, Any], ...]:
    """Load the deliberate disposition for each prior merge-review pair."""
    payload = _read_payload(Path(path))
    if payload.get("schema_version") != REVIEWED_TAG_IDENTITY_RESOLUTION_SCHEMA:
        raise ValueError("Reviewed Tag identity resolutions use an unsupported schema.")
    if str(payload.get("roadmap_id") or "") != "KM-04":
        raise ValueError("Reviewed Tag identity resolutions require roadmap ID KM-04.")
    cohort_id = str(payload.get("cohort_id") or "").strip()
    reviewed_at = str(payload.get("reviewed_at") or "").strip()
    if not cohort_id or not reviewed_at:
        raise ValueError("Reviewed Tag identity resolutions require cohort provenance.")

    resolutions = payload.get("resolutions")
    if not isinstance(resolutions, list) or not resolutions:
        raise ValueError("Reviewed Tag identity resolutions require a non-empty list.")
    result: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for item in resolutions:
        if not isinstance(item, dict):
            raise ValueError("Reviewed Tag identity resolution entries must be objects.")
        tags = tuple(normalize_tag(str(value or "")) for value in item.get("tags", ()))
        if len(tags) != 2 or any(not value for value in tags) or tags[0] == tags[1]:
            raise ValueError("Reviewed Tag identity resolutions require two distinct Tags.")
        pair = tuple(sorted(tags))
        if pair in seen_pairs:
            raise ValueError(f"Reviewed Tag identity resolution pair is duplicated: {pair!r}.")
        seen_pairs.add(pair)

        decision = str(item.get("decision") or "").strip()
        if decision not in {
            "merge_approved",
            "keep_separate",
            "compatibility_only",
        }:
            raise ValueError(
                f"Reviewed Tag identity pair {pair!r} has invalid resolution {decision!r}."
            )
        canonical = normalize_tag(str(item.get("canonical") or ""))
        compatibility_tag = normalize_tag(str(item.get("compatibility_tag") or ""))
        if decision == "merge_approved":
            if canonical not in tags or compatibility_tag:
                raise ValueError(
                    f"Approved merge {pair!r} requires one canonical endpoint only."
                )
        elif decision == "compatibility_only":
            if compatibility_tag not in tags or canonical:
                raise ValueError(
                    f"Compatibility disposition {pair!r} requires one compatibility endpoint only."
                )
        elif canonical or compatibility_tag:
            raise ValueError(
                f"Keep-separate disposition {pair!r} cannot select an endpoint."
            )
        reason = str(item.get("reason") or "").strip()
        if len(reason) < 30 or len(reason) > 600:
            raise ValueError(
                f"Reviewed Tag identity pair {pair!r} needs a concise resolution reason."
            )
        result.append(
            {
                "tags": tags,
                "decision": decision,
                **({"canonical": canonical} if canonical else {}),
                **(
                    {"compatibility_tag": compatibility_tag}
                    if compatibility_tag
                    else {}
                ),
                "reason": reason,
                "cohort_id": cohort_id,
                "reviewed_at": reviewed_at,
            }
        )
    return tuple(result)


def reviewed_tag_identity_merge_plan(
    source_tags: Iterable[Mapping[str, Any]],
    resolution_path: str | Path,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return approved donor-ID redirects and donor-name compatibility aliases."""
    exact = _exact_tag_index(source_tags)
    redirects: dict[str, str] = {}
    aliases: dict[str, str] = {}
    endpoint_decisions: dict[str, str] = {}
    for resolution in load_reviewed_tag_identity_resolutions(resolution_path):
        records = tuple(_resolved_record(exact, value) for value in resolution["tags"])
        for record in records:
            stable_id = str(record.get("id") or "")
            prior = endpoint_decisions.get(stable_id)
            decision = str(resolution["decision"])
            if prior is not None and prior != decision:
                raise ValueError(
                    f"Reviewed Tag identity {stable_id!r} has incompatible dispositions."
                )
            endpoint_decisions[stable_id] = decision
        if resolution["decision"] != "merge_approved":
            continue
        canonical_name = normalize_tag(str(resolution["canonical"]))
        survivor = _resolved_record(exact, canonical_name)
        survivor_id = str(survivor.get("id") or "")
        donor = next(
            record
            for record in records
            if str(record.get("id") or "") != survivor_id
        )
        donor_id = str(donor.get("id") or "")
        if not donor_id or not survivor_id or donor_id == survivor_id:
            raise ValueError(
                f"Approved merge for {resolution['tags']!r} does not identify two stable IDs."
            )
        prior_target = redirects.get(donor_id)
        if prior_target is not None and prior_target != survivor_id:
            raise ValueError(
                f"Approved merge donor {donor_id!r} has two canonical targets."
            )
        redirects[donor_id] = survivor_id
        aliases[normalize_tag(str(donor.get("display") or ""))] = survivor_id
    donor_ids = set(redirects)
    invalid_targets = sorted(donor_ids.intersection(redirects.values()))
    if invalid_targets:
        raise ValueError(
            "Reviewed Tag identity merges contain donor-to-donor chains: "
            + ", ".join(invalid_targets[:10])
        )
    return dict(sorted(redirects.items())), dict(sorted(aliases.items()))


def build_reviewed_tag_identity_merge_aliases(
    tags: TagDatabase,
    source_tags: Iterable[Mapping[str, Any]],
    resolution_path: str | Path,
) -> tuple[KnowledgeRelationship, ...]:
    """Compile removed donor names as canonical aliases of their live survivor."""
    source_records = tuple(copy.deepcopy(dict(item)) for item in source_tags)
    exact = _exact_tag_index(source_records)
    expected: dict[str, KnowledgeRelationship] = {}
    for resolution in load_reviewed_tag_identity_resolutions(resolution_path):
        if resolution["decision"] != "merge_approved":
            continue
        survivor = _resolved_record(exact, resolution["canonical"])
        survivor_id = str(survivor.get("id") or "")
        donor = next(
            _resolved_record(exact, value)
            for value in resolution["tags"]
            if str(_resolved_record(exact, value).get("id") or "") != survivor_id
        )
        donor_id = str(donor.get("id") or "")
        live_id = tags.resolve_tag_id(donor_id)
        live = tags.tags.get(str(live_id or ""))
        if live is None or live.id != tags.resolve_tag_id(survivor_id):
            raise ValueError(
                f"Approved Tag merge {donor_id!r} does not resolve to its reviewed survivor."
            )
        alias = str(donor.get("display") or "").strip()
        relationship = KnowledgeRelationship(
            id=tag_alias_relationship_id(alias),
            kind=IDENTITY_ALIAS,
            endpoints=(
                RelationshipEndpoint("alias_text", normalize_tag(alias), alias),
                RelationshipEndpoint("tag", live.id, live.display),
            ),
            directed=True,
            scope="any",
            label="Merged Tag name",
            reviewed=True,
            source=REVIEWED_TAG_IDENTITY_MERGE_SOURCE,
            details=(
                ("default_compiler", REVIEWED_TAG_IDENTITY_MERGE_COMPILER),
                ("identity_resolution", "merge_approved"),
                ("donor_tag_id", donor_id),
                ("resolution_cohort_id", str(resolution["cohort_id"])),
                ("reviewed_at", str(resolution["reviewed_at"])),
                ("reason", str(resolution["reason"])),
            ),
            layer="default",
        )
        if relationship.id in expected:
            raise ValueError(
                f"Approved Tag merges create duplicate donor alias {alias!r}."
            )
        expected[relationship.id] = relationship
    return tuple(expected[key] for key in sorted(expected))


def load_reviewed_tag_semantic_audits(audit_dir: str | Path) -> tuple[dict[str, Any], ...]:
    root = Path(audit_dir)
    payloads = tuple(_read_payload(path) for path in sorted(root.glob("*.json")))
    if not payloads:
        raise ValueError(f"No reviewed Tag semantic audit files were found in {root}.")
    cohort_ids: set[str] = set()
    reviewed_tags: set[str] = set()
    reviewed_holds: set[tuple[str, str, tuple[str, ...]]] = set()
    reviewed_identity_pairs: set[tuple[str, str]] = set()
    reviewed_constraint_ids: set[str] = set()
    reviewed_allowed_dimension_retirements: set[str] = set()
    for payload in payloads:
        if int(payload.get("schema_version", 0) or 0) != 1:
            raise ValueError("Reviewed Tag semantic audits must use schema version 1.")
        cohort_id = str(payload.get("cohort_id") or "").strip()
        if not cohort_id or cohort_id in cohort_ids:
            raise ValueError(f"Reviewed Tag semantic audit cohort ID is missing or duplicated: {cohort_id!r}.")
        cohort_ids.add(cohort_id)
        if not str(payload.get("reviewed_at") or "").strip():
            raise ValueError(f"Reviewed Tag semantic audit {cohort_id!r} has no review time.")
        source = payload.get("research_source")
        if not isinstance(source, dict) or not str(source.get("version") or "").strip():
            raise ValueError(f"Reviewed Tag semantic audit {cohort_id!r} has no versioned research source.")
        for retirement in payload.get("allowed_dimension_retirements", ()):
            if not isinstance(retirement, dict):
                raise ValueError(
                    f"Reviewed Tag semantic audit {cohort_id!r} has a malformed "
                    "Allowed Details retirement."
                )
            relationship_id = str(
                retirement.get("relationship_id") or ""
            ).strip()
            tag = normalize_tag(str(retirement.get("tag") or ""))
            family = str(retirement.get("family") or "").strip()
            expected_scope = str(
                retirement.get("expected_value_scope") or ""
            ).strip()
            reason = str(retirement.get("reason") or "").strip()
            if (
                not relationship_id
                or relationship_id in reviewed_allowed_dimension_retirements
                or not tag
                or not family
                or expected_scope not in {"", "unreviewed"}
                or len(reason) < 20
                or len(reason) > 600
                or "\n" in reason
                or "\r" in reason
            ):
                raise ValueError(
                    f"Reviewed Tag semantic audit {cohort_id!r} has an invalid "
                    f"Allowed Details retirement {relationship_id!r}."
                )
            reviewed_allowed_dimension_retirements.add(relationship_id)
        for decision in payload.get("decisions", ()):
            if not isinstance(decision, dict):
                raise ValueError(f"Reviewed Tag semantic audit {cohort_id!r} has a malformed decision.")
            tag = normalize_tag(str(decision.get("tag") or ""))
            notes = str(decision.get("notes") or "").strip()
            category = str(decision.get("category") or "").strip()
            decision_reviewed_at = str(decision.get("reviewed_at") or "").strip()
            if "reviewed_at" in decision and not decision_reviewed_at:
                raise ValueError(
                    "Reviewed Tag semantic audit decision for "
                    f"{tag!r} has an invalid review time."
                )
            if not tag or tag in reviewed_tags:
                raise ValueError(f"Reviewed Tag semantic audit decision is missing or duplicates {tag!r}.")
            if notes in PLACEHOLDER_NOTES or len(notes) < 20 or len(notes) > 600:
                raise ValueError(f"Reviewed Tag semantic audit note for {tag!r} is not a useful concise summary.")
            if "[[" in notes or "\n" in notes or "\r" in notes:
                raise ValueError(f"Reviewed Tag semantic audit note for {tag!r} contains copied markup or line breaks.")
            if not category:
                raise ValueError(f"Reviewed Tag semantic audit decision for {tag!r} has no Category.")
            research_basis = decision.get("research_basis")
            if research_basis is not None:
                if not isinstance(research_basis, dict):
                    raise ValueError(
                        f"Reviewed Tag semantic audit decision for {tag!r} has a malformed research basis."
                    )
                kind = str(research_basis.get("kind") or "").strip()
                name = str(research_basis.get("name") or "").strip()
                version = str(research_basis.get("version") or "").strip()
                summary = str(research_basis.get("summary") or "").strip()
                if kind not in {
                    "reviewed_local_concept",
                    "reviewed_external_evidence",
                    "reviewed_multi_source_evidence",
                } or not name or not version:
                    raise ValueError(
                        f"Reviewed Tag semantic audit decision for {tag!r} has an incomplete per-decision research basis."
                    )
                if len(summary) < 20 or len(summary) > 600 or "\n" in summary or "\r" in summary:
                    raise ValueError(
                        f"Reviewed Tag semantic audit decision for {tag!r} needs a concise evidence summary."
                    )
            if decision.get("add"):
                stable_id = str(decision.get("id") or "").strip()
                if not stable_id:
                    raise ValueError(
                        f"Reviewed missing-Tag addition {tag!r} has no stable ID."
                    )
                aliases = tuple(
                    normalize_tag(str(value or ""))
                    for value in decision.get("aliases", ())
                )
                if any(not value for value in aliases) or len(aliases) != len(set(aliases)):
                    raise ValueError(
                        f"Reviewed missing-Tag addition {tag!r} has invalid aliases."
                    )
                if tag in aliases:
                    raise ValueError(
                        f"Reviewed missing-Tag addition {tag!r} repeats its canonical name as an alias."
                    )
                if decision.get("add_aliases"):
                    raise ValueError(
                        f"Reviewed missing-Tag addition {tag!r} must declare its complete aliases list instead of add_aliases."
                    )
            elif decision.get("aliases"):
                raise ValueError(
                    f"Reviewed existing Tag {tag!r} must use add_aliases instead of replacing its alias list."
                )
            canonical_name = str(decision.get("canonical_name") or "").strip()
            if canonical_name:
                canonical_identity = normalize_tag(canonical_name)
                if (
                    decision.get("add")
                    or not canonical_identity
                    or canonical_identity == tag
                    or "\n" in canonical_name
                    or "\r" in canonical_name
                ):
                    raise ValueError(
                        f"Reviewed Tag {tag!r} has an invalid canonical-name correction."
                    )
            added_aliases = tuple(
                normalize_tag(str(value or ""))
                for value in decision.get("add_aliases", ())
            )
            if (
                any(not value for value in added_aliases)
                or len(added_aliases) != len(set(added_aliases))
                or tag in added_aliases
            ):
                raise ValueError(
                    f"Reviewed existing Tag {tag!r} has invalid added aliases."
                )
            removed_aliases = tuple(
                normalize_tag(str(value or ""))
                for value in decision.get("remove_aliases", ())
            )
            if (
                any(not value for value in removed_aliases)
                or len(removed_aliases) != len(set(removed_aliases))
                or tag in removed_aliases
                or set(removed_aliases) & set(added_aliases)
            ):
                raise ValueError(
                    f"Reviewed existing Tag {tag!r} has invalid removed aliases."
                )
            if decision.get("add") and removed_aliases:
                raise ValueError(
                    f"Reviewed missing-Tag addition {tag!r} cannot remove aliases."
                )
            removed_inline_implies = tuple(
                normalize_tag(str(value or ""))
                for value in decision.get("remove_inline_implies", ())
            )
            if (
                any(not value for value in removed_inline_implies)
                or len(removed_inline_implies) != len(set(removed_inline_implies))
                or tag in removed_inline_implies
            ):
                raise ValueError(
                    f"Reviewed Tag {tag!r} has invalid removed inline implications."
                )
            if decision.get("add") and removed_inline_implies:
                raise ValueError(
                    f"Reviewed missing-Tag addition {tag!r} cannot remove legacy inline implications."
                )
            removed_inline_conflicts = tuple(
                normalize_tag(str(value or ""))
                for value in decision.get("remove_inline_conflicts", ())
            )
            if (
                any(not value for value in removed_inline_conflicts)
                or len(removed_inline_conflicts) != len(set(removed_inline_conflicts))
                or tag in removed_inline_conflicts
            ):
                raise ValueError(
                    f"Reviewed Tag {tag!r} has invalid removed inline conflicts."
                )
            if decision.get("add") and removed_inline_conflicts:
                raise ValueError(
                    f"Reviewed missing-Tag addition {tag!r} cannot remove legacy inline conflicts."
                )
            removed_inline_parent = normalize_tag(
                str(decision.get("remove_inline_parent") or "")
            )
            if removed_inline_parent == tag:
                raise ValueError(
                    f"Reviewed Tag {tag!r} has an invalid removed inline parent."
                )
            if decision.get("add") and removed_inline_parent:
                raise ValueError(
                    f"Reviewed missing-Tag addition {tag!r} cannot remove a legacy inline parent."
                )
            replaced_inline_parent = decision.get("replace_inline_parent")
            if replaced_inline_parent is not None:
                if (
                    not isinstance(replaced_inline_parent, dict)
                    or set(replaced_inline_parent) != {"from", "to"}
                ):
                    raise ValueError(
                        f"Reviewed Tag {tag!r} has a malformed inline-parent replacement."
                    )
                old_parent = normalize_tag(
                    str(replaced_inline_parent.get("from") or "")
                )
                new_parent = normalize_tag(
                    str(replaced_inline_parent.get("to") or "")
                )
                if (
                    not new_parent
                    or old_parent == tag
                    or new_parent == tag
                    or old_parent == new_parent
                    or removed_inline_parent
                    or decision.get("add")
                ):
                    raise ValueError(
                        f"Reviewed Tag {tag!r} has an invalid inline-parent replacement."
                    )
            replaced_inline_allowed_groups = decision.get(
                "replace_inline_allowed_modifier_groups"
            )
            if replaced_inline_allowed_groups is not None:
                if (
                    not isinstance(replaced_inline_allowed_groups, dict)
                    or set(replaced_inline_allowed_groups)
                    != {"from", "to", "expected_provenance"}
                ):
                    raise ValueError(
                        f"Reviewed Tag {tag!r} has a malformed inline Allowed Details replacement."
                    )
                old_groups = tuple(
                    str(value or "").strip()
                    for value in replaced_inline_allowed_groups.get("from", ())
                )
                new_groups = tuple(
                    str(value or "").strip()
                    for value in replaced_inline_allowed_groups.get("to", ())
                )
                expected_provenance = replaced_inline_allowed_groups.get(
                    "expected_provenance"
                )
                if (
                    not isinstance(replaced_inline_allowed_groups.get("from"), list)
                    or not isinstance(replaced_inline_allowed_groups.get("to"), list)
                    or any(not value for value in (*old_groups, *new_groups))
                    or len(old_groups) != len(set(old_groups))
                    or len(new_groups) != len(set(new_groups))
                    or old_groups == new_groups
                    or not isinstance(expected_provenance, dict)
                    or not expected_provenance
                    or any(
                        not str(key or "").strip() or not str(value or "").strip()
                        for key, value in expected_provenance.items()
                    )
                    or decision.get("add")
                ):
                    raise ValueError(
                        f"Reviewed Tag {tag!r} has an invalid inline Allowed Details replacement."
                    )
            sensitivity = decision.get("content_sensitivity")
            if sensitivity is not None:
                if not isinstance(sensitivity, dict):
                    raise ValueError(
                        f"Reviewed Tag {tag!r} has malformed content sensitivity."
                    )
                minimum_rating = str(sensitivity.get("minimum_rating") or "").strip()
                flags = tuple(
                    str(value or "").strip()
                    for value in sensitivity.get("content_flags", ())
                )
                if (
                    minimum_rating not in CONTENT_RATINGS
                    or any(not value for value in flags)
                    or len(flags) != len(set(flags))
                ):
                    raise ValueError(
                        f"Reviewed Tag {tag!r} has invalid content sensitivity."
                    )
            reviewed_tags.add(tag)
        for hold in payload.get("holds", ()):
            if not isinstance(hold, dict):
                raise ValueError(f"Reviewed Tag semantic audit {cohort_id!r} has a malformed hold.")
            subject = normalize_tag(str(hold.get("subject") or ""))
            field = str(hold.get("field") or "").strip()
            candidates = tuple(
                normalize_tag(str(value or ""))
                for value in hold.get("candidates", ())
            )
            reason = str(hold.get("reason") or "").strip()
            key = (subject, field, candidates)
            if not subject or subject not in reviewed_tags:
                raise ValueError(
                    f"Reviewed Tag semantic audit hold references unaudited subject {subject!r}."
                )
            if not field or not candidates or any(not value for value in candidates):
                raise ValueError(f"Reviewed Tag semantic audit hold for {subject!r} is incomplete.")
            if subject in candidates or len(candidates) != len(set(candidates)):
                raise ValueError(f"Reviewed Tag semantic audit hold for {subject!r} has invalid candidates.")
            if len(reason) < 20 or len(reason) > 600:
                raise ValueError(f"Reviewed Tag semantic audit hold for {subject!r} needs a concise reason.")
            if key in reviewed_holds:
                raise ValueError(f"Reviewed Tag semantic audit hold is duplicated for {subject!r}.")
            reviewed_holds.add(key)
        for review in payload.get("identity_reviews", ()):
            if not isinstance(review, dict):
                raise ValueError(f"Reviewed Tag semantic audit {cohort_id!r} has a malformed identity review.")
            pair = tuple(
                normalize_tag(str(value or ""))
                for value in review.get("tags", ())
            )
            decision = str(review.get("decision") or "").strip()
            reason = str(review.get("reason") or "").strip()
            if len(pair) != 2 or any(not value for value in pair) or pair[0] == pair[1]:
                raise ValueError("Reviewed Tag semantic identity reviews require two distinct Tags.")
            canonical_pair = tuple(sorted(pair))
            if not any(value in reviewed_tags for value in canonical_pair):
                raise ValueError(
                    f"Reviewed identity pair {canonical_pair!r} does not include an audited Tag."
                )
            if decision not in {"keep_separate", "merge_candidate"}:
                raise ValueError(f"Reviewed identity pair {canonical_pair!r} has invalid decision {decision!r}.")
            if len(reason) < 20 or len(reason) > 600:
                raise ValueError(f"Reviewed identity pair {canonical_pair!r} needs a concise reason.")
            if canonical_pair in reviewed_identity_pairs:
                raise ValueError(f"Reviewed identity pair {canonical_pair!r} is duplicated.")
            reviewed_identity_pairs.add(canonical_pair)
        for constraint in payload.get("constraints", ()):
            if not isinstance(constraint, dict):
                raise ValueError(
                    f"Reviewed Tag semantic audit {cohort_id!r} has a malformed constraint."
                )
            constraint_id = str(constraint.get("id") or "").strip()
            endpoints = tuple(
                normalize_tag(str(value or ""))
                for value in constraint.get("endpoints", ())
            )
            scope = str(constraint.get("scope") or "").strip()
            label = str(constraint.get("label") or "").strip()
            reason = str(constraint.get("reason") or "").strip()
            dimension = str(constraint.get("dimension") or "").strip()
            if not constraint_id or constraint_id in reviewed_constraint_ids:
                raise ValueError(
                    f"Reviewed Tag semantic constraint ID is missing or duplicated: {constraint_id!r}."
                )
            if (
                len(endpoints) < 2
                or any(not value for value in endpoints)
                or len(endpoints) != len(set(endpoints))
            ):
                raise ValueError(
                    f"Reviewed Tag semantic constraint {constraint_id!r} has invalid endpoints."
                )
            if not any(value in reviewed_tags for value in endpoints):
                raise ValueError(
                    f"Reviewed Tag semantic constraint {constraint_id!r} has no audited endpoint."
                )
            if scope not in {"image", "same_character", "same_object", "same_context"}:
                raise ValueError(
                    f"Reviewed Tag semantic constraint {constraint_id!r} has invalid scope {scope!r}."
                )
            if not label or not dimension or len(reason) < 20 or len(reason) > 600:
                raise ValueError(
                    f"Reviewed Tag semantic constraint {constraint_id!r} is incomplete."
                )
            reviewed_constraint_ids.add(constraint_id)
    return payloads


def _exact_tag_index(tags: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    for item in tags:
        name = normalize_tag(str(item.get("display") or ""))
        if not name:
            continue
        if name in result:
            duplicates.add(name)
        else:
            result[name] = dict(item)
    if duplicates:
        raise ValueError(
            "Reviewed Tag semantic audit found duplicate canonical names: "
            + ", ".join(sorted(duplicates)[:10])
        )
    return result


def _resolved_record(index: Mapping[str, dict[str, Any]], value: Any) -> dict[str, Any]:
    name = normalize_tag(str(value or ""))
    record = index.get(name)
    if record is None:
        raise ValueError(f"Reviewed Tag semantic audit cannot resolve exact canonical Tag {value!r}.")
    return record


def reviewed_tag_retirement_plan(
    tags: Iterable[Mapping[str, Any]],
    audits: Iterable[Mapping[str, Any]],
    *,
    include_restored: bool = False,
    allow_layer_ids: bool = False,
) -> dict[str, str]:
    """Resolve every approved retirement candidate to its stable Tag identity."""
    audit_rows = tuple(audits)
    exact = _exact_tag_index(tags)
    corrections = {
        normalize_tag(str(decision["tag"])): normalize_tag(
            str(decision["canonical_name"])
        )
        for audit in audit_rows
        for decision in audit.get("decisions", ())
        if isinstance(decision, Mapping) and decision.get("canonical_name")
    }
    planned: dict[str, str] = {}
    seen_names: set[str] = set()
    for audit in audit_rows:
        selection = audit.get("selection", {})
        if not isinstance(selection, Mapping):
            continue
        dispositions = {
            normalize_tag(str(item.get("candidate") or "")): str(
                item.get("action") or ""
            )
            for item in selection.get("existence_dispositions", ())
            if isinstance(item, Mapping)
            and normalize_tag(str(item.get("candidate") or ""))
        }
        for value in selection.get("retirement_candidates", ()):
            name = normalize_tag(
                str(value.get("tag") if isinstance(value, Mapping) else value)
            )
            if not name:
                raise ValueError("Reviewed Tag retirement candidate cannot be blank.")
            if name in seen_names:
                raise ValueError(
                    f"Reviewed Tag retirement candidate {name!r} appears more than once."
                )
            seen_names.add(name)
            if dispositions.get(name) != "retire_candidate":
                raise ValueError(
                    f"Reviewed Tag retirement candidate {name!r} lacks an exact "
                    "retire_candidate disposition."
                )
            record = _resolved_record(exact, corrections.get(name, name))
            stable_id = str(record.get("id") or "").strip()
            if not stable_id:
                raise ValueError(
                    f"Reviewed Tag retirement candidate {name!r} has no stable identity."
                )
            if stable_id in planned:
                raise ValueError(
                    f"Reviewed Tag retirement candidates resolve more than once to "
                    f"stable identity {stable_id!r}."
                )
            planned[stable_id] = name
    if not include_restored:
        restored = reviewed_tag_restoration_plan(tuple(exact.values()), audit_rows, allow_layer_ids=allow_layer_ids)
        for stable_id in restored:
            planned.pop(stable_id)
    return dict(sorted(planned.items()))


def reviewed_tag_restoration_plan(tags, audits, *, allow_layer_ids: bool = False) -> dict[str, str]:
    """Reverse only exact, previously reviewed default retirements.

    Historical retirement decisions stay intact. A correction must pin the
    original cohort, canonical spelling and stable ID; it cannot undo a merge
    or turn an unrelated local tombstone into a shipped restoration.
    """
    tags, audits = tuple(tags), tuple(audits)
    retired = reviewed_tag_retirement_plan(tags, audits, include_restored=True)
    cohorts = {str(audit["cohort_id"]): audit for audit in audits}
    restored = {}
    for audit in audits:
        for correction in audit.get("restorations", ()):
            if not isinstance(correction, Mapping):
                raise ValueError("Reviewed Tag restoration must be an exact record.")
            declared_id = str(correction.get("id") or "").strip()
            name = normalize_tag(str(correction.get("tag") or ""))
            stable_id = next((key for key, value in retired.items() if value == name), "")
            compatibility_ids = correction.get("compatibility_ids", [])
            if (
                not isinstance(compatibility_ids, list)
                or any(not isinstance(value, str) or not value.strip() for value in compatibility_ids)
                or len(set(compatibility_ids)) != len(compatibility_ids)
                or declared_id in compatibility_ids
            ):
                raise ValueError("Reviewed Tag restoration has invalid compatibility IDs.")
            allowed_ids = {declared_id, *compatibility_ids}
            prior = cohorts.get(str(correction.get("retirement_cohort") or ""), {})
            candidates = {
                normalize_tag(str(value.get("tag") if isinstance(value, Mapping) else value))
                for value in prior.get("selection", {}).get("retirement_candidates", ())
            }
            prior_reason = next((
                str(item.get("reason") or "")
                for item in prior.get("selection", {}).get("existence_dispositions", ())
                if normalize_tag(str(item.get("candidate") or "")) == name
                and item.get("action") == "retire_candidate"
            ), "")
            if (
                not prior_reason or correction.get("previous_reason") != prior_reason
                or not declared_id or not stable_id
                or (stable_id not in allowed_ids if allow_layer_ids else stable_id != declared_id)
                or name not in candidates or stable_id in restored
                or not str(correction.get("reason") or "").strip()
            ):
                raise ValueError(f"Reviewed Tag restoration {stable_id!r} has stale or incomplete evidence.")
            restored[stable_id] = name
    return dict(sorted(restored.items()))


def reviewed_tag_retirement_guidance(tags, audits):
    """Compile reviewed reasons/choice labels without inventing replacement Tags."""
    tags=tuple(tags); audits=tuple(audits)
    plan=reviewed_tag_retirement_plan(tags,audits,allow_layer_ids=True)
    by_name={}
    for record in tags:
        if record.get("id") in plan:
            continue
        for name in (record.get("display"),*record.get("aliases",())):
            key=normalize_tag(str(name or ""))
            if key:by_name.setdefault(key,set()).add(str(record["id"]))
    decisions={normalize_tag(str(item.get("candidate") or "")):(item,audit)
        for audit in audits
        for item in audit.get("selection",{}).get("existence_dispositions",())
        if isinstance(item,Mapping) and item.get("action")=="retire_candidate"}
    result={}
    for stable_id,name in plan.items():
        decision,audit=decisions[name]
        alternatives=[]; seen=set()
        for label in decision.get("alternatives",()):
            label=str(label).strip(); key=normalize_tag(label)
            if not key or key in seen:continue
            seen.add(key); owners=by_name.get(key,set())
            alternatives.append({"label":label,"tag_id":next(iter(owners)) if len(owners)==1 else ""})
        result[stable_id]={
            "schema_version":1,"reviewed":True,
            "reason":str(decision.get("reason") or "").strip(),
            "alternatives":alternatives,
            "source":str(audit.get("cohort_id") or ""),
            "evidence_url":str(decision.get("evidence_url") or ""),
        }
    return result


def compile_reviewed_tag_semantic_defaults(
    defaults_dir: str | Path,
    audit_dir: str | Path,
    *,
    category_user_path: str | Path | None = None,
    identity_resolution_path: str | Path | None = None,
) -> ReviewedTagSemanticCompilation:
    defaults = Path(defaults_dir)
    if identity_resolution_path is None:
        discovered_resolution = (
            Path(audit_dir).parent
            / "tag-identity-reviews"
            / "km04_identity_reconciliation_001.json"
        )
        if discovered_resolution.is_file():
            identity_resolution_path = discovered_resolution
    audits = load_reviewed_tag_semantic_audits(audit_dir)
    layer_payloads = [
        _read_payload(defaults / name)
        for name in ("foundation_tags.json", "community_tags.json", "promoted_tags.json")
    ]
    raw_source = resolve_effective_tag_defaults(
        (
            (item for item in payload.get("tags", ()) if isinstance(item, dict))
            for payload in layer_payloads
        )
    )
    retirement_plan = reviewed_tag_retirement_plan(raw_source.tags, audits)
    restoration_plan = reviewed_tag_restoration_plan(raw_source.tags, audits)
    restoration_compatibility_ids = {
        str(source) for source, target in raw_source.id_remap.items()
        if str(source) != str(target) and str(target) in restoration_plan
    }
    restoration_evidence = {}
    for audit in audits:
        for item in audit.get("restorations", ()):
            stable_id = str(item["id"])
            expected_compatibility = {
                str(source) for source, target in raw_source.id_remap.items()
                if str(source) != str(target) and str(target) == stable_id
            }
            if set(item.get("compatibility_ids", ())) != expected_compatibility:
                raise ValueError("Reviewed Tag restoration compatibility mapping has changed.")
            original_provenance = item.get("previous_provenance", [])
            if not isinstance(original_provenance, list) or any(
                not isinstance(value, Mapping) for value in original_provenance
            ):
                raise ValueError("Reviewed Tag restoration has malformed original provenance.")
            restoration_evidence[stable_id] = (item, audit)
    clear_restored_concepts = {
        str(item["id"]) for audit in audits
        for item in audit.get("restorations", ())
        if item.get("clear_legacy_concept") is True
    }
    # A historical layer-local ID may resolve to the reviewed identity that is
    # now being retired. Retire that compatibility ID as well, but do not keep
    # a redirect to the retired target: redirects must always resolve to live
    # Tag authority.
    retirement_compatibility_ids = {
        str(source)
        for source, target in raw_source.id_remap.items()
        if str(source) != str(target) and str(target) in retirement_plan
    }
    approved_redirects: dict[str, str] = {}
    if identity_resolution_path is not None:
        approved_redirects, _aliases = reviewed_tag_identity_merge_plan(
            raw_source.tags, identity_resolution_path,
        )
    effective = load_tag_default_compilation_source(
        defaults,
        include_removed_tag_ids=(
            *approved_redirects,
            *retirement_plan,
            *restoration_plan,
            *restoration_compatibility_ids,
            *retirement_compatibility_ids,
        ),
    )
    if effective.findings:
        raise ValueError(
            "Default Tag layers have unresolved identity findings: "
            + " ".join(effective.findings[:3])
        )
    effective_index = _exact_tag_index(effective.tags)
    effective_by_id = {
        str(item.get("id") or ""): dict(item)
        for item in effective.tags
        if str(item.get("id") or "")
    }
    identity_owners: dict[str, str] = {}
    for item in effective.tags:
        stable_id = str(item.get("id") or "")
        for value in (item.get("display"), *item.get("aliases", ())):
            identity = normalize_tag(str(value or ""))
            if not identity:
                continue
            owner = identity_owners.get(identity)
            if owner is not None and owner != stable_id:
                raise ValueError(
                    f"Reviewed Tag semantic audit found duplicate identity owner for {identity!r}."
                )
            identity_owners[identity] = stable_id
    promoted_path = defaults / "promoted_tags.json"
    promoted = _read_payload(promoted_path)
    reusable_modifier_projections_retired = (
        int(promoted.get("schema_version", 1) or 1) >= 3
    )
    records = [copy.deepcopy(item) for item in promoted.get("tags", ()) if isinstance(item, dict)]
    record_by_id = {str(item.get("id") or ""): index for index, item in enumerate(records)}
    record_by_name = {
        normalize_tag(str(item.get("display") or "")): index
        for index, item in enumerate(records)
    }
    categories = CategoryRegistry(
        defaults / "categories.json",
        Path(category_user_path) if category_user_path is not None else defaults / ".reviewed-tag-semantics-no-user.json",
    )
    note_changes = 0
    category_changes = 0
    decision_count = 0
    addition_count = 0

    for audit in audits:
        cohort_id = str(audit["cohort_id"])
        reviewed_at = str(audit["reviewed_at"])
        source = dict(audit["research_source"])
        for decision in audit.get("decisions", ()):
            decision_reviewed_at = str(
                decision.get("reviewed_at") or reviewed_at
            )
            canonical = normalize_tag(str(decision["tag"]))
            is_addition = bool(decision.get("add"))
            original = effective_index.get(canonical)
            if original is None and decision.get("canonical_name"):
                original = effective_index.get(
                    normalize_tag(str(decision["canonical_name"]))
                )
            if original is None and not is_addition:
                raise ValueError(
                    f"Reviewed Tag semantic audit cannot resolve exact canonical Tag {decision['tag']!r}."
                )
            if is_addition:
                requested_id = str(decision.get("id") or "").strip()
                existing_id_record = effective_by_id.get(requested_id)
                if (
                    existing_id_record is not None
                    and normalize_tag(str(existing_id_record.get("display") or "")) != canonical
                ):
                    raise ValueError(
                        f"Reviewed missing-Tag addition {decision['tag']!r} reuses stable ID "
                        f"{requested_id!r} owned by {existing_id_record.get('display')!r}."
                    )
                if original is not None and str(original.get("id") or "") != requested_id:
                    raise ValueError(
                        f"Reviewed missing-Tag addition {decision['tag']!r} collides with existing "
                        f"stable ID {original.get('id')!r}."
                    )
                for value in (decision["tag"], *decision.get("aliases", ())):
                    identity = normalize_tag(str(value or ""))
                    owner = identity_owners.get(identity)
                    if owner is not None and owner != requested_id:
                        raise ValueError(
                            f"Reviewed missing-Tag addition {decision['tag']!r} collides with "
                            f"identity {value!r} owned by stable ID {owner!r}."
                        )
                if original is None:
                    original = {
                        "id": requested_id,
                        "display": str(decision["tag"]),
                        "category": "uncategorized",
                        "aliases": list(decision.get("aliases", ())),
                        "seen_count": 0,
                        "first_seen": None,
                        "last_seen": None,
                        "concept_id": None,
                        "properties": {},
                        "notes": "",
                        "provenance": [],
                        "relations": {
                            "parent": None,
                            "attributes": [],
                            "implies": [],
                            "conflicts": [],
                        },
                    }
                identity_owners[canonical] = requested_id
                for value in decision.get("aliases", ()):
                    identity_owners[normalize_tag(str(value))] = requested_id
                addition_count += 1
            else:
                stable_id = str(original.get("id") or "")
                canonical_name = str(decision.get("canonical_name") or "").strip()
                if canonical_name:
                    identity = normalize_tag(canonical_name)
                    owner = identity_owners.get(identity)
                    if owner is not None and owner != stable_id:
                        raise ValueError(
                            f"Reviewed Tag {decision['tag']!r} cannot use canonical name "
                            f"{canonical_name!r}; it is owned by stable ID {owner!r}."
                        )
                for value in decision.get("add_aliases", ()):
                    identity = normalize_tag(str(value or ""))
                    owner = identity_owners.get(identity)
                    if owner is not None and owner != stable_id:
                        raise ValueError(
                            f"Reviewed Tag {decision['tag']!r} cannot add alias {value!r}; "
                            f"it is owned by stable ID {owner!r}."
                        )
            category = str(decision["category"])
            if category not in categories.categories:
                raise ValueError(
                    f"Reviewed Tag semantic audit {cohort_id!r} references missing Category {category!r}."
                )
            notes = str(decision["notes"]).strip()
            note_changes += int(str(original.get("notes") or "").strip() != notes)
            category_changes += int(str(original.get("category") or "uncategorized") != category)
            compiled = copy.deepcopy(original)
            if is_addition:
                compiled["id"] = str(decision["id"])
                compiled["display"] = str(decision["tag"])
                compiled["aliases"] = list(decision.get("aliases", ()))
            elif (
                decision.get("canonical_name")
                or decision.get("add_aliases")
                or decision.get("remove_aliases")
            ):
                aliases = list(compiled.get("aliases", ()))
                removed_aliases = {
                    normalize_tag(str(value))
                    for value in decision.get("remove_aliases", ())
                }
                aliases = [
                    value
                    for value in aliases
                    if normalize_tag(str(value)) not in removed_aliases
                ]
                for identity in removed_aliases:
                    if identity_owners.get(identity) == stable_id:
                        identity_owners.pop(identity, None)
                known = {normalize_tag(str(value)) for value in aliases}
                canonical_name = str(decision.get("canonical_name") or "").strip()
                if canonical_name:
                    old_display = str(compiled.get("display") or "").strip()
                    canonical_identity = normalize_tag(canonical_name)
                    aliases = [
                        value
                        for value in aliases
                        if normalize_tag(str(value)) != canonical_identity
                    ]
                    known = {normalize_tag(str(value)) for value in aliases}
                    old_identity = normalize_tag(old_display)
                    if old_identity and old_identity != canonical_identity and old_identity not in known:
                        aliases.append(old_display)
                        known.add(old_identity)
                    compiled["display"] = canonical_name
                    identity_owners[canonical_identity] = str(compiled.get("id") or "")
                for value in decision.get("add_aliases", ()):
                    identity = normalize_tag(str(value))
                    if identity not in known:
                        aliases.append(str(value))
                        known.add(identity)
                    identity_owners[identity] = str(compiled.get("id") or "")
                compiled["aliases"] = aliases
            if decision.get("remove_inline_implies"):
                relations = (
                    copy.deepcopy(compiled.get("relations"))
                    if isinstance(compiled.get("relations"), dict)
                    else {}
                )
                if "implies" in relations:
                    implies = list(relations.get("implies", ()))
                    for value in decision.get("remove_inline_implies", ()):
                        target = _resolved_record(effective_index, value)
                        target_id = str(target.get("id") or "")
                        target_name = normalize_tag(str(target.get("display") or ""))
                        implies = [
                            current
                            for current in implies
                            if str(current) != target_id
                            and normalize_tag(str(current)) != target_name
                        ]
                    if implies:
                        relations["implies"] = implies
                    else:
                        relations.pop("implies", None)
                    compiled["relations"] = relations
            if decision.get("remove_inline_conflicts"):
                relations = (
                    copy.deepcopy(compiled.get("relations"))
                    if isinstance(compiled.get("relations"), dict)
                    else {}
                )
                conflicts = list(relations.get("conflicts", ()))
                for value in decision.get("remove_inline_conflicts", ()):
                    target = _resolved_record(effective_index, value)
                    target_id = str(target.get("id") or "")
                    target_name = normalize_tag(str(target.get("display") or ""))
                    conflicts = [
                        current
                        for current in conflicts
                        if str(current) != target_id
                        and normalize_tag(str(current)) != target_name
                    ]
                relations["conflicts"] = conflicts
                constraint_provenance = list(
                    relations.get("constraint_provenance", ())
                )
                removed_targets = {
                    str(_resolved_record(effective_index, value).get("id") or "")
                    for value in decision.get("remove_inline_conflicts", ())
                }
                relations["constraint_provenance"] = [
                    item
                    for item in constraint_provenance
                    if not isinstance(item, dict)
                    or str(item.get("target_id") or "") not in removed_targets
                ]
                compiled["relations"] = relations
            if decision.get("remove_inline_parent"):
                relations = (
                    copy.deepcopy(compiled.get("relations"))
                    if isinstance(compiled.get("relations"), dict)
                    else {}
                )
                if "parent" in relations:
                    target = _resolved_record(
                        effective_index, decision["remove_inline_parent"]
                    )
                    target_id = str(target.get("id") or "")
                    target_name = normalize_tag(str(target.get("display") or ""))
                    current = str(relations.get("parent") or "")
                    if (
                        not current
                        or current == target_id
                        or normalize_tag(current) == target_name
                    ):
                        relations.pop("parent", None)
                    else:
                        raise ValueError(
                            f"Reviewed Tag {decision['tag']!r} expected inline parent "
                            f"{decision['remove_inline_parent']!r}, but found {current!r}."
                        )
                    compiled["relations"] = relations
            if decision.get("replace_inline_parent"):
                replacement = decision["replace_inline_parent"]
                relations = (
                    copy.deepcopy(compiled.get("relations"))
                    if isinstance(compiled.get("relations"), dict)
                    else {}
                )
                if "parent" not in relations:
                    compiled["relations"] = relations
                else:
                    old_value = replacement.get("from")
                    old_target = (
                        _resolved_record(effective_index, old_value)
                        if old_value
                        else None
                    )
                    new_target = _resolved_record(effective_index, replacement["to"])
                    current = str(relations.get("parent") or "")

                    def matches_parent(target: dict[str, Any] | None) -> bool:
                        if target is None:
                            return not current
                        return (
                            current == str(target.get("id") or "")
                            or normalize_tag(current)
                            == normalize_tag(str(target.get("display") or ""))
                        )

                    if not matches_parent(old_target) and not matches_parent(new_target):
                        raise ValueError(
                            f"Reviewed Tag {decision['tag']!r} expected inline parent "
                            f"{replacement.get('from')!r}, but found {current or None!r}."
                        )
                    relations.pop("parent", None)
                    compiled["relations"] = relations
            if decision.get("replace_inline_allowed_modifier_groups"):
                replacement = decision["replace_inline_allowed_modifier_groups"]
                relations = (
                    copy.deepcopy(compiled.get("relations"))
                    if isinstance(compiled.get("relations"), dict)
                    else {}
                )
                raw_current_groups = relations.get("allowed_modifier_groups")
                current_groups = (
                    tuple(
                        str(value or "").strip()
                        for value in raw_current_groups
                    )
                    if isinstance(raw_current_groups, list)
                    else None
                )
                old_groups = tuple(replacement["from"])
                new_groups = tuple(replacement["to"])
                current_provenance = relations.get(
                    "allowed_modifier_groups_provenance", {}
                )
                expected_provenance = replacement["expected_provenance"]
                matches_old_provenance = (
                    isinstance(current_provenance, dict)
                    and all(
                        str(current_provenance.get(key) or "") == str(value)
                        for key, value in expected_provenance.items()
                    )
                )
                matches_reviewed_provenance = (
                    isinstance(current_provenance, dict)
                    and str(current_provenance.get("source") or "")
                    == REVIEWED_TAG_SEMANTICS_SOURCE
                    and str(current_provenance.get("source_version") or "")
                    == cohort_id
                )
                if (
                    current_groups is None
                    and reusable_modifier_projections_retired
                ):
                    # Schema-v3 shipped layers intentionally omit this reviewed
                    # compatibility field. The canonical compiler immediately
                    # validates the corresponding no-details relationship, so
                    # rerunning the audit must not recreate retired authority.
                    relations.pop("allowed_modifier_groups_provenance", None)
                    compiled["relations"] = relations
                else:
                    if not (
                        (current_groups == old_groups and matches_old_provenance)
                        or (
                            current_groups == new_groups
                            and matches_reviewed_provenance
                        )
                    ):
                        raise ValueError(
                            f"Reviewed Tag {decision['tag']!r} found unexpected inline "
                            "Allowed Details state while applying a reviewed replacement."
                        )
                    relations["allowed_modifier_groups"] = list(new_groups)
                    relations["allowed_modifier_groups_provenance"] = {
                        "source": REVIEWED_TAG_SEMANTICS_SOURCE,
                        "source_version": cohort_id,
                        "reviewed_at": decision_reviewed_at,
                    }
                    compiled["relations"] = relations
            if decision.get("content_sensitivity"):
                sensitivity = decision["content_sensitivity"]
                compiled["content_sensitivity"] = {
                    "minimum_rating": str(sensitivity["minimum_rating"]),
                    "content_flags": list(sensitivity.get("content_flags", ())),
                    "classification_confidence": 1.0,
                    "classification_status": "reviewed",
                    "provenance": [
                        {
                            "source": REVIEWED_TAG_SEMANTICS_SOURCE,
                            "source_version": cohort_id,
                            "reviewed_at": decision_reviewed_at,
                        }
                    ],
                }
            compiled["notes"] = notes
            compiled["category"] = category
            ordering = compiled.get("ordering") if isinstance(compiled.get("ordering"), dict) else {}
            if not ordering.get("reviewed"):
                group, priority, _source_id, _inherited = categories.effective_ordering(category)
                compiled["ordering"] = {
                    **ordering,
                    "group": group,
                    "priority": priority,
                    "reviewed": False,
                    "provenance": list(ordering.get("provenance", ())),
                }
            provenance = [
                copy.deepcopy(item)
                for item in compiled.get("provenance", ())
                if isinstance(item, dict)
                and not (
                    str(item.get("source") or "") == REVIEWED_TAG_SEMANTICS_SOURCE
                    and str(item.get("source_version") or "") == cohort_id
                )
            ]
            reviewed_fields = (
                ["identity", "aliases", "notes", "category"]
                if is_addition
                else ["notes", "category"]
            )
            if not is_addition and (
                decision.get("add_aliases") or decision.get("remove_aliases")
            ):
                reviewed_fields.append("aliases")
            if not is_addition and decision.get("canonical_name"):
                reviewed_fields.extend(["identity", "aliases"])
            if not is_addition and (
                decision.get("remove_inline_implies")
                or decision.get("remove_inline_conflicts")
                or decision.get("remove_inline_parent")
                or decision.get("replace_inline_parent")
                or decision.get("replace_inline_allowed_modifier_groups")
            ):
                reviewed_fields.append("relations")
            if decision.get("content_sensitivity"):
                reviewed_fields.append("content_sensitivity")
            provenance.append(
                {
                    "source": REVIEWED_TAG_SEMANTICS_SOURCE,
                    "source_version": cohort_id,
                    "research_source": str(
                        (decision.get("research_basis") or source).get("name") or ""
                    ),
                    "research_version": str(
                        (decision.get("research_basis") or source).get("version") or ""
                    ),
                    "research_kind": str(
                        (decision.get("research_basis") or {}).get("kind") or "pinned_exact_page"
                    ),
                    "research_title": str(decision["tag"]).replace(" ", "_"),
                    "research_url": (
                        str((decision.get("research_basis") or {}).get("url") or "")
                        if decision.get("research_basis")
                        else str(source.get("wiki_page_base_url") or "")
                        + str(decision["tag"]).replace(" ", "_")
                    ),
                    **(
                        {
                            "research_summary": str(
                                decision["research_basis"].get("summary") or ""
                            )
                        }
                        if decision.get("research_basis")
                        else {}
                    ),
                    "reviewed_fields": reviewed_fields,
                    "reviewed_at": decision_reviewed_at,
                    "classification_confidence": 1.0,
                }
            )
            compiled["provenance"] = provenance
            promotion = compiled.get("promotion") if isinstance(compiled.get("promotion"), dict) else {}
            compiled["promotion"] = {
                **promotion,
                "reviewed": True,
                "reviewed_at": decision_reviewed_at,
            }
            stable_id = str(compiled.get("id") or "")
            canonical = normalize_tag(str(compiled.get("display") or ""))
            index = record_by_id.get(stable_id, record_by_name.get(canonical))
            if index is None:
                index = len(records)
                records.append(compiled)
            else:
                # Exact canonical-name overlays may use a layer-local stable ID
                # that the effective resolver maps to the first shipped identity.
                # Preserve that existing layer ID so old references retain their
                # remap path instead of disappearing when reviewed fields change.
                layer_id = str(records[index].get("id") or "").strip()
                if layer_id:
                    compiled["id"] = layer_id
                records[index] = compiled
            record_by_id[stable_id] = index
            record_by_id[str(compiled.get("id") or stable_id)] = index
            record_by_name[canonical] = index
            decision_count += 1

    # Preserve the established promoted-layer order. Existing reviewed records
    # are replaced in place, while newly promoted records are appended in the
    # deterministic audit/cohort order above. Re-sorting the whole layer would
    # create unrelated generated-data churn for every bounded audit cohort.
    promoted["tags"] = records
    removed = {
        str(value)
        for value in promoted.get("removed_tag_ids", ())
        if str(value).strip()
    }
    redirects = {
        str(source): str(target)
        for source, target in dict(promoted.get("merged_tag_ids") or {}).items()
        if str(source).strip() and str(target).strip()
    }
    if (set(restoration_plan) | restoration_compatibility_ids) & (set(redirects) | set(approved_redirects)):
        raise ValueError("Reviewed Tag restoration cannot undo an identity merge.")
    removed.difference_update(restoration_plan)
    removed.difference_update(restoration_compatibility_ids)
    if identity_resolution_path is not None:
        for donor_id, survivor_id in approved_redirects.items():
            existing = redirects.get(donor_id)
            if existing is not None and existing != survivor_id:
                raise ValueError(
                    f"Shipped Tag redirect {donor_id!r} conflicts with the approved "
                    f"canonical target {survivor_id!r}."
                )
            if survivor_id in removed:
                raise ValueError(
                    f"Approved Tag merge target {survivor_id!r} is already removed."
                )
            redirects[donor_id] = survivor_id
            removed.add(donor_id)
    for source_id in retirement_compatibility_ids:
        redirects.pop(source_id, None)
        removed.add(source_id)
    overlap = sorted(set(retirement_plan) & set(redirects))
    if overlap:
        raise ValueError(
            "Reviewed Tag retirement cannot also be an identity-merge donor: "
            + ", ".join(overlap[:10])
        )
    retired_survivors = sorted(
        set(retirement_plan) & set(approved_redirects.values())
    )
    if retired_survivors:
        raise ValueError(
            "Reviewed Tag retirement cannot remove an identity-merge survivor: "
            + ", ".join(retired_survivors[:10])
        )
    removed.update(retirement_plan)
    guidance=reviewed_tag_retirement_guidance(records,audits)
    for record in records:
        if record.get("id") in guidance:
            record["retirement_guidance"]=guidance[record["id"]]
        elif record.get("id") in restoration_plan or record.get("id") in restoration_compatibility_ids:
            record.pop("retirement_guidance", None)
            canonical_id = raw_source.id_remap.get(record["id"], record["id"])
            if canonical_id in clear_restored_concepts:
                record["concept_id"] = None
            correction, audit = restoration_evidence[canonical_id]
            correction_source = "CollectiGen reviewed Tag retirement correction"
            # Keep the original evidence as historical attribution, separately
            # from the maintainer correction that now owns the revised meaning.
            provenance = copy.deepcopy(correction.get("previous_provenance", []))
            for item in record.get("provenance", ()):
                if item.get("source") == correction_source:
                    continue
                if item.get("research_source") == "Maintainer-requested retirement correction under maximal Image Knowledge":
                    continue
                if item not in provenance:
                    provenance.append(copy.deepcopy(item))
            provenance.append({
                "source": correction_source,
                "source_version": str(audit["cohort_id"]),
                "reviewed_at": str(audit["reviewed_at"]),
                "reviewed_fields": ["notes", "category", "lifecycle"],
                "research_kind": "reviewed_local_concept",
                "research_summary": str(correction["reason"]),
            })
            record["provenance"] = provenance
    promoted["removed_tag_ids"] = sorted(removed)
    promoted["merged_tag_ids"] = dict(sorted(redirects.items()))
    return ReviewedTagSemanticCompilation(
        promoted,
        tuple(str(item["cohort_id"]) for item in audits),
        decision_count,
        addition_count,
        note_changes,
        category_changes,
        sum(len(item.get("holds", ())) for item in audits),
        sum(len(item.get("identity_reviews", ())) for item in audits),
        sum(
            str(review.get("decision") or "") == "merge_candidate"
            for item in audits
            for review in item.get("identity_reviews", ())
        ),
        len(approved_redirects),
        len(retirement_plan),
    )


def build_reviewed_tag_semantic_relationships(
    tags: TagDatabase,
    audit_dir: str | Path,
    *,
    source_tags: Iterable[Mapping[str, Any]] | None = None,
) -> tuple[KnowledgeRelationship, ...]:
    audits = load_reviewed_tag_semantic_audits(audit_dir)
    if source_tags is None:
        audit_root = Path(audit_dir)
        repository_root = audit_root.parents[1] if len(audit_root.parents) > 1 else None
        discovered_resolution = (
            audit_root.parent
            / "tag-identity-reviews"
            / "km04_identity_reconciliation_001.json"
        )
        discovered_defaults = (
            repository_root / "data" / "defaults"
            if repository_root is not None
            else None
        )
        if (
            discovered_resolution.is_file()
            and discovered_defaults is not None
            and discovered_defaults.is_dir()
        ):
            source_tags = load_tag_default_compilation_source(
                discovered_defaults,
                include_removed_tag_ids=tags.removed_tag_ids,
            ).tags
    source_records = tuple(
        copy.deepcopy(dict(item))
        for item in (
            source_tags
            if source_tags is not None
            else (record.to_dict() for record in tags.tags.values())
        )
    )
    exact = _exact_tag_index(source_records)
    canonical_corrections = {
        normalize_tag(str(decision["tag"])): normalize_tag(
            str(decision["canonical_name"])
        )
        for audit in audits
        for decision in audit.get("decisions", ())
        if decision.get("canonical_name")
    }

    def tag(value: Any) -> dict[str, Any]:
        name = normalize_tag(str(value or ""))
        historical = _resolved_record(exact, canonical_corrections.get(name, name))
        historical_id = str(historical.get("id") or "")
        live_id = tags.resolve_tag_id(historical_id)
        live = tags.tags.get(str(live_id or ""))
        if (
            live is None
            and historical_id in tags.removed_tag_ids
            and historical_id not in tags.merged_tag_ids
        ):
            return historical
        if live is None:
            raise ValueError(
                f"Reviewed Tag semantic audit cannot resolve historical stable ID "
                f"{historical_id!r} to one live canonical Tag."
            )
        return live.to_dict()

    def historical_tag(value: Any) -> dict[str, Any]:
        name = normalize_tag(str(value or ""))
        return _resolved_record(exact, canonical_corrections.get(name, name))

    expected: dict[str, KnowledgeRelationship] = {}
    held_pairs: set[tuple[str, str]] = set()

    def remember(relationship: KnowledgeRelationship) -> None:
        if relationship.id in expected:
            prior = expected[relationship.id]
            allowed_dimension_extension = (
                prior.kind == ALLOWED_DIMENSION
                and relationship.kind == ALLOWED_DIMENSION
                and prior.endpoints[:2] == relationship.endpoints[:2]
                and prior.directed == relationship.directed
                and prior.scope == relationship.scope
                and prior.disabled == relationship.disabled
                and prior.reviewed == relationship.reviewed
            )
            structurally_equal = (
                prior.kind == relationship.kind
                and (
                    prior.endpoints == relationship.endpoints
                    or allowed_dimension_extension
                )
                and prior.directed == relationship.directed
                and prior.scope == relationship.scope
                and prior.disabled == relationship.disabled
                and prior.reviewed == relationship.reviewed
            )
            prior_details = dict(prior.details)
            next_details = dict(relationship.details)
            variable_keys = {"audit_cohort_id", "audit_cohort_ids", "reviewed_at", "reason"}
            if not structurally_equal or {
                key: value for key, value in prior_details.items() if key not in variable_keys
            } != {
                key: value for key, value in next_details.items() if key not in variable_keys
            }:
                raise ValueError(
                    f"Reviewed Tag semantic audits create incompatible duplicate "
                    f"relationship {relationship.id!r}: {prior.label!r} "
                    f"{prior.endpoints!r} and {relationship.label!r} "
                    f"{relationship.endpoints!r}."
                )
            cohorts = {
                value
                for raw in (
                    prior_details.get("audit_cohort_ids", ""),
                    prior_details.get("audit_cohort_id", ""),
                    next_details.get("audit_cohort_ids", ""),
                    next_details.get("audit_cohort_id", ""),
                )
                for value in str(raw).split(",")
                if value
            }
            reasons = tuple(
                dict.fromkeys(
                    value
                    for value in (
                        prior_details.get("reason", ""),
                        next_details.get("reason", ""),
                    )
                    if value
                )
            )
            merged_details = {
                key: value
                for key, value in prior_details.items()
                if key not in variable_keys
            }
            merged_details.update(
                {
                    "audit_cohort_ids": ",".join(sorted(cohorts)),
                    "reviewed_at": max(
                        str(prior_details.get("reviewed_at") or ""),
                        str(next_details.get("reviewed_at") or ""),
                    ),
                    "reason": " | ".join(reasons),
                }
            )
            merged_endpoints = prior.endpoints
            if allowed_dimension_extension:
                merged_endpoints = (
                    *prior.endpoints,
                    *tuple(
                        endpoint
                        for endpoint in relationship.endpoints[2:]
                        if endpoint not in prior.endpoints[2:]
                    ),
                )
            expected[relationship.id] = replace(
                prior,
                endpoints=merged_endpoints,
                details=tuple(merged_details.items()),
            )
            return
        expected[relationship.id] = relationship

    for audit in audits:
        cohort_id = str(audit["cohort_id"])
        reviewed_at = str(audit["reviewed_at"])
        common = (
            ("default_compiler", REVIEWED_TAG_SEMANTICS_COMPILER),
            ("audit_cohort_id", cohort_id),
            ("reviewed_at", reviewed_at),
            ("review_source", "evidence_backed_semantic_audit"),
        )
        for item in audit.get("holds", ()):
            historical_source = historical_tag(item.get("subject"))
            historical_source_id = str(historical_source.get("id") or "")
            if historical_source_id in tags.merged_tag_ids:
                # A donor-scoped quarantine prevented ambiguous authority while
                # the identity decision was unresolved. The approved redirect
                # supersedes that temporary hold; survivor-scoped holds remain.
                continue
            source = tag(item.get("subject"))
            for value in item.get("candidates", ()):
                target = tag(value)
                held_pairs.add((str(source["id"]), str(target["id"])))
        for item in audit.get("identity_reviews", ()):
            for value in item.get("tags", ()):
                tag(value)
        for item in audit.get("relationships", ()):
            kind = str(item.get("kind") or "")
            if kind not in {SPECIFICITY, ENTAILMENT}:
                raise ValueError(f"Reviewed Tag semantic audit does not support relationship kind {kind!r} yet.")
            source = tag(item.get("source"))
            raw_targets = tuple(tag(value) for value in item.get("targets", ()))
            if kind == SPECIFICITY and len(raw_targets) != 1:
                raise ValueError(
                    f"Reviewed Broader Tag {source['display']!r} must have exactly one target."
                )
            if kind == ENTAILMENT and not raw_targets:
                raise ValueError(
                    f"Reviewed Also Means {source['display']!r} must have at least one target."
                )
            if len({str(target["id"]) for target in raw_targets}) != len(raw_targets):
                raise ValueError(
                    f"Reviewed {kind} relationship for {source['display']!r} repeats a target."
                )
            targets = tuple(
                target
                for target in dict.fromkeys(
                    str(target["id"]) for target in raw_targets
                )
                for target in (tags.tags[target].to_dict(),)
                if target["id"] != source["id"]
            )
            if not targets:
                # An approved identity merge can collapse an earlier semantic
                # edge into self-identity. The redirect and canonical alias now
                # carry that meaning; a self relationship would be invalid.
                continue
            relationship = KnowledgeRelationship(
                id=tag_semantic_relationship_id(kind, str(source["id"])),
                kind=kind,
                endpoints=(
                    RelationshipEndpoint("tag", str(source["id"]), str(source["display"])),
                    *tuple(
                        RelationshipEndpoint("tag", str(target["id"]), str(target["display"]))
                        for target in targets
                    ),
                ),
                directed=True,
                scope="any",
                label=(
                    f"{source['display']} Broader Tag"
                    if kind == SPECIFICITY
                    else f"{source['display']} Also Means"
                ),
                reviewed=True,
                source=REVIEWED_TAG_SEMANTICS_SOURCE,
                details=(*common, ("reason", str(item.get("reason") or ""))),
                layer="default",
            )
            remember(relationship)

        for item in audit.get("constraints", ()):
            endpoints = tuple(
                tags.tags[stable_id].to_dict()
                for stable_id in dict.fromkeys(
                    str(tag(value)["id"]) for value in item.get("endpoints", ())
                )
            )
            if len(endpoints) < 2:
                continue
            remember(
                KnowledgeRelationship(
                    id=str(item["id"]),
                    kind=SCOPED_CONSTRAINT,
                    endpoints=tuple(
                        RelationshipEndpoint(
                            "tag", str(endpoint["id"]), str(endpoint["display"])
                        )
                        for endpoint in endpoints
                    ),
                    directed=False,
                    scope=str(item["scope"]),
                    label=str(item["label"]),
                    reviewed=True,
                    source=REVIEWED_TAG_SEMANTICS_SOURCE,
                    details=(
                        *common,
                        ("dimension", str(item["dimension"])),
                        ("reason", str(item["reason"])),
                        ("compatibility_projection", "tag.relations.conflicts"),
                    ),
                    layer="default",
                )
            )

        for item in audit.get("allowed_dimensions", ()):
            base = tag(item.get("tag"))
            family = str(item.get("family") or "")
            mode = str(item.get("mode") or "")
            if mode == "none":
                if family or tuple(item.get("values", ())):
                    raise ValueError(
                        f"Reviewed no-details policy for {base['display']!r} "
                        "cannot name a family or values."
                    )
                remember(
                    KnowledgeRelationship(
                        id=allowed_dimension_none_relationship_id(str(base["id"])),
                        kind=ALLOWED_DIMENSION,
                        endpoints=(
                            RelationshipEndpoint(
                                "tag", str(base["id"]), str(base["display"])
                            ),
                            RelationshipEndpoint(
                                ALLOWED_DIMENSION_STATE,
                                NO_ALLOWED_DIMENSIONS,
                                "No allowed details",
                            ),
                        ),
                        directed=True,
                        scope="any",
                        label=f"{base['display']} allows no details",
                        reviewed=True,
                        source=REVIEWED_TAG_SEMANTICS_SOURCE,
                        details=(
                            *common,
                            ("policy_mode", "explicit_none"),
                            ("reason", str(item.get("reason") or "")),
                        ),
                        layer="default",
                    )
                )
                continue
            if mode != "only" or not family:
                raise ValueError(f"Reviewed Allowed Details policy for {base['display']!r} is not narrowly value-scoped.")
            values = tuple(tag(value) for value in item.get("values", ()))
            if not values:
                raise ValueError(f"Reviewed Allowed Details policy for {base['display']!r} has no values.")
            for value in item.get("values", ()):
                historical_base = _resolved_record(exact, item.get("tag"))
                compound = normalize_tag(
                    f"{value} {historical_base['display']}"
                )
                if compound not in exact:
                    raise ValueError(
                        f"Reviewed Allowed Details value {value!r} for {base['display']!r} has no exact shipped compound Tag."
                    )
            family_label = family.removeprefix("modifier_").replace("_", " ").title()
            remember(
                KnowledgeRelationship(
                    id=allowed_dimension_relationship_id(str(base["id"]), family),
                    kind=ALLOWED_DIMENSION,
                    endpoints=(
                        RelationshipEndpoint("tag", str(base["id"]), str(base["display"])),
                        RelationshipEndpoint("modifier_dimension", family, family_label),
                        *tuple(
                            RelationshipEndpoint(
                                "tag",
                                str(value["id"]),
                                str(value["display"]),
                                {SCOPE_ROLE_KEY: SCOPE_ROLE_SELECTION},
                            )
                            for value in values
                        ),
                    ),
                    directed=True,
                    scope="any",
                    label=f"{base['display']} allowed {family_label}",
                    reviewed=True,
                    source=REVIEWED_TAG_SEMANTICS_SOURCE,
                    details=(
                        *common,
                        ("policy_mode", "explicit_set"),
                        ("canonical_family_id", family),
                        ("value_scope", mode),
                        ("reason", str(item.get("reason") or "")),
                    ),
                    layer="default",
                )
            )

        for item in audit.get("modifier_values", ()):
            compound = tag(item.get("tag"))
            base = tag(item.get("base"))
            value = tag(item.get("value"))
            family = str(item.get("family") or "")
            historical_compound = _resolved_record(exact, item.get("tag"))
            historical_base = _resolved_record(exact, item.get("base"))
            historical_value = _resolved_record(exact, item.get("value"))
            expected_name = normalize_tag(
                f"{historical_value['display']} {historical_base['display']}"
            )
            if normalize_tag(str(historical_compound["display"])) != expected_name:
                raise ValueError(
                    f"Reviewed Modifier Value {compound['display']!r} does not match its exact value and base."
                )
            remember(
                KnowledgeRelationship(
                    id=modifier_composition_relationship_id(str(compound["id"]), family),
                    kind=MODIFIER_VALUE,
                    endpoints=(
                        RelationshipEndpoint("tag", str(compound["id"]), str(compound["display"])),
                        RelationshipEndpoint("tag", str(value["id"]), str(value["display"])),
                    ),
                    directed=True,
                    scope="any",
                    label=f"{compound['display']} modifier composition",
                    reviewed=True,
                    source=REVIEWED_TAG_SEMANTICS_SOURCE,
                    details=(
                        *common,
                        ("modifier_dimension_id", family),
                        ("operation", "reviewed_semantic_audit"),
                        ("base_tag_id", str(base["id"])),
                        ("tag_name", normalize_tag(str(compound["display"]))),
                    ),
                    layer="default",
                )
            )
    retired_ids = set(tags.removed_tag_ids) - set(tags.merged_tag_ids)
    retired_endpoint_ids = retired_ids | {
        str(source)
        for source, target in tags.merged_tag_ids.items()
        if str(target) in retired_ids
    }
    active_expected = {
        relationship_id: relationship
        for relationship_id, relationship in expected.items()
        if not any(
            endpoint.kind == "tag" and endpoint.id in retired_endpoint_ids
            for endpoint in relationship.endpoints
        )
    }
    for relationship in active_expected.values():
        source = relationship.source_endpoint
        if source is None:
            continue
        for target in relationship.target_endpoints:
            if (source.id, target.id) in held_pairs:
                raise ValueError(
                    "Reviewed Tag semantic audit activates an explicitly held relationship: "
                    f"{source.label or source.id!r} -> {target.label or target.id!r}."
                )
    return tuple(active_expected[key] for key in sorted(active_expected))


def reviewed_tag_semantic_audit_fingerprint(audit_dir: str | Path) -> str:
    digest = sha256()
    for path in sorted(Path(audit_dir).glob("*.json")):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
