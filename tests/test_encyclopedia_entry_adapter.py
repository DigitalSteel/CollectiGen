"""Source doubles only: no native admission, profile, channel or launch."""
from types import SimpleNamespace
import pytest
from core.encyclopedia_entry_adapter import EncyclopediaEntryUpdateCheck, EncyclopediaEntryRequest, entry_notice
from suite_core.encyclopedia.control_entry import EncyclopediaChildControl


def control_double():
    control=object.__new__(EncyclopediaChildControl)
    control.outstanding=None;control.outstanding_sent=False
    control.wire=SimpleNamespace(channel=SimpleNamespace(read_fault=None))
    return control


def available():
    return dict(status='available',reason='signed_release_available',version='0.2.0-alpha.1',
        manifest_sha256='a'*64,package_sha256='b'*64,notes='Reviewed release notes',route='manual')


def test_metadata_projection_never_constructs_offer_or_install_authority():
    notice=entry_notice(available())
    assert notice.version=='0.2.0-alpha.1' and not notice.production_install_authorized
    assert not hasattr(notice,'offer') and not hasattr(notice,'subject')


@pytest.mark.parametrize('mutation',['offer','hash','notes','version','route','future'])
def test_malformed_or_unknown_metadata_is_refused(mutation):
    value=available()
    if mutation=='hash':value['manifest_sha256']='bad'
    elif mutation=='notes':value['notes']='x'*32769
    elif mutation=='version':value['version']='invalid'
    elif mutation=='route':value['route']='automatic'
    else:value[mutation]={}
    with pytest.raises(ValueError):entry_notice(value)


def test_late_reply_resumes_original_id_without_resend():
    control=control_double();calls=[]
    def request(operation,body,**kwargs):
        calls.append(('send',operation))
        control.outstanding=dict(id='c'*32,operation=operation,body=body)
        control.outstanding_sent=True
        raise RuntimeError('synthetic pending timeout')
    def resume(**kwargs):
        calls.append(('resume',control.outstanding['id']))
        control.outstanding=None;control.outstanding_sent=False
        return dict(status='not_found',reason='no_newer_release')
    control.request=request;control.resume_outstanding=resume
    checker=EncyclopediaEntryUpdateCheck(control)
    with pytest.raises(RuntimeError):checker.check()
    assert checker.close_held
    assert checker.check().status=='not_found'
    assert calls==[('send','check'),('resume','c'*32)]
    assert not checker.close_held


def test_cancel_before_request_is_local_and_pending_request_must_settle():
    control=control_double();control.request=lambda *a,**k:pytest.fail('request sent')
    checker=EncyclopediaEntryUpdateCheck(control)
    assert checker.check(should_cancel=lambda:True).status=='held'
    control.outstanding=dict(id='d'*32,operation='check',body={});control.outstanding_sent=True
    control.resume_outstanding=lambda **k:dict(status='not_found',reason='late_reply')
    assert checker.check(should_cancel=lambda:True).reason=='late_reply'


@pytest.mark.parametrize('fault',['different_operation','send_unconfirmed','read_fault'])
def test_ambiguous_or_different_pending_work_holds_close(fault):
    control=control_double();control.outstanding=dict(id='e'*32,operation='check',body={})
    control.outstanding_sent=fault!='send_unconfirmed'
    if fault=='read_fault':control.wire.channel.read_fault='partial_frame'
    control.request=lambda *a,**k:pytest.fail('new close sent')
    checker=EncyclopediaEntryUpdateCheck(control)
    with pytest.raises(ValueError):checker.request_close()
    assert checker.close_held and not checker.close_admitted


def test_close_requires_exact_acknowledgement_and_is_idempotent():
    control=control_double();calls=[]
    control.request=lambda op,body,**kw:calls.append(op) or {'status':'close_requested'}
    checker=EncyclopediaEntryUpdateCheck(control)
    assert checker.request_close() and checker.request_close()
    assert calls==['close'] and checker.close_admitted


def test_wrong_control_and_wrong_acknowledgement_are_refused():
    with pytest.raises(ValueError):EncyclopediaEntryUpdateCheck(object())
    control=control_double();control.request=lambda *a,**k:{'status':'idle'}
    checker=EncyclopediaEntryUpdateCheck(control)
    with pytest.raises(ValueError):checker.request_close()
    assert not checker.close_admitted


def test_confirm_requires_exact_retained_notice_and_explicit_standalone_action():
    from dataclasses import replace
    control=control_double(); calls=[]
    value=available(); value['route']='standalone'
    def request(op,body,**kw):
        calls.append((op,body))
        return value if op=='check' else {'status':'waiting_for_close','request_sha256':'c'*64}
    control.request=request; checker=EncyclopediaEntryUpdateCheck(control)
    notice=checker.check(); assert notice.manifest_sha256=='a'*64 and notice.package_sha256=='b'*64
    with pytest.raises(ValueError): checker.confirm_update(notice)
    with pytest.raises(ValueError): checker.confirm_update(replace(notice),confirmed=True)
    pending=checker.confirm_update(notice,confirmed=True)
    assert pending is checker.pending_request and pending.request_sha256=='c'*64
    assert calls==[('check',{}),('confirm',{'manifest_sha256':'a'*64})]
    assert not checker.close_admitted  # Confirmation never silently closes/applies.


def test_status_retains_request_reference_and_held_reply_never_discards_it():
    control=control_double(); checker=EncyclopediaEntryUpdateCheck(control)
    value={'status':'waiting','phase':'applying','current_version':'0.2.0-alpha.2','request_sha256':'d'*64}
    control.request=lambda *a,**k:value
    status=checker.status(); pending=checker.pending_request
    assert status.request_sha256=='d'*64 and pending.status=='retained'
    checker.status(); assert checker.pending_request is pending
    value={'status':'held','reason':'operation_held','operation':'status'}
    assert checker.status().status=='held' and checker.pending_request is pending


def test_request_actions_use_original_pin_and_reject_copied_reference():
    from dataclasses import replace
    control=control_double(); checker=EncyclopediaEntryUpdateCheck(control)
    original=EncyclopediaEntryRequest('retained','e'*64); checker.pending_request=original
    calls=[]
    replies={'retry-connection':'save_pending','recover':'recovery_waiting_for_close','cancel':'cancelled_before_apply'}
    control.request=lambda op,body,**kw:calls.append((op,body)) or {'status':replies[op]}
    with pytest.raises(ValueError): checker.retry_connection(replace(original))
    with pytest.raises(ValueError): checker.recover_update(original)
    assert checker.retry_connection(original).status=='save_pending'
    assert checker.pending_request is original
    assert checker.recover_update(original,confirmed=True).status=='recovery_waiting_for_close'
    assert checker.cancel_update(original).status=='cancelled_before_apply' and checker.pending_request is None
    assert all(body=={'request_sha256':'e'*64} for op,body in calls)


def test_different_pending_confirmation_body_is_not_resent_or_resumed():
    control=control_double(); checker=EncyclopediaEntryUpdateCheck(control)
    value=available();value['route']='standalone';checker.retained_notice=entry_notice(value)
    control.outstanding={'id':'f'*32,'operation':'confirm','body':{'manifest_sha256':'0'*64}}
    control.outstanding_sent=True
    control.request=lambda *a,**k:pytest.fail('no resend');control.resume_outstanding=lambda **kw:pytest.fail('wrong body resumed')
    with pytest.raises(ValueError): checker.confirm_update(checker.retained_notice,confirmed=True)


@pytest.mark.parametrize('change',['pin','phase','version','extra'])
def test_invalid_status_does_not_replace_retained_request(change):
    control=control_double();checker=EncyclopediaEntryUpdateCheck(control)
    pending=EncyclopediaEntryRequest('retained','a'*64);checker.pending_request=pending
    value={'status':'waiting','phase':'applying','current_version':'0.2.0-alpha.2','request_sha256':'a'*64}
    if change=='pin':value['request_sha256']='bad'
    elif change=='phase':value['phase']=None
    elif change=='version':value['current_version']='bad'
    else:value['future']={}
    control.request=lambda *a,**k:value
    with pytest.raises(ValueError):checker.status()
    assert checker.pending_request is pending
