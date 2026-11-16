"""Pending intent only: synthetic roots/public trust, no process or signing work."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.workflow_candidate_admission import CandidateWorkflowAdmission, MAX_DOCUMENT_BYTES


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


@pytest.fixture
def admission(tmp_path):
    # The stock public-trust schema permits an empty key set. It enrolls no key
    # and cannot verify any signed artifact or grant candidate execution.
    trust = encode({'schema_version': 1, 'keys': []})
    root = tmp_path / 'isolated-candidate'
    value = {'schema': 1, 'purpose': 'workflow-candidate-evaluation',
             'candidate_id': 'a' * 32, 'status': 'pending', 'suite_root': str(root),
             'data_root': str(root / 'Data'),
             'components': {'collectigen.generate': '0.1.0-alpha.1',
                            'collectigen.train': '0.1.0-alpha.1'},
             'manager_version': '0.8.0-alpha.1', 'bootstrap_version': '0.1.1',
             'workspace_contract_version': 1, 'trust_sha256': sha256(trust).hexdigest()}
    raw = encode(value)
    return CandidateWorkflowAdmission(raw, sha256(raw).hexdigest(), trust, sha256(trust).hexdigest())


def changed(admission, **updates):
    value = admission.document(); value.update(updates)
    raw = encode(value)
    return replace(admission, document_bytes=raw, document_sha256=sha256(raw).hexdigest())


def marker(admission):
    path = admission.suite_root / '_bootstrap/workflow-candidate.json'
    path.parent.mkdir(parents=True)
    path.write_bytes(admission.document_bytes)
    return path


def test_pending_identity_and_independent_document_copy(admission):
    assert admission.validate() is admission
    assert admission.candidate_id == 'a' * 32
    assert admission.manager_version == '0.8.0-alpha.1'
    assert admission.bootstrap_version == '0.1.1'
    assert admission.data_root == admission.suite_root / 'Data'
    assert admission.version_for('collectigen.train') == '0.1.0-alpha.1'
    copied = admission.document(); copied['components']['collectigen.train'] = '99.0.0'
    assert admission.version_for('collectigen.train') == '0.1.0-alpha.1'
    assert not admission.suite_root.exists()  # Validation creates no workspace.
    single = changed(admission, components={'collectigen.generate': '0.1.0-alpha.1'})
    assert single.validate() is single
    with pytest.raises(ValueError): single.version_for('collectigen.train')


@pytest.mark.parametrize('field,value', [
    ('schema', True), ('schema', 2), ('workspace_contract_version', True),
    ('workspace_contract_version', 2), ('purpose', 'production'), ('purpose', []),
    ('candidate_id', 'A' * 32), ('candidate_id', 7), ('candidate_id', 'a' * 31),
    ('status', 'qualified'), ('status', 'complete'), ('status', True),
    ('components', {}), ('components', []),
    ('components', {'collectigen.unknown': '0.1.0'}),
    ('components', {'collectigen.generate': True}),
    ('components', {'collectigen.generate': '0.1'}),
    ('components', {'collectigen.generate': '0.1.0+foreign'}),
    ('components', {'collectigen.generate': '01.1.0'}),
    ('manager_version', 1), ('manager_version', ' 0.8.0'),
    ('bootstrap_version', '0.1.0'), ('bootstrap_version', True),
    ('suite_root', 'relative-root'), ('suite_root', None),
    ('data_root', 'relative-data'), ('trust_sha256', 'f' * 64),
    ('artifact_sha256', 'f' * 64),
])
def test_malformed_or_extra_contract_fields_refused(admission, field, value):
    with pytest.raises(ValueError): changed(admission, **{field: value}).validate()


def test_root_scope_refuses_alias_parent_and_foreign_data(admission):
    root = admission.suite_root
    for updates in ({'suite_root': str(root.anchor)},
                    {'suite_root': str(root / '..' / 'foreign')},
                    {'data_root': str(root.parent / 'Data')},
                    {'data_root': str(root / 'Other')},
                    {'suite_root': '//server/share/candidate'}):
        with pytest.raises(ValueError): changed(admission, **updates).validate()


def test_missing_fields_and_mixed_numeric_types_are_not_defaults(admission):
    for field in admission.document():
        value = admission.document(); del value[field]
        raw = encode(value)
        with pytest.raises(ValueError):
            replace(admission, document_bytes=raw, document_sha256=sha256(raw).hexdigest()).validate()
    for field in ('schema', 'workspace_contract_version'):
        with pytest.raises(ValueError): changed(admission, **{field: 1.0}).validate()


@pytest.mark.parametrize('raw', [b'{}', b'[]', b'{"schema":1,"schema":1}',
    b'{"bad":NaN}', b'{"bad":' + b'[' * 17 + b'0' + b']' * 17 + b'}',
    b' ' * (MAX_DOCUMENT_BYTES + 1), b'\xff'],
    ids=['empty', 'array', 'duplicate', 'nan', 'depth', 'oversize', 'encoding'])
def test_strict_json_size_depth_duplicate_and_unknown_guards(admission, raw):
    candidate = replace(admission, document_bytes=raw, document_sha256=sha256(raw).hexdigest())
    with pytest.raises(ValueError): candidate.validate()


def test_exact_document_and_trust_pins_and_trust_schema(admission):
    for updates in ({'document_sha256': '0' * 64}, {'document_sha256': True},
                    {'document_bytes': bytearray(admission.document_bytes)},
                    {'trust_sha256': '0' * 64}, {'trust_bytes': b'{}'},
                    {'trust_bytes': bytearray(admission.trust_bytes)}):
        with pytest.raises(ValueError): replace(admission, **updates).validate()
    malformed = encode({'schema_version': True, 'keys': []})
    candidate = changed(admission, trust_sha256=sha256(malformed).hexdigest())
    candidate = replace(candidate, trust_bytes=malformed, trust_sha256=sha256(malformed).hexdigest())
    with pytest.raises(ValueError): candidate.validate()


def test_fixed_marker_exact_readback_missing_changed_and_caller_mismatch(admission):
    with pytest.raises(FileNotFoundError):
        admission.check_marker(admission.suite_root, admission.data_root)
    path = marker(admission)
    assert admission.check_marker(admission.suite_root, admission.data_root,
        component_id='collectigen.generate', manager_version=admission.manager_version) is admission
    for code, data, options in (
        (admission.suite_root.parent, admission.data_root, {}),
        (admission.suite_root, admission.data_root.parent, {}),
        (admission.suite_root, admission.data_root, {'component_id': 'collectigen.unknown'}),
        (admission.suite_root, admission.data_root, {'component_id': []}),
        (admission.suite_root, admission.data_root, {'manager_version': '0.9.0'})):
        with pytest.raises(ValueError): admission.check_marker(code, data, **options)
    path.write_bytes(admission.document_bytes + b'\n')
    with pytest.raises(ValueError, match='marker_changed'):
        admission.check_marker(admission.suite_root, admission.data_root)
    assert path.read_bytes() == admission.document_bytes + b'\n'


@pytest.mark.parametrize('alias', ['hardlink', 'symlink', 'junction'])
def test_marker_alias_guard_refuses_before_content_read(admission, monkeypatch, alias):
    import stat
    import core.workflow_candidate_admission as contract
    path = marker(admission)
    original = Path.lstat
    def metadata(current, *args, **kwargs):
        if current == path:
            return SimpleNamespace(st_mode=(stat.S_IFLNK if alias == 'symlink' else stat.S_IFREG),
                st_nlink=(2 if alias == 'hardlink' else 1),
                st_file_attributes=(0x400 if alias == 'junction' else 0))
        return original(current, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', metadata)
    monkeypatch.setattr(contract, 'read_stable_training_source',
                        lambda *_a, **_k: pytest.fail('Aliased marker content was read'))
    with pytest.raises(ValueError): admission.check_marker(admission.suite_root, admission.data_root)
