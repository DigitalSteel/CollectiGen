"""Generate's independently admitted window entry."""
from suite_core.workflow_modules.window import WorkflowWindow
from suite_core.generate.surface import GenerateSurface


def create_window(session):
    return WorkflowWindow(session, 'collectigen.generate', surface_factory=GenerateSurface)
