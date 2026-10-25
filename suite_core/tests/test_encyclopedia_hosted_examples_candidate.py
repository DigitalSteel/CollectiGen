"""Hosted real-service composition with fake jobs/controls and memory-only rows."""
from dataclasses import replace
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys
import pytest

from suite_core.tests.test_encyclopedia_media_examples_candidate import browser, Signal, Index
from suite_core.tests.test_media_examples import query_service, SyntheticIndex
from suite_core.tests.test_selected_catalog import publication
from suite_core.media_examples import ExampleResult, ExampleStatus


class Bus:
    def __init__(self): self.listeners = {}
    def subscribe(self, event, callback): self.listeners.setdefault(event, []).append(callback)
    def unsubscribe(self, event, callback): self.listeners[event].remove(callback)
    def publish(self, event, **payload):
        for callback in list(self.listeners.get(event, ())): callback(**payload)


class Task:
    """Shape of CoordinatedCallTask; no thread, job manager or Qt is executed."""
    def __init__(self, manager, name, work, *, resource, key):
        self.manager, self.work, self.key, self.resource = manager, work, key, resource
        self.signals = SimpleNamespace(complete=Signal(), failed=Signal(), cancelled=Signal())
        self.cancelled = False
    def start(self): self.manager.tasks.append(self)
    def cancel(self): self.cancelled = True
    def finish(self):
        result = self.work(SimpleNamespace(checkpoint=lambda: not self.cancelled))
        self.signals.complete.emit(result)
        return result


class Federation:
    def __init__(self, dataset, queries): self.dataset, self.queries = dataset, queries; self.calls = 0
    def evidence_snapshot_token(self):
        return ('federated', tuple((profile.stable_id, 'synthetic-cache', 9, manager.index.revision)
                                  for profile, manager in self.dataset.scoped_profile_managers()
                                  if manager.index is not None))
    def query_evidence_groups(self, groups, match_all, *, should_continue=None, progress=None):
        self.calls += 1; paths, coverage = [], {}
        for _, manager in self.dataset.scoped_profile_managers():
            result = self.queries.query_result(manager.index, groups, match_all, should_continue=should_continue)
            paths.extend(result.paths); coverage.update(result.coverage)
        return tuple(paths), coverage


@pytest.fixture
def hosted(browser, query_service, monkeypatch):
    window, module = browser
    path = Path(__file__).with_name('hosted_examples.py')
    if not path.is_file(): path = Path(__file__).resolve().parents[1] / 'encyclopedia/hosted_examples.py'
    spec = importlib.util.spec_from_file_location('suite_core.encyclopedia.hosted_examples', path)
    adapter = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, adapter); spec.loader.exec_module(adapter)
    a = SyntheticIndex('a', [('same.png', ('Red Fox',)), ('other.png', ('dog',))])
    b = SyntheticIndex('b', [('same.png', ('Vulpes',))])
    participants = [(SimpleNamespace(stable_id='a'), SimpleNamespace(index=a)),
                    (SimpleNamespace(stable_id='b'), SimpleNamespace(index=b))]
    dataset = SimpleNamespace(scope_library_ids=('a', 'b'),
        scoped_profile_managers=lambda: tuple(row for row in participants if row[0].stable_id in dataset.scope_library_ids),
        identity_for_path=lambda path: SimpleNamespace(library_id=path.split('/')[2], relative_path=path.split('/')[3]))
    dataset.index = Federation(dataset, query_service)
    record = SimpleNamespace(id='shared-id', display='Red Fox', aliases=('Vulpes',), category='animals')
    knowledge = SimpleNamespace(tags={record.id: record}, identity_names=lambda tag_id:
        (knowledge.tags[tag_id].display, *knowledge.tags[tag_id].aliases) if tag_id in knowledge.tags else ())
    session = SimpleNamespace(dataset=dataset, evidence_queries=query_service, knowledge=knowledge,
                              events=Bus(), jobs=SimpleNamespace(tasks=[]))
    gui = []
    binding = window.attach_hosted_library_session(session, admitted_publication=window.publication,
        task_factory=Task, post_to_gui=gui.append)
    window.table.setCurrentIndex(Index('shared-id'))
    return SimpleNamespace(window=window, module=module, session=session, binding=binding,
                           gui=gui, participants=participants, adapter=adapter)


def flush(hosted):
    while hosted.gui: hosted.gui.pop(0)()


def find(hosted):
    hosted.window.examples_button.click()
    return hosted.session.jobs.tasks[-1]


def test_visible_hosted_entry_uses_same_native_service_and_federation(hosted):
    task = find(hosted)
    assert task.manager is hosted.session.jobs and task.resource == 'cpu'
    result = task.finish()
    assert result.status == ExampleStatus.SUCCESS and not hosted.window.examples_model.rows
    assert hosted.session.dataset.index.calls == 1
    flush(hosted)
    assert [row.columns for row in hosted.window.examples_model.rows] == [('a', 'same.png'), ('b', 'same.png')]
    assert not hosted.binding._deliveries and not hosted.binding._tasks


def test_no_scope_retains_no_library_state_and_never_queries(hosted):
    hosted.session.dataset.scope_library_ids = ()
    hosted.session.events.publish('library_scope_changed'); flush(hosted)
    assert 'No library connected' in hosted.window.examples_status.text()
    assert not hosted.window.examples_button.enabled and not hosted.session.jobs.tasks


def test_native_names_must_equal_published_identity_groups(hosted):
    hosted.session.knowledge.tags['shared-id'].aliases = ('Different',)
    result = find(hosted).finish(); flush(hosted)
    assert result.status == ExampleStatus.STALE and not hosted.window.examples_model.rows
    assert hosted.session.dataset.index.calls == 0


@pytest.mark.parametrize('change', ['scope', 'index', 'names', 'event', 'session', 'catalog'])
def test_changes_between_work_and_delivery_hold_without_gui_cache_reads(hosted, change):
    task = find(hosted); task.finish()
    if change == 'scope': hosted.session.dataset.scope_library_ids = ('b',)
    elif change == 'index': hosted.participants[0][1].index = SyntheticIndex('a', [])
    elif change == 'names': hosted.session.knowledge.tags['shared-id'].display = 'Changed'
    elif change == 'event': hosted.session.events.publish('image_data_changed', evidence_changed=True)
    elif change == 'session': hosted.session.dataset = SimpleNamespace()
    else:
        hosted.module.read = lambda _: publication('different-catalog')
        hosted.window.open_snapshot('synthetic-foreign.json')
    # Any accidental native token call on delivery is a hard test failure.
    hosted.binding._source_token = lambda: (_ for _ in ()).throw(AssertionError('GUI cache read'))
    flush(hosted)
    assert not hosted.window.examples_model.rows


def test_library_event_rebinds_current_scope_and_retry_is_visible(hosted):
    find(hosted).finish(); flush(hosted)
    hosted.session.dataset.scope_library_ids = ('b',)
    hosted.session.events.publish('library_scope_changed'); flush(hosted)
    assert not hosted.window.examples_model.rows and hosted.window.examples_button.enabled
    result = find(hosted).finish(); flush(hosted)
    assert result.request.library_ids == ('b',)
    assert [row.columns for row in hosted.window.examples_model.rows] == [('b', 'same.png')]


@pytest.mark.parametrize('missing', ['participant', 'index', 'generation'])
def test_partial_or_unknown_native_sources_are_unavailable(hosted, missing):
    if missing == 'participant': hosted.participants.pop()
    elif missing == 'index': hosted.participants[0][1].index = None
    else: hosted.participants[0][1].index.revision = -1
    result = find(hosted).finish(); flush(hosted)
    assert result.status == ExampleStatus.UNAVAILABLE and not hosted.window.examples_model.rows


def test_native_source_generation_changed_during_work_discards_refs(hosted):
    index = hosted.session.dataset.index; query = index.query_evidence_groups
    def changed(*args, **kwargs):
        result = query(*args, **kwargs); hosted.participants[0][1].index.revision += 1; return result
    index.query_evidence_groups = changed
    result = find(hosted).finish(); flush(hosted)
    assert result.status == ExampleStatus.STALE and not hosted.window.examples_model.rows


def test_browser_cancel_stops_native_checkpoint_and_older_job_cannot_clear_retry(hosted):
    old = find(hosted); hosted.window.examples_cancel.click(); new = find(hosted)
    assert old.key != new.key
    assert old.finish().status == ExampleStatus.STALE
    flush(hosted); assert hosted.window.examples_cancel.enabled
    new.finish(); flush(hosted); assert len(hosted.window.examples_model.rows) == 2


def test_native_job_checkpoint_cancelled_status_is_visible(hosted):
    task = find(hosted); task.cancelled = True
    assert task.finish().status == ExampleStatus.CANCELLED
    flush(hosted); assert 'Cancelled' in hosted.window.examples_status.text()


@pytest.mark.parametrize('signal, text', [('failed', 'could not be queried'), ('cancelled', 'Cancelled')])
def test_native_job_terminal_signals_leave_loading_and_suppress_private_error(hosted, signal, text):
    task = find(hosted)
    if signal == 'failed': task.signals.failed.emit('PRIVATE NATIVE PATH')
    else: task.signals.cancelled.emit()
    flush(hosted)
    assert text in hosted.window.examples_status.text()
    assert 'PRIVATE' not in hosted.window.examples_status.text()
    assert hosted.window.examples_button.enabled and not hosted.binding._tasks


def test_detach_and_window_close_unsubscribe_and_cancel_host_observers(hosted):
    task = find(hosted); hosted.window.detach_hosted_library_session()
    assert task.cancelled and all(not callbacks for callbacks in hosted.session.events.listeners.values())
    assert 'No library connected' in hosted.window.examples_status.text()
    assert not hosted.binding._tasks and not hosted.binding._deliveries
    hosted.session.events.publish('knowledge_changed'); flush(hosted)
    hosted.window.closeEvent(object())


def test_foreign_publication_requires_explicit_host_admission(hosted):
    with pytest.raises(ValueError, match='host-admitted Publication'):
        hosted.window.attach_hosted_library_session(hosted.session, admitted_publication=publication('foreign'),
            task_factory=Task, post_to_gui=hosted.gui.append)
    assert 'No library connected' in hosted.window.examples_status.text()


def test_no_subset_index_or_scope_rewrite_is_manufactured(hosted):
    request = hosted.window.catalog_session.example_request(selection_token=hosted.window.example_selection_token(),
        library_ids=('a',), tag_ids=('shared-id',))
    result = hosted.binding.query(request)
    assert result.status == ExampleStatus.STALE and hosted.session.dataset.scope_library_ids == ('a', 'b')
    assert hosted.session.dataset.index.calls == 0


def test_catalog_switch_keeps_native_scope_but_requires_explicit_rebinding(hosted):
    hosted.module.read = lambda _: publication('different-catalog')
    hosted.window.open_snapshot('synthetic-foreign.json')
    hosted.window.table.setCurrentIndex(Index('shared-id'))
    assert not hosted.window.examples_button.enabled
    assert 'not bound to the hosted library session' in hosted.window.examples_status.text()
    assert not hosted.session.jobs.tasks


def test_failed_task_start_detaches_native_observer_and_recovers_visible_state(hosted):
    class RejectedTask(Task):
        def start(self):
            super().start(); raise RuntimeError('PRIVATE scheduler detail')
    hosted.binding.task_factory = RejectedTask
    task = find(hosted)
    assert task.cancelled and not hosted.binding._tasks
    assert 'could not be scheduled' in hosted.window.examples_status.text()
    assert hosted.window.examples_button.enabled
    task.finish(); flush(hosted)
    assert not hosted.binding._deliveries and not hosted.window.examples_model.rows


def test_close_during_work_cannot_register_or_deliver_new_receipt(hosted):
    task = find(hosted)
    original = hosted.session.dataset.index.query_evidence_groups
    def close_after_query(*args, **kwargs):
        result = original(*args, **kwargs)
        hosted.window.closeEvent(object())
        return result
    hosted.session.dataset.index.query_evidence_groups = close_after_query
    result = task.finish(); flush(hosted)
    assert result.status == ExampleStatus.STALE
    assert task.cancelled and not hosted.binding._deliveries and not hosted.window.examples_model.rows
