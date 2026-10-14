"""Connect explicit resource review to the existing acquisition and cache UI."""
from pathlib import Path

from PySide6.QtWidgets import QDialog

from core.model_cache import ModelCacheService
from core.model_companion_readiness import CompanionReadinessService
from core.model_selection import protected_model_roots
from core.model_workflow_guidance import ModelWorkflowGuidanceService


def resource_boundaries(snapshot, settings, protected_roots=()):
    """Use the same library and managed-resource exclusions as Models."""
    protected = tuple(dict.fromkeys((*protected_roots, *protected_model_roots(settings))))
    managed = [location["path"] for record in snapshot.get("resources", {}).values()
        if record.get("identity", {}).get("kind") == "managed_model_pack"
        for location in record.get("locations", ())]
    directory = str(getattr(settings, "ai_models_directory", "") or "")
    if directory and Path(directory).is_absolute():
        from core.ai_interrogation import WD_TAGGER_MODELS, PLAIN_LANGUAGE_MODELS
        managed.extend(str(Path(directory) / row[1])
            for row in (*WD_TAGGER_MODELS.values(), *PLAIN_LANGUAGE_MODELS.values()))
    return protected, tuple(dict.fromkeys(managed))


def open_companion_review(owner, registry, jobs, main_resource_id, *, settings=None,
                          protected_roots=(), initial_request=None, setup_folders=None,
                          on_guidance_choices=None):
    from .model_companion_readiness_dialog import ModelCompanionReadinessDialog
    from .model_cache_dialog import ModelCacheDialog
    from .model_workspace_dialog import ModelWorkspaceDialog

    def service_factory(snapshot):
        protected, managed = resource_boundaries(snapshot, settings, protected_roots)
        return CompanionReadinessService(registry,
            guidance=ModelWorkflowGuidanceService(update_path=registry.path.with_name("model_workflow_knowledge.json")),
            protected_roots=protected, managed_roots=managed)

    review = ModelCompanionReadinessDialog(registry, jobs, main_resource_id, owner,
        settings=settings, protected_roots=protected_roots,
        service_factory=service_factory, initial_request=initial_request,
        on_guidance_choices=on_guidance_choices)

    def acquire(handoff):
        from core.model_storage_policy import retained_fast_roots
        snapshot, _ = registry.snapshot()
        protected, managed = resource_boundaries(snapshot, settings, protected_roots)
        child = ModelWorkspaceDialog(registry, jobs, review, settings=settings,
            protected_roots=(*protected, *managed, *retained_fast_roots(snapshot)),
            initial_companion_handoff=handoff)
        review.track_external_child(child)
        def returned(result):
            if review._closing: return
            choice = child.selected_companion_choice if result == QDialog.Accepted else None
            review.refresh_after_child(choice)
        child.finished.connect(returned)
        child.show()

    def prepare(handoff):
        def cache_factory(snapshot):
            protected, managed = resource_boundaries(snapshot, settings, protected_roots)
            return ModelCacheService(registry, protected_roots=protected, managed_roots=managed,
                requirement_resolver=handoff.resolver)
        child = ModelCacheDialog(registry, jobs,
            tuple(resource.resource_id for resource in handoff.requirements.resources), review,
            service_factory=cache_factory, initial_requirements=handoff.requirements,
            setup_folders=setup_folders)
        review.track_external_child(child)
        child.finished.connect(lambda _: review.refresh_after_child() if not review._closing else None)
        child.show()

    review.acquisition_requested.connect(acquire)
    review.preparation_requested.connect(prepare)
    review.show()
    return review
