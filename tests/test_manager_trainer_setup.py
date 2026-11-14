"""Synthetic Manager trainer lifecycle boundaries; no native software execution."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import manager_trainer_setup as host_module
from core.manager_trainer_setup import ManagerTrainerSetup
from core.training_setup_selection import TrainingSetupSelectionError
from qt_app import training_setup_queue_controller as controller_module
from qt_app.training_setup_queue_controller import ManagerTrainerSetupController, QueuedTrainerSetup


ACTIONS = ('managed', 'existing', 'import', 'export', 'repair', 'activate', 'rollback')
GENERATION = 'a' * 32


class FakeQueue:
    def __init__(self, settings, jobs, events, path, *, trainer_executor, rows=(), load_error=''):
        self.injected = (settings, jobs, events, path, trainer_executor)
        self.rows = list(rows)
        self.load_error = load_error
        self.enqueued = []
        self.cancelled = []
        self.reject_cancel = False

    def snapshot(self):
        return tuple(self.rows)

    def enqueue_confirmed(self, kind, label, params):
        row = operation(str(len(self.enqueued)), 'waiting', params)
        self.enqueued.append((kind, label, deepcopy(params)))
        self.rows.append(row)
        return SimpleNamespace(operation=row, message='Confirmed synthetic operation')

    def cancel_operation(self, identity):
        self.cancelled.append(identity)
        if self.reject_cancel:
            return False, 'Original operation could not be cancelled'
        row = next(row for row in self.rows if row.id == identity)
        row.state = 'cancelled'
        return True, 'Cancelled'


def operation(identity, state, params=None, *, kind='setup_trainer_runtime'):
    return SimpleNamespace(id=identity, state=state, kind=kind,
        params=deepcopy(params or {}), detail='Retained synthetic operation')


class FakeExecutor:
    def __init__(self, root):
        self.root = root
        self.managed_root = root / 'managed'
        self.catalog_path = root / 'synthetic-catalog.json'
        self.process = SimpleNamespace(has_retained_owners=False)
        self.profile_value = SimpleNamespace(sha256='b' * 64, byte_count=1024)
        self.calls = []
        self.result = SimpleNamespace(software_root=root / 'managed' / 'generations' / GENERATION / 'software')

    def profile(self):
        return self.profile_value

    def prepare(self, action, selection, *, should_cancel, progress):
        assert not should_cancel()
        self.calls.append(('prepare', action, deepcopy(selection)))
        reference = {'action': action, 'selection': deepcopy(selection), 'review_sha256': 'c' * 64}
        value, _ = self._review(reference, should_cancel)
        return reference, value

    def _review(self, reference, should_cancel):
        if should_cancel():
            raise InterruptedError('Synthetic review cancelled')
        self.calls.append(('review', deepcopy(reference)))
        selection = reference['selection']
        if reference['action'] == 'managed' and selection is None:
            selection = {'download_bytes': 1234, 'synthetic': True}
        return {'action': reference['action'], 'selection': deepcopy(selection),
            'root': str(self.managed_root), 'storage_budget_bytes': 4096}, 'revision'

    def __call__(self, operation, *, should_cancel, progress):
        self.calls.append(('execute', operation, should_cancel, progress))
        return 'executed'

    def completed_result(self, reference, *, should_cancel):
        if should_cancel():
            raise InterruptedError('Synthetic completion cancelled')
        self.calls.append(('completed', reference))
        return self.result

    def save_resources(self, *args, should_cancel):
        assert not should_cancel()
        self.calls.append(('save', args))
        return 'saved'


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(controller_module, 'profile_overview', lambda profile: SimpleNamespace(details='Synthetic software review'))
    root = tmp_path / 'manager-training'
    executor = FakeExecutor(root)
    session = {'allowed': True}
    protected = [tmp_path / 'protected-library']
    jobs, settings = object(), object()
    options = {'rows': (), 'load_error': ''}
    created = []

    def queue_factory(*args, **kwargs):
        queue = FakeQueue(*args, **kwargs, **options)
        created.append(queue)
        return queue

    def make():
        return ManagerTrainerSetup(jobs, root, settings,
            profile_path=tmp_path / 'profile.json', catalog_path=executor.catalog_path,
            source_root=tmp_path / 'software-source', session_allowed=lambda: session['allowed'],
            protected_roots=lambda: tuple(protected), executor=executor, queue_factory=queue_factory)

    owner = make()
    return SimpleNamespace(owner=owner, executor=executor, session=session, protected=protected,
        jobs=jobs, settings=settings, options=options, created=created, make=make, tmp=tmp_path)


def selection(h, action):
    if action == 'managed':
        return None
    if action in {'activate', 'rollback'}:
        return GENERATION
    if action == 'export':
        return (GENERATION, str(h.tmp / 'export.zip'))
    return str(h.tmp / ('source.zip' if action == 'import' else 'source-software'))


def controller(h, *, managed=True):
    return ManagerTrainerSetupController(h.owner, h.executor.profile(), managed_available=managed)


@pytest.mark.parametrize('action', ACTIONS)
def test_all_original_actions_review_then_enqueue_without_execution(harness, action):
    h = harness
    bridge = controller(h)
    assert bridge.supports(action)
    preview = bridge.prepare(action, selection(h, action), should_cancel=lambda: False, progress=None)
    assert h.owner.queue.enqueued == []
    assert not any(call[0] == 'execute' for call in h.executor.calls)
    pending = bridge.apply(preview, should_cancel=lambda: False, progress=None)
    assert isinstance(pending, QueuedTrainerSetup)
    assert pending.action == action
    assert h.owner.queue.enqueued == [('setup_trainer_runtime', preview.confirmation_label, preview.payload)]
    assert not any(call[0] == 'execute' for call in h.executor.calls)


def test_unavailable_catalog_gates_only_managed_action(harness):
    bridge = controller(harness, managed=False)
    assert not bridge.supports('managed')
    assert all(bridge.supports(action) for action in ACTIONS if action != 'managed')
    with pytest.raises(ValueError, match='qualified Setup profile'):
        bridge.prepare('managed', None, should_cancel=lambda: False, progress=None)
    assert harness.owner.queue.enqueued == []


@pytest.mark.parametrize('valid_catalog', (True, False))
def test_describe_reads_only_synthetic_catalog_and_retained_generation_names(harness, monkeypatch, valid_catalog):
    h = harness
    generations = h.executor.managed_root / 'generations'
    (generations / GENERATION).mkdir(parents=True)
    monkeypatch.setattr(host_module, '_read_json', lambda path: {'synthetic': True})

    def catalog(value):
        assert value == {'synthetic': True}
        if not valid_catalog:
            raise ValueError('Synthetic catalog not qualified')
        return object()

    monkeypatch.setattr(host_module, 'RuntimeSoftwareCatalog', SimpleNamespace(from_dict=catalog))
    profile, managed, choices, issue = h.owner.describe()
    assert profile is h.executor.profile_value and managed is valid_catalog
    assert choices == ((GENERATION, 'Generation ' + GENERATION),)
    assert (not issue) if valid_catalog else ('Synthetic catalog not qualified' in issue)
    assert h.owner.queue.enqueued == [] and h.executor.calls == []


def test_host_injects_one_retained_queue_without_dispatch(harness):
    h = harness
    settings, jobs, events, path, execute = h.owner.queue.injected
    assert settings is h.settings and jobs is h.jobs and events is h.owner.events
    assert path == h.owner.root / 'setup-queue.json'
    assert execute.__self__ is h.owner
    assert h.executor.calls == []
    assert h.owner.queue.enqueued == []


@pytest.mark.parametrize('fault', ('foreign', 'load_error'))
def test_restored_queue_refused_without_mutating_rows(harness, fault):
    h = harness
    row = operation('foreign', 'waiting', kind='setup_application')
    h.options.update(rows=(row,) if fault == 'foreign' else (),
                     load_error='Unreadable synthetic queue' if fault == 'load_error' else '')
    with pytest.raises(ValueError, match='another capability|Unreadable synthetic queue'):
        h.make()
    assert h.created[-1].enqueued == [] and h.created[-1].cancelled == []
    assert row.state == 'waiting'


@pytest.mark.parametrize('action', ('existing', 'import', 'repair', 'export'))
def test_protected_original_selection_refused_before_review(harness, action):
    h = harness
    blocked = str(h.protected[0] / 'selected-software')
    value = (GENERATION, blocked) if action == 'export' else blocked
    with pytest.raises(TrainingSetupSelectionError, match='protected'):
        controller(h).prepare(action, value, should_cancel=lambda: False, progress=None)
    assert h.executor.calls == [] and h.owner.queue.enqueued == []


@pytest.mark.parametrize('boundary', ('prepare', 'apply', 'completed', 'execute', 'save'))
def test_original_session_loss_prevents_each_mutating_boundary(harness, boundary):
    h = harness
    bridge = controller(h)
    preview = bridge.prepare('existing', selection(h, 'existing'), should_cancel=lambda: False, progress=None)
    h.executor.calls.clear()
    h.session['allowed'] = False
    with pytest.raises(InterruptedError, match='session ended'):
        if boundary == 'prepare':
            bridge.prepare('existing', selection(h, 'existing'), should_cancel=lambda: False, progress=None)
        elif boundary == 'apply':
            bridge.apply(preview, should_cancel=lambda: False, progress=None)
        elif boundary == 'completed':
            bridge.completed(SimpleNamespace(reference=preview.payload), should_cancel=lambda: False, progress=None)
        elif boundary == 'execute':
            h.owner.execute(operation('row', 'running', preview.payload), should_cancel=lambda: False, progress=None)
        else:
            h.owner.save_resources(preview.payload, h.tmp / 'settings.json', h.tmp / 'tokens',
                h.tmp / 'output', 'revision', should_cancel=lambda: False)
    assert h.executor.calls == [] and h.owner.queue.enqueued == []


def test_execute_retains_exact_operation_params_and_live_cancellation(harness):
    h = harness
    params = {'action': 'import', 'selection': str(h.tmp / 'pack.zip'), 'review_sha256': 'd' * 64,
              'unknown_forward_field': 'preserve'}
    row = operation('selected', 'running', params)
    cancelled = {'value': False}
    progress = object()
    assert h.owner.execute(row, should_cancel=lambda: cancelled['value'], progress=progress) == 'executed'
    _, received, cancel, received_progress = h.executor.calls[-1]
    assert received is row and received.params == params and received_progress is progress
    assert not cancel()
    cancelled['value'] = True
    assert cancel()
    cancelled['value'] = False
    h.session['allowed'] = False
    assert cancel()


@pytest.mark.parametrize('action', ('managed', 'activate', 'rollback'))
@pytest.mark.parametrize('wrapped', (False, True))
def test_save_resources_checks_resulting_software_root_not_plan_or_generation_id(harness, action, wrapped):
    h = harness
    result = h.executor.result
    h.executor.result = SimpleNamespace(generation=result) if wrapped else result
    reference = {'action': action, 'selection': selection(h, action), 'review_sha256': 'e' * 64}
    h.protected[:] = [result.software_root]
    with pytest.raises(TrainingSetupSelectionError, match='protected'):
        h.owner.save_resources(reference, h.tmp / 'settings.json', h.tmp / 'tokens', h.tmp / 'output',
                               'original-revision', should_cancel=lambda: False)
    assert not any(call[0] == 'save' for call in h.executor.calls)
    h.protected.clear()
    args = (reference, h.tmp / 'settings.json', h.tmp / 'tokens', h.tmp / 'output', 'original-revision')
    assert h.owner.save_resources(*args, should_cancel=lambda: False) == 'saved'
    assert h.executor.calls[-1] == ('save', args)


def test_completed_rechecks_saved_selection_before_reading_result(harness):
    h = harness
    reference = {'action': 'existing', 'selection': str(h.protected[0] / 'software')}
    with pytest.raises(TrainingSetupSelectionError):
        controller(h).completed(SimpleNamespace(reference=reference), should_cancel=lambda: False, progress=None)
    assert not any(call[0] == 'completed' for call in h.executor.calls)
    with pytest.raises(InterruptedError):
        h.owner.completed(reference, should_cancel=lambda: True)


@pytest.mark.parametrize('pending,retained', ((True, False), (False, True), (True, True), (False, False)))
def test_close_holds_for_queue_or_retained_native_owner(harness, pending, retained):
    h = harness
    h.owner.queue.rows = [operation('pending', 'running' if pending else 'complete')]
    h.executor.process.has_retained_owners = retained
    assert h.owner.close_held is (pending or retained)
    h.owner.queue.rows[0].state = 'complete'
    h.executor.process.has_retained_owners = False
    assert not h.owner.close_held


def test_begin_close_cancels_only_exact_pending_rows_and_retains_process_hold(harness):
    h = harness
    h.owner.queue.rows = [operation('wait', 'waiting'), operation('run', 'running'),
                          operation('done', 'complete'), operation('failed', 'failed')]
    h.executor.process.has_retained_owners = True
    assert not h.owner.begin_close()
    assert h.owner.closing
    assert h.owner.queue.cancelled == ['wait', 'run']
    h.executor.process.has_retained_owners = False
    assert not h.owner.close_held
    with pytest.raises(InterruptedError):
        h.owner.check()


def test_close_still_waits_when_original_queue_cannot_cancel(harness):
    h = harness
    h.owner.queue.rows = [operation('still-owned', 'running')]
    h.owner.queue.reject_cancel = True
    assert not h.owner.begin_close()
    assert h.owner.close_held
    assert h.owner.queue.cancelled == ['still-owned']
    assert h.owner.queue.rows[0].state == 'running'


def test_process_owner_property_observes_retention_without_native_execution():
    service = host_module.ManagerTrainerSetupProcess(backend=lambda: pytest.fail('Native backend must not be invoked'))
    assert not service.has_retained_owners
    service._held['synthetic-owner'] = object()
    assert service.has_retained_owners
    service._held.clear()
    assert not service.has_retained_owners


@pytest.mark.parametrize('state', ('waiting', 'failed', 'cancelled'))
def test_retained_review_is_explicit_and_only_confirm_requeues(harness, state):
    h = harness
    reference = {'action': 'import', 'selection': str(h.tmp / 'original-pack.zip'), 'review_sha256': 'f' * 64}
    row = operation('original', state, reference)
    h.owner.queue.rows = [row]
    bridge = controller(h)
    assert h.owner.queue.enqueued == [] and h.executor.calls == []
    preview = bridge.review_retained(row.id, should_cancel=lambda: False, progress=None)
    assert h.owner.queue.enqueued == [] and h.owner.queue.cancelled == []
    bridge.apply(preview, should_cancel=lambda: False, progress=None)
    assert h.owner.queue.cancelled == (['original'] if state == 'waiting' else [])
    assert h.owner.queue.enqueued[0][2]['selection'] == reference['selection']


@pytest.mark.parametrize('change', ('params', 'running', 'missing', 'session', 'protected'))
def test_retained_confirmation_revalidates_original_row_and_authority(harness, change):
    h = harness
    row = operation('original', 'waiting', {'action': 'import', 'selection': str(h.tmp / 'pack.zip')})
    h.owner.queue.rows = [row]
    bridge = controller(h)
    preview = bridge.review_retained(row.id, should_cancel=lambda: False, progress=None)
    if change == 'params':
        row.params['selection'] = str(h.tmp / 'different.zip')
    elif change == 'running':
        row.state = 'running'
    elif change == 'missing':
        h.owner.queue.rows.clear()
    elif change == 'session':
        h.session['allowed'] = False
    else:
        h.protected[:] = [Path(row.params['selection'])]
    with pytest.raises((ValueError, InterruptedError, TrainingSetupSelectionError)):
        bridge.apply(preview, should_cancel=lambda: False, progress=None)
    assert h.owner.queue.enqueued == [] and h.owner.queue.cancelled == []


@pytest.mark.parametrize('state', ('running', 'complete'))
def test_retained_running_or_complete_opens_observation_without_requeue(harness, state):
    h = harness
    row = operation('original', state, {'action': 'activate', 'selection': GENERATION})
    h.owner.queue.rows = [row]
    pending = controller(h).review_retained(row.id, should_cancel=lambda: False, progress=None)
    assert isinstance(pending, QueuedTrainerSetup) and pending.operation_id == row.id
    assert h.owner.queue.enqueued == [] and h.owner.queue.cancelled == []


def test_retained_waiting_resume_refuses_failed_exact_row_cancellation(harness):
    h = harness
    row = operation('original', 'waiting', {'action': 'rollback', 'selection': GENERATION})
    h.owner.queue.rows = [row]
    h.owner.queue.reject_cancel = True
    bridge = controller(h)
    preview = bridge.review_retained(row.id, should_cancel=lambda: False, progress=None)
    with pytest.raises(ValueError, match='could not be cancelled'):
        bridge.apply(preview, should_cancel=lambda: False, progress=None)
    assert h.owner.queue.cancelled == ['original']
    assert h.owner.queue.enqueued == [] and row.state == 'waiting'


def test_stale_profile_does_not_cancel_original_waiting_operation(harness):
    h = harness
    row = operation('original', 'waiting', {'action': 'import', 'selection': str(h.tmp / 'pack.zip')})
    h.owner.queue.rows = [row]
    bridge = controller(h)
    preview = bridge.review_retained(row.id, should_cancel=lambda: False, progress=None)
    bridge.profile.sha256 = 'changed-profile'
    with pytest.raises(ValueError, match='profile changed'):
        bridge.apply(preview, should_cancel=lambda: False, progress=None)
    assert h.owner.queue.enqueued == [] and h.owner.queue.cancelled == []
    assert row.state == 'waiting'


@pytest.mark.parametrize('change', ('profile', 'cancelled'))
def test_confirmation_still_requires_original_profile_and_current_consent(harness, change):
    h = harness
    bridge = controller(h)
    preview = bridge.prepare('existing', selection(h, 'existing'), should_cancel=lambda: False, progress=None)
    if change == 'profile':
        bridge.profile.sha256 = 'changed-profile'
    with pytest.raises((ValueError, InterruptedError)):
        bridge.apply(preview, should_cancel=lambda: change == 'cancelled', progress=None)
    assert h.owner.queue.enqueued == []


def test_busy_original_queue_prevents_independent_or_retained_conflicting_review(harness):
    h = harness
    h.owner.queue.rows = [operation('first', 'running'),
        operation('second', 'failed', {'action': 'activate', 'selection': GENERATION})]
    bridge = controller(h)
    with pytest.raises(ValueError, match='retained Trainer Setup'):
        bridge.prepare('existing', selection(h, 'existing'), should_cancel=lambda: False, progress=None)
    with pytest.raises(ValueError, match='original pending'):
        bridge.review_retained('second', should_cancel=lambda: False, progress=None)
    assert h.owner.queue.enqueued == []


def test_cancelled_close_can_resume_review_without_restarting_or_releasing_native_owner(harness):
    h = harness
    h.executor.process.has_retained_owners = True
    h.owner.closing = True
    h.owner.resume_review()
    assert not h.owner.closing
    assert h.owner.close_held
    assert h.owner.queue.enqueued == []
    h.session['allowed'] = False
    h.owner.closing = True
    with pytest.raises(InterruptedError):
        h.owner.resume_review()
    assert h.owner.closing and h.owner.close_held
