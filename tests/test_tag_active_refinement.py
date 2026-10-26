"""Synthetic controller evidence; no catalog, real image, diagnostic or inference access."""
from types import SimpleNamespace as NS, MethodType
from copy import deepcopy
from pathlib import Path
import pytest
from core.annotation import ImageAnnotation,ObjectAnnotation,ImageContextAnnotation
from core.tag_database import TagRecord
from core.tag_collection import TagCollection,normalize_tag
from core.knowledge_relationships import KnowledgeRelationship as R,RelationshipEndpoint as E
from core.confirmed_tag_identity import (ConfirmedTagIdentityEntry as Entry,ConfirmedTagIdentityPlan as IdentityPlan,
    ConfirmedTagIdentityResolution as Resolution,image_tag_occurrences,active_refinement_inputs,retired_refinement_hold)
from core.related_tag_suggestions import add_related_tag_to_destination
from core.tag_refinement_plan import plan_refinement


def native_fixture():
    records={i:TagRecord(i,v) for i,v in (('synthetic.hair','hand in hair'),
       ('synthetic.own','hand in own hair'),('synthetic.another',"hand in another's hair"))}
    graph=tuple(R('synthetic.link.'+i,'specificity',(E('tag',i),E('tag','synthetic.hair')))
                for i in ('synthetic.own','synthetic.another'))
    kg=NS(tags=records,removed_archive_for_tag=lambda _id:None)
    return kg,graph


@pytest.mark.parametrize('decision',['review','keep','skip','alias'])
def test_hair_ownership_never_inferred_or_alias(decision):
    kg,graph=native_fixture();inputs=active_refinement_inputs(kg,None,graph,'synthetic.hair')
    result=plan_refinement('hand in hair','synthetic.hair',('hand in hair',),**inputs,
                          scope_ref='object:a:tags',relationship_scope='object',retain_source=True,decision=decision)
    assert result.after==result.before
    assert {c.target_id for c in result.choices}=={'synthetic.own','synthetic.another'}


@pytest.mark.parametrize('selected',['synthetic.own','synthetic.another'])
def test_hair_explicit_one_choice_keeps_unknown_original_details(selected):
    kg,graph=native_fixture();inputs=active_refinement_inputs(kg,None,graph,'synthetic.hair')
    result=plan_refinement('hand in hair','synthetic.hair',('hand in hair','local fact'),**inputs,
       scope_ref='object:a:tags',relationship_scope='object',retain_source=True,
       decision='add_specific',selected_id=selected,reviewed=True)
    assert result.after==('hand in hair','local fact',kg.tags[selected].display)
    assert result.undo_values==result.before
    correction=plan_refinement('hand in hair','synthetic.hair',('hand in hair',),**inputs,
       scope_ref='object:a:tags',retain_source=True,decision='correct',selected_id=selected,reviewed=True)
    assert not correction.changed and correction.holds


def test_occurrences_do_not_merge_objects_or_contexts():
    annotation=ImageAnnotation('synthetic.png',objects=[ObjectAnnotation(1,id='a',tags=['hand in hair']),
        ObjectAnnotation(2,id='b',tags=['hand in hair'])],contexts=[ImageContextAnnotation('panel',id='c',tags=['hand in hair'])])
    before=annotation.to_dict();occurrences=image_tag_occurrences(annotation,'hand in hair')
    assert len(occurrences)==3 and len({o.scope_ref for o in occurrences})==3
    assert annotation.to_dict()==before
    add_related_tag_to_destination(annotation,display='hand in own hair',owner_kind='object',owner_id='a',owner_group='tags')
    assert annotation.objects[0].tags==['hand in hair','hand in own hair']
    assert annotation.objects[1].tags==['hand in hair'] and annotation.contexts[0].tags==['hand in hair']


@pytest.mark.parametrize('archive',[None,NS(relationships_before={}),
    NS(recovery_kinds={'old':'current_default'},relationships_before={'override':{}}),
    NS(extras={'complete':True,'schema_version':1,'provenance':'claimed'},relationships_before={'slot':{}})])
def test_recovery_records_and_unverified_complete_claims_do_not_authorize_historical_replacement(archive):
    assert retired_refinement_hold(NS(removed_archive_for_tag=lambda _:archive),'old')


def controller(monkeypatch,tmp_path,*,action='add_specific',selected='synthetic.own'):
    from qt_app import image_data_editor as module
    Editor=module.ImageDataEditor
    kg,graph=native_fixture()
    # This remains an injected component fixture, not native-store qualification.
    # Production capture is strict; the separate native tests use real stores.
    from core.relation_native_snapshot import NativeRefinementSnapshot,detach_live_inputs
    from core.relation_rule_service import FrozenPayload
    from dataclasses import asdict
    import json
    def component_capture(knowledge,categories,semantic,policies,current,*,source_id,source_value,extra_dependencies):
        inputs=active_refinement_inputs(knowledge,categories,semantic.effective_relationships,source_id)
        return NativeRefinementSnapshot(FrozenPayload.capture(dict(source_id=source_id,source_value=source_value,
            mode='injected_component_fixture',inputs=detach_live_inputs(inputs),image=current.to_dict(),
            occurrences=json.loads(json.dumps([asdict(row) for row in image_tag_occurrences(current,source_value)])),
            records={tid:knowledge.tags[tid].to_dict() for tid in inputs['displays']},
            graph=[row.to_dict() for row in semantic.effective_relationships],
            policies=[row.to_dict() for row in policies.records],extra_dependencies=extra_dependencies)))
    monkeypatch.setattr(module,'capture_live_refinement',component_capture)
    annotation=ImageAnnotation('synthetic.png',objects=[ObjectAnnotation(1,id='a',tags=['hand in hair']),
        ObjectAnnotation(2,id='b',tags=['hand in hair'])],source={'wd':{'raw_value':'hand_in_hair','score':0.91,'source':'synthetic-WD'}})
    editor=NS(knowledge=kg,categories=None,record=NS(image_path=tmp_path/'synthetic.png',annotation=annotation),
        current_tags=TagCollection(['hand in hair']),_working_tag_identities={'hand in hair':'synthetic.hair'},
        _edit_snapshot=NS(assess=lambda _:NS(stale=False)),_ai_busy=False,
        _pending_scoped_tag_additions={},_scoped_ingress_restore_states={},_tag_history=[],
        _approved_conflict_states=set(),_prepared_conflict_replacements=(),_prepared_absent_restores=set(),
        absent=set(),ignored=set(),warnings=[],reject=False,decision=True,messages=[])
    entry=Entry('hand in hair','hand in hair','known','synthetic.hair','hand in hair')
    def identity(values,**_):
        entries=[]
        for value in values:
            ids=[i for i,r in kg.tags.items() if normalize_tag(r.display)==normalize_tag(value)]
            entries.append(Entry(value,normalize_tag(value),'known' if len(ids)==1 else 'ambiguous',
                ids[0] if len(ids)==1 else '',value))
        return IdentityPlan(tuple(entries))
    def existing(values,**kwargs):
        plan=identity(values,**kwargs)
        return Resolution(tuple(e.canonical_display for e in plan.entries),tuple(e.stable_id for e in plan.entries),
            {e.normalized_value:e.stable_id for e in plan.entries})
    editor._identity_service=NS(plan=identity,require_existing=existing)
    editor._review_removed_identity_plan=lambda plan,**_: (plan,())
    editor._remember_identity_resolution=lambda resolution:editor._working_tag_identities.update(resolution.identity_by_value)
    editor._identity_plan_details=lambda _: 'synthetic ambiguous identity'
    editor._confirmed_absent_tags=lambda:editor.absent
    editor._ignored_tags=lambda:editor.ignored
    editor._confirm_restore_absent_tags=lambda _:False
    editor._confirm_restore_ignored_tags=lambda _:False
    editor._confirm_diagnostic_tag_conflicts=lambda _:editor.decision
    editor.automation_engine=NS(is_evidence_rejected=lambda _:editor.reject,conflict_warnings=lambda *_:editor.warnings)
    policy=NS(permits_related_review=True)
    editor._related_tag_service=NS(semantic_relationships=NS(_verify_authority=lambda:None,effective_relationships=graph),
        consumer_policies=NS(records=()),_policy_decision=lambda **_:policy)
    def current_annotation():
        current=deepcopy(annotation);current.tag_identities=dict(editor._working_tag_identities)
        for (kind,owner,group,_),value in editor._pending_scoped_tag_additions.items():
            add_related_tag_to_destination(current,display=value,owner_kind=kind,owner_id=owner,owner_group=group)
        return current
    editor._related_annotation_state=current_annotation
    editor._related_image_wide_tags=lambda annotation:list(annotation.sdxl_tags)
    editor._restore_ignore_for_add=lambda _: {}
    editor._undo_restored_ignore_for_add=lambda *_:None
    editor._sync_current_tags_from_objects=lambda:None
    editor._remember_recent_tags=lambda _:None
    editor._refresh_tag_lists=lambda **_:None;editor._refresh_suggestions=lambda:None
    for name in ('_active_refinement_snapshot','_review_active_tag_details','_stage_add_related_suggestions',
                 '_prepare_tag_additions','_revert_add_history_entry'):
        setattr(editor,name,MethodType(getattr(Editor,name),editor))
    class Dialog:
        def __init__(self,*args,occurrences=(),**kwargs):
            self.action=action;self.replacement_ids=(selected,);self.occurrence=NS(currentData=lambda:occurrences[0])
        def exec(self):return module.QDialog.Accepted
    monkeypatch.setattr(module,'RetiredImageTagDialog',Dialog)
    for name in ('warning','critical','information'):
        monkeypatch.setattr(module.QMessageBox,name,lambda *args:editor.messages.append(args[-1]))
    return editor,module


def test_connected_controller_native_stage_scope_undo_and_synthetic_reload(monkeypatch,tmp_path):
    editor,_=controller(monkeypatch,tmp_path);raw=deepcopy(editor.record.annotation.source)
    assert editor._review_active_tag_details('hand in hair')
    staged=editor._related_annotation_state()
    assert staged.objects[0].tags==['hand in hair','hand in own hair']
    assert staged.objects[1].tags==['hand in hair'] and staged.source==raw
    # Native schema round trip proves exact scopes/evidence; not full editor Save qualification.
    reloaded=ImageAnnotation.from_dict(staged.to_dict(),'synthetic.png')
    assert reloaded.objects[0].tags==staged.objects[0].tags and reloaded.source==raw
    editor._revert_add_history_entry(editor._tag_history.pop())
    assert editor._related_annotation_state().objects[0].tags==['hand in hair']
    assert editor.record.annotation.source==raw


def test_native_image_staging_and_sidecar_save_reload_keep_scope_and_evidence(monkeypatch,tmp_path):
    from qt_app.image_data_editor import ImageDataEditor as Editor
    from core.image_record import ImageRecord
    editor,_=controller(monkeypatch,tmp_path)
    editor._working_ownership_acknowledgements=[];editor._clear_legacy_character_assignments=False
    editor._working_characters=[];editor._working_objects=deepcopy(editor.record.annotation.objects)
    editor._working_contexts=[]
    editor._apply_retired_tag_replacements=lambda _:None
    editor._apply_character_ownership_placements=lambda _:None
    editor._related_annotation_state=MethodType(Editor._related_annotation_state,editor)
    assert editor._review_active_tag_details('hand in hair')
    staged=editor._related_annotation_state()
    record=ImageRecord(tmp_path/'synthetic.png',auto_load=False)
    record.image_path.write_bytes(b'synthetic fixture image; pixels are not consumed')
    record.annotation=staged;record.loaded=True;record.modified=True
    assert record.save_sidecar()
    reloaded=ImageRecord(record.image_path)
    assert reloaded.annotation.objects[0].tags==['hand in hair','hand in own hair']
    assert reloaded.annotation.objects[1].tags==['hand in hair']
    assert reloaded.annotation.source==editor.record.annotation.source
    editor._revert_add_history_entry(editor._tag_history.pop())
    assert editor._related_annotation_state().objects[0].tags==['hand in hair']


@pytest.mark.parametrize('action',['keep','skip','cancel'])
def test_controller_noop_decisions(monkeypatch,tmp_path,action):
    editor,_=controller(monkeypatch,tmp_path,action=action)
    assert not editor._review_active_tag_details('hand in hair')
    assert not editor._pending_scoped_tag_additions and not editor._tag_history


@pytest.mark.parametrize('boundary',['absent','ignored','reject','conflict','ambiguous','stale'])
def test_native_negative_conflict_ambiguous_and_stale_holds(monkeypatch,tmp_path,boundary):
    editor,_=controller(monkeypatch,tmp_path)
    if boundary in ('absent','ignored'):getattr(editor,boundary).add('hand in own hair')
    if boundary=='reject':editor.reject=True
    if boundary=='conflict':editor.warnings=['synthetic conflict'];editor.decision=False
    if boundary=='ambiguous':editor.knowledge.tags['synthetic.duplicate']=TagRecord('synthetic.duplicate','hand in hair')
    if boundary=='stale':editor._edit_snapshot.assess=lambda _:NS(stale=True)
    assert not editor._review_active_tag_details('hand in hair')
    assert not editor._pending_scoped_tag_additions and not editor._tag_history


def test_mutation_during_native_confirmation_invalidates_choice_before_staging(monkeypatch,tmp_path):
    editor,_=controller(monkeypatch,tmp_path);editor.warnings=['synthetic conflict']
    def change(_):editor.record.annotation.source['new_user_fact']='later edit';return True
    editor._confirm_diagnostic_tag_conflicts=change
    assert not editor._review_active_tag_details('hand in hair')
    assert not editor._pending_scoped_tag_additions and not editor._tag_history
    assert 'changed' in editor.messages[-1]


@pytest.mark.parametrize('boundary',['negative','target','graph','policy'])
def test_changed_authority_during_modal_review_is_held(monkeypatch,tmp_path,boundary):
    editor,_=controller(monkeypatch,tmp_path);editor.warnings=['synthetic conflict']
    def change(_):
        if boundary=='negative':editor.absent.add('hand in own hair')
        if boundary=='target':editor.knowledge.tags['synthetic.own'].display='later own hair wording'
        if boundary=='graph':editor._related_tag_service.semantic_relationships.effective_relationships=()
        if boundary=='policy':editor._related_tag_service._policy_decision=lambda **_:NS(permits_related_review=False)
        return True
    editor._confirm_diagnostic_tag_conflicts=change
    assert not editor._review_active_tag_details('hand in hair')
    assert not editor._pending_scoped_tag_additions and not editor._tag_history


def test_native_positive_modifier_slots_select_only_existing_colored_compound():
    kg,graph=native_fixture()
    kg.tags={i:TagRecord(i,label) for i,label in [('source','white footwear'),('base','footwear'),('boots','boots'),('compound','white boots')]}
    def slot(tid,label,base):
        return R('synthetic.slot.'+tid,'modifier_value',(E('tag',tid),E('modifier_value','white')),
            details={'modifier_dimension_id':'color','operation':'reviewed_shipped_default','base_tag_id':base,
                     'tag_name':label,'reviewed_at':'2026-10-05'})
    graph=(R('synthetic.boots','specificity',(E('tag','boots'),E('tag','base'))),slot('source','white footwear','base'),slot('compound','white boots','boots'))
    inputs=active_refinement_inputs(kg,None,graph,'source')
    result=plan_refinement('white footwear','source',('white footwear',),**inputs,scope_ref='object:a:tags',relationship_scope='object',retain_source=True,
        decision='add_specific',selected_id='boots',reviewed=True)
    assert result.after==('white footwear','white boots')
    kg.tags.pop('compound');inputs=active_refinement_inputs(kg,None,graph,'source')
    held=plan_refinement('white footwear','source',('white footwear',),**inputs,scope_ref='object:a:tags',retain_source=True,
        decision='add_specific',selected_id='boots',reviewed=True)
    assert not held.changed and held.holds==('modifier_preserving_candidate_missing',)


def test_active_row_with_retirement_tombstone_is_not_an_available_target():
    kg,graph=native_fixture();kg.removed_tag_ids={'synthetic.own'}
    inputs=active_refinement_inputs(kg,None,graph,'synthetic.hair')
    assert 'synthetic.own' not in inputs['displays']
    result=plan_refinement('hand in hair','synthetic.hair',('hand in hair',),**inputs,scope_ref='image::sdxl_tags',retain_source=True,
        decision='add_specific',selected_id='synthetic.own',reviewed=True)
    assert not result.changed and result.holds==('missing_candidate',)


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


def test_historical_caller_dispatch_holds_before_any_replacement(monkeypatch,tmp_path):
    editor,module=controller(monkeypatch,tmp_path)
    editor._retired_review_plan=lambda:IdentityPlan((Entry('old','old','tombstone','retired'),))
    assert not module.ImageDataEditor._stage_retired_tag_replacement(editor,Entry('old','old','tombstone','retired'),('synthetic.own',))
    assert not editor._pending_scoped_tag_additions and not editor._tag_history


_APP=None
def test_existing_dialog_visible_choices_exact_occurrence_and_keep(monkeypatch):
    global _APP
    from PySide6.QtWidgets import QApplication,QPushButton
    from PySide6.QtCore import Qt
    from qt_app.tag_identity_review import RetiredImageTagDialog
    _APP=QApplication.instance() or QApplication([])
    kg,graph=native_fixture();annotation=ImageAnnotation('synthetic.png',objects=[ObjectAnnotation(1,id='a',tags=['hand in hair']),ObjectAnnotation(2,id='b',tags=['hand in hair'])])
    occurrences=image_tag_occurrences(annotation,'hand in hair');inputs=active_refinement_inputs(kg,None,graph,'synthetic.hair')
    choices=plan_refinement('hand in hair','synthetic.hair',occurrences[0].values,**inputs,scope_ref=occurrences[0].scope_ref,retain_source=True).choices
    dialog=RetiredImageTagDialog(kg,None,Entry('hand in hair','hand in hair','known','synthetic.hair'),refinement_choices=choices,occurrences=occurrences)
    assert dialog.occurrence.currentIndex()==-1 and not dialog.replace_button.isEnabled()
    dialog.occurrence.setCurrentIndex(1);dialog.choices.topLevelItem(0).setCheckState(0,Qt.Checked)
    assert dialog.replace_button.isEnabled()
    dialog.choices.topLevelItem(1).setCheckState(0,Qt.Checked)
    assert not dialog.replace_button.isEnabled()
    next(b for b in dialog.findChildren(QPushButton) if b.text()=='Keep Original').click()
    assert dialog.action=='keep'
    dialog.close()


def test_retired_dialog_exposes_historical_hold_and_explicit_manual_choice():
    global _APP
    from PySide6.QtWidgets import QApplication,QPushButton
    from qt_app.tag_identity_review import RetiredImageTagDialog
    _APP=QApplication.instance() or QApplication([])
    kg,_=native_fixture()
    dialog=RetiredImageTagDialog(kg,None,Entry('retired source','retired source','tombstone','synthetic.retired'))
    assert dialog.choose_button.isEnabled() and not dialog.replace_button.isEnabled()
    assert dialog._historical_hold  # No automatic historical composition authority.
    next(b for b in dialog.findChildren(QPushButton) if b.text()=='Skip Review').click()
    assert dialog.action=='skip'
    dialog.close()
