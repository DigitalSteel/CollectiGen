"""Reviewed Models-to-Generate handoff through the existing guarded draft store."""
from pathlib import Path, PurePosixPath
import re

from core.generation_model_guidance import apply_reviewed_model_guidance
from core.generation_planning import GenerationPlan, load_plan_with_digest, save_plan
from core.generation_saved_work import new_draft_path


def draft_descriptor(value):
    """Validate a locator only. Authenticated workspace admission grants access."""
    if (type(value) is not dict or not {'relative_path', 'sha256'} <= set(value)
            <= {'relative_path', 'sha256', 'resume_image_review'}
            or type(value['relative_path']) is not str
            or not re.fullmatch(r'generation/drafts/[0-9a-f]{32}\.json', value['relative_path'])
            or type(value['sha256']) is not str or not re.fullmatch('[0-9a-f]{64}', value['sha256'])):
        raise ValueError('exact_saved_generation_handoff_required')
    if 'resume_image_review' in value and type(value['resume_image_review']) is not bool:
        raise ValueError('exact_saved_generation_handoff_required')
    return dict(value)


def save_reviewed_handoff(workspace_root, registry, guidance, configuration, *,
                          expected_registry_revision, selected_resources=(), check=lambda: None):
    root = Path(workspace_root)
    if not root.is_absolute() or '..' in root.parts:
        raise ValueError('canonical_generation_workspace_required')
    check()
    plan = apply_reviewed_model_guidance(GenerationPlan.create(None), registry, guidance, configuration,
        expected_registry_revision=expected_registry_revision, selected_resources=selected_resources)
    if plan is None:
        raise ValueError('reviewed_generation_configuration_required')
    check()
    path = new_draft_path(root / 'generation')
    token = save_plan(path, plan)
    loaded, observed = load_plan_with_digest(path)
    if loaded != plan or observed != token:
        raise ValueError('saved_generation_handoff_readback_changed')
    check()
    return draft_descriptor(dict(relative_path=path.relative_to(root).as_posix(), sha256=token))


def read_reviewed_handoff(workspace_root, descriptor):
    value = draft_descriptor(descriptor)
    path = Path(workspace_root).joinpath(*PurePosixPath(value['relative_path']).parts)
    plan, token = load_plan_with_digest(path)
    if token != value['sha256']:
        raise ValueError('saved_generation_handoff_changed_review_again')
    return plan, path, token
