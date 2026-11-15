"""Injected dispatch and real canonical evidence DTO; no server or persistence."""
from dataclasses import FrozenInstanceError,replace
import hashlib
import io
import json
from types import SimpleNamespace
import socket
import threading

from PIL import Image
import pytest

from core.ai_interrogation import (
    AIInterrogationService,InterrogationResult,result_to_evidence,hash_image,
)
from core.lmstudio_backend import (
    LMStudioContext,LMStudioDescriptionBackend,LMStudioBackendError,LMStudioBackendCancelled,
)
from core.lmstudio_description import encode_description,decode_description
from core.lmstudio_description_transport import DescriptionTransportError
from core.lmstudio_discovery import InstanceConfig


def generated(tmp_path):
    path=tmp_path/'generated.png'
    with Image.new('RGBA',(3,2),(20,40,60,128)) as image:image.save(path)
    return path


def context(**changes):
    values=dict(model_key='generated/model',expected_instance_id='generated-instance',
                prompt='Describe the generated image.')
    values.update(changes)
    return LMStudioContext(**values)


def answer(kwargs):
    inputs={key:value for key,value in kwargs.items() if key!='cancelled'}
    request=encode_description(**inputs)
    raw=json.dumps(dict(model_instance_id='generated-instance',
        output=[dict(type='message',content='A generated translucent image.')],
        stats=dict(input_tokens=10,total_output_tokens=5))).encode()
    return decode_description(raw,request)


def test_real_result_and_unreviewed_evidence_conversion_without_writes(tmp_path,monkeypatch):
    path=generated(tmp_path)
    sidecar=path.with_suffix('.tags.json');sidecar.write_bytes(b'generated-protected-evidence')
    txt=path.with_suffix('.txt');txt.write_bytes(b'generated-confirmed-caption')
    before={p:p.read_bytes() for p in [path,sidecar,txt]};calls=[]
    def no_network(*args,**kwargs):raise AssertionError('Unexpected network')
    monkeypatch.setattr(socket,'create_connection',no_network)
    def dispatch(**kwargs):
        calls.append(kwargs)
        with Image.open(io.BytesIO(kwargs['image_bytes'])) as prepared:
            assert prepared.mode=='RGB' and prepared.size==(3,2) and prepared.info=={}
        return answer(kwargs)
    backend=LMStudioDescriptionBackend(context(),dispatch=dispatch)
    result=backend.analyze(path)
    assert type(result) is InterrogationResult and result.tags==[] and result.kind=='plain_language'
    assert result.model_revision=='' and result.settings['server_version'] is None
    assert result.settings['effective_inference_settings']=='not_observed'
    assert result.raw_output['source_sha256']==hashlib.sha256(before[path]).hexdigest()
    assert result.raw_output['payload_sha256']==hashlib.sha256(calls[0]['image_bytes']).hexdigest()
    assert result.raw_output['metadata_copied'] is False
    evidence=result_to_evidence(path,result)
    assert evidence.image_hash==hash_image(path) and evidence.decisions=={}
    assert evidence.tags==[] and evidence.typed_plain_language()==result.plain_language
    assert evidence.model_revision=='' and evidence.raw_output==result.raw_output
    assert {p:p.read_bytes() for p in before}==before and set(tmp_path.iterdir())==set(before)
    assert len(calls)==1 and not any(key in calls[0] for key in ['endpoint','token','headers','body'])
    for name in ['release','bind_runtime_manager','bind_timing_store','known_facts']:
        assert not hasattr(backend,name)


def test_evidence_identity_matches_existing_cache_comparison_and_is_detached(tmp_path):
    path=generated(tmp_path);backend=LMStudioDescriptionBackend(context(),dispatch=lambda **kw:answer(kw))
    result=backend.analyze(path);evidence=result_to_evidence(path,result)
    record=SimpleNamespace(image_path=path,annotation=SimpleNamespace(ai_evidence=[evidence]))
    assert AIInterrogationService.matching_evidence(record,backend) is evidence
    identity=backend.evidence_identity();identity['settings']['requested_generation']['temperature']=1
    assert backend.evidence_identity()['settings']['requested_generation']['temperature']==0
    assert 'transport_statistics' not in result.settings and 'request_sha256' not in result.settings
    evidence.settings['provider']='another-provider'
    assert AIInterrogationService.matching_evidence(record,backend) is None


@pytest.mark.parametrize('changes',[dict(model_key=''),dict(prompt=''),dict(temperature=True),
    dict(model_revision='bad\n'),dict(server_version='x'*513),dict(observed_instance_config={}),
    dict(observed_instance_config=InstanceConfig(True,None,None,None,None,None))])
def test_invalid_context(changes):
    with pytest.raises(LMStudioBackendError,match='^invalid_context$'):context(**changes)


def test_context_known_observations_are_explicit_not_measured():
    value=context(model_revision='supplied-revision',quantization='supplied-Q4',server_version='supplied-version',
        observed_instance_config=InstanceConfig(4096,None,1,True,None,None))
    assert value.settings()['observed_instance_config']['context_length']==4096
    assert value.settings()['effective_inference_settings']=='not_observed'
    assert 'supplied' not in repr(value)
    with pytest.raises(FrozenInstanceError):value.prompt='changed'
    with pytest.raises(TypeError):LMStudioDescriptionBackend(value)
    with pytest.raises(LMStudioBackendError,match='^invalid_dispatch$'):
        LMStudioDescriptionBackend(value,dispatch=None)


@pytest.mark.parametrize('kind',['model','instance','request','text','statistics','type'])
def test_untrusted_callback_result_is_rejected_once(tmp_path,kind):
    path=generated(tmp_path);calls=[]
    def dispatch(**kwargs):
        calls.append(True);value=answer(kwargs)
        if kind=='model':return replace(value,model_key='other-model')
        if kind=='instance':return replace(value,instance_id='other-instance')
        if kind=='request':return replace(value,request_sha256='0'*64)
        if kind=='text':return replace(value,description=' ')
        if kind=='statistics':return replace(value,statistics=replace(value.statistics,input_tokens=True))
        return object()
    backend=LMStudioDescriptionBackend(context(),dispatch=dispatch)
    with pytest.raises(LMStudioBackendError) as error:backend.analyze(path)
    assert str(error.value)=='invalid_dispatch_result' and error.value.submission_state=='outcome_unknown'
    assert calls==[True] and not path.with_suffix('.tags.json').exists()


def test_changed_source_cannot_publish_result(tmp_path):
    path=generated(tmp_path);calls=[]
    def dispatch(**kwargs):
        calls.append(True);value=answer(kwargs)
        path.write_bytes(path.read_bytes()+b'generated-adversarial-change')
        return value
    with pytest.raises(LMStudioBackendError,match='^source_changed$') as error:
        LMStudioDescriptionBackend(context(),dispatch=dispatch).analyze(path)
    assert error.value.submission_state=='outcome_unknown' and calls==[True]


@pytest.mark.parametrize('code,state,cleanup',[
    ('invalid_request','not_sent',False),('description_io_timeout','outcome_unknown',True),
    ('description_cancelled','outcome_unknown',True)])
def test_transport_failure_disposition_and_cleanup_are_retained(tmp_path,code,state,cleanup):
    path=generated(tmp_path);calls=[]
    def dispatch(**kwargs):
        calls.append(True)
        raise DescriptionTransportError(code,state,cleanup_failed=cleanup)
    backend=LMStudioDescriptionBackend(context(),dispatch=dispatch)
    expected=LMStudioBackendCancelled if code=='description_cancelled' else LMStudioBackendError
    with pytest.raises(expected) as error:backend.analyze(path)
    assert error.value.submission_state==state and error.value.cleanup_failed==cleanup
    assert error.value.__context__ is None and calls==[True]


@pytest.mark.parametrize('phase',['before','after-dispatch'])
def test_cancellation_never_publishes_or_retries(tmp_path,phase):
    path=generated(tmp_path);cancel=threading.Event();calls=[]
    if phase=='before':cancel.set()
    def dispatch(**kwargs):
        calls.append(True);value=answer(kwargs);cancel.set();return value
    backend=LMStudioDescriptionBackend(context(),dispatch=dispatch)
    with pytest.raises(LMStudioBackendCancelled) as error:
        backend.analyze_with_cancellation(path,cancel.is_set)
    assert error.value.submission_state==('not_sent' if phase=='before' else 'outcome_unknown')
    assert len(calls)==(0 if phase=='before' else 1)


def test_callback_exception_is_content_free(tmp_path):
    path=generated(tmp_path);calls=[]
    def dispatch(**kwargs):calls.append(True);raise RuntimeError('generated-private-token-or-response')
    with pytest.raises(LMStudioBackendError) as error:
        LMStudioDescriptionBackend(context(),dispatch=dispatch).analyze(path)
    assert str(error.value)=='dispatch_failed' and error.value.__context__ is None
    assert error.value.submission_state=='outcome_unknown' and calls==[True]
