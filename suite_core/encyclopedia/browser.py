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
)
from qt_app.browse_components import IncrementalResultModel, IncrementalResultRow
from suite_core.knowledge import TagLookup
from .publication import read, require_successor
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
        self._example_provider = None
        self._hosted_binding = None
        self._example_submit = None
        self._example_libraries = ()
        self._example_token = uuid4().hex
        self._example_cancel = Event()
        self._example_pending = None
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
        layout.addWidget(QLabel("Browse an approved published catalog. This browser never edits Knowledge."))
        commands = QHBoxLayout()
        self.open_button = QPushButton("Open Snapshot…")
        self.open_button.clicked.connect(self._choose_snapshot)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setEnabled(False)
        self.refresh_button.clicked.connect(self.refresh)
        commands.addWidget(self.open_button)
        commands.addWidget(self.refresh_button)
        commands.addStretch()
        layout.addLayout(commands)
        self.identity = QLabel("No catalog opened")
        self.identity.setTextFormat(Qt.PlainText)
        self.identity.setWordWrap(True)
        layout.addWidget(self.identity)
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search IDs, labels, aliases and category paths")
        self.search.setAccessibleName("Search Tags")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        self.search.returnPressed.connect(self.resolve_query)
        self.category = QComboBox()
        self.category.setAccessibleName("Category path filter")
        self.category.addItem("All published categories", None)
        self.category.currentIndexChanged.connect(self._filter)
        self.resolve_button = QPushButton("Resolve Exact")
        self.resolve_button.clicked.connect(self.resolve_query)
        filters.addWidget(self.search, 3)
        filters.addWidget(self.category, 2)
        filters.addWidget(self.resolve_button)
        layout.addLayout(filters)
        splitter = QSplitter()
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
        self.table.selectionModel().currentChanged.connect(self._selection_changed)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setAccessibleName("Read-only Tag details")
        splitter.addWidget(self.table)
        splitter.addWidget(self.details)
        splitter.setSizes([700, 350])
        layout.addWidget(splitter, 1)
        paging = QHBoxLayout()
        self.counts = QLabel("0 Tags")
        self.more_button = QPushButton("Load More")
        self.more_button.clicked.connect(self.model.fetch_more)
        self.model.counts_changed.connect(self._counts_changed)
        paging.addWidget(self.counts)
        paging.addStretch()
        paging.addWidget(self.more_button)
        layout.addLayout(paging)
        self.status = QLabel("Open a snapshot to begin. No native catalog is accessed.")
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Catalog status")
        layout.addWidget(self.status)
        example_commands = QHBoxLayout()
        example_commands.addWidget(QLabel("Library examples"))
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

    def _choose_snapshot(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Open published snapshot", "", "Snapshot JSON (*.json);;All files (*)")
        if filename:
            self.open_snapshot(filename)

    def open_snapshot(self, path, *, refreshing=False):
        try:
            candidate = read(path)
            if refreshing and self.publication is not None:
                require_successor(self.publication, candidate)
            lookup = TagLookup(candidate.snapshot)
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
        elif refreshing:
            self.catalog_session.refresh(candidate)
        else:
            self.catalog_session.select(candidate)
        self._invalidate_examples()
        self._example_tag_id = ""
        self.publication, self.lookup, self.path = candidate, lookup, Path(path)
        self.stale = False
        self._selected_id = selected
        self.category.blockSignals(True)
        self.category.clear()
        self.category.addItem("All published categories", None)
        allowed = frozenset(candidate.snapshot.source.scope)
        for row in sorted(candidate.snapshot.categories, key=lambda row: (row.path.casefold(), row.id)):
            if row.id in allowed:
                self.category.addItem(row.path, row.id)
        index = self.category.findData(category_id)
        self.category.setCurrentIndex(max(0, index))
        self.category.blockSignals(False)
        self.refresh_button.setEnabled(True)
        self._identity()
        self._filter()
        if selected:
            self._show_resolution(self.lookup.get(selected))
        else:
            self.details.clear()
        self.status.setText("Snapshot refreshed." if refreshing else "Snapshot opened. Enter a term and use Resolve Exact to inspect identity decisions.")
        return True

    def refresh(self):
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
        self._show_resolution(result)
        self.status.setText(f"{'Stale snapshot · ' if self.stale else ''}Exact resolution: {result.status}")

    def _show_resolution(self, result):
        self._invalidate_examples()
        self._example_tag_id = result.tag.id if result.tag is not None else ""
        self._examples_readiness()
        text = f"Resolution: {result.status}\nRequested: {result.value}"
        if result.tag is not None:
            tag = result.tag
            categories = {row.id: row.path for row in self.publication.snapshot.categories}
            text += (f"\n\nStable ID: {tag.id}\nPreferred label: {tag.display}"
                     f"\nCategory path: {categories[tag.category_id]}\nAliases: {', '.join(tag.aliases) or 'None'}"
                     f"\nPublished rating: {tag.minimum_rating or 'Unspecified'}"
                     f"\nClassification: {tag.classification_status}")
        if result.owner_ids:
            text += "\nCurrent owner IDs: " + ", ".join(result.owner_ids)
        if result.retired_ids:
            text += "\nRetired IDs (not assignable): " + ", ".join(result.retired_ids)
        if result.unavailable_ids:
            text += "\nUnavailable IDs (not assignable): " + ", ".join(result.unavailable_ids)
        if result.relationship_ids:
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
        ready = ready and not catalog_unbound
        self.examples_button.setEnabled(ready and self._example_pending is None)
        if not self._example_provider or not self._example_libraries:
            self.examples_status.setText("No library connected. A host must supply an approved read-only library scope.")
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
        if hasattr(self, "examples_model"):
            self.examples_model.set_rows(())
            self.examples_cancel.setEnabled(False)
            self._examples_readiness()

    def cancel_examples(self):
        self._invalidate_examples()
        self.examples_status.setText("Cancelled. No example references retained; choose Find Examples to retry.")

    def find_examples(self):
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
        self._invalidate_examples()
        if self._hosted_binding is not None:
            self._hosted_binding.close()
        super().closeEvent(event)

    def example_current_request(self):
        return self._example_pending

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
