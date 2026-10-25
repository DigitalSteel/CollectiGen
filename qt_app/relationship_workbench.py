from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QItemSelectionModel, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QTreeView,
    QVBoxLayout,
    QWidget,
)
from .status_notifications import FooterMessageBox as QMessageBox

from core.allowed_dimension_policy import (
    AllowedDimensionValueScope,
    VALUE_SCOPE_ALL,
    VALUE_SCOPE_ALL_EXCEPT,
    VALUE_SCOPE_ONLY,
)
from core.conflict_rules import ConflictRuleEngine
from core.knowledge_engine import FoundationKnowledge
from core.knowledge_relationships import (
    ALLOWED_DIMENSION,
    ASSOCIATED_CHARACTER,
    CHARACTER_SOURCE,
    ENTITY_TAG,
    IDENTITY_ALIAS,
    MODIFIER_ROLE,
    MODIFIER_VALUE,
    OTHER_APPEARANCE,
    SCOPED_CONSTRAINT,
    SPECIES_TRAIT,
    SPECIFICITY,
    ENTAILMENT,
    VERSION_OF,
    KnowledgeRelationshipRegistry,
)
from core.modifier_role_decisions import ModifierRoleDecisionService
from core.paths import DEFAULTS_DIR, RELATIONSHIPS_PATH
from core.relationship_batch import (
    RelationshipBatchPlan,
    RelationshipBatchResult,
    RelationshipBatchService,
)
from .components import (
    BoundedSearchableComboBox,
    IncrementalResultModel,
    IncrementalResultRow,
)
from .modal_feedback import FeedbackEnabledDialog
from .relationship_workspace import RelationshipMigrationReportDialog
from .tag_relationship_editor import TokenChipEditor


_CHARACTER_KINDS = {
    ENTITY_TAG,
    CHARACTER_SOURCE,
    ASSOCIATED_CHARACTER,
    OTHER_APPEARANCE,
    VERSION_OF,
    SPECIES_TRAIT,
}


def _display(record) -> str:
    return str(getattr(record, "display", "") or getattr(record, "name", ""))


class RelationshipBatchPreviewDialog(FeedbackEnabledDialog):
    """Exact before/after review for one atomic multi-Tag action."""

    feedback_workflow = "Relationship batch preview"

    def __init__(self, plan: RelationshipBatchPlan, parent=None) -> None:
        super().__init__(parent)
        self.plan = plan
        self.setWindowTitle(f"Preview — {plan.title}")
        self.resize(940, 620)
        root = QVBoxLayout(self)
        title = QLabel(plan.title)
        title.setObjectName("dialogTitle")
        root.addWidget(title)
        summary = QLabel(plan.summary)
        summary.setWordWrap(True)
        summary.setAccessibleName("Batch outcome summary")
        root.addWidget(summary)
        note = QLabel(
            "Apply saves every listed change as one recoverable transaction. "
            "Unchanged and held rows are not written."
        )
        note.setObjectName("mutedLabel")
        note.setWordWrap(True)
        root.addWidget(note)
        self.model = IncrementalResultModel(
            ("Tag", "Before", "After", "Outcome"), page_size=200, parent=self,
        )
        rows = []
        for item in plan.rows:
            outcome = {
                "change": "Will change",
                "unchanged": "Already matches",
                "hold": "Held for review",
            }.get(item.outcome, item.outcome)
            detail = f"{outcome}. {item.reason}" if item.reason else outcome
            rows.append(IncrementalResultRow(
                item.tag_id,
                (item.display, item.before, item.after, detail),
                tooltip=item.reason,
                accessible_text=(
                    f"{item.display}; before {item.before}; after {item.after}; {detail}"
                ),
            ))
        self.model.set_rows(rows)
        view = QTreeView()
        view.setModel(self.model)
        view.setRootIsDecorated(False)
        view.setUniformRowHeights(True)
        view.setSelectionBehavior(QAbstractItemView.SelectRows)
        view.setAccessibleName("Relationship batch outcomes")
        header = view.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        root.addWidget(view, 1)
        count_row = QHBoxLayout()
        self.count = QLabel()
        self.count.setObjectName("mutedLabel")
        more = QPushButton("Load 200 More…")
        more.setProperty("compact", True)
        more.clicked.connect(self.model.fetch_more)
        self.model.counts_changed.connect(
            lambda loaded, total: (
                self.count.setText(f"Showing {loaded:,} of {total:,} outcomes."),
                more.setVisible(loaded < total),
            )
        )
        count_row.addWidget(self.count, 1)
        count_row.addWidget(more)
        root.addLayout(count_row)
        self.model.counts_changed.emit(
            self.model.loaded_count, self.model.total_count,
        )
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.apply_button = buttons.addButton(
            f"Apply {len(plan.changed_rows):,} Change"
            f"{'s' if len(plan.changed_rows) != 1 else ''}",
            QDialogButtonBox.AcceptRole,
        )
        self.apply_button.setEnabled(bool(plan.changed_rows))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)


@dataclass(frozen=True)
class _TagListEntry:
    tag_id: str
    display: str
    category: str
    search_text: str


class _TagBatchTab(QWidget):
    """Shared selection-first table used by the three batch sections."""

    PAGE_SIZE = 200

    def __init__(
        self,
        workbench,
        *,
        title: str,
        description: str,
        status_header: str = "",
        status_getter=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.workbench = workbench
        self._entries = workbench.tag_entries
        self._filtered = self._entries
        self._status_header = str(status_header)
        self._status_getter = status_getter
        root = QVBoxLayout(self)
        heading = QLabel(title)
        heading.setObjectName("sectionTitle")
        root.addWidget(heading)
        intro = QLabel(description)
        intro.setWordWrap(True)
        root.addWidget(intro)
        filter_row = QHBoxLayout()
        self.filter_row = filter_row
        self.search = QLineEdit()
        self.search.setPlaceholderText("Find Tags or Categories…")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName(f"Find Tags for {title}")
        self.scope = QComboBox()
        self.scope.setAccessibleName("Batch scope")
        self.scope.addItem("Selected Tags", "selected")
        self.scope.addItem("All filtered Tags", "filtered")
        filter_row.addWidget(self.search, 1)
        filter_row.addWidget(QLabel("Apply to"))
        filter_row.addWidget(self.scope)
        root.addLayout(filter_row)
        headers = ("Tag", "Category", self._status_header) if self._status_header else ("Tag", "Category")
        self.model = IncrementalResultModel(
            headers, page_size=self.PAGE_SIZE, parent=self,
        )
        self.view = QTreeView()
        self.view.setModel(self.model)
        self.view.setRootIsDecorated(False)
        self.view.setUniformRowHeights(True)
        self.view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.view.setAccessibleName(f"Tags for {title}")
        self.view.setAccessibleDescription(
            "Use Ctrl or Shift to select several Tags. Actions use the selection "
            "unless Apply to All filtered Tags is explicitly chosen."
        )
        self.view.setMinimumHeight(100)
        header = self.view.header()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        if self._status_header:
            header.setSectionResizeMode(2, QHeaderView.Stretch)
        root.addWidget(self.view, 1)
        counts = QHBoxLayout()
        self.selection_status = QLabel("No Tags selected")
        self.selection_status.setObjectName("mutedLabel")
        self.result_status = QLabel()
        self.result_status.setObjectName("mutedLabel")
        self.more = QPushButton("Load 200 More…")
        self.more.setProperty("compact", True)
        self.more.clicked.connect(self.model.fetch_more)
        counts.addWidget(self.selection_status)
        counts.addStretch(1)
        counts.addWidget(self.result_status)
        counts.addWidget(self.more)
        root.addLayout(counts)
        self.action_frame = QFrame()
        self.action_layout = QVBoxLayout(self.action_frame)
        self.action_layout.setContentsMargins(0, 8, 0, 0)
        root.addWidget(self.action_frame)
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(120)
        self.search_timer.timeout.connect(self._filter)
        self.search.textChanged.connect(lambda *_: self.search_timer.start())
        self.scope.currentIndexChanged.connect(self._update_status)
        self.model.counts_changed.connect(self._model_counts_changed)
        self.view.selectionModel().selectionChanged.connect(self._update_status)
        self._filter()

    def _accept_entry(self, _entry: _TagListEntry) -> bool:
        return True

    def _row_columns(self, entry: _TagListEntry) -> tuple[str, ...]:
        columns = (entry.display, entry.category)
        if self._status_header:
            columns += (str(self._status_getter(entry.tag_id)),)
        return columns

    def _model_row(self, entry: _TagListEntry) -> IncrementalResultRow:
        columns = self._row_columns(entry)
        return IncrementalResultRow(
            entry.tag_id,
            columns,
            tooltip=columns[-1] if self._status_header else "",
            accessible_text="; ".join(columns),
        )

    def _filter(self) -> None:
        terms = tuple(self.search.text().casefold().split())
        self._filtered = tuple(
            entry for entry in self._entries
            if all(term in entry.search_text for term in terms)
            and self._accept_entry(entry)
        )
        selected = set(self.selected_ids())
        self.model.set_rows(
            tuple(
                self._model_row(entry)
                for entry in self._filtered
            ),
            ensure_ids=selected,
        )
        selection = self.view.selectionModel()
        for tag_id in selected:
            index = self.model.index_for_id(tag_id)
            if index.isValid():
                selection.select(
                    index,
                    QItemSelectionModel.Select | QItemSelectionModel.Rows,
                )
        self._update_status()

    def refresh_summaries(self) -> None:
        self._filter()

    def _model_counts_changed(self, loaded: int, total: int) -> None:
        self.result_status.setText(f"Showing {loaded:,} of {total:,} filtered Tags")
        self.more.setVisible(loaded < total)

    def selected_ids(self) -> tuple[str, ...]:
        selection = self.view.selectionModel()
        if selection is None:
            return ()
        return tuple(
            dict.fromkeys(
                self.model.stable_id(index)
                for index in selection.selectedRows(0)
                if self.model.stable_id(index)
            )
        )

    def target_ids(self) -> tuple[str, ...]:
        if self.scope.currentData() == "filtered":
            return tuple(entry.tag_id for entry in self._filtered)
        ids = self.selected_ids()
        if not ids:
            raise ValueError(
                "Select one or more Tags, or explicitly choose All filtered Tags."
            )
        return ids

    def _update_status(self, *_args) -> None:
        selected = len(self.selected_ids())
        if self.scope.currentData() == "filtered":
            self.selection_status.setText(
                f"Batch scope: all {len(self._filtered):,} filtered Tags "
                f"({selected:,} selected)"
            )
        else:
            self.selection_status.setText(
                f"Batch scope: {selected:,} selected Tag"
                f"{'s' if selected != 1 else ''}"
            )

    def focus_tag(self, tag_id: str) -> None:
        index = self.model.index_for_id(tag_id)
        if not index.isValid():
            return
        self.view.selectionModel().select(
            index,
            QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows,
        )
        self.view.scrollTo(index)

    def open_selected_tag(self) -> None:
        selected = self.selected_ids()
        if len(selected) != 1:
            QMessageBox.information(
                self, "Choose One Tag", "Select exactly one Tag to open its details."
            )
            return
        self.workbench.review_tag_id = selected[0]
        self.workbench.accept()


class _NamesMeaningTab(_TagBatchTab):
    def __init__(self, workbench) -> None:
        super().__init__(
            workbench,
            title="Names & Meaning",
            description=(
                "Set one Broader Tag or add/remove one Also Means link across a "
                "selection. Edit aliases and one-off relationships in Tag Details."
            ),
            status_header="Current Meaning",
            status_getter=self._meaning_summary,
        )
        form = QFormLayout()
        self.action = QComboBox()
        self.action.addItem("Set Broader Tag", "broader")
        self.action.addItem("Add Also Means", "add_meaning")
        self.action.addItem("Remove Also Means", "remove_meaning")
        self.target = workbench.tag_choice(
            "Find the Broader or meaning Tag…", allow_none=True,
        )
        form.addRow("Batch action", self.action)
        form.addRow("Target Tag", self.target)
        self.action_layout.addLayout(form)
        buttons = QHBoxLayout()
        preview = QPushButton("Preview Batch…")
        preview.clicked.connect(self._preview)
        details = QPushButton("Open Selected Tag Details")
        details.clicked.connect(self.open_selected_tag)
        buttons.addWidget(preview)
        buttons.addWidget(details)
        buttons.addStretch(1)
        self.action_layout.addLayout(buttons)

    def _meaning_summary(self, tag_id: str) -> str:
        current = self.workbench.batch.semantic.effective_for_tag(tag_id)
        parent = self.workbench.knowledge.tags.get(current.parent_id)
        broader = _display(parent) if parent is not None else "None"
        return f"Broader: {broader} • Also Means: {len(current.implies):,}"

    def _preview(self) -> None:
        try:
            ids = self.target_ids()
            target_id = str(self.target.currentData() or "")
            action = self.action.currentData()
            if action == "broader":
                plan = self.workbench.batch.plan_set_broader_tag(ids, target_id)
            else:
                if not target_id:
                    raise ValueError("Choose a meaning Tag first.")
                plan = self.workbench.batch.plan_also_means(
                    ids, target_id, remove=action == "remove_meaning",
                )
            self.workbench.preview(plan)
        except Exception as exc:
            QMessageBox.warning(self, "Batch Cannot Be Previewed", str(exc))


class _ReusableDetailsTab(_TagBatchTab):
    def __init__(self, workbench) -> None:
        self._resolution_cache = {}
        super().__init__(
            workbench,
            title="Reusable Details",
            description=(
                "Allow a reusable value collection broadly, narrow it to exact "
                "Tags or Category branches, remove it, or reset personal policies."
            ),
            status_header="Current Effective Policy",
            status_getter=self._policy_summary,
        )
        self.policy_filter = QComboBox()
        self.policy_filter.setAccessibleName("Reusable policy filter")
        self.policy_filter.addItem("All policies", "")
        self.policy_filter.addItem("Needs review", "needs_review")
        self.policy_filter.addItem("All values", "all")
        self.policy_filter.addItem("Narrow values", "narrow")
        self.policy_filter.addItem("No collections", "none")
        self.filter_row.addWidget(QLabel("Policies"))
        self.filter_row.addWidget(self.policy_filter)
        self.policy_filter.currentIndexChanged.connect(self._filter)
        form = QFormLayout()
        self.action_form = form
        self.action = QComboBox()
        self.action.addItem("Allow or update a collection", "set")
        self.action.addItem("Remove a collection", "remove")
        self.action.addItem("Reset personal policies", "reset")
        self.family = BoundedSearchableComboBox(
            placeholder="Find a reusable value collection…", max_visible=12,
        )
        definitions = workbench.foundation.modifier_group_definitions(
            workbench.knowledge, workbench.categories,
        )
        self._definitions = definitions
        for family_id in workbench.categories.reusable_modifier_family_ids():
            label = workbench.foundation.modifier_group_label(
                family_id, workbench.categories,
            )
            self.family.addItem(
                f"{label} — {len(definitions.get(family_id, ())):,} reviewed values",
                family_id,
                search_terms=(label, family_id),
            )
        self.family.setCurrentIndex(-1)
        self.mode = QComboBox()
        self.mode.addItem("All values in the collection", VALUE_SCOPE_ALL)
        self.mode.addItem("Only chosen values or branches", VALUE_SCOPE_ONLY)
        self.mode.addItem(
            "All except chosen values or branches", VALUE_SCOPE_ALL_EXCEPT,
        )
        form.addRow("Batch action", self.action)
        form.addRow("Reusable collection", self.family)
        form.addRow("Value scope", self.mode)
        self.action_layout.addLayout(form)
        self.choices_label = QLabel("Chosen values or Category branches")
        self.choices = TokenChipEditor(
            kind="value or branch",
            allow_text=False,
            placeholder="Find a Tag or Category branch…",
        )
        self.choices.add_button.setText("Add Value or Branch")
        self.action_layout.addWidget(self.choices_label)
        self.action_layout.addWidget(self.choices)
        buttons = QHBoxLayout()
        preview = QPushButton("Preview Batch…")
        preview.clicked.connect(self._preview)
        details = QPushButton("Open Selected Tag Details")
        details.clicked.connect(self.open_selected_tag)
        buttons.addWidget(preview)
        buttons.addWidget(details)
        buttons.addStretch(1)
        self.action_layout.addLayout(buttons)
        self.action.currentIndexChanged.connect(self._action_changed)
        self.mode.currentIndexChanged.connect(self._action_changed)
        self.family.currentIndexChanged.connect(self._family_changed)
        self._action_changed()

    def _resolution(self, tag_id: str):
        if tag_id not in self._resolution_cache:
            self._resolution_cache[tag_id] = (
                self.workbench.batch.allowed.effective_for_tag(tag_id)
            )
        return self._resolution_cache[tag_id]

    def _policy_summary(self, tag_id: str) -> str:
        resolution = self._resolution(tag_id)
        if resolution.blocked_reason:
            return "Needs review — " + resolution.blocked_reason
        if not resolution.groups:
            return "No reusable collections"
        parts = []
        for family_id in resolution.groups:
            label = self.workbench.foundation.modifier_group_label(
                family_id, self.workbench.categories,
            )
            scope = resolution.scope_for(family_id)
            scope_label = {
                "all": "All",
                "only": "Only chosen",
                "all_except": "All except chosen",
                "unreviewed": "Needs review",
            }.get(scope.mode, scope.mode)
            parts.append(f"{label}: {scope_label}")
        authority = resolution.authority.replace("_", " ").title()
        return " • ".join((*parts, authority))

    def _accept_entry(self, entry: _TagListEntry) -> bool:
        policy_filter = getattr(self, "policy_filter", None)
        wanted = str(policy_filter.currentData() or "") if policy_filter else ""
        if not wanted:
            return True
        resolution = self._resolution(entry.tag_id)
        scopes = tuple(resolution.value_scopes)
        if wanted == "needs_review":
            return bool(
                resolution.blocked_reason
                or resolution.authority == "compatibility"
                or any(scope.mode == "unreviewed" for scope in scopes)
            )
        if wanted == "all":
            return any(scope.mode == "all" for scope in scopes)
        if wanted == "narrow":
            return any(scope.mode in {"only", "all_except"} for scope in scopes)
        if wanted == "none":
            return not resolution.groups
        return True

    def refresh_summaries(self) -> None:
        self._resolution_cache.clear()
        super().refresh_summaries()

    def _family_changed(self, *_args) -> None:
        family_id = str(self.family.currentData() or "")
        choices = []
        if family_id:
            for category in self.workbench.categories.categories.values():
                if (
                    category.id != family_id
                    and self.workbench.categories.modifier_family_source(category.id)
                    == family_id
                ):
                    choices.append((
                        f"category:{category.id}",
                        f"Category: {category.name}",
                        self.workbench.categories.path_label(category.id),
                    ))
            for value in self._definitions.get(family_id, ()):
                record = self.workbench.knowledge.tags.get(value.id)
                choices.append((
                    f"tag:{value.id}",
                    f"Tag: {value.display}",
                    self.workbench.categories.path_label(
                        getattr(record, "category", "")
                    ),
                ))
        choices.sort(key=lambda item: (item[1].casefold(), item[0]))
        self.choices.set_values(())
        self.choices.set_choices(choices)

    def _action_changed(self, *_args) -> None:
        action = self.action.currentData()
        is_reset = action == "reset"
        is_narrow = action == "set" and self.mode.currentData() != VALUE_SCOPE_ALL
        for widget, visible in (
            (self.family, not is_reset),
            (self.mode, action == "set"),
        ):
            widget.setVisible(visible)
            label = self.action_form.labelForField(widget)
            if label is not None:
                label.setVisible(visible)
        self.choices_label.setVisible(is_narrow)
        self.choices.setVisible(is_narrow)

    def _preview(self) -> None:
        try:
            ids = self.target_ids()
            action = self.action.currentData()
            if action == "reset":
                plan = self.workbench.batch.plan_reset_reusable_policy(ids)
            else:
                family_id = str(self.family.currentData() or "")
                if not family_id:
                    raise ValueError("Choose a reusable value collection first.")
                if action == "remove":
                    plan = self.workbench.batch.plan_reusable_collection(
                        ids, family_id, remove=True,
                    )
                else:
                    mode = str(self.mode.currentData())
                    tag_ids = []
                    category_ids = []
                    for choice in self.choices.values():
                        kind, stable_id = choice.split(":", 1)
                        (tag_ids if kind == "tag" else category_ids).append(stable_id)
                    if mode != VALUE_SCOPE_ALL and not (tag_ids or category_ids):
                        raise ValueError(
                            "Choose at least one exact Tag or Category branch for this scope."
                        )
                    if mode == VALUE_SCOPE_ONLY:
                        scope = AllowedDimensionValueScope(
                            family_id, mode,
                            value_ids=tuple(tag_ids),
                            category_ids=tuple(category_ids),
                        )
                    elif mode == VALUE_SCOPE_ALL_EXCEPT:
                        scope = AllowedDimensionValueScope(
                            family_id, mode,
                            exception_value_ids=tuple(tag_ids),
                            exception_category_ids=tuple(category_ids),
                        )
                    else:
                        scope = AllowedDimensionValueScope(family_id, mode)
                    plan = self.workbench.batch.plan_reusable_collection(
                        ids, family_id, scope=scope,
                    )
            self.workbench.preview(plan)
        except Exception as exc:
            QMessageBox.warning(self, "Batch Cannot Be Previewed", str(exc))


class _CompatibilityTab(_TagBatchTab):
    def __init__(self, workbench) -> None:
        super().__init__(
            workbench,
            title="Compatibility",
            description=(
                "Create one reviewed Contrary Values set from the selected Tags. "
                "Every selected value becomes contrary to every other selected value."
            ),
        )
        form = QFormLayout()
        self.label = QLineEdit()
        self.label.setPlaceholderText("Example: mutually exclusive hair colors")
        self.context = QComboBox()
        self.context.addItem("Same Character", "same_character")
        self.context.addItem("Same Object", "same_object")
        self.context.addItem("Same Image", "image")
        self.context.addItem("Same Context", "same_context")
        form.addRow("Set label", self.label)
        form.addRow("Applies within", self.context)
        self.action_layout.addLayout(form)
        buttons = QHBoxLayout()
        preview = QPushButton("Preview Contrary Set…")
        preview.clicked.connect(self._preview)
        details = QPushButton("Open Selected Tag Details")
        details.clicked.connect(self.open_selected_tag)
        buttons.addWidget(preview)
        buttons.addWidget(details)
        buttons.addStretch(1)
        self.action_layout.addLayout(buttons)

    def _preview(self) -> None:
        try:
            plan = self.workbench.batch.plan_contrary_set(
                self.target_ids(),
                label=self.label.text().strip(),
                scope=str(self.context.currentData()),
            )
            self.workbench.preview(plan)
        except Exception as exc:
            QMessageBox.warning(self, "Batch Cannot Be Previewed", str(exc))


class _AuditTab(QWidget):
    PAGE_SIZE = 200

    def __init__(self, workbench, *, title: str, description: str) -> None:
        super().__init__()
        self.workbench = workbench
        self._rows: tuple[IncrementalResultRow, ...] = ()
        root = QVBoxLayout(self)
        heading = QLabel(title)
        heading.setObjectName("sectionTitle")
        root.addWidget(heading)
        intro = QLabel(description)
        intro.setWordWrap(True)
        root.addWidget(intro)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter this audit…")
        self.search.setClearButtonEnabled(True)
        root.addWidget(self.search)
        self.model = IncrementalResultModel(
            ("Relationship", "Type", "State"), page_size=200, parent=self,
        )
        self.view = QTreeView()
        self.view.setModel(self.model)
        self.view.setRootIsDecorated(False)
        self.view.setUniformRowHeights(True)
        self.view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.SingleSelection)
        header = self.view.header()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        root.addWidget(self.view, 1)
        row = QHBoxLayout()
        self.count = QLabel()
        self.count.setObjectName("mutedLabel")
        self.more = QPushButton("Load 200 More…")
        self.more.setProperty("compact", True)
        self.more.clicked.connect(self.model.fetch_more)
        row.addWidget(self.count, 1)
        row.addWidget(self.more)
        root.addLayout(row)
        self.actions = QHBoxLayout()
        root.addLayout(self.actions)
        self.search.textChanged.connect(self._filter)
        self.model.counts_changed.connect(self._counts)

    def set_rows(self, rows) -> None:
        self._rows = tuple(rows)
        self._filter()

    def _filter(self, *_args) -> None:
        terms = tuple(self.search.text().casefold().split())
        rows = tuple(
            row for row in self._rows
            if all(term in " ".join(row.columns).casefold() for term in terms)
        )
        self.model.set_rows(rows)

    def _counts(self, loaded: int, total: int) -> None:
        self.count.setText(f"Showing {loaded:,} of {total:,} matching relationships")
        self.more.setVisible(loaded < total)

    def selected_relationship_id(self) -> str:
        indexes = self.view.selectionModel().selectedRows(0)
        return self.model.stable_id(indexes[0]) if indexes else ""


class _CharactersTab(_AuditTab):
    def __init__(self, workbench) -> None:
        super().__init__(
            workbench,
            title="Characters",
            description=(
                "Audit character identity, source, version, appearance, and trait "
                "links here. Use the focused Character editor for individual changes."
            ),
        )
        review = QPushButton("Review Character Links…")
        review.clicked.connect(self._review_characters)
        record = QPushButton("Open Selected Record")
        record.clicked.connect(self._open_record)
        tag = QPushButton("Open Related Tag Details")
        tag.clicked.connect(self._open_tag)
        self.actions.addWidget(record)
        self.actions.addWidget(review)
        self.actions.addWidget(tag)
        self.actions.addStretch(1)

    def _review_characters(self) -> None:
        self.workbench.review_character_links = True
        self.workbench.accept()

    def _open_record(self) -> None:
        relationship_id = self.selected_relationship_id()
        relationship = self.workbench.relationship_by_id.get(relationship_id)
        endpoint = next(
            (
                item for item in getattr(relationship, "endpoints", ())
                if item.kind in {"character_source", "character", "outfit"}
            ),
            None,
        )
        if endpoint is None:
            QMessageBox.information(
                self,
                "No Focused Record",
                "This relationship has no Character Source, character, or outfit record to open.",
            )
            return
        self.workbench.review_character_kind = endpoint.kind
        self.workbench.review_character_id = endpoint.id
        self.workbench.accept()

    def _open_tag(self) -> None:
        relationship_id = self.selected_relationship_id()
        relationship = self.workbench.relationship_by_id.get(relationship_id)
        tag_id = next(
            (endpoint.id for endpoint in getattr(relationship, "endpoints", ())
             if endpoint.kind == "tag"),
            "",
        )
        if not tag_id:
            QMessageBox.information(
                self, "No Related Tag", "This relationship has no Tag endpoint to open."
            )
            return
        self.workbench.review_tag_id = tag_id
        self.workbench.accept()


class _MigrationTab(_AuditTab):
    def __init__(self, workbench) -> None:
        super().__init__(
            workbench,
            title="Migration & History",
            description=(
                "Review compatibility records that still need canonical authority "
                "and inspect disabled history without mixing them into active batch work."
            ),
        )
        review = QPushButton("Review Migration…")
        review.clicked.connect(workbench.review_migration)
        self.actions.addWidget(review)
        self.actions.addStretch(1)


class RelationshipWorkspaceDialog(FeedbackEnabledDialog):
    """Task-focused relationship batch and audit workbench."""

    feedback_workflow = "System Knowledge relationship workbench"

    def __init__(
        self,
        knowledge,
        categories,
        characters,
        parent=None,
        *,
        focus_tag_id: str = "",
        initial_kind: str = "",
    ) -> None:
        super().__init__(parent)
        self.knowledge = knowledge
        self.categories = categories
        self.characters = characters
        self.foundation = FoundationKnowledge(
            DEFAULTS_DIR / "modifiers.json", DEFAULTS_DIR / "concepts.json",
        )
        self.batch = RelationshipBatchService(
            knowledge,
            categories,
            self.foundation,
            default_path=DEFAULTS_DIR / "relationships.json",
            user_path=RELATIONSHIPS_PATH,
        )
        self.changed = False
        self.review_tag_id = ""
        self.review_character_links = False
        self.review_character_kind = ""
        self.review_character_id = ""
        self.last_result: RelationshipBatchResult | None = None
        self.relationship_by_id = {}
        self._audit_loaded = False
        self.tag_entries = tuple(
            _TagListEntry(
                record.id,
                record.display,
                categories.path_label(record.category),
                " ".join((
                    record.display,
                    *knowledge.effective_aliases(record),
                    categories.path_label(record.category),
                )).casefold(),
            )
            for record in sorted(
                knowledge.tags.values(),
                key=lambda item: (item.display.casefold(), item.id),
            )
        )
        self.setWindowTitle("Manage Relationships")
        self.resize(1320, 820)
        root = QVBoxLayout(self)
        intro = QLabel(
            "Make broad, reviewed relationship changes by task. Batch actions "
            "default to the selected Tags, show exact before/after outcomes, and "
            "save once with recovery. Individual edits remain in Tag Details or "
            "the focused Character editor."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)
        self.tabs = QTabWidget()
        self.tabs.setMinimumHeight(0)
        self.tabs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        self.names_tab = _NamesMeaningTab(self)
        self.reusable_tab = _ReusableDetailsTab(self)
        self.compatibility_tab = _CompatibilityTab(self)
        self.characters_tab = _CharactersTab(self)
        self.migration_tab = _MigrationTab(self)
        self.tabs.addTab(self.names_tab, "Names && Meaning")
        self.tabs.addTab(self.reusable_tab, "Reusable Details")
        self.tabs.addTab(self.compatibility_tab, "Compatibility")
        self.tabs.addTab(self.characters_tab, "Characters")
        self.tabs.addTab(self.migration_tab, "Migration && History")
        self.tabs.currentChanged.connect(self._tab_changed)
        root.addWidget(self.tabs, 1)
        bottom = QHBoxLayout()
        self.status = QLabel("No batch changes in this session.")
        self.status.setObjectName("mutedLabel")
        self.status.setAccessibleName("Relationship workbench status")
        self.undo_button = QPushButton("Undo Last Batch")
        self.undo_button.setEnabled(False)
        self.undo_button.clicked.connect(self.undo_last_batch)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.accept)
        bottom.addWidget(self.status, 1)
        bottom.addWidget(self.undo_button)
        bottom.addWidget(self.close_button)
        root.addLayout(bottom)
        initial_index = 0
        if initial_kind == ALLOWED_DIMENSION:
            initial_index = 1
        elif initial_kind == SCOPED_CONSTRAINT:
            initial_index = 2
        elif initial_kind in _CHARACTER_KINDS:
            initial_index = 3
        elif initial_kind == MODIFIER_ROLE:
            initial_index = 4
        self.tabs.setCurrentIndex(initial_index)
        if focus_tag_id:
            (self.names_tab, self.reusable_tab, self.compatibility_tab)[
                min(initial_index, 2)
            ].focus_tag(str(focus_tag_id))
        if initial_kind == MODIFIER_ROLE:
            QTimer.singleShot(0,self.review_migration)

    def tag_choice(self, placeholder: str, *, allow_none: bool = False):
        choice = BoundedSearchableComboBox(
            placeholder=placeholder, max_visible=12,
        )
        if allow_none:
            choice.addItem("None — clear this relationship", "")
        for entry in self.tag_entries:
            choice.addItem(
                f"{entry.display} — {entry.category}",
                entry.tag_id,
                search_terms=(entry.display, entry.category),
            )
        choice.setCurrentIndex(0 if allow_none else -1)
        return choice

    def preview(self, plan: RelationshipBatchPlan) -> None:
        dialog = RelationshipBatchPreviewDialog(plan, self)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            result = self.batch.apply(plan)
        except Exception as exc:
            QMessageBox.warning(
                self, "Batch completion not confirmed",
                "Completion could not be confirmed. Keep any recovery records and backups. "
                "Review the current saved state before retrying.\n\n" + str(exc),
            )
            return
        self.last_result = result
        self.changed = True
        self.undo_button.setEnabled(True)
        self.status.setText(
            f"Saved {len(result.tag_ids):,} Tag relationship change"
            f"{'s' if len(result.tag_ids) != 1 else ''} as one transaction."
        )
        self.names_tab.refresh_summaries()
        self.reusable_tab.refresh_summaries()

    def undo_last_batch(self) -> None:
        if self.last_result is None:
            return
        try:
            result = self.last_result
            self.batch.undo(result)
        except Exception as exc:
            QMessageBox.warning(self, "Batch Not Undone", str(exc))
            return
        self.last_result = None
        self.changed = True
        self.undo_button.setEnabled(False)
        self.status.setText(
            f"Undid the last batch for {len(result.tag_ids):,} Tag"
            f"{'s' if len(result.tag_ids) != 1 else ''}."
        )
        self.names_tab.refresh_summaries()
        self.reusable_tab.refresh_summaries()

    def _tab_changed(self, index: int) -> None:
        if index >= 3 and not self._audit_loaded:
            self._load_audits()

    def _legacy_registry(self):
        return KnowledgeRelationshipRegistry.from_legacy(
            self.knowledge,
            categories=self.categories,
            conflicts=ConflictRuleEngine.load(DEFAULTS_DIR / "conflicts.json"),
            characters=self.characters,
            include_advisory=True,
        )

    def _load_audits(self) -> None:
        try:
            legacy = self._legacy_registry()
            entries = self.batch.workspace.workspace_entries(legacy)
        except Exception as exc:
            QMessageBox.warning(self, "Relationship Audit Unavailable", str(exc))
            return
        self._audit_loaded = True
        character_rows = []
        migration_rows = []
        self.relationship_by_id = {
            entry.relationship.id: entry.relationship for entry in entries
        }
        for entry in entries:
            relationship = entry.relationship
            endpoint_labels = [
                endpoint.label or endpoint.id for endpoint in relationship.endpoints
            ]
            label = relationship.label or " → ".join(endpoint_labels)
            state = (
                "Disabled history" if relationship.disabled
                else "Needs migration" if entry.migration_state == "migration_required"
                else "Active"
            )
            row = IncrementalResultRow(
                relationship.id,
                (label, relationship.kind.replace("_", " ").title(), state),
                tooltip=f"{entry.authority_layer}; {entry.migration_state}",
            )
            if relationship.kind in _CHARACTER_KINDS:
                character_rows.append(row)
            if relationship.disabled or entry.migration_state == "migration_required":
                migration_rows.append(row)
        self.characters_tab.set_rows(character_rows)
        self.migration_tab.set_rows(migration_rows)

    def review_migration(self) -> None:
        try:
            registry = self._legacy_registry()
            assessment = self.batch.workspace.assess_legacy_migration(registry)
            decisions = ModifierRoleDecisionService(
                self.knowledge,
                self.categories,
                self.foundation,
                workspace=self.batch.workspace,
            )
            report = RelationshipMigrationReportDialog(
                assessment,
                decisions.inventory(),
                self,
                decision_service=decisions,
                relationship_review_count=sum(
                    1 for row in self.migration_tab._rows
                    if "Needs migration" in row.columns
                ),
            )
        except Exception as exc:
            QMessageBox.warning(self, "Migration Review Unavailable", str(exc))
            return
        report.exec()
        if report.changed:
            self.changed = True
            self.batch._reload_services()
            self._audit_loaded = False
            self._load_audits()
