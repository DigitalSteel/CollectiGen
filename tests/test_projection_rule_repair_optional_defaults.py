"""Optional legacy defaults must not disable the published user-rule repair route."""
import json
import pytest
from core import AutomationRuleRepository,TagDatabase
from core.projection_rule_repair import ProjectionRuleRepairService
from core.rule_intents import TRAINING_PROJECTION

def fixture_service(tmp_path,defaults=None):
    default_path=tmp_path/'defaults.json';user_path=tmp_path/'user.json';knowledge_path=tmp_path/'tags.json'
    if defaults is not None:default_path.write_bytes(defaults)
    payload={'variables':{'color':['blue','green']},'variable_overrides':{},'disabled_rules':[],
        'merge_rules':[{'remove':['panel','{color} panel'],'replace':'{color} panel','intent':TRAINING_PROJECTION},
            {'remove':['panel','striped panel','{color} panel'],'replace':'{color} striped panel','intent':TRAINING_PROJECTION,'future_field':{'keep':True}}],
        'combine_rules':[],'wildcard_rules':[],'ignore_rules':[],'forward_payload':{'keep':True}}
    user_path.write_text(json.dumps(payload,indent=2)+'\n')
    knowledge=TagDatabase(knowledge_path);knowledge.save()
    repository=AutomationRuleRepository(default_path,user_path)
    return ProjectionRuleRepairService(repository,knowledge,journal_path=tmp_path/'operation_journal.jsonl'),default_path,user_path,knowledge_path

def test_absent_optional_defaults_review_apply_and_exact_undo(tmp_path):
    service,defaults,user,knowledge=fixture_service(tmp_path)
    before=json.loads(user.read_bytes());knowledge_before=knowledge.read_bytes()
    review=service.review();assert len(review.candidates)==1
    assert review.authorities[0].source is None
    service.apply([review.candidates[0].id],review=review)
    after=json.loads(user.read_bytes());assert after!=before
    assert after['merge_rules'][1]['remove']==['striped panel','{color} panel']
    assert after['merge_rules'][1]['future_field']=={'keep':True}
    assert after['forward_payload']=={'keep':True}
    assert knowledge.read_bytes()==knowledge_before and not defaults.exists()
    service.undo_last()
    assert json.loads(user.read_bytes())==before
    assert knowledge.read_bytes()==knowledge_before and not defaults.exists()

@pytest.mark.parametrize('initially_present',[False,True])
def test_default_presence_change_after_review_refuses_before_writes(tmp_path,initially_present):
    service,defaults,user,knowledge=fixture_service(tmp_path,b'{}' if initially_present else None)
    review=service.review();before=(user.read_bytes(),knowledge.read_bytes())
    if initially_present:defaults.unlink()
    else:defaults.write_text('{}\n')
    with pytest.raises(RuntimeError,match='changed'):
        service.apply([review.candidates[0].id],review=review)
    assert (user.read_bytes(),knowledge.read_bytes())==before
    assert not (tmp_path/'operation_journal.jsonl').exists()

@pytest.mark.parametrize('required',['user','knowledge'])
def test_required_source_absence_remains_refused(tmp_path,required):
    service,_defaults,user,knowledge=fixture_service(tmp_path)
    missing=user if required=='user' else knowledge;other=knowledge if required=='user' else user
    before=other.read_bytes();missing.unlink()
    with pytest.raises(RuntimeError):service.review()
    assert other.read_bytes()==before and not missing.exists()

@pytest.mark.parametrize('raw',[b'',b'{"merge_rules":'])
def test_present_invalid_defaults_remain_refused(tmp_path,raw):
    with pytest.raises((RuntimeError,ValueError)):
        service,*_=fixture_service(tmp_path,raw);service.review()
