"""Organizer specialization of the original admitted component transaction kernel.

Public package identity supplies no stage or native authority. Callers retain the
original component-bound root capability, executor and settings ownership.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from threading import local
from uuid import uuid4
import base64
import json

from core.encyclopedia_installation import EncyclopediaInstallation
from core.organizer_connection import COMPONENT, POLICY, OrganizerConnection, review_existing_package
from core.organizer_connection_settings import OrganizerConnectionStore, KEY
from core.settings import AppSettings, _settings_writer
from suite_core.discovery.component_staging import (
    read_json, validate_journal, prior_journal_bytes, verify_directory, require,
)


@dataclass(frozen=True)
class ManagedOrganizerSelection:
    directory: Path
    inventory_sha256: str
    version: str

    def mapping(self):
        return {'schema_version':1, 'directory':str(self.directory),
                'inventory_sha256':self.inventory_sha256, 'version':self.version}


class _OrganizerPublication:
    """Keep the existing Organizer schema/revision; kernel comparisons use code identity."""
    def __init__(self, service, store):
        self.service, self.store, self.settings = service, store, store.settings

    def observe(self):
        return self.store.observe()

    def selected(self, snapshot):
        selected=self.store.selected(snapshot)
        self.service._observed_connection=selected
        return None if selected is None else ManagedOrganizerSelection(
            selected.directory, selected.inventory_sha256, selected.version)

    def save(self, selected, *, expected):
        return self.store.save(self.service.signed_selection(selected), expected=expected)

    def clear(self, *, expected):
        # Same canonical writer/stale guard as existing Reader clear. Unknown
        # unrelated settings remain intact; no second settings file is created.
        with _settings_writer(self.settings.path):
            current=self.observe()
            require(current.digest==expected.digest, 'settings_changed_review_again')
            if KEY not in current._payload:return current
            payload=deepcopy(current._payload);payload.pop(KEY)
            AppSettings._from_payload(payload)._save_locked(self.settings.path, _patch_payload=payload)
        saved=self.observe();require(self.selected(saved) is None, 'organizer_clear_readback')
        return saved


class OrganizerInstallation(EncyclopediaInstallation):
    """One original serial installation/rollback/publication algorithm, finite Organizer policy."""
    component = COMPONENT
    payload_policy = POLICY
    connection_type = ManagedOrganizerSelection

    def __init__(self, acquisition, store, *, admission=None, native_owner=None):
        require(type(store) is OrganizerConnectionStore, 'canonical_organizer_store_required')
        from core.organizer_native_owner import OwnedOrganizer
        require(native_owner is None or type(native_owner) is OwnedOrganizer,'typed_organizer_native_owner_required')
        self.original_store=store
        self._observed_connection=None
        self.native_owner=native_owner
        self._scope_local=local()
        super().__init__(acquisition, _OrganizerPublication(self,store), admission=admission,
                         native_health=self._native_health if native_owner is not None else None)

    @contextmanager
    def _scope(self):
        with super()._scope() as binding:
            require(getattr(self._scope_local,'binding',None) is None, 'nested_component_scope_refused')
            self._scope_local.binding=binding
            try:yield binding
            finally:self._scope_local.binding=None

    def review_selected(self, directory, *, expected):
        require(Path(directory)==expected.directory, 'organizer_directory_changed')
        verify_directory(expected.directory,expected.inventory_sha256,COMPONENT,expected.version,payload_policy=POLICY)
        return expected

    def _observe(self, root, base, owner):
        installed=super()._observe(root,base,owner)
        self._observed_connection=self.original_store.selected(self.original_store.observe())
        return installed

    def signed_selection(self, selected):
        """Reconstruct publisher evidence only from the authenticated original journal chain."""
        require(type(selected) is ManagedOrganizerSelection, 'managed_organizer_selection_required')
        binding=getattr(self._scope_local,'binding',None)
        require(binding is not None, 'original_component_scope_required')
        root,base,owner,check=binding;check()
        journal=read_json(base/'transaction.json');seen=set()
        while True:
            validate_journal(journal,COMPONENT,owner,self.acquisition.trust)
            pin=sha256(json.dumps(journal,sort_keys=True).encode()).hexdigest()
            require(pin not in seen,'cyclic_component_history');seen.add(pin)
            target=journal['target']
            if (root/target['directory'],target['inventory_sha256'],target['version']) == (
                    selected.directory,selected.inventory_sha256,selected.version):
                message=base64.b64decode(journal['manifest'],validate=True)
                signature=base64.b64decode(journal['signature'],validate=True)
                saved=self.original_store.selected(self.original_store.observe())
                revision=(saved.binding_revision if saved is not None and
                    (saved.directory,saved.version,saved.inventory_sha256)==
                    (selected.directory,selected.version,selected.inventory_sha256) else str(uuid4()))
                result=review_existing_package(selected.directory,manifest_bytes=message,signature_bytes=signature,
                    trust_bytes=self.acquisition.trust,expected_version=selected.version,binding_revision=revision)
                check();return result
            raw=prior_journal_bytes(base,journal,COMPONENT,owner,self.acquisition.trust)
            require(raw is not None,'signed_organizer_origin_unavailable')
            journal=json.loads(raw)

    def _native_health(self, selected):
        require(self.native_owner is not None,'original_organizer_native_owner_required')
        # Stock confirm_health calls its native callback outside its own scope.
        # Re-admit signed evidence under a fresh original component scope.
        with self._scope() as (root,base,owner,check):
            signed=self.signed_selection(selected)
            require(self.original_store.selected(self.original_store.observe())==signed,
                    'saved_organizer_changed_review_again')
            result=self.native_owner.native_health(signed)
            check();return result

    def launch(self, *, confirmed=False):
        require(confirmed is True,'explicit_organizer_launch_required')
        require(self.native_owner is not None,'original_organizer_native_owner_required')
        with self._scope() as (root,base,owner,check):
            installed=self._observe(root,base,owner)
            require(installed.selected is not None and not installed.pending and not installed.connection_pending,
                    'connected_organizer_required')
            selected=self.signed_selection(installed.selected)
            require(self.original_store.selected(self.original_store.observe())==selected,
                    'saved_organizer_changed_review_again')
            check()
        # Release staging exclusion before creation; original native capability
        # independently re-admits its exact selected program and root generation.
        return self.native_owner.launch(selected)

    @property
    def launch_available(self):
        if self.native_owner is None or not self.native_owner.available:return False
        selection=self.native_owner.normal_entry_session.organizer_selection
        return self._observed_connection is not None and selection.selected==self._observed_connection
