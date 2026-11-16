"""Measured frozen-workflow closure uses a finite caller-selected policy."""
from dataclasses import replace
from hashlib import sha256
import json
import zipfile
import pytest
from suite_core.installation import ComponentId
from suite_core.discovery.component_payload_policy import (
    FROZEN_PAYLOAD_POLICY, WORKFLOW_FROZEN_PAYLOAD_POLICY as POLICY,
    select_payload_policy, require_payload_policy, payload_relative)
from suite_core.discovery.offline_component_producer import produce_inventory
from suite_core.discovery.component_staging import (verify_directory, extract_component_archive,
    create_disposable_root, stage_and_activate, finish_or_recover, active_token, INVENTORY)
from suite_core.tests.test_component_staging import TRUST, sign
from tools.build_component_package import build_package


@pytest.mark.parametrize('component', ['collectigen.generate', 'collectigen.train'])
def test_only_finite_workflow_windows_x64_selects_measured_policy(component):
    selected = ComponentId(component)
    assert select_payload_policy(selected, 'windows', 'x86_64') is POLICY
    assert select_payload_policy(selected, 'linux', 'x86_64') is FROZEN_PAYLOAD_POLICY
    assert select_payload_policy(selected, 'windows', 'aarch64') is FROZEN_PAYLOAD_POLICY
    assert require_payload_policy(POLICY, selected, {'os': 'windows', 'architecture': 'x86_64'}) is POLICY
    assert POLICY.max_archive_bytes == FROZEN_PAYLOAD_POLICY.max_archive_bytes
    assert POLICY.max_unpacked_bytes == FROZEN_PAYLOAD_POLICY.max_unpacked_bytes
    assert (POLICY.max_inventory_members, POLICY.max_zip_entries, POLICY.max_assembly_tree_entries) == (1536, 1537, 2048)
    assert POLICY.max_inventory_json_bytes == 1024 * 1024
    for bad in (replace(POLICY, max_inventory_members=9999),):
        with pytest.raises(ValueError): require_payload_policy(bad)
    with pytest.raises(ValueError): require_payload_policy(POLICY, ComponentId('collectigen.suite-manager'))
    with pytest.raises(ValueError): require_payload_policy(POLICY, selected, {'os': 'linux', 'architecture': 'x86_64'})
    assert payload_relative('Qt+stock/file', payload_policy=POLICY) == payload_relative('Qt+stock/file', payload_policy=FROZEN_PAYLOAD_POLICY)
    with pytest.raises(ValueError): payload_relative('arbitrary/.qt/file', payload_policy=POLICY)


@pytest.mark.parametrize('count', [1536, 1537])
def test_workflow_inventory_member_bound_is_not_remote_selected(count):
    members = tuple((f'files/f{i}.txt', b'') for i in range(count))
    kwargs = dict(component_id=ComponentId('collectigen.generate'), software_version='0.1.0-alpha.1',
                  os_name='windows', architecture='x86_64', runtime_kind='frozen-portable')
    if count == 1536:
        assert len(json.loads(produce_inventory(members, **kwargs))['files']) == count
    else:
        with pytest.raises(ValueError): produce_inventory(members, **kwargs)
    with pytest.raises(ValueError): produce_inventory(members, **{**kwargs, 'component_id': ComponentId('collectigen.suite-manager')})


@pytest.mark.parametrize('component', ['generate', 'train'])
def test_large_frozen_workflow_packages_extracts_and_revalidates_under_same_policy(tmp_path, component):
    payload = tmp_path / 'payload'; payload.mkdir()
    version = '0.1.0-alpha.1'; identity = 'collectigen.' + component
    files = {f'files/{i:04d}-' + 's' * 100 + '.txt': b'inert' for i in range(900)}
    files[component.title() + '.exe'] = b'INERT, NOT A PROGRAM'
    files['MODULE_MANIFEST.json'] = json.dumps(dict(schema_version=1, module_id=identity, software_version=version)).encode()
    for name, raw in files.items():
        path = payload / name; path.parent.mkdir(exist_ok=True); path.write_bytes(raw)
    runtime = dict(schema_version=1, component=component, feature_version=version, os='windows',
        architecture='x86_64', inventory_excludes_itself='RUNTIME_MANIFEST.json',
        files=[dict(path=n, size_bytes=len(r), sha256=sha256(r).hexdigest()) for n,r in files.items()])
    raw = json.dumps(runtime).encode(); assert len(raw) > 128 * 1024
    (payload / 'RUNTIME_MANIFEST.json').write_bytes(raw)
    output = tmp_path / 'output'
    receipt = build_package(payload, output, runtime_manifest='RUNTIME_MANIFEST.json', module_manifest='MODULE_MANIFEST.json',
        component_id=identity, software_version=version, os_name='windows', architecture='x86_64',
        artifact_name='inert.zip', supported_from=version, published_at='2026-11-16T00:00:00Z', release_notes='Synthetic only.')
    target = tmp_path / 'extracted'
    target.mkdir()
    extract_component_archive(output / 'inert.zip', target, payload_policy=POLICY)
    assert verify_directory(target, receipt['inventory_sha256'], ComponentId(identity), version,
                            payload_policy=POLICY)['component_id'] == identity
    with pytest.raises(ValueError): verify_directory(target, receipt['inventory_sha256'], ComponentId(identity), version)
    root = create_disposable_root(tmp_path / 'disposable-install')
    sentinel = root / 'Data/sentinel.txt'; sentinel.parent.mkdir(); sentinel.write_bytes(b'preserved')
    manifest = (output / 'release-manifest.input.json').read_bytes()
    stage_and_activate(root, output / 'inert.zip', component=ComponentId(identity), version=version,
        os_name='windows', architecture='x86_64', manifest_bytes=manifest, signature_bytes=sign(manifest),
        trust_bytes=TRUST, expected_active_token=active_token(None), operator_approved=True, idle=True,
        payload_policy=POLICY)
    # No application or native health proof: the original recovery restores no app.
    assert finish_or_recover(root, component=ComponentId(identity), trust_bytes=TRUST, payload_policy=POLICY) is None
    assert sentinel.read_bytes() == b'preserved'
    file = target / next(n for n in files if n.startswith('files/')); file.write_bytes(b'changed')
    with pytest.raises(ValueError): verify_directory(target, receipt['inventory_sha256'], ComponentId(identity), version, payload_policy=POLICY)


def test_existing_directory_enforces_inventory_inclusive_unpacked_budget_before_hash(tmp_path, monkeypatch):
    import suite_core.discovery.component_staging as staging
    directory = tmp_path / 'oversize'; directory.mkdir()
    large = directory / 'inert.bin'
    with large.open('wb') as stream:
        stream.truncate(POLICY.max_unpacked_bytes)
    profile = json.dumps(dict(schema_version=1, component_id='collectigen.generate',
        software_version='0.1.0-alpha.1', platform={'os': 'windows', 'architecture': 'x86_64'},
        runtime_kind='frozen-portable', files=[dict(path='inert.bin',
            size_bytes=POLICY.max_unpacked_bytes, sha256='a' * 64)])).encode()
    (directory / INVENTORY).write_bytes(profile)
    monkeypatch.setattr(staging, 'sha256_file', lambda *args, **kwargs: pytest.fail('Budget precedes large payload hashing'))
    with pytest.raises(ValueError, match='payload_bounds'):
        verify_directory(directory, sha256(profile).hexdigest(), ComponentId('collectigen.generate'),
                         '0.1.0-alpha.1', payload_policy=POLICY)
