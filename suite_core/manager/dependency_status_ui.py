"""Presentation and routing only; the host owns status reads and recovery actions."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QWidget, QLabel, QScrollArea, QFrame, QVBoxLayout

from core.manager_dependency_status import DependencyStatusSnapshot, project_dependency
from qt_app.module_card import ModuleCard, ModuleCardPresentation
from suite_core.manager.ui_cards import workspace_layout, workspace_heading


class DependencyStatusPanel(QWidget):
    route_requested = Signal(str, str)

    def __init__(self, snapshot=None, parent=None):
        super().__init__(parent)
        self.cards = {}
        self._dispatched = set()
        self.snapshot = None
        body = workspace_layout(self)
        body.addWidget(workspace_heading('Dependencies'))
        intro = QLabel('Review supplied status, then open the component’s existing setup or recovery workspace. Nothing is checked or changed when this page opens.')
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.PlainText)
        intro.setObjectName('mutedLabel')
        body.addWidget(intro)
        self.empty = QLabel('No dependency status connected. Requirements and current readiness are unknown. Review available connection and setup tools in Applications.')
        self.empty.setWordWrap(True)
        self.empty.setTextFormat(Qt.PlainText)
        body.addWidget(self.empty)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        self.rows_layout = QVBoxLayout(content)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(8)
        self.rows_layout.addStretch(1)
        self.scroll.setWidget(content)
        body.addWidget(self.scroll, 1)
        self.set_snapshot(snapshot if snapshot is not None else DependencyStatusSnapshot('empty'))

    def set_snapshot(self, snapshot):
        if type(snapshot) is not DependencyStatusSnapshot:
            raise ValueError('Explicit dependency status snapshot required')
        if self.snapshot is not None and self.snapshot.revision == snapshot.revision and self.snapshot != snapshot:
            raise ValueError('Changed dependency observations require a new revision')
        if self.snapshot is None or self.snapshot.revision != snapshot.revision:
            self._dispatched.clear()
        self.snapshot = snapshot
        wanted = {row.key for row in snapshot.observations}
        for key in tuple(self.cards):
            if key not in wanted:
                card = self.cards.pop(key)
                self.rows_layout.removeWidget(card)
                card.hide()
                card.deleteLater()
        for index, row in enumerate(snapshot.observations):
            presentation = self._presentation(row)
            if row.key not in self.cards:
                card = ModuleCard(presentation)
                card.action_requested.connect(self._dispatch)
                self.cards[row.key] = card
            else:
                card = self.cards[row.key]
                card.set_presentation(presentation)
                self.rows_layout.removeWidget(card)
            self.rows_layout.insertWidget(index, card)
        self.empty.setVisible(not wanted)
        self.scroll.setVisible(bool(wanted))

    def _presentation(self, row):
        view = project_dependency(row)
        sent = row.key in self._dispatched
        return ModuleCardPresentation(row.key, row.component, view.status, view.detail,
            view.action_text, 'Setup', view.action_enabled and not sent,
            'Setup workspace requested; waiting for the next status observation.' if sent else view.disabled_reason)

    def _dispatch(self, key):
        row = next((row for row in self.snapshot.observations if row.key == key), None)
        if row is None or key in self._dispatched or not project_dependency(row).action_enabled:
            return
        self._dispatched.add(key)
        self.cards[key].set_presentation(self._presentation(row))
        self.route_requested.emit(key, row.route.value)
