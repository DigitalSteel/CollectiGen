"""Finite admitted module host; Manager keeps application management and connections."""
from types import SimpleNamespace

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMainWindow

from suite_core.workflow_modules.controllers import WorkflowController, WorkflowMethods
from suite_core.workflow_modules.session import ModuleEntrySession, RETURN_TARGETS


def require_session(session, component_id):
    if type(session) is not ModuleEntrySession or session.component_id != component_id:
        raise ValueError('genuine_matching_module_session_required')
    session.assert_ready()
    return session


class _WorkflowNavigation:
    """Canonical handlers return to their single real workflow surface."""
    def currentRow(self):
        return 0

    def setCurrentRow(self, row):
        if row != 0:
            raise ValueError('module_workflow_home_required')


class WorkflowWindow(WorkflowMethods, QMainWindow):
    _TRAINING_DRAFT_CLOSE_NOTICE = 'Close the Dataset draft review before closing this application.'

    def __init__(self, session, component_id, *, surface_factory=None, dataset_verifier=None,
                 training_caption_review_factory=None):
        require_session(session, component_id)
        suffix = component_id.removeprefix('collectigen.')
        expected_name = 'GenerateSurface' if suffix == 'generate' else 'TrainSurface'
        if (not isinstance(surface_factory, type)
                or surface_factory.__module__ != 'suite_core.' + suffix + '.surface'
                or surface_factory.__name__ != expected_name):
            raise ValueError('matching_workflow_surface_factory_required')
        super().__init__()
        from qt_app.components import WindowStatusBar
        self.setStatusBar(WindowStatusBar(self))
        self.module_session = session
        self._controller = None
        self._return_target = None
        self._return_resume_draft = None
        self._return_save_pending = False
        self.exit_code = 0
        self.close_policy = SimpleNamespace(pending=False)
        self.view = SimpleNamespace(synthetic=False)
        self.navigation = _WorkflowNavigation()
        self.page_rows = {'Generate': 0, 'Training': 0}
        self.dataset_verifier = dataset_verifier
        self.training_caption_review_factory = training_caption_review_factory
        self.training_draft_settings_path = session.workspace_root / 'generation-settings.json'
        from core.paths import MODEL_RESOURCES_PATH
        self.krea_registry_path = session.workspace_root / MODEL_RESOURCES_PATH.name
        for name in ('_generation_setup_dialog', '_video_setup_cancel', '_generation_dialog',
                     '_video_generation_dialog', '_verified_result_remix', '_training_history_dialog',
                     '_training_package_recovery_task', '_training_package_recovery_dialog',
                     '_krea_review_dialog', '_trainer_setup_dialog', '_trainer_setup_loading_task',
                     '_training_merge_dialog'):
            setattr(self, name, None)
        for name in ('_image_draft_picker_open', '_video_draft_picker_open',
                     '_training_package_picker_open', '_close_after_krea_review',
                     '_trainer_setup_close_requested', '_training_draft_review_open'):
            setattr(self, name, False)
        session.retain_cleanup_owner(self, is_drained=self.local_drained)
        self.surface = surface_factory(self)
        if component_id == 'collectigen.generate':
            self.generation_page = self.surface.page
            self.generation_page.active_changed.connect(self.generation_workspace_changed)
            self.surface.new_requested.connect(self.new_request)
            self.surface.resume_requested.connect(self.resume_request)
            self.surface.create_setup_requested.connect(self.create_generation_setup)
            self.surface.use_setup_requested.connect(self.use_saved_generation_setup)
            self.surface.results_requested.connect(self.review_generation_jobs)
            self.surface.video_setup_requested.connect(lambda: self.setup_video_components())
            self.surface.video_recovery_requested.connect(lambda: self.setup_video_components(recover=True))
            menu = self.surface.more.menu()
            self.video_setup_button = next(action for action in menu.actions()
                if action.text() == 'Review Video Components…')
            self.video_recovery_button = next(action for action in menu.actions()
                if action.text() == 'Recover Video Setup…')
            menu.addAction('Import Prompt Catalog…', lambda: self.open_generate_plan(prompt_catalog=True))
            menu.addAction('Plan a Remix…', self.open_remix_plan)
            self.verified_result_remix_button = self.surface.more.menu().addAction('Plan Remix from Verified Result…')
            self.verified_result_remix_button.setVisible(False)
            self.verified_result_remix_button.triggered.connect(self.open_verified_result_remix)
            self._resume_image_setup_action = self.surface.more.menu().addAction(
                'Resume Image Setup Review…', self._resume_saved_image_setup)
            self._resume_image_setup_action.setVisible(False)
            self.surface.more.menu().aboutToShow.connect(self._sync_saved_review_action)
            title = 'CollectiGen Generate'
        elif component_id == 'collectigen.train':
            self.surface.package_requested.connect(self.open_krea_training_review)
            self.surface.draft_requested.connect(self.open_training_draft)
            self.surface.history_requested.connect(self.open_training_history)
            self.surface.comparison_requested.connect(self.open_training_comparison)
            self.surface.experiments_requested.connect(self.open_training_experiments)
            self.surface.interrupted_package_requested.connect(self.review_interrupted_training_package)
            self.surface.review_requested.connect(self.show_training_review)
            self.training_open_reviews = self.surface.reviews
            self.training_open_review_button = self.surface.continue_review
            self.surface.more.menu().addAction('Set Up Trainer…', self.open_existing_trainer_setup)
            self.surface.more.menu().addAction('Inspect or Merge LoRA…', self.open_training_merge)
            self.surface.more.menu().addAction('Saved Preparations…', lambda: self.open_training_history(preparations=True))
            self.surface.more.menu().addAction('Export Training Draft…', lambda: self.export_training_experiment_draft())
            self.training_package_recovery_button = next(action for action in self.surface.more.menu().actions()
                if action.text() == 'Review Interrupted Package…')
            title = 'CollectiGen Train'
        else:
            raise ValueError('finite_workflow_component_required')
        self.surface.manager_requested.connect(self.request_manager_return)
        from PySide6.QtGui import QShortcut, QKeySequence
        from qt_app.components import ContextHelpButton
        from qt_app.help_content import manager_workspace_help
        topic = 'generation' if component_id == 'collectigen.generate' else 'training'
        label = 'Generate' if topic == 'generation' else 'Train'
        self.workflow_help = ContextHelpButton(label, manager_workspace_help(topic), self)
        self.workflow_help.hide()
        self.surface.help_requested.connect(self.workflow_help.click)
        self._workflow_help_shortcut = QShortcut(QKeySequence('F1'), self)
        self._workflow_help_shortcut.activated.connect(self.workflow_help.click)
        self.setCentralWidget(self.surface)
        from importlib import import_module
        version = import_module('suite_core.' + component_id.removeprefix('collectigen.') + '.version')
        self.setWindowTitle(title + ' · ' + version.DISPLAY_VERSION)
        self.resize(1280, 850)
        from suite_core.workflow_modules.worker_runtime import bind_worker_capabilities
        admitted_verifier = bind_worker_capabilities(session)
        if self.dataset_verifier is None:
            self.dataset_verifier = admitted_verifier
        self._controller = WorkflowController(self, session)
        self.state = self._controller.state
        self.state.retain_verified_result_remix = self.retain_verified_result_remix
        self._close_timer = QTimer(self)
        self._close_timer.setInterval(100)
        self._close_timer.timeout.connect(self.close)
        if component_id == 'collectigen.generate' and session.initial_draft is not None:
            QTimer.singleShot(0, self._open_initial_draft)

    def _open_initial_draft(self):
        if self.generation_state() is None:
            return
        from core.generation_module_handoff import read_reviewed_handoff
        from qt_app.generation_workflow import open_generation_workspace
        try:
            draft = read_reviewed_handoff(self.module_session.workspace_root, self.module_session.initial_draft)
            def opened(target):
                # Only this authenticated canonical handoff adopts the original
                # saved authority. Imported external plans still get a new copy.
                target._draft_path = draft[1]
                if self.module_session.initial_draft.get('resume_image_review') is True:
                    QTimer.singleShot(0, lambda: target.restore_saved_image_setup_review(
                        resume_admission=self.module_session.initial_draft))
            open_generation_workspace(self, force_new=True, preloaded_draft=draft, on_opened=opened)
        except (OSError, RuntimeError, ValueError) as error:
            self.surface.set_status('The reviewed setup could not be reopened. Its saved draft was preserved. ' + str(error))

    def _sync_saved_review_action(self):
        active = self.generation_page.active_workspace()
        plan = getattr(active, 'plan', None)
        self._resume_image_setup_action.setVisible(
            plan is not None and 'unfinished_image_setup_review' in plan.target_context)

    def _resume_saved_image_setup(self):
        if self.generation_state() is None:
            return
        active = self.generation_page.active_workspace()
        resume = getattr(active, 'restore_saved_image_setup_review', None)
        if callable(resume):
            resume()  # A normal saved-draft reopen requires explicit source selection.

    def generation_state(self):
        return None if self._controller is None else self._controller.current_state()

    def module_state(self):
        # Retained callbacks recheck the original peer before admitting more work.
        return 'ready' if self.generation_state() is not None else 'held'

    def checked_image_connection(self):
        return self._controller.checked_connection()

    def review_generator_connection(self):
        return self.request_manager_return('connections')

    def review_image_description_setup(self, owner=None, review=None):
        return self.request_manager_return('description-settings', review_owner=owner, review=review)

    def setup_video_components(self, *, recover=False):
        return self.request_manager_return('video-recovery' if recover else 'video-settings')

    def open_existing_trainer_setup(self):
        return self.request_manager_return('trainer-settings')

    def show_generate_workspace(self):
        return self.module_session.component_id == 'collectigen.generate'

    def new_request(self, media):
        if self.generation_state() is None:
            return
        if media == 'video':
            return self.open_video_plan()
        if media == 'image':
            from qt_app.generation_workflow import open_generation_workspace
            return open_generation_workspace(self, force_new=True)
        raise ValueError('finite_workflow_media_required')

    def resume_request(self, media):
        if media == 'video':
            return self.open_video_plan(reopen=True)
        if media == 'image':
            return self.open_generate_plan()
        raise ValueError('finite_workflow_media_required')

    def refresh_open_training_reviews(self):
        if not hasattr(self.surface, 'set_reviews'):
            return
        rows = {}
        for name in ('_sdxl_preparation_controller', '_training_comparison_controller'):
            controller = getattr(self, name, None)
            if controller is not None:
                for dialog in controller.open_reviews():
                    rows[str(id(dialog))] = (controller, dialog)
        self._training_reviews = rows
        self.surface.set_reviews([(identity, dialog.windowTitle() or 'Training review')
                                 for identity, (_controller, dialog) in rows.items()])

    def show_training_review(self, identity):
        if self.generation_state() is None:
            return
        row = getattr(self, '_training_reviews', {}).get(identity)
        if row is not None:
            if row[0].show_review(row[1]):
                return True
            self.refresh_open_training_reviews()
        return False

    def return_to_open_training_review(self):
        return self.show_training_review(self.surface.reviews.currentData())

    def request_manager_return(self, target, *, review_owner=None, review=None):
        if type(target) is not str or target not in RETURN_TARGETS:
            raise ValueError('finite_workflow_return_target_required')
        if self.generation_state() is None:
            return
        if self._return_save_pending:
            return
        owner = review_owner
        if owner is None and hasattr(self, 'generation_page'):
            owner = self.generation_page.active_workspace()
        if owner is not None and callable(getattr(owner, 'save_for_manager_return', None)):
            if review is None:
                review = getattr(owner, '_image_setup_dialog', None)
            if review is not None and getattr(owner, '_image_setup_dialog', None) is not review:
                return
            try:
                snapshot = review.unfinished_review_snapshot() if review is not None else None
            except (ValueError, RuntimeError, OSError) as error:
                review.suggestion_status.setText(str(error))
                return
            self._return_save_pending = True
            self.centralWidget().setEnabled(False)
            def failed(error):
                self._return_save_pending = False
                self.centralWidget().setEnabled(True)
                self.statusBar().showMessage('Your review remains open. ' + str(error))
            def saved(descriptor):
                from shiboken6 import isValid
                self._return_save_pending = False
                self.centralWidget().setEnabled(False)
                self._return_resume_draft = {**descriptor, 'resume_image_review': review is not None}
                self._return_target = target
                if review is not None and isValid(review):
                    review.reject()
                self._close_timer.start()
                self.close()
            try:
                started = owner.save_for_manager_return(on_saved=saved, review_snapshot=snapshot, on_failed=failed)
            except (ValueError, RuntimeError, OSError) as error:
                failed(str(error))
                return
            if not started:
                failed(owner.status.text())
            return
        # Dirty request/editor review runs before latching module Close.
        self._return_target = target
        self.close()

    def local_drained(self):
        return self._controller is not None and self._controller.drained()

    def closeEvent(self, event):
        event.ignore()
        if self._return_save_pending:
            self.statusBar().showMessage('Saving the exact review before returning to Manager…')
            return
        if self._controller is None:
            return
        if not self.close_policy.pending:
            finder = self._training_package_recovery_dialog
            if finder is not None:
                finder.reject()
                self.statusBar().showMessage('Training package review closed. Close again after its original check settles.')
                return
            if self._training_package_recovery_task is not None:
                self._training_package_recovery_cancel.set()
                self.statusBar().showMessage('Stopping the original training package check. Close again when it finishes.')
                return
            page = getattr(self, 'generation_page', None)
            if page is not None:
                for draft in page.retained_workspaces():
                    if draft.has_active_background_work():
                        page.show_workspace(draft)
                        self.statusBar().showMessage('Close held: finish or cancel the original request before closing this application.')
                        return
            if not self._controller.confirm_close():
                if self._return_target is not None and page is not None and page._draft_close_pending:
                    self._close_timer.start()
                    return
                self._close_timer.stop()
                self._return_target = None
                self._return_resume_draft = None
                self.centralWidget().setEnabled(True)
                return
            guide = self.workflow_help._help_dialog
            if guide is not None and guide.isVisible() and not guide.close():
                return
            try:
                if self._return_target is not None:
                    self.module_session.request_return(self._return_target, resume_draft=self._return_resume_draft)
                self.module_session.begin_close()
            except (ValueError, RuntimeError, OSError):
                self.statusBar().showMessage('The original Manager connection needs recovery. Cleanup remains retained.')
            self.close_policy.pending = True
            self.workflow_help.setEnabled(False)
            self._workflow_help_shortcut.setEnabled(False)
            self.surface.set_closing()
            self._controller.begin_close()
            self._close_timer.start()
        page = getattr(self, 'generation_page', None)
        if page is not None:
            for draft in page.retained_workspaces():
                if draft.has_active_background_work():
                    return
                close = getattr(draft, 'close_workspace', draft.close)
                close()
                if draft in page.retained_workspaces():
                    return
        if not self.local_drained():
            return
        try:
            if not self.module_session.local_drain_reported:
                self.module_session.report_local_drained()
        except (ValueError, RuntimeError, OSError):
            self._close_timer.stop()
            # Every local owner has physically drained above. A lost ACK must
            # not trap an empty UI forever or claim Root's grant was released.
            # The parent still owns the original native Job and records failure.
            self.exit_code = 2
            self.statusBar().showMessage('Local work is closed. Manager retains the failed connection for recovery.')
            event.accept()
            return
        self._close_timer.stop()
        event.accept()
