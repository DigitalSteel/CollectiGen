"""Original lease/readback with doubled native channel; no actor or signing."""
from types import SimpleNamespace
import pytest
from core.manager_launch_owner import DeploymentOwner, WindowsNormalTransport, WindowsControlChannel, LaunchHold
from suite_core.manager.bootstrap_entry import NormalOwnerRoute
from core.encyclopedia_connection import ReaderConnectionStore, root_reader_selection
from tests.test_encyclopedia_connection import package


@pytest.fixture
def original(tmp_path):
    root=tmp_path/'root';root.mkdir();owner=DeploymentOwner(root).acquire()
    owner.record['phase']='normal';owner._persist()
    api=object();counts=[1]
    transport=object.__new__(WindowsNormalTransport)
    transport.api=transport._creation_api=api;transport.job=transport._creation_job=91
    transport.job_active_count=lambda:counts[0]
    owner.native_transport=transport
    owner.children['manager']=SimpleNamespace(_handle=73,poll=lambda:None)
    channel=object.__new__(WindowsControlChannel)
    channel.transport=transport;channel.peer_handle=73
    channel.peer_witness=lambda:{'entry':'original'}
    channel.local_witness=lambda:{'launcher':'original'}
    route=object.__new__(NormalOwnerRoute)
    route.owner=owner;route.channel=channel;route.state='ready'
    route.wire=SimpleNamespace(document={'entry':{'entry':'original'},'launcher':{'launcher':'original'}},
        proof=SimpleNamespace(channel_epoch='a'*32))
    yield owner,route,counts
    owner.abandon()


def test_original_stage_scope_excludes_grants_seal_and_foreign_release(original):
    owner,route,counts=original
    scope=owner.component_stage_scope(route,'begin',None)
    assert scope['owner_generation']==owner.record['owner']
    assert owner.component_stage_scope(route,'check',scope['scope_id'])==scope
    with pytest.raises(LaunchHold,match='component_stage_in_progress'):owner.stop_helper_grants()
    with pytest.raises(LaunchHold,match='component_stage_in_progress'):
        owner.reserve_helper('owned-encyclopedia','b'*64,verify_entry=lambda:True)
    with pytest.raises(LaunchHold,match='exact_original'):owner.component_stage_scope(route,'release','c'*32)
    assert 'component_stage' in owner.record
    owner.component_stage_scope(route,'release',scope['scope_id'])
    assert 'component_stage' not in owner.record


def test_physical_member_or_original_lease_drift_retains_scope(original):
    owner,route,counts=original
    scope=owner.component_stage_scope(route,'begin',None)
    counts[0]=2
    with pytest.raises(LaunchHold,match='original_component_stage_peer'):owner.component_stage_scope(route,'check',scope['scope_id'])
    counts[0]=1;owner.marker.write_bytes(b'changed original lease marker')
    with pytest.raises(LaunchHold,match='owner_lease_or_marker_lost'):owner.component_stage_scope(route,'release',scope['scope_id'])
    assert 'component_stage' in owner.record


def test_reader_root_uses_saved_complete_inventory_and_rejects_change(original):
    owner,route,counts=original
    selected=package(owner.root/'reader');store=ReaderConnectionStore(owner.data_root/'settings.json')
    store.save(selected,expected=store.observe())
    selection=root_reader_selection(owner.root,store.settings.path)
    admitted=owner.admit_helper_wire_reader(selection)
    assert admitted.selection is selection and admitted.context==selection.context()
    selected.program.write_bytes(b'changed')
    with pytest.raises(ValueError):owner.admit_helper_wire_reader(selection)
