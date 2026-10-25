"""Bounded folder/category presentation for authorized resource descriptors.

The browser accepts projected ModelSelectionRow values and optionally loads
only validated, owned artwork thumbnails through a bounded page loader.
The supplied table retains selection authority for the owning dialog.
"""
from __future__ import annotations

import ntpath
from dataclasses import asdict
from itertools import islice

from PySide6.QtCore import QSignalBlocker, QSize, Qt, Signal, QTimer
from PySide6.QtGui import QPainter, QPen, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
    QSplitter, QStackedWidget, QStyledItemDelegate, QStyle, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)
from core.resource_browser_view import (
    MAX_ROWS, BrowserViewPreferences, folder_key, normalize_view_preferences, order_visible, project_rows, resource_key,
)
from .tooltips import wrapped_tooltip

VIEW_WIDTHS = (206, 232, 264, 300, 344, 392)
_KEEP_SELECTION = object()


def _row_key(row):
    return resource_key(row)


def _folder_key(value):
    """Normalize registered path text lexically; never probe the filesystem."""
    return folder_key(value)


def _row_folders(row):
    explicit = getattr(row, "browser_folders", None)
    if explicit is not None: return {_folder_key(folder) for folder in explicit if folder}
    return {_folder_key(ntpath.dirname(str(path))) for path in row.locations if path}


def _is_within(folder, root):
    return bool(root) and (folder == root or folder.startswith(root.rstrip("\\") + "\\"))


def _role_label(role):
    if role == "lora":
        return "LoRA"
    if role == "vae":
        return "VAE"
    return str(role).replace("_", " ").title()


def _compatibility_label(row, contextual):
    if not contextual:
        return ""
    state = getattr(row, "context_compatibility", "unknown")
    if state == "compatible":
        return "With selected model: Compatible"
    if state == "incompatible":
        return "With selected model: Incompatible"
    return "With selected model: Compatibility unknown"


def _card_state_line(row, contextual):
    if getattr(row, "resource_kind", "models") == "presets":
        return getattr(row, "browser_state", "Saved preset · review before applying")
    cached = "Stored" if row.cached_status == "stored" else "No local location"
    if not contextual:
        return "Stored · verify to use" if row.cached_status == "stored" else cached
    state = getattr(row, "context_compatibility", "unknown")
    if state == "compatible":
        return f"Compatible with selected model · {cached}"
    if state == "incompatible":
        return f"Incompatible with selected model · {cached}"
    return f"Compatibility unknown · {cached}"


class _ResourceCardDelegate(QStyledItemDelegate):
    """Paint bounded metadata and policy-filtered, validated artwork."""

    def __init__(self, parent=None, *, contextual=False, presets=False):
        super().__init__(parent)
        self.contextual = contextual
        self.presets = presets
        self.mode, self.width = "cards", VIEW_WIDTHS[2]
        self.artwork_loader = None

    def sizeHint(self, option, index):
        return QSize(self.width, self.width + 48 if self.mode == "thumbnails" else 214 if self.presets else 312)

    def paint(self, painter, option, index):
        row = index.data(Qt.UserRole)
        if row is None:
            return
        painter.save()
        painter.setFont(option.font)
        rect = option.rect.adjusted(5, 5, -5, -5)
        palette = option.palette
        selected = bool(option.state & QStyle.State_Selected)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(palette.highlight().color().darker(125) if selected else palette.mid().color())
        painter.setBrush(palette.highlight().color() if selected else palette.base().color())
        painter.drawRoundedRect(rect, 7, 7)
        foreground = palette.highlightedText().color() if selected else palette.text().color()
        painter.setPen(foreground)
        text_rect = rect.adjusted(12, 9, -12, -8)
        metrics = option.fontMetrics
        role = _role_label(row.role)
        loader = self.artwork_loader
        artwork = loader.display_pixmap(str(row.resource_id)) if loader is not None else None
        artwork_label = loader.label(str(row.resource_id)) if loader is not None else "No qualified preview"
        if self.mode == "thumbnails":
            preview = rect.adjusted(12, 12, -12, -66)
            painter.setPen(palette.mid().color()); painter.setBrush(palette.alternateBase().color())
            painter.drawRoundedRect(preview, 4, 4)
            painter.setPen(palette.text().color())
            if artwork is not None and not artwork.isNull():
                scaled = artwork.scaled(preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
                painter.drawPixmap(preview.x() + (preview.width() - scaled.width()) // 2,
                                   preview.y() + (preview.height() - scaled.height()) // 2, scaled)
            else:
                painter.drawText(preview, Qt.AlignCenter | Qt.TextWordWrap, f"{role}\nNo artwork" if self.presets else f"{role}\n{artwork_label}")
            painter.setPen(foreground)
            bottom = rect.bottom() - 47
            painter.drawText(text_rect.left(), bottom, metrics.elidedText(str(row.display_name), Qt.ElideRight, text_rect.width()))
            subtitle = getattr(row, "source", "Local preset") if self.presets else row.family or "Family unknown"
            painter.drawText(text_rect.left(), bottom + 22, metrics.elidedText(str(subtitle), Qt.ElideRight, text_rect.width()))
            painter.restore(); return
        if not self.presets:
            preview = rect.adjusted(12, 10, -12, 0)
            preview.setHeight(116)
            painter.setPen(palette.mid().color()); painter.setBrush(palette.alternateBase().color())
            painter.drawRoundedRect(preview, 4, 4)
            if artwork is not None and not artwork.isNull():
                scaled = artwork.scaled(preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
                painter.drawPixmap(preview.x() + (preview.width() - scaled.width()) // 2,
                                   preview.y() + (preview.height() - scaled.height()) // 2, scaled)
            else:
                painter.setPen(palette.text().color())
                painter.drawText(preview, Qt.AlignCenter | Qt.TextWordWrap, artwork_label)
            text_rect = text_rect.adjusted(0, 128, 0, 0)
        mark = "P" if self.presets else "L" if row.role == "lora" else "V" if row.role == "vae" else "M"
        badge = text_rect.adjusted(0, 0, -text_rect.width() + 27, -text_rect.height() + 27)
        painter.setPen(Qt.NoPen)
        painter.setBrush(palette.highlightedText().color() if selected else palette.highlight().color())
        painter.drawRoundedRect(badge, 5, 5)
        painter.setPen(palette.highlight().color() if selected else palette.highlightedText().color())
        painter.drawText(badge, Qt.AlignCenter, mark)
        painter.setPen(foreground)
        title = metrics.elidedText(str(row.display_name), Qt.ElideRight, text_rect.width() - 38)
        painter.drawText(text_rect.adjusted(36, 0, 0, -text_rect.height() + 27), Qt.AlignVCenter, title)
        painter.setPen(foreground)
        if self.presets:
            preview = getattr(row, "prompt_preview", "") or "No positive prompt text"
            painter.drawText(text_rect.adjusted(0, 39, 0, -text_rect.height() + 93), Qt.TextWordWrap, preview)
            lines = (getattr(row, "settings_summary", "No scalar settings"),
                     f"{getattr(row, 'version_label', '')} · {getattr(row, 'source', 'Local preset')}",
                     _card_state_line(row, False), _folder_summary(row))
            positions = (103, 125, 147, 169)
        else:
            lines = (
            getattr(row, "declaration_summary", "") or f"{role} · {row.family or 'Family unknown'}",
            ("Favorite · " if row.favorite else "") + (getattr(row, "author", "") or "Author unknown"),
            getattr(row, "version_label", "") or "Version unknown",
            _card_state_line(row, self.contextual),
            _folder_summary(row),
            )
            positions = (40, 62, 84, 106, 128)
        for line, y in zip(lines, positions):
            painter.drawText(text_rect.adjusted(0, y, 0, -text_rect.height() + y + 19),
                             Qt.AlignVCenter, metrics.elidedText(line, Qt.ElideRight, text_rect.width()))
        if option.state & QStyle.State_HasFocus:
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(foreground, 1, Qt.DotLine))
            painter.drawRoundedRect(rect.adjusted(3, 3, -3, -3), 5, 5)
        painter.restore()


def _folder_summary(row):
    folders = sorted(_row_folders(row))
    if not folders:
        return "No registered folder"
    label = ntpath.basename(folders[0].rstrip("\\")) or folders[0]
    return f"{label} +{len(folders) - 1} folders" if len(folders) > 1 else label


class ModelResourceBrowser(QWidget):
    """Four shared views; the supplied table owns exact resource selection."""

    viewChanged = Signal()
    preference_changed = Signal(dict)
    PAGE_LIMIT = 100

    def __init__(self, table: QTreeWidget, parent=None, *, contextual=False, initial_state=None, resource_kind="models",
                 table_value_to_row=None, name_column=1, compact_columns=(0, 1)):
        super().__init__(parent)
        self.table = table
        self._table_value_to_row = table_value_to_row or (lambda value: value)
        self.name_column = name_column
        self.compact_columns = tuple(compact_columns)
        self.contextual = bool(contextual)
        if resource_kind not in {"models", "presets"}: raise ValueError("Choose a supported resource browser context.")
        self.resource_kind = resource_kind
        self._all_folders_label = "All name groups" if resource_kind == "presets" else "All folders"
        self._folder_labels = {}
        self._rows = ()
        self.artwork_loader = None
        self._busy = False
        self._folder = ""
        self._picker_chrome_visible = True
        self._selected_key = None
        self._table_rebuilding = False
        self._views = ()
        self._folder_items = {}
        self._scrolls = {}
        self._mode = "cards"
        self._presentation_closed = False
        self._scroll_value = None
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.timeout.connect(self._apply_pending_scroll)
        state = initial_state or BrowserViewPreferences()
        self._state = normalize_view_preferences(asdict(state) if isinstance(state, BrowserViewPreferences) else state)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        controls = QHBoxLayout()
        self.view_mode = QComboBox()
        self.view_mode.addItem("Cards", "cards")
        self.view_mode.addItem("Thumbnails", "thumbnails")
        self.view_mode.addItem("List", "list")
        self.view_mode.addItem("Details List", "details")
        self.view_mode.setAccessibleName("Preset view" if resource_kind == "presets" else "Model resource view")
        self.sort_order = QComboBox()
        for label, key in (("Default order", "recommended"), ("Name", "name"),
                           ("Family", "family"), ("Recently selected", "recent"), ("Author", "author"), ("Size", "size")):
            self.sort_order.addItem(label, key)
        if resource_kind == "presets":
            self.sort_order.setItemText(self.sort_order.findData("family"), "Preset type")
            self.sort_order.setItemText(self.sort_order.findData("recent"), "Recently saved")
        self.sort_order.setAccessibleName("Sort presets" if resource_kind == "presets" else "Sort model resources")
        controls.addWidget(self.view_mode)
        controls.addWidget(QLabel("View size"))
        self.density = QComboBox(); self.density.setAccessibleName("Resource view size")
        for index, label in enumerate(("Smallest", "Small", "Standard", "Large", "Larger", "Largest")):
            self.density.addItem(label, index)
        controls.addWidget(self.density)
        controls.addWidget(self.sort_order)
        self.reverse = QCheckBox("Reverse"); controls.addWidget(self.reverse)
        controls.addStretch(1)
        self.presentation_controls = QWidget()
        self.presentation_controls.setLayout(controls)
        controls.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.presentation_controls)
        navigation = QHBoxLayout()
        self.navigation_toggle = QPushButton("Name Groups" if resource_kind == "presets" else "Folders")
        self.navigation_toggle.setCheckable(True); self.navigation_toggle.setChecked(self._state.navigation_visible)
        self.navigation_toggle.setAccessibleName("Show logical name groups" if resource_kind == "presets" else "Show registered folders")
        navigation.addWidget(self.navigation_toggle)
        self.up = QPushButton("Up"); self.up.clicked.connect(self._folder_up)
        self.breadcrumbs = QComboBox(); self.breadcrumbs.setAccessibleName("Logical name-group breadcrumbs" if resource_kind == "presets" else "Folder breadcrumbs")
        self.descendants = QCheckBox("Include descendants")
        self.depth = QComboBox(); self.depth.setAccessibleName("Maximum descendant folder depth")
        for label, value in (("All depths", 0), *((f"{value} level(s)", value) for value in (1, 2, 3, 4, 8, 16, 32))):
            self.depth.addItem(label, value)
        if self.depth.findData(self._state.max_depth) < 0: self.depth.addItem(f"{self._state.max_depth} levels", self._state.max_depth)
        navigation.addWidget(self.up); navigation.addWidget(self.breadcrumbs, 1)
        navigation.addWidget(self.descendants); navigation.addWidget(self.depth)
        self.folder_navigation = QWidget()
        self.folder_navigation.setLayout(navigation)
        navigation.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.folder_navigation)
        self.summary = QLabel(); self.summary.setWordWrap(True); self.summary.setTextFormat(Qt.PlainText)
        outer.addWidget(self.summary)
        self.splitter = QSplitter(Qt.Horizontal)
        self.folders = QTreeWidget()
        self.folders.setHeaderHidden(True)
        self.folders.setAccessibleName("Logical preset name groups" if resource_kind == "presets" else "Registered model folders")
        self.folders.setMinimumWidth(170)
        self.folders.setMaximumWidth(300)
        self.splitter.addWidget(self.folders)
        self.stack = QStackedWidget()
        self.cards = QListWidget()
        self.cards.setAccessibleName("Prompt preset cards" if resource_kind == "presets" else "Model resource cards")
        self.cards.setViewMode(QListWidget.IconMode)
        self.cards.setFlow(QListWidget.LeftToRight)
        self.cards.setWrapping(True)
        self.cards.setResizeMode(QListWidget.Adjust)
        self.cards.setMovement(QListWidget.Static)
        self.cards.setSpacing(6)
        self.cards.setGridSize(QSize(270, 158))
        self.delegate = _ResourceCardDelegate(self.cards, contextual=self.contextual, presets=resource_kind == "presets")
        self.cards.setItemDelegate(self.delegate)
        self.cards.setSelectionMode(QAbstractItemView.SingleSelection)
        self.stack.addWidget(self.cards)
        self.stack.addWidget(self.table)
        self.splitter.addWidget(self.stack)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes((210, 690))
        outer.addWidget(self.splitter, 1)
        self.view_mode.setCurrentIndex(self.view_mode.findData(self._state.mode))
        self.density.setCurrentIndex(self._state.density)
        self.sort_order.setCurrentIndex(self.sort_order.findData(self._state.sort_key))
        self.reverse.setChecked(self._state.reverse); self.descendants.setChecked(self._state.include_descendants)
        self.depth.setCurrentIndex(self.depth.findData(self._state.max_depth))
        self.view_mode.currentIndexChanged.connect(self._mode_changed)
        self.density.currentIndexChanged.connect(self._density_changed)
        for control in (self.sort_order, self.depth): control.currentIndexChanged.connect(self._ordering_changed)
        self.reverse.toggled.connect(self._ordering_changed); self.descendants.toggled.connect(self._ordering_changed)
        self.breadcrumbs.currentIndexChanged.connect(self._breadcrumb_changed)
        self.folders.itemSelectionChanged.connect(self._folder_changed)
        self.cards.currentItemChanged.connect(self._card_selected)
        self.table.itemSelectionChanged.connect(self._table_selected)
        self.table.model().modelAboutToBeReset.connect(self._table_will_reset)
        self.navigation_toggle.toggled.connect(self._navigation_changed)
        self.folders.setVisible(self._state.navigation_visible)
        self.set_rows(())
        self._mode_changed(emit=False)

    def set_rows(self, all_rows):
        """Rebuild folder navigation from row locations, preserving a valid filter."""
        rows = tuple(islice(iter(all_rows), MAX_ROWS + 1))
        views = project_rows(rows)
        self._rows, self._views = rows, views
        self._by_key = {_row_key(row): row for row in self._rows}
        self._folder_labels = {}
        if self.resource_kind == "presets":
            for row in rows:
                for original in getattr(row, "browser_folders", ()):
                    for end in range(1, len(original.split("\\")) + 1):
                        display = "\\".join(original.split("\\")[:end])
                        self._folder_labels.setdefault(_folder_key(display), ntpath.basename(display))
        membership = {}
        leaf_folders = set()
        edges = 0
        self._folder_limit_notice = ""
        for view in self._views:
            key = view.key
            for folder in view.folders:
                leaf_folders.add(folder)
                while folder:
                    membership.setdefault(folder, set()).add(key)
                    edges += 1
                    if len(membership) > 10_000 or edges > 200_000:
                        self._folder_limit_notice = " Folder tree exceeds its display limit; narrow the search to browse folders."
                        break
                    parent = _folder_key(ntpath.dirname(folder))
                    if not parent or parent == folder:
                        break
                    folder = parent
                if self._folder_limit_notice: break
            if self._folder_limit_notice: break
        if self._folder_limit_notice:
            membership.clear(); leaf_folders.clear()
        groups = {}
        for folder in leaf_folders:
            groups.setdefault(ntpath.splitdrive(folder)[0], []).append(folder)
        roots = []
        for folders in groups.values():
            try:
                common = _folder_key(ntpath.commonpath(folders))
                roots.extend([common] if common else [folder for folder in membership
                    if _folder_key(ntpath.dirname(folder)) not in membership])
            except ValueError:
                roots.extend(folders)
        root_set = set(roots)
        if self.resource_kind == "presets":
            # Logical name groups retain every ancestor even when one branch
            # contains all presets; filesystem common-root shortening is wrong here.
            roots = [folder for folder in membership
                     if _folder_key(ntpath.dirname(folder)) not in membership]
            root_set = set(roots)
        shown_folders = set()
        for folder in sorted(membership, key=lambda value: (value.count("\\"), value)):
            if folder in root_set or _folder_key(ntpath.dirname(folder)) in shown_folders:
                shown_folders.add(folder)
        if self._folder and self._folder not in shown_folders:
            self._folder = ""
        with QSignalBlocker(self.folders):
            self.folders.clear()
            all_item = QTreeWidgetItem((f"{self._all_folders_label} ({len({_row_key(r) for r in self._rows})})",))
            all_item.setData(0, Qt.UserRole, "")
            self.folders.addTopLevelItem(all_item)
            items = {}
            for folder in sorted(shown_folders, key=lambda p: (p.count("\\"), p)):
                label = self._folder_labels.get(folder) or ntpath.basename(folder.rstrip("\\")) or folder
                item = QTreeWidgetItem((f"{label} ({len(membership[folder])})",))
                item.setData(0, Qt.UserRole, folder)
                item.setToolTip(0, wrapped_tooltip(folder))
                parent = items.get(_folder_key(ntpath.dirname(folder)))
                (parent.addChild(item) if parent is not None else self.folders.addTopLevelItem(item))
                items[folder] = item
            selected = items.get(self._folder, all_item)
            self.folders.setCurrentItem(selected)
            for root in roots:
                if root in items:
                    self.folders.expandItem(items[root])
            ancestor = selected.parent()
            while ancestor is not None:
                self.folders.expandItem(ancestor)
                ancestor = ancestor.parent()
            all_item.setExpanded(True)
        self._folder_items = items
        if self.resource_kind == "presets":
            nested = any(_folder_key(ntpath.dirname(folder)) in items for folder in items)
            self.descendants.setVisible(nested); self.depth.setVisible(nested)
        self._breadcrumbs()

    def visible_rows(self, *, query=""):
        keys = order_visible(self._views, self._preferences(), folder=self._folder, query=query)
        self.summary.setText(f"{len(keys):,} matching choices · {len(self._rows):,} loaded." + self._folder_limit_notice)
        return tuple(self._by_key[key] for key in keys)

    def folder_limit_notice(self):
        return self._folder_limit_notice.strip()

    def begin_update(self, *, selected_key=_KEEP_SELECTION):
        """Preserve explicit selection before the owner clears its source table."""
        current = self.table.currentItem()
        row = self._table_row(current)
        self._selected_key = (_row_key(row) if row is not None else None) if selected_key is _KEEP_SELECTION else selected_key
        self._table_rebuilding = True

    def _table_row(self, item):
        return self._table_value_to_row(item.data(0, Qt.UserRole)) if item is not None else None

    def show_page(self, rows):
        """Present one bounded page. The owner populates the authoritative table."""
        rows = tuple(islice(iter(rows), self.PAGE_LIMIT))
        current = self.table.currentItem()
        current_row = self._table_row(current)
        selected = self._selected_key if self._table_rebuilding else (
            _row_key(current_row) if current_row is not None else self._selected_key
        )
        matching_table = None
        if selected is not None:
            for index in range(self.table.topLevelItemCount()):
                candidate = self.table.topLevelItem(index)
                value = self._table_row(candidate)
                if value is not None and _row_key(value) == selected:
                    matching_table = candidate
                    break
        if matching_table is not None and matching_table is not current:
            self.table.setCurrentItem(matching_table)
        elif matching_table is None:
            self._selected_key = None
        self._table_rebuilding = False
        with QSignalBlocker(self.cards):
            self.cards.clear()
            for row in islice(iter(rows), self.PAGE_LIMIT):
                item = QListWidgetItem(str(row.display_name))
                item.setData(Qt.UserRole, row)
                item.setSizeHint(self.delegate.sizeHint(None, None))
                compatibility_label = _compatibility_label(row, self.contextual)
                compatibility = f"\n{compatibility_label}" if compatibility_label else ""
                item.setToolTip(wrapped_tooltip(f"{row.display_name}\n{_role_label(row.role)} · {row.family}\n{row.resource_id}{compatibility}"))
                if self.resource_kind == "presets":
                    item.setToolTip(wrapped_tooltip(f"{row.display_name}\nPreset ID: {getattr(row, 'preset_id', row.resource_id)}\n"
                        f"{getattr(row, 'prompt_preview', '')}\n{getattr(row, 'settings_summary', '')}\n{_card_state_line(row, False)}"))
                item.setData(Qt.AccessibleTextRole,
                             f"{row.display_name}, {_role_label(row.role)}, {row.family}, "
                             f"{'favorite, ' if row.favorite else ''}{getattr(row, 'cached_status', 'Saved preset')}"
                             f"{', ' + compatibility_label if compatibility_label else ''}")
                self.cards.addItem(item)
                if selected == _row_key(row):
                    self.cards.setCurrentItem(item)
        self._apply_table_mode()
        if self.artwork_loader is not None: self.artwork_loader.set_rows(rows)
        if selected is not None: self._restore_scroll(None)

    def configure_artwork(self, service, jobs, *, settings=None):
        if self.resource_kind != "models": return
        from .model_artwork_dialog import ArtworkPageLoader
        if self.artwork_loader is not None: self.artwork_loader.close()
        self.artwork_loader = ArtworkPageLoader(service, jobs, self, settings=settings)
        self.delegate.artwork_loader = self.artwork_loader
        self.artwork_loader.changed.connect(self.refresh_artwork)
        self._artwork_protection = self.artwork_loader.policy.preview_state("")["blurred"]
        self._artwork_timer = QTimer(self); self._artwork_timer.setInterval(200)
        self._artwork_timer.timeout.connect(self._check_artwork_protection); self._artwork_timer.start()

    def _check_artwork_protection(self):
        protected = self.artwork_loader.policy.preview_state("")["blurred"]
        if protected != self._artwork_protection:
            self._artwork_protection = protected; self.refresh_artwork()

    def refresh_artwork(self):
        loader = self.artwork_loader
        if loader is None: return
        for index in range(self.table.topLevelItemCount()):
            item = self.table.topLevelItem(index); row = self._table_row(item)
            if row is None: continue
            key = str(row.resource_id)
            item.setIcon(self.name_column, QIcon(loader.display_pixmap(key)))
            item.setToolTip(self.name_column, row.resource_id + "\n" + loader.label(key)
                            + "\n" + getattr(row, "declaration_summary", ""))
        self.cards.viewport().update()

    def close_artwork(self):
        self._presentation_closed = True
        self._scroll_timer.stop()
        if self.artwork_loader is not None:
            self._artwork_timer.stop(); self.artwork_loader.close()

    def set_busy(self, busy):
        self._busy = bool(busy)
        for widget in (self.folders, self.cards, self.view_mode, self.sort_order, self.density,
                       self.reverse, self.descendants, self.depth, self.breadcrumbs, self.up, self.navigation_toggle):
            widget.setEnabled(not self._busy)
        self.up.setEnabled(not self._busy and bool(self._folder))
        self.depth.setEnabled(not self._busy and self.descendants.isChecked())

    def reset_filters(self):
        changed = bool(self._folder or self.sort_order.currentIndex() or self.reverse.isChecked()
                       or not self.descendants.isChecked() or self.depth.currentData())
        self._folder = ""
        with QSignalBlocker(self.folders), QSignalBlocker(self.sort_order), QSignalBlocker(self.reverse), QSignalBlocker(self.descendants), QSignalBlocker(self.depth):
            self.folders.setCurrentItem(self.folders.topLevelItem(0))
            self.sort_order.setCurrentIndex(0)
            self.reverse.setChecked(False); self.descendants.setChecked(True); self.depth.setCurrentIndex(0)
        if changed:
            self._breadcrumbs()
            self._ordering_changed()

    def _folder_changed(self):
        item = self.folders.currentItem()
        folder = str(item.data(0, Qt.UserRole) or "") if item else ""
        if folder != self._folder:
            self._folder = folder
            self._breadcrumbs()
            self.viewChanged.emit()

    def active_folder_scope(self):
        """Return the exact active folder and its short visible label."""
        if not self._folder:
            return "", ""
        return self._folder, (self._folder_labels.get(self._folder)
                              or ntpath.basename(self._folder.rstrip("\\")) or self._folder)

    def clear_folder_scope(self):
        if self._folder:
            self.folders.setCurrentItem(self.folders.topLevelItem(0))

    def set_picker_chrome_visible(self, visible):
        """Hide browsing tools for a focused choice without changing filters or preferences."""
        self._picker_chrome_visible = bool(visible)
        self.presentation_controls.setVisible(self._picker_chrome_visible)
        self.folder_navigation.setVisible(self._picker_chrome_visible)
        self.folders.setVisible(self._picker_chrome_visible and self.navigation_toggle.isChecked())

    def _preferences(self):
        return BrowserViewPreferences(str(self.view_mode.currentData()), int(self.density.currentData()),
            str(self.sort_order.currentData()), self.reverse.isChecked(), self.descendants.isChecked(), int(self.depth.currentData()),
            self.navigation_toggle.isChecked())

    def _navigation_changed(self, checked):
        self.folders.setVisible(checked and self._picker_chrome_visible)
        self._preference_changed()

    def _preference_changed(self):
        self._state = self._preferences(); self.preference_changed.emit(asdict(self._state))

    def _ordering_changed(self, *_):
        self.depth.setEnabled(not self._busy and self.descendants.isChecked())
        self._preference_changed(); self.viewChanged.emit()

    def _mode_changed(self, *_args, emit=True):
        self._scrolls[self._mode] = self.stack.currentWidget().verticalScrollBar().value()
        self._mode = str(self.view_mode.currentData())
        self.stack.setCurrentIndex(0 if self._mode in ("cards", "thumbnails") else 1)
        self._relayout()
        if self.stack.currentWidget() is self.cards:
            self._table_selected()
        self._restore_scroll(self._scrolls.get(self._mode))
        if emit: self._preference_changed()

    def _density_changed(self, *_):
        offset = self.stack.currentWidget().verticalScrollBar().value()
        self._relayout(); self._restore_scroll(offset); self._preference_changed()

    def _restore_scroll(self, value):
        if self._presentation_closed: return
        self._scroll_value = value
        self._scroll_timer.start(0)

    def _apply_pending_scroll(self):
        if self._presentation_closed: return
        widget = self.stack.currentWidget()
        if self._scroll_value is None:
            if widget.currentItem() is not None: widget.scrollToItem(widget.currentItem())
        else:
            widget.verticalScrollBar().setValue(self._scroll_value)

    def _relayout(self):
        self.delegate.mode = self._mode
        self.delegate.width = VIEW_WIDTHS[int(self.density.currentData())]
        size = self.delegate.sizeHint(None, None)
        self.cards.setGridSize(size + QSize(12, 12))
        for index in range(self.cards.count()): self.cards.item(index).setSizeHint(size)
        self.cards.doItemsLayout(); self.cards.viewport().update()
        self._apply_table_mode()

    def _apply_table_mode(self):
        compact = self._mode == "list"
        # The supplied table keeps original row data and selection authority.
        for column in range(self.table.columnCount()): self.table.setColumnHidden(column, compact and column not in self.compact_columns)
        self.table.header().setVisible(not compact)
        height = (20, 23, 26, 30, 34, 38)[int(self.density.currentData())]
        for index in range(self.table.topLevelItemCount()):
            self.table.topLevelItem(index).setSizeHint(0, QSize(0, height))

    def _breadcrumbs(self):
        chain, folder = [], self._folder
        while folder and folder in self._folder_items:
            chain.append(folder)
            parent = _folder_key(ntpath.dirname(folder))
            if parent == folder: break
            folder = parent
        with QSignalBlocker(self.breadcrumbs):
            self.breadcrumbs.clear(); self.breadcrumbs.addItem(self._all_folders_label, "")
            labels = [self._all_folders_label]
            for folder in reversed(chain):
                labels.append(self._folder_labels.get(folder) or ntpath.basename(folder.rstrip("\\")) or folder)
                self.breadcrumbs.addItem(" › ".join(labels), folder)
            self.breadcrumbs.setCurrentIndex(max(0, self.breadcrumbs.findData(self._folder)))
        self.up.setEnabled(not self._busy and bool(self._folder))

    def _breadcrumb_changed(self, *_):
        folder = str(self.breadcrumbs.currentData() or "")
        self.folders.setCurrentItem(self._folder_items.get(folder, self.folders.topLevelItem(0)))

    def _folder_up(self):
        folder = _folder_key(ntpath.dirname(self._folder))
        self.folders.setCurrentItem(self._folder_items.get(folder, self.folders.topLevelItem(0)))

    def _card_selected(self, current, _previous):
        if current is None:
            return
        row = current.data(Qt.UserRole)
        if row is None:
            return
        key = _row_key(row)
        for index in range(self.table.topLevelItemCount()):
            item = self.table.topLevelItem(index)
            value = self._table_row(item)
            if value is not None and _row_key(value) == key:
                self.table.setCurrentItem(item)
                return

    def _table_selected(self):
        item = self.table.currentItem()
        row = self._table_row(item)
        key = _row_key(row) if row is not None else None
        if self.table.topLevelItemCount() == 0:
            self._table_rebuilding = True
            return
        if self._table_rebuilding:
            return
        if key is not None:
            self._selected_key = key
        with QSignalBlocker(self.cards):
            for index in range(self.cards.count()):
                card = self.cards.item(index)
                value = card.data(Qt.UserRole)
                if value is not None and _row_key(value) == key:
                    self.cards.setCurrentItem(card)
                    return
            self.cards.setCurrentRow(-1)

    def _table_will_reset(self):
        self._table_rebuilding = True
