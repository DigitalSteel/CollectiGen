"""Closed request schema; input DTOs confer no Root or process authority."""
from copy import deepcopy
import pytest
from suite_core.manager.bootstrap_entry import check_legacy_reader_request


def request(operation='prepare'):
    return dict(schema=1,request_id='a'*32,profile_sha256='b'*64,
        operation=operation,handoff_id=None if operation=='prepare' else 'c'*64)


@pytest.mark.parametrize('operation',['prepare','commit','cancel','status'])
def test_closed_known_operations(operation):
    value=request(operation)
    assert check_legacy_reader_request(value) is value


@pytest.mark.parametrize('change',[
    {'schema':True},{'schema':2},{'request_id':'a'*31},{'request_id':'../arbitrary'},
    {'profile_sha256':'B'*64},{'operation':'launch'},{'operation':[]},
    {'program':'C:/arbitrary.exe'},{'argv':[]},{'handoff_id':'c'*64},
])
def test_foreign_or_extended_prepare_is_rejected(change):
    value=request();value.update(change)
    with pytest.raises(ValueError):check_legacy_reader_request(value)


@pytest.mark.parametrize('operation',['commit','cancel','status'])
def test_missing_prepared_identity_cannot_commit_cancel_or_query(operation):
    value=request(operation);value['handoff_id']=None
    with pytest.raises(ValueError):check_legacy_reader_request(value)
