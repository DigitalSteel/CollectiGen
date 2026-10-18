"""Retained supervisor component doubles, not native retirement evidence."""
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import pytest

from core.encyclopedia_legacy_handoff import LegacyReaderHandoff
from core.encyclopedia_legacy_host import CompiledLegacyReaderProfile
from suite_core.manager.legacy_reader_supervisor import LegacyReaderSupervisor


@pytest.fixture
def actor(tmp_path,monkeypatch):
    profile=object.__new__(CompiledLegacyReaderProfile)
    plan=object.__new__(LegacyReaderHandoff)
    binding=SimpleNamespace(root=tmp_path,bootstrap_program=tmp_path/'TagEncyclopedia.exe')
    object.__setattr__(plan,'binding',binding)
    monkeypatch.setattr(LegacyReaderHandoff,'revalidate',lambda self:None)
    monkeypatch.setattr(CompiledLegacyReaderProfile,'verify_bootstrap',lambda self,root:None)
    state=dict(code=None,members=1,alive=True,closed=False,spawn_calls=0,fail_birth=False)
    child=SimpleNamespace(poll=lambda:state['code'])
    class Native:
        def __init__(self):
            self.job=SimpleNamespace(active=lambda:state['members'])
            self.drained=Event();self.drained.set()
            self.drain=SimpleNamespace(is_alive=lambda:state['alive'])
            self.drain_failed=False
        def spawn_command(self,*args):
            state['spawn_calls']+=1
            # Verify this actor selects the explicit non-killing policy before
            # any native construction. Separate Job tests check Win32 flags.
            assert self.job_factory.keywords=={'interactive':True,'kill_on_close':False}
            if state['fail_birth']:raise OSError('injected uncertain birth')
            return child
        def identity(self,*args):return {'original':True}
        def check_identity(self,*args):pass
        def close(self,*args):state['closed']=True
    monkeypatch.setattr('core.ollama_owned_windows.WindowsOwnedProcess',Native)
    return LegacyReaderSupervisor(profile,plan),state


def test_exit_alone_never_retires_descendants_or_output(actor):
    subject,state=actor
    subject.start({})
    state['code']=0
    assert not subject.poll() and not state['closed']
    state['members']=0
    assert not subject.poll() and not state['closed']
    state['alive']=False;subject.native.drained.clear()
    assert not subject.poll() and not state['closed']
    subject.native.drained.set()
    assert subject.poll() and subject.retired and state['closed']
    assert subject.exit_code==0 and not subject.close_held


def test_original_attempt_cannot_be_replayed_after_uncertain_birth(actor):
    subject,state=actor;state['fail_birth']=True
    with pytest.raises(OSError):subject.start({})
    assert subject.native is not None and subject.close_held
    with pytest.raises(ValueError,match='already_started'):subject.start({})
    with pytest.raises(ValueError,match='partial_legacy_birth'):subject.poll()
    assert state['spawn_calls']==1 and not state['closed']


def test_failed_output_never_becomes_a_drain_receipt(actor):
    subject,state=actor;subject.start({})
    state.update(code=0,members=0,alive=False)
    subject.native.drain_failed=True
    with pytest.raises(ValueError,match='output_drain_failed'):subject.poll()
    assert not subject.retired and not state['closed']


def test_native_retirement_does_not_hide_an_outstanding_worker(actor):
    subject,state=actor;subject.start({})
    state.update(code=0,members=0,alive=False)
    executor=SimpleNamespace(close_held=True,begin_close=lambda:None)
    subject.executor=executor
    assert subject.poll() and subject.retired and subject.close_held
    executor.close_held=False
    assert subject.recheck() and not subject.close_held


def test_recovery_cannot_race_a_worker_before_its_native_birth(actor):
    subject,state=actor
    subject.executor=SimpleNamespace(close_held=True,begin_close=lambda:None)
    assert not subject.recheck() and not subject.retired and subject.native is None
    subject.start({})
    assert subject.child is not None and not subject.retired and subject.close_held


def test_last_member_exit_during_output_drain_does_not_fault_progress(actor):
    from core.training_process_windows import NativeObservationError
    subject,state=actor;subject.start({})
    def empty_snapshot():raise NativeObservationError('last original member retired')
    subject.native.job.process_ids=empty_snapshot
    state.update(code=0,members=0,alive=True)
    assert not subject.poll() and not subject.reader_visible()
    assert subject.close_held and not state['closed']
    state['alive']=False
    assert subject.poll() and subject.retired


def test_live_job_observation_failure_remains_held(actor):
    from core.training_process_windows import NativeObservationError
    subject,state=actor;subject.start({})
    def bad_snapshot():raise NativeObservationError('unknown original membership')
    subject.native.job.process_ids=bad_snapshot
    with pytest.raises(NativeObservationError):subject.reader_visible()
    assert subject.close_held and not state['closed']
