"""Read-only standalone Qt browser; opens published DTOs, never native catalogs."""
from pathlib import Path
from threading import Event
from uuid import uuid4
from suite_core.selected_catalog import SelectedCatalogSession
from suite_core.media_examples import ExampleResult, ExampleStatus, MediaReference
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QPushButton, QSplitter, QTableView, QTextEdit, QVBoxLayout, QWidget,
    QListWidget, QApplication, QCheckBox, QSizePolicy,
)
from qt_app.browse_components import IncrementalResultModel, IncrementalResultRow
from suite_core.knowledge import TagLookup
from .publication import read, require_successor, Publication
from suite_core.manager.command_bar import CommandBar, UiCommand, CommandSearchDialog
from .version import DISPLAY_VERSION


class EncyclopediaWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Tag Encyclopedia {DISPLAY_VERSION}")
        self.resize(1100, 720)
        self.publication = None
        self.path = None
        self.lookup = None
        self.stale = False
        self._selected_id = ""
        self._example_tag_id = ""
        self.catalog_session = None
        self.catalog_host = None
        self.help_commands = None
        self._example_provider = None
        self._hosted_binding = None
        self._example_submit = None
        self._example_libraries = ()
        self._example_token = uuid4().hex
        self._example_cancel = Event()
        self._example_pending = None
        self._example_observers = []
        root = QWidget(self)
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 20, 24, 20)
        title = QLabel("Tag Encyclopedia")
        title_font = title.font()
        title_font.setPointSize(18)
        title_font.setBold(True)
        title.setFont(title_font)
        layout.addWidget(title)
        layout.addWidget(QLabel("Find and explore Tags from your selected database. Optional default content is reviewed before importing."))
        self.bar = CommandBar(self)
        layout.addWidget(self.bar)
        self.open_button = QPushButton("Open exported tag catalog", self)
        self.open_button.clicked.connect(self._choose_snapshot)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setEnabled(False)
        self.refresh_button.clicked.connect(self.refresh)
        self.open_button.hide(); self.refresh_button.hide()
        self.refresh_button.setParent(self)
        self.identity = QLabel("No Tag Database selected", root)
        self.identity.setTextFormat(Qt.PlainText)
        self.identity.setWordWrap(True)
        layout.addWidget(self.identity)
        self.identity.hide()
        self.advanced = QCheckBox("Show technical IDs and source details")
        self.advanced.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        self.advanced.toggled.connect(self._show_advanced)
        self.advanced.hide()
        layout.addWidget(self.advanced)
        self.empty_hint = QLabel("A tag database stores your tags, categories and edits. Choose Home > Create a database for a new collection, or Home > Open Tag Database to use one you already have. Optional default tags can be reviewed separately.")
        self.empty_hint.setWordWrap(True)
        layout.addWidget(self.empty_hint)
        self.empty_database_action = QPushButton('Review optional starter tags')
        self.empty_database_action.clicked.connect(self.review_defaults)
        self.empty_database_action.hide()
        layout.addWidget(self.empty_database_action)
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Find a Tag by name or alias")
        self.search.setAccessibleName("Search Tags")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        self.search.returnPressed.connect(self.resolve_query)
        self.category = QComboBox()
        self.category.setAccessibleName("Category path filter")
        self.category.addItem("All categories", None)
        self.category.currentIndexChanged.connect(self._filter)
        self.resolve_button = QPushButton("Resolve Exact")
        self.resolve_button.clicked.connect(self.resolve_query)
        self.resolve_button.setVisible(False)
        filters.addWidget(self.search, 3)
        self.category.setParent(self); self.category.hide()
        filters.addWidget(self.resolve_button)
        layout.addLayout(filters)
        splitter = QSplitter()
        self.browse_panes = splitter
        splitter.hide()
        self.categories = QListWidget()
        self.categories.setAccessibleName("Browse Categories")
        self.categories.setMinimumWidth(150)
        self.categories.currentRowChanged.connect(lambda index: self.category.setCurrentIndex(index))
        splitter.addWidget(self.categories)
        self.table = QTableView()
        self.table.setAccessibleName("Tag results")
        self.model = IncrementalResultModel(("Preferred label", "Stable ID", "Category path"), page_size=200)
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 240)
        self.table.setColumnWidth(1, 220)
        self.table.setColumnHidden(1, True)
        self.table.selectionModel().currentChanged.connect(self._selection_changed)
        self.table.clicked.connect(lambda index: self._selection_changed(index, None))
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setAccessibleName("Read-only Tag details")
        splitter.addWidget(self.table)
        splitter.addWidget(self.details)
        splitter.setSizes([190, 550, 350])
        layout.addWidget(splitter, 1)
        self.empty_space = QWidget(root)
        self.empty_space.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        layout.addWidget(self.empty_space, 1)
        paging = QHBoxLayout()
        self.counts = QLabel("0 Tags")
        self.more_button = QPushButton("Load More")
        self.more_button.clicked.connect(self.model.fetch_more)
        self.model.counts_changed.connect(self._counts_changed)
        paging.addWidget(self.counts)
        paging.addStretch()
        paging.addWidget(self.more_button)
        layout.addLayout(paging)
        self.status = QLabel("No database is open yet. Browsing works offline.")
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Catalog status")
        layout.addWidget(self.status)
        from qt_app.components import IndeterminateActivityBar
        self.database_activity = IndeterminateActivityBar(root)
        self.database_elapsed = QLabel(root)
        self.database_activity.hide()
        self.database_elapsed.hide()
        layout.addWidget(self.database_activity)
        layout.addWidget(self.database_elapsed)
        self.cancel_work_button = QPushButton("Cancel current work")
        self.cancel_work_button.clicked.connect(lambda: self.catalog_host.cancel() if self.catalog_host else None)
        self.cancel_work_button.hide()
        self.cancel_close_button = QPushButton("Cancel Close")
        self.cancel_close_button.clicked.connect(lambda: self.catalog_host.cancel_close() if self.catalog_host else None)
        self.cancel_close_button.hide()
        layout.addWidget(self.cancel_work_button)
        layout.addWidget(self.cancel_close_button)
        example_commands = QHBoxLayout()
        self.examples_heading = QLabel("Library examples")
        example_commands.addWidget(self.examples_heading)
        self.examples_button = QPushButton("Find Examples")
        self.examples_button.clicked.connect(self.find_examples)
        self.examples_cancel = QPushButton("Cancel")
        self.examples_cancel.clicked.connect(self.cancel_examples)
        self.examples_cancel.setEnabled(False)
        example_commands.addWidget(self.examples_button)
        example_commands.addWidget(self.examples_cancel)
        example_commands.addStretch()
        layout.addLayout(example_commands)
        self.examples_status = QLabel("No library connected. A host must supply an approved read-only library scope.")
        self.examples_status.setTextFormat(Qt.PlainText)
        self.examples_status.setWordWrap(True)
        self.examples_status.setAccessibleName("Library example status")
        layout.addWidget(self.examples_status)
        self.examples_model = IncrementalResultModel(("Library ID", "Relative image reference"), page_size=200)
        self.examples_table = QTableView()
        self.examples_table.setAccessibleName("Cached library example references")
        self.examples_table.setModel(self.examples_model)
        self.examples_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.examples_table.horizontalHeader().setStretchLastSection(True)
        self.examples_table.setMaximumHeight(180)
        layout.addWidget(self.examples_table)
        self.examples_more = QPushButton("Load More Examples")
        self.examples_more.clicked.connect(self.examples_model.fetch_more)
        self.examples_model.counts_changed.connect(
            lambda shown, total: self.examples_more.setEnabled(shown < total))
        layout.addWidget(self.examples_more)
        self.examples_more.setEnabled(False)
        self._invalidate_examples()
        self._counts_changed(0, 0)
        self._set_examples_visible(False)
        self._set_commands()
        self.bar.searchRequested.connect(self.search_commands)

    def _sync_start_choices(self):
        if not hasattr(self, 'empty_hint'):
            return
        selected = self.publication is not None
        empty = selected and not self.publication.snapshot.tags
        self.empty_database_action.setVisible(empty)
        self.empty_database_action.setEnabled(empty and (self.catalog_host is None or not self.catalog_host.busy))
        idle = self.catalog_host is None or not self.catalog_host.busy
        for name in ('search', 'category', 'counts', 'more_button', 'examples_heading', 'examples_button', 'examples_cancel', 'examples_status'):
            widget = getattr(self, name, None)
            if widget is not None and not selected:
                widget.hide()

    def _set_commands(self):
        self._sync_start_choices()
        idle = lambda: self.catalog_host is None or not self.catalog_host.busy
        self.commands = [
            UiCommand("encyclopedia:create-database", "Create a database", "Home", callback=self.create_database, enabled=lambda: idle() and self.catalog_host is not None),
            UiCommand("encyclopedia:open-database", "Open Tag Database", "Home", callback=self.choose_database, enabled=idle),
            UiCommand("encyclopedia:refresh", "Refresh", "Home", callback=self.refresh,
                      enabled=lambda: idle() and self.publication is not None),
            UiCommand("encyclopedia:defaults", "Use optional default tags", "Home", callback=self.review_defaults, enabled=idle),
            UiCommand("encyclopedia:open-export", "Open exported tag catalog", "Home", callback=self._choose_snapshot, enabled=idle),
            UiCommand("encyclopedia:copy", "Copy Tag Name", "Home", callback=self.copy_tag,
                      enabled=lambda: bool(self._selected_id and self.lookup)),
        ]
        if self.help_commands is not None:
            from dataclasses import replace
            for command in self.help_commands.commands:
                self.commands.append(replace(command,enabled=lambda original=command:idle() and original.available())
                    if command.key=='encyclopedia:check-updates' else command)
        if self.catalog_host is not None:
            self.commands.extend(self.catalog_host.editor_commands())
            self.commands.extend(self.catalog_host.entry_commands())
        if self.catalog_host is not None and self.catalog_host.organizer_receiver is not None:
            self.commands.extend([
                UiCommand('encyclopedia:organizer-search','Find Matching Images in Organizer','Home',
                    callback=lambda:self.catalog_host.send_organizer_handoff(),enabled=self.catalog_host.organizer_ready,
                    description="Open Organizer's display-name search for this selected native Tag; no image is changed."),
                UiCommand('encyclopedia:organizer-review','Review Image Tag Names','Home',
                    callback=self.review_organizer_tag_names,enabled=self.catalog_host.organizer_ready,
                    description='Choose a recognized alias and open original image-name correction review. No automatic image writes.')])
        self.bar.set_commands("Tag Encyclopedia", self.commands, groups=["Home", "Help"] if self.help_commands else ["Home"])

    def search_commands(self):
        CommandSearchDialog(self.commands, "Tag Encyclopedia", self).exec()

    def review_organizer_tag_names(self):
        if self.catalog_host is None or not self.catalog_host.organizer_ready():return False
        tag=self.lookup.get(self._selected_id).tag
        aliases=tuple(tag.aliases) if tag is not None else ()
        if not aliases:
            self.status.setText('This Tag has no recognized previous name to suggest. Review its native aliases before requesting image-name correction.')
            return False
        from PySide6.QtWidgets import QInputDialog
        previous,accepted=QInputDialog.getItem(self,'Review Image Tag Names',
            'Choose a recognized previous name. Organizer opens review; nothing is changed automatically.',aliases,0,False)
        return self.catalog_host.send_organizer_handoff(previous_display=previous) if accepted else False

    def create_database(self):
        if self.catalog_host is not None:
            return self.catalog_host.create_database()
        self.status.setText('Database creation is unavailable in this host. Open an existing exported catalog instead.')
        return False

    def choose_database(self):
        if self.catalog_host is not None:
            return self.catalog_host.choose_database()
        self.status.setText("Database loading is unavailable in this host. Open an exported tag catalog instead.")
        return False

    def review_defaults(self):
        if self.catalog_host is not None:
            return self.catalog_host.show_defaults()
        self.status.setText("Optional content import needs an explicitly owned local catalog session.")
        return False

    def copy_tag(self):
        if self.lookup is not None and self._selected_id:
            result = self.lookup.get(self._selected_id)
            if result.tag is not None:
                QApplication.clipboard().setText(result.tag.display)

    def _show_advanced(self, checked):
        self.identity.setVisible(checked)
        self.table.setColumnHidden(1, not checked)
        self.resolve_button.setVisible(checked)
        self.search.setPlaceholderText("Search IDs, names, aliases and category paths" if checked else "Find a Tag by name or alias")
        if self.lookup is not None and self._selected_id:
            self._show_resolution(self.lookup.get(self._selected_id, category_ids=self._scope()))

    def _set_examples_visible(self, visible):
        for widget in (self.examples_heading, self.examples_button, self.examples_cancel,
                       self.examples_status, self.examples_table, self.examples_more):
            widget.setVisible(visible)

    def _choose_snapshot(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Open exported tag catalog", "", "Exported tag catalog (*.json);;All files (*)")
        if filename:
            self.open_snapshot(filename)

    def open_snapshot(self, path, *, refreshing=False):
        try:
            candidate = read(path)
        except (OSError, ValueError) as error:
            self.stale = self.publication is not None
            self._invalidate_examples()
            self.status.setText("Could not open the exported catalog. Previous view is retained. " + str(error))
            self._identity()
            return False
        return self.open_publication(candidate, path=path, refreshing=refreshing)

    def open_publication(self, candidate, *, path=None, refreshing=False, native_root=None, directory_pin=None,native_adapter=None):
        """Immutable host delivery; never writes native data or snapshot files."""
        try:
            if type(candidate) is not Publication:
                raise ValueError("An admitted immutable Tag catalog is required")
            if refreshing and self.publication is not None:
                if native_root is not None:
                    # Original native host validates the current coherent state.
                    # In-memory views allocate no durable publication generation.
                    if candidate.catalog_id != self.publication.catalog_id:
                        raise ValueError("Native database identity changed during refresh")
                else:
                    require_successor(self.publication, candidate)
            lookup = TagLookup(candidate.snapshot)
            descriptor = self.catalog_host.prepare_delivery(native_root=native_root, export_path=path,
                directory_pin=directory_pin,native_adapter=native_adapter,publication=candidate) if self.catalog_host is not None else None
        except (OSError, ValueError) as error:
            self.stale = self.publication is not None
            self._invalidate_examples()
            self.status.setText(f"{'Stale — showing previous snapshot. ' if self.stale else 'Unavailable. '}{error}")
            self._identity()
            return False
        selected = self._selected_id if refreshing else ""
        category_id = self.category.currentData() if refreshing else None
        if self.catalog_session is None:
            self.catalog_session = SelectedCatalogSession(candidate)
        elif refreshing and native_root is not None:
            if candidate != self.publication:
                self.catalog_session.select(candidate)
        elif refreshing:
            self.catalog_session.refresh(candidate)
        else:
            self.catalog_session.select(candidate)
        self._invalidate_examples()
        self._example_tag_id = ""
        self.publication, self.lookup, self.path = candidate, lookup, Path(path) if path is not None else None
        if not candidate.snapshot.tags:
            self.empty_hint.setText('Your database is open and empty. Review optional starter tags to begin a collection, or open a different database from Home. Nothing is imported automatically.')
            self.empty_hint.show()
            self.empty_database_action.show()
        else:
            self.empty_hint.hide()
        for widget in (self.search, self.counts, self.more_button):
            widget.setVisible(bool(candidate.snapshot.tags))
        self.empty_space.setVisible(not bool(candidate.snapshot.tags))
        self.advanced.setVisible(bool(candidate.snapshot.tags))
        self.browse_panes.setVisible(bool(candidate.snapshot.tags))
        self.stale = False
        self._selected_id = selected
        self.category.blockSignals(True)
        self.category.clear()
        self.category.addItem("All categories", None)
        allowed = frozenset(candidate.snapshot.source.scope)
        for row in sorted(candidate.snapshot.categories, key=lambda row: (row.path.casefold(), row.id)):
            if row.id in allowed:
                self.category.addItem(row.path, row.id)
        index = self.category.findData(category_id)
        self.category.setCurrentIndex(max(0, index))
        self.category.blockSignals(False)
        self.categories.blockSignals(True)
        self.categories.clear()
        for index in range(self.category.count()):
            self.categories.addItem(self.category.itemText(index))
        self.categories.setCurrentRow(self.category.currentIndex())
        self.categories.blockSignals(False)
        self.refresh_button.setEnabled(True)
        self._identity()
        self._filter()
        if selected:
            self._show_resolution(self.lookup.get(selected))
        else:
            self.details.clear()
        self.status.setText("Tag catalog refreshed." if refreshing else "Tag catalog opened. Search or choose a category to browse.")
        if self.catalog_host is not None:
            self.catalog_host.accept_delivery(descriptor)
        return True

    def refresh(self):
        if self.catalog_host is not None:
            return self.catalog_host.refresh()
        return self.refresh_snapshot()

    def refresh_snapshot(self):
        if self.path is not None:
            return self.open_snapshot(self.path, refreshing=True)
        return False

    def _identity(self):
        if self.publication is None:
            return
        publication = self.publication
        source = publication.snapshot.source
        self.identity.setText(f"{'STALE · ' if self.stale else ''}{publication.catalog_id} · generation {publication.generation} · revision {source.revision} · {source.completeness}\nSource: {source.reference}")

    def _scope(self):
        if self.publication is None:
            return ()
        selected = self.category.currentData()
        return (selected,) if selected is not None else self.publication.snapshot.source.scope

    def _filter(self, *_):
        if self.publication is None:
            return
        self._invalidate_examples()
        self._example_tag_id = ""
        selected = self._selected_id
        snapshot = self.publication.snapshot
        categories = {row.id: row.path for row in snapshot.categories}
        scope = frozenset(self._scope())
        query = self.search.text().strip().casefold()
        rows = []
        for tag in sorted(snapshot.tags, key=lambda row: (row.display.casefold(), row.id)):
            path = categories[tag.category_id]
            haystack = " ".join((tag.id, tag.display, *tag.aliases, path)).casefold()
            if tag.category_id in scope and (not query or query in haystack):
                rows.append(IncrementalResultRow(tag.id, (tag.display, tag.id, path),
                                                tooltip="Aliases: " + ", ".join(tag.aliases)))
        self.model.set_rows(rows, ensure_ids=(selected,))
        index = self.model.index_for_id(selected)
        if index.isValid():
            self.table.setCurrentIndex(index)
        self._examples_readiness()

    def _counts_changed(self, shown, total):
        self.counts.setText(f"{shown} shown / {total} matching Tags")
        self.more_button.setEnabled(shown < total)

    def _selection_changed(self, current, _previous):
        if self.lookup is not None and current.isValid():
            self._selected_id = self.model.stable_id(current)
            self._show_resolution(self.lookup.get(self._selected_id, category_ids=self._scope()))

    def resolve_query(self):
        if self.lookup is None:
            self.status.setText("Unavailable: open a published snapshot first.")
            return
        result = self.lookup.resolve(self.search.text().strip(), category_ids=self._scope())
        self._selected_id = result.tag.id if result.tag is not None else ""
        if self._selected_id:
            index = self.model.index_for_id(self._selected_id)
            if index.isValid():
                self.table.setCurrentIndex(index)
        else:
            self.table.clearSelection()
        self._show_resolution(result)
        self.status.setText(f"{'Stale snapshot · ' if self.stale else ''}Exact resolution: {result.status}")

    def _show_resolution(self, result):
        self._invalidate_examples()
        self._selected_id = result.tag.id if result.tag is not None else ""
        self._example_tag_id = result.tag.id if result.tag is not None else ""
        self._examples_readiness()
        summaries = {"ambiguous": "Several tags match (ambiguous). Try a more specific name.",
                     "retired": "This tag is retired and cannot be assigned.",
                     "unavailable": "This tag is currently unavailable.",
                     "missing": "No matching tag (missing)."}
        text = summaries.get(result.status, "Tag found.")
        if self.advanced.isChecked():
            text += f"\nResolution: {result.status}\nRequested: {result.value}"
        if result.tag is not None:
            tag = result.tag
            categories = {row.id: row.path for row in self.publication.snapshot.categories}
            text += (f"\n\nPreferred label: {tag.display}"
                     f"\nCategory path: {categories[tag.category_id]}\nAliases: {', '.join(tag.aliases) or 'None'}"
                     f"\nPublished rating: {tag.minimum_rating or 'Unspecified'}"
                     f"\nClassification: {tag.classification_status}")
            if self.advanced.isChecked():
                text += f"\nStable ID: {tag.id}"
            text += "\n\nNotes and richer native policy details are unavailable in this catalog view."
        if result.owner_ids and self.advanced.isChecked():
            text += "\nCurrent owner IDs: " + ", ".join(result.owner_ids)
        if result.retired_ids and self.advanced.isChecked():
            text += "\nRetired IDs (not assignable): " + ", ".join(result.retired_ids)
        if result.unavailable_ids and self.advanced.isChecked():
            text += "\nUnavailable IDs (not assignable): " + ", ".join(result.unavailable_ids)
        if result.relationship_ids and self.advanced.isChecked():
            evidence={row.id:row for row in self.publication.snapshot.relationships}
            text += "\n\nRelationship evidence:"
            for identity in result.relationship_ids:
                row=evidence[identity]
                text += (f"\n{row.id} | Source: {row.source} | Version: {row.version}"
                         f" | Layer: {row.layer} | Reviewed: {row.reviewed} | Disabled: {row.disabled}")
        self.details.setPlainText(text)


    def configure_examples(self, *, provider, library_ids, submit):
        """Host-only injection; no store, worker, reader or provider construction.

        submit(work, deliver) runs work off the GUI thread and delivers on it.
        Provider must supply query AND delivery-time accepts. Its current-selection
        and Knowledge callbacks use example_selection_token/example_catalog_token.
        Scope is explicitly host-approved; reconfigure on library-scope changes.
        Published identity-name equivalence requires host qualification beforehand.
        """
        if (not callable(getattr(provider, "query", None))
                or not callable(getattr(provider, "accepts", None)) or not callable(submit)):
            raise ValueError("A read-only provider with delivery validation and host scheduler is required")
        if (not isinstance(library_ids, tuple) or len(set(library_ids)) != len(library_ids)
                or any(not isinstance(value, str) or not value for value in library_ids)):
            raise ValueError("An immutable approved library scope is required")
        self._invalidate_examples()
        self._example_provider, self._example_libraries, self._example_submit = provider, library_ids, submit
        self._set_examples_visible(bool(library_ids))
        self._examples_readiness()

    def example_selection_token(self):
        return self._example_token

    def example_catalog_token(self):
        return self.catalog_session.catalog_token() if self.catalog_session is not None else ""

    def _examples_readiness(self):
        ready = bool(self._example_provider and self._example_libraries
                     and self._example_tag_id and not self.stale)
        catalog_unbound = (self._hosted_binding is not None
                           and self._hosted_binding.catalog_token != self.example_catalog_token())
        admission = getattr(self._example_provider, 'example_queries_admitted', None)
        query_paused = callable(admission) and not admission()
        ready = ready and not catalog_unbound and not query_paused
        self.examples_button.setEnabled(ready and self._example_pending is None)
        if not self._example_provider or not self._example_libraries:
            self.examples_status.setText("No library connected. A host must supply an approved read-only library scope.")
        elif query_paused:
            self.examples_status.setText("Example queries are paused while the original host is closing its workers.")
        elif self.stale:
            self.examples_status.setText("Catalog is stale. Refresh the snapshot before finding examples.")
        elif catalog_unbound:
            self.examples_status.setText("This catalog is not bound to the hosted library session. Ask the host to approve and reconnect this catalog before finding examples.")
        elif not self._example_tag_id:
            self.examples_status.setText("Select or resolve one current Tag, then choose Find Examples.")
        elif self._example_pending is None:
            self.examples_status.setText("Ready to find cached examples in " + str(len(self._example_libraries)) + " approved libraries.")

    def _invalidate_examples(self):
        self._example_cancel.set()
        self._example_cancel = Event()
        self._example_token = uuid4().hex
        self._example_pending = None
        self._notify_example_observers(None)
        if hasattr(self, "examples_model"):
            self.examples_model.set_rows(())
            self.examples_cancel.setEnabled(False)
            self._examples_readiness()

    def cancel_examples(self):
        self._invalidate_examples()
        self.examples_status.setText("Cancelled. No example references retained; choose Find Examples to retry.")

    def find_examples(self):
        admission = getattr(self._example_provider, 'example_queries_admitted', None)
        if callable(admission) and not admission():
            self._examples_readiness()
            return
        if (not self._example_provider or not self._example_libraries
                or not self._example_tag_id or self.stale or self._example_pending is not None):
            self._examples_readiness()
            return
        self._invalidate_examples()
        request = self.catalog_session.example_request(
            selection_token=self._example_token, library_ids=self._example_libraries,
            tag_ids=(self._example_tag_id,))
        provider, cancel = self._example_provider, self._example_cancel
        self._example_pending = request
        self.examples_button.setEnabled(False)
        self.examples_cancel.setEnabled(True)
        self.examples_status.setText("Finding cached examples in " + str(len(request.library_ids)) + " approved libraries. Cancel is available.")

        def work(job=None):
            try:
                return provider.query(request, should_continue=lambda:
                    not cancel.is_set() and (job is None or job.checkpoint()))
            except Exception:
                return ExampleResult(request, ExampleStatus.ERROR)

        def deliver(result):
            if self._example_pending != request or cancel.is_set():
                return
            self._example_pending = None
            self.examples_cancel.setEnabled(False)
            self._examples_readiness()
            try:
                accepted = (isinstance(result, ExampleResult) and result.request == request
                            and request.selection_token == self._example_token
                            and request.knowledge_revision == self.example_catalog_token()
                            and request.library_ids == self._example_libraries
                            and provider.accepts(result))
                if accepted and result.status == ExampleStatus.SUCCESS:
                    accepted = (isinstance(result.references, tuple)
                                and 0 < len(result.references) <= 100_000
                                and all(isinstance(ref, MediaReference)
                                        and ref.library_id in request.library_ids
                                        for ref in result.references)
                                and len(set(result.references)) == len(result.references))
            except Exception:
                accepted = False
            if not accepted:
                self.examples_status.setText("Examples became stale. Choose Find Examples to retry the current scope.")
                return
            messages = {
                ExampleStatus.EMPTY: "No cached examples match this Tag in the approved libraries.",
                ExampleStatus.UNAVAILABLE: "Cached examples unavailable. Ask the host to reconnect the approved library scope, then retry.",
                ExampleStatus.ERROR: "Examples could not be queried. Choose Find Examples to retry.",
                ExampleStatus.CANCELLED: "Cancelled. Choose Find Examples to retry.",
                ExampleStatus.STALE: "Examples became stale. Choose Find Examples to retry the current scope.",
            }
            if result.status != ExampleStatus.SUCCESS:
                self.examples_status.setText(messages.get(result.status, messages[ExampleStatus.ERROR]))
                return
            # Reference-only presentation: no thumbnails, hydration, path resolution,
            # image editor, sharing or publication authority is granted here.
            self.examples_model.set_rows(tuple(
                IncrementalResultRow(str(index), (ref.library_id, ref.relative_path))
                for index, ref in enumerate(result.references)))
            self.examples_status.setText(str(len(result.references)) + " cached example references. File availability and image facts are unverified.")
            self._notify_example_observers(result)

        try:
            self._example_submit(work, deliver)
        except Exception:
            if self._example_pending == request:
                cancel.set()
                self._example_pending = None
                self.examples_cancel.setEnabled(False)
                self._examples_readiness()
                self.examples_status.setText("Examples could not be scheduled. Ask the host to restore its query worker, then retry.")

    def closeEvent(self, event):
        if self.catalog_host is not None and not self.catalog_host.close_ready():
            event.ignore()
            return
        self._invalidate_examples()
        if self._hosted_binding is not None:
            self._hosted_binding.close()
        super().closeEvent(event)

    def example_current_request(self):
        return self._example_pending

    def subscribe_example_results(self, accepted, invalidated):
        """GUI-only host hook; results arrive during the original delivery receipt.

        No observer is enrolled by standalone startup. Local image authority must
        be independently supplied by an explicitly attached Organizer host.
        """
        if not callable(accepted) or not callable(invalidated) or len(self._example_observers) >= 4:
            raise ValueError('Bounded explicit example observers required')
        subscription = (accepted, invalidated)
        self._example_observers.append(subscription)
        return subscription

    def unsubscribe_example_results(self, subscription):
        self._example_observers[:] = [item for item in self._example_observers if item is not subscription]

    def _notify_example_observers(self, result):
        for accepted, invalidated in tuple(self._example_observers):
            try:
                invalidated() if result is None else accepted(result)
            except Exception:
                # An optional presentation observer cannot break the original
                # reference browser's query, cancellation or close lifecycle.
                pass

    def attach_hosted_library_session(self, session, *, admitted_publication,
                                      task_factory, post_to_gui):
        """Embed this browser in an already-admitted native library host.

        The host supplies its existing session, CoordinatedCallTask and GUI
        dispatcher; no AppState, index, native cache reader or pool is created.
        """
        from .hosted_examples import HostedLibraryBinding
        self.detach_hosted_library_session()
        self._hosted_binding = HostedLibraryBinding(self, session,
            admitted_publication=admitted_publication, task_factory=task_factory,
            post_to_gui=post_to_gui)
        self._examples_readiness()
        return self._hosted_binding

    def detach_hosted_library_session(self):
        if self._hosted_binding is not None:
            self._hosted_binding.close()
        self._hosted_binding = None
        self._example_provider, self._example_submit, self._example_libraries = None, None, ()
        self._invalidate_examples()
