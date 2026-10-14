"""Independent command bridge borrowing the original native Category editor.

The standalone shell owns composition, worker admission, selection and refresh.
No replacement Tag form is supplied: pass an extracted original Tag editor host
implementing new_tag(), edit_tag(id), busy and close_ready(), or keep Tag actions
honestly unavailable until that extraction has landed.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from core.encyclopedia_editing import EncyclopediaEditingSession
from core.knowledge_editor_context import SelectedTagIdentityMergeService
from qt_app.category_workspace import CategoryWorkspace
from qt_app.command_bar import UiCommand


class _SelectedCategoryWorkspace(CategoryWorkspace):
    """Retain original failure recovery, then hold until explicit host reopen."""

    def create_category_dialog(self, **kwargs):
        self._creation_dialog_active = True
        try:
            return super().create_category_dialog(**kwargs)
        finally:
            self._creation_dialog_active = False

    def _merge_membership_tags(self, *args, **kwargs):
        try:self.editor_session.require_character_authority()
        except (OSError, RuntimeError, ValueError) as error:
            self._set_status(f"Review Character Knowledge before merging: {error}",announce=True)
            return False
        return super()._merge_membership_tags(*args, **kwargs)

    def _automatic_save_failed(self, message, *, operation="Change"):
        try:
            super()._automatic_save_failed(message, operation=operation)
        finally:
            # The original failure handler may replace the session with a plain
            # native session. Never allow another edit without selected binding.
            self.editor_session.reload_required = True
            self.setEnabled(False)

    def _tag_merge_failed(self, message):
        self._automatic_save_task=None
        self._automatic_save_outcome=False
        self._set_automatic_save_busy(False)
        self.editor_session.reload_required=True
        self.setEnabled(False)
        self._set_status(f"Duplicate merge completion needs saved-authority review: {message}. "
            "Keep backups/recovery evidence and reopen this selected database before editing.",announce=True)

    def reload_from_live(self):
        # Reopening is owned by the selected host, not implicit widget recovery.
        self.editor_session.reload_required = True
        self.setEnabled(False)
        return False


def create_category_workspace(session, *, parent=None, pool=None, settings=None):
    """Stock editor with explicitly prepared authority; no implicit recovery.

    The shell supplies its worker pool/operation exclusion and owns this widget.
    No library is attached and no image-update authority is manufactured here.
    """
    if type(session) is not EncyclopediaEditingSession:
        raise TypeError("Selected native editor session required.")
    if pool is None:
        raise ValueError("Supply the original host's explicitly owned Qt worker pool.")
    session.require_editor_ready()
    if session.category_session.characters is not session.characters:
        raise ValueError("Prepared Category session lost selected Character authority.")
    holder={}
    def selected_session(tags,categories,patterns,characters):
        session.require_editor_ready()
        if (tags is not session.tags or categories is not session.categories
                or patterns is not session.patterns or characters is not session.characters):
            raise ValueError("Workspace reconstruction lost selected native authority.")
        return session.category_session
    def adopt_merge(result):
        workspace=holder["workspace"]
        try:
            session.adopt_native_operation(getattr(workspace,"editor_tag_page",None),result)
        except Exception:
            session.reload_required=True
            workspace.setEnabled(False)
            raise
    workspace = _SelectedCategoryWorkspace(
        session.tags, session.categories, session.patterns,
        characters=session.characters,
        prepared_session=session.category_session, pool=pool, settings=settings,
        defer_sequence=True, defer_initial_details=True, parent=parent,
        session_factory=selected_session,
        merge_service_factory=lambda tags,categories,characters:SelectedTagIdentityMergeService(session,tags,categories,characters),
        merge_saved_adopter=adopt_merge,
    )
    holder["workspace"]=workspace
    workspace.editor_session = session
    return workspace


class EncyclopediaEditingBridge(QObject):
    """Borrow one original workspace; contribute commands without editing shell."""

    saved = Signal()
    unavailable = Signal(str)

    def __init__(self, session, *, category_workspace, tag_editor=None,
                 selected_tag=lambda: "", selected_category=lambda: "",
                 host_busy=lambda: False, parent=None):
        super().__init__(parent)
        if type(session) is not EncyclopediaEditingSession:
            raise TypeError("Selected native editor session required.")
        if (not isinstance(category_workspace, CategoryWorkspace)
                or category_workspace.session is not session.category_session
                or category_workspace.tags is not session.tags
                or category_workspace.categories is not session.categories
                or category_workspace.patterns is not session.patterns):
            raise ValueError("Borrow the original workspace with this exact prepared session.")
        self.session, self.workspace = session, category_workspace
        if tag_editor is not None and getattr(tag_editor, "session", None) is not session:
            raise ValueError("Bind the original Tag editor to this exact selected session.")
        self.tag_editor = tag_editor
        self.selected_tag, self.selected_category = selected_tag, selected_category
        self.host_busy = host_busy
        if tag_editor is None or not getattr(tag_editor,"owns_category_completion",False):
            self.workspace.routine_change_saved.connect(self._category_saved)
        if tag_editor is not None and hasattr(tag_editor, "saved"):
            tag_editor.saved.connect(self.saved.emit)
        if tag_editor is not None and hasattr(tag_editor, "unavailable"):
            tag_editor.unavailable.connect(self.unavailable.emit)
        self.commands = [
            UiCommand("encyclopedia:new-tag", "New SDXL Tag", "Home",
                      callback=self.new_tag, enabled=lambda: self.ready and self.tag_editor is not None),
            UiCommand("encyclopedia:edit-tag", "Edit Selected Tag", "Home",
                      callback=self.edit_tag, enabled=lambda: self.ready and self.tag_editor is not None
                      and self.selected_tag() in self.session.tags.tags),
            UiCommand("encyclopedia:new-category", "New Category", "Home",
                      callback=self.new_category, enabled=lambda: self.ready),
            UiCommand("encyclopedia:edit-category", "Edit Selected Category", "Home",
                      callback=self.edit_category, enabled=lambda: self.ready and bool(self.selected_category())),
        ]

    @property
    def busy(self):
        return bool(self.session.busy or self.workspace.save_in_progress
                    or self.workspace.has_active_tag_removal()
                    or self.tag_editor is not None and self.tag_editor.busy)

    @property
    def ready(self):
        return not (self.busy or self.host_busy() or self.session.closed or self.session.reload_required
                    or self.session.write_hold
                    or self.workspace.has_unsaved_changes())

    def new_tag(self):
        if self.tag_editor is None or not self._admit_entry():
            self.unavailable.emit("The original Tag Details editor needs a selected native host.")
            return False
        self.tag_editor.new_tag()
        return True

    def edit_tag(self):
        tag_id = self.selected_tag()
        if tag_id and tag_id not in self.session.tags.tags:
            self.unavailable.emit("This Tag has no admitted native editable record. Review its default override before editing.")
            return False
        if self.tag_editor is None or not tag_id or not self._admit_entry():
            self.unavailable.emit("Select a Tag and connect the original Tag Details editor.")
            return False
        self.tag_editor.edit_tag(tag_id)
        return True

    def new_category(self):
        if not self._admit_entry():
            return False
        parent_id = self.selected_category() or None
        return self.workspace.create_category_dialog(initial_parent_id=parent_id)

    def edit_category(self):
        category_id = self.selected_category()
        if self.session.categories.get(category_id) is None or not self._admit_entry():
            return False
        self.workspace.restore_navigation_context(("category", category_id))
        self.workspace._focus_category_details()
        return True

    def _admit_entry(self):
        if not self.ready:
            return False
        try:
            self.session.require_editor_ready()
        except (OSError, RuntimeError, ValueError) as error:
            self.session.reload_required = True
            self.workspace.setEnabled(False)
            self.unavailable.emit(f"Reopen this exact Tag Database before editing: {error}")
            return False
        return True

    def _category_saved(self, _change):
        try:
            self.session.adopt_category_workspace_save(self.workspace)
        except (OSError, RuntimeError, ValueError) as error:
            self.session.reload_required = True
            self.workspace.setEnabled(False)
            self.unavailable.emit(f"Category save needs a fresh saved-authority review: {error}")
            return
        self.saved.emit()

    def close_ready(self):
        if getattr(self.workspace, "_creation_dialog_active", False):
            return False
        # The owning dialog must flush its invalid/pending details close gate.
        dialog = self.workspace._category_edit_dialog
        if dialog is not None and dialog.isVisible():
            return False
        if self.busy or self.workspace.has_unsaved_changes():
            return False
        return self.tag_editor is None or bool(self.tag_editor.close_ready())

    def discard(self):
        if self.busy:
            return False
        if self.workspace._category_edit_dialog is not None:
            self.unavailable.emit("Close Category Details through its own save/discard controls first.")
            return False
        return self.workspace.discard_staged_changes()
