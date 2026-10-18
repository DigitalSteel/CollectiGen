"""Search and correction-suggestion admission from an explicitly selected enrolled native catalog.

Reuse native guards/capture; no profile discovery, enrollment, library scan,
writer, catalog adoption, transport, or historical rename witness is supplied.
"""
import sys
from pathlib import Path

from .tag_collection import normalize_tag

from .organizer_encyclopedia_handoffs import AdmittedCatalog, AdmittedTag, HandoffHold


class OrganizerCatalogAdmissionReader:
    """Callable fresh admission reader bound to one exact Organizer Knowledge.

    The owner supplies its existing NativeTagCatalogAdapter for the same enrolled
    root. Construction reads nothing. Calling captures native effective values
    and holds when Organizer has not adopted their current saved authority.
    Returned revision is the stock complete native revision, not a new hash.
    """
    def __init__(self, state, *, adapter):
        from .tag_database import TagDatabase
        from suite_core.knowledge.native_tag_catalog_host import NativeTagCatalogAdapter
        if type(adapter) is not NativeTagCatalogAdapter:
            raise HandoffHold("selected_original_native_adapter_required")
        if type(getattr(state, "knowledge", None)) is not TagDatabase:
            raise HandoffHold("original_organizer_knowledge_required")
        self.state, self.knowledge, self.adapter = state, state.knowledge, adapter
        self.root = adapter.root
        self._publication = None
        self._publication_revision = None
        self._check_binding(state)

    def _check_binding(self, state):
        if state is not self.state or getattr(state, "knowledge", None) is not self.knowledge:
            raise HandoffHold("organizer_knowledge_authority_changed")
        if self.adapter.root != self.root or not self.root.is_absolute() or ".." in self.root.parts:
            raise HandoffHold("selected_native_root_changed")
        owner = sys.modules.get("core.knowledge_pack")
        if owner is None or self.adapter.native is not owner:
            raise HandoffHold("already_loaded_original_native_owner_required")
        path = self.root / "data/user/tags.json"
        binding = getattr(self.knowledge, "rule_pair_admission", None)
        if (Path(self.knowledge.path) != path or binding is None
                or binding.root != path.parent or not binding.owns(path, "knowledge")):
            raise HandoffHold("knowledge_must_own_selected_native_participant")
        return path

    def __call__(self, state):
        path = self._check_binding(state)
        try:
            with self.adapter.guard():
                self.adapter.validate()
                enrollment = self.adapter._state()
                if not enrollment.enrolled or enrollment.catalog_id != self.adapter.catalog_id:
                    raise HandoffHold("enrolled_selected_catalog_required")
                before = self.adapter.revision()
                raw, _stamp = self.adapter.native._recovery_bytes(path)
                if raw != getattr(self.knowledge, "_loaded_bytes", None):
                    raise HandoffHold("organizer_must_adopt_current_saved_tags")
                # Reuse one immutable stock capture only while the FULL native
                # revision is still verified. Live aliases/bytes are checked on
                # every call; this is not a TTL or a weaker filesystem token.
                publication = (self._publication if self._publication_revision == before
                               else self.adapter.capture(1))
                snapshot = publication.snapshot
                if (publication.catalog_id != enrollment.catalog_id
                        or snapshot.source.kind != "effective_native"
                        or snapshot.source.completeness != "complete"
                        or snapshot.source.revision != before
                        or self.adapter.revision() != before):
                    raise HandoffHold("native_catalog_capture_changed_or_incomplete")
                # Capture reads fresh persisted effective authority; it cannot
                # silently install that authority in the borrowed Organizer.
                if any(self.knowledge.tags.get(tag.id) is None
                       or self.knowledge.tags[tag.id].display != tag.display
                       for tag in snapshot.tags):
                    raise HandoffHold("organizer_effective_identities_need_refresh")
                # Verify the borrowed native alias index against fresh effective
                # capture. Never repair/rebuild it or infer a former rename.
                lookup_rows = {row.key: row for row in snapshot.lookups}
                admitted = []
                for tag in snapshot.tags:
                    live_names = self.knowledge.identity_names(tag.id)
                    if {normalize_tag(name) for name in live_names} != {
                            normalize_tag(name) for name in (tag.display, *tag.aliases)}:
                        raise HandoffHold("organizer_effective_aliases_need_refresh")
                    aliases = []
                    for alias in tag.aliases:
                        row = lookup_rows.get(normalize_tag(alias))
                        if row is None:
                            raise HandoffHold("native_alias_lookup_unavailable")
                        live_owners = tuple(sorted(self.knowledge.identity_owner_ids(alias)))
                        if live_owners != tuple(sorted(row.owner_ids)):
                            raise HandoffHold("organizer_alias_ownership_needs_refresh")
                        # Ambiguous/retired/unavailable names may be displayed
                        # in the source, but cannot admit a correction hint.
                        if (row.tag_id == tag.id and live_owners == (tag.id,)
                                and not row.retired_ids and not row.unavailable_ids
                                and row.status not in {"ambiguous", "retired", "unavailable", "missing"}):
                            aliases.append(alias)
                    admitted.append(AdmittedTag(tag.id, tag.display, tuple(aliases)))
                if self.adapter.revision() != before:
                    raise HandoffHold("native_catalog_changed_during_alias_check")
                if self.adapter.native._recovery_bytes(path)[0] != raw:
                    raise HandoffHold("native_tags_changed_during_capture")
                self._check_binding(state)
                self._publication, self._publication_revision = publication, before
                return AdmittedCatalog(self.knowledge, enrollment.catalog_id, before,
                    tuple(admitted))
        except HandoffHold:
            raise
        except (OSError, ValueError, RuntimeError) as error:
            raise HandoffHold("native_catalog_admission_unavailable") from error
