"""Accepted native/store serialization regression; disposable records only."""
from types import SimpleNamespace as NS
import pytest
from core.tag_database import TagRecord
from core.knowledge_relationships import KnowledgeRelationship as R, RelationshipEndpoint as E
from core.confirmed_tag_identity import active_refinement_inputs
from core.tag_refinement_plan import plan_refinement

def native_fixture():
    records={i:TagRecord(i,v) for i,v in (('synthetic.hair','hand in hair'),
       ('synthetic.own','hand in own hair'),('synthetic.another',"hand in another's hair"))}
    graph=tuple(R('synthetic.link.'+i,'specificity',(E('tag',i),E('tag','synthetic.hair')))
                for i in ('synthetic.own','synthetic.another'))
    kg=NS(tags=records,removed_archive_for_tag=lambda _id:None)
    return kg,graph


@pytest.mark.parametrize('loaded_via',['roundtrip','store'])
@pytest.mark.parametrize('case',['positive','missing','ambiguous'])
def test_serialized_native_modifier_refinement_preserves_color_or_holds(tmp_path,loaded_via,case):
    import json
    from core.relationship_store import RelationshipStore
    kg,_=native_fixture()
    kg.tags={i:TagRecord(i,label) for i,label in (
        ('source','white footwear'),('base','footwear'),('boots','boots'),('compound','white boots'))}
    def slot(tid,label,base):
        return R('synthetic.slot.'+tid,'modifier_value',(E('tag',tid),E('modifier_value','white')),
            details=tuple({'modifier_dimension_id':'color','operation':'reviewed_shipped_default',
                'base_tag_id':base,'tag_name':label,'reviewed_at':'2026-10-05'}.items()))
    graph=[R('synthetic.boots','specificity',(E('tag','boots'),E('tag','base'))),
        slot('source','white footwear','base'),slot('compound','white boots','boots')]
    if case=='missing':kg.tags.pop('compound')
    if case=='ambiguous':
        kg.tags['other-compound']=TagRecord('other-compound','other white boots')
        graph.append(slot('other-compound','other white boots','boots'))
    raw=json.dumps({'schema_version':1,'relationships':[r.to_dict() for r in graph],
                    'future_root':{'preserve':True}}).encode('utf-8')
    path=tmp_path/'synthetic-relationships.json';path.write_bytes(raw)
    if loaded_via=='store':
        store=RelationshipStore(path)
        graph=tuple(store.relationships.values())
        assert store.extras['future_root']=={'preserve':True}
    else:
        graph=tuple(R.from_dict(row) for row in json.loads(raw)['relationships'])
    assert all(type(r.details) is tuple for r in graph)
    inputs=active_refinement_inputs(kg,None,graph,'source')
    plan=plan_refinement('white footwear','source',('white footwear','local fact'),**inputs,
        scope_ref='object:a:tags',relationship_scope='object',retain_source=True,
        decision='add_specific',selected_id='boots',reviewed=True)
    if case=='positive':
        assert plan.after==('white footwear','local fact','white boots') and not plan.holds
        assert plan.choices[0].target_id=='compound'
    else:
        assert plan.after==plan.before
        assert plan.holds==(('modifier_preserving_candidate_missing',) if case=='missing'
                           else ('ambiguous_modifier_preserving_candidates',))
    assert plan.undo_values==plan.before
    assert path.read_bytes()==raw
