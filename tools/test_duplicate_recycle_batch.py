from concurrent.futures import Future
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
from core.duplicate_recycle_batch import (
    apply_duplicate_recycle_batch,family_identities,missing_duplicate_recycle_outcome,
)
from core.verified_transfer import TransferRecoveryRequired


@pytest.mark.parametrize('case',[
    'success','before_start','cancel_after_first','authority_after_first',
    'authority_error','ambiguous_second','interrupted_second','recovery_second',
    'progress_after_first','progress_after_final','duplicate',
])
def test_exact_partition_and_final_future(case):
    records=[NS(identity='generated-'+str(i),path=Path('generated-'+str(i)+'.png')) for i in [1,2,3]]
    if case=='duplicate':records[1].identity=records[0].identity
    plans=tuple((record,record.identity) for record in records);calls=[];progress=[];future=Future()
    def checkpoint():return case!='before_start' and not(case=='cancel_after_first' and calls)
    def authority():
        if calls and case=='authority_error':raise OSError('Generated currentness unavailable')
        return not(case=='authority_after_first' and calls)
    def apply(identity):
        calls.append(identity)
        if identity=='generated-2':
            if case=='ambiguous_second':raise OSError('Generated ambiguous acknowledgement')
            if case=='interrupted_second':raise InterruptedError('Generated interruption inside family')
            if case=='recovery_second':raise TransferRecoveryRequired('Generated recovery required')
    def update(current,total,message):
        progress.append(current)
        if case=='progress_after_first' and current==1 or case=='progress_after_final' and current==3:raise OSError('Generated progress failure')
    outcome=apply_duplicate_recycle_batch(plans,apply=apply,checkpoint=checkpoint,authority_current=authority,progress=update,final_outcome=future)
    assert future.done() and future.result(timeout=0) is outcome
    known=[x.identity for x in outcome.moved];unknown=[x.family.identity for x in outcome.unconfirmed];pending=[x.identity for x in outcome.unattempted]
    if case in {'success','progress_after_final'}:assert known==['generated-1','generated-2','generated-3'] and not unknown and not pending
    elif case in {'before_start','duplicate'}:assert not calls and not known and not unknown and len(pending)==3
    elif case in {'cancel_after_first','authority_after_first','authority_error','progress_after_first'}:assert known==['generated-1'] and not unknown and pending==['generated-2','generated-3'] and outcome.stop_reason
    elif case=='ambiguous_second':assert known==['generated-1','generated-3'] and unknown==['generated-2'] and not pending
    else:assert known==['generated-1'] and unknown==['generated-2'] and pending==['generated-3'] and outcome.stop_reason
    assert outcome.recovery_required==(case=='recovery_second')
    assert len(calls)==len(set(calls))
    records[0].identity='changed-after-return';records[0].path=Path('changed-after-return.png')
    assert all(x.identity!='changed-after-return' and x.path!='changed-after-return.png' for x in (*outcome.moved,*outcome.unattempted))
    with pytest.raises(FrozenInstanceError):outcome.stop_reason='changed'


@pytest.mark.parametrize('invoked',[False,True])
def test_missing_receipt_does_not_manufacture_unchanged_evidence(invoked):
    families=family_identities(((NS(identity='generated',path=Path('generated.png')),None),))
    result=missing_duplicate_recycle_outcome(families,invoked=invoked,message='Generated missing accounting')
    assert not result.moved and len(result.unconfirmed)==int(invoked) and len(result.unattempted)==int(not invoked)
