"""Workflow composition over canonical request, trainer and persistence owners."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtWidgets import QDialog, QFileDialog


class WorkflowController:
    def __init__(self, window, session):
        from core.background_jobs import BackgroundJobManager
        from core.settings import AppSettings
        from core.training_source_io import _assert_unredirected
        from core.video_decoder import VideoDecoderLifetime
        self.window, self.session = window, session
        self.closing = False
        self.modal_depth = 0
        self.dialogs = set()
        self.training = None
        session.assert_ready()
        root = session.workspace_root
        settings_path = root / 'generation-settings.json'
        _assert_unredirected(settings_path)
        settings = (AppSettings.load(settings_path) if settings_path.exists()
                    else AppSettings(ai_models_directory=str(root / 'models')))
        self.state = SimpleNamespace(jobs=BackgroundJobManager(), settings=settings,
            generation_data_root=root, generation_settings_path=settings_path,
            generation_runtime_allowed=False, video_decoder_lifetime=(getattr(session, 'video_decoder_lifetime', None) or VideoDecoderLifetime()),
            admit_manager_video_runtime=getattr(session, 'admit_manager_video_runtime', None), generation_session_state=self.current_state,
            video_draft_session_state=self.current_state, checked_image_connection=self.checked_connection,
            review_generator_connection=lambda: self.window.request_manager_return('connections'),
            review_image_description_setup=self.window.review_image_description_setup,
            image_setup_suggestion_provider=(session.caption_runtime.suggest
                if getattr(session, 'caption_runtime', None) is not None else None), checked_image_submission=True,
            offline_generation_alternatives=True, review_verified_generation_results=True)

    def allowed(self):
        if self.closing:
            return False
        try:
            self.session.assert_ready()
        except (ValueError, RuntimeError, OSError):
            return False
        return True

    def current_state(self):
        return self.state if self.allowed() else None

    def checked_connection(self):
        if not self.allowed():
            return None
        from core.paths import SWARM_RUNTIME_PATH
        from core.swarm_runtime import SwarmRuntimeManager
        return SwarmRuntimeManager(self.state.generation_data_root / SWARM_RUNTIME_PATH.name).checked_generation_connection()

    @contextmanager
    def modal(self):
        self.modal_depth += 1
        try:
            yield
        finally:
            self.modal_depth -= 1

    def run_dialog(self, dialog):
        self.dialogs.add(dialog)
        try:
            with self.modal():
                return dialog.exec()
        finally:
            self.dialogs.discard(dialog)
            dialog.deleteLater()

    def confirm_close(self):
        blockers = ('_image_draft_picker_open', '_video_draft_picker_open',
            '_training_package_picker_open', '_training_draft_review_open',
            '_generation_setup_dialog', '_training_history_dialog', '_krea_review_dialog',
            '_training_merge_dialog', '_trainer_setup_dialog', '_trainer_setup_loading_task',
            '_training_package_recovery_dialog', '_training_package_recovery_task',
            '_generation_source_task', '_video_runtime_admission_task')
        if self.modal_depth or any(getattr(self.window, name, None) for name in blockers):
            self.window.statusBar().showMessage('Finish or close the current review before closing this application.')
            return False
        page = getattr(self.window, 'generation_page', None)
        if page is not None and not page.confirm_draft_loss():
            return False
        if page is None:
            for dialog in self.floating_generation():
                confirm = getattr(dialog, 'confirm_draft_loss', None)
                if callable(confirm) and not confirm():
                    return False
        return all(controller.confirm_editor_close() for controller in self.training_controllers())

    def floating_generation(self):
        return tuple(dialog for name in ('_generation_dialog', '_video_generation_dialog')
                     if (dialog := getattr(self.window, name, None)) is not None)

    def training_controllers(self):
        return tuple(controller for name in ('_sdxl_preparation_controller', '_training_comparison_controller')
                     if (controller := getattr(self.window, name, None)) is not None)

    def begin_close(self):
        self.closing = True
        runtime_owner = getattr(self.session, 'worker_capabilities', None)
        if runtime_owner is not None:
            runtime_owner.begin_close()
        page = getattr(self.window, 'generation_page', None)
        if page is not None:
            page.request_background_shutdown()
        else:
            for dialog in self.floating_generation():
                shutdown = getattr(dialog, 'request_background_shutdown', None)
                if callable(shutdown):
                    shutdown()
        for controller in self.training_controllers():
            controller.request_background_shutdown()
        setup = getattr(self.window, '_manager_trainer_setup', None)
        if setup is not None:
            setup.begin_close()
        self.state.video_decoder_lifetime.begin_mutation()
        self.state.jobs.shutdown(wait=False)

    def drained(self):
        if not self.closing or self.modal_depth:
            return False
        page = getattr(self.window, 'generation_page', None)
        setup = getattr(self.window, '_manager_trainer_setup', None)
        return (not (page is not None and page.has_active_background_work())
            and not (getattr(self.session, 'worker_capabilities', None) is not None
                     and self.session.worker_capabilities.close_held)
            and not any(getattr(dialog, 'has_active_background_work', lambda: False)()
                        for dialog in self.floating_generation())
            and not any(controller.has_active_background_work() or controller.dialogs
                        for controller in self.training_controllers())
            and not (setup is not None and setup.close_held)
            and self.state.video_decoder_lifetime.drained
            and not self.state.jobs.active() and self.state.jobs.shutdown(wait=False))


# Mechanically extracted Manager handlers. Persistence and controller owners
# remain canonical; the module host supplies former closure inputs explicitly.
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QVBoxLayout,
    QHBoxLayout, QPushButton, QScrollArea, QFrame, QComboBox)
from suite_core.manager.ui_cards import compact_action


class WorkflowMethods:
    _IMAGE_PICKER_CLOSE_NOTICE = "Close held. Finish or close the image chooser, then close this application again."
    def generation_workspace_changed(self, _dialog=None):
        self._generation_dialog = self.generation_page.retained_workspace("image")
        self._video_generation_dialog = self.generation_page.retained_workspace("video")

    def open_generate_plan(self, *, prompt_catalog=False):
        if self._image_draft_picker_open:
            return
        state = self.generation_state()
        if state is None:
            return
        from qt_app.generation_workflow import choose_generation_draft
        self.show_generate_workspace()
        self._image_draft_picker_open = True
        try:
            choose_generation_draft(
                self, allow_draft_transfer=True,
                selection_allowed=lambda: self.generation_state() is state,
                prompt_catalog_only=prompt_catalog,
            )
        finally:
            self._image_draft_picker_open = False
            if self.statusBar().currentMessage() == self._IMAGE_PICKER_CLOSE_NOTICE:
                self.statusBar().clearMessage()

    def review_generation_jobs(self):
        if self._image_draft_picker_open or self.generation_state() is None:
            return
        self.navigation.setCurrentRow(self.page_rows["Generate"])
        from qt_app.generation_workflow import open_generation_workspace

        def review(dialog):
            dialog.tabs.setCurrentIndex(3)
            if dialog.history_button.isEnabled():
                dialog.load_history(on_open=True, keep_current_tab=True)

        open_generation_workspace(self, on_opened=review)

    def retain_verified_result_remix(self, paths, verifier, evidence):
        if self.generation_state() is None or len(paths) != 1:
            return
        self._verified_result_remix = (tuple(paths), verifier, evidence)
        self.verified_result_remix_button.setVisible(True)

    def open_verified_result_remix(self):
        state = self.generation_state()
        pending = self._verified_result_remix
        if state is None or pending is None or self._image_draft_picker_open:
            return
        from qt_app.generation_workflow import open_generation_workspace

        paths, verifier, evidence = pending

        def opened(_dialog):
            self._verified_result_remix = None
            self.verified_result_remix_button.setVisible(False)

        open_generation_workspace(self, paths=paths, remix=True, force_new=True,
            source_context={"verified_source": verifier, "verified_source_evidence": evidence}, on_opened=opened,
            source_result_allowed=lambda: self.generation_state() is state and self._verified_result_remix is pending)

    def open_remix_plan(self):
        if self._image_draft_picker_open or getattr(self, "_generation_source_task", None) is not None:
            return
        state = self.generation_state()
        if state is None:
            return
        from PySide6.QtWidgets import QFileDialog
        from qt_app.generation_workflow import open_generation_workspace

        self._image_draft_picker_open = True
        try:
            path, _ = QFileDialog.getOpenFileName(
                self, "Choose an image for a Remix plan", "",
                "Images (*.png *.jpg *.jpeg *.webp)")
        finally:
            self._image_draft_picker_open = False
            if self.statusBar().currentMessage() == self._IMAGE_PICKER_CLOSE_NOTICE:
                self.statusBar().clearMessage()
        if path and self.generation_state() is state:
            open_generation_workspace(
                self, paths=(Path(path),), remix=True, force_new=True,
                source_result_allowed=lambda: self.generation_state() is state,
            )

    def setup_video_components(self, *, recover=False):
        if self._video_setup_cancel is not None:
            self._video_setup_cancel.set()
            self.statusBar().showMessage("Stopping Video setup. Waiting for the current step to finish…")
            return
        state = self.generation_state()
        review = getattr(self.module_session, "review_manager_video_setup", None)
        install = getattr(self.module_session, "install_manager_video_setup", None)
        recovery = getattr(self.module_session, "recover_manager_video_setup", None)
        if state is None or (recover and not callable(recovery)):
            return
        if not recover and (not callable(review) or not callable(install)):
            return
        if (self._video_draft_picker_open or getattr(self, "_video_generation_dialog", None) is not None
                or getattr(self, "_video_runtime_admission_task", None) is not None):
            self.statusBar().showMessage("Close the Video draft before setting up Video components.")
            return
        from threading import Event
        from PySide6.QtWidgets import QMessageBox
        from qt_app.workers import CoordinatedCallTask

        lifetime = state.video_decoder_lifetime
        if not lifetime.begin_mutation():
            lifetime.end_mutation()
            self.statusBar().showMessage("Wait for Video work to finish before starting Setup.")
            return
        cancelled = self._video_setup_cancel = Event()
        self.video_setup_button.setText("Cancel Video Setup")
        self.video_recovery_button.setEnabled(False)

        def finish(message):
            self._video_setup_task = None
            self._video_setup_cancel = None
            lifetime.end_mutation()
            self.video_setup_button.setText("Set Up Video Components…")
            self.video_recovery_button.setEnabled(True)
            self.statusBar().showMessage(message)

        def start(label, operation, complete):
            self.statusBar().showMessage(label + "…")

            def work(job):
                def check():
                    if cancelled.is_set() or not job.checkpoint():
                        raise InterruptedError("Video setup cancelled.")
                check()
                return operation(check)

            task = CoordinatedCallTask(state.jobs, label, work, resource="io")
            self._video_setup_task = task
            task.signals.complete.connect(complete)
            task.signals.failed.connect(lambda message: finish(message))
            task.signals.cancelled.connect(lambda: finish("Video setup cancelled."))
            try:
                task.start()
            except (RuntimeError, ValueError) as exc:
                finish(str(exc))

        def reviewed(plan):
            if cancelled.is_set() or self.generation_state() is not state:
                finish("Video setup cancelled.")
                return
            choice = QMessageBox.question(self, "Set Up Video Components",
                "Download and install the reviewed Video decoder and encoder for Suite Manager?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if choice != QMessageBox.Yes or cancelled.is_set() or self.generation_state() is not state:
                finish("Video setup cancelled.")
                return
            start("Installing Video components", lambda check: install(plan, check_interrupt=check),
                  lambda cleanup: finish("Video components installed. Open a Video draft to check them."
                      if not cleanup else "Video components installed. Previous files still need cleanup."))

        if recover:
            choice = QMessageBox.question(self, "Recover Video Setup",
                "Recover the interrupted Video setup and remove its temporary files?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if choice != QMessageBox.Yes or cancelled.is_set() or self.generation_state() is not state:
                finish("Video setup recovery cancelled.")
                return
            start("Recovering Video setup", lambda check: recovery(check_interrupt=check),
                  lambda restored: finish("Previous Video components restored."
                      if restored else "Video setup recovery check finished."))
        else:
            start("Reviewing Video setup", lambda check: review(check_interrupt=check), reviewed)

    def open_video_plan(self, *, reopen=False):
        state = self.generation_state()
        if (state is None or self._video_draft_picker_open or self._video_setup_cancel is not None
                or getattr(self, "_video_runtime_admission_task", None) is not None):
            return
        self.navigation.setCurrentRow(self.page_rows["Generate"])
        from qt_app.video_generation_workflow import (
            choose_video_generation_draft, open_video_generation_workspace,
        )

        def connect_close_notices(video):
            video.offline_draft_work_settled.connect(self.clear_video_close_notice)
            video.offline_draft_work_settled.connect(self.clear_video_picker_close_notice)
            video.workspace_closed.connect(self.clear_video_close_notice)

        if reopen:
            self._video_draft_picker_open = True
            try:
                choose_video_generation_draft(
                    self, selection_allowed=lambda: self.generation_state() is state,
                    on_opened=connect_close_notices,
                )
            finally:
                self._video_draft_picker_open = False
                self.clear_video_picker_close_notice()
        else:
            open_video_generation_workspace(self, on_opened=connect_close_notices)

    _VIDEO_PICKER_CLOSE_NOTICE = "Close held. Finish or close the video draft chooser, then close Manager again."

    def clear_video_picker_close_notice(self):
        bar = self.statusBar()
        if bar.currentMessage() == self._VIDEO_PICKER_CLOSE_NOTICE:
            bar.clearMessage()

    _VIDEO_CLOSE_NOTICE = "Close held. Finish or save the video draft, then close Manager again."

    def clear_video_close_notice(self):
        bar = self.statusBar()
        if bar.currentMessage() == self._VIDEO_CLOSE_NOTICE:
            bar.clearMessage()

    def run_generation_setup_dialog(self, dialog):
        self._generation_setup_dialog = dialog
        try:
            return dialog.exec() == QDialog.Accepted
        finally:
            self._generation_setup_dialog = None
            dialog.deleteLater()

    def review_image_description_setup(self, owner, review):
        from shiboken6 import isValid
        state = self.generation_state()
        if state is None or self._generation_setup_dialog is not None:
            return
        def allowed():
            return (self.generation_state() is state and isValid(owner) and isValid(review)
                    and getattr(owner, "_image_setup_dialog", None) is review
                    and not getattr(owner, "_closed", True))
        if not allowed():
            return
        from suite_core.manager.description_setup_ui import DescriptionSetupDialog
        caption_runtime = getattr(self.module_session, "caption_runtime", None)
        dialog = DescriptionSetupDialog(state.generation_settings_path, review, allowed=allowed,
                                        components_available=caption_runtime is not None)
        accepted = self.run_generation_setup_dialog(dialog)
        if not allowed():
            return
        if accepted and dialog.saved_snapshot is not None:
            for key, value in dialog.saved_snapshot.values.items():
                setattr(state.settings, key, value)
        if accepted and dialog.components_requested and caption_runtime is not None and allowed():
            from suite_core.manager.caption_components_ui import CaptionComponentsDialog
            components = CaptionComponentsDialog(caption_runtime, state.settings, state.jobs, review, allowed=allowed)
            self.run_generation_setup_dialog(components)
        if not allowed():
            return
        review.resume_description_setup()
        review.raise_(); review.activateWindow()

    def create_generation_setup(self):
        state = self.generation_state()
        if state is None or self._generation_setup_dialog is not None or self._image_draft_picker_open:
            return
        from qt_app.generation_setup_source_dialog import GenerationSetupSourceDialog
        choice = GenerationSetupSourceDialog(parent=self, allow_image=True, allow_entities=False)
        if not self.run_generation_setup_dialog(choice) or self.generation_state() is not state:
            return
        if choice.source_kind == "manual":
            self.create_manual_generation_setup()
            return
        from PySide6.QtWidgets import QFileDialog
        self._image_draft_picker_open = True
        try:
            path, _ = QFileDialog.getOpenFileName(
                self, "Choose an image for a Generation Setup", "", "Images (*.png *.jpg *.jpeg *.webp)")
        finally:
            self._image_draft_picker_open = False
            if self.statusBar().currentMessage() == self._IMAGE_PICKER_CLOSE_NOTICE:
                self.statusBar().clearMessage()
        if path and self.generation_state() is state:
            from qt_app.generation_workflow import open_generation_workspace
            open_generation_workspace(self, paths=(Path(path),), force_new=True, setup_from_image=True,
                source_result_allowed=lambda: self.generation_state() is state)

    def create_manual_generation_setup(self):
        state = self.generation_state()
        if state is None or self._generation_setup_dialog is not None:
            return
        from qt_app.generation_manual_setup_dialog import ManualGenerationSetupDialog
        from qt_app.generation_workflow import _generation_model_registry

        dialog = ManualGenerationSetupDialog(
            state.jobs, state.generation_data_root / "generation",
            registry=_generation_model_registry(self), parent=self,
        )
        if self.run_generation_setup_dialog(dialog) and dialog.saved_path is not None:
            self.statusBar().showMessage(
                "Generation Setup saved. Choose Use Saved Setup to review it in a new image plan.",
                12000,
            )

    def use_saved_generation_setup(self):
        state = self.generation_state()
        if state is None or self._generation_setup_dialog is not None:
            return
        from qt_app.generation_saved_work_dialog import SavedGenerationWorkDialog
        from qt_app.generation_workflow import _generation_model_registry, open_generation_workspace

        browser = SavedGenerationWorkDialog(
            state.jobs, state.generation_data_root / "generation", "recipes",
            parent=self, selection_only=True, allow_setup_transfer=True,
            registry=_generation_model_registry(self),
            file_transfer_allowed=lambda: self.generation_state() is state,
        )
        if not self.run_generation_setup_dialog(browser) or self.generation_state() is not state:
            return
        if browser.requested_action == "create":
            self.create_generation_setup()
            return
        path, digest = browser.selected_path, browser.selected_digest
        if path is None or not digest:
            return

        def review(target):
            original = target.plan.digest

            def unapplied(error, _location_saved):
                if target.plan.digest == original:
                    target.reject()
                if error:
                    self.statusBar().showMessage(
                        "Setup could not be opened. Choose Use Saved Setup to review the current file.",
                        12000,
                    )

            target.load_recipe(selected_path=path, selected_digest=digest, on_unapplied=unapplied)

        open_generation_workspace(self, force_new=True, on_opened=review)

    _TRAINING_PICKER_CLOSE_NOTICE = "Close held. Finish or close the training package chooser, then close Manager again."

    def refresh_open_training_reviews(self):
        selected = self.training_open_reviews.currentData()
        rows = []
        seen = set()
        for name in ("_sdxl_preparation_controller", "_training_comparison_controller"):
            controller = getattr(self, name, None)
            if controller is None:
                continue
            for dialog in controller.open_reviews():
                if id(dialog) in seen:
                    continue
                seen.add(id(dialog))
                rows.append((controller, dialog))
        self.training_open_reviews.blockSignals(True)
        self.training_open_reviews.clear()
        selected_index = -1
        for index, pair in enumerate(rows):
            controller, dialog = pair
            self.training_open_reviews.addItem(dialog.windowTitle() or "Training review", pair)
            if selected is not None and selected[0] is controller and selected[1] is dialog:
                selected_index = index
        self.training_open_reviews.setCurrentIndex(
            selected_index if selected_index >= 0 else 0 if rows else -1)
        self.training_open_reviews.blockSignals(False)
        allowed = (not getattr(self, "view", self.view).synthetic
                   and self.module_state() == "ready"
                   and not self.close_policy.pending)
        self.training_open_reviews.setEnabled(bool(rows) and allowed)
        self.training_open_review_button.setEnabled(bool(rows) and allowed)

    def return_to_open_training_review(self):
        if (self.view.synthetic or self.module_state() != "ready"
                or self.close_policy.pending):
            return
        selected = self.training_open_reviews.currentData()
        if selected is None:
            self.refresh_open_training_reviews()
            return
        controller, dialog = selected
        if not controller.show_review(dialog):
            self.refresh_open_training_reviews()
            self.statusBar().showMessage("That Training review has closed. Choose another open review or use Run History.")
            return
        self.navigation.setCurrentRow(self.page_rows["Training"])

    def open_training_experiments(self):
        from qt_app.generation_workflow import _generation_model_registry
        if self.generation_state() is None:
            return
        self.navigation.setCurrentRow(self.page_rows["Training"])
        controller = self.sdxl_preparation_controller(_generation_model_registry(self))
        if controller is not None:
            controller.experiments()

    def export_training_experiment_draft(self, draft=None):
        state = self.generation_state()
        controller = getattr(self, "_sdxl_preparation_controller", None)
        if state is None or controller is None or self.close_policy.pending:
            return
        from qt_app.training_draft_transfer_dialog import TrainingDraftTransferDialog
        for dialog in controller.open_reviews():
            if isinstance(dialog, TrainingDraftTransferDialog):
                controller.show_review(dialog)
                return
        controller._show(TrainingDraftTransferDialog(state.jobs,
            state.generation_data_root / "training" / "training-plan-drafts.json", self, selected=draft))

    def open_training_draft(self):
        state = self.generation_state()
        if state is None or getattr(self, "_training_draft_review_open", False):
            return
        self.navigation.setCurrentRow(self.page_rows["Training"])
        from pathlib import Path
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        from core.training_plan_drafts import TrainingPlanDraft, TrainingPlanDraftError
        from qt_app.training_candidate_plan_dialog import TrainingPlanDraftPickerDialog

        self._training_draft_review_open = True
        try:
            path, _ = QFileDialog.getOpenFileName(
                self, "Choose a Dataset draft", "", "Dataset draft (*.json)")
            if not path or self.generation_state() is not state:
                return
            try:
                draft = TrainingPlanDraft.read_transfer(Path(path))
            except TrainingPlanDraftError as exc:
                QMessageBox.warning(self, "Dataset draft unavailable", str(exc))
                return
            dialog = TrainingPlanDraftPickerDialog((draft,), self, saved_details_only=True)
            cancel_recheck = self.bind_training_draft_recheck(dialog, draft, state)
            try:
                dialog.exec()
            finally:
                cancel_recheck()
                dialog.deleteLater()
        finally:
            self._training_draft_review_open = False
            if self.statusBar().currentMessage() == self._TRAINING_DRAFT_CLOSE_NOTICE:
                self.statusBar().clearMessage()

    def bind_training_draft_recheck(self, dialog, draft, state):
        from PySide6.QtWidgets import QDialogButtonBox
        from core.settings import AppSettings
        from core.training_source_io import read_stable_training_source
        from qt_app.workers import CoordinatedCallTask

        status = QLabel()
        status.setWordWrap(True)
        status.setTextFormat(Qt.PlainText)
        dialog.layout().insertWidget(3, status)
        button = dialog.findChild(QDialogButtonBox).addButton(
            "Check Source Images", QDialogButtonBox.ActionRole,
        )
        button.setEnabled(self.training_draft_settings_path is not None)
        caption_button = dialog.findChild(QDialogButtonBox).addButton(
            "Check Captions…", QDialogButtonBox.ActionRole,
        )
        caption_button.setEnabled(button.isEnabled() and bool(
            draft.plan_manifest.get("assessment_context_fingerprint")
        ))
        status.setText("Check the saved images or choose their Dataset data folder to compare captions."
                       if button.isEnabled() else "Source checks require a configured Manager profile.")
        task = None
        closed = False

        def check(job, selected_root):
            try:
                raw, _ = read_stable_training_source(self.training_draft_settings_path, max_bytes=16 * 1024 * 1024)
                registry = AppSettings._from_bytes(raw).library_registry()
            except (OSError, RuntimeError, ValueError) as exc:
                raise ValueError("Manager library settings could not be read.") from exc
            counts = None

            def read_sources(consume=None):
                nonlocal counts
                counts = draft.recheck_source_files(
                    registry, should_cancel=lambda: job.cancel_requested,
                    on_verified_annotation=consume,
                )

            caption_status = "unavailable"
            if selected_root is not None:
                from core.training_candidate_selection import admit_dataset_caption_root, prepare_dataset_caption_review

                root = admit_dataset_caption_root(selected_root, registry)
                try:
                    review = prepare_dataset_caption_review(root, cancel=lambda: job.cancel_requested)
                except FileNotFoundError as exc:
                    raise ValueError(
                        "The selected Dataset folder is missing files needed to check captions. "
                        "Choose the original Dataset data folder or rebuild the draft in Organizer."
                    ) from exc
                caption_status = review(
                    draft.plan_manifest, read_sources, cancel=lambda: job.cancel_requested,
                )
            elif self.training_caption_review_factory is not None and draft.plan_manifest.get("assessment_context_fingerprint"):
                try:
                    review = self.training_caption_review_factory()
                except (OSError, RuntimeError, ValueError):
                    review = None
                if review is not None:
                    caption_status = review(
                        draft.plan_manifest, read_sources, cancel=lambda: job.cancel_requested,
                    )
            if counts is None:
                read_sources()
            return counts, caption_status

        def finish(message):
            nonlocal task
            task = None
            if closed:
                return
            status.setText(message)
            enabled = self.generation_state() is state
            button.setEnabled(enabled)
            caption_button.setEnabled(enabled and bool(draft.plan_manifest.get("assessment_context_fingerprint")))

        def start(selected_root=None):
            nonlocal task
            if closed or task is not None or self.generation_state() is not state:
                return
            button.setEnabled(False)
            caption_button.setEnabled(False)
            status.setText("Checking saved source images…" if selected_root is None else "Checking Dataset context and saved captions…")
            task = CoordinatedCallTask(
                state.jobs, "Check Training draft images",
                lambda job: check(job, selected_root), resource="io",
            )
            def show_result(result):
                counts, caption_status = result
                captions = {
                    "matches": "Current captions match the saved draft.",
                    "captions_changed": "Current captions differ from the saved draft. Rebuild the draft in Organizer.",
                    "context_changed": "Dataset context changed. Rebuild the draft in Organizer.",
                    "unavailable": "Current captions could not be checked.",
                    "cancelled": "Caption check cancelled.",
                }
                guidance = captions[caption_status]
                if caption_status == "unavailable" and (counts[1] or counts[2]):
                    guidance += " Review the changed sources and rebuild the draft in Organizer."
                finish(
                    f"Checked {counts[0]:,} image(s). {counts[1]:,} changed. "
                    f"Saved Image Knowledge: {counts[2]:,} changed, {counts[3]:,} unavailable. "
                    + guidance
                )

            task.signals.complete.connect(show_result)
            task.signals.failed.connect(finish)
            task.signals.cancelled.connect(lambda: finish("Source check cancelled."))
            try:
                task.start()
            except (RuntimeError, ValueError):
                finish("Source check could not start. Try again.")

        def cancel(*_):
            nonlocal closed
            if closed:
                return
            closed = True
            if task is not None:
                task.cancel()

        def choose_caption_root():
            from PySide6.QtWidgets import QFileDialog

            selected = QFileDialog.getExistingDirectory(dialog, "Choose the Dataset data folder")
            if selected:
                start(Path(selected))

        button.clicked.connect(lambda: start())
        caption_button.clicked.connect(choose_caption_root)
        dialog.finished.connect(cancel)
        return cancel

    def open_training_history(self, *, preparations=False):
        from time import monotonic

        state = self.generation_state()
        if state is None or getattr(self, "_training_history_dialog", None) is not None:
            return
        self.navigation.setCurrentRow(self.page_rows["Training"])
        from core.training_run_index import ScopedPreparationHistory, ScopedTrainingHistory
        from core.paths import MODEL_RESOURCES_PATH
        from qt_app.training_history_dialog import TrainingHistoryDialog
        from qt_app.workers import CoordinatedCallTask

        write_allowed = lambda: self.generation_state() is state
        history = (ScopedPreparationHistory(state.generation_data_root / "TrainingReview", write_allowed=write_allowed) if preparations
                   else ScopedTrainingHistory(state.generation_data_root / "training",
                       model_registry_path=state.generation_data_root / MODEL_RESOURCES_PATH.name,
                       write_allowed=write_allowed))
        task = None
        closing_review = False
        receipt_started = 0.0

        class ManagerTrainingHistoryDialog(TrainingHistoryDialog):
            def done(self, result):
                nonlocal closing_review
                if task is not None:
                    if result == QDialog.Rejected and not closing_review:
                        closing_review = True
                        self.set_retained_index_status("Closing after the current receipt check finishes…")
                        if task.job_id:
                            try:
                                state.jobs.cancel(task.job_id)
                            except Exception:
                                self.set_retained_index_status(
                                    "Stop could not be requested. Closing after the receipt check finishes.")
                    return
                super().done(result)

        dialog = ManagerTrainingHistoryDialog(history, state.jobs, self)
        if preparations:
            dialog.setWindowTitle("Saved SDXL Preparations")
        dialog.open_button.setText("Review Selected Preparation")
        for button in (dialog.refresh_button, dialog.forget_button, dialog.recover_button,
                       dialog.open_button, *dialog.buttons.buttons()):
            compact_action(button)
        dialog.open_button.setMinimumWidth(max(dialog.open_button.minimumWidth(),
            dialog.open_button.fontMetrics().horizontalAdvance("Review Saved Process Outcome") + 32))
        self._training_history_dialog = dialog
        receipt_timer = QTimer(dialog)
        def receipt_activity():
            if task is not None and not closing_review:
                dialog.set_retained_index_status(
                    f"Checking the selected saved review · Elapsed {int(monotonic() - receipt_started)}s")
        receipt_timer.timeout.connect(receipt_activity)
        reviewed = None
        handoff = None
        retained_handoff = None
        import_validation = False
        open_run = QPushButton("Open Saved Training Review", dialog)
        open_run.setAutoDefault(False)
        open_run.setProperty("primary", True)
        open_run.hide()
        dialog.layout().addWidget(open_run)
        plan_result = QPushButton("Plan an Image With This Result…", dialog)
        plan_result.setAutoDefault(False)
        plan_result.hide()
        dialog.layout().addWidget(plan_result)
        open_prepared = QPushButton("Open Prepared Workspace", dialog)
        open_prepared.setAutoDefault(False)
        open_prepared.hide()
        if preparations:
            dialog.layout().addWidget(open_prepared)
        prepared_review = None

        def has_saved_outcome(record):
            return (record.kind == "execution" and bool(record.latest_journal_tip)
                    and record.phase in {"stopped", "cancelled", "failed", "interrupted",
                                         "verification_required", "registration_required", "completed"})

        def can_open_run(record):
            return (not preparations and record is not None and has_saved_outcome(record)
                    and Path(record.registry_path) == state.generation_data_root / MODEL_RESOURCES_PATH.name)

        def update_open_run():
            selected = dialog._selected()
            available = can_open_run(selected)
            open_run.setVisible(available)
            open_run.setEnabled(available and task is None and not closing_review
                                and bool(dialog._revision) and self.generation_state() is state
                                and not self.close_policy.pending and not dialog._history_operation_busy())
            dialog.open_button.setProperty("primary", not available)

        def verify(record, revision, *, open_workspace=False, open_retained=False):
            nonlocal task, reviewed, prepared_review, receipt_started
            prepared_review = None
            open_prepared.hide()
            reviewed = None
            plan_result.hide()
            if self.generation_state() is not state:
                dialog.set_retained_index_status(
                    "Manager session is no longer ready. Close this review and reopen it when ready.")
                return
            if task is not None:
                dialog.set_retained_index_status(
                    "A saved review is still being checked. Wait for it to finish, then try again.")
                return
            validation = record.kind == "validation" and record.phase == "completed"
            execution = has_saved_outcome(record)
            anima = record.kind == "anima"
            if anima:
                review = history.review_anima_settings
            elif validation:
                review = history.review_validation
            elif execution:
                review = history.review_execution_outcome
            else:
                review = history.review_preparation
            def inspect(_job):
                result = review(record, revision)
                if not open_workspace:
                    return result
                from core.training_source_io import _assert_unredirected
                from core.training_package import _directory_state

                folder = Path(record.destination) / "work" / "prepared"
                try:
                    _assert_unredirected(folder)
                    return folder, _directory_state(folder)
                except (OSError, RuntimeError, ValueError):
                    raise ValueError("The prepared workspace is missing or changed. Restore the original folder or prepare the package again.") from None

            dialog.set_retained_index_status("Checking the selected saved review…")
            task = CoordinatedCallTask(state.jobs, "Verify Training receipt",
                                       inspect,
                                       resource="io")
            receipt_started = monotonic()
            dialog.progress.setRange(0, 0)
            dialog.progress.show()
            receipt_activity()
            receipt_timer.start(250)
            update_open_run()

            def finish(message):
                nonlocal task
                task = None
                receipt_timer.stop()
                dialog.progress.hide()
                if closing_review:
                    dialog.reject()
                    return
                update_open_run()
                if self._training_history_dialog is dialog:
                    if self.generation_state() is not state:
                        dialog.set_retained_index_status(
                            "Manager session is no longer ready. Close this review and reopen it when ready.")
                    elif dialog._selected() == record and dialog._revision == revision:
                        dialog.set_retained_index_status(message)
                    else:
                        message = ("The previous check finished. You can now review the selected run."
                                   if dialog._selected() is not None else "")
                        dialog.set_retained_index_status(message)

            def complete(result):
                nonlocal reviewed, prepared_review, retained_handoff
                if closing_review:
                    finish("")
                    return
                if preparations:
                    finish("Saved SDXL preparation receipt verified. Training was not started. Live inputs were not rechecked.")
                    if (self._training_history_dialog is not dialog or self.generation_state() is not state
                            or dialog._selected() != record or dialog._revision != revision):
                        return
                    if open_workspace:
                        from core.training_source_io import _assert_unredirected
                        from core.training_package import _directory_state
                        from PySide6.QtCore import QUrl
                        from PySide6.QtGui import QDesktopServices

                        folder, identity = result
                        try:
                            _assert_unredirected(folder)
                            if _directory_state(folder) != identity:
                                raise ValueError("Prepared workspace changed")
                        except (OSError, RuntimeError, ValueError):
                            dialog.set_retained_index_status("The prepared workspace changed. Review the saved preparation again.")
                            return
                        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
                            dialog.set_retained_index_status("Windows could not open the prepared workspace. Choose Open Prepared Workspace to retry.")
                    prepared_review = record, revision
                    open_prepared.show()
                elif anima:
                    finish(f"Saved Anima settings: {result.max_steps} optimizer steps, "
                           f"{result.resolution} px, LoRA rank {result.rank}, seed {result.seed}, "
                           f"learning rate {result.learning_rate:g}, time limit {result.timeout_seconds:g} seconds. "
                           f"Batch size {result.batch_size}, gradient accumulation {result.gradient_accumulation}, "
                           f"repeats {result.repeats}. "
                           "Model outputs and live inputs were not rechecked.")
                elif validation:
                    qualification = ("Candidate measurement. " if result["qualification"] == "candidate_only" else "")
                    integrity = ("Saved report hash verified. " if record.validation_report_sha256
                                 else "Older record has no saved report hash. ")
                    finish(f"{qualification}Saved held-out denoising MSE: {result['mean_mse']:.6g}. "
                           f"Sample count: {len(result['values'])}. "
                           f"{integrity}Live inputs were not rechecked. MSE does not measure image quality.")
                    if (self._training_history_dialog is dialog and self.generation_state() is state
                            and dialog._selected() == record and dialog._revision == revision):
                        from qt_app.training_validation_dialog import TrainingValidationDialog
                        report_dialog = TrainingValidationDialog.from_saved_report(
                            result, state.jobs,
                            evaluation_root=state.generation_data_root / "training" / "validation",
                            report_hash_verified=bool(record.validation_report_sha256), parent=dialog)
                        report_dialog.setAttribute(Qt.WA_DeleteOnClose)
                        report_dialog.setWindowModality(Qt.WindowModal)
                        report_dialog.show()
                elif execution:
                    terminal_state, exit_code, registered = result
                    outcome = {
                        "exited_unverified": "exited",
                        "cancelled": "was cancelled",
                        "failed": "failed",
                        "interrupted": "was interrupted",
                    }[terminal_state]
                    exit_detail = f"Exit code: {exit_code}. " if exit_code is not None else ""
                    result_detail = (f"Recorded registered result: {registered.resource_id}. "
                                     if registered is not None else "")
                    finish(f"Saved journal verified: the training process {outcome} and its process tree stopped. "
                           f"{exit_detail}{result_detail}Output files and live inputs were not checked.")
                    if (open_retained and can_open_run(record)
                            and self._training_history_dialog is dialog
                            and self.generation_state() is state and not self.close_policy.pending
                            and dialog._selected() == record and dialog._revision == revision):
                        retained_handoff = record, revision
                        dialog.accept()
                        return
                    if (registered is not None and self._training_history_dialog is dialog
                            and self.generation_state() is state and dialog._selected() == record
                            and dialog._revision == revision
                            and Path(record.registry_path) == state.generation_data_root / MODEL_RESOURCES_PATH.name):
                        reviewed = record, revision, registered
                        plan_result.show()
                else:
                    finish(f"Saved preparation verified for {record.annotation.name or record.run_id}. "
                           f"Configured time limit: {result:g} seconds. Results have not been checked.")

            task.signals.complete.connect(complete)
            task.signals.failed.connect(finish)
            task.signals.cancelled.connect(lambda: finish("Receipt check cancelled."))
            try:
                task.start()
            except (RuntimeError, ValueError) as exc:
                finish(str(exc))

        def selection_changed():
            nonlocal reviewed, prepared_review
            prepared_review = None
            open_prepared.hide()
            reviewed = None
            plan_result.hide()
            if not closing_review:
                message = "Still checking a saved receipt…" if task is not None else ""
                dialog.set_retained_index_status(message)
            selected = dialog._selected()
            update_open_run()
            completed_validation = selected is not None and selected.kind == "validation" and selected.phase == "completed"
            if selected is not None and selected.kind == "anima":
                dialog.open_button.setText("Review Saved Anima Settings")
            elif completed_validation:
                dialog.open_button.setText("Review Saved Measurement")
            elif selected is not None and has_saved_outcome(selected):
                dialog.open_button.setText("Review Saved Process Outcome")
            else:
                dialog.open_button.setText("Review Selected Preparation")

        def plan_with_result():
            nonlocal handoff
            if (reviewed is None or self.generation_state() is not state
                    or dialog._selected() != reviewed[0] or dialog._revision != reviewed[1]):
                return
            current = getattr(self, "_generation_dialog", None)
            if current is not None and not current._closed:
                dialog.set_retained_index_status("Close the current image draft before planning with this result.")
                return
            handoff = reviewed
            dialog.accept()

        def open_saved_workspace():
            if (task is not None or prepared_review is None or self.generation_state() is not state
                    or dialog._selected() != prepared_review[0] or dialog._revision != prepared_review[1]):
                return
            verify(*prepared_review, open_workspace=True)

        open_prepared.clicked.connect(open_saved_workspace)
        def open_saved_run():
            selected = dialog._selected()
            if (task is not None or closing_review or not can_open_run(selected)
                    or not dialog._revision or self.generation_state() is not state
                    or self.close_policy.pending or dialog._history_operation_busy()):
                return
            verify(selected, dialog._revision, open_retained=True)

        open_run.clicked.connect(open_saved_run)
        plan_result.clicked.connect(plan_with_result)
        if not preparations:
            from PySide6.QtWidgets import QFileDialog
            from qt_app.training_history_dialog import PAGE_SIZE

            find_validation = QPushButton("Import Validation Review…", dialog)
            find_validation.setAutoDefault(False)
            find_validation.setToolTip("Open a saved Validation receipt that is not listed here. No new evaluation starts.")
            dialog.layout().addWidget(find_validation)

            def choose_validation():
                nonlocal import_validation
                if (task is not None or closing_review or dialog._history_operation_busy()
                        or self.generation_state() is not state or self.close_policy.pending):
                    return
                import_validation = True
                dialog.accept()

            find_validation.clicked.connect(choose_validation)
            find_anima = QPushButton("Review Unlisted Anima Preparation…", dialog)
            save_anima = QPushButton("Add Reviewed Preparation to History", dialog)
            review_pending = QPushButton("Review Pending Anima History Write", dialog)
            finish_pending = QPushButton("Recover Reviewed Anima History Entry", dialog)
            for button in (find_anima, save_anima, review_pending, finish_pending):
                button.setAutoDefault(False)
                dialog.layout().addWidget(button)
            save_anima.hide()
            finish_pending.hide()
            anima_review = None
            pending_review = None
            restored_run = None

            def select_restored_run():
                nonlocal restored_run
                if restored_run is None:
                    return
                for index in range(dialog.runs.topLevelItemCount()):
                    item = dialog.runs.topLevelItem(index)
                    if item.data(0, Qt.UserRole)[0] == restored_run:
                        restored_run = None
                        dialog.runs.setCurrentItem(item)
                        dialog.set_retained_index_status("Anima preparation is in Training History.")
                        break

            dialog.runs.model().rowsInserted.connect(lambda *_: QTimer.singleShot(0, select_restored_run))

            def recover_anima(*, save=False, pending=False):
                nonlocal task, anima_review, pending_review
                if task is not None or closing_review or self.generation_state() is not state:
                    return
                current = pending_review if pending else anima_review
                if save:
                    if current is None:
                        return

                    def work(job):
                        session_ready = lambda: not job.cancel_requested and self.generation_state() is state
                        if pending:
                            history.recover_pending_anima(current, confirmed=True, session_ready=session_ready)
                            record = current[1]
                        else:
                            try:
                                record = history.save_unlisted_anima(current, confirmed=True, session_ready=session_ready)
                            except OSError:
                                return None, None
                        rows, _ = history.snapshot()
                        position = next((i for i, row in enumerate(rows) if row.run_id == record.run_id), None)
                        return record, None if position is None else position // PAGE_SIZE * PAGE_SIZE
                elif pending:
                    work = lambda _job: history.review_pending_anima()
                else:
                    path, _ = QFileDialog.getOpenFileName(dialog, "Select the retained Anima receipt",
                        str(state.generation_data_root / "training"), "Anima receipt (anima-review.json)")
                    if (not path or self._training_history_dialog is not dialog or not dialog.isVisible()
                            or self.generation_state() is not state or task is not None):
                        return
                    work = lambda _job: history.review_unlisted_anima(path)

                save_anima.hide()
                finish_pending.hide()
                anima_review = None
                pending_review = None

                find_anima.setEnabled(False)
                review_pending.setEnabled(False)
                dialog.set_retained_index_status("Saving the reviewed preparation…" if save else
                                                 "Checking the retained Anima receipt…")
                task = CoordinatedCallTask(state.jobs, "Recover Anima preparation", work, resource="io")

                def finish(message="", result=None):
                    nonlocal task, anima_review, pending_review, restored_run
                    task = None
                    if closing_review:
                        dialog.reject()
                        return
                    if self.generation_state() is not state:
                        dialog.set_retained_index_status("Manager session ended. Reopen Training History to retry.")
                        return
                    find_anima.setEnabled(True)
                    review_pending.setEnabled(True)
                    if result is None:
                        dialog.set_retained_index_status(message)
                    elif save:
                        record, offset = result
                        if record is None:
                            anima_review = current
                            save_anima.show()
                            dialog.set_retained_index_status(
                                "Training History could not save the preparation. Choose Add Reviewed Preparation to History to retry.")
                            return
                        if offset is None:
                            dialog.set_retained_index_status("Training History changed. Refresh to find the saved preparation.")
                            return
                        restored_run = record.run_id
                        dialog._request_page(offset)
                        dialog.set_retained_index_status("Anima preparation saved. Reading its Training History page…")
                    elif pending:
                        pending_review = result
                        _, record, settings = result
                        dialog.set_retained_index_status(
                            f"Pending Anima History entry {record.run_id}: {settings.max_steps} steps. "
                            "Recover to finish saving this entry. Training will not start.")
                        finish_pending.show()
                    else:
                        anima_review = result
                        _, settings = result
                        dialog.set_retained_index_status(
                            f"Retained Anima preparation: {settings.max_steps} steps, {settings.resolution} px, "
                            f"LoRA rank {settings.rank}, seed {settings.seed}, learning rate {settings.learning_rate:g}, "
                            f"time limit {settings.timeout_seconds} seconds, batch size {settings.batch_size}, "
                            f"gradient accumulation {settings.gradient_accumulation}, repeats {settings.repeats}. "
                            "Add this preparation to History to find it again. Training will not start.")
                        save_anima.show()

                task.signals.complete.connect(lambda result: finish(result=result))
                task.signals.failed.connect(finish)
                task.signals.cancelled.connect(lambda: finish("Anima preparation recovery cancelled."))
                try:
                    task.start()
                except (RuntimeError, ValueError) as exc:
                    finish(str(exc))

            find_anima.clicked.connect(lambda: recover_anima())
            save_anima.clicked.connect(lambda: recover_anima(save=True))
            review_pending.clicked.connect(lambda: recover_anima(pending=True))
            finish_pending.clicked.connect(lambda: recover_anima(save=True, pending=True))
        dialog.runs.itemSelectionChanged.connect(selection_changed)
        dialog.openRunRequested.connect(verify)
        try:
            dialog.exec()
        finally:
            self._training_history_dialog = None
            if task is not None:
                task.cancel()
            dialog.deleteLater()
        if (import_validation and self.generation_state() is state
                and not self.close_policy.pending):
            from core.model_resource_registry import ModelResourceRegistry

            registry = ModelResourceRegistry(state.generation_data_root / MODEL_RESOURCES_PATH.name)
            controller = self.sdxl_preparation_controller(registry)
            if controller is not None:
                controller.open_validation()
        if (retained_handoff is not None and self.generation_state() is state
                and not self.close_policy.pending):
            from core.model_resource_registry import ModelResourceRegistry

            registry = ModelResourceRegistry(state.generation_data_root / MODEL_RESOURCES_PATH.name)
            controller = self.sdxl_preparation_controller(registry)
            if controller is not None:
                controller._open_retained(*retained_handoff)
        if handoff is not None and self.generation_state() is state:
            from qt_app.generation_workflow import open_generation_workspace

            record, revision, registered = handoff

            def check_resource(resource):
                if history.review_execution_outcome(record, revision)[2] != registered or not any(
                        row.get("operation") == "training"
                        and row.get("authority") == "collectigen_verified"
                        and row.get("plan_fingerprint") == record.plan_fingerprint
                        and row.get("output_id") == registered.resource_id
                        and row.get("execution_receipt") == "sha256:" + registered.receipt_sha256
                        for row in resource["lineage"]):
                    raise ValueError("The registered Training result changed. Review it again in Training History.")

            open_generation_workspace(self, resource_id=registered.resource_id, selected_role="lora",
                selected_family="sdxl", force_new=True, resource_guard=check_resource,
                source_result_allowed=lambda: self.generation_state() is state)

    def open_training_comparison(self, _checked=False, *, run_id="", required_resource=""):
        state = self.generation_state()
        if state is None:
            return
        self.navigation.setCurrentRow(self.page_rows["Training"])
        from qt_app.training_workflow_controller import TrainingWorkflowController

        controller = getattr(self, "_training_comparison_controller", None)
        if controller is None:
            controller = TrainingWorkflowController(state.jobs, self)
            self._training_comparison_controller = controller
            controller.workspaceChanged.connect(self.refresh_open_training_reviews)
        return controller.evaluation(run_id, required_resource,
            on_return=lambda: self.navigation.setCurrentRow(self.page_rows["Training"]),
            batch_review_only=False, protect_existing_draft=True,
            generation_allowed=lambda: self.generation_state() is state,
        )

    def review_interrupted_training_package(self):
        state = self.generation_state()
        if (state is None or self._training_package_picker_open
                or self._training_package_recovery_task is not None
                or self._training_package_recovery_dialog is not None or self._krea_review_dialog is not None):
            return
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QFileDialog
        from threading import Event
        from core.training_package import TrainingPackageService
        from qt_app.training_page import TrainingRecoveryFinderDialog
        from qt_app.workers import CoordinatedCallTask

        finder = TrainingRecoveryFinderDialog(self)
        for button in (finder.folder_button, finder.exact_button,
                       finder.browse_receipt_button, finder.close_button, finder.review_button):
            compact_action(button)
        self._training_package_recovery_dialog = finder
        selected = []

        def current():
            return (self._training_package_recovery_dialog is finder
                    and self.generation_state() is state)

        def selected_receipt(path, close=True):
            if not current() or self._training_package_recovery_task is not None:
                return
            selected[:] = [str(path)]
            if close:
                finder.accept()

        def finished(_result):
            if self._training_package_recovery_dialog is not finder:
                return
            self._training_package_recovery_dialog = None
            if self._training_package_recovery_task is not None:
                self._training_package_recovery_cancel.set()
                # Keep the hidden owner alive until its original task and
                # shared progress binding settle; late work cannot reopen it.
            else:
                finder.deleteLater()
            if selected and self.generation_state() is state:
                path = selected[0]
                QTimer.singleShot(0, lambda: self._review_training_package_receipt(path, state))

        def pick(kind):
            if not current() or self._training_package_recovery_task is not None:
                return
            self._training_package_picker_open = True
            try:
                if kind == "folder":
                    path = QFileDialog.getExistingDirectory(
                        finder, "Where Would the Training Package Be Saved?")
                else:
                    path, _ = QFileDialog.getOpenFileName(
                        finder, "Review an Exact Interrupted Training Package", "",
                        "Training recovery receipt (*.recovery.json)")
            finally:
                self._training_package_picker_open = False
                if self.statusBar().currentMessage() == self._TRAINING_PICKER_CLOSE_NOTICE:
                    self.statusBar().clearMessage()
            if not path or not current():
                return
            if kind == "receipt":
                selected_receipt(path)
                return
            finder.show_searching()
            cancelled = self._training_package_recovery_cancel = Event()

            def search(job):
                return TrainingPackageService.recovery_choices(path, should_cancel=lambda: (
                    cancelled.is_set() or self.module_state() != "ready" or not job.checkpoint()))

            task = CoordinatedCallTask(state.jobs, "Find interrupted Training packages", search, resource="io")
            self._training_package_recovery_task = task
            self.training_package_recovery_button.setEnabled(False)
            from qt_app.job_progress import bind_job_progress
            progress = bind_job_progress(finder, task,
                title=f"Find packages in {Path(path).name}", unit="packages")

            def settle(choices=None, message=None):
                if self._training_package_recovery_task is not task:
                    return
                self._training_package_recovery_task = None
                progress.finish(task)
                active = self.generation_state() is state
                self.training_package_recovery_button.setEnabled(active)
                if not current():
                    if self._training_package_recovery_dialog is not finder:
                        finder.deleteLater()
                    else:
                        finder.show_search_error("The Manager session ended or changed. Close this finder and reopen Training; nothing was changed.")
                        finder.folder_button.setEnabled(False)
                        finder.browse_receipt_button.setEnabled(False)
                    return
                if choices is None or cancelled.is_set():
                    finder.show_search_error(message or
                        "Package search stopped. Choose a location to retry; nothing was recovered.")
                else:
                    finder.show_choices(path, choices)

            task.signals.complete.connect(settle)
            task.signals.failed.connect(lambda _error: settle(message=
                "Could not check that location. Choose another location or browse an exact receipt; nothing was changed."))
            task.signals.cancelled.connect(lambda: settle())
            try:
                task.start()
            except (RuntimeError, ValueError):
                settle(message="Could not start the package search. Wait for active work to finish, then choose a location again or browse an exact receipt.")

        finder.folder_requested.connect(lambda: pick("folder"))
        finder.exact_receipt_requested.connect(lambda: pick("receipt"))
        finder.receipt_requested.connect(lambda path: selected_receipt(path, close=False))
        finder.finished.connect(finished)
        finder.open()

    def _review_training_package_receipt(self, path, state):
        if (not self.isVisible() or self.generation_state() is not state or self._training_package_picker_open
                or self._training_package_recovery_task is not None
                or self._training_package_recovery_dialog is not None or self._krea_review_dialog is not None):
            return
        from PySide6.QtWidgets import QMessageBox
        from threading import Event
        from core.training_package import TrainingPackageService
        from qt_app.workers import CoordinatedCallTask

        cancelled = self._training_package_recovery_cancel = Event()

        def preview(job):
            return TrainingPackageService().preview_recovery(path, should_cancel=lambda: (
                cancelled.is_set() or self.module_state() != "ready" or not job.checkpoint()))

        task = CoordinatedCallTask(state.jobs, "Review interrupted Training package", preview, resource="io")
        self._training_package_recovery_task = task
        self.training_package_recovery_button.setEnabled(False)
        self.statusBar().showMessage("Checking the selected Training package receipt…")

        def settle(result=None):
            if self._training_package_recovery_task is not task:
                return
            self._training_package_recovery_task = None
            active = self.generation_state() is state
            self.training_package_recovery_button.setEnabled(active)
            if not active:
                return
            if result is None or cancelled.is_set():
                self.statusBar().showMessage("Receipt review stopped. Choose Review Interrupted Training Package to try again.")
                return
            self.statusBar().clearMessage()
            details = "\n".join(result.issues)
            if result.recovery_phase:
                details = f"Destination: {result.destination}\n\n{details}"
                next_step = ("Choose Review Published Package to check the published folder. "
                             "Use the original package workspace to finalize its receipt."
                             if result.recovery_phase == "promoted" else
                             "Return to the original package workspace to review recovery.")
                details += f"\n\n{next_step}"
            box = QMessageBox(self)
            box.setWindowTitle("Interrupted Training Package")
            box.setTextFormat(Qt.PlainText)
            box.setText(details + "\n\nManager did not change any package files.")
            open_package = None
            if result.recovery_phase == "promoted" and result.verification is not None:
                box.setStandardButtons(QMessageBox.Close)
                open_package = box.addButton("Review Published Package", QMessageBox.ActionRole)
            self._training_package_recovery_dialog = box
            try:
                box.exec()
                confirmed = open_package is not None and box.clickedButton() is open_package
            finally:
                self._training_package_recovery_dialog = None
                box.deleteLater()
            if confirmed and self.generation_state() is state:
                self.open_krea_training_review(destination=result.destination,
                    expected_package_fingerprint=result.verification.package_fingerprint)

        task.signals.complete.connect(settle)
        task.signals.failed.connect(lambda _error: settle())
        task.signals.cancelled.connect(settle)
        try:
            task.start()
        except (RuntimeError, ValueError):
            settle()

    def open_existing_trainer_setup(self):
        state = self.generation_state()
        if state is None or self.training_draft_settings_path is None:
            return False
        from suite_core.manager.trainer_setup_ui import open_manager_trainer_setup
        return open_manager_trainer_setup(self, state, self.training_draft_settings_path,
            allowed=lambda: (self.module_state() == 'ready'
                and not self.close_policy.pending and self.generation_state() is state))

    def sdxl_preparation_controller(self, registry):
        state = self.generation_state()
        if state is None or self.close_policy.pending:
            return None
        from core.training_run_index import TrainingRunIndex, ScopedTrainingHistory
        from core.training_source_io import _assert_unredirected
        from qt_app.training_workflow_controller import TrainingWorkflowController
        from suite_core.manager.krea_training_review import resolve_manager_saved_sdxl_setup

        root = state.generation_data_root / "training"
        runs = root / "runs"
        controller = getattr(self, "_sdxl_preparation_controller", None)
        if controller is None:
            try:
                _assert_unredirected(runs)
                runs.mkdir(parents=True, exist_ok=True)
            except (OSError, RuntimeError) as exc:
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(self, "Training folder unavailable", str(exc))
                return None
            def session_allowed():
                return self.module_state() == "ready" and not self.close_policy.pending

            def load_saved_setup(*, should_cancel, progress):
                return resolve_manager_saved_sdxl_setup(
                    self.training_draft_settings_path, registry.path,
                    should_cancel=lambda: should_cancel() or not session_allowed(), progress=progress,
                )

            controller = TrainingWorkflowController(
                state.jobs, self, run_index=TrainingRunIndex(root / "training-runs.json"),
                preparation_root=root / "preparation", run_output_scope=runs,
                saved_setup=load_saved_setup if self.training_draft_settings_path is not None else None,
                session_allowed=session_allowed,
                draft_store_path=root / "training-plan-drafts.json",
                clone_opener=self.export_training_experiment_draft,
                evaluation_opener=self.open_training_comparison,
                validation_root=root / "validation",
                prepare_opener=self.open_krea_training_review,
                experiments_index=ScopedTrainingHistory(root, model_registry_path=registry.path),
            )
            controller.packageReviewRequested.connect(
                lambda selected: self.open_krea_training_review(destination=selected.destination,
                    expected_package_fingerprint=selected.package_fingerprint))
            self._sdxl_preparation_controller = controller
            controller.workspaceChanged.connect(self.refresh_open_training_reviews)
        return controller

    def open_sdxl_preparation(self, package, registry, recipe):
        controller = self.sdxl_preparation_controller(registry)
        if controller is None:
            return False
        return controller.prepare(package, registry, recipe) is not None

    def open_krea_training_review(self, *, destination=None, expected_package_fingerprint=None):
        registry_path = self.krea_registry_path
        if (self.view.synthetic or self.module_state() != "ready"
                or self.close_policy.pending or self._training_package_picker_open
                or self._training_package_recovery_task is not None
                or self._krea_review_dialog is not None):
            return
        from PySide6.QtWidgets import QFileDialog
        from suite_core.manager.krea_training_review import KreaTrainingReviewDialog
        self.navigation.setCurrentRow(self.page_rows["Training"])
        if destination is None:
            self._training_package_picker_open = True
            try:
                destination = QFileDialog.getExistingDirectory(self, "Select the training package")
            finally:
                self._training_package_picker_open = False
                if self.statusBar().currentMessage() == self._TRAINING_PICKER_CLOSE_NOTICE:
                    self.statusBar().clearMessage()
        if (not destination or self.close_policy.pending
                or self.module_state() != "ready"):
            return
        from core.background_jobs import BackgroundJobManager
        preview_jobs = self.state.jobs
        try:
            dialog = KreaTrainingReviewDialog(destination, registry_path, self,
                                                dataset_verifier=self.dataset_verifier, preview_jobs=preview_jobs,
                                                expected_package_fingerprint=expected_package_fingerprint,
                                                open_trainer_setup=self.open_existing_trainer_setup,
                                                open_sdxl_preparation=self.open_sdxl_preparation,
                                                anima_settings_path=self.training_draft_settings_path,
                                                anima_history_root=self.generation_state().generation_data_root / "training",
                                                anima_review_allowed=lambda: (
                                                    not self.close_policy.pending
                                                    and self.module_state() == "ready"))
        except BaseException:
            raise
        self._krea_review_dialog = dialog
        try:
            dialog.exec()
        finally:
            self._krea_review_dialog = None
            dialog.deleteLater()
            if self._close_after_krea_review:
                self._close_after_krea_review = False
                QTimer.singleShot(0, self.close)

    def open_training_merge(self):
        if (self.view.synthetic or self.module_state() != "ready"
                or self.close_policy.pending):
            return
        if getattr(self, "_training_merge_dialog", None) is not None:
            return
        from core.background_jobs import BackgroundJobManager
        from qt_app.training_merge_dialog import TrainingMergeDialog
        jobs = self.state.jobs
        try:
            dialog = TrainingMergeDialog(
                jobs, self, inspection_allowed=lambda: (
                    self.module_state() == "ready" and not self.close_policy.pending),
            )
            self._training_merge_dialog = dialog
            dialog.exec()
        finally:
            dialog = getattr(self, "_training_merge_dialog", None)
            self._training_merge_dialog = None
            if dialog is not None:
                dialog.deleteLater()
