"""Qt component state/Close tests with a manual worker and authenticated-session double.

Private slot driving is component evidence, not a packaged visible journey.
"""
from concurrent.futures import Future
from types import SimpleNamespace
import pytest
from PySide6.QtWidgets import QApplication, QWidget
from suite_core.manager.close_policy import CloseCoordinator
from suite_core.manager import legacy_reader_handoff_ui as ui


class ManualExecutor:
    def __init__(self):self.close_held=False;self.closed=False;self.pending=None
    def submit(self,fn,*args):
        assert not self.closed and self.pending is None
        future=Future();self.pending=(future,fn,args);return future
    def finish(self):
        future,fn,args=self.pending;self.pending=None
        try:future.set_result(fn(*args))
        except BaseException as error:future.set_exception(error)
    def begin_close(self):self.closed=True


@pytest.fixture
def flow(monkeypatch):
    app=QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui,'ResourceExecutorOwner',ManualExecutor)
    selected=SimpleNamespace(version='0.2.2-alpha.1')
    class Window(QWidget):
        def __init__(self):
            super().__init__();self.close_policy=CloseCoordinator();self.accept_close=False;self.close_calls=0
        def close(self):
            self.close_calls+=1
            self.close_policy.pending=True;self.close_policy.request_id+=1
            return self.accept_close
        def cancel_pending_close(self):self.close_policy.cancel()
    class Session:
        def __init__(self):
            self.reader_selection=SimpleNamespace(selected=selected);self.operations=[]
            self.state='ready';self.refused=False;self.lost_reply=False
            self.unsent=False
        def request_legacy_reader(self,operation,handoff_id):
            self.operations.append(operation)
            if self.unsent:raise ui.LegacyReaderPrepareNotSent('Synthetic preparation never sent.')
            if self.lost_reply:
                self.state='held';raise TimeoutError('injected unknown original reply')
            if self.refused:
                return dict(status='refused',handoff_id=None,committed=False,consumed=True,reason='synthetic selection conflict')
            return dict(status='ready',handoff_id='c'*64,committed=operation=='commit',
                        consumed=operation=='cancel',reason=None)
    window=Window();session=Session();subject=ui.LegacyReaderHandoffFlow(window,session)
    subject.open(selected)
    yield subject,window,session
    subject.timer.stop()
    assert not subject.executor.close_held
    subject.dialog.hide();subject.dialog.deleteLater();window.deleteLater();app.processEvents()


def prepare(subject):
    subject.advance();subject.executor.finish();subject.poll()


def test_preparation_wait_is_visible_and_cancel_waits_for_original_worker(flow):
    subject,window,session=flow
    subject.advance()
    assert subject.state=='preparing' and subject.close_held
    assert 'Checking' in subject.dialog.status.text()
    subject.cancel()
    assert subject.cancel_pending and subject.close_held
    subject.executor.finish();subject.poll()
    assert subject.state=='cancelling' and subject.close_held
    subject.executor.finish();subject.poll()
    assert subject.state=='done' and not subject.close_held
    assert session.operations==['prepare','cancel'] and window.close_calls==0


def test_clean_refusal_leaves_manager_and_close_available(flow):
    subject,window,session=flow;session.refused=True
    prepare(subject)
    assert subject.state=='done' and not subject.close_held and session.state=='ready'
    assert subject.dialog.cancel.isEnabled() and subject.dialog.cancel.text()=='Close'
    assert 'Manager stays open' in subject.dialog.status.text()
    subject.cancel()
    assert not subject.dialog.isVisible() and window.close_calls==0


def test_proven_unsent_prepare_can_close_its_dialog_without_arming_launch(flow):
    subject,window,session=flow;session.unsent=True
    prepare(subject)
    assert subject.state=='done' and not subject.close_held and session.state=='ready'
    assert subject.dialog.cancel.isEnabled() and subject.dialog.cancel.text()=='Close'
    subject.cancel()
    assert not subject.dialog.isVisible() and window.close_calls==0


def test_declined_close_revokes_queued_close_intent_before_cancel(flow):
    subject,window,session=flow
    prepare(subject);subject.advance();subject.executor.finish();subject.poll()
    assert subject.state=='cancelling' and subject.close_held
    assert not window.close_policy.pending and window.close_policy.request_id==2
    subject.executor.finish();subject.poll()
    assert not subject.close_held and not window.close_policy.pending
    assert session.operations==['prepare','commit','cancel'] and window.close_calls==1


def test_accepted_close_does_not_cancel_the_committed_handoff(flow):
    subject,window,session=flow;window.accept_close=True
    prepare(subject);subject.advance();subject.executor.finish();subject.poll()
    assert subject.state=='closing' and not subject.close_held and subject.executor.closed
    assert session.operations==['prepare','commit'] and window.close_calls==1


def test_lost_reply_keeps_the_original_operation_held(flow):
    subject,window,session=flow;session.lost_reply=True
    prepare(subject)
    assert subject.state=='held' and subject.close_held and session.state=='held'
    assert not subject.dialog.cancel.isEnabled() and not subject.dialog.primary.isEnabled()
    assert window.close_calls==0


def test_completed_future_does_not_hide_a_live_worker(flow):
    subject,window,session=flow
    subject.advance();subject.executor.finish();subject.executor.close_held=True
    subject.poll()
    assert subject.state=='preparing' and subject.close_held
    subject.executor.close_held=False;subject.poll()
    assert subject.state=='prepared'
    subject.cancel();subject.executor.finish();subject.poll()
    assert not subject.close_held
