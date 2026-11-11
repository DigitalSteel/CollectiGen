"""Synthetic inventory read/identity controls; no models or trainer are launched."""
from pathlib import Path
import os
import time
import pytest
from core import training_file_inventory as inventory


def test_complete_tree_digest_refuses_same_stat_content_drift(tmp_path):
    root=tmp_path/'software';root.mkdir(); file=root/'code.py'; file.write_bytes(b'first')
    service=inventory.SoftwareInventoryService(); expected=service.capture(root)
    manifest=tmp_path/'inventory.json';service.save(expected,manifest)
    assert service.verify(root,manifest)==expected.summary()
    original=file.stat();file.write_bytes(b'other');os.utime(file,ns=(original.st_atime_ns,original.st_mtime_ns))
    with pytest.raises(inventory.FileInventoryError,match='changed'):
        service.verify(root,manifest)


class ObservedReader:
    def __init__(self,handle,on_read=None,on_close=None):
        self.handle=handle;self.reads=0;self.on_read=on_read;self.on_close=on_close
    def __enter__(self):return self
    def __exit__(self,*args):
        self.handle.close()
        if self.on_close:self.on_close()
    def fileno(self):return self.handle.fileno()
    def read(self,*args):
        self.reads+=1; raw=self.handle.read(*args)
        if self.on_read and self.reads==1:self.on_read()
        return raw


def selected(tmp_path):
    path=tmp_path/'selected.bin';path.write_bytes(b'original selected bytes')
    return path,inventory._check_path(path)


def test_replacement_between_path_check_and_open_is_refused_before_read(tmp_path,monkeypatch):
    path,state=selected(tmp_path); replacement=tmp_path/'replacement';replacement.write_bytes(path.read_bytes())
    initial=path.stat();os.utime(replacement,ns=(initial.st_atime_ns,initial.st_mtime_ns))
    original_open=Path.open;readers=[]
    def opening(target,*args,**kwargs):
        if target==path and args and args[0]=='rb':
            path.replace(tmp_path/'retained-original');replacement.replace(path)
            reader=ObservedReader(original_open(target,*args,**kwargs));readers.append(reader);return reader
        return original_open(target,*args,**kwargs)
    monkeypatch.setattr(Path,'open',opening)
    with pytest.raises(inventory.FileInventoryError,match='before reading'):
        inventory._hash_file(path,state,None,time.monotonic()+30)
    assert len(readers)==1 and readers[0].reads==0


def test_change_while_open_is_refused_after_read(tmp_path,monkeypatch):
    path,state=selected(tmp_path);original_open=Path.open
    def change():
        with original_open(path,'ab') as writer:writer.write(b' changed')
    def opening(target,*args,**kwargs):
        handle=original_open(target,*args,**kwargs)
        return ObservedReader(handle,on_read=change) if target==path and args and args[0]=='rb' else handle
    monkeypatch.setattr(Path,'open',opening)
    with pytest.raises(inventory.FileInventoryError,match='while reading'):
        inventory._hash_file(path,state,None,time.monotonic()+30)


def test_final_path_replacement_is_refused_after_descriptor_read(tmp_path,monkeypatch):
    path,state=selected(tmp_path);replacement=tmp_path/'replacement';replacement.write_bytes(path.read_bytes())
    original_open=Path.open
    def change():
        path.replace(tmp_path/'retained-original');replacement.replace(path)
    def opening(target,*args,**kwargs):
        handle=original_open(target,*args,**kwargs)
        return ObservedReader(handle,on_close=change) if target==path and args and args[0]=='rb' else handle
    monkeypatch.setattr(Path,'open',opening)
    with pytest.raises(inventory.FileInventoryError,match='while hashing'):
        inventory._hash_file(path,state,None,time.monotonic()+30)


def test_cancel_and_time_bound_remain_in_effect(tmp_path):
    path,state=selected(tmp_path)
    with pytest.raises(InterruptedError):inventory._hash_file(path,state,lambda:True,time.monotonic()+30)
    with pytest.raises(inventory.FileInventoryError,match='inspection time'):
        inventory._hash_file(path,state,None,time.monotonic()-1)


@pytest.mark.parametrize('reason,code,error_type',[
    ('Software inventory exceeded its bounded inspection time.','resource_unavailable','FileInventoryError'),
    ('Selected software bytes or directory membership changed.','input_changed','TrainingExecutionPlanError'),
])
def test_worker_retains_timeout_category_through_suppressed_exception_context(reason,code,error_type):
    from core.training_execution_plan import TrainingExecutionPlanError
    from tools.training_execution_worker import failure_record
    try:
        try:raise inventory.FileInventoryError(reason)
        except inventory.FileInventoryError:raise TrainingExecutionPlanError('training_execution_unavailable_or_changed') from None
    except TrainingExecutionPlanError as exc:
        record=failure_record(exc)
    assert record['code']==code and record['error_type']==error_type
    assert reason not in str(record)


def test_current_checkpoint_code_policy_accepts_reviewed_inventory_guard():
    from core import training_continuation
    profile = training_continuation.check_profile_code()
    assert profile == training_continuation.profile_contract()
    assert profile["qualification"]["application_writer_verified"] is False
