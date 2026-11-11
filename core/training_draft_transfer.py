"""Source-neutral Training draft transfer using canonical stores and new-file writes."""
from pathlib import Path
import json

from .training_plan_drafts import TrainingPlanDraft, TrainingPlanDraftError
from .training_source_io import _assert_unredirected


def _checkpoint(should_cancel):
    if should_cancel is not None and should_cancel():
        raise InterruptedError("Training draft transfer cancelled.")


def export_training_draft(draft, destination, *, should_cancel=None):
    """Publish a complete new transfer; never overwrite a selected existing file."""
    from .image_record import _atomic_create_text
    if not isinstance(draft, TrainingPlanDraft):
        raise TrainingPlanDraftError("Choose one saved Training draft.")
    target = Path(destination)
    if not target.is_absolute() or not target.parent.is_dir():
        raise TrainingPlanDraftError("Choose an existing folder and a new draft filename.")
    _checkpoint(should_cancel)
    _assert_unredirected(target)
    payload = json.dumps(draft.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if len(payload.encode("utf-8")) > 16 * 1024 * 1024:
        raise TrainingPlanDraftError("This draft exceeds the supported transfer size.")
    _checkpoint(should_cancel)
    _atomic_create_text(target, payload)
    observed = TrainingPlanDraft.read_transfer(target)
    if observed.fingerprint != draft.fingerprint:
        raise TrainingPlanDraftError("The saved draft transfer changed during readback.")
    return target


def import_training_draft(source, store, *, should_cancel=None):
    """Create a fresh destination-owned draft; preserve the source's identity."""
    _checkpoint(should_cancel)
    draft = TrainingPlanDraft.read_transfer(Path(source))
    _checkpoint(should_cancel)
    return store.save_manifest(draft.plan_manifest, draft.name).draft
