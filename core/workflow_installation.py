"""Finite workflow publication adapters over the original component transaction kernel."""
import base64
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from hashlib import sha256
import json
from pathlib import Path
from threading import local
from uuid import uuid4

from core.encyclopedia_installation import EncyclopediaInstallation
from core.workflow_connection import workflow_component, review_existing_package
from core.workflow_connection_settings import WorkflowConnectionStore
from suite_core.discovery.component_payload_policy import FROZEN_PAYLOAD_POLICY
from suite_core.discovery.component_staging import (
    read_json, validate_journal, prior_journal_bytes, require, verify_directory)
from suite_core.workflow_modules.contracts import WORKFLOW_EXECUTABLES


@dataclass(frozen=True)
class ManagedWorkflowSelection:
    component_id: str
    directory: Path
    inventory_sha256: str
    version: str

    def mapping(self):
        return {'schema_version': 1, 'component_id': self.component_id,
                'directory': str(self.directory), 'inventory_sha256': self.inventory_sha256,
                'version': self.version}


class _WorkflowPublication:
    def __init__(self, service, store):
        self.service, self.store, self.settings = service, store, store.settings

    def observe(self):
        return self.store.observe()

    def selected(self, snapshot):
        selected = self.store.selected(snapshot)
        return None if selected is None else ManagedWorkflowSelection(selected.component_id,
            selected.directory, selected.inventory_sha256, selected.version)

    def save(self, selected, *, expected):
        return self.store.save(self.service.signed_selection(selected), expected=expected)

    def clear(self, *, expected):
        return self.store.clear(expected=expected)


class WorkflowInstallation(EncyclopediaInstallation):
    """Stock serial install/recovery with optional original workflow native owner."""
    payload_policy = FROZEN_PAYLOAD_POLICY

    def __init__(self, component_id, acquisition, store, *, admission=None, native_owner=None):
        self.component = workflow_component(component_id)
        require(type(store) is WorkflowConnectionStore and store.component_id == component_id,
                'same_workflow_store_required')
        self.original_store = store
        if native_owner is not None:
            from core.workflow_native_owner import WorkflowWorkspaceOwner
            require(type(native_owner) is WorkflowWorkspaceOwner, 'original_workflow_native_owner_required')
        self.native_owner = native_owner
        self.connection_type = partial(ManagedWorkflowSelection, component_id)
        self._scope_local = local()
        super().__init__(acquisition, _WorkflowPublication(self, store), admission=admission,
                         native_health=self._native_health if native_owner is not None else None)

    @contextmanager
    def _scope(self):
        with super()._scope() as binding:
            require(getattr(self._scope_local, 'binding', None) is None,
                    'nested_workflow_scope_refused')
            self._scope_local.binding = binding
            try:
                yield binding
            finally:
                self._scope_local.binding = None

    def review_selected(self, directory, *, expected, checkpoint=None):
        require(type(expected) is ManagedWorkflowSelection
                and expected.component_id == self.component.value
                and Path(directory) == expected.directory, 'same_managed_workflow_required')
        profile = verify_directory(expected.directory, expected.inventory_sha256, self.component,
            expected.version, payload_policy=self.payload_policy,
            **({'checkpoint': checkpoint} if checkpoint is not None else {}))
        require(WORKFLOW_EXECUTABLES[self.component.value] in {row['path'] for row in profile['files']},
                'workflow_entry_contract_missing')
        return expected

    def _observe(self, root, base, owner, *, checkpoint=None):
        installed = super()._observe(root, base, owner, checkpoint=checkpoint)
        saved = self.original_store.selected(self.original_store.observe())
        selected = installed.selected
        if saved is not None and selected is not None and (
                saved.directory, saved.inventory_sha256, saved.version) == (
                selected.directory, selected.inventory_sha256, selected.version):
            require(self.signed_selection(selected) == saved,
                    'saved_workflow_signed_identity_changed')
        return installed

    def signed_selection(self, selected):
        """Recover exact publisher evidence from the authenticated stock journal chain."""
        require(type(selected) is ManagedWorkflowSelection
                and selected.component_id == self.component.value, 'same_managed_workflow_required')
        binding = getattr(self._scope_local, 'binding', None)
        require(binding is not None, 'original_component_scope_required')
        root, base, owner, check = binding
        check()
        journal = read_json(base / 'transaction.json')
        seen = set()
        while True:
            validate_journal(journal, self.component, owner, self.acquisition.trust)
            pin = sha256(json.dumps(journal, sort_keys=True).encode()).hexdigest()
            require(pin not in seen, 'cyclic_component_history')
            seen.add(pin)
            target = journal['target']
            if (root / target['directory'], target['inventory_sha256'], target['version']) == (
                    selected.directory, selected.inventory_sha256, selected.version):
                saved = self.original_store.selected(self.original_store.observe())
                revision = (saved.binding_revision if saved is not None and
                    (saved.directory, saved.version, saved.inventory_sha256) ==
                    (selected.directory, selected.version, selected.inventory_sha256) else uuid4().hex)
                # The validated journal binds channel and exact signed origin.
                message = base64.b64decode(journal['manifest'], validate=True)
                result = review_existing_package(self.component.value, selected.directory,
                    manifest_bytes=message,
                    signature_bytes=base64.b64decode(journal['signature'], validate=True),
                    trust_bytes=self.acquisition.trust, expected_version=selected.version,
                    binding_revision=revision, channel=json.loads(message)['channel'])
                check()
                return result
            raw = prior_journal_bytes(base, journal, self.component, owner, self.acquisition.trust)
            require(raw is not None, 'signed_workflow_origin_unavailable')
            journal = json.loads(raw)

    @property
    def launch_available(self):
        if self.native_owner is None or not self.native_owner.available(self.component.value):
            return False
        selected = self.original_store.selected(self.original_store.observe())
        admitted = self.native_owner.session.workflow_selections[self.component.value]
        return selected is not None and admitted.selected == selected

    def launch(self, *, confirmed=False, should_cancel=None):
        require(confirmed is True, 'explicit_workflow_launch_required')
        require(self.native_owner is not None, 'original_workflow_native_owner_required')
        if should_cancel is not None and should_cancel():
            raise ValueError('workflow_launch_cancelled_before_creation')
        with self._scope() as (root, base, owner, check):
            installed = self._observe(root, base, owner)
            require(installed.selected is not None and not installed.pending and not installed.connection_pending,
                    'connected_workflow_required')
            selected = self.signed_selection(installed.selected)
            require(self.original_store.selected(self.original_store.observe()) == selected,
                    'saved_workflow_changed_review_again')
            check()
        if should_cancel is not None and should_cancel():
            raise ValueError('workflow_launch_cancelled_before_creation')
        return self.native_owner.launch(selected)

    def _native_health(self, selected):
        with self._scope() as (_root, _base, _owner, check):
            signed = self.signed_selection(selected)
            require(self.original_store.selected(self.original_store.observe()) == signed,
                    'saved_workflow_changed_review_again')
            result = self.native_owner.native_health(signed)
            check()
            return result

    def confirm_health(self, **kwargs):
        require(self.native_owner is not None, 'original_workflow_native_owner_required')
        return super().confirm_health(**kwargs)
