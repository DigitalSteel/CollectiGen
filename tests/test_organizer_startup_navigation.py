"""Organizer cold navigation retains stock task/authority and selected scope."""
from types import SimpleNamespace
import pytest
from PySide6.QtCore import QObject
from PySide6.QtWidgets import QWidget
from suite_core.media_organizer.entry import make_window_type


class Signal:
    def __init__(self):self.callbacks=[]
    def connect(self,callback):self.callbacks.append(callback)
    def emit(self,*args):
        for callback in tuple(self.callbacks):callback(*args)


@pytest.fixture
def waiting_window(qt_test_scope):
    class Base(QWidget):
        def __init__(self,**kwargs):
            super().__init__();self.startup_cancelled=True;self.built=[];self.progress=[];self.errors=[];self.messages=[]
            self.index=2;self.selected=1;self._dataset_preparation_shutdown=False
            owner=self
            class Navigation(QObject):
                def currentRow(self):return owner.selected
                def setCurrentRow(self,index):
                    owner.selected=index
                    if not self.signalsBlocked():owner.change_page(index)
            self.navigation=Navigation(self)
            placeholder=SimpleNamespace(set_progress=lambda *args:self.progress.append(args),set_error=self.errors.append)
            self.stack=SimpleNamespace(currentIndex=lambda:self.index,setCurrentIndex=lambda index:setattr(self,'index',index),widget=lambda index:placeholder)
            self._dataset_page_task=SimpleNamespace(signals=SimpleNamespace(complete=Signal(),failed=Signal(),cancelled=Signal()))
        def statusBar(self):return SimpleNamespace(showMessage=lambda *args:self.messages.append(args))
        def change_page(self,index):
            self.built.append(index)
            if index==1:self.ingest_page=object()
        def _open_ingest_attention(self):
            self.attention_opened=getattr(self,'attention_opened',0)+1
    window=make_window_type(Base,current_application=lambda:None)()
    qt_test_scope.show(window)
    return window


def test_ingest_waits_once_for_original_dataset_reader_then_opens(waiting_window):
    window=waiting_window;task=window._dataset_page_task
    window.change_page(1);window.change_page(1)
    assert not window.built and window.index==1 and window.progress
    assert len(task.signals.complete.callbacks)==1
    window._dataset_page_task=None
    task.signals.complete.emit(object())
    assert window.built==[1] and hasattr(window,'ingest_page')


@pytest.mark.parametrize('terminal',['failed','cancelled'])
def test_ingest_failed_preparation_remains_recoverable_without_constructor(waiting_window,terminal):
    window=waiting_window;task=window._dataset_page_task;window.change_page(1)
    window._dataset_page_task=None
    if terminal=='failed':task.signals.failed.emit('Synthetic guarded preparation failure')
    else:task.signals.cancelled.emit()
    assert not window.built and window.errors and 'retry' in window.messages[-1][0]
    window.change_page(1)
    assert window.built==[1]


@pytest.mark.parametrize('stop',['navigation','close'])
def test_ingest_pending_navigation_does_not_reopen_after_scope_change(waiting_window,stop):
    window=waiting_window;task=window._dataset_page_task;window.change_page(1)
    if stop=='navigation':window.selected=6;window.change_page(6)
    else:window._dataset_preparation_shutdown=True
    window._dataset_page_task=None;task.signals.complete.emit(object())
    assert 1 not in window.built


def test_existing_ingest_does_not_wait_on_unrelated_dataset_work(waiting_window):
    window=waiting_window;window.ingest_page=object();window.change_page(1)
    assert window.built==[1] and not window.progress


def test_attention_entry_cannot_bypass_initial_knowledge_reader(waiting_window):
    window=waiting_window;task=window._dataset_page_task
    window._open_ingest_attention()
    assert not window.built and not getattr(window,'attention_opened',0)
    window._dataset_page_task=None;task.signals.complete.emit(object())
    assert window.built==[1] and window.attention_opened==1


def test_attention_pending_intent_is_discarded_when_navigation_changes(waiting_window):
    window=waiting_window;task=window._dataset_page_task;window._open_ingest_attention()
    window.navigation.setCurrentRow(6)
    window._dataset_page_task=None;task.signals.complete.emit(object())
    assert 1 not in window.built and not getattr(window,'attention_opened',0)


@pytest.mark.parametrize('entry_route',['navigation','attention'])
def test_setup_refusal_preserves_page_and_pending_intent_after_worker_completion(waiting_window,entry_route):
    window=waiting_window;task=window._dataset_page_task;decisions=[]
    window.index=6;window.selected=6
    window._organizer_ingest_wait_task=None;window._organizer_ingest_attention_pending=False
    def decline():decisions.append(False);return False
    window.setup_page=SimpleNamespace(confirm_leave=decline)
    if entry_route=='navigation':window.navigation.setCurrentRow(1)
    else:window._open_ingest_attention()
    assert window.index==6 and window.selected==6 and not window.built
    assert decisions==[False] and not window.progress
    assert window._organizer_ingest_wait_task is None and not window._organizer_ingest_attention_pending
    window._dataset_page_task=None;task.signals.complete.emit(object())
    assert window.index==6 and window.selected==6 and not window.built
    assert window._organizer_ingest_wait_task is None and not window._organizer_ingest_attention_pending
    assert not getattr(window,'attention_opened',0)


def test_setup_acceptance_gates_wait_once_then_opens_ingest(waiting_window):
    window=waiting_window;task=window._dataset_page_task;decisions=[]
    window.index=6
    def accept():decisions.append(True);return True
    window.setup_page=SimpleNamespace(confirm_leave=accept)
    window.navigation.setCurrentRow(1)
    assert decisions==[True] and window.index==1 and not window.built
    window._dataset_page_task=None;task.signals.complete.emit(object())
    assert decisions==[True] and window.built==[1]


@pytest.mark.parametrize("state",["complete","failed","cancelled"])
def test_ingest_replays_dataset_terminal_result_emitted_before_subscription(waiting_window,qt_test_scope,state):
    window=waiting_window;task=window._dataset_page_task;task.job_id="synthetic-dataset-job"
    job=SimpleNamespace(state=state,error="synthetic reader failure" if state=="failed" else "")
    window.state=SimpleNamespace(jobs=SimpleNamespace(get=lambda job_id:job))
    # The task already emitted; only the stock Dataset owner received it.
    task.signals.complete.emit(object())
    window.change_page(1)
    assert not window.built and window._organizer_ingest_wait_task is task
    # Stock queued completion clears its task before the deferred replay runs.
    window._dataset_page_task=None
    qt_test_scope.app.processEvents()
    assert window._organizer_ingest_wait_task is None
    if state=="complete":
        assert window.built==[1] and not window.errors
    else:
        assert not window.built
        assert window.errors==["synthetic reader failure" if state=="failed" else "Dataset preparation was cancelled."]


def test_dataset_completion_racing_terminal_read_opens_ingest_once(waiting_window,qt_test_scope):
    window=waiting_window;task=window._dataset_page_task;task.job_id="synthetic-dataset-job"
    job=SimpleNamespace(state="complete",error="")
    def terminal_read(_job_id):
        window._dataset_page_task=None
        task.signals.complete.emit(object())
        return job
    window.state=SimpleNamespace(jobs=SimpleNamespace(get=terminal_read))
    window.change_page(1);qt_test_scope.app.processEvents()
    assert window.built==[1] and window._organizer_ingest_wait_task is None


def test_deferred_terminal_replay_does_not_reopen_ingest_after_navigation(waiting_window,qt_test_scope):
    window=waiting_window;task=window._dataset_page_task;task.job_id="synthetic-dataset-job"
    window.state=SimpleNamespace(jobs=SimpleNamespace(get=lambda _job_id:SimpleNamespace(state="complete",error="")))
    window.change_page(1);window.selected=6;window.change_page(6)
    window._dataset_page_task=None;qt_test_scope.app.processEvents()
    assert window.built==[6] and window._organizer_ingest_wait_task is None
