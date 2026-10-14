"""Explicit, guarded read composition for snapshot publication; never synchronize.

Reuse default_sync's reviewed default compiler and TagDatabase's native lookup
engine. Admit saved default-to-user identity before composing records; retained
user records override whole defaults, as in native knowledge_pack's content
reader. This does not apply sync baselines, enroll new
user incarnations, write a catalog, or authorize a Foundation transition.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path

from .catalog_admission import snapshot_catalog_admission
from .default_sync import load_effective_tag_defaults, _comparable, _content_hash
from .default_tag_identity import DefaultTagIdentityHold, _payload, admit_default_tag_identity, resolve_tag_identity
from .relationship_store import RelationshipStore
from .tag_database import TagDatabase, TagRecord


class EffectiveTagReaderChanged(RuntimeError):
    """An input/revision changed; discard this view and explicitly read again."""


# Explicit read-recipe identity. Version2 retains admitted saved owners when a
# stock compiler equivalence would reverse their identity and create a cycle.
# This is not a Pack/software release version
# or a DTO wire schema. Host revision owners must include it before cache reload.
SNAPSHOT_READER_RECIPE_ID = "collectigen/native-effective-tag-snapshot/2"


def snapshot_reader_revision(authority_revision: str) -> str:
    """Bind read semantics to a host's existing exact participant/receipt revision.

    Never changes approved Pack/release fingerprints or disables successor/token
    checks. Existing unversioned caches require explicit stock reload/migration.
    """
    if not isinstance(authority_revision, str) or not authority_revision.strip():
        raise ValueError("Explicit native authority revision required")
    payload = [SNAPSHOT_READER_RECIPE_ID, authority_revision]
    return sha256(b"collectigen-snapshot-reader-revision\0" + json.dumps(
        payload, separators=(",", ":")).encode()).hexdigest()


def _deny_write(*args, **kwargs):
    raise RuntimeError("Snapshot read authority cannot mutate native storage")


class _SnapshotTags(TagDatabase):
    """Native lookup methods on detached containers; not a persisted Tag store."""
    __init__ = _deny_write
    load = save = save_verified = _save_unadmitted = _save_verified_unadmitted = _deny_write
    _payload = _deny_write  # Native write serializer must not promote this read view.
    _atomic_bytes = _atomic_json = staticmethod(_deny_write)
    add_known_tag = restore_removed_tag = ensure_tag = register_seen_tags = record_seen = _deny_write
    add_merge_rule = delete_merge_rules_verified = _deny_write


def _pin(path):
    # These are the native read participants, not a transaction/recovery ledger.
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise EffectiveTagReaderChanged("Redirected snapshot read participant")
    raw = path.read_bytes() if path.exists() else None
    return None if raw is None else sha256(raw).digest()


def _memory_pin(tags):
    redirects, removed = tags.effective_identity_projection()
    payload = ({key: row.to_dict() for key, row in sorted(tags.tags.items())}, redirects, removed,
               {key: row.to_dict() for key, row in sorted(tags.removed_tag_archives.items())})
    return sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).digest()


def _bind_saved_identity(tags, defaults, defaults_dir, tracking_raw):
    """Native Tags/tracking binding, scoped to relevant *saved* identity claims.

    Unlike AppState's post-synchronization reader, a Foundation snapshot may have
    genuinely new default-only rows. They have no saved user mapping to admit;
    do not fabricate prospective user IDs or infer equivalence from spelling.
    """
    # Match native host invalidation before reading/admitting the saved graph.
    tags.invalidate_default_identity_context()
    state = _payload(tracking_raw)
    records = state.get("records", {})
    tracked = records.get("tags", {}) if isinstance(records, dict) else None
    if not isinstance(tracked, dict):
        raise DefaultTagIdentityHold("Invalid saved identity graph")
    relevant = {row["id"] for row in defaults.tags}
    relevant.update(defaults.id_remap)
    relevant.update(defaults.merged_tag_ids)
    relevant.update(defaults.merged_tag_ids.values())
    relevant.update(defaults.removed_tag_ids)
    relevant.update(tags.merged_tag_ids.values())
    previous = state.get("tag_lifecycle", {})
    previous_redirects = dict(previous.get("merged_tag_ids") or {}) if isinstance(previous, dict) else {}
    for source in defaults.merged_tag_ids:
        prior = previous_redirects.get(source)
        if (source not in tags.tags and source in tags.merged_tag_ids
                and tags.merged_tag_ids[source] != prior
                and prior in (set(tracked) | set(tags.merged_tag_ids))):
            relevant.add(prior)  # Native sync's changed-redirect baseline admission.
    for path in (defaults_dir / "relationships.json", tags.path.with_name("relationships.json")):
        for row in RelationshipStore(path).relationships.values():
            relevant.update(endpoint.id for endpoint in row.endpoints if endpoint.kind == "tag")
    # Admit exact-active/retired/redirected native identities too (including
    # physical duplicate checks); only truly default-only identities stay outside
    # user-pair admission. No prospective user membership is fabricated.
    stored = set(tracked) | set(tags.tags) | set(tags.merged_tag_ids) | set(tags.removed_tag_ids)
    required = relevant.intersection(stored)
    equivalent = {}
    for identity in required:
        equivalent.setdefault(defaults.canonical_id(identity), set()).add(identity)
    context = admit_default_tag_identity(tags._loaded_bytes, tracking_raw,
        tags_identity=str(tags.path.absolute()), tracking_identity=str(tags.path.with_name(
            "default_knowledge_state.json").absolute()), required_ids=required,
        generation=tags.default_identity_generation, equivalent_ids=equivalent.values())
    tags.bind_default_identity_context(context, pair_revision=context.revision)


def _effective_lifecycle(tags, defaults, tracking_raw):
    """Read counterpart of default_sync's kept-local/removal/redirect rules.

    Reuse native comparability/hashes and typed identity resolution. Do not run
    the synchronizer, mutate its baseline, revive user tombstones, or turn stock
    compiler id_remap equivalences into user restorations.
    """
    state, payload = _payload(tracking_raw), _payload(tags._loaded_bytes)
    previous = state.get("tag_lifecycle", {})
    previous = previous if isinstance(previous, dict) else {}
    previous_removed = {str(value) for value in previous.get("removed_tag_ids", ())}
    previous_redirects = {str(key): str(value) for key, value in dict(previous.get("merged_tag_ids") or {}).items()}
    tracked = state.get("records", {}).get("tags", {})
    raw_records = {row.get("id"): row for row in payload.get("tags", ()) if isinstance(row, dict)}
    shipped = {row["id"]: row for row in defaults.tags}
    removed = set(tags.removed_tag_ids)
    redirects = dict(tags.merged_tag_ids)
    kept_local = set(tags.tags) - removed - set(redirects)
    for identity in tuple(kept_local & set(defaults.removed_tag_ids)):
        record = raw_records[identity]
        current_hash = _content_hash(_comparable(record))
        previous_hash = str(tracked.get(identity, {}).get("hash") or "")
        shipped_hash = _content_hash(_comparable(shipped[identity])) if identity in shipped else ""
        untouched = bool(current_hash and current_hash in {previous_hash, shipped_hash})
        if identity not in previous_removed and untouched:
            kept_local.remove(identity)
            removed.add(identity)
        # Otherwise native sync keeps this restored/locally changed record.
    for identity in defaults.removed_tag_ids:
        if identity in kept_local:
            continue
        if identity in previous_removed and identity not in tags.removed_tag_ids:
            continue  # Deliberately cleared prior tombstone, even without a live row.
        removed.add(identity)
    for source, target in defaults.merged_tag_ids.items():
        if source in kept_local:
            continue  # Exact-active user restoration overrides shipped redirect.
        prior, current = previous_redirects.get(source), redirects.get(source)
        if prior and source not in redirects:
            continue  # Native explicit absence of a previously shipped redirect.
        if prior and current != prior:
            resolved = resolve_tag_identity(prior, active_ids=kept_local, removed_ids=removed,
                redirects=redirects, context=tags._default_identity_context)
            if resolved is None or current != resolved:
                continue  # Explicit local redirect override, not a baseline update.
        redirects[source] = resolve_tag_identity(target, active_ids=kept_local,
            removed_ids=removed, redirects=redirects, context=tags._default_identity_context) or target
    return redirects, removed


@dataclass(frozen=True)
class EffectiveTagSnapshotReader:
    raw_tags: TagDatabase = field(repr=False, compare=False)
    tags: TagDatabase = field(repr=False, compare=False)
    revision: str
    _pins: tuple = field(repr=False)
    _raw_memory_pin: bytes = field(repr=False)
    _view_memory_pin: bytes = field(repr=False)

    def verify(self, revision):
        """Under the host's guard, before and after capture; never refresh silently."""
        if revision() != self.revision:
            raise EffectiveTagReaderChanged("Snapshot read revision changed")
        if any(_pin(path) != digest for path, digest in self._pins):
            raise EffectiveTagReaderChanged("Snapshot read inputs changed")
        if _memory_pin(self.raw_tags) != self._raw_memory_pin:
            raise EffectiveTagReaderChanged("Raw loaded Tag authority changed")
        if _memory_pin(self.tags) != self._view_memory_pin:
            raise EffectiveTagReaderChanged("Effective Tag read values changed")
        if revision() != self.revision:
            raise EffectiveTagReaderChanged("Snapshot read revision changed")

    def verify_participants(self, categories, aliases):
        """Check already-loaded stock readers against the same pinned recipe."""
        expected = dict(self._pins)
        defaults = self.raw_tags.path.absolute().parent.parent / "defaults"
        user = self.raw_tags.path.absolute().parent
        if (Path(categories.default_path).absolute(), Path(categories.user_path).absolute()) != (
                defaults / "categories.json", user / "categories.json"):
            raise EffectiveTagReaderChanged("Category reader belongs to another catalog")
        for path in (defaults / "categories.json", user / "categories.json"):
            digest = expected[path]
            if categories._loaded_source_fingerprints.get(path) != (() if digest is None else digest):
                raise EffectiveTagReaderChanged("Loaded Category reader is stale")
        for store, path in ((aliases.workspace.default_store, defaults / "relationships.json"),
                            (aliases.workspace.user_store, user / "relationships.json")):
            if Path(store.path).absolute() != path:
                raise EffectiveTagReaderChanged("Relationship reader belongs to another catalog")
            raw = store._loaded_bytes
            if (None if raw is None else sha256(raw).digest()) != expected[path]:
                raise EffectiveTagReaderChanged("Loaded Relationship reader is stale")


def prepare_effective_tag_snapshot_reader(*, tags: TagDatabase, defaults_dir: Path,
                                         revision, expected_revision: str) -> EffectiveTagSnapshotReader:
    """Compose exact selected defaults plus retained user authority under admission.

    expected_revision must be observed before loading any participating native
    reader. The caller retains its host guard. The raw store is used for existing
    snapshot admission; the detached native lookup view is never used for it.
    """
    defaults_dir = Path(defaults_dir).absolute()
    user_path = Path(tags.path).absolute()
    if (user_path.name != "tags.json" or user_path.parent.name != "user"
            or user_path.parent.parent.name != "data"
            or defaults_dir != user_path.parent.parent / "defaults"):
        raise ValueError("Effective snapshot reader requires one canonical selected catalog")
    with snapshot_catalog_admission(tags, role="snapshot"):
        if revision() != expected_revision:
            raise EffectiveTagReaderChanged("Snapshot read source is stale")
        paths = tuple(defaults_dir / name for name in (
            "foundation_tags.json", "community_tags.json", "promoted_tags.json", "tag_aliases.json"))
        paths += (user_path, user_path.with_name("default_knowledge_state.json"))
        paths += (defaults_dir / "categories.json", user_path.with_name("categories.json"),
                  defaults_dir / "relationships.json", user_path.with_name("relationships.json"))
        for path in paths:
            for parent in (path.parent, *path.parents):
                if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
                    raise EffectiveTagReaderChanged("Redirected snapshot read catalog")
        pins = tuple((path, _pin(path)) for path in paths)
        if getattr(tags, "_loaded_bytes", None) != user_path.read_bytes():
            raise EffectiveTagReaderChanged("Raw Tag participant is stale")
        defaults = load_effective_tag_defaults(defaults_dir)
        if defaults.findings:
            raise ValueError("Reviewed default identity compiler has unresolved findings")
        tracking_path = user_path.with_name("default_knowledge_state.json")
        tracking_raw = tracking_path.read_bytes() if tracking_path.exists() else None
        _bind_saved_identity(tags, defaults, defaults_dir, tracking_raw)
        raw_memory = _memory_pin(tags)

        # Same stock record composition as knowledge_pack's content reader;
        # no default synchronizer, guessed field overrides or new identity engine.
        records = {row["id"]: TagRecord.from_dict(deepcopy(row)) for row in defaults.tags}
        records.update(deepcopy(tags.tags))
        redirects = {}
        for key, target in defaults.id_remap.items():
            if key == target:
                continue
            # The compiler retains its first shipped ID, but an exact admitted
            # saved owner can be the later ID. Reversing that owner recreates a
            # target->owner->target cycle and deletes both effective records.
            # Reuse native identity resolution; spelling alone never grants it.
            if key in tags.tags and tags.resolve_tag_id(target) == key:
                continue
            redirects[key] = target
        lifecycle_redirects, removed = _effective_lifecycle(tags, defaults, tracking_raw)
        redirects.update(lifecycle_redirects)
        raw_redirects, _raw_removed = tags.effective_identity_projection()
        redirects.update((source, target) for source, target in raw_redirects
                         if source not in tags.merged_tag_ids)  # Admitted context-only ownership.
        records = {key: row for key, row in records.items() if key not in removed and key not in redirects}

        # Keep native indexes/configuration, detached from every mutable raw container.
        view = object.__new__(_SnapshotTags)
        view.__dict__ = deepcopy({key: value for key, value in tags.__dict__.items()
                                  if key not in {"rule_pair_admission", "_categories", "_default_identity_context"}})
        view.rule_pair_admission = None
        view._categories = None
        view._loaded_bytes = None  # Cannot masquerade as loaded persisted authority.
        view._snapshot_read_view = True
        view.tags, view.merged_tag_ids, view.removed_tag_ids = records, redirects, removed
        view.clear_alias_authority()
        view._rebuild_lookup()  # Native resolver; discard the raw identity context.
        result = EffectiveTagSnapshotReader(tags, view, expected_revision, pins, raw_memory, _memory_pin(view))
        result.verify(revision)
        return result
