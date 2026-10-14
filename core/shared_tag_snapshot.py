"""Detach already-loaded authority; admit canonical enrolled catalogs at entry.

Persisted capture observes identity/operation receipts and loaded participants.
Detached legacy values retain no-I/O capture. Never initialize stores or overlays.
"""
from suite_core.knowledge import (
    Category, EffectiveTag, Lookup, RelationshipEvidence, Snapshot, Source, TagLookup,
    normalize_term,
)


from .catalog_admission import (
    snapshot_catalog_admission, verify_snapshot_participants, require_snapshot_catalog_admission,
)


class SnapshotChangedError(RuntimeError):
    """The host authority changed during capture; no snapshot may be published."""


def capture_tag_snapshot(*, tags, categories, aliases, relationships, source: Source,
                         revision, effective_reader=None) -> Snapshot:
    """Admit raw persisted Tags; an explicit effective reader may supply read values.

    Construct that reader under the host guard before this call and build aliases
    against reader.tags. Existing detached legacy capture remains I/O-free.
    """
    if getattr(tags, "_snapshot_read_view", False):
        raise ValueError("Supply raw Tag authority separately from its effective read view")
    with snapshot_catalog_admission(tags, role="snapshot") as admission:
        verify_snapshot_participants(admission, categories, aliases, relationships)
        read_tags = tags
        if effective_reader is not None:
            from .effective_tag_snapshot_reader import EffectiveTagSnapshotReader
            if (not isinstance(effective_reader, EffectiveTagSnapshotReader)
                    or effective_reader.raw_tags is not tags
                    or effective_reader.revision != source.revision):
                raise ValueError("Effective snapshot reader belongs to different raw authority or revision")
            effective_reader.verify(revision)
            effective_reader.verify_participants(categories, aliases)
            read_tags = effective_reader.tags
        result = _capture_tag_snapshot_admitted(tags=read_tags, categories=categories, aliases=aliases,
            relationships=relationships, source=source, revision=revision)
        if effective_reader is not None:
            effective_reader.verify(revision)
            effective_reader.verify_participants(categories, aliases)
            verify_snapshot_participants(admission, categories, aliases, relationships)
        return result


def _capture_tag_snapshot_admitted(*, tags, categories, aliases, relationships, source: Source,
                         revision) -> Snapshot:
    """Detach effective values under a host lock or monotonic combined revision.

revision() must cover Tags, Categories, redirects/removals and configured alias
authority, and equal source.revision. Relationships must be that service's effective
view. Source reference/evidence must already be approved for the consumer's scope.
Unknown native fields remain in the host, never serialized into this portable DTO.
"""
    require_snapshot_catalog_admission(tags)
    before = revision()
    if before != source.revision:
        raise SnapshotChangedError("Source revision is stale")
    if aliases.tags is not tags:
        raise ValueError("Alias authority belongs to a different Tag store")
    redirects, removed_ids = tags.effective_identity_projection()
    records = tuple(tags.tags.values())
    category_rows = tuple(categories.categories.values())
    relation_rows = tuple(relationships)
    detached, keys = [], set()
    for record in sorted(records, key=lambda item: item.id):
        effective = aliases.effective_for_tag(record.id)
        sensitivity = getattr(record, "content_sensitivity", None)
        detached.append(EffectiveTag(record.id, record.display, record.category,
                                     tuple(effective.aliases),
                                     getattr(sensitivity, "minimum_rating", ""),
                                     getattr(sensitivity, "classification_status", "unclassified")))
        keys.add(normalize_term(record.display))
        keys.update(normalize_term(value) for value in record.aliases)
        keys.update(normalize_term(value) for value in effective.aliases)
    evidence = []
    for row in sorted(relation_rows, key=lambda item: item.id):
        if row.kind != "identity_alias":
            continue
        evidence.append(RelationshipEvidence(row.id, row.source, row.version, row.layer,
                                              row.reviewed, row.disabled))
        for endpoint in row.endpoints:
            if endpoint.kind == "alias_text":
                keys.add(normalize_term(endpoint.id or endpoint.label))
    lookups = []
    for key in sorted(keys - {""}):
        # Avoid triggering raw-ID precedence while compiling normalized *names*.
        # Whitespace preserves native normalization; finite IDs guarantee a spare
        # spelling. Runtime exact-ID precedence remains in TagLookup.resolve.
        spelling = " " + key + " "
        while spelling in tags.tags:
            spelling = " " + spelling + " "
        outcome = aliases.resolve(spelling)
        if outcome.status != "missing":
            lookups.append(Lookup(key, outcome.status, outcome.tag_id,
                                  outcome.owner_ids, outcome.relationship_ids,
                                  outcome.retired_ids, outcome.unavailable_ids))
    snapshot = Snapshot(source, tuple(detached),
                        tuple(Category(row.id, row.name, categories.path_label(row.id))
                              for row in sorted(category_rows, key=lambda item: item.id)),
                        tuple(lookups), redirects, removed_ids, tuple(evidence))
    TagLookup(snapshot)  # Reject inconsistent/dangling authority before publication.
    if revision() != before:
        raise SnapshotChangedError("Authority changed during snapshot capture")
    return snapshot
