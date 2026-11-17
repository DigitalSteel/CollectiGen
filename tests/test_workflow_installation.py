"""Synthetic signed software, canonical settings and original recovery; no native execution."""
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
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


@pytest.fixture
def retained_workflow_case(tmp_path, workflow):
    """Generated inert signed software and original synthetic owner; never launch."""
    args, _, message, signature = package(tmp_path, workflow)
    component = ComponentId('collectigen.' + workflow)
    root = tmp_path / 'retained-root'; root.mkdir()
    data = root / 'Data'; data.mkdir()
    settings_path = data / 'manager.json'
    settings_path.write_text(json.dumps({'foreign': [1, 2], KEYS[component.value]: {'future': True}}))
    held = []
    @contextmanager
    def hold(selected_component):
        assert selected_component == component and not held
        held.append(True)
        def check(): assert held == [True]
        try: yield ComponentStageBinding(root, component_stage_owner(root, data, component), check, data)
        finally: held.pop()
    admission = ComponentStageAdmission(hold)
    store = WorkflowConnectionStore(settings_path, component.value)
    acquisition = SimpleNamespace(maximum=64*1024*1024, trust=TRUST, _trust=lambda:None)
    service = WorkflowInstallation(component.value, acquisition, store, admission=admission)
    kwargs=dict(component=component, version='0.1.0-alpha.1', os_name='windows', architecture='x86_64',
        manifest_bytes=message, signature_bytes=signature, trust_bytes=TRUST,
        expected_active_token=active_token(None), payload_policy=service.payload_policy)
    artifact=args['output_root']/args['artifact_name']
    service._stage_installation(artifact, **kwargs)
    service.retry_connection()
    service.recover(confirmed=True)
    with admission.transaction(component) as (_, base, _, _): pass
    destination=root/'Apps'/component.value/'0.1.0-alpha.1'
    def snapshot():
        return {f.relative_to(destination).as_posix():(f.read_bytes(), f.stat().st_mtime_ns)
            for f in destination.rglob('*') if f.is_file()}
    return SimpleNamespace(service=service,store=store,admission=admission,artifact=artifact,kwargs=kwargs,
        base=base,root=root,destination=destination,settings_path=settings_path,held=held,
        snapshot=snapshot,before=snapshot(),journal=(base/'transaction.json').read_bytes())


def test_exact_rolled_back_workflow_reactivation_keeps_software_and_original_health(retained_workflow_case, monkeypatch):
    from suite_core.discovery import component_staging as staging
    case=retained_workflow_case
    def no_extract(*args, **kwargs): raise AssertionError('Retained software must not be extracted again')
    monkeypatch.setattr(staging,'extract_component_archive',no_extract)
    replace=staging.os.replace
    def no_replace_version(source,destination):
        assert Path(destination)!=case.destination, 'Immutable version must never be replaced'
        return replace(source,destination)
    monkeypatch.setattr(staging.os,'replace',no_replace_version)
    result=case.service._stage_installation(case.artifact,**case.kwargs)
    assert result['directory']=='Apps/'+case.kwargs['component'].value+'/0.1.0-alpha.1'
    published=case.service.retry_connection()
    assert published.connection_saved and published.installed.phase=='activated'
    assert not case.service.launch_available
    with pytest.raises(ValueError,match='native_owner_required'):case.service.confirm_health()
    assert case.snapshot()==case.before and not case.held
    assert json.loads(case.settings_path.read_bytes())['foreign']==[1,2]
    journal=json.loads((case.base/'transaction.json').read_bytes())
    assert journal['phase']=='activated' and journal['prior_journal']
    assert not (case.base/journal['stage']).exists()
    case.service.recover(confirmed=True)
    assert case.store.selected(case.store.observe()) is None
    assert (case.base/'transaction.json').read_bytes()==case.journal
    assert case.snapshot()==case.before


@pytest.mark.parametrize('outcome', ['healthy', 'held', 'changed-saved'])
def test_health_confirmation_uses_original_kernel_preflight_and_signed_identity(retained_workflow_case, outcome):
    from core.workflow_native_owner import WorkflowWorkspaceOwner
    case = retained_workflow_case
    case.service._stage_installation(case.artifact, **case.kwargs)
    case.service.retry_connection()
    calls = []
    native = WorkflowWorkspaceOwner.__new__(WorkflowWorkspaceOwner)
    def health(selected):
        calls.append(selected)
        assert case.held == [True]  # Signed native selection retains its original read scope.
        if outcome == 'changed-saved':
            raw=json.loads(case.settings_path.read_bytes())
            raw[KEYS[selected.component_id]]['manifest_sha256']='c'*64
            case.settings_path.write_text(json.dumps(raw))
        return outcome != 'held'
    native.native_health=health
    case.service.native_owner=native
    case.service.native_health=case.service._native_health
    before=(case.base/'transaction.json').read_bytes()
    if outcome == 'healthy':
        installed=case.service.confirm_health()
        assert installed.phase == 'healthy'
        assert case.service.reconcile_launch().installed.phase == 'healthy'
    else:
        with pytest.raises(ValueError, match='native_reader_health_held|signed_identity_changed'):
            case.service.confirm_health()
        assert (case.base/'transaction.json').read_bytes() == before
    assert len(calls) == 1 and case.snapshot() == case.before and not case.held
    assert json.loads(case.settings_path.read_bytes())['foreign'] == [1,2]
    with pytest.raises(ValueError, match='original_component_scope_required'):
        case.service.signed_selection(case.service.connection_type(case.destination, calls[0].inventory_sha256, calls[0].version))


def test_default_stage_still_refuses_retained_workflow_directory(retained_workflow_case):
    case=retained_workflow_case
    with pytest.raises(ValueError,match='immutable_version_already_exists'):
        stage_and_activate_admitted(case.admission,case.artifact,**case.kwargs)
    assert (case.base/'transaction.json').read_bytes()==case.journal
    assert case.snapshot()==case.before


@pytest.mark.parametrize('change',['missing','extra','changed','manifest','signature','abandoned','pending','stale_active'])
def test_retained_workflow_reactivation_refuses_changed_or_unowned_evidence(retained_workflow_case,change):
    case=retained_workflow_case;kwargs=dict(case.kwargs)
    if change=='missing':(case.destination/'RUNTIME_MANIFEST.json').unlink()
    elif change=='extra':(case.destination/'unlisted-synthetic.txt').write_bytes(b'not admitted')
    elif change=='changed':(case.destination/(case.kwargs['component'].value.split('.')[-1].title()+'.exe')).write_bytes(b'changed inert bytes')
    elif change=='manifest':
        value=json.loads(kwargs['manifest_bytes']);value['release_notes']='Different signed metadata for a retained version'
        kwargs['manifest_bytes']=json.dumps(value).encode();kwargs['signature_bytes']=sign(kwargs['manifest_bytes'])
    elif change=='signature':kwargs['signature_bytes']=b'{}'
    elif change=='abandoned':case.service.recover(confirmed=True)
    elif change=='pending':(case.base/'pending-transaction.json').write_bytes(b'{}')
    elif change=='stale_active':kwargs['expected_active_token']='0'*64
    journal=(case.base/'transaction.json').read_bytes()
    with pytest.raises(ValueError):case.service._stage_installation(case.artifact,**kwargs)
    assert (case.base/'transaction.json').read_bytes()==journal
    assert not (case.base/'active.json').exists() and not case.held


@pytest.mark.parametrize('phase',[1,2])
def test_retained_verification_cancel_preserves_original_rollback(retained_workflow_case,monkeypatch,phase):
    from suite_core.discovery import component_staging as staging
    case=retained_workflow_case;original=staging.verify_directory;seen=[0]
    class Cancelled(RuntimeError):pass
    def verify(directory,*args,**kwargs):
        if Path(directory)==case.destination:seen[0]+=1
        return original(directory,*args,**kwargs)
    def cancel():
        if seen[0]==phase:raise Cancelled('Generated verification cancellation')
    monkeypatch.setattr(staging,'verify_directory',verify)
    with pytest.raises(Cancelled):case.service._stage_installation(case.artifact,**case.kwargs,copy_checkpoint=cancel)
    assert not (case.base/'active.json').exists()
    if (case.base/'pending-transaction.json').exists():case.service.recover(confirmed=True)
    assert (case.base/'transaction.json').read_bytes()==case.journal
    assert case.snapshot()==case.before and not case.held


@pytest.mark.parametrize('phase',['prior_journal_preserved','new_journal_established','version_published','active_reference_replaced'])
def test_retained_reactivation_failure_uses_original_scoped_recovery(retained_workflow_case,phase):
    case=retained_workflow_case
    class Interrupted(RuntimeError):pass
    def failpoint(point):
        if point==phase:raise Interrupted('Generated transaction failure')
    with pytest.raises(Interrupted):case.service._stage_installation(case.artifact,**case.kwargs,failpoint=failpoint)
    case.service.recover(confirmed=True)
    assert case.store.selected(case.store.observe()) is None
    assert (case.base/'transaction.json').read_bytes()==case.journal
    assert case.snapshot()==case.before and not case.held
    saved=json.loads(case.settings_path.read_bytes())
    assert saved['foreign']==[1,2] and saved[KEYS[case.kwargs['component'].value]]=={'future':True}


def test_retained_reactivation_rejects_changed_journal_before_handoff(retained_workflow_case):
    case=retained_workflow_case
    def drift(point):
        if point=='before_version_publish':
            (case.base/'transaction.json').write_bytes(case.journal+b'\n')
    with pytest.raises(ValueError,match='current_journal_changed'):
        case.service._stage_installation(case.artifact,**case.kwargs,failpoint=drift)
    assert not (case.base/'active.json').exists() and case.snapshot()==case.before


@pytest.mark.parametrize('policy',['wrong_component','wrong_payload','not_bool'])
def test_retained_opt_in_is_finite_and_never_expands_shared_component_behavior(retained_workflow_case,policy):
    from suite_core.discovery.component_payload_policy import FROZEN_PAYLOAD_POLICY
    case=retained_workflow_case;kwargs=dict(case.kwargs);flag=True
    if policy=='wrong_component':
        from core.encyclopedia_connection import COMPONENT
        kwargs['component']=COMPONENT
    elif policy=='wrong_payload':kwargs['payload_policy']=FROZEN_PAYLOAD_POLICY
    else:flag=1
    with pytest.raises(ValueError,match='retained_workflow_policy_required'):
        stage_and_activate_admitted(case.admission,case.artifact,**kwargs,reuse_rolled_back=flag)
    assert not case.held and (case.base/'transaction.json').read_bytes()==case.journal
