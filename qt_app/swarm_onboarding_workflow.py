"""Connect reviewed Swarm onboarding to canonical Models and Setup actions."""
from pathlib import Path

from PySide6.QtWidgets import QApplication, QDialog


def publish_imported_appearance(owner, settings, theme):
    """Refresh presentation only; the guarded settings writer already saved it."""
    current = owner
    synced = set()
    while current is not None:
        sync = getattr(current, "_sync_imported_appearance", None)
        if callable(sync):
            sync(theme); synced.add(id(current))
        change = getattr(current, "change_theme", None)
        if callable(change):
            change(theme)
            setup = getattr(current, "setup_page", None)
            sync = getattr(setup, "_sync_imported_appearance", None)
            if callable(sync) and id(setup) not in synced: sync(theme)
            return
        current = current.parentWidget()
    from .theme import apply_theme
    apply_theme(QApplication.instance(), theme, bool(getattr(settings, "color_blind_mode", False)))


def open_swarm_onboarding(owner, registry, jobs, *, settings, protected_roots=(),
                          manager=None, settings_path=None, receipt_directory=None,
                          service_factory=None, resource_ids=(), generation_storage_root=None,
                          preset_import_service=None):
    from core.settings import DEFAULT_SETTINGS_PATH
    from core.model_storage_policy import retained_fast_roots
    from core.model_workspace import ModelWorkspace
    from core.swarm_runtime import SwarmRuntimeManager
    from core.swarm_model_paths import SwarmModelPaths
    from core.swarm_model_import import SwarmModelImport
    from core.swarm_onboarding import SwarmOnboardingService
    from .model_companion_workflow import resource_boundaries
    from .model_selector_dialog import ModelSelectorDialog
    from .model_workspace_dialog import ModelWorkspaceDialog
    from .swarm_model_import_dialog import SwarmModelImportDialog
    from .swarm_onboarding_dialog import SwarmOnboardingDialog

    if settings is None:
        raise ValueError("Open Import Existing SwarmUI Setup from Setup to use your saved preferences.")
    settings_path = Path(settings_path) if settings_path is not None else DEFAULT_SETTINGS_PATH
    receipt_directory = Path(receipt_directory) if receipt_directory is not None else settings_path.parent / "swarm-onboarding"
    manager = manager if manager is not None else SwarmRuntimeManager()

    def boundaries(snapshot):
        protected, managed = resource_boundaries(snapshot, settings, protected_roots)
        return tuple(dict.fromkeys((*protected, *managed, *retained_fast_roots(snapshot))))

    def make_service(snapshot):
        return SwarmOnboardingService(ModelWorkspace(registry, protected_roots=boundaries(snapshot)),
            SwarmModelPaths(manager), settings, settings_path=settings_path,
            receipt_directory=receipt_directory)

    review = SwarmOnboardingDialog(registry, jobs, owner,
        service_factory=service_factory or make_service, resource_ids=tuple(resource_ids))

    def locate(source):
        snapshot, _ = registry.snapshot()
        child = ModelSelectorDialog(registry, jobs, parent=review, role="", settings=settings,
            protected_roots=boundaries(snapshot), current_choice_label=source.model_name)
        review.track_external_child(child)
        def returned(result):
            if review._closing: return
            if result == QDialog.Accepted and child.selected_resource_id and child.selected_path:
                review.accept_favorite_target(source, child.selected_resource_id, child.selected_path)
            else:
                review.refresh_after_child()
        child.finished.connect(returned)
        child.show()

    def register(_source=None):
        snapshot, _ = registry.snapshot()
        child = ModelWorkspaceDialog(registry, jobs, review, settings=settings,
            protected_roots=boundaries(snapshot))
        child._onboarding_parent = True
        child._update_actions()
        review.track_external_child(child)
        child.finished.connect(lambda _: review.refresh_after_child() if not review._closing else None)
        child.show()

    def metadata(identities):
        if not identities:
            snapshot, _ = registry.snapshot()
            picker = ModelSelectorDialog(registry, jobs, parent=review, role="", settings=settings,
                protected_roots=boundaries(snapshot), current_choice_label="Choose a model for metadata review")
            review.track_external_child(picker)
            def picked(result):
                if review._closing: return
                if result == QDialog.Accepted and picker.selected_resource_id:
                    metadata((picker.selected_resource_id,))
                else:
                    review.refresh_after_child()
            picker.finished.connect(picked)
            picker.show()
            return
        def metadata_service(snapshot):
            source_service = (service_factory or make_service)(snapshot)
            return SwarmModelImport(ModelWorkspace(registry, protected_roots=boundaries(snapshot)),
                source_service.paths)
        child = SwarmModelImportDialog(registry, jobs, tuple(identities), review,
            service_factory=metadata_service)
        review.track_external_child(child)
        child.finished.connect(lambda _: review.refresh_after_child() if not review._closing else None)
        child.show()

    review.locate_favorite_requested.connect(locate)
    review.registration_requested.connect(register)
    review.metadata_requested.connect(metadata)
    def presets():
        from core.swarm_preset_source import make_swarm_preset_import_service
        from core.paths import USER_DATA_DIR
        from .swarm_preset_import_dialog import SwarmPresetImportDialog
        root = generation_storage_root if generation_storage_root is not None else USER_DATA_DIR / "generation"
        service = preset_import_service
        if service is None:
            source_service = (service_factory or make_service)(registry.snapshot()[0])
            service = make_swarm_preset_import_service(root, paths=source_service.paths)
        child = SwarmPresetImportDialog(jobs, service=service, parent=review)
        review.track_external_child(child)
        imported = []
        child.imported.connect(imported.append)
        def returned(_result):
            if review._closing: return
            review.refresh_after_child()
            if imported:
                review.status.setText("Preset import outcomes are retained in the prompt preset collection. Open Generate → Prompt Presets → Open Collection to review and use them in a draft.")
        child.finished.connect(returned)
        child.show()
    review.presets_requested.connect(presets)
    review.appearance_changed.connect(lambda theme: publish_imported_appearance(owner, settings, theme))
    review.show()
    return review
