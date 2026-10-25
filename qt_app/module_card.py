"""Presentation-only module cards using CollectiGen's existing visual language.

Proposed integration path: qt_app/module_card.py. No launch/settings authority.
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIcon, QPainter
from PySide6.QtWidgets import QSizePolicy, QWidget

from qt_app.components import StatusActionPanel
from qt_app.navigation_icons import NAVIGATION_ICON_NAMES, navigation_icon


@dataclass(frozen=True)
class ModuleCardPresentation:
    """Owner-supplied state; availability is never inferred from display text."""

    key: str
    title: str
    status: str
    detail: str
    action_text: str
    icon_name: str = "Setup"
    action_enabled: bool = False
    disabled_reason: str = "Availability has not been checked."

    def __post_init__(self):
        for name in ("key", "title", "status", "action_text"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be nonempty text")
        if not isinstance(self.detail, str) or not isinstance(self.disabled_reason, str):
            raise ValueError("detail and disabled_reason must be text")
        if type(self.action_enabled) is not bool:
            raise ValueError("action_enabled must be an explicit bool")
        if self.icon_name not in NAVIGATION_ICON_NAMES:
            raise ValueError("Use an existing CollectiGen navigation icon")
        if not self.action_enabled and not self.disabled_reason.strip():
            raise ValueError("A disabled action needs a visible reason")


class _ModuleGlyph(QWidget):
    """Paint the stock icon live so a theme switch cannot retain old pixels."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setFixedSize(28, 28)
        self.icon = QIcon()
        self.setFocusPolicy(Qt.NoFocus)

    def paintEvent(self, event):
        painter = QPainter(self)
        self.icon.paint(painter, self.rect(), Qt.AlignCenter, QIcon.Normal)
        painter.end()


class ModuleCard(StatusActionPanel):
    """One app identity, explicit state and one gated local action.

    Connect action_requested to the existing host dispatcher. The host must
    revalidate authority on dispatch; this signal never grants execution rights.
    """

    action_requested = Signal(str)

    def __init__(self, presentation: ModuleCardPresentation, parent=None):
        super().__init__(
            presentation.title, presentation.status, presentation.detail,
            presentation.action_text, self._request_action, parent,
            primary=True, action_width=150,
        )
        self._key = presentation.key
        self.glyph = _ModuleGlyph(self.card)
        heading = self.card.layout().itemAt(0).layout()
        heading.insertWidget(0, self.glyph)
        heading.setStretch(1, 1)
        heading.setStretch(2, 0)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.title_label.setWordWrap(True)
        for label in (self.title_label, self.status_label, self.detail_label):
            label.setTextFormat(Qt.PlainText)
        self.set_presentation(presentation)

    @property
    def module_key(self):
        return self._key

    def set_presentation(self, presentation: ModuleCardPresentation):
        """Update the same module without rebuilding widgets or signal bindings."""
        if presentation.key != self._key:
            raise ValueError("Replace the card to change module identity")
        self.presentation = presentation
        self.title_label.setText(presentation.title)
        self.status_label.setText(presentation.status)
        detail = presentation.detail.strip()
        if not presentation.action_enabled:
            reason = presentation.disabled_reason.strip()
            if reason not in detail:
                detail = "\n".join(part for part in (detail, reason) if part)
        self.detail_label.setText(detail)
        self.detail_label.setVisible(bool(detail))
        self.action_button.setText(presentation.action_text)
        self.action_button.setEnabled(presentation.action_enabled)
        self.action_button.setToolTip(
            "" if presentation.action_enabled else presentation.disabled_reason
        )
        self.action_button.setAccessibleName(
            f"{presentation.action_text}: {presentation.title}"
        )
        self.setAccessibleName(presentation.title)
        self.setAccessibleDescription(". ".join(
            part for part in (presentation.status, detail) if part
        ))
        self.glyph.icon = navigation_icon(presentation.icon_name)
        self.glyph.update()

    def _request_action(self):
        if self.presentation.action_enabled and self.action_button.isEnabled():
            self.action_requested.emit(self._key)

