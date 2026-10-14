"""Detached catalog transition proposals; no apply, persistence or host initialization.

Snapshot authority remains with the host. Returned Tag records are proposals, not
an effective Snapshot: the native writer must reconcile categories/relationships.
"""
from dataclasses import dataclass, fields, replace

from .tag_lookup import EffectiveTag, Snapshot, TagLookup, normalize_term

SOFTWARE_VERSION = "0.1.0-alpha.2"
PREVIEW_INTERFACE_VERSION = 1
RELEASE_DATE = "2026-10-06"
MAX_ASSIGNMENTS = 10000
MAX_OFFERS = 1000


def _explicit_collection(values, *, item_type, maximum, name, minimum=0):
    """Admit only explicit list/tuple contents before immutable normalization.

    Scalars, mappings, sets and lazy/custom iterables carry no endpoint/order
    contract here. Never iterate them, including for malformed-input diagnostics.
    """
    if type(values) not in (list, tuple):
        raise ValueError(name + " requires an explicit list or tuple")
    if not minimum <= len(values) <= maximum:
        raise ValueError(name + " collection bound exceeded")
    if any(type(item) is not item_type for item in values):
        raise ValueError(name + " has invalid element types")
    if item_type is str:
        if any(not item.strip() for item in values) or len(set(values)) != len(values):
            raise ValueError(name + " requires distinct nonblank IDs")
    return tuple(values)


@dataclass(frozen=True)
class PackRevision:
    pack_id: str
    content_version: str
    snapshot: Snapshot

    def __post_init__(self):
        if any(type(v) is not str or not v.strip() for v in (self.pack_id, self.content_version)):
            raise ValueError("Explicit pack identity and content version required")


@dataclass(frozen=True)
class Assignment:
    occurrence_id: str
    tag_id: str
    source_value: str
    evidence_ref: str


@dataclass(frozen=True)
class RefinementOffer:
    """Explicit output of host reviewed refinement authority, never inferred here.

    relationship_ref identifies the host's native plan/evidence, whose full typed
    endpoints/modifiers remain in the host. This DTO grants no replacement power.
    """
    source_id: str
    target_ids: tuple[str, ...]
    relationship_ref: str

    def __post_init__(self):
        object.__setattr__(self, "target_ids", _explicit_collection(
            self.target_ids, item_type=str, maximum=64, minimum=1, name="target_ids"))


@dataclass(frozen=True)
class TagChange:
    tag_id: str
    reason: str
    before: EffectiveTag | None
    proposed: EffectiveTag | None
    protected_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class AssignmentReview:
    original: Assignment
    reason: str
    candidate_ids: tuple[str, ...]
    choices: tuple[str, ...]
    relationship_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class CatalogPreview:
    mode: str
    base: PackRevision
    target: PackRevision
    local: Snapshot
    assignments: tuple[Assignment, ...]
    records: tuple[EffectiveTag, ...]
    changes: tuple[TagChange, ...]
    reviews: tuple[AssignmentReview, ...]
    holds: tuple[str, ...]

    @property
    def rollback_snapshot(self):
        """Original detached authority only; not a native rollback receipt."""
        return self.local

    def matches_catalog_inputs(self, base, target, local):
        """Catalog comparison only; no assignment/offer freshness or apply authority."""
        return (base.pack_id == self.base.pack_id and target.pack_id == self.target.pack_id
                and base.content_version == self.base.content_version
                and target.content_version == self.target.content_version
                and base.snapshot.fingerprint == self.base.snapshot.fingerprint
                and target.snapshot.fingerprint == self.target.snapshot.fingerprint
                and local.fingerprint == self.local.fingerprint)


def preview_catalog_transition(base: PackRevision, target: PackRevision, local: Snapshot,
                               assignments=(), *, mode="merged", offers=(), retirement_admissions=(),
                               default_lifecycle_admissions=(), native_user_record_ids=()):
    """Compare explicit prior-default, next-default and effective user snapshots.

    Blank/own/default/merged select candidate catalog contents only. User edits
    and removals block default-only adoption, rather than disappearing. Merged
    uses field-level three-way comparison; missing baseline ownership holds.
    Every original assignment and its provenance survives every mode. Even exact
    alias/redirect results are choices, never automatic reassignment.
    """
    if mode not in {"blank", "own", "default", "merged"} or base.pack_id != target.pack_id:
        raise ValueError("Invalid catalog transition")
    assignments = _explicit_collection(assignments, item_type=Assignment,
                                       maximum=MAX_ASSIGNMENTS, name="assignments")
    offers = _explicit_collection(offers, item_type=RefinementOffer,
                                  maximum=MAX_OFFERS, name="offers")
    for admissions, maximum in ((retirement_admissions, MAX_ASSIGNMENTS),
                               (default_lifecycle_admissions, len(local.redirects) + len(local.removed_ids)),
                               (native_user_record_ids, len(local.tags))):
        if (type(admissions) is not tuple or len(admissions) > maximum
                or any(type(identity) is not str or not identity for identity in admissions)
                or len(set(admissions)) != len(admissions)):
            raise ValueError("Explicit native-owned default retirement admissions required")
    admitted_retirements = set(retirement_admissions)
    for row in assignments:
        if type(row) is not Assignment or any(type(v) is not str or not v.strip()
                for v in (row.occurrence_id, row.tag_id, row.source_value, row.evidence_ref)):
            raise ValueError("Explicit occurrence identity and provenance required")
    if len({r.occurrence_id for r in assignments}) != len(assignments):
        raise ValueError("Duplicate assignment occurrence")
    readers = []
    for snapshot in (base.snapshot, target.snapshot, local):
        if snapshot.source.completeness != "complete":
            raise ValueError("Incomplete catalog authority")
        reader = TagLookup(snapshot)
        if any(reader.get(tag.id).tag is None for tag in snapshot.tags):
            raise ValueError("Catalog contains unavailable or out-of-scope records")
        readers.append(reader)
    old, new, user = ({r.id: r for r in snapshot.tags}
                      for snapshot in (base.snapshot, target.snapshot, local))
    local_redirects, base_redirects = dict(local.redirects), dict(base.snapshot.redirects)
    if any(identity not in new or not (
            identity in local_redirects and local_redirects[identity] == base_redirects.get(identity)
            or identity in local.removed_ids and identity in base.snapshot.removed_ids)
           for identity in default_lifecycle_admissions):
        raise ValueError("Default lifecycle admission requires exact prior-source lifecycle and a live target")
    if any(identity not in user for identity in native_user_record_ids):
        raise ValueError("Native user record ownership requires an active local record")
    native_owned_records = set(native_user_record_ids)
    proposed = {} if mode == "blank" else dict(new if mode == "default" else user)
    changes, holds = [], []
    retired = set(local.removed_ids) | {donor for donor, _ in local.redirects}
    # Explicit native proof distinguishes shipped effective lifecycle from user
    # intent. Keep the original Snapshot/history untouched; do not rewrite IDs.
    retired.difference_update(default_lifecycle_admissions)
    target_retired = set(target.snapshot.removed_ids) | {donor for donor, _ in target.snapshot.redirects}
    editable = tuple(f.name for f in fields(EffectiveTag) if f.name != "id")
    if mode in {"merged", "default"}:
        for identity in sorted(set(old) | set(new) | set(user)):
            before, prior, incoming = user.get(identity), old.get(identity), new.get(identity)
            if identity in retired:
                proposed.pop(identity, None)
                changes.append(TagChange(identity, "local_retirement_preserved", None, None))
                if mode == "default" and incoming:
                    holds.append("default_conflicts_with_local_retirement:" + identity)
            elif incoming is None:
                if before is not None:
                    if identity in target_retired:
                        resolution = readers[1].get(identity)
                        declared = (resolution.status == "redirected" and resolution.tag is not None
                                    and resolution.tag.id in new)
                        if declared and prior is not None and before == prior and identity in admitted_retirements:
                            # Exclude an unchanged retired default from assignable
                            # catalog rows, not from history or image evidence.
                            # The original assignment still requires a specific
                            # reviewed target choice below; never rewrite its ID.
                            proposed.pop(identity, None)
                            changes.append(TagChange(identity, "declared_default_retirement_proposed", before, None))
                            continue
                        proposed[identity] = before
                        changes.append(TagChange(identity, "local_retired_identity_preserved", before, before))
                        holds.append("default_retirement_requires_local_review:" + identity if declared else
                                     "target_retirement_requires_native_review:" + identity)
                        continue
                    # Default removal never destroys local history or its assignments.
                    if mode == "merged":
                        proposed[identity] = before
                    changes.append(TagChange(identity, "removed_default_retained" if prior else "user_only",
                                             before, before if mode == "merged" else None))
                    if mode == "default":
                        holds.append("default_would_exclude_local_record:" + identity)
            elif before is None:
                if prior is not None:
                    # Absence could be a user removal without a portable tombstone.
                    proposed.pop(identity, None)
                    changes.append(TagChange(identity, "missing_local_baseline_requires_review", None, None))
                    holds.append("missing_local_baseline:" + identity)
                else:
                    proposed[identity] = incoming
                    changes.append(TagChange(identity, "added_default", None, incoming))
            elif prior is None:
                proposed[identity] = before
                changes.append(TagChange(identity, "untracked_identity_preserved", before, before))
                if before != incoming:
                    holds.append("untracked_identity_collision:" + identity)
            else:
                protected = (editable if identity in native_owned_records else
                             tuple(name for name in editable if getattr(before, name) != getattr(prior, name)))
                merged = (before if identity in native_owned_records else
                          replace(incoming, **{name: getattr(before, name) for name in protected}))
                proposed[identity] = merged
                changes.append(TagChange(identity, "native_user_record_preserved" if identity in native_owned_records else
                                         "user_override_preserved" if protected else
                                         "unchanged" if merged == before else "default_update",
                                         before, merged, protected))
                if mode == "default" and merged != incoming:
                    holds.append("default_conflicts_with_user_override:" + identity)
    offer_index = {}
    for offer in offers:
        if (type(offer) is not RefinementOffer or type(offer.source_id) is not str
                or offer.source_id not in set(old) | set(user) | retired
                or type(offer.relationship_ref) is not str or not offer.relationship_ref.strip()
                or not 1 <= len(offer.target_ids) <= 64
                or any(type(i) is not str for i in offer.target_ids)
                or len(set(offer.target_ids)) != len(offer.target_ids)
                or offer.source_id in offer_index
                or any(type(i) is not str or i not in new or i == offer.source_id for i in offer.target_ids)):
            raise ValueError("Invalid explicit refinement offer")
        offer_index[offer.source_id] = offer
    # Inspect collisions only. No overlay name inference establishes equivalence.
    names = {}
    for record in proposed.values():
        for value in (record.display, *record.aliases):
            key = normalize_term(value)
            if key:
                names.setdefault(key, set()).add(record.id)
    holds.extend("catalog_name_collision:" + key for key, identities in names.items()
                 if len(identities) > 1)
    reviews = []
    for assignment in sorted(assignments, key=lambda row: row.occurrence_id):
        identity = assignment.tag_id
        offer = offer_index.get(identity)
        refs, candidates = (), ()
        if offer is not None:
            candidates = tuple(sorted(i for i in offer.target_ids if i in proposed and i not in retired))
            refs = (offer.relationship_ref,)
            reason = "specific_refinement_requires_review" if candidates else "refinement_targets_unavailable"
        elif identity in proposed and identity in target_retired and identity not in retired:
            resolution = readers[1].get(identity)
            if resolution.tag is not None and resolution.tag.id in proposed and resolution.tag.id not in retired:
                candidates = (resolution.tag.id,)
            refs = resolution.relationship_ids
            reason = "local_identity_retirement_requires_review"
        elif identity in proposed:
            reason = "unchanged_identity"
        elif identity in retired:
            reason = "local_retirement_retained"
        else:
            # An unknown explicit ID cannot be overridden by its display spelling.
            resolution = readers[1].get(identity)
            if resolution.status == "missing":
                local_resolution = readers[2].resolve(assignment.source_value)
                # User alias ownership/suppression takes precedence over a pack.
                resolution = (readers[1].resolve(assignment.source_value)
                              if local_resolution.status == "missing" else local_resolution)
            if resolution.tag is not None and resolution.tag.id in proposed and resolution.tag.id not in retired:
                candidates = (resolution.tag.id,)
                reason = "exact_identity_choice_requires_review"
            elif resolution.status == "ambiguous":
                candidates = tuple(sorted(i for i in resolution.owner_ids if i in proposed and i not in retired))
                reason = "ambiguous_identity_requires_review"
            else:
                reason = "assignment_retained_without_catalog_target"
            refs = resolution.relationship_ids
        choices = ("keep_original", "skip_review")
        if candidates:
            choices += tuple("review_specific:" + i for i in candidates)
        reviews.append(AssignmentReview(assignment, reason, candidates, choices, refs))
    return CatalogPreview(mode, base, target, local, assignments,
                          tuple(proposed[i] for i in sorted(proposed)), tuple(changes),
                          tuple(reviews), tuple(sorted(set(holds))))
