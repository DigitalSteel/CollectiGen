"""Inactive in-memory catalog binding; no writer, startup, native or Qt imports."""
from dataclasses import dataclass
from hashlib import sha256
import json
from threading import RLock
from uuid import uuid4

from suite_core.encyclopedia.publication import Publication, require_successor
from suite_core.knowledge import Resolution, TagLookup
from suite_core.knowledge.catalog_migration_preview import (
    CatalogPreview, preview_catalog_transition,
)
from suite_core.media_examples import ExampleIdentity, ExampleRequest

SOFTWARE_VERSION = "0.1.0-alpha.1"
BINDING_INTERFACE_VERSION = 1
RELEASE_DATE = "2026-10-06"


@dataclass(frozen=True)
class CatalogBinding:
    """Consumer freshness only; not authentication, writer or sharing authority."""
    session_id: str
    selection_epoch: int
    catalog_id: str
    generation: int
    authority_revision: str
    authority_fingerprint: str
    snapshot_fingerprint: str

    @property
    def token(self):
        values = (self.session_id, self.selection_epoch, self.catalog_id, self.generation,
                  self.authority_revision, self.authority_fingerprint, self.snapshot_fingerprint)
        return sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class BoundLookupResult:
    binding: CatalogBinding
    selection_token: str
    resolution: Resolution


@dataclass(frozen=True)
class BoundCatalogProposal:
    binding: CatalogBinding
    preview: CatalogPreview


class SelectedCatalogSession:
    """One host-owned selected published catalog and shared read-only facade.

    The host approves Publications before selection. This class does not read,
    publish, select filesystem stores, install defaults or enroll native writers.
    Host callbacks and consumer jobs remain separate from this short memory lock.
    """
    def __init__(self, publication: Publication):
        self._lock = RLock()
        self._session_id = uuid4().hex
        self._epoch = 0
        self._publication = None
        self.select(publication)

    def _adopt(self, publication):
        if type(publication) is not Publication:
            raise TypeError("Select an admitted Publication")
        lookup = TagLookup(publication.snapshot)
        fingerprint = publication.snapshot.fingerprint
        self._epoch += 1
        self._publication = publication
        self._lookup = lookup
        self._binding = CatalogBinding(
            self._session_id, self._epoch, publication.catalog_id, publication.generation,
            publication.snapshot.source.revision, publication.snapshot.source.authority_fingerprint,
            fingerprint,
        )

    def select(self, publication: Publication):
        """Explicit catalog selection, including intentional identity changes.

        Every deliberate selection invalidates pending work, even a return to the
        same Publication. To refresh the current identity, use refresh instead.
        """
        with self._lock:
            self._adopt(publication)
            return self._binding

    def refresh(self, publication: Publication):
        """Retain current binding on failure; reuse existing successor semantics."""
        with self._lock:
            if type(publication) is not Publication:
                raise TypeError("Refresh an admitted Publication")
            require_successor(self._publication, publication)
            if publication != self._publication:
                self._adopt(publication)
            return self._binding

    @property
    def binding(self):
        with self._lock:
            return self._binding

    def catalog_token(self):
        """Pass directly as the media provider's current_knowledge_revision reader."""
        return self.binding.token

    def resolve(self, value, *, selection_token, category_ids=None):
        if not isinstance(selection_token, str) or not selection_token:
            raise ValueError("A unique consumer selection token is required")
        with self._lock:
            resolution = self._lookup.resolve(value, category_ids=category_ids)
            return BoundLookupResult(self._binding, selection_token, resolution)

    def accepts_lookup(self, result: BoundLookupResult, *, current_selection_token):
        with self._lock:
            return (result.binding == self._binding
                    and result.selection_token == current_selection_token)

    def example_request(self, *, selection_token, library_ids, tag_ids, match_all=True):
        """Capture published canonical/alias groups from this exact selected view.

        No caller-supplied names can accidentally come from a different catalog.
        Full native editor semantics remain with the canonical writer; the host
        qualifies equivalence of published identity names before UI integration.
        """
        if type(tag_ids) not in (tuple, list):
            raise ValueError("Explicit selected Tag IDs are required")
        with self._lock:
            identities = []
            for tag_id in tag_ids:
                resolution = self._lookup.get(tag_id)
                if resolution.tag is None:
                    raise ValueError("A selected Tag is unavailable in this catalog scope")
                tag = resolution.tag
                identities.append(ExampleIdentity(tag.id, (tag.display, *tag.aliases), tag.category_id))
            return ExampleRequest(selection_token, self._binding.token, library_ids,
                                  tuple(identities), match_all)

    def propose(self, base, target, *, mode, assignments=(), offers=()):
        """Reuse existing optional catalog preview; never adopt its proposed rows."""
        with self._lock:
            preview = preview_catalog_transition(
                base, target, self._publication.snapshot, assignments, mode=mode, offers=offers,
            )
            return BoundCatalogProposal(self._binding, preview)

    def accepts_proposal(self, proposal, *, current_base, current_target):
        """Catalog/pack freshness only; image, offer, policy and writer gates remain."""
        with self._lock:
            return (proposal.binding == self._binding
                    and proposal.preview.matches_catalog_inputs(
                        current_base, current_target, self._publication.snapshot))
