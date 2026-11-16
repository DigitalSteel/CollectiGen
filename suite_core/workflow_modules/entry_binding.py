"""Frozen workflow entry binding. Signed review is separate from native authority.

The original launcher supplies the trust and peer pins from its admitted package
configuration. This module does not parse a received offer or CLI into authority.
Imports and construction perform no workspace/service initialization.
"""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re
import sys

from core.workflow_connection import WorkflowConnection, review_existing_package
from suite_core.discovery.component_payload_policy import WORKFLOW_FROZEN_PAYLOAD_POLICY
from suite_core.discovery.component_release_signature import inventory_json
from suite_core.discovery.component_staging import guard
from suite_core.installation import InstallMode, SuiteLayout
from suite_core.workflow_modules.contracts import WORKFLOW_EXECUTABLES


def require(value, reason):
    if not value:
        raise ValueError(reason)


def digest(value):
    require(type(value) is str and re.fullmatch('[0-9a-f]{64}', value),
            'independently_admitted_workflow_pin_required')
    return value


@dataclass(frozen=True)
class ModuleEntryBinding:
    layout: SuiteLayout
    selected: WorkflowConnection
    manifest_bytes: bytes
    signature_bytes: bytes
    trust_bytes: bytes
    expected_trust_sha256: str
    expected_peer_image_sha256: str
    restart_arguments: tuple[str, ...]
    executable: Path

    @property
    def settings_path(self):
        # Stock native channel binding field; no settings read/creation here.
        return self.workspace_root / 'generation-settings.json'

    @property
    def workspace_root(self):
        return self.layout.data_root / 'Manager'

    @property
    def component_id(self):
        return self.selected.component_id

    def validate(self):
        require(type(self.layout) is SuiteLayout and self.layout.mode is InstallMode.PORTABLE
                and self.layout.data_root == self.layout.code_root / 'Data',
                'original_portable_workflow_layout_required')
        require(type(self.selected) is WorkflowConnection, 'typed_workflow_selection_required')
        require(self.selected.directory == self.layout.code_root / 'Apps' / self.component_id / self.selected.version,
                'exact_workflow_package_root')
        require(getattr(sys, 'frozen', False) is True and self.executable == Path(sys.executable)
                and self.executable == self.selected.directory / WORKFLOW_EXECUTABLES[self.component_id],
                'exact_frozen_workflow_entry_required')
        require(type(self.restart_arguments) is tuple
                and all(type(item) is str and len(item) <= 4096 and '\0' not in item for item in self.restart_arguments)
                and self.restart_arguments == tuple(sys.argv[1:]), 'actual_workflow_arguments_required')
        digest(self.expected_peer_image_sha256)
        digest(self.expected_trust_sha256)
        require(all(type(value) is bytes and 0 < len(value) <= 65536 for value in
                    (self.manifest_bytes, self.signature_bytes, self.trust_bytes)),
                'bounded_signed_workflow_admission_required')
        require(sha256(self.trust_bytes).hexdigest() == self.expected_trust_sha256,
                'independent_workflow_trust_pin_changed')
        for path in (self.layout.code_root, self.layout.data_root, self.workspace_root,
                     self.settings_path, self.selected.directory, self.executable):
            guard(path)
        review_existing_package(self.component_id, self.selected.directory,
            manifest_bytes=self.manifest_bytes, signature_bytes=self.signature_bytes,
            trust_bytes=self.trust_bytes, expected_version=self.selected.version,
            binding_revision=self.selected.binding_revision, expected=self.selected)
        path = self.selected.directory / 'BOOTSTRAP_INVENTORY.json'
        guard(path)
        require(path.stat().st_size <= WORKFLOW_FROZEN_PAYLOAD_POLICY.max_inventory_json_bytes,
                'workflow_inventory_size')
        raw = path.read_bytes()
        require(sha256(raw).hexdigest() == self.selected.inventory_sha256, 'workflow_inventory_changed')
        rows = inventory_json(raw, max_bytes=WORKFLOW_FROZEN_PAYLOAD_POLICY.max_inventory_json_bytes)['files']
        matches = [row for row in rows if row['path'] == WORKFLOW_EXECUTABLES[self.component_id]]
        require(len(matches) == 1, 'exact_workflow_executable_inventory_row')
        return digest(matches[0]['sha256'])


def admit_module_entry(*, code_root, data_root, selected, manifest_bytes, signature_bytes,
                       trust_bytes, expected_trust_sha256, expected_peer_image_sha256):
    """Review independent installed-package inputs; result still needs native admission.

    No offer/CLI trust fallback exists. Missing compiled/admitted pins fail closed.
    The caller must obtain these inputs from the original admission, not a peer
    payload. ModuleEntrySession independently checks the native child and peer.
    """
    binding = ModuleEntryBinding(SuiteLayout(Path(code_root), Path(data_root), InstallMode.PORTABLE),
        selected, manifest_bytes, signature_bytes, trust_bytes, expected_trust_sha256,
        expected_peer_image_sha256, tuple(sys.argv[1:]), Path(sys.executable))
    binding.validate()
    return binding
