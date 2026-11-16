"""Synthetic signed software, canonical settings and original recovery; no native execution."""
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import json
from types import SimpleNamespace
import zipfile

import pytest

from core.workflow_connection import WorkflowConnection, WorkflowConnectionHold, review_existing_package
from core.workflow_connection_settings import WorkflowConnectionStore, KEYS
from core.workflow_installation import WorkflowInstallation
from suite_core.discovery.component_staging import (ComponentStageAdmission, ComponentStageBinding,
    component_stage_owner, stage_and_activate_admitted, active_token)
from suite_core.installation import ComponentId
from suite_core.tests.test_component_staging import TRUST, sign
from suite_core.tests.test_workflow_component_packages import frozen_input
from tools.build_component_package import build_package


@pytest.fixture(params=['generate', 'train'])
def workflow(request):
    return request.param


def package(tmp_path, workflow):
    args = frozen_input(tmp_path, workflow)
    root = args['payload_root']
    executable = workflow.title() + '.exe'
    content = b'INERT SYNTHETIC BYTES; NEVER EXECUTE'
    (root / executable).write_bytes(content)
    runtime_path = root / 'RUNTIME_MANIFEST.json'
    runtime = json.loads(runtime_path.read_bytes())
    runtime['files'].append({'path': executable, 'size_bytes': len(content),
                             'sha256': sha256(content).hexdigest()})
    runtime_path.write_text(json.dumps(runtime), encoding='utf-8')
    build_package(**args)
    output = args['output_root']
    message = (output / 'release-manifest.input.json').read_bytes()
    signature = sign(message)
    extracted = tmp_path / 'extracted'
    with zipfile.ZipFile(output / args['artifact_name']) as archive:
        archive.extractall(extracted)  # Only this test's generated finite inert archive.
    return args, extracted, message, signature


def reviewed(workflow, directory, message, signature):
    return review_existing_package('collectigen.' + workflow, directory,
        manifest_bytes=message, signature_bytes=signature, trust_bytes=TRUST,
        expected_version='0.1.0-alpha.1', binding_revision='review-1')


def test_signed_connection_rechecks_inventory_and_rejects_cross_component(tmp_path, workflow):
    _, directory, message, signature = package(tmp_path, workflow)
    selected = reviewed(workflow, directory, message, signature)
    assert not selected.execution_authorized and not selected.installation_authorized
    assert selected != replace(selected, binding_revision='review-2')
    assert selected.identity != replace(selected, binding_revision='review-2').identity
    other = 'train' if workflow == 'generate' else 'generate'
    with pytest.raises(ValueError):
        reviewed(other, directory, message, signature)
    (directory / (workflow.title() + '.exe')).write_bytes(b'CHANGED')
    with pytest.raises(ValueError):
        reviewed(workflow, directory, message, signature)


def test_settings_stale_guards_foreign_fields_and_revision_readback(tmp_path, workflow):
    component = 'collectigen.' + workflow
    path = tmp_path / 'manager.json'
    key = KEYS[component]
    path.write_text(json.dumps({'future': {'keep': True}, key: {'future_local': [1, 2]}}))
    store = WorkflowConnectionStore(path, component)
    initial = store.observe()
    selected = WorkflowConnection(component, tmp_path / 'software', '0.1.0-alpha.1',
                                  'a' * 64, 'b' * 64, 'review-1')
    saved_snapshot = store.save(selected, expected=initial)
    saved = store.selected(saved_snapshot)
    assert saved.binding_revision != selected.binding_revision
    assert replace(saved, binding_revision=selected.binding_revision) == selected
    assert saved_snapshot._payload['future'] == {'keep': True}
    assert saved_snapshot._payload[key]['future_local'] == [1, 2]
    before = path.read_bytes()
    with pytest.raises(WorkflowConnectionHold, match='settings_changed'):
        store.save(selected, expected=initial)
    assert path.read_bytes() == before
    other = 'collectigen.train' if workflow == 'generate' else 'collectigen.generate'
    with pytest.raises(WorkflowConnectionHold, match='same_workflow'):
        store.save(replace(selected, component_id=other), expected=saved_snapshot)
    cleared = store.clear(expected=saved_snapshot)
    assert store.selected(cleared) is None
    assert cleared._payload[key] == {'future_local': [1, 2]}
    assert cleared._payload['future'] == {'keep': True}


def test_saved_cross_component_and_malformed_identity_are_not_adopted(tmp_path, workflow):
    component = 'collectigen.' + workflow
    store = WorkflowConnectionStore(tmp_path / 'manager.json', component)
    other = 'collectigen.train' if workflow == 'generate' else 'collectigen.generate'
    row = {'schema_version': 1, 'component_id': other, 'directory': str(tmp_path),
           'version': '0.1.0-alpha.1', 'inventory_sha256': 'a' * 64,
           'manifest_sha256': 'b' * 64, 'binding_revision': 'revision'}
    store.settings.path.write_text(json.dumps({KEYS[component]: row}))
    with pytest.raises(ValueError):
        store.selected(store.observe())
    row['component_id'] = component
    row['inventory_sha256'] = 'invalid'
    store.settings.path.write_text(json.dumps({KEYS[component]: row}))
    with pytest.raises(ValueError):
        store.selected(store.observe())
    with pytest.raises(ValueError):
        WorkflowConnectionStore(tmp_path / 'other.json', 'collectigen.unknown')


def test_stock_stage_publication_and_first_install_recovery_keep_settings_and_native_hold(tmp_path, workflow):
    args, _, message, signature = package(tmp_path, workflow)
    component = ComponentId('collectigen.' + workflow)
    root = tmp_path / 'owned-root'
    root.mkdir()
    data = root / 'Data'
    data.mkdir()
    settings_path = data / 'manager.json'
    settings_path.write_text(json.dumps({'foreign': [1, 2], KEYS[component.value]: {'future': True}}))
    held = []

    @contextmanager
    def hold(selected_component):
        assert selected_component == component and not held
        held.append(True)
        def check():
            assert held == [True]
        try:
            yield ComponentStageBinding(root, component_stage_owner(root, data, component), check, data)
        finally:
            held.pop()

    admission = ComponentStageAdmission(hold)
    store = WorkflowConnectionStore(settings_path, component.value)
    acquisition = SimpleNamespace(maximum=64 * 1024 * 1024, trust=TRUST, _trust=lambda: None)
    service = WorkflowInstallation(component.value, acquisition, store, admission=admission)
    stage_and_activate_admitted(admission, args['output_root'] / args['artifact_name'],
        component=component, version='0.1.0-alpha.1', os_name='windows', architecture='x86_64',
        manifest_bytes=message, signature_bytes=signature, trust_bytes=TRUST,
        expected_active_token=active_token(None))
    published = service.retry_connection()
    assert published.connection_saved and published.installed.phase == 'activated'
    selected = store.selected(store.observe())
    assert selected.component_id == component.value
    assert selected.manifest_sha256 == sha256(message).hexdigest()
    before = settings_path.read_bytes()
    changed = json.loads(before)
    changed[KEYS[component.value]]['manifest_sha256'] = 'c' * 64
    settings_path.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match='signed_identity_changed'):
        service.observe()
    settings_path.write_bytes(before)
    assert not service.launch_available
    with pytest.raises(ValueError, match='native_owner_required'):
        service.launch(confirmed=True)
    with pytest.raises(ValueError, match='native_owner_required'):
        service.confirm_health()
    result = service.recover(confirmed=True)
    assert result.connection_saved and store.selected(store.observe()) is None
    saved = json.loads(settings_path.read_bytes())
    assert saved['foreign'] == [1, 2]
    assert saved[KEYS[component.value]] == {'future': True}
    assert not held
