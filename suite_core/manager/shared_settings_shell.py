"""Review-only Settings entry and original Manager close coordination.

No discovery, settings writer, parallel store or enrollment wiring.
"""
from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QGroupBox, QLabel,
                             QVBoxLayout, QPushButton, QMessageBox)
from .ui_cards import compact_action

AVAILABILITY = ("Shared settings updates are not available yet. App settings stay independent "
                "while shared updates and app reload support are reviewed.")


class SharedSettingsShell(QObject):
    def __init__(self, window, controller=None, *, submit=None, close_choice=None):
        super().__init__(window)
        self.window, self.controller = window, controller
        self.initial_selection = set(controller.selected) if controller is not None else set()
        self.submit, self.close_choice = submit, close_choice
        self.dialog = self.panel = None
        self.group = QGroupBox("Shared settings across modules", window)
        layout = QVBoxLayout(self.group)
        self.availability = QLabel(AVAILABILITY)
        self.availability.setWordWrap(True)
        self.availability.setObjectName("sharedSettingsAvailability")
        layout.addWidget(self.availability)
        self.review = compact_action(QPushButton("Review shared settings"))
        self.review.setObjectName("reviewSharedSettings")
        self.review.setEnabled(controller is not None)
        self.review.setToolTip("Review supplied recipients and settings without applying changes." if controller else
                               "No enrolled settings recipients are connected for review in this launch.")
        layout.addWidget(self.review)
        self.review.clicked.connect(self.open)
        self.timer = QTimer(self); self.timer.setInterval(40); self.timer.timeout.connect(self.poll)
        if controller is not None:
            from .shared_settings_controller import SharedSettingsController
            if type(controller) is not SharedSettingsController:
                raise TypeError("Explicit shared settings review controller required")
            controller.contract_ready = False  # Native activation/core acceptance remains separate.
        self.closing = False

    @property
    def close_held(self):
        return self.controller is not None and self.controller.close_held

    @property
    def dirty(self):
        return self.controller is not None and (
            self.controller.values != self.controller.base.values or
            self.controller.selected != self.initial_selection or
            self.panel is not None and (self.panel.editor_dirty or
                any(c.isChecked() for c in self.panel.replace_groups.values())))

    def open(self):
        if self.controller is None or self.closing or self.window.close_policy.pending:
            return
        if self.dialog is None:
            from .shared_settings_ui import SharedSettingsPanel
            self.dialog = QDialog(self.window)
            self.dialog.setWindowTitle("Review shared settings")
            self.dialog.resize(1300, 820)
            layout = QVBoxLayout(self.dialog)
            self.panel = SharedSettingsPanel(self.controller, self.dialog, submit=self.submit)
            layout.addWidget(self.panel, 1)
            actions = QDialogButtonBox(QDialogButtonBox.Close)
            actions.rejected.connect(self.dialog.reject); layout.addWidget(actions)
            self.dialog.finished.connect(self.retain_draft)
        self.dialog.show(); self.dialog.raise_()
        self.timer.start(); self.panel.render()

    def retain_draft(self, *_):
        # Dialog closure hides the editor. Its original draft/controller remains
        # owned by Manager; pending work remains polled after it is hidden.
        if self.dirty:
            self.availability.setText(AVAILABILITY + " Your draft is kept; reopen Review shared settings to continue.")
        if not self.close_held: self.timer.stop()

    def poll(self):
        if self.controller is None: return
        if self.panel is not None: self.panel.poll()
        else: self.controller.poll()
        if self.closing and not self.close_held: self.window.reconsider_close()
        if not self.close_held and not self.closing and (self.dialog is None or not self.dialog.isVisible()):
            self.timer.stop()

    def permit_close(self):
        if self.close_held:
            self.controller.cancel(); self.closing = True; self.review.setEnabled(False)
            self.controller.closed = True
            if self.panel is not None: self.panel.render()
            self.timer.start()
            self.window.statusBar().showMessage("Shared settings work is settling. Manager keeps its worker and draft until completion.")
            return False
        if self.dirty:
            discard = (self.close_choice() if self.close_choice is not None else
                       QMessageBox.question(self.window, "Unapplied shared settings draft",
                           "Close Manager and discard the unapplied shared settings draft?",
                           QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes)
            if discard is not True:
                self.resume()
                self.window.statusBar().showMessage("Close cancelled. Shared settings draft retained; reopen its review to continue.")
                return False
            # Explicit discard consumes only the retained in-memory baseline.
            # A later close hold/cancellation sees synchronized editor state;
            # storage refresh remains a separate deliberate action.
            if not self.controller.discard_draft(): return False
            self.controller.selected = set(self.initial_selection)
            if self.panel is not None:
                for check in self.panel.replace_groups.values():
                    check.blockSignals(True); check.setChecked(False); check.blockSignals(False)
                self.panel.sync_editor_from_values()
            self.availability.setText(AVAILABILITY)
        self.closing = True
        if self.controller is not None: self.controller.closed = True
        if self.panel is not None: self.panel.render()
        return True

    def resume(self):
        self.closing = False; self.review.setEnabled(self.controller is not None)
        if self.controller is not None: self.controller.closed = False
        if self.panel is not None: self.panel.render()

    def finish_close(self):
        if self.close_held: return False
        self.timer.stop()
        if self.panel is not None: self.panel.timer.stop()
        if self.dialog is not None: self.dialog.hide()
        return True
