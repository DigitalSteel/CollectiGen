"""Original-output reconciliation doubles; no process is created or adopted."""
from types import SimpleNamespace
import pytest
from core.ollama_owned_windows import WindowsOwnedProcess
from core.ollama_owned_executor import ExecutorHold

COMMAND=('D:/synthetic-root/TagEncyclopedia.exe',)


def actor(job=None,entered=False):
    native=object.__new__(WindowsOwnedProcess)
    native.job=job;native.logs={};native.drain=None
    native.ordinary_create_entered=entered
    return native


def job(**changes):
    state=SimpleNamespace(closed=False)
    value=SimpleNamespace(kill_on_close=False,creation_command=COMMAND,creation_entered=True,
        creation_returned=False,creation_info=SimpleNamespace(process=0,thread=0,pid=0),
        process=None,thread=None,pid=None,active=lambda:0,
        close=lambda:setattr(state,'closed',True))
    for key,val in changes.items():setattr(value,key,val)
    return value,state


def test_positive_failure_before_create_can_settle_without_a_child():
    native=actor()
    assert native.recover_failed_root_child(COMMAND) is None


def test_missing_job_after_entered_create_is_not_positive_noncreation():
    with pytest.raises(ExecutorHold,match='positive_precreation'):
        actor(entered=True).recover_failed_root_child(COMMAND)


@pytest.mark.parametrize('entered,returned',[(False,None),(True,False)])
def test_positive_native_noncreation_closes_only_its_empty_job(entered,returned):
    original,state=job(creation_entered=entered,creation_returned=returned)
    native=actor(original,entered=True)
    assert native.recover_failed_root_child(COMMAND) is None
    assert state.closed and native.job is None


def test_unknown_native_return_retains_the_original_job():
    original,state=job(creation_returned=None)
    native=actor(original,entered=True)
    with pytest.raises(ExecutorHold,match='positive_native_noncreation'):
        native.recover_failed_root_child(COMMAND)
    assert native.job is original and not state.closed


def test_original_native_outputs_recover_the_same_handle_once():
    original,state=job(creation_returned=True,creation_info=SimpleNamespace(process=91,thread=92,pid=93))
    native=actor(original,entered=True)
    child=native.recover_failed_root_child(COMMAND)
    assert child._handle==91 and child.pid==93 and child.job is original
    assert native.recover_failed_root_child(COMMAND) is child
    assert not state.closed and list(native.logs)==[child]


def test_foreign_command_cannot_recover_a_process():
    original,state=job()
    with pytest.raises(ExecutorHold,match='original_non_killing'):
        actor(original,entered=True).recover_failed_root_child(('D:/elsewhere.exe',))
    assert not state.closed
