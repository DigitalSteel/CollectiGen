from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import Qt, QSignalBlocker
from PySide6.QtWidgets import (
    QAbstractButton, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)

from core.command_search import SearchEntry, rank_commands


@dataclass
class _Destination:
    section: str
    label: str
    callback: Callable[[], None] | None = None
    control: QWidget | None = None
    keywords: tuple[str, ...] = ()


class SettingsNavigator(QWidget):
    """Navigate existing editors without rebuilding, saving or starting work."""

    def __init__(self, parent=None, *, accessible_name="Settings sections"):
        super().__init__(parent)
        self._sections: dict[str, _Destination] = {}
        self._targets: list[_Destination] = []
        self.current_section = ""
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(14)
        side = QWidget()
        side.setMinimumWidth(190)
        side.setMaximumWidth(270)
        column = QVBoxLayout(side)
        column.setContentsMargins(0, 0, 0, 0)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Find a setting…")
        self.search.setAccessibleName("Find a setting in " + accessible_name)
        self.search.setClearButtonEnabled(True)
        column.addWidget(self.search)
        self.section_list = QListWidget()
        self.section_list.setAccessibleName(accessible_name)
        self.section_list.setWordWrap(True)
        self.section_list.setSpacing(3)
        self.section_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        column.addWidget(self.section_list, 1)
        self.empty = QLabel("No matching settings. Try another word.")
        self.empty.setWordWrap(True)
        self.empty.setObjectName("mutedLabel")
        self.empty.hide()
        column.addWidget(self.empty)
        row.addWidget(side)
        self._content_layout = QVBoxLayout()
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        row.addLayout(self._content_layout, 1)
        self._content = None
        self.search.textChanged.connect(self._rebuild)
        self.search.returnPressed.connect(self._activate_first_result)
        self.section_list.currentItemChanged.connect(self._item_changed)

    def set_content(self, widget):
        if self._content is not None:
            self._content_layout.removeWidget(self._content)
        self._content = widget
        self._content_layout.addWidget(widget, 1)

    def add_section(self, key, label, activate_callback, *, keywords=()):
        if key in self._sections:
            raise ValueError(f"Duplicate settings section: {key}")
        self._sections[key] = _Destination(key, label, activate_callback,
                                           keywords=tuple(keywords))
        self._rebuild()

    def add_target(self, section_key, label, control, *, keywords=(), activate=None):
        if section_key not in self._sections:
            raise ValueError(f"Unknown settings section: {section_key}")
        self._targets.append(_Destination(section_key, label, activate, control,
                                          tuple(keywords)))
        self._rebuild()

    def destinations(self) -> tuple[_Destination, ...]:
        """Return the existing destinations for shared command navigation."""
        return (*self._sections.values(), *self._targets)

    def activate_destination(self, destination: _Destination):
        """Activate a destination owned by this navigator."""
        if not any(candidate is destination for candidate in self.destinations()):
            raise ValueError("Settings destination does not belong to this navigator")
        self._activate(destination)

    def _rebuild(self, *_args):
        query = self.search.text()
        blocker = QSignalBlocker(self.section_list)
        self.section_list.clear()
        destinations = list(self._sections.values())
        if query.strip():
            candidates = self._targets + destinations
            entries = []
            by_key = {}
            for index, destination in enumerate(candidates):
                section = self._sections[destination.section]
                key = f"d:{index}"
                entries.append(SearchEntry(
                    key=key,
                    label=destination.label,
                    section=section.label if destination is not section else "",
                    keywords=destination.keywords,
                    context=section.label,
                ))
                by_key[key] = destination
            destinations = [
                by_key[entry.key]
                for entry in rank_commands(
                    entries, query, context=self.current_section,
                    limit=len(entries),
                )
            ]
        for destination in destinations:
            section = self._sections[destination.section]
            label = destination.label
            if destination.control is not None:
                label += "\n" + section.label
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, destination)
            item.setToolTip(destination.label + " — " + section.label)
            self.section_list.addItem(item)
            if not query.strip() and destination.section == self.current_section:
                self.section_list.setCurrentItem(item)
        self.empty.setVisible(self.section_list.count() == 0)
        del blocker

    def _activate_first_result(self):
        item = self.section_list.currentItem() or self.section_list.item(0)
        if item is not None:
            if self.section_list.currentItem() is item:
                self.activate_destination(item.data(Qt.UserRole))
            else:
                self.section_list.setCurrentItem(item)

    def _item_changed(self, item, _previous):
        if item is not None:
            self.activate_destination(item.data(Qt.UserRole))

    def select_section(self, key) -> bool:
        destination = self._sections.get(key)
        if destination is None:
            return False
        self.activate_destination(destination)
        return True

    def sync_section(self, key):
        if key in self._sections:
            self.current_section = key
            self._rebuild()

    def _activate(self, destination):
        section = self._sections[destination.section]
        self.current_section = section.section
        section.callback()
        if destination is not section and destination.callback is not None:
            destination.callback()
        if destination.control is not None:
            self._reveal(destination.control)
        if not self.search.text():
            self._rebuild()

    @staticmethod
    def _reveal(control):
        # Open containing tabs and existing disclosure controls, outermost first.
        ancestors = []
        child = control
        while child.parentWidget() is not None:
            parent = child.parentWidget()
            ancestors.append((parent, child))
            child = parent
        for parent, child in reversed(ancestors):
            if isinstance(parent, QTabWidget):
                for candidate, _nested in ancestors:
                    index = parent.indexOf(candidate)
                    if index >= 0:
                        parent.setCurrentIndex(index)
                        break
        window = control.window()
        for button in window.findChildren(QAbstractButton):
            target = getattr(button, "target", None)
            if target is None:
                target = getattr(button, "_target", None)
            if isinstance(target, QWidget) and (target is control or target.isAncestorOf(control)):
                if button.isCheckable():
                    button.setChecked(True)
        control.setFocus(Qt.OtherFocusReason)
        for parent, _child in ancestors:
            if isinstance(parent, QScrollArea):
                parent.ensureWidgetVisible(control, 16, 16)
