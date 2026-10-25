"""Synthetic routing only; never launches native entries or installs packages."""
from hashlib import sha256
import json
import sys
from types import SimpleNamespace

import pytest
from core.encyclopedia_connection import COMPONENT, KEY
from core.encyclopedia_enrollment import EncyclopediaEnrollmentContext, StandaloneEncyclopediaEnrollment, compiled_enrollment_needed
from suite_core.encyclopedia import bootstrap_entry, entry_binding


@pytest.fixture
def profile(tmp_path, monkeypatch):
    root = tmp_path / 'root'; root.mkdir()
    data = root / 'Data'; data.mkdir()
    program = root / 'TagEncyclopedia.exe'; program.write_bytes(b'INERT ROOT, NEVER EXECUTE')
    trust = b'INERT PUBLIC CONTEXT ONLY'
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(program))
    value = entry_binding.StandaloneEntryProfile(root, data, data / 'settings.json',
        sha256(program.read_bytes()).hexdigest(), trust, sha256(trust).hexdigest(), 'beta', None)
    monkeypatch.setattr(bootstrap_entry, 'PROFILE', value)
    return value


def context(profile):
    return EncyclopediaEnrollmentContext(profile.root, profile.data_root, profile.settings_path,
        profile.bootstrap_sha256, profile.trust_bytes, profile.trust_sha256)


def test_only_absent_state_routes_to_original_enrollment_factory(profile, monkeypatch):
    calls = []
    expected = object()
    monkeypatch.setattr(entry_binding, 'create_enrollment_entry',
        lambda supplied: calls.append(('enroll', supplied)) or expected)
    monkeypatch.setattr(entry_binding, 'create_root_entry', lambda supplied: pytest.fail('no installed selection'))
    assert compiled_enrollment_needed(context(profile))
    assert bootstrap_entry.from_compiled_profile() is expected
    assert calls == [('enroll', profile)]
    assert not (profile.root / 'State').exists() and not profile.settings_path.exists()


@pytest.mark.parametrize('marker', ['apps', 'component-origin.json', 'active.json', 'transaction.json',
    'pending-transaction.json', 'connection-pending.json'])
def test_existing_or_corrupt_marker_never_falls_back_to_enrollment(profile, monkeypatch, marker):
    if marker == 'apps':
        (profile.root / 'Apps' / COMPONENT.value).mkdir(parents=True)
    else:
        path = profile.root / 'State' / COMPONENT.value / marker
        path.parent.mkdir(parents=True); path.write_bytes(b'CORRUPT, DO NOT ADOPT')
    monkeypatch.setattr(entry_binding, 'create_enrollment_entry', lambda supplied: pytest.fail('not a fresh installation'))
    def held(supplied): raise ValueError('original installed admission held')
    monkeypatch.setattr(entry_binding, 'create_root_entry', held)
    assert not compiled_enrollment_needed(context(profile))
    with pytest.raises(ValueError, match='original installed admission held'):
        bootstrap_entry.from_compiled_profile()


def test_saved_reader_prevents_fresh_enrollment_without_markers(profile):
    profile.settings_path.write_text(json.dumps({KEY: {'schema_version': 1,
        'directory': str(profile.root / 'OldReader'), 'inventory_sha256': 'a' * 64, 'version': '0.1.0-alpha.1'}}))
    assert not compiled_enrollment_needed(context(profile))


def test_enrollment_return_is_retained_and_never_launches_before_install(monkeypatch):
    adapter = object.__new__(StandaloneEncyclopediaEnrollment)
    events = []; adapter.subject = object()
    adapter.owner = SimpleNamespace(_standalone_enrollment_owned=lambda supplied:
        events.append(supplied) if supplied is adapter.subject else pytest.fail('subject changed'))
    monkeypatch.setattr(bootstrap_entry, 'from_compiled_profile', lambda: adapter)
    monkeypatch.setattr(entry_binding, 'start_original_reader', lambda backend: pytest.fail('no reader before explicit install'))
    assert entry_binding.start_compiled_control() is adapter
    assert events == [adapter.subject]


def test_original_reader_start_rejects_structural_backend_before_creation():
    with pytest.raises(ValueError, match='original_reader_bootstrap_required'):
        entry_binding.start_original_reader(SimpleNamespace(launch_current=lambda: pytest.fail('no duck-typed birth')))


def test_enrollment_close_uses_original_owner_and_refuses_after_handoff():
    adapter = object.__new__(StandaloneEncyclopediaEnrollment)
    adapter.subject = object(); adapter._reader_backend = None
    calls = []; adapter.owner = SimpleNamespace(close_standalone_enrollment=lambda supplied: calls.append(supplied) or 'closed')
    assert adapter.close() == 'closed' and calls == [adapter.subject]
    adapter._reader_backend = object()
    with pytest.raises(ValueError, match='original_reader_root_retained_after_enrollment_handoff'):
        adapter.close()
    assert calls == [adapter.subject]


def test_inactive_profile_retains_legacy_route(monkeypatch):
    monkeypatch.setattr(bootstrap_entry, 'PROFILE', None)
    assert bootstrap_entry.from_compiled_profile() is None


def test_normal_reader_profile_performs_no_filesystem_or_acquisition_before_stock_admission(tmp_path, monkeypatch):
    from pathlib import Path
    from tests.test_encyclopedia_qualification_build import normal_namespace
    root = tmp_path / 'portable with spaces'; namespace = normal_namespace(root, monkeypatch)
    actual = root / 'Apps/collectigen.tag-encyclopedia/versions/v1/TagEncyclopedia/TagEncyclopedia.exe'
    monkeypatch.setattr(sys, 'executable', str(actual))
    monkeypatch.setattr(sys, 'argv', [str(actual), '--encyclopedia-code-root', str(root),
        '--encyclopedia-data-root', str(root / 'Data'), '--encyclopedia-settings',
        str(root / 'Data/Encyclopedia/settings.json'), '--encyclopedia-context-sha256', 'a' * 64])
    def deny(*args, **kwargs): pytest.fail('Reader must defer filesystem and acquisition to stock admission')
    namespace['_local_drive'] = lambda path: None
    for name in ('guard', 'sha256_file', 'ComponentAcquisition', 'ComponentReleaseTransport', 'uuid4'):
        namespace[name] = deny
    with monkeypatch.context() as scope:
        for name in ('mkdir', 'read_bytes', 'lstat', 'resolve', 'is_dir', 'is_file'):
            scope.setattr(Path, name, deny)
        supplied = namespace['make_profile']('reader')
    assert type(supplied) is entry_binding.StandaloneEntryProfile
    assert supplied.root == root and supplied.acquisition is None and supplied.bootstrap_sha256 is None
    assert not (root / 'Data').exists()


@pytest.mark.parametrize('change', ['duplicate', 'missing', 'wrong_data', 'wrong_settings', 'custody', 'wrong_image'])
def test_normal_reader_grammar_or_layout_refuses_before_io(tmp_path, monkeypatch, change):
    from tests.test_encyclopedia_qualification_build import normal_namespace
    root = tmp_path / 'portable'; namespace = normal_namespace(root, monkeypatch)
    actual = root / 'Apps/collectigen.tag-encyclopedia/versions/v1/TagEncyclopedia/TagEncyclopedia.exe'
    arguments = [str(actual), '--encyclopedia-code-root', str(root), '--encyclopedia-data-root', str(root / 'Data'),
        '--encyclopedia-settings', str(root / 'Data/Encyclopedia/settings.json'),
        '--encyclopedia-context-sha256', 'a' * 64]
    if change == 'duplicate': arguments.extend(['--encyclopedia-code-root', str(root)])
    elif change == 'missing': arguments = arguments[:-2]
    elif change == 'wrong_data': arguments[4] = str(root / 'OtherData')
    elif change == 'wrong_settings': arguments[6] = str(root / 'Data/other.json')
    elif change == 'custody': arguments.append('--encyclopedia-custody')
    else: actual = root / 'TagEncyclopedia.exe'
    monkeypatch.setattr(sys, 'executable', str(actual)); monkeypatch.setattr(sys, 'argv', arguments)
    namespace['guard'] = lambda *args: pytest.fail('must refuse before filesystem observation')
    with pytest.raises(ValueError): namespace['make_profile']('reader')
    assert not (root / 'Data').exists()


def test_normal_custody_requires_exact_role_and_reads_only_guarded_bootstrap(tmp_path, monkeypatch):
    from tests.test_encyclopedia_qualification_build import normal_namespace
    root = tmp_path / 'portable'; namespace = normal_namespace(root, monkeypatch)
    monkeypatch.setattr(sys, 'argv', [sys.executable, '--encyclopedia-custody'])
    events = []; original_guard, original_sha = namespace['guard'], namespace['sha256_file']
    def guard(path):
        assert path == root / 'TagEncyclopedia.exe'; events.append('guard'); return original_guard(path)
    def digest(path):
        assert events == ['guard']; events.append('image'); return original_sha(path)
    namespace['guard'], namespace['sha256_file'] = guard, digest
    for name in ('ComponentAcquisition', 'ComponentReleaseTransport', 'uuid4', 'compiled_enrollment_needed'):
        namespace[name] = lambda *args, **kwargs: pytest.fail('custody must not access mutable setup/cache')
    supplied = namespace['make_profile']('custody')
    assert events == ['guard', 'image'] and supplied.acquisition is None
    assert supplied.bootstrap_sha256 == sha256(b'INERT ROOT; NEVER EXECUTE').hexdigest()
    assert not (root / 'Data').exists()
    monkeypatch.setattr(sys, 'argv', [sys.executable, '--encyclopedia-custody', '--extra'])
    with pytest.raises(ValueError, match='exact_normal_root_argv'):
        namespace['make_profile']('custody')
    assert events == ['guard', 'image']


def test_normal_original_path_guard_precedes_resolve(tmp_path, monkeypatch):
    from tests.test_encyclopedia_qualification_build import normal_namespace
    root = tmp_path / 'portable'; namespace = normal_namespace(root, monkeypatch)
    original = root / 'TagEncyclopedia.exe'; calls = []
    def held(path):
        assert path == original; calls.append(path); raise ValueError('redirected original path')
    namespace['guard'] = held
    namespace['_path'] = lambda path: pytest.fail('must guard before resolve')
    with pytest.raises(ValueError, match='redirected original'):
        namespace['make_profile']('root')
    assert calls == [original] and not (root / 'Data').exists()
