"""Job configuration regression; no child is created by these tests."""
import ctypes
import pytest
from core import training_process_windows as windows


@pytest.mark.parametrize('policy,expected', [(None,0x2000),(True,0x2000),(False,0)])
def test_default_training_and_explicit_custody_policies(monkeypatch,policy,expected):
    flags=[];closed=[]
    class API:
        def CreateJobObjectW(self,*args):return 91
        def SetInformationJobObject(self,handle,kind,pointer,size):
            assert handle==91 and kind==9
            flags.append(ctypes.cast(pointer,ctypes.POINTER(windows._ExtendedLimits)).contents.basic.flags)
            return 1
        def CloseHandle(self,handle):closed.append(handle);return 1
    monkeypatch.setattr(windows,'_api',lambda:API())
    monkeypatch.setattr(windows.C,'get_last_error',lambda:0)
    kwargs={} if policy is None else {'kill_on_close':policy}
    job=windows.WindowsTrainingJob('inert-configuration-only',**kwargs)
    assert flags==[expected]
    assert job.process is None and job.thread is None
    job.close()
    assert closed==[91]


def test_unknown_policy_cannot_create_a_job(monkeypatch):
    monkeypatch.setattr(windows,'_api',lambda:pytest.fail('native construction must not start'))
    with pytest.raises(ValueError,match='explicit_job_close_policy_required'):
        windows.WindowsTrainingJob('inert',kill_on_close='false')
