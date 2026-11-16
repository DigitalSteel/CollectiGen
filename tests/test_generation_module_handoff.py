"""Generated reviewed-model draft handoff; no model, GUI or generator execution."""
from types import SimpleNamespace as NS

import pytest

from core.generation_module_handoff import draft_descriptor, save_reviewed_handoff, read_reviewed_handoff
from core.generation_planning import GenerationPlan, save_plan


def reviewed():
    identity = 'sha256:' + 'a' * 64
    registry = NS(snapshot=lambda: ({'resources': {identity: {
        'declarations': [{'role': 'checkpoint', 'family': 'SDXL'}], 'lineage': [],
        'locations': [], 'user': {'display_name': 'Generated exact checkpoint'}}}}, 'r1'))
    guidance = NS(request=NS(workflow_kind='generation', base_model_id=identity,
        resource_bindings=(), goal='Reviewed generated still life'), companion_requirements=(),
        source_urls=('https://example.invalid/generated-model',), record_id='generated-review')
    config = NS(request_changes=lambda: {'seed': 6237327949615356897, 'steps': 12},
        recipe_id='generated-recipe', recipe_version=1, target_model_family='SDXL', target_model_version='1')
    return identity, registry, guidance, config


def test_saved_handoff_reopens_exact_model_settings_and_does_not_overwrite_other_draft(tmp_path):
    root = tmp_path / 'Manager'
    existing = root / 'generation/drafts' / ('0' * 32 + '.json')
    save_plan(existing, GenerationPlan.create(None, goal='Keep the existing request'))
    previous = existing.read_bytes()
    identity, registry, guidance, config = reviewed()
    selected = save_reviewed_handoff(root, registry, guidance, config, expected_registry_revision='r1')
    plan, path, token = read_reviewed_handoff(root, selected)
    assert path != existing and existing.read_bytes() == previous
    assert token == selected['sha256']
    assert plan.request.seed == 6237327949615356897
    assert plan.goal == guidance.request.goal and plan.request.steps == 12
    assert [(item.id, item.role) for item in plan.request.resources] == [(identity, 'checkpoint')]
    assert plan.target_context['model_guidance']['execution_readiness'] == 'preflight_required'
    assert plan.target_context['resource_display_labels'][0]['label'] == 'Generated exact checkpoint'
    path.write_bytes(b'changed after authenticated offer')
    with pytest.raises((ValueError, RuntimeError)):
        read_reviewed_handoff(root, selected)


def test_stale_registry_or_catalog_does_not_publish_handoff(tmp_path):
    _identity, registry, guidance, config = reviewed()
    with pytest.raises(ValueError, match='Registered models changed'):
        save_reviewed_handoff(tmp_path, registry, guidance, config, expected_registry_revision='stale')
    def changed():
        raise ValueError('catalog changed')
    with pytest.raises(ValueError, match='catalog changed'):
        save_reviewed_handoff(tmp_path, registry, guidance, config,
            expected_registry_revision='r1', check=changed)
    assert not (tmp_path / 'generation').exists()


@pytest.mark.parametrize('path', ['../escape', '/absolute', 'generation/drafts/../other.json',
                                  'generation/results/' + 'a' * 32 + '.json', 'Data/private.json'])
def test_handoff_locator_cannot_choose_other_workspace_material(path):
    with pytest.raises(ValueError, match='exact_saved_generation_handoff_required'):
        draft_descriptor({'relative_path': path, 'sha256': 'a' * 64})
