"""Resolve the user's saved trainer choice for a new explicit run review."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from .trainer_environment import TrainerEnvironmentService, TrainerProfile, _read_json
from .training_setup_selection import TrainingSetupSelectionService, TrainingSetupSelectionError

RUNTIME_PROFILE = Path(__file__).absolute().parents[1] / 'tools/trainer_runtime_profile.json'


class SavedTrainingSetupResolver:
    """Construction performs no I/O; callers run resolution in a cancellable job."""
    def __init__(self, selection_path, *, profile_path=RUNTIME_PROFILE):
        self.selection = TrainingSetupSelectionService(selection_path)
        self.profile_path = Path(profile_path).absolute()

    def __call__(self, *, should_cancel=None, progress=None):
        if should_cancel and should_cancel():
            raise InterruptedError('Loading saved training setup was cancelled.')
        try:
            profile = TrainerProfile.from_dict(_read_json(self.profile_path))
        except (OSError, ValueError, RuntimeError) as exc:
            raise TrainingSetupSelectionError(
                'A reviewed trainer runtime profile is unavailable. Open Setup or use the manual package.'
            ) from exc
        selected, _ = self.selection.snapshot()
        if selected is None:
            raise TrainingSetupSelectionError('Choose a trainer and local resources in Setup first.')
        managed = (TrainerEnvironmentService(selected.managed.root, profile)
                   if selected.mode == 'managed' else None)
        return self.selection.resolve(profile, 'sdxl-lora-' + uuid4().hex[:8],
            managed_environment=managed, should_cancel=should_cancel, progress=progress).request()
