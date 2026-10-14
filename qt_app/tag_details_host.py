"""Selected host for the original Tag Details controls and save transaction."""
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QWidget, QMessageBox

from core.knowledge_editor_context import KnowledgeEditorContext, SelectedCharacterRecordEditService
from .knowledge_page import KnowledgePage, prepare_knowledge_page
from .character_record_editor import CharacterRecordEditorDialog


class _SelectedCharacterDialog(CharacterRecordEditorDialog):
    """Original controls; preserve selected service across native picker refresh."""
    def __init__(self, host, service, *args, **kwargs):
        self._selected_host = host
        super().__init__(service, *args, **kwargs)
        self._save_pool = host.page.pool

    @property
    def service(self):
        return self._selected_service

    @service.setter
    def service(self, value):
        # The original Source picker reconstructs a base service. Its assignment
        # remains stock; this selected subclass retains the admitted owner.
        self._adopt_pending()
        self._selected_service = SelectedCharacterRecordEditService(
            self._selected_host.session, value.database, value.tag_database, value.categories)

    def _adopt_pending(self):
        pending = getattr(self._selected_host.session, "_pending_native_operation", None)
        if pending is not None:
            if pending[0] != "character_edit":
                raise RuntimeError("A different selected writer owns the pending result.")
            self._selected_host._native_adopted(self._selected_host.page, pending[1])

    def _autosave_complete(self, result):
        try:self._selected_host._native_adopted(self._selected_host.page,result)
        except Exception as error:
            self._autosave_failed(str(error)); return
        super()._autosave_complete(result)

    def accept(self):
        try:self._adopt_pending()
        except Exception as error:
            self._last_error = str(error)
            self._set_status(f"Character save needs selected-authority review: {error}", error=True)
            return
        super().accept()


def prepare_tag_details_host(session, state, job=None):
    """Call on the host's worker, with its actual selected operation/event owner."""
    session.require_editor_ready()
    if state.knowledge is not session.tags:
        raise ValueError("Host must borrow the exact selected native Tag authority.")
    context = KnowledgeEditorContext.selected(session)
    prepared = prepare_knowledge_page(state, job, editor_context=context,
                                      category_session=session.category_session)
    session.require_editor_ready()
    return prepared


class _SelectedTagPage(KnowledgePage):
    """Original forms; borrowed Category UI stays in its existing visible home."""

    def __init__(self, *args, workspace, **kwargs):
        self._borrowed_workspace = workspace
        super().__init__(*args, **kwargs)

    def _build_categories(self):
        workspace = self._borrowed_workspace
        self.category_workspace = self.order_workspace = workspace
        self.category_views = workspace.views
        self._changing_category_view = False
        workspace.staged_state_changed.connect(self._update_workbench_staged_state)
        return QWidget(self)

    def _queue_category_workspace_refresh(self, **kwargs):
        # The verified selected adoption already rebased the same native session.
        # Never replace it with the original default-path detached reload session.
        self._pending_tag_workspace_rebase = None
        self.category_workspace.refresh_all()
        self._refresh_review_views()

    def _changed(self, *, defer_workspace=False, workspace_current=False, **dependency):
        self._tag_view_cache = None
        self.state.events.publish("knowledge_changed", **dependency)
        if dependency.get("complete") is True:
            self.scoped_data_changed.emit(dependency)
        else:
            self.data_changed.emit()
        if not workspace_current:
            self._queue_category_workspace_refresh()

    def _modifier_compatibility_save_failed(self, message):
        # A selected identity/recovery rejection is not permission to read a
        # replacement catalog through the ordinary failure-reload path.
        self._modifier_policy_save_task = None
        self.category_workspace.editor_session.reload_required = True
        self.category_workspace.setEnabled(False)
        self.tag_editor_panel.setEnabled(True)
        self._set_tag_save_status(
            f"Policy completion needs saved-authority review: {message}. "
            "Your separate Tag draft remains here; reopen the selected database before retrying.",error=True)


class TagDetailsEditorHost(QObject):
    """One selected session, real host owner/pool, original New/Edit/Save/Close."""
    saved = Signal()
    unavailable = Signal(str)
    owns_category_completion = True

    def __init__(self, session, state, *, preparation, category_workspace,
                 pool, parent=None, ui_settings=None):
        super().__init__(parent)
        session.require_editor_ready()
        context = KnowledgeEditorContext.selected(session)
        if (state.knowledge is not session.tags or preparation.tags is not session.tags
                or preparation.categories is not session.categories
                or preparation.characters is not session.characters
                or preparation.patterns is not session.patterns
                or preparation.foundation is not session.services["foundation_snapshot"]
                or preparation.category_session is not session.category_session
                or preparation.editor_context != context or not preparation.is_current(state)
                or category_workspace.session is not session.category_session
                or category_workspace.characters is not session.characters):
            raise ValueError("Prepared original editor must own this exact selected session.")
        if pool is None or not callable(getattr(state, "begin_long_operation", None)) or not callable(getattr(state, "finish_long_operation", None)):
            raise ValueError("Borrow the actual host's operation owner and retained Qt pool.")
        self.session = session
        self.page = _SelectedTagPage(state, parent, preparation=preparation,
            editor_context=context, pool=pool, ui_settings=ui_settings,
            external_category_search=False,
            workspace=category_workspace,
            tag_save_runner=lambda request: session.save_tag_request(request, defer_adoption=True),
            tag_save_adopted=self._adopted,
            native_operation_runner=session.run_native_operation,
            native_operation_adopted=self._native_adopted,
            character_initialization_review=self._review_character_initialization,
            character_editor_factory=self._character_editor)
        category_workspace.editor_tag_page=self.page
        category_workspace.edit_tag_requested.connect(self.edit_tag)
        category_workspace.add_tag_requested.connect(self._add_tags_to_category)
        category_workspace.routine_change_saved.connect(self._category_saved)

    def _add_tags_to_category(self, category_id):
        self.session.require_editor_ready()
        return self.page._add_tags_to_category(category_id)

    def _review_character_initialization(self):
        self.session.require_editor_ready()
        missing = [p for p in (self.session.characters.series_path,
            self.session.characters.characters_path, self.session.characters.outfits_path) if not p.exists()]
        if not missing:return True
        return QMessageBox.question(self.page, "Initialize Character Knowledge?",
            "This setup will create the missing Character Knowledge stores in the selected Tag Database "
            "and use the original reviewed Character setup transaction. The reusable Tag remains saved "
            "if you cancel. Continue?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    def _character_editor(self, service, *args, **kwargs):
        self.session.require_character_authority()
        return _SelectedCharacterDialog(self, service, *args, **kwargs)

    def _native_adopted(self, page, result):
        try:
            self.session.adopt_native_operation(page,result)
        except Exception:
            self.session.reload_required=True
            page.category_workspace.setEnabled(False)
            raise
        self.saved.emit()

    def _category_saved(self, _change):
        try:
            self.session.adopt_category_workspace_save(self.page.category_workspace, defer_ready=True)
            self.page._adopt_tag_save_authority(self.session.tags, self.session.services)
            self.session.finish_category_adoption()
            self.saved.emit()
        except Exception as error:
            self.session.reload_required = True
            self.page.category_workspace.setEnabled(False)
            self.unavailable.emit(f"Reopen the selected editor after Category authority review: {error}")

    def _adopted(self, page, result):
        try:
            self.session.adopt_tag_editor_save(page, result)
        except Exception:
            self.session.reload_required = True
            page.category_workspace.setEnabled(False)
            raise
        self.saved.emit()

    @property
    def busy(self):
        return bool(self.session.busy or self.page.has_active_background_work()
                    or self.page._modifier_policy_save_task is not None)

    def close_ready(self):
        dialog = getattr(self.page, "_active_tag_editor_dialog", None)
        return not (self.busy or self.page._tag_save_pending
                    or dialog is not None and dialog.isVisible())

    def new_tag(self):
        self.session.require_editor_ready()
        self.page._new_tag_from_canvas()

    def edit_tag(self, stable_id):
        self.session.require_editor_ready()
        if stable_id not in self.session.tags.tags:
            raise ValueError("Select a current editable native Tag identity.")
        self.page._open_tag_details(stable_id)
