"""Content-free evidence survives the original actor's physical cleanup."""
import json
from types import SimpleNamespace
import pytest
from pathlib import Path

from core.workflow_native_owner import WorkflowWorkspaceOwner
from suite_core.diagnostics import Diagnostics, Component, valid_workflow_failure
import suite_core.diagnostics as diagnostic_source
assert Path(diagnostic_source.__file__).resolve() == Path(__file__).resolve().parents[1]/'suite_core/diagnostics.py'


def snapshot():
    return dict(phase='receive', state='ready', error='control_channel_timeout',
        incoming=1, outgoing=2, read_bytes=4, pending_frame=False,
        read_fault=True, write_fault=False, local_drain=False)


def records(recorder):
    return json.loads(dict(recorder.preview().files)['diagnostics.json'])['events']


def test_first_fault_survives_cleanup_and_has_no_exception_text(tmp_path):
    owner = WorkflowWorkspaceOwner.__new__(WorkflowWorkspaceOwner)
    owner.session = SimpleNamespace(update_profile={'current_version': '0.8.0-alpha.1'})
    owner.selection = SimpleNamespace(workspace_root=tmp_path)
    owner.actor = SimpleNamespace(first_failure=snapshot())
    owner.diagnostics = owner.failure_reference = None
    owner._record_actor_failure(RuntimeError('synthetic secret path and prompt'))
    reference = owner.failure_reference
    assert reference
    # Physical cleanup removes the actor/selection; original diagnostics persist.
    owner.actor = owner.selection = None
    row, = records(owner.diagnostics)
    assert row['correlation'] == reference
    assert row['workflow'] == snapshot()
    assert 'synthetic secret' not in json.dumps(row)
    owner.diagnostics.close()
    reopened = Diagnostics(tmp_path, component=Component.MANAGER, build='0.8.0-alpha.1')
    try:
        assert records(reopened)[0]['workflow'] == snapshot()
    finally:
        reopened.close()


@pytest.mark.parametrize('key,value', [
    ('phase','private path'), ('phase',[]), ('error','synthetic prompt'),
    ('incoming',True), ('outgoing',-1), ('read_bytes',65541), ('read_fault',1),
    ('payload','not allowed')])
def test_unknown_or_unbounded_snapshot_is_rejected(tmp_path, key, value):
    value_map=snapshot(); value_map[key]=value
    assert not valid_workflow_failure(value_map)
    recorder=Diagnostics(tmp_path,component=Component.MANAGER,build='0.8.0-alpha.1')
    try:
        assert recorder.failure(RuntimeError('secret'),workflow=value_map) is None
        assert records(recorder)==[]
    finally:
        recorder.close()


def test_snapshot_copy_and_memory_evidence_survive_storage_failure(tmp_path):
    recorder=Diagnostics(tmp_path,component=Component.MANAGER,build='0.8.0-alpha.1')
    recorder._handler.failed=True
    value=snapshot()
    try:
        assert recorder.failure(RuntimeError('secret'),workflow=value)
        value['error']='private prompt'
        assert records(recorder)[0]['workflow']==snapshot()
    finally:
        recorder.close()
