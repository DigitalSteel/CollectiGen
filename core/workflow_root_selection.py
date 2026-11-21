"""Signed finite workflow selections issued by the original deployment owner.

This review is read-only. A reconstructed selection is not an issued helper
grant, and no selection grants access to the shared working data.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path

from core.workflow_connection import review_existing_package, review_inventory
from core.workflow_connection_settings import WorkflowConnectionStore
from suite_core.discovery.component_payload_policy import WORKFLOW_FROZEN_PAYLOAD_POLICY
from suite_core.discovery.component_release_signature import inventory_json
from suite_core.discovery.component_staging import guard
from suite_core.workflow_modules.contracts import WORKFLOW_EXECUTABLES


_ADMISSION_PROFILE = '_internal/suite_core/workflow_modules/compiled_admission.py'
_MAX_ADMISSION_PROFILE_BYTES = 128 * 1024


def _compiled_admission(raw):
    """Read the stock freezer's literal profile without importing module code."""
    import ast
    from suite_core.workflow_modules.compiled_admission import CompiledWorkflowAdmission

    try:
        assignment, = [node for node in ast.parse(raw).body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == 'PROFILE'
                               for target in node.targets)]
        call = assignment.value
        if (len(assignment.targets) != 1 or not isinstance(call, ast.Call) or call.args
                or not isinstance(call.func, ast.Name) or call.func.id != 'CompiledWorkflowAdmission'):
            raise ValueError('literal profile required')
        values = {arg.arg: ast.literal_eval(arg.value) for arg in call.keywords}
        if len(values) != len(call.keywords) or None in values:
            raise ValueError('unique profile fields required')
        return CompiledWorkflowAdmission(**values)
    except (SyntaxError, TypeError, ValueError, RecursionError) as error:
        raise ValueError('invalid_compiled_workflow_profile') from error


@dataclass(frozen=True)
class RootWorkflowSelection:
    root: Path
    data_root: Path
    settings_path: Path
    selected: object
    manifest_bytes: bytes
    signature_bytes: bytes
    trust_bytes: bytes

    @property
    def component_id(self):
        return self.selected.component_id

    @property
    def helper_role(self):
        return {'collectigen.generate': 'owned-generate', 'collectigen.train': 'owned-train'}[self.component_id]

    @property
    def program(self):
        return self.selected.directory / WORKFLOW_EXECUTABLES[self.component_id]

    @property
    def workspace_root(self):
        return self.data_root / 'Manager'

    @property
    def command(self):
        # These are locators, never workspace or execution authority.
        return (str(self.program), '--suite-code-root', str(self.root),
                '--suite-data-root', str(self.data_root))

    def validate(self, *, checkpoint=None):
        from core.workflow_connection import WorkflowConnection
        if (type(self.selected) is not WorkflowConnection
                or self.component_id not in WORKFLOW_EXECUTABLES
                or any(not isinstance(p, Path) or not p.is_absolute() or '..' in p.parts
                       for p in (self.root, self.data_root, self.settings_path))
                or self.data_root != self.root / 'Data'
                or not self.settings_path.is_relative_to(self.workspace_root)
                or self.selected.directory != self.root / 'Apps' / self.component_id / self.selected.version):
            raise ValueError('original_workflow_roots_required')
        for path in (self.root, self.data_root, self.settings_path, self.workspace_root):
            guard(path)
        store = WorkflowConnectionStore(self.settings_path, self.component_id)
        if store.selected(store.observe()) != self.selected:
            raise ValueError('saved_workflow_selection_changed')
        return review_existing_package(self.component_id, self.selected.directory,
            manifest_bytes=self.manifest_bytes, signature_bytes=self.signature_bytes,
            trust_bytes=self.trust_bytes, expected_version=self.selected.version,
            binding_revision=self.selected.binding_revision, expected=self.selected,
            checkpoint=checkpoint)

    def _context(self):
        return dict(component=self.component_id, directory=str(self.selected.directory),
            inventory_sha256=self.selected.inventory_sha256, manifest_sha256=self.selected.manifest_sha256,
            version=self.selected.version, program=str(self.program), settings_path=str(self.settings_path),
            profile_root=str(self.workspace_root), working_directory=str(self.selected.directory),
            argv_sha256=sha256(json.dumps(list(self.command), sort_keys=True,
                separators=(',', ':'), allow_nan=False).encode()).hexdigest())

    def context(self):
        self.validate()
        return self._context()

    def _program_sha256(self):
        row, = self._inventory_records(WORKFLOW_EXECUTABLES[self.component_id])
        return row['sha256']

    def _inventory_records(self, relative_path):
        path = self.selected.directory / 'BOOTSTRAP_INVENTORY.json'
        guard(path)
        limit = WORKFLOW_FROZEN_PAYLOAD_POLICY.max_inventory_json_bytes
        with path.open('rb') as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError('workflow_inventory_size')
        if sha256(raw).hexdigest() != self.selected.inventory_sha256:
            raise ValueError('workflow_inventory_changed')
        return [row for row in inventory_json(raw, max_bytes=limit)['files'] if row['path'] == relative_path]

    def accepts_manager_version(self, manager_version, *, checkpoint=None):
        """Check the signed selection's compatibility without launching it."""
        from suite_core.discovery.component_release_signature import _version

        version = _version(manager_version)
        self.validate(checkpoint=checkpoint)
        records = self._inventory_records(_ADMISSION_PROFILE)
        if len(records) != 1:
            raise ValueError('signed_compiled_workflow_profile_required')
        row = records[0]
        path = self.selected.directory / _ADMISSION_PROFILE
        guard(path)
        with path.open('rb') as stream:
            raw = stream.read(_MAX_ADMISSION_PROFILE_BYTES + 1)
        if (len(raw) > _MAX_ADMISSION_PROFILE_BYTES or len(raw) != row['size_bytes']
                or sha256(raw).hexdigest() != row['sha256']):
            raise ValueError('signed_compiled_workflow_profile_changed')
        profile = _compiled_admission(raw).validate(self.component_id)
        if profile.software_version != self.selected.version or profile.trust_bytes != self.trust_bytes:
            raise ValueError('signed_compiled_workflow_profile_identity_changed')
        return (version.compare(_version(profile.minimum_manager_version)) >= 0
                and version.compare(_version(profile.maximum_manager_version_exclusive)) < 0)

    @property
    def program_sha256(self):
        self.validate()
        return self._program_sha256()

    def helper_snapshot(self):
        self.validate()
        return self._context(), self._program_sha256()

    def document(self):
        return dict(context=self.context(), manifest_hex=self.manifest_bytes.hex(),
                    signature_hex=self.signature_bytes.hex(), trust_hex=self.trust_bytes.hex())


@dataclass(frozen=True)
class WorkflowHelperWireAdmission:
    context: dict
    program_sha256: str
    selection: RootWorkflowSelection


def selection_from_offer(document, admitted, component_id):
    """Only call after the genuine Manager channel authenticated its Root offer."""
    import re
    if (component_id not in WORKFLOW_EXECUTABLES or type(document) is not dict
            or set(document) != {'context', 'manifest_hex', 'signature_hex', 'trust_hex'}):
        raise ValueError('original_workflow_offer_required')
    signed = []
    for key in ('manifest_hex', 'signature_hex', 'trust_hex'):
        value = document[key]
        if type(value) is not str or not 0 < len(value) <= 131072 or len(value) % 2 or not re.fullmatch('[0-9a-f]+', value):
            raise ValueError('bounded_workflow_signed_offer_required')
        signed.append(bytes.fromhex(value))
    settings = Path(admitted.settings_path)
    if settings.suffix.casefold() == '.ini':
        settings = settings.with_suffix('.json')
    store = WorkflowConnectionStore(settings, component_id)
    selected = store.selected(store.observe())
    if selected is None:
        raise ValueError('saved_workflow_offer_required')
    selection = RootWorkflowSelection(admitted.layout.code_root, admitted.layout.data_root,
                                      settings, selected, *signed)
    if selection.context() != document['context']:
        raise ValueError('original_workflow_offer_changed')
    return selection
