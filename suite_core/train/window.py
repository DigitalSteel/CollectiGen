"""Train's independently admitted window entry."""
from suite_core.workflow_modules.window import WorkflowWindow
from suite_core.train.surface import TrainSurface


def create_window(session, *, dataset_verifier=None, training_caption_review_factory=None):
    return WorkflowWindow(session, 'collectigen.train', surface_factory=TrainSurface, dataset_verifier=dataset_verifier,
        training_caption_review_factory=training_caption_review_factory)
