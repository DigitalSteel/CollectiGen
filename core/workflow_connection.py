"""Finite signed Generate/Train software selections; no execution authority."""
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re

from suite_core.installation import ComponentId
from suite_core.discovery.component_payload_policy import WORKFLOW_FROZEN_PAYLOAD_POLICY
from suite_core.discovery.component_release_signature import _version, verify_component_release
from suite_core.discovery.component_staging import guard, verify_directory
from suite_core.workflow_modules.contracts import WORKFLOW_EXECUTABLES


class WorkflowConnectionHold(ValueError):
    pass


def workflow_component(component_id):
    if type(component_id) is not str or component_id not in WORKFLOW_EXECUTABLES:
        raise WorkflowConnectionHold('finite_workflow_component_required')
    return ComponentId(component_id)


@dataclass(frozen=True)
class WorkflowConnection:
    component_id: str
    directory: Path
    version: str
    inventory_sha256: str
    manifest_sha256: str
    binding_revision: str

    def __post_init__(self):
        workflow_component(self.component_id)
        _version(self.version)
        if (not isinstance(self.directory, Path) or not self.directory.is_absolute()
                or '..' in self.directory.parts):
            raise WorkflowConnectionHold('absolute_workflow_directory_required')
        for value in (self.inventory_sha256, self.manifest_sha256):
            if type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None:
                raise WorkflowConnectionHold('exact_workflow_digest_required')
        if type(self.binding_revision) is not str or not 0 < len(self.binding_revision) <= 256:
            raise WorkflowConnectionHold('connection_revision_required')

    @property
    def execution_authorized(self):
        return False

    @property
    def installation_authorized(self):
        return False

    @property
    def identity(self):
        return sha256(json.dumps([self.component_id, str(self.directory), self.version,
            self.inventory_sha256, self.manifest_sha256, self.binding_revision],
            separators=(',', ':')).encode()).hexdigest()


def review_existing_package(component_id, directory, *, manifest_bytes, signature_bytes,
                            trust_bytes, expected_version, binding_revision, expected=None,
                            checkpoint=None, channel='beta'):
    component = workflow_component(component_id)
    root = Path(directory)
    if not root.is_absolute():
        raise WorkflowConnectionHold('absolute_workflow_directory_required')
    guard(root)
    if not root.is_dir():
        raise WorkflowConnectionHold('selected_package_directory_required')
    root = root.resolve(strict=True)
    release = verify_component_release(manifest_bytes, signature_bytes, trust_bytes,
        expected_component=component, expected_version=expected_version,
        expected_channel=channel, expected_os='windows', expected_architecture='x86_64')
    selected = WorkflowConnection(component_id, root, expected_version,
        release.artifact.portable_manifest_sha256, release.manifest_sha256, binding_revision)
    if expected is not None and (type(expected) is not WorkflowConnection or expected != selected):
        raise WorkflowConnectionHold('connected_workflow_changed_review_required')
    review_inventory(selected, checkpoint=checkpoint)
    return selected


def review_inventory(selected, *, checkpoint=None):
    """Inventory consistency only; publisher authentication stays with signed review."""
    if type(selected) is not WorkflowConnection:
        raise WorkflowConnectionHold('typed_workflow_connection_required')
    profile = verify_directory(selected.directory, selected.inventory_sha256,
        workflow_component(selected.component_id), selected.version,
        payload_policy=WORKFLOW_FROZEN_PAYLOAD_POLICY,
        **({'checkpoint': checkpoint} if checkpoint is not None else {}))
    if (profile['platform'] != {'os': 'windows', 'architecture': 'x86_64'}
            or profile.get('runtime_kind') != 'frozen-portable'):
        raise WorkflowConnectionHold('workflow_windows_x64_frozen_required')
    if WORKFLOW_EXECUTABLES[selected.component_id] not in {row['path'] for row in profile['files']}:
        raise WorkflowConnectionHold('workflow_entry_contract_missing')
    return selected
