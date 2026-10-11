"""Tiny inert packaging outputs, canonical inventory and signed pair admission."""
from hashlib import sha256
import json
import subprocess
import sys
import zipfile

import pytest
from tools.build_component_package import build_package
from suite_core.discovery.component_staging import create_disposable_root, INVENTORY
from suite_core.discovery.offline_component_producer import PackageInput, qualify_pair
from suite_core.discovery.component_release_signature import verify_component_release
from suite_core.installation import ComponentId
from test_component_staging import COMPONENT, TRUST, sign


def payload(tmp_path, version='0.1.0-alpha.9', component='collectigen.suite-manager'):
    root = tmp_path / ('payload-' + version)
    wrapped = root / 'App'
    wrapped.mkdir(parents=True)
    files = {'bin/inert.txt': ('INERT ' + version).encode(), '_internal/MODULE_MANIFEST.json': json.dumps({
        'schema_version': 1, 'module_id': component, 'software_version': version}).encode()}
    for name, content in files.items():
        file = wrapped / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content)
    runtime = {'schema_version': 1, 'component': 'manager' if component == COMPONENT.value else 'encyclopedia',
        'feature_version': version, 'os': 'windows', 'architecture': 'x86_64',
        'inventory_excludes_itself': 'RUNTIME_MANIFEST.json', 'GUI_qualified': False,
        'files': [{'path': n, 'size_bytes': len(v), 'sha256': sha256(v).hexdigest()} for n, v in files.items()]}
    (wrapped / 'RUNTIME_MANIFEST.json').write_text(json.dumps(runtime), encoding='utf-8')
    return root


def options(root, output, version='0.1.0-alpha.9', component=COMPONENT.value):
    return dict(payload_root=root, output_root=output, runtime_manifest='App/RUNTIME_MANIFEST.json',
        module_manifest='App/_internal/MODULE_MANIFEST.json', component_id=component, software_version=version,
        os_name='windows', architecture='x86_64', artifact_name='fixture-' + version + '.zip',
        supported_from='0.1.0-alpha.9', published_at='2026-10-06T00:00:00Z', release_notes='INERT fixture only.')


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('component', [COMPONENT.value, 'collectigen.tag-encyclopedia'])
def test_output_has_exact_inventory_and_unsigned_envelope_with_deterministic_bytes(tmp_path, component):
    root = payload(tmp_path, component=component)
    before = snapshot(root)
    first, second = tmp_path / 'one', tmp_path / 'two'
    receipt = build_package(**options(root, first, component=component))
    build_package(**options(root, second, component=component))
    assert snapshot(root) == before
    assert snapshot(first) == snapshot(second)
    assert not (first / 'INCOMPLETE.json').exists()
    assert not receipt['signed'] and not receipt['production_install_authorized'] and not receipt['native_health_verified']
    raw = (first / INVENTORY).read_bytes()
    inventory = json.loads(raw)
    assert [r['path'] for r in inventory['files']] == sorted(before)
    assert INVENTORY not in [r['path'] for r in inventory['files']]
    with zipfile.ZipFile(next(first.glob('*.zip'))) as z:
        assert z.namelist() == sorted((*before, INVENTORY))
        assert z.read(INVENTORY) == raw
        for row in inventory['files']:
            actual = z.read(row['path'])
            assert actual == before[row['path']]
            assert len(actual) == row['size_bytes'] and sha256(actual).hexdigest() == row['sha256']
    envelope = (first / 'release-manifest.input.json').read_bytes()
    verified = verify_component_release(envelope, sign(envelope), TRUST, expected_component=ComponentId(component),
        expected_version='0.1.0-alpha.9', expected_channel='beta', expected_os='windows', expected_architecture='x86_64')
    assert verified.artifact.portable_manifest_sha256 == sha256(raw).hexdigest()
    assert verified.artifact.sha256 == receipt['archive_sha256']


def test_two_generated_inert_versions_qualify_through_canonical_validator(tmp_path):
    inputs = []
    for version in ('0.1.0-alpha.9', '0.1.0-alpha.10'):
        root = payload(tmp_path, version)
        out = tmp_path / ('out-' + version)
        build_package(**options(root, out, version))
        manifest = (out / 'release-manifest.input.json').read_bytes()
        inputs.append(PackageInput(next(out.glob('*.zip')), manifest, sign(manifest)))
    scratch = create_disposable_root(tmp_path / 'synthetic-pair')
    proof = qualify_pair(*inputs, scratch_root=scratch, trust_bytes=TRUST, component_id=COMPONENT,
        prior_version='0.1.0-alpha.9', target_version='0.1.0-alpha.10', os_name='windows', architecture='x86_64')
    assert not proof.production_install_authorized and not proof.native_health_verified
    assert len(proof.prior_member_pins) == len(proof.target_member_pins) == 3
    assert not list(scratch.rglob('offline-qualification-*'))


@pytest.mark.parametrize('problem', ['platform', 'architecture', 'version', 'component', 'wrong_hash', 'extra',
    'missing', 'nested_logs', 'inventory_present', 'duplicate', 'unknown_component', 'builder_platform', 'boolean_schema'])
def test_rejects_mismatched_and_excluded_inputs_before_output_write(tmp_path, problem):
    root = payload(tmp_path)
    runtime_path = root / 'App/RUNTIME_MANIFEST.json'
    runtime = json.loads(runtime_path.read_bytes())
    args = options(root, tmp_path / 'out')
    if problem in ('platform', 'architecture', 'version', 'component'):
        field = {'platform': 'os', 'architecture': 'architecture', 'version': 'feature_version', 'component': 'component'}[problem]
        runtime[field] = {'platform': 'linux', 'architecture': 'aarch64', 'version': '0.1.0-alpha.10', 'component': 'encyclopedia'}[problem]
    elif problem == 'wrong_hash':
        runtime['files'][0]['sha256'] = '0' * 64
    elif problem == 'duplicate':
        runtime['files'].append(runtime['files'][0].copy())
    elif problem in ('extra', 'nested_logs', 'inventory_present'):
        name = {'extra': 'extra.txt', 'nested_logs': 'App/logs/accidental.txt', 'inventory_present': INVENTORY}[problem]
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'INERT')
    elif problem == 'missing':
        (root / 'App/bin/inert.txt').unlink()
    elif problem == 'unknown_component':
        args['component_id'] = 'collectigen.unknown'
    elif problem == 'builder_platform':
        args['os_name'] = 'linux'
    elif problem == 'boolean_schema':
        runtime['schema_version'] = True
    runtime_path.write_text(json.dumps(runtime), encoding='utf-8')
    before = snapshot(root)
    with pytest.raises(ValueError):
        build_package(**args)
    assert snapshot(root) == before and not (tmp_path / 'out').exists()


def test_never_overwrites_output_or_places_output_in_input(tmp_path):
    root = payload(tmp_path)
    existing = tmp_path / 'exists'
    existing.mkdir()
    (existing / 'keep.txt').write_bytes(b'KEEP')
    with pytest.raises(ValueError):
        build_package(**options(root, existing))
    assert (existing / 'keep.txt').read_bytes() == b'KEEP'
    with pytest.raises(ValueError):
        build_package(**options(root, root / 'new-output'))
    assert not (root / 'new-output').exists()


def test_cli_consumes_explicit_fixture_and_emits_unsigned_input(tmp_path):
    root = payload(tmp_path)
    args = options(root, tmp_path / 'cli-out')
    notes = tmp_path / 'fixture-notes.txt'
    notes.write_text(args.pop('release_notes'), encoding='utf-8')
    command = [sys.executable, '-B', 'tools/build_component_package.py']
    for name, value in args.items():
        command.extend(['--' + name.replace('_', '-'), str(value)])
    command.extend(['--release-notes-file', str(notes)])
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)['signed'] is False
    assert (tmp_path / 'cli-out/release-manifest.input.json').exists()


def test_stale_input_addition_is_rejected_before_any_output(tmp_path, monkeypatch):
    import tools.build_component_package as builder
    root = payload(tmp_path)
    original = builder.produce_inventory
    def changed(*args, **kwargs):
        inventory = original(*args, **kwargs)
        (root / 'added-during-build.txt').write_bytes(b'INERT')
        return inventory
    monkeypatch.setattr(builder, 'produce_inventory', changed)
    with pytest.raises(ValueError, match='payload_closure_changed'):
        build_package(**options(root, tmp_path / 'out'))
    assert not (tmp_path / 'out').exists()


def test_failed_archive_readback_retains_incomplete_output_without_touching_input(tmp_path, monkeypatch):
    root = payload(tmp_path)
    before = snapshot(root)
    original = zipfile.ZipFile.writestr
    def corrupted(self, info, content, **kwargs):
        return original(self, info, content + b'CORRUPT', **kwargs)
    monkeypatch.setattr(zipfile.ZipFile, 'writestr', corrupted)
    with pytest.raises(ValueError, match='output_readback'):
        build_package(**options(root, tmp_path / 'out'))
    assert snapshot(root) == before
    assert (tmp_path / 'out/INCOMPLETE.json').exists()
    assert not (tmp_path / 'out/packaging-receipt.json').exists()


@pytest.mark.parametrize('os_name,architecture,suffix,reason', [
    ('linux', 'x86_64', '.tar.gz', 'builder_windows_x64_only'),
    ('linux', 'aarch64', '.tar.gz', 'builder_windows_x64_only'),
    ('linux', 'x86_64', '.zip', 'builder_windows_x64_only'),
    ('windows', 'x86_64', '.tar.gz', 'artifact_basename'),
])
def test_unadmitted_linux_and_tar_formats_stay_disabled_even_with_matching_metadata(
        tmp_path, os_name, architecture, suffix, reason):
    root = payload(tmp_path)
    runtime_path = root / 'App/RUNTIME_MANIFEST.json'
    runtime = json.loads(runtime_path.read_bytes())
    runtime.update(os=os_name, architecture=architecture)
    runtime_path.write_text(json.dumps(runtime), encoding='utf-8')
    before = snapshot(root)
    args = options(root, tmp_path / 'unsupported-output')
    args.update(os_name=os_name, architecture=architecture, artifact_name='inert' + suffix)
    with pytest.raises(ValueError, match=reason):
        build_package(**args)
    assert snapshot(root) == before
    assert not (tmp_path / 'unsupported-output').exists()


def test_dot_dot_output_alias_inside_payload_is_rejected_without_writes(tmp_path):
    root = payload(tmp_path)
    before = snapshot(root)
    anchor = tmp_path / 'sibling'
    anchor.mkdir()
    alias = anchor / '..' / root.name / 'generated-output'
    assert alias.resolve().is_relative_to(root)
    with pytest.raises(ValueError, match='separate_output_required'):
        build_package(**options(root, alias))
    assert snapshot(root) == before
    assert not (root / 'generated-output').exists()


def test_padded_raw_notes_over_canonical_limit_rejected_before_output(tmp_path):
    root = payload(tmp_path)
    before = snapshot(root)
    args = options(root, tmp_path / 'notes-output')
    args['release_notes'] = ' ' * 100000 + 'x'
    with pytest.raises(ValueError, match='release_notes'):
        build_package(**args)
    assert snapshot(root) == before
    assert not (tmp_path / 'notes-output').exists()
