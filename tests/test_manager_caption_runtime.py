"""Manager caption transport evidence; fake services and synthetic process payloads.

The Windows test exercises contained protocol/lifetime ownership only. It is
not evidence of real caption inference, package admission or model installation.
"""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import time

import pytest

from core import manager_caption_runtime as transport
from core.settings import AppSettings


class FakeOwner:
    def __init__(self, response, *, code=0, on_resume=None):
        self.response = response
        self.code = code
        self.on_resume = on_resume
        self.stdout = BytesIO(b'ignored output\nCG_CAPTION_PROGRESS {"current":1,"total":2,"message":"stock progress"}\n')
        self.returncode = None
        self.members = 1
        self.closed = False
        self.terminated = False
        self.resumed = False
    def spawn_suspended(self, command, cwd, environment):
        self.command, self.cwd, self.environment = command, Path(cwd), environment
        self.request = json.loads(Path(command[command.index('--request') + 1]).read_bytes())
        self.result = Path(command[command.index('--result') + 1])
        assert sha256(Path(command[command.index('--request') + 1]).read_bytes()).hexdigest() == command[command.index('--request-sha256') + 1]
    def resume(self):
        self.resumed = True
        if self.on_resume:
            self.on_resume(self)
        if self.response is not None:
            self.result.write_bytes(self.response if isinstance(self.response, bytes) else json.dumps(self.response).encode())
        self.returncode, self.members = self.code, 0
    def poll(self):
        return self.returncode
    def active(self):
        return self.members
    def terminate(self):
        self.terminated = True
        self.returncode, self.members = 1, 0
    def close(self):
        assert self.poll() is not None and self.active() == 0
        self.closed = True


def reply(value):
    return {'schema_version': 1, 'ok': True, 'value': value}


@pytest.fixture
def setup_runtime(tmp_path):
    source, worker, profile = (tmp_path / name for name in ('source', 'worker', 'Descriptions'))
    (source / 'core').mkdir(parents=True)
    worker.mkdir()
    (source / 'core/manager_caption_worker.py').write_text('# synthetic transport source\n')
    executable = worker / 'python.exe'
    executable.write_bytes(b'synthetic interpreter')
    binding = transport.CaptionWorkerBinding(source, worker, executable,
        sha256(executable.read_bytes()).hexdigest(), '3.13.15', profile)
    settings = AppSettings(ai_models_directory=str(tmp_path / 'models'))
    owners, checks = [], []
    def build(response=None, *, owner=None):
        def factory(name):
            result = owner or FakeOwner(reply({'ready': False, 'components': [], 'runtime_issue': ''}) if response is None else response)
            owners.append(result)
            return result
        def check(cancel):
            cancel()
            checks.append('binding checked')
        return transport.ManagerCaptionRuntime(binding, check, native_factory=factory)
    return build, settings, binding, owners, checks


def test_status_finite_request_review_and_success_cleanup(setup_runtime):
    build, settings, binding, owners, checks = setup_runtime
    runtime = build()
    value = runtime.status(settings)
    assert value['ready'] is False and len(value['review_token']) == 64
    owner = owners[0]
    assert owner.request['operation'] == 'status'
    assert set(owner.request['settings']) == set(transport.FIELDS)
    assert owner.command[0] == str(binding.interpreter)
    assert owner.command[1:4] == ('-I', '-B', '-u')
    assert owner.closed and not runtime.close_held and len(checks) == 3
    assert not owner.cwd.exists(), 'Successful transient settings and results are removed.'


@pytest.mark.parametrize('change', ['settings', 'inventory'])
def test_review_token_rejects_changed_settings_or_component_inventory_before_launch(setup_runtime, change):
    build, settings, binding, owners, _ = setup_runtime
    runtime = build()
    token = runtime.status(settings)['review_token']
    if change == 'settings':
        settings.joycaption_temperature += .1
    else:
        inventory = binding.profile_root / 'data/optional-components/installed-components.json'
        inventory.parent.mkdir(parents=True)
        inventory.write_text('{"synthetic_revision":2}')
    with pytest.raises(RuntimeError, match='review it again'):
        runtime.install_runtime(settings, token)
    assert len(owners) == 1 and not runtime.close_held


def test_reviewed_setup_only_dispatches_explicit_requested_operation(setup_runtime):
    build, settings, _, owners, _ = setup_runtime
    runtime = build()
    token = runtime.status(settings)['review_token']
    runtime.download_model(settings, token)
    assert [owner.request['operation'] for owner in owners] == ['status', 'download-model']
    with pytest.raises(ValueError, match='Unsupported'):
        runtime._request('remove', settings)
    assert len(owners) == 2


def test_concurrent_request_is_refused_while_original_owns_transport(setup_runtime):
    build, settings, _, owners, _ = setup_runtime
    nested = []
    runtime = None
    def on_resume(owner):
        assert runtime.close_held
        with pytest.raises(RuntimeError, match='current caption operation') as error:
            runtime.status(settings)
        nested.append(error.value)
    runtime = build(owner=FakeOwner(reply({'ready': False, 'components': [], 'runtime_issue': ''}), on_resume=on_resume))
    runtime.status(settings)
    assert len(nested) == 1 and len(owners) == 1 and owners[0].closed
    assert not runtime.close_held


@pytest.mark.parametrize('response,code,error', [
    (reply({'ready': False, 'components': [], 'runtime_issue': ''}), 7, RuntimeError),
    ({'schema_version': 2, 'ok': True, 'value': {}}, 0, ValueError),
    ({'schema_version': 1, 'ok': 'yes', 'value': {}}, 0, ValueError),
    ({'schema_version': 1, 'ok': False, 'error': 'Stock recovery remains pending.'}, 1, RuntimeError),
    ({'schema_version': 1, 'ok': False, 'cancelled': True}, 1, InterruptedError),
    (b'x' * (transport.MAX_RESULT + 1), 0, (ValueError, OSError)),
], ids=['nonzero-exit','wrong-schema','invalid-ok','stock-error','cancelled','oversized-result'])
def test_error_and_bounded_result_refusal_settle_owner_and_keep_recovery_evidence(setup_runtime, response, code, error):
    build, settings, _, owners, _ = setup_runtime
    runtime = build(owner=FakeOwner(response, code=code))
    with pytest.raises(error):
        runtime.status(settings)
    assert owners[0].closed and not runtime.close_held
    assert (owners[0].cwd / 'request.json').exists()


def test_binding_failure_and_prelaunch_cancel_do_not_create_process(setup_runtime):
    build, settings, binding, owners, _ = setup_runtime
    runtime = build()
    with pytest.raises(InterruptedError):
        runtime.status(settings, cancelled=lambda: True)
    assert owners == [] and not runtime.close_held and not binding.profile_root.exists()
    def stale(check):
        raise RuntimeError('admitted worker changed')
    runtime._check_binding = stale
    with pytest.raises(RuntimeError, match='worker changed'):
        runtime.status(settings)
    assert owners == [] and not runtime.close_held


def test_cancelled_descendant_keeps_close_and_new_requests_held_until_terminal(setup_runtime, monkeypatch):
    build, settings, _, owners, _ = setup_runtime
    class HeldOwner(FakeOwner):
        def resume(self):
            self.resumed = True
            self.returncode, self.members = 0, 1  # root exited, descendant still owned
        def terminate(self):
            self.terminated = True  # synthetic native observer has not confirmed exit
    owner = HeldOwner(None)
    runtime = build(owner=owner)
    counter = iter(range(10000))
    monkeypatch.setattr(transport.time, 'monotonic', lambda: next(counter))
    monkeypatch.setattr(transport.time, 'sleep', lambda seconds: None)
    with pytest.raises(InterruptedError):
        runtime.status(settings, cancelled=lambda: owner.resumed)
    assert owner.terminated and runtime.close_held and not owner.closed
    assert (owner.cwd / 'cancel.flag').exists()
    with pytest.raises(RuntimeError, match='current caption operation'):
        runtime.status(settings)
    owner.members = 0
    assert not runtime.close_held and owner.closed
    assert runtime._lock.acquire(blocking=False)
    runtime._lock.release()


def test_suggestion_results_retain_labels_and_refuse_out_of_scope_payloads(setup_runtime):
    build, settings, _, _, _ = setup_runtime
    proposal = {'kind': 'character', 'text': 'visible coat',
                'source_label': 'Local AI suggestion (uncertain)', 'model_id': 'reviewed-model'}
    runtime = build(reply({'proposals': [proposal], 'notes': ['Review before use.']}))
    result = runtime.suggest('synthetic.png', settings, selected_kinds=['character'])
    assert result.proposals[0].source_label == proposal['source_label']
    assert result.notes == ('Review before use.',)
    bad = deepcopy(proposal)
    bad['text'] = 'x' * 4097
    runtime = build(reply({'proposals': [bad], 'notes': []}))
    with pytest.raises(ValueError, match='invalid proposal'):
        runtime.suggest('synthetic.png', settings, selected_kinds=['character'])
    runtime = build(reply({'proposals': [], 'notes': [], 'saved_knowledge': True}))
    with pytest.raises(ValueError, match='invalid suggestion'):
        runtime.suggest('synthetic.png', settings, selected_kinds=['character'])


@pytest.mark.skipif(os.name != 'nt', reason='Windows Job containment protocol/process evidence')
def test_real_contained_synthetic_child_cancellation_waits_for_descendant_terminal(tmp_path):
    """Synthetic transport/process evidence only; no caption libs or model work."""
    import ctypes
    from ctypes import wintypes
    from core.training_process_windows import WindowsTrainingJob

    source, profile = tmp_path / 'source', tmp_path / 'Descriptions'
    (source / 'core').mkdir(parents=True)
    script = source / 'core/manager_caption_worker.py'
    script.write_text('''import argparse, hashlib, json, pathlib, subprocess, sys, time
p = argparse.ArgumentParser()
p.add_argument('--request'); p.add_argument('--request-sha256'); p.add_argument('--result')
a = p.parse_args()
request = pathlib.Path(a.request)
raw = request.read_bytes()
assert len(raw) <= 65536 and hashlib.sha256(raw).hexdigest() == a.request_sha256
payload = json.loads(raw)
if payload['operation'] == 'status':
    result = {'schema_version': 1, 'ok': True, 'value': {'ready': False, 'components': [], 'runtime_issue': ''}}
    encoded = json.dumps(result).encode()
    assert len(encoded) <= 131072
    pathlib.Path(a.result).write_bytes(encoded)
else:
    child = subprocess.Popen([sys.executable, '-I', '-B', '-c', 'import time; time.sleep(90)'])
    (request.parent / 'synthetic-descendant.json').write_text(json.dumps({'pid': child.pid}))
    time.sleep(90)
''', encoding='utf-8')
    executable = Path(sys.executable).absolute()
    binding = transport.CaptionWorkerBinding(source, executable.parent, executable,
        sha256(executable.read_bytes()).hexdigest(), sys.version.split()[0], profile)
    settings = AppSettings(ai_models_directory=str(tmp_path / 'models'))
    owners, retained, close_observations = [], [], []

    class CapturedOwner(WindowsTrainingJob):
        def close(self):
            if getattr(self, 'process', None) is not None:
                # Observe terminal state while owner handles are still retained.
                close_observations.append((self.poll(), self.active(),
                    tuple(self.api.WaitForSingleObject(handle, 0) for handle in retained)))
            super().close()

    def factory(name):
        owner = CapturedOwner(name)
        owners.append(owner)
        return owner

    # Explicit synthetic callback replaces production admission for this test only.
    runtime = transport.ManagerCaptionRuntime(binding, lambda check: check(), native_factory=factory)
    started = time.monotonic()
    def cancel_after_own_descendant_is_observed():
        if time.monotonic() - started > 15:
            raise AssertionError('Synthetic descendant did not become observable within 15 seconds.')
        if len(owners) < 2:
            return False
        owner = owners[-1]
        if not owner.process:
            return False
        files = list((profile / 'operations').glob('*/synthetic-descendant.json'))
        if not files:
            return False
        try:
            pid = json.loads(files[0].read_text())['pid']
        except (OSError, ValueError):
            return False
        members = owner.process_ids()  # bounded own-Job membership, no global enumeration
        assert pid in members and owner.pid in members and pid != owner.pid
        if not retained:
            for owned_pid in (owner.pid, pid):
                handle = owner.api.OpenProcess(0x100000 | 0x1000, False, owned_pid)
                assert handle
                retained.append(handle)
                member = wintypes.BOOL()
                assert owner.api.IsProcessInJob(handle, owner.handle, ctypes.byref(member))
                assert member.value and owner.api.WaitForSingleObject(handle, 0) == 258
        return True

    try:
        assert runtime.status(settings)['ready'] is False
        with pytest.raises(InterruptedError):
            runtime.suggest('synthetic-only.png', settings, selected_kinds=['character'],
                            cancelled=cancel_after_own_descendant_is_observed)
        assert len(retained) == 2 and not runtime.close_held
        assert close_observations[-1][0] is not None
        assert close_observations[-1][1:] == (0, (0, 0))
        assert all(owners[-1].api.WaitForSingleObject(handle, 0) == 0 for handle in retained)
    finally:
        # Limit recovery to the exact owners and handles created by this test.
        for owner in owners:
            if owner.handle:
                owner.terminate()
                WindowsTrainingJob.close(owner)
        if owners:
            for handle in retained:
                owners[-1].api.CloseHandle(handle)

@pytest.mark.parametrize('program_name', ['source-files', 'worker-files'])
def test_component_only_profile_bootstrap_is_isolated_and_preserves_default_contract(tmp_path, program_name):
    """A fresh interpreter binds a component profile without touching host globals."""
    import subprocess
    program = tmp_path / program_name
    program.mkdir()
    profile = tmp_path / 'component-profile'
    source = Path(transport.__file__).absolute().parents[1]
    code = r'''
import json, pathlib, sys
sys.path.insert(0, sys.argv[1])
from core.application_profile import bind_profile_root, selected_profile_root
program, profile = pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
assert 'core.paths' not in sys.modules
assert bind_profile_root(profile, program_root=program, initialize_defaults=False) == profile
assert selected_profile_root() == profile
assert sorted(p.name for p in profile.iterdir()) == ['data']
assert list((profile / 'data').iterdir()) == []
assert 'core.paths' not in sys.modules
print(json.dumps({'component_only': True}))
'''
    completed = subprocess.run([sys.executable, '-I', '-B', '-c', code,
                               str(source), str(program), str(profile)],
                              capture_output=True, text=True, timeout=15, check=True)
    assert json.loads(completed.stdout) == {'component_only': True}
    assert not (profile / 'data/defaults').exists()

    # Default True still requires catalog-default authority; each check gets a
    # fresh process, so no reset of application/profile globals is necessary.
    rejection = r'''
import pathlib, sys
sys.path.insert(0, sys.argv[1])
from core.application_profile import bind_profile_root
program, root, mode = pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]), sys.argv[4]
try:
    if mode == 'component':
        bind_profile_root(root, program_root=program, initialize_defaults=False)
    else:
        bind_profile_root(root, program_root=program)
except (RuntimeError, ValueError) as error:
    print(type(error).__name__)
else:
    raise AssertionError('Unsafe profile binding unexpectedly succeeded')
'''
    for root, mode, expected in [(program, 'component', 'ValueError'),
                                  (program / 'nested-profile', 'component', 'ValueError'),
                                  (tmp_path / 'catalog-profile', 'catalog', 'RuntimeError')]:
        completed = subprocess.run([sys.executable, '-I', '-B', '-c', rejection,
                                   str(source), str(program), str(root), mode],
                                  capture_output=True, text=True, timeout=15, check=True)
        assert completed.stdout.strip() == expected

@pytest.mark.parametrize('readiness', [
    {'ready': 'yes', 'components': [], 'runtime_issue': ''},
    {'ready': False},
    {'ready': False, 'components': {}, 'runtime_issue': ''},
    {'ready': False, 'components': [{'name': 'PyTorch', 'installed': 'yes'}], 'runtime_issue': ''},
    {'ready': False, 'components': [{'name': 7, 'installed': False}], 'runtime_issue': ''},
    {'ready': False, 'components': [], 'runtime_issue': 'x' * 4097},
])
def test_malformed_readiness_never_receives_setup_review_token(setup_runtime, readiness):
    build, settings, _, owners, _ = setup_runtime
    runtime = build(reply(readiness))
    with pytest.raises(ValueError, match='invalid readiness'):
        runtime.status(settings)
    assert owners[0].closed and not runtime.close_held


@pytest.mark.parametrize("change", ["none", "source", "non_core_source", "session", "worker"])
def test_normal_entry_caption_binding_retains_signed_source_and_worker(tmp_path, monkeypatch, change):
    """Binding component evidence; no native child or real admission is executed."""
    from types import SimpleNamespace as NS
    from hashlib import sha256
    import json
    import core.paths
    from suite_core.manager.entry_binding import _bind_manager_caption_runtime
    import core.training_file_inventory
    import suite_core.discovery.manager_runtime_detached_admission as admission
    code = tmp_path / "Suite/Apps/Manager/current"
    source = code / "_internal"; worker = code / "training-worker/python"
    worker.mkdir(parents=True); (worker / "python.exe").write_bytes(b"synthetic interpreter")
    inventory = NS(root=worker, manifest_path=tmp_path/"worker-inventory.json",
                   manifest_sha256="a"*64, file_count=1, byte_count=21)
    names = ["core/manager_caption_worker.py", "core/generation_image_suggestions.py",
             "tools/runtime-pins.json", "tools/optional-ai-profiles.json", "release_contract.py"]
    rows=[]
    for name in names:
        path=source/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b"synthetic source")
        rows.append({"path":"_internal/"+name,"sha256":sha256(path.read_bytes()).hexdigest()})
    proof=json.dumps({"files":rows}).encode()
    plan=NS(inventory_sha256=sha256(proof).hexdigest(),
            training_worker_file_rows=lambda _proof:{"python.exe":{"sha256":sha256((worker/"python.exe").read_bytes()).hexdigest()}})
    admitted=NS(layout=NS(code_root=tmp_path/"Suite",data_root=tmp_path/"Data"))
    session=NS(state="ready",admitted=admitted)
    monkeypatch.setattr(core.paths,"PROGRAM_ROOT",source)
    monkeypatch.setattr(admission,"manager_inventory_json",lambda _proof:{"files":rows})
    def verify(_self,root,manifest,should_cancel):
        should_cancel()
        return {"inventory_sha256":("b"*64 if change=="worker" else inventory.manifest_sha256),
                "files":inventory.file_count,"bytes":inventory.byte_count}
    monkeypatch.setattr(core.training_file_inventory.SoftwareInventoryService,"verify",verify)
    runtime=_bind_manager_caption_runtime(session,NS(inventory=inventory,python_relative="python.exe"),(plan,proof))
    assert runtime.binding.profile_root==tmp_path/"Data/Manager/Descriptions"
    assert not runtime.binding.profile_root.exists()
    if change=="source":(source/names[0]).write_bytes(b"changed source")
    if change=="non_core_source":(source/"release_contract.py").write_bytes(b"changed source")
    if change=="session":session.state="ended"
    if change=="none":runtime._check_binding(lambda:None)
    else:
        with pytest.raises((RuntimeError,InterruptedError)):runtime._check_binding(lambda:None)
