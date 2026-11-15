"""Original service/writer/decision join in fresh generated component processes.

No application, real server, existing profile or model is executed. Public source
members are copied explicitly; no ignored/untracked checkout data is read.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.fixture(scope='module')
def selected_public_source(tmp_path_factory):
    source = Path(__file__).resolve().parents[1]
    manifest = json.loads((source/'public-source.json').read_bytes())
    target = tmp_path_factory.mktemp('selected-public-source')
    for name in sorted(set(manifest['files'])|{'public-source.json'}):
        relative = Path(name)
        assert not relative.is_absolute() and '..' not in relative.parts
        assert ':' not in name and '\\' not in name
        original = source/relative
        assert not original.is_symlink() and not (getattr(original.lstat(),'st_file_attributes',0)&0x400)
        raw = original.read_bytes()
        if name!='public-source.json':
            pin=manifest['files'][name]
            assert len(raw)==pin['size_bytes'] and hashlib.sha256(raw).hexdigest()==pin['sha256']
        destination=target/relative;destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_bytes(raw)
    return target


CHILD = r'''
import sys,json,hashlib,os
from pathlib import Path
program,case_root,case=Path(sys.argv[1]).resolve(),Path(sys.argv[2]).resolve(),sys.argv[3]
profile=case_root/'profile'
(profile/'data/defaults').mkdir(parents=True)
sys.path.insert(0,str(program))
allowed=(program,case_root,Path(sys.prefix).resolve(),Path(sys.base_prefix).resolve())
violations=[]
def beneath(path,root):return path==root or root in path.parents
def audit(event,args):
    if event in {'socket.connect','socket.bind','subprocess.Popen','os.system'}:
        violations.append(event);raise PermissionError('component_operation_refused')
    if event in {'open','os.listdir','os.scandir'} and args and isinstance(args[0],(str,bytes,os.PathLike)):
        path=Path(os.fsdecode(args[0])).absolute()
        if path==Path(os.devnull).absolute():return
        if not any(beneath(path,root) for root in allowed):
            violations.append(event);raise PermissionError('component_path_refused')
        if event=='open':
            mode=args[1] if len(args)>1 else None
            flags=args[2] if len(args)>2 else 0
            writes=(isinstance(mode,str) and any(c in mode for c in 'wax+')) or (
                isinstance(flags,int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND))
            if writes and not beneath(path,case_root):
                violations.append('foreign_write');raise PermissionError('component_write_refused')
sys.addaudithook(audit)
from core.application_profile import bind_profile_root
bind_profile_root(profile,program_root=program)
from core import paths
assert paths.APP_ROOT==profile
assert all(beneath(p,profile) for p in [paths.DATA_DIR,paths.DEFAULTS_DIR,paths.USER_DATA_DIR,paths.CACHE_DIR])
from core.automation_rules import DEFAULT_RULES,AUTOMATION_RULE_SCHEMA_VERSION
(paths.DEFAULTS_DIR/'automation_rules.json').write_text(json.dumps(dict(DEFAULT_RULES,schema_version=AUTOMATION_RULE_SCHEMA_VERSION)))
from PIL import Image,PngImagePlugin
from core.ai_interrogation import AIInterrogationService
from core.image_record import ImageRecord,ImageRecordStaleWriteError
from core.lmstudio_backend import LMStudioContext,LMStudioDescriptionBackend,LMStudioBackendCancelled
from core.lmstudio_description import encode_description,decode_description
from core.lmstudio_description_transport import DescriptionTransportError

image=case_root/'generated.png'
metadata=PngImagePlugin.PngInfo();metadata.add_text('parameters','generated-exact-raw-generator')
with Image.new('RGB',(3,2),(20,40,60)) as value:value.save(image,pnginfo=metadata)
record=ImageRecord(image)
record.annotation.plain_language=['An existing confirmed fixture sentence.']
record.annotation.source['generated_raw_evidence']={'raw':'generated-raw-payload','future_parameter':17}
record.annotation.extra_fields['future_fixture']={'nested':['unchanged',3]}
record.modified=True;record.save_sidecar(force=True)
sidecar=record.sidecar_path;caption=record.caption_path
caption.write_bytes(b'generated-exact-TXT-sentinel')
baseline={p:p.read_bytes() for p in [image,sidecar,caption]}
calls=[];progress=[];newer={}
description='A distinct synthetic sculpture stands outdoors.'
def dispatch(**kwargs):
    calls.append(True)
    if case=='unknown_failure' and len(calls)==1:
        raise DescriptionTransportError('description_io_timeout','outcome_unknown')
    if case in {'cancel_default','cancel_partial'}:
        raise DescriptionTransportError('description_cancelled','outcome_unknown')
    if case=='sidecar_race':
        concurrent=ImageRecord(image)
        concurrent.annotation.plain_language.append('A newer confirmed fixture sentence.')
        concurrent.modified=True;concurrent.save_sidecar(force=True)
        newer['bytes']=sidecar.read_bytes()
    request=encode_description(**{k:v for k,v in kwargs.items() if k!='cancelled'})
    raw=json.dumps(dict(model_instance_id='generated-instance',output=[dict(type='message',content=description)])).encode()
    return decode_description(raw,request)
context=LMStudioContext(model_key='generated/model',expected_instance_id='generated-instance',prompt='Describe this generated fixture.')
class Backend(LMStudioDescriptionBackend):
    def analyze_with_cancellation(self,path,cancelled):
        result=super().analyze_with_cancellation(path,cancelled)
        if case=='source_race':image.write_bytes(image.read_bytes()+b'generated-adversarial-change')
        return result
backend=Backend(context,dispatch=dispatch)
service=AIInterrogationService()
def run(**extra):
    return service.analyze_batch([image],backend,auto_merge=False,run_mode='manual',
        should_cancel=lambda:False,progress=lambda i,n,text:progress.append(text),**extra)
result={}
if case=='cancel_default':
    try:run()
    except LMStudioBackendCancelled as error:
        assert error.submission_state=='outcome_unknown'
        result['raised_submission_state']=error.submission_state
    else:raise AssertionError('Expected original cancellation exception')
    assert len(calls)==1 and sidecar.read_bytes()==baseline[sidecar]
elif case=='cancel_partial':
    batch=run(return_partial_on_cancel=True)
    assert batch.cancelled and batch.processed==0 and len(calls)==1
    assert sidecar.read_bytes()==baseline[sidecar]
    assert not hasattr(batch,'submission_state')
    result['partial_cancel_uncertainty_gap_retained']=True
elif case in {'sidecar_race','source_race'}:
    batch=run()
    assert batch.processed==0 and batch.errors and len(calls)==1
    assert not ImageRecord(image).annotation.ai_evidence
    assert sidecar.read_bytes()==(newer['bytes'] if case=='sidecar_race' else baseline[sidecar])
    result['newer_authority_preserved']=True
else:
    if case=='unknown_failure':
        failed=run()
        assert failed.processed==0 and failed.errors and len(calls)==1
        assert sidecar.read_bytes()==baseline[sidecar]
        result['no_automatic_retry_before_explicit_second_run']=True
    batch=run()
    assert batch.processed==1 and not batch.errors and batch.review_paths==[image]
    loaded=ImageRecord(image)
    assert len(loaded.annotation.ai_evidence)==1
    evidence=loaded.annotation.ai_evidence[0]
    assert evidence.kind=='plain_language' and evidence.plain_language==description and evidence.decisions=={}
    assert loaded.annotation.plain_language==['An existing confirmed fixture sentence.']
    assert loaded.annotation.source['generated_raw_evidence']==record.annotation.source['generated_raw_evidence']
    assert loaded.annotation.extra_fields['future_fixture']==record.annotation.extra_fields['future_fixture']
    evidence_id=evidence.id
    if case=='cache_reuse':
        saved=sidecar.read_bytes();count=len(calls);cached=run()
        assert cached.skipped_existing==1 and cached.processed==0 and len(calls)==count
        assert sidecar.read_bytes()==saved and ImageRecord(image).annotation.ai_evidence[0].id==evidence_id
        result['cache_reused_without_dispatch']=True
    elif case=='stale_acceptance':
        concurrent=ImageRecord(image)
        concurrent.annotation.plain_language.append('A newer confirmed fixture sentence.')
        concurrent.modified=True;concurrent.save_sidecar(force=True)
        saved=sidecar.read_bytes()
        try:service.accept_plain_language(loaded,evidence,description)
        except ImageRecordStaleWriteError:pass
        else:raise AssertionError('Expected original stale review refusal')
        assert sidecar.read_bytes()==saved and description in loaded.annotation.plain_language
        result['stale_review_newer_bytes_and_draft_preserved']=True
    elif case=='persist_accept':
        service.accept_plain_language(loaded,evidence,description)
        accepted=ImageRecord(image)
        assert description in accepted.annotation.plain_language
        assert 'An existing confirmed fixture sentence.' in accepted.annotation.plain_language
        assert accepted.annotation.ai_evidence[0].decisions['plain_language']=='accepted'
        assert accepted.annotation.source['generated_raw_evidence']==record.annotation.source['generated_raw_evidence']
        assert accepted.annotation.extra_fields['future_fixture']==record.annotation.extra_fields['future_fixture']
        result['persist_reload_accept_reload']=True
    assert len(calls)==(2 if case=='unknown_failure' else 1)
assert caption.read_bytes()==baseline[caption]
if case!='source_race':assert image.read_bytes()==baseline[image]
assert backend._runtime_progress is None
assert not violations,violations
result.update(case=case,passed=True,dispatch_count=len(calls),profile_paths_bound=True,
    component_only=True,source_sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
    sidecar_sha256=hashlib.sha256(sidecar.read_bytes()).hexdigest(),
    caption_sha256=hashlib.sha256(caption.read_bytes()).hexdigest(),
    evidence_count=len(ImageRecord(image).annotation.ai_evidence),
    generic_loading_phase_observed=any(text.startswith('Loading') for text in progress),
    actual_provider_or_server=False,blocked_operations=violations)
(case_root/'RESULT.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result))
'''


@pytest.mark.parametrize('case',['persist_accept','cache_reuse','unknown_failure',
    'cancel_default','cancel_partial','sidecar_race','source_race','stale_acceptance'])
def test_original_service_persist_review_join(selected_public_source,tmp_path,case):
    env={key:value for key,value in os.environ.items() if not key.startswith(
        ('TAG_MANAGER_UI_TEST','TAG_MANAGER_PYTEST'))}
    completed=subprocess.run([sys.executable,'-I','-B','-c',CHILD,
        str(selected_public_source),str(tmp_path),case],cwd=selected_public_source,
        env=env,capture_output=True,text=True,timeout=45,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    (tmp_path/'STDOUT.log').write_text(completed.stdout,encoding='utf-8')
    (tmp_path/'STDERR.log').write_text(completed.stderr,encoding='utf-8')
    assert completed.returncode==0,completed.stderr[-2000:]
    result=json.loads((tmp_path/'RESULT.json').read_bytes())
    assert result['passed'] and result['profile_paths_bound'] and not result['blocked_operations']
