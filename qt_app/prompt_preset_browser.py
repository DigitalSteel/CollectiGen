"""Shared preset presentation over owner-supplied bounded descriptors.

No collection discovery, artwork loading, application or second preset store.
Collection paging stays with generation_saved_work; draft paging is in-memory.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import Qt, Signal, QSignalBlocker
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from core.generation_saved_work import prompt_preset_browser_metadata
from core.resource_browser_view import BrowserViewPreferences, load_view_preferences, preset_name_group
from .model_resource_browser import ModelResourceBrowser
from .tooltips import wrapped_tooltip


@dataclass(frozen=True)
class PromptPresetDescriptor:
    resource_id: str
    preset_id: str
    display_name: str
    browser_key: tuple[str, ...]
    browser_folders: tuple[str, ...]
    prompt_preview: str = ""
    settings_summary: str = ""
    source: str = ""
    version_label: str = ""
    author: str = ""
    last_used_at: str = ""
    size_bytes: int = 0
    browser_state: str = "Review before applying"
    available: bool = True
    saved_label: str = "In this draft"
    role: str = "prompt_preset"
    family: str = "Prompt preset"
    favorite: bool = False
    resource_kind: str = "presets"
    cached_status: str = "Saved preset"

    @property
    def search_text(self):
        return self.prompt_preview + " " + self.settings_summary


def draft_preset_descriptor(preset):
    metadata = prompt_preset_browser_metadata(preset)
    return PromptPresetDescriptor(preset.preset_id, preset.preset_id, preset.name,
        ("presets", "draft", preset.preset_id), (preset_name_group(preset.name),),
        metadata["prompt_preview"], metadata["settings_summary"], metadata["source"],
        "Supported format" if metadata["version"] in {1, 2} else "Unsupported format", metadata["author"],
        browser_state="Future version · retained, cannot apply" if preset.schema_version not in {1, 2} else "Review before applying")


def collection_preset_descriptor(item):
    metadata = item.get("browser", {})
    saved = datetime.fromtimestamp(item["modified_ns"] / 1_000_000_000)
    return PromptPresetDescriptor(item["path"], item.get("preset_id", ""), item["name"],
        ("presets", "collection", item["path"]), (preset_name_group(item["name"]),),
        metadata.get("prompt_preview", ""), metadata.get("settings_summary", ""),
        metadata.get("source", "Unknown"), ("Supported format" if metadata["version"] in {1, 2} else "Unsupported format") if "version" in metadata else "Unknown format",
        metadata.get("author", ""), saved.isoformat(), item.get("size_bytes", 0), item["summary"],
        item["available"], saved.strftime("%Y-%m-%d %H:%M"))


class PromptPresetBrowser(QWidget):
    selectionChanged = Signal()
    openRequested = Signal()
    searchRequested = Signal(str)
    pageRequested = Signal(int)

    def __init__(self, parent=None, *, scope="draft", settings=None, settings_path=None, table=None):
        super().__init__(parent)
        if scope not in {"draft", "collection"}: raise ValueError("Choose a preset browsing scope.")
        self.scope, self.settings, self.settings_path = scope, settings, settings_path
        self._rows = (); self._offset = 0; self._remembered_key = None
        self._visible_count = 0
        self._busy = False; self._rebuilding = False
        self._external_previous = self._external_next = False; self._page_number = 1; self._applied_query = ""
        self._saved_preferences = deepcopy(getattr(settings, "resource_browser_views", {}))
        self._preferences_writable = callable(getattr(settings, "update_resource_browser_view", None))
        notice = ""
        try: initial = load_view_preferences(self._saved_preferences, "presets")
        except ValueError as exc:
            initial = BrowserViewPreferences(); self._preferences_writable = False
            notice = f"Saved preset layout is unavailable; using this window's defaults. {exc}"
        layout = QVBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0)
        search_row = QHBoxLayout()
        self.search = QLineEdit(); self.search.setMaxLength(512)
        self.search.setPlaceholderText("Search collection names and details; Next searches more pages" if scope == "collection" else "Search presets in this draft")
        self.search.setAccessibleName("Search prompt preset collection" if scope == "collection" else "Search draft prompt presets")
        self.search_button = QPushButton("Search")
        self.previous_button = QPushButton("Previous")
        self.next_button = QPushButton("Next / Search More" if scope == "collection" else "Next")
        for control in (self.search, self.search_button, self.previous_button, self.next_button): search_row.addWidget(control)
        layout.addLayout(search_row)
        self.table = table if table is not None else QTreeWidget()
        self.table.setHeaderLabels(["Name", "Prompt preview", "Settings", "Source", "Format", "Saved", "State"])
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setColumnWidth(0, 230); self.table.setColumnWidth(1, 260); self.table.setColumnWidth(2, 180)
        self.table.setAccessibleName("Prompt preset details")
        self.browser = ModelResourceBrowser(self.table, initial_state=initial, resource_kind="presets")
        layout.addWidget(self.browser, 1)
        self.notice = QLabel(notice); self.notice.setWordWrap(True); self.notice.setTextFormat(Qt.PlainText)
        self.notice.setVisible(bool(notice)); layout.addWidget(self.notice)
        self.browser.viewChanged.connect(self._refresh)
        self.browser.preference_changed.connect(self._save_preferences)
        self.table.itemSelectionChanged.connect(self._selected)
        self.table.itemActivated.connect(lambda *_: self.openRequested.emit())
        self.browser.cards.itemActivated.connect(lambda *_: self.openRequested.emit())
        self.search.textChanged.connect(self._query_changed)
        self.search.returnPressed.connect(self._search)
        self.search_button.clicked.connect(self._search)
        self.previous_button.clicked.connect(lambda: self._turn_page(-1))
        self.next_button.clicked.connect(lambda: self._turn_page(1))
        self.set_rows(())

    def selected_descriptor(self):
        selected = self.table.selectedItems()
        return selected[0].data(0, Qt.UserRole) if len(selected) == 1 else None

    def select_resource(self, identity):
        row = next((row for row in self._rows if row.resource_id == identity), None)
        if row is not None:
            self._remembered_key = row.browser_key
            self._refresh()

    def clear_selection(self):
        self._remembered_key = None
        with QSignalBlocker(self.table), QSignalBlocker(self.browser.cards):
            self.table.setCurrentItem(None); self.table.clearSelection()
            self.browser.cards.setCurrentRow(-1)
        self.selectionChanged.emit()

    def set_rows(self, rows, *, page_number=1, has_previous=False, has_next=False, query=""):
        self.browser.set_rows(rows)  # shared bounded projection validates identities
        self._rows = self.browser._rows
        self._page_number, self._external_previous, self._external_next = page_number, has_previous, has_next
        self._applied_query = query
        self._refresh()

    def _selected(self):
        if self._rebuilding: return
        selected = self.selected_descriptor()
        if selected is not None: self._remembered_key = selected.browser_key
        self.selectionChanged.emit()

    def _refresh(self, *, navigation=False):
        visible = self.browser.visible_rows(query=self.search.text() if self.scope == "draft" else "")
        if self.scope == "draft":
            selected_index = next((i for i, row in enumerate(visible) if row.browser_key == self._remembered_key), None)
            if not navigation:
                self._offset = selected_index // self.browser.PAGE_LIMIT * self.browser.PAGE_LIMIT if selected_index is not None else 0
            self._offset = min(self._offset, max(0, (len(visible) - 1) // self.browser.PAGE_LIMIT * self.browser.PAGE_LIMIT))
            page = visible[self._offset:self._offset + self.browser.PAGE_LIMIT]
            count = f"{len(visible)} matching presets in this draft · {len(self._rows)} total · page {self._offset // self.browser.PAGE_LIMIT + 1}"
        else:
            page = visible
            count = f"Collection page {self._page_number} · {len(visible)} shown of {len(self._rows)} loaded. Name groups and sort apply to this page; Search and Next search other pages."
        self._rebuilding = True
        self.browser.begin_update(selected_key=self._remembered_key)
        with QSignalBlocker(self.table):
            self.table.clear()
            for row in page:
                item = QTreeWidgetItem([row.display_name, row.prompt_preview, row.settings_summary,
                                       row.source, row.version_label, row.saved_label, row.browser_state])
                item.setData(0, Qt.UserRole, row)
                for index in range(self.table.columnCount()): item.setToolTip(index, wrapped_tooltip(item.text(index)))
                item.setToolTip(0, wrapped_tooltip(f"{row.display_name}\nPreset ID: {row.preset_id}\n{row.browser_state}"))
                self.table.addTopLevelItem(item)
                if row.browser_key == self._remembered_key: self.table.setCurrentItem(item)
        self.browser.show_page(page)
        self._rebuilding = False
        if not page:
            count += " No matches; clear search or choose All name groups." if self._rows else (
                " No presets on this page; continue searching or create/import a preset." if self.scope == "collection" else
                " No draft presets yet. Create New from Draft or Open Collection.")
        self.browser.summary.setText(count)
        self._visible_count = len(visible)
        self._update_actions(len(visible))
        self.selectionChanged.emit()

    def _update_actions(self, count=None):
        idle = not self._busy
        self.search.setEnabled(idle); self.search_button.setEnabled(idle)
        if self.scope == "collection":
            applied = self.search.text().strip() == self._applied_query
            previous, following = self._external_previous and applied, self._external_next and applied
        else:
            if count is None: count = self._visible_count
            previous, following = self._offset > 0, self._offset + self.browser.PAGE_LIMIT < count
        self.previous_button.setEnabled(idle and previous); self.next_button.setEnabled(idle and following)

    def _query_changed(self, *_):
        if self.scope == "draft": self._refresh()
        else: self._update_actions()

    def _search(self):
        if self._busy: return
        if self.scope == "collection": self.searchRequested.emit(self.search.text().strip())
        else: self._refresh()

    def _turn_page(self, direction):
        if self._busy: return
        if self.scope == "collection": self.pageRequested.emit(direction)
        else:
            self._offset = max(0, self._offset + direction * self.browser.PAGE_LIMIT)
            self._refresh(navigation=True)

    def set_busy(self, busy):
        self._busy = bool(busy); self.browser.set_busy(busy); self.table.setEnabled(not busy)
        self._update_actions()

    def _save_preferences(self, changes):
        if not self._preferences_writable: return
        try:
            kwargs = {"path": self.settings_path} if self.settings_path is not None else {}
            saved = self.settings.update_resource_browser_view("presets", changes,
                expected=deepcopy(self._saved_preferences), **kwargs)
            self._saved_preferences = deepcopy(saved)
            self.notice.hide()
        except (ValueError, OSError, RuntimeError) as exc:
            self._preferences_writable = False
            self.notice.setText(f"Layout changed in this window. Saved preferences were preserved: {exc}")
            self.notice.show()
