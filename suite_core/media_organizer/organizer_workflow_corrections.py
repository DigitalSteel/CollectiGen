"""Review corrections using native Library and Dataset command owners only."""
from dataclasses import replace
from .hosted_encyclopedia_overlay import compose_startup_runtime, CompositionBoundaryError


def _native_library_label(item, *, compact):
    # Explicit native UI use only; never called by fake tests or module import.
    from PySide6.QtCore import Qt
    item.setText("" if compact else "Library")
    item.setToolTip("Library Status and Guided Tag Review")
    item.setData(Qt.AccessibleTextRole, "Library")


def _restrict_setup_execution(page):
    """Filter stock Setup presentation/search without changing saved settings."""
    if getattr(page, "_organizer_execution_scope_applied", False):
        return
    roots = []
    for key in ("generation", "training"):
        button = getattr(page, "workflow_default_buttons", {}).get(key)
        if button is None:
            continue
        roots.append(button)
        layout = button.parentWidget().layout()
        # The stock defaults grid owns both the description and action in this
        # row. Hide that row together, retaining the widgets and their authority.
        row = layout.getItemPosition(layout.indexOf(button))[0]
        for column in range(layout.columnCount()):
            item = layout.itemAtPosition(row, column)
            if item is not None and item.widget() is not None:
                item.widget().hide()
        button.setEnabled(False)
    indexes = getattr(page, "_subsection_indexes", {})
    for tabs_name, group, key in (("ai_sections", "ai", "generation"),
                                  ("component_sections", "components", "training")):
        tabs = getattr(page, tabs_name, None)
        index = indexes.get(group, {}).get(key)
        if tabs is not None and index is not None:
            roots.append(tabs.widget(index))
            tabs.setTabEnabled(index, False)
            tabs.setTabVisible(index, False)
    navigator = getattr(page, "navigator", None)
    if navigator is not None:
        # SettingsNavigator has no removal API. Filter only destinations rooted
        # in these exact unsupported widgets; supported advanced controls stay.
        navigator._targets[:] = [target for target in navigator._targets
            if target.control is None or not any(
                target.control is root or root.isAncestorOf(target.control)
                for root in roots)]
        navigator._rebuild()
    page._organizer_execution_scope_applied = True


def with_workflow_corrections(reviewed_window_type, *, label_library_item=_native_library_label):
    """Expose existing Library home and preserve visible Dataset review fallback.

    No TrainingPage or executor is constructed. Dataset retains its own modal
    review, saved drafts, collection planning, package decisions and cancellation.
    """
    class CorrectedMediaOrganizerWindow(reviewed_window_type):
        NAVIGATION_PAGE_ORDER = (0, 2, 1, 6, 3, 4, 5, 7, 8)

        def _label_library_home(self):
            row = self.NAVIGATION_PAGE_ORDER.index(0)
            self.navigation.item(row).setHidden(False)
            label_library_item(self.navigation.item(row),
                               compact=getattr(self, "_navigation_mode", "expanded") == "compact")

        def _build_ui(self):
            super()._build_ui()
            self._label_library_home()
            setup = getattr(self, "setup_page", None)
            if setup is not None:
                _restrict_setup_execution(setup)
            # Reuse Library status, recommendation and guided-review panels.
            # Generation remains a separate unavailable product capability.
            dashboard = getattr(self, "dashboard", None)
            generation = getattr(dashboard, "local_generation", None)
            if generation is not None:
                generation.hide()

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if not self.startup_cancelled and not kwargs.get("genuinely_new_profile", False):
                self.navigation.setCurrentRow(0)

        def set_navigation_mode(self, mode, *, persist=True):
            result = super().set_navigation_mode(mode, persist=persist)
            self._label_library_home()
            return result

        def retain_training_review(self, dialog, finished):
            # Truthful refusal selects native Dataset's existing dialog.exec()
            # branch. Do not reparent, finish, destroy or change the review/draft.
            return False

        def closeEvent(self, event):
            from .organizer_prompt_entry import confirm_prompt_close, request_prompt_shutdown, finish_prompt_close
            if not confirm_prompt_close(self):
                event.ignore()
                return
            request_prompt_shutdown(self)
            # Reuse native unsaved-owner checks and actual coordinator/metadata drain.
            super().closeEvent(event)
            finish_prompt_close(self, event.isAccepted())

        def _ensure_page(self, index):
            if index == 8:
                self.statusBar().showMessage(
                    "Training execution is unavailable. Review data and saved drafts in Dataset.", 12000)
                return
            result = super()._ensure_page(index)
            if index == 6 and hasattr(self, "setup_page"):
                _restrict_setup_execution(self.setup_page)
            if index == 2:
                from .organizer_prompt_entry import install_prompt_action
                install_prompt_action(self)
                resume = getattr(getattr(self, "dataset_page", None), "training_resume_button", None)
                if resume is not None:
                    resume.setText("Saved Dataset Drafts.")
                    resume.setToolTip(
                        "Review and resume saved Dataset plans. Training execution is unavailable in Media Organizer.")
                    # Reuse the shared Ribbon's existing source binding.
                    controller = getattr(self, "command_controller", None)
                    binding = getattr(controller, "_page_commands", {}).get(2)
                    if binding is not None and binding[0] is self.dataset_page:
                        for command in binding[1]:
                            if command.source is resume:
                                command.label = resume.text()
            return result

        def open_training_workspace(self, *, group=None, draft=None, collection_reviewed=False):
            self.organizer._check_context()
            self.navigation.setCurrentRow(2)
            if self.stack.currentIndex() != 2:
                return False  # preserve native unsaved/leave guard
            if group is not None:
                if group.get("rules") is not None and not collection_reviewed:
                    self._queue_dataset_page_action(
                        lambda: self.dataset_page.open_concept_groups(selected_group_id=group["id"]))
                    return False  # collection owner must review its current scope
                return bool(self._queue_dataset_page_action(
                    lambda: self.dataset_page.plan_concept_training(group)))
            self._queue_dataset_page_action(
                lambda: self.dataset_page.resume_training_dataset_draft(draft=draft))
            # Legacy callers must not assume a TrainingPage exists. The owning
            # Dataset chooser/resolver remains visible; no trainer is activated.
            return False

    CorrectedMediaOrganizerWindow.__name__ = "CorrectedMediaOrganizerWindow"
    return CorrectedMediaOrganizerWindow


def compose_final_runtime(reviewed_runtime, *, label_library_item=_native_library_label):
    """One final composition over unchanged reviewed startup and hosted overlay.

    The reviewed startup installs its window factory before start_application.
    This callback adds corrections to that factory for the native event loop,
    then restores it before the reviewed export bridge performs its own cleanup.
    """
    hosted_runtime = compose_startup_runtime(reviewed_runtime)

    def start_corrected(*, startup_notice=""):
        exports = vars(reviewed_runtime.window_exports)
        reviewed_type = exports.get("TagManagerWindow")
        if not isinstance(reviewed_type, type):
            raise CompositionBoundaryError("Reviewed startup window factory must already be installed.")
        corrected_type = with_workflow_corrections(reviewed_type,
            label_library_item=label_library_item)
        exports["TagManagerWindow"] = corrected_type
        try:
            return reviewed_runtime.start_application(startup_notice=startup_notice)
        finally:
            if exports.get("TagManagerWindow") is not corrected_type:
                raise CompositionBoundaryError("Corrected window factory ownership changed; left untouched.")
            exports["TagManagerWindow"] = reviewed_type

    return replace(hosted_runtime, start_application=start_corrected)
