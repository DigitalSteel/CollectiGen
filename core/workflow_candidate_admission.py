"""Pinned pending workflow candidate intent; never trial or release authority.

This document binds an isolated producer output and public trust before artifacts
exist. It carries no artifact qualification, process grant, or user-data consent.
"""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re

from core.training_source_io import read_stable_training_source
from suite_core.discovery.component_release_signature import _json, _trust, _version
from suite_core.discovery.component_staging import guard
from suite_core.installation import _path
from suite_core.workflow_modules.contracts import WORKFLOW_EXECUTABLES

MAX_DOCUMENT_BYTES = 65536
MARKER = Path('_bootstrap/workflow-candidate.json')
_FIELDS = frozenset(('schema', 'purpose', 'candidate_id', 'status', 'suite_root',
    'data_root', 'components', 'manager_version', 'bootstrap_version',
    'workspace_contract_version', 'trust_sha256'))


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _digest(raw, pin, label):
    _require(type(raw) is bytes and 0 < len(raw) <= MAX_DOCUMENT_BYTES
             and type(pin) is str and re.fullmatch('[0-9a-f]{64}', pin)
             and sha256(raw).hexdigest() == pin, label + '_pin_required')


def _root(value):
    _require(type(value) is str and '\0' not in value and value.strip() == value,
             'candidate_canonical_root_required')
    _require(not value.startswith(('//', '\\\\')), 'candidate_local_root_required')
    path = Path(value)
    _require(path.is_absolute() and path.parent != path
             and not any(part in ('.', '..') for part in value.replace('\\', '/').split('/')),
             'candidate_isolated_absolute_root_required')
    guard(path)
    _require(_path(path) == path, 'candidate_canonical_root_required')
    return path


@dataclass(frozen=True)
class CandidateWorkflowAdmission:
    document_bytes: bytes
    document_sha256: str
    trust_bytes: bytes
    trust_sha256: str

    def _validated_document(self):
        _digest(self.document_bytes, self.document_sha256, 'candidate_document')
        _digest(self.trust_bytes, self.trust_sha256, 'candidate_trust')
        _trust(self.trust_bytes)
        document = _json(self.document_bytes, 'workflow candidate')
        _require(set(document) == _FIELDS, 'candidate_exact_schema_required')
        _require(type(document['schema']) is int and document['schema'] == 1
                 and type(document['workspace_contract_version']) is int
                 and document['workspace_contract_version'] == 1,
                 'candidate_contract_version_required')
        _require(document['purpose'] == 'workflow-candidate-evaluation'
                 and document['status'] == 'pending', 'candidate_pending_intent_required')
        _require(type(document['candidate_id']) is str
                 and re.fullmatch('[0-9a-f]{32}', document['candidate_id']),
                 'candidate_exact_id_required')
        suite, data = _root(document['suite_root']), _root(document['data_root'])
        _require(data == suite / 'Data', 'candidate_isolated_data_root_required')
        components = document['components']
        _require(type(components) is dict and 0 < len(components) <= 2
                 and set(components) <= set(WORKFLOW_EXECUTABLES),
                 'candidate_finite_components_required')
        for version in components.values():
            _version(version)
        _version(document['manager_version'])
        _require(type(document['bootstrap_version']) is str
                 and document['bootstrap_version'] == '0.1.1', 'candidate_bootstrap_version_required')
        _require(type(document['trust_sha256']) is str
                 and document['trust_sha256'] == self.trust_sha256, 'candidate_trust_binding_required')
        return document

    def validate(self):
        self._validated_document()
        return self

    def document(self):
        """Return a freshly parsed, independently validated JSON object."""
        return self._validated_document()

    @property
    def suite_root(self):
        return Path(self.document()['suite_root'])

    @property
    def data_root(self):
        return Path(self.document()['data_root'])

    @property
    def candidate_id(self):
        return self.document()['candidate_id']

    @property
    def manager_version(self):
        return self.document()['manager_version']

    @property
    def bootstrap_version(self):
        return self.document()['bootstrap_version']

    def version_for(self, component_id):
        components = self.document()['components']
        _require(type(component_id) is str and component_id in components,
                 'candidate_selected_component_required')
        return components[component_id]

    def check_marker(self, code_root, data_root, component_id=None, manager_version=None):
        """Read only the fixed marker; byte agreement grants no execution authority."""
        document = self.document()
        code = _root(str(code_root)) if isinstance(code_root, (str, Path)) else None
        data = _root(str(data_root)) if isinstance(data_root, (str, Path)) else None
        _require(code == Path(document['suite_root']) and data == Path(document['data_root']),
                 'candidate_caller_roots_changed')
        if component_id is not None:
            self.version_for(component_id)
        if manager_version is not None:
            _version(manager_version)
            _require(manager_version == document['manager_version'], 'candidate_manager_changed')
        marker = code / MARKER
        guard(marker)
        raw, _metadata = read_stable_training_source(marker, max_bytes=MAX_DOCUMENT_BYTES)
        guard(marker)
        _require(raw == self.document_bytes and sha256(raw).hexdigest() == self.document_sha256,
                 'candidate_marker_changed')
        return self
