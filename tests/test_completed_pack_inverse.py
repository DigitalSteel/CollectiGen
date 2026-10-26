"""Schema-7 backend qualification over generated enrolled native catalogs only."""
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest
import core.knowledge_pack as native
from core.catalog_admission import catalog_admission
from core.catalog_identity_codec import CatalogIdentity, encode_identity


@pytest.fixture
def imported(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path))
    spec = importlib.util.spec_from_file_location('inverse_fixture_helpers',
        Path(__file__).with_name('test_knowledge_pack_records.py'))
    helper = importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)
    source, root = helper._root(tmp_path/'source'), helper._root(tmp_path/'recipient')
    helper._write(source/'data/user/categories.json', {'schema_version':6,
        'categories':[{'id':'inverse_category','name':'Inverse category'}]})
    helper._write(source/'data/user/tags.json', {'schema_version':10,
        'tags':[{'id':'inverse_added','display':'Inverse addition','category':'inverse_category'}]})
    helper._write(root/'data/user/tags.json', {'schema_version':10,
        'tags':[{'id':'personal','display':'My retained tag','category':'colors',
                 'properties':{'kept':'yes'},'future_record':{'raw':[1,2]}}],
        'future_root':{'retain':True}})
    helper._write(root/'data/user/categories.json', {'schema_version':6,
        'categories':[{'id':'personal-category','name':'Personal category'}],
        'future_categories':{'retain':True}})
    (root/'data/user/catalog_identity.json').write_bytes(encode_identity(CatalogIdentity(
        '00000000-0000-0000-0000-000000000011','00000000-0000-0000-0000-000000000012')))
    pack=native.export_knowledge_pack(source,source/'user.zip')
    before=data_tree(root)
    plan=native.plan_knowledge_pack_import(root,pack); assert plan.can_apply
    result=native.import_knowledge_pack(root,pack,plan=plan)
    receipt=Path(result['backup']['path'])/'recovery.json'
    review=native.review_completed_knowledge_pack_import(root,receipt,
        catalog_identity_binding=plan.catalog_identity_binding)
    assert not (root/'synthetic-pack-undo-incarnation.json').exists()
    return root,receipt,review,before


def data_tree(root):
    return {p.relative_to(root).as_posix():p.read_bytes() for p in (root/'data').rglob('*.json')}


def evidence_tree(root):
    return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*')
            if p.is_file() and (p.suffix=='.json' or 'knowledge-pack-import' in p.parts)}


def recover(root,inverse,review):
    return native.recover_completed_knowledge_pack_inverse(root,inverse,
        catalog_identity_binding=review.catalog_identity_binding)


def interrupt(imported,monkeypatch):
    root,receipt,review,_before=imported
    original=native._atomic_bytes
    value=json.loads(receipt.read_text())
    targets={Path(row['target']) for row in value['files'] if row['expected_before']!=row['expected_after']}
    assert len(targets)>=2
    fired=[]
    def fail(path,payload):
        original(path,payload)
        if Path(path) in targets and not fired:
            fired.append(True)
            raise OSError('Injected interruption after first changed participant')
    with monkeypatch.context() as patch:
        patch.setattr(native,'_atomic_bytes',fail)
        with pytest.raises(native.CompletedPackInverseFailure) as error:
            native.undo_completed_knowledge_pack_import(root,review)
    assert fired
    inverse=error.value.receipt
    assert json.loads(inverse.read_text())['status']=='pending'
    current=[sha256(Path(row['target']).read_bytes()).hexdigest() if Path(row['target']).exists() else 'missing'
             for row in value['files'] if Path(row['target']) in targets]
    before_hashes={row['expected_before'] for row in value['files'] if Path(row['target']) in targets}
    after_hashes={row['expected_after'] for row in value['files'] if Path(row['target']) in targets}
    assert any(h in before_hashes for h in current) and any(h in after_hashes for h in current)
    return inverse


def test_native_inverse_exact_result_original_evidence_and_noop(imported):
    root,receipt,review,before=imported
    original={p:p.read_bytes() for p in receipt.parent.iterdir() if p.is_file()}
    inverse=native.undo_completed_knowledge_pack_import(root,review)
    value=json.loads(inverse.read_text())
    assert value['schema_version']==7 and value['status']=='restored'
    assert value['operation_kind']==value['identity_bound_operation']=='completed_pack_undo'
    assert data_tree(root)==before
    assert all(p.read_bytes()==b for p,b in original.items())
    snapshot=evidence_tree(root)
    assert recover(root,inverse,review) is False and evidence_tree(root)==snapshot
    with pytest.raises(RuntimeError):native.undo_completed_knowledge_pack_import(root,review)
    assert evidence_tree(root)==snapshot


def test_pending_inverse_holds_ordinary_and_old_forward_route(imported,monkeypatch):
    root,_receipt,review,before=imported
    inverse=interrupt(imported,monkeypatch); snapshot=evidence_tree(root)
    with pytest.raises(RuntimeError):
        with catalog_admission(root,native,role='ordinary'):pass
    with pytest.raises(RuntimeError):
        native.recover_selected_knowledge_pack_import(root,inverse,
            catalog_identity_binding=review.catalog_identity_binding)
    assert evidence_tree(root)==snapshot
    assert recover(root,inverse,review) is True and data_tree(root)==before


@pytest.mark.parametrize('case',['later_edit','header','defaults','source','history','foundation','schema6'])
def test_initial_refusal_never_allocates_inverse(imported,case):
    root,receipt,review,_before=imported
    value=json.loads(receipt.read_text())
    if case=='later_edit':
        target=root/'data/user/tags.json';target.write_bytes(target.read_bytes()+b' ')
    elif case=='header':
        target=root/'data/user/catalog_identity.json';target.write_bytes(target.read_bytes()+b' ')
    elif case=='defaults':
        name=next(n for n,h in value['default_authority_fingerprints'] if h!='missing')
        target=root/name;target.write_bytes(target.read_bytes()+b' ')
    elif case=='source':receipt.write_bytes(receipt.read_bytes()+b' ')
    elif case=='history':
        extra=receipt.parent.parent/'another'/'recovery.json';extra.parent.mkdir();extra.write_bytes(receipt.read_bytes())
    else:
        if case=='foundation':value['foundation_transition']={}
        else:value['schema_version']=6
        receipt.write_text(json.dumps(value),encoding='utf-8')
    snapshot=evidence_tree(root)
    with pytest.raises((RuntimeError,ValueError)):native.undo_completed_knowledge_pack_import(root,review)
    assert evidence_tree(root)==snapshot
    assert not tuple(receipt.parent.parent.glob('completed-pack-undo-*'))


@pytest.mark.parametrize('phase',['backup','before_pending','after_pending','final_before','final_silent','final_after'])
def test_publication_faults_expose_one_exact_inverse(imported,monkeypatch,phase):
    root,receipt,review,before=imported
    original_receipt=receipt.read_bytes(); after=data_tree(root)
    atomic_json,atomic_bytes=native._atomic_json,native._atomic_bytes
    fired=[]
    def json_fault(path,value):
        if value.get('operation_kind')=='completed_pack_undo':
            target=(phase in {'before_pending','after_pending'} and value['status']=='pending'
                    or phase.startswith('final_') and value['status']=='restored')
            if target and not fired:
                fired.append(True)
                if phase in {'after_pending','final_after'}:atomic_json(path,value)
                if phase=='final_silent':return
                raise OSError('Injected '+phase)
        return atomic_json(path,value)
    def backup_fault(path,payload):
        if Path(path).parent.name.startswith('completed-pack-undo-') and Path(path).name!='recovery.json' and not fired:
            fired.append(True);raise OSError('Injected backup preparation failure')
        return atomic_bytes(path,payload)
    with monkeypatch.context() as patch:
        patch.setattr(native,'_atomic_json',json_fault)
        if phase=='backup':patch.setattr(native,'_atomic_bytes',backup_fault)
        if phase=='final_after':
            inverse=native.undo_completed_knowledge_pack_import(root,review)
        else:
            with pytest.raises(native.CompletedPackInverseFailure) as error:
                native.undo_completed_knowledge_pack_import(root,review)
            inverse=error.value.receipt
    assert fired and receipt.read_bytes()==original_receipt
    assert len(tuple(receipt.parent.parent.glob('completed-pack-undo-*')))==1
    if phase in {'backup','before_pending'}:
        assert not inverse.exists() and data_tree(root)==after
    else:
        if phase=='after_pending':assert data_tree(root)==after
        if phase!='final_after':assert recover(root,inverse,review) is True
        assert data_tree(root)==before and json.loads(inverse.read_text())['status']=='restored'
        snapshot=evidence_tree(root);assert recover(root,inverse,review) is False
        assert evidence_tree(root)==snapshot


@pytest.mark.parametrize('case',['source','intent','backup','identity','defaults','history','missing','path','operation_id','epoch','schema6'])
def test_restart_tamper_and_later_changes_hold(imported,monkeypatch,case):
    root,receipt,review,_before=imported;inverse=interrupt(imported,monkeypatch)
    value=json.loads(inverse.read_text());selected=inverse
    if case=='source':receipt.write_bytes(receipt.read_bytes()+b' ')
    elif case=='backup':
        row=next(row for row in value['files'] if row['existed']);(inverse.parent/row['backup']).write_bytes(b'corrupt')
    elif case=='identity':
        path=root/'data/user/catalog_identity.json';path.write_bytes(path.read_bytes()+b' ')
    elif case=='defaults':
        name=next(n for n,h in value['default_authority_fingerprints'] if h!='missing');path=root/name;path.write_bytes(path.read_bytes()+b' ')
    elif case=='history':
        other=receipt.parent.parent/'unrelated'/'recovery.json';other.parent.mkdir();other.write_bytes(receipt.read_bytes())
    elif case=='missing':inverse.unlink()
    elif case=='path':selected=root/'recovery.json'
    else:
        if case=='intent':value['files']=list(reversed(value['files']))
        elif case=='operation_id':value['operation_id']='foreign'
        elif case=='epoch':value['catalog_receipt_epoch']='0'*64
        else:value['schema_version']=6
        inverse.write_text(json.dumps(value),encoding='utf-8')
    snapshot=evidence_tree(root)
    with pytest.raises((RuntimeError,ValueError,OSError)):recover(root,selected,review)
    assert evidence_tree(root)==snapshot


@pytest.mark.parametrize('reference',['same','separator','extra','copied_path','schema'])
def test_duplicate_source_is_held_even_if_after_bytes_reappear(imported,reference):
    root,receipt,review,_before=imported;after=data_tree(root)
    inverse=native.undo_completed_knowledge_pack_import(root,review)
    value=json.loads(inverse.read_text())
    if reference=='separator':value['undo_of']['receipt_relative']=value['undo_of']['receipt_relative'].replace('/','\\')
    elif reference=='extra':value['undo_of']['future']='unexpected authority field'
    elif reference=='copied_path':value['undo_of']['receipt_relative']='backups/knowledge-pack-import/copied/recovery.json'
    elif reference=='schema':value['schema_version']=6
    if reference!='same':inverse.write_text(json.dumps(value),encoding='utf-8')
    for relative,payload in after.items():
        target=root/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(payload)
    fresh=native.review_completed_knowledge_pack_import(root,receipt,catalog_identity_binding=review.catalog_identity_binding)
    snapshot=evidence_tree(root)
    with pytest.raises(RuntimeError):
        native.undo_completed_knowledge_pack_import(root,fresh)
    assert evidence_tree(root)==snapshot
    assert list(receipt.parent.parent.glob('completed-pack-undo-*/recovery.json'))==[inverse]


def test_nested_and_foreign_engine_admission_are_refused(imported):
    from core.catalog_admission import selected_completed_pack_inverse
    root,_receipt,review,_before=imported;snapshot=evidence_tree(root)
    with catalog_admission(root,native,role='ordinary'):
        with pytest.raises(RuntimeError):native.undo_completed_knowledge_pack_import(root,review)
    with pytest.raises(RuntimeError):
        with selected_completed_pack_inverse(root,object(),binding=review.catalog_identity_binding,review=review):pass
    assert evidence_tree(root)==snapshot


def cold_recover(root,inverse,review):
    source=Path(__file__).resolve().parents[1]
    code=r'''
import sys,json,os,importlib.abc
from pathlib import Path
source,root,receipt=map(Path,sys.argv[1:4]);fixture=root.parent.resolve()
sys.path.insert(0,str(source))
archives={Path(p).resolve() for p in sys.path if p.lower().endswith('.zip')}
class NoHost(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.startswith(('PySide6','shiboken6','qt_app','core.app_state')):raise AssertionError('Host import forbidden')
def local(value):return isinstance(value,int) or Path(value).resolve().is_relative_to(fixture)
def audit(event,args):
 if event.startswith('socket.') or event in {'os.system','subprocess.Popen'}:raise AssertionError('External operation forbidden')
 if event=='open' and not isinstance(args[0],int):
  mode,flags=args[1:3];writes=isinstance(mode,str) and any(c in mode for c in 'wax+') or isinstance(flags,int) and flags&(os.O_WRONLY|os.O_RDWR|os.O_CREAT)
  if writes and not local(args[0]):raise AssertionError('Nonfixture write forbidden')
  if Path(args[0]).suffix.lower() in {'.json','.zip','.db','.sqlite'} and not local(args[0]) and Path(args[0]).resolve() not in archives:raise AssertionError('Nonfixture data read forbidden')
 if event in {'os.mkdir','os.remove','os.rmdir','os.chmod'} and not local(args[0]):raise AssertionError('Nonfixture mutation forbidden')
 if event in {'os.rename','os.replace'} and not all(local(p) for p in args[:2]):raise AssertionError('Nonfixture relocation forbidden')
sys.meta_path.insert(0,NoHost());sys.addaudithook(audit)
import core.knowledge_pack as native
assert Path(native.__file__).resolve()==(source/'core/knowledge_pack.py').resolve()
binding=tuple(tuple(row) for row in json.loads(sys.argv[4]))
try:changed=native.recover_completed_knowledge_pack_inverse(root,receipt,catalog_identity_binding=binding)
except Exception as exc:
 print(json.dumps({'error':type(exc).__name__,'message':str(exc)}));sys.exit(3)
print(json.dumps({'changed':changed,'status':json.loads(receipt.read_text())['status']}))
'''
    result=subprocess.run([sys.executable,'-I','-B','-c',code,str(source),str(root),str(inverse),
        json.dumps(review.catalog_identity_binding)],capture_output=True,encoding='utf-8',timeout=45)
    return result


def test_fresh_process_resumes_exact_mixed_inverse_and_verifies_noop(imported,monkeypatch):
    root,receipt,review,before=imported;original=receipt.read_bytes()
    inverse=interrupt(imported,monkeypatch)
    result=cold_recover(root,inverse,review);assert result.returncode==0,result.stdout+result.stderr
    assert json.loads(result.stdout)['changed'] is True
    assert data_tree(root)==before and receipt.read_bytes()==original
    snapshot=evidence_tree(root)
    result=cold_recover(root,inverse,review);assert result.returncode==0,result.stdout+result.stderr
    assert json.loads(result.stdout)=={'changed':False,'status':'restored'}
    assert evidence_tree(root)==snapshot


def test_fresh_process_preserves_a_later_third_state(imported,monkeypatch):
    root,_receipt,review,_before=imported;inverse=interrupt(imported,monkeypatch)
    path=root/'data/user/tags.json';path.write_bytes(path.read_bytes()+b' ')
    snapshot=evidence_tree(root);result=cold_recover(root,inverse,review)
    assert result.returncode==3 and evidence_tree(root)==snapshot,result.stdout+result.stderr
    error=json.loads(result.stdout)
    assert error['error']=='RuntimeError' and 'newer changes' in error['message']
