"""Unfinished setup choices survive saved return; synthetic data, no analysis or models."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from copy import deepcopy
import json
from types import SimpleNamespace as NS

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from core.annotation import GenerationRecord, GenerationResource
from core.generation_setup_review_draft import validate_review_snapshot, ReviewSnapshotError
from core.settings import AppSettings
from core.ai_interrogation import JOYCAPTION_MODEL
from qt_app.generation_image_setup_dialog import ImageGenerationSetupDialog


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def record():
    return GenerationRecord(id='synthetic-evidence', seed=2**80 + 17, positive_prompt='Recorded scene',
        resources=[GenerationResource(id='checkpoint', role='checkpoint', name='Recorded checkpoint',
            hashes={'sha256': 'a' * 64}),
            GenerationResource(id='lora', role='lora', name='Recorded LoRA', hashes={'sha256': 'b' * 64},
                weights={'model': 0.123456789012345, 'text_encoder': 2**60 + 3},
                extra_fields={'future_resource': {'exact': 2**90 + 11}})],
        raw={'unrecognized_generator_blob': {'seed': 2**100 + 31}},
        extra_fields={'future_record': ['retained', 2**95 + 9]})


def dialog(*, saved=None, current=None, registry=None, provider=None):
    evidence = record().to_dict() if saved is None else saved['records'][0]
    fragments = [{'kind': 'Description', 'text': 'Original reviewed words', 'reviewed': True,
                  'future_fragment': {'keep': 2**74 + 3}}] if saved is None else saved['fragments']
    identity = {'collection': [{'image_sha256': 'c' * 64}], 'future_source': 'preserved'} if saved is None else saved['source_identity']
    settings = AppSettings()
    settings.plain_language_model = JOYCAPTION_MODEL
    return ImageGenerationSetupDialog([GenerationRecord.from_dict(evidence)], fragments,
        settings=settings, registry=registry, source_identity=identity,
        source_current_provider=current or (lambda: deepcopy(identity)), suggestion_provider=provider)


def author_choices(view):
    view.name.setText('An unfinished named setup')
    view.text_tree.topLevelItem(0).setCheckState(0, Qt.Unchecked)
    view.text_tree.setCurrentItem(view.text_tree.topLevelItem(0))
    view.part_fields['scene'].setText('An edited copy, independently authored')
    view.part_checks['scene'].setChecked(True)
    view.part_fields['style'].setText('Pending style text that is not selected')
    view.part_checks['style'].setChecked(False)
    view.saved_text_status.setText('Edited copy retained; original text is unchanged.')
    view.saved_text_status.show()
    view.include_model.setChecked(True)
    view.include_prompt.setChecked(True)
    view.lora_tree.topLevelItem(0).setCheckState(0, Qt.Unchecked)
    change, value = view._lora_strength_controls[1]['model']
    value.setValue(0.654321); change.setChecked(True)
    other_change, other_value = view._lora_strength_controls[1]['text_encoder']
    other_value.setValue(-0.25); other_change.setChecked(False)
    view.recorded_resource_tree.topLevelItem(0).setCheckState(0, Qt.Unchecked)
    chosen = {'id': 'sha256:' + 'd' * 64, 'role': 'lora', 'name': 'Registered choice',
        'hashes': {'sha256': 'd' * 64}, 'weights': {'model': 1, 'text_encoder': 1},
        'future_registered': {'exact': 2**89 + 3}}
    view._append_resource_choice(chosen, checked=False, weights={'model': 1.25, 'text_encoder': -0.5})
    view._suggestion_candidates = [
        {'kind': 'scene', 'target': 'style', 'text': 'First pending proposal',
         'source_label': 'Synthetic uncertain proposal', 'model_id': 'synthetic-caption-model'},
        {'kind': 'other', 'target': 'pose', 'text': 'Second unselected proposal',
         'source_label': 'Synthetic uncertain proposal', 'model_id': 'synthetic-caption-model',
         'future_proposal': {'keep': True}}]
    view.suggestion_choice.addItems(['First uncertain proposal', 'Second uncertain proposal'])
    view.suggestion_choice.setCurrentIndex(1)
    view.suggestion_text.setPlainText('Edited pending answer, still not used')
    view.suggestion_target.setCurrentIndex(view.suggestion_target.findData('character'))
    view.suggestion_goal.setCurrentIndex(view.suggestion_goal.findData(('scene',)))
    view.manual_parts_toggle.setChecked(True)
    view.resource_toggle.setChecked(True)
    view.lora_toggle.setChecked(True)
    view.task_tabs.setCurrentIndex(1)


def test_roundtrip_keeps_unselected_pending_choices_unknown_evidence_and_authored_weights(app):
    calls = []
    source = dialog(provider=lambda *args, **kwargs: calls.append('analysis'))
    author_choices(source)
    snapshot = source.unfinished_review_snapshot()
    snapshot['future_snapshot'] = {'keep': 2**110 + 5}
    snapshot['state']['future_ui'] = {'keep': True}
    snapshot['state']['lora_rows'][0]['future_control'] = {'keep': 'unselected'}
    encoded = json.loads(json.dumps(snapshot))
    restored = dialog(saved=encoded, provider=lambda *args, **kwargs: calls.append('analysis'))
    try:
        assert restored.restore_unfinished_review_snapshot(encoded)
        after = restored.unfinished_review_snapshot()
        assert after['records'] == snapshot['records']
        assert type(after['records'][0]['seed']) is int and after['records'][0]['seed'] == 2**80 + 17
        assert after['records'][0]['resources'][1]['weights']['text_encoder'] == 2**60 + 3
        for key in ('name', 'record_index', 'text_rows', 'text_index', 'parts', 'include',
                    'lora_rows', 'recorded_rows', 'chosen_resources', 'suggestions', 'suggestion_index',
                    'suggestion_goal', 'tab', 'disclosures', 'saved_text_status', 'saved_text_status_visible'):
            assert after['state'][key] == snapshot['state'][key], key
        assert after['future_snapshot'] == snapshot['future_snapshot']
        assert after['state']['future_ui'] == snapshot['state']['future_ui']
        assert restored.part_fields['style'].text() == 'Pending style text that is not selected'
        assert not restored.part_checks['style'].isChecked()
        assert restored.recipe is None and not restored.save_button.isEnabled()
        assert calls == [] and restored._suggestion_task is None
    finally:
        source.reject(); restored.reject()


def test_cancel_and_failed_validation_leave_dirty_review_intact(app):
    source = dialog(); author_choices(source)
    before = source.unfinished_review_snapshot()
    for change in ('schema', 'weight', 'precision', 'constructor', 'choice_label'):
        invalid = deepcopy(before)
        if change == 'schema': invalid['schema_version'] = True
        elif change == 'weight': invalid['state']['lora_rows'][0]['strengths']['model']['value'] = 11
        elif change == 'precision': invalid['state']['lora_rows'][0]['strengths']['model']['value'] = 0.1234567
        elif change == 'constructor': invalid['records'][0]['raw']['different'] = 'different evidence'
        else: invalid['state']['suggestions'][0]['choice_label'] = {'not': 'text'}
        with pytest.raises(ValueError):
            source.restore_unfinished_review_snapshot(invalid)
        assert source.unfinished_review_snapshot() == before
    source.reject()
    # Cancel closes only the editor. The retained JSON and raw evidence stay exact.
    assert validate_review_snapshot(before) == before
    assert before['state']['parts']['scene']['checked']
    assert before['state']['suggestions'][1]['text'] == 'Edited pending answer, still not used'


@pytest.mark.parametrize('exact', [2**89 + 3, -(2**89 + 3)])
def test_registered_choice_keeps_raw_json_opaque_and_returns_independent_copies(app, monkeypatch, exact):
    source = dialog(registry=object())
    raw = {'id': 'sha256:' + 'd' * 64, 'role': 'lora', 'name': 'Exact future evidence',
           'weights': {'model': 1, 'text_encoder': 1},
           'future': {'nested': [exact, {'exact': exact}], 'boolean': True, 'null': None}}
    expected = deepcopy(raw)
    try:
        source._append_resource_choice(raw)
        raw['future']['nested'][0] = 0
        selected = source._chosen_registered_resources()[0]
        assert selected['future'] == expected['future']
        assert type(selected['future']['nested'][0]) is int
        selected['future']['nested'][0] = 1
        snapshot = source.unfinished_review_snapshot()
        assert snapshot['state']['chosen_resources'][0]['resource'] == expected
        # Choosing this ID again replaces its original row; duplicate matching
        # must consume the opaque Python payload too, without Qt conversion.
        monkeypatch.setattr('qt_app.generation_manual_setup_dialog.choose_registered_setup_resource',
                            lambda *args, **kwargs: deepcopy(expected))
        source._choose_resource('lora')
        assert source.chosen_resource_tree.topLevelItemCount() == 1
        assert source._chosen_registered_resources()[0]['future'] == expected['future']
    finally:
        source.reject()


def test_reviewed_proposal_survives_as_evidence_without_automatic_acceptance(app):
    source = dialog()
    source.record_choice.setCurrentIndex(0)
    source.name.setText('Reviewed manual choices')
    source.part_fields['scene'].setText('An explicitly selected authored scene')
    source.part_checks['scene'].setChecked(True)
    source.review()
    assert source.recipe is not None
    source.exact_toggle.setChecked(True)
    snapshot = source.unfinished_review_snapshot()
    restored = dialog(saved=snapshot)
    try:
        assert restored.restore_unfinished_review_snapshot(snapshot)
        assert restored.recipe is None and not restored.save_button.isEnabled()
        assert restored.exact.toPlainText() == snapshot['state']['exact_text']
        assert restored.exact_toggle.isChecked()
        assert restored.unfinished_review_snapshot()['state']['reviewed_proposal'] == snapshot['state']['reviewed_proposal']
    finally:
        source.reject(); restored.reject()


def test_suggestion_and_save_must_settle_before_snapshot_or_restore(app):
    source = dialog(); author_choices(source)
    before = source.unfinished_review_snapshot()
    for attribute, busy in (('_suggestion_task', object()), ('save_in_progress', True)):
        original = getattr(source, attribute); setattr(source, attribute, busy)
        try:
            with pytest.raises(ValueError, match='Finish the current'):
                source.unfinished_review_snapshot()
            with pytest.raises(ValueError, match='Finish the current'):
                source.restore_unfinished_review_snapshot(before)
            assert source.name.text() == before['state']['name']
        finally:
            setattr(source, attribute, original)
    assert source.unfinished_review_snapshot() == before
    source.reject()


def test_preview_must_settle_for_capture_but_restore_reuses_original_preview(app):
    source = dialog(); author_choices(source)
    before = source.unfinished_review_snapshot()
    preview = object()
    source._source_preview_task = preview
    try:
        with pytest.raises(ValueError, match='Finish the current'):
            source.unfinished_review_snapshot()
        # Reconstructed hosts start their own preview before restoring choices.
        # It is retained; restoring JSON neither replaces nor restarts its work.
        assert source.restore_unfinished_review_snapshot(before)
        assert source._source_preview_task is preview
        assert source.name.text() == before['state']['name']
        assert source.part_fields['scene'].text() == before['state']['parts']['scene']['text']
        assert source._suggestion_task is None and source.recipe is None
        assert not source.save_button.isEnabled()
    finally:
        source._source_preview_task = None
        source.reject()


def test_stale_source_restore_keeps_choices_but_holds_suggestion_and_save(app):
    source = dialog(); author_choices(source)
    snapshot = source.unfinished_review_snapshot()
    calls = []
    restored = dialog(saved=snapshot, current=lambda: {'changed': True},
                      provider=lambda *args, **kwargs: calls.append('analysis'))
    try:
        assert restored.restore_unfinished_review_snapshot(snapshot) is False
        assert 'selected image changed' in restored.suggestion_status.text().lower()
        assert not restored.suggest_button.isEnabled() and not restored.save_button.isEnabled()
        assert restored.name.text() == snapshot['state']['name']
        assert restored._suggestion_candidates[1]['text'] == 'Edited pending answer, still not used'
        assert restored.records[0].to_dict() == snapshot['records'][0]
        assert calls == []
    finally:
        source.reject(); restored.reject()


@pytest.mark.parametrize('changed', [False, True])
def test_missing_or_changed_registered_choice_stays_exact_and_cannot_be_reviewed_as_another_id(app, changed):
    source = dialog(); author_choices(source)
    source.chosen_resource_tree.topLevelItem(0).setCheckState(0, Qt.Checked)
    snapshot = source.unfinished_review_snapshot()
    selected_id = snapshot['state']['chosen_resources'][0]['resource']['id']
    records = ({selected_id: {'identity': {'resource_id': 'sha256:' + 'e' * 64},
                              'declarations': [{'role': 'lora'}]}} if changed else {})
    registry = NS(snapshot=lambda: ({'resources': records}, 'synthetic-revision'))
    restored = dialog(saved=snapshot, registry=registry)
    try:
        assert restored.restore_unfinished_review_snapshot(snapshot) is False
        assert 'retained' in restored.review_status.text() and 'unavailable or changed' in restored.review_status.text()
        restored.review()
        assert restored.recipe is None and not restored.save_button.isEnabled()
        row = restored.unfinished_review_snapshot()['state']['chosen_resources'][0]
        assert row == snapshot['state']['chosen_resources'][0]
        assert row['resource']['id'] == selected_id
        assert row['weights'] == {'model': 1.25, 'text_encoder': -0.5}
    finally:
        source.reject(); restored.reject()


@pytest.mark.parametrize('payload', [b'pixels', lambda: None, float('nan'), {'provider': object()}])
def test_non_json_runtime_state_is_refused_without_coercion(payload):
    with pytest.raises((ReviewSnapshotError, ValueError)):
        validate_review_snapshot(payload)
