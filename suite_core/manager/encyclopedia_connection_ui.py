"""Reviewed existing reader connection, separate from Tag data import."""
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QFileDialog)
from core.encyclopedia_connection import ReaderConnectionStore, OwnedEncyclopedia, review_directory
from .ui_cards import compact_action, workspace_layout


class EncyclopediaConnectionPanel(QWidget):
    def __init__(self, settings_path, *, normal_entry_session=None, parent=None, owner=None):
        super().__init__(parent)
        self.store = ReaderConnectionStore(settings_path)
        self.owner = owner or OwnedEncyclopedia(launch_allowed=normal_entry_session is None,
            normal_entry_session=normal_entry_session)
        if normal_entry_session is not None and getattr(normal_entry_session,'reader_selection',None) is None:
            self.owner.launch_allowed = False
        self.reviewed = self.selected = self.snapshot = None
        self.reader_settled = None
        self.last_owner_reason = self.owner.reason
        layout = workspace_layout(self)
        title = QLabel("Connect an existing Tag Encyclopedia"); title.setObjectName("sectionHeading")
        layout.addWidget(title)
        scope = QLabel("Choose an approved, already extracted Windows x64 reader package. Review checks every declared file. Save connects it; Launch opens an empty, separate reader. No download, installation or Tag import.")
        scope.setWordWrap(True); layout.addWidget(scope)
        self.directory = QLineEdit(); self.directory.setObjectName("encyclopediaPackageDirectory")
        self.directory.setPlaceholderText("Extracted directory containing BOOTSTRAP_INVENTORY.json")
        row = QHBoxLayout(); row.addWidget(self.directory)
        self.browse = compact_action(QPushButton("Browse")); row.addWidget(self.browse); layout.addLayout(row)
        self.browse.clicked.connect(self.choose_directory)
        self.directory.textChanged.connect(self.invalidate)
        self.status = QLabel(); self.status.setWordWrap(True); layout.addWidget(self.status)
        self.details = QLabel("Inventory checks establish unchanged bytes; they do not authenticate the publisher. Select only a package approved for execution. Never select Local Only material.")
        self.details.setWordWrap(True); layout.addWidget(self.details)
        actions = QHBoxLayout()
        self.review = compact_action(QPushButton("Review package"))
        self.reload = compact_action(QPushButton("Reload saved"))
        self.save = compact_action(QPushButton("Save connection"))
        self.launch = compact_action(QPushButton("Launch reader"))
        self.stop = compact_action(QPushButton("Stop retained reader"))
        for button, slot in ((self.review,self.review_package),(self.reload,self.reload_saved),
                (self.save,self.save_connection),(self.launch,self.launch_reader),(self.stop,self.stop_reader)):
            actions.addWidget(button); button.clicked.connect(slot)
        layout.addLayout(actions); layout.addStretch()
        self.timer = QTimer(self); self.timer.setInterval(150); self.timer.timeout.connect(self.poll)
        self.timer.start(); self.reload_saved()

    def invalidate(self):
        self.reviewed = None
        self.save.setEnabled(False)

    def choose_directory(self):
        path = QFileDialog.getExistingDirectory(self, "Select an approved extracted reader package")
        if path:self.directory.setText(path)

    def error(self, action, error):
        self.status.setText(f"{action} held. {error}")

    def restart_required(self):
        session = self.owner.normal_entry_session
        if self.selected is None or session is None:
            return False
        startup = getattr(session, "reader_selection", None)
        return startup is None or getattr(startup, "selected", None) != self.selected

    def saved_status(self):
        if self.restart_required():
            return f"Reader {self.selected.version} connected. Restart Manager to launch this reader."
        if not self.owner.launch_allowed:
            return f"Reader {self.selected.version} connected. Launch awaits this Manager's reader admission."
        return f"Reader {self.selected.version} connected. Launch reader when ready."

    def reload_saved(self):
        if self.owner.close_held:return
        try:
            self.snapshot = self.store.observe()
            self.selected = self.store.selected(self.snapshot)
            self.directory.setText("" if self.selected is None else str(self.selected.directory))
            self.status.setText("No reader connected. Select its extracted package, then Review package." if self.selected is None else
                self.saved_status())
        except Exception as error:
            self.selected = None; self.snapshot = None; self.error("Reload",error)
        self.render()

    def review_package(self):
        try:
            self.snapshot = self.store.observe()
            self.reviewed = review_directory(Path(self.directory.text()))
            self.status.setText(f"Reviewed reader {self.reviewed.version}. Save connection preserves this exact package; it does not launch it.")
        except Exception as error:
            self.reviewed = None; self.error("Review",error)
        self.render()

    def save_connection(self):
        if self.reviewed is None:return
        try:
            self.snapshot = self.store.save(self.reviewed,expected=self.snapshot)
            self.selected = self.reviewed; self.reviewed = None
            self.status.setText(self.saved_status())
        except Exception as error:self.error("Save",error)
        self.render()

    def launch_reader(self):
        try:
            # A different saved connection is not silently executed.
            current = self.store.observe()
            if self.snapshot is None or current.digest != self.snapshot.digest:
                raise ValueError("Settings changed. Reload saved before launching.")
            if self.restart_required():
                self.status.setText(self.saved_status()); self.render(); return
            self.owner.launch(self.selected); self.status.setText(self.owner.reason)
        except Exception as error:self.error("Launch",error)
        self.render()

    def stop_reader(self):
        self.owner.stop_failed_start(); self.status.setText(self.owner.reason); self.render()

    def poll(self):
        had_native = self.owner.native is not None
        try:self.owner.poll()
        except Exception as error:self.error("Reader cleanup",error)
        if had_native and self.owner.native is None and self.reader_settled is not None:
            self.reader_settled()
        if self.owner.reason != self.last_owner_reason:
            self.last_owner_reason = self.owner.reason
            self.status.setText(self.saved_status() if self.restart_required() and not self.owner.close_held else self.owner.reason)
        self.render()

    def render(self):
        held = self.owner.close_held
        for button in (self.review,self.reload,self.browse):button.setEnabled(not held)
        self.directory.setEnabled(not held)
        self.save.setEnabled(not held and self.reviewed is not None)
        self.launch.setEnabled(not held and self.selected is not None and self.owner.launch_allowed and not self.restart_required())
        self.launch.setToolTip("Restart Manager to launch this reader; its original root admission is required." if self.restart_required() or not self.owner.launch_allowed else
            "Recheck and launch only the saved reader. It opens without a snapshot.")
        self.stop.setEnabled(held)


def supervise_reader_recovery(held):
    """Reuse the original window, panel and native owner after failed entry."""
    from PySide6.QtWidgets import QApplication
    original = held.owner.window
    if (original is None or original.reader_panel is None
            or original.reader_panel.owner is not held.owner.reader_owner):
        raise ValueError("Original reader recovery surface is unavailable.")
    app = QApplication.instance()
    original.setWindowTitle("CollectiGen needs attention")
    original.reader_panel.status.setText("Manager encountered a problem. Close the reader window or explicitly Stop retained reader to finish cleanup.")
    original.show()
    acquisition = getattr(held.owner, 'acquisition_panel', None)
    if acquisition is not None:
        if acquisition is not getattr(original, 'acquisition_panel', None):
            raise ValueError("Original acquisition recovery surface is unavailable.")
        acquisition.status.setText("Manager encountered a problem. Cancel and wait for this original acquisition worker to settle before closing.")
        original.open_reader_acquisition()
        acquisition.request_cancel()
    else:
        original.open_reader_connection()
    timer = QTimer(original); timer.setInterval(100)
    def settle():
        original.reader_panel.poll()
        if acquisition is not None and not acquisition.close_ready():return
        if not held.owner._drain_acquisition():return
        if held.owner.reader_owner.close_held:return
        original.close()
        if not original.isVisible() and held.owner.poll_cleanup():
            timer.stop(); app.quit()
    timer.timeout.connect(settle); timer.start()
    app.exec()
    timer.stop()
    if not held.owner.poll_cleanup():
        raise RuntimeError("Original reader cleanup remains held.")
    return 1
