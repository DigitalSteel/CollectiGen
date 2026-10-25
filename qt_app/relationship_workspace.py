from __future__ import annotations

from dataclasses import dataclass, replace
from uuid import uuid4

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTreeView,
    QVBoxLayout,
    QWidget,
)
from .status_notifications import FooterMessageBox as QMessageBox

from core.conflict_rules import ConflictRuleEngine
from core.allowed_dimension_policy import (
    AllowedDimensionPolicyService,
    VALUE_SCOPE_ALL,
    VALUE_SCOPE_ALL_EXCEPT,
    VALUE_SCOPE_ONLY,
    VALUE_SCOPE_UNREVIEWED,
)
from core.knowledge_engine import FoundationKnowledge
from core.modifier_role_decisions import (
    CHANGE_MODIFIER_FAMILY,
    KEEP_ORDINARY,
    RETAIN_MODIFIER_ROLE,
    ModifierRoleDecisionService,
)
from core.knowledge_relationships import (
    ADVISORY_ASSOCIATION,
    ALLOWED_DIMENSION,
    ASSOCIATED_CHARACTER,
    CATEGORY_BASE,
    CHARACTER_SOURCE,
    ENTAILMENT,
    ENTITY_TAG,
    IDENTITY_ALIAS,
    MODIFIER_ROLE,
    MODIFIER_VALUE,
    OTHER_APPEARANCE,
    SCOPED_CONSTRAINT,
    SPECIES_TRAIT,
    SPECIFICITY,
    VERSION_OF,
    KnowledgeRelationship,
    KnowledgeRelationshipRegistry,
    RelationshipEndpoint,
)
from core.paths import DEFAULTS_DIR, RELATIONSHIPS_PATH
from core.relationship_workspace import (
    RelationshipWorkspaceService,
    relationship_fingerprint,
)
from core.tag_collection import normalize_tag
from .components import (
    BoundedSearchableComboBox,
    IncrementalResultModel,
    IncrementalResultRow,
)
from .modal_feedback import FeedbackEnabledDialog


@dataclass(frozen=True)
class _TypeChoice:
    kind: str
    label: str
    tooltip: str
    endpoints: tuple[tuple[str, str, bool], ...]
    directed: bool = True
    scope: str = "any"


_TYPES = (
    _TypeChoice(
        IDENTITY_ALIAS,
        "Other Names",
        "Another written name that resolves to one canonical Tag.",
        (("Other name", "alias_text", False), ("Canonical Tag", "tag", False)),
    ),
    _TypeChoice(
        ALLOWED_DIMENSION,
        "Allowed Details",
        "Details that can specialize this Tag, such as Color or Length.",
        (("Base Tag", "tag", False), ("Detail", "modifier_dimension", False)),
        scope="same_character",
    ),
    _TypeChoice(
        SPECIFICITY,
        "Broader Tag",
        "A less-specific Tag that is also true in the same context.",
        (("Detailed Tag", "tag", False), ("Broader Tag", "tag", False)),
        scope="same_character",
    ),
    _TypeChoice(
        ENTAILMENT,
        "Also Means",
        "Another Tag that is always true under this relationship's scope.",
        (("When this Tag", "tag", False), ("Also means", "tag", False)),
    ),
    _TypeChoice(
        ADVISORY_ASSOCIATION,
        "Usually Related",
        "A possible association that remains a review suggestion.",
        (("Starting item", "any_entity", False), ("Related item", "any_entity", False)),
    ),
    _TypeChoice(
        SCOPED_CONSTRAINT,
        "Contrary Values",
        "Values that cannot both describe the same subject and context.",
        (
            ("Value 1", "tag", False),
            ("Value 2", "tag", False),
            ("Value 3", "tag", True),
            ("Value 4", "tag", True),
        ),
        directed=False,
        scope="same_character",
    ),
    _TypeChoice(
        CATEGORY_BASE,
        "Category Base",
        "The reviewed base Tag used to organize reusable category details.",
        (("Category", "category", False), ("Base Tag", "tag", False)),
    ),
    _TypeChoice(
        MODIFIER_VALUE,
        "Modifier Value",
        "A reviewed detail value used to build a more specific Tag.",
        (("Tag", "tag", False), ("Value", "modifier_value", False)),
    ),
    _TypeChoice(
        ENTITY_TAG,
        "Entity Tag",
        "Links a Character Source, character, or outfit to its reviewed SDXL Tag.",
        (("Knowledge record", "character_entity", False), ("SDXL Tag", "tag", False)),
    ),
    _TypeChoice(
        CHARACTER_SOURCE,
        "Character Source",
        "Links a character to the work or franchise they come from.",
        (("Character", "character", False), ("Source", "character_source", False)),
    ),
    _TypeChoice(
        ASSOCIATED_CHARACTER,
        "Associated Characters",
        "Links an outfit or appearance to the characters that use it.",
        (("Outfit", "outfit", False), ("Character", "character", False)),
    ),
    _TypeChoice(
        OTHER_APPEARANCE,
        "Other Appearances",
        "Links a character or outfit to an additional work without changing its primary source.",
        (("Character or outfit", "character_or_outfit", False), ("Additional source", "character_source", False)),
    ),
    _TypeChoice(
        VERSION_OF,
        "Version Of",
        "Links a distinct alternate form to its base character without merging their identities.",
        (("Alternate form", "character", False), ("Base character", "character", False)),
    ),
    _TypeChoice(
        SPECIES_TRAIT,
        "Species / Traits",
        "Relates a character or character-owned Tag to reviewed species or trait vocabulary.",
        (("Character or Tag", "character_or_tag", False), ("Species or trait Tag", "tag", False)),
        scope="same_character",
    ),
)
_TYPE_BY_KIND = {item.kind: item for item in _TYPES}
_SCOPES = (
    ("Any", "any"),
    ("Same Image", "image"),
    ("Same Character", "same_character"),
    ("Same Object", "same_object"),
    ("Same Context", "same_context"),
)


class _EndpointChoiceModel(QStandardItemModel):
    """Reusable stable-ID endpoint choices without per-combo item copies."""

    def __init__(self,kind,values,*,optional=False,parent=None):
        super().__init__(parent)
        self.kind=str(kind)
        self._positions={}
        self._position_offset=1 if optional else 0
        items=[]
        if optional:
            items.append(self._item("(Not used)",None,"(Not used)"))
        for endpoint_kind,endpoint_id,endpoint_label in values:
            items.append(
                self._endpoint_item(endpoint_kind,endpoint_id,endpoint_label)
            )
        self.invisibleRootItem().appendRows(items)

    def _display(self,endpoint_kind,endpoint_label):
        prefix=(
            endpoint_kind.replace("_"," ").title()+" · "
            if self.kind in {
                "character_entity","character_or_outfit","character_or_tag",
                "any_entity",
            }
            else ""
        )
        return prefix+endpoint_label

    @staticmethod
    def _item(display,value,search):
        item=QStandardItem(display)
        item.setData(value,Qt.UserRole)
        item.setData(search,BoundedSearchableComboBox.SEARCH_ROLE)
        item.setData(
            () if value is None else (value[1],value[2]),
            BoundedSearchableComboBox.SEARCH_EXTRAS_ROLE,
        )
        item.setData(display,Qt.AccessibleTextRole)
        return item

    def _endpoint_item(self,endpoint_kind,endpoint_id,endpoint_label):
        value=(str(endpoint_kind),str(endpoint_id),str(endpoint_label))
        position=self._position_offset+len(self._positions)
        display=self._display(value[0],value[2])
        search="\n".join(dict.fromkeys((display,value[2],value[1])))
        self._positions[value[:2]]=position
        return self._item(display,value,search)

    def ensure_endpoint(self,endpoint):
        key=(str(endpoint.kind),str(endpoint.id))
        match=self._positions.get(key)
        if match is not None:
            return match
        position=self.rowCount()
        item=self._endpoint_item(
            endpoint.kind,endpoint.id,endpoint.label or endpoint.id,
        )
        self.appendRow(item)
        return position


class _EndpointRow(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        self.choice = BoundedSearchableComboBox(max_visible=14)
        self.text = QLineEdit()
        self.text.setPlaceholderText("Enter the other name")
        self.stack.addWidget(self.choice)
        self.stack.addWidget(self.text)
        layout.addWidget(self.stack)
        self.expected_kind = "tag"

    def set_text_mode(self, enabled: bool) -> None:
        self.stack.setCurrentWidget(self.text if enabled else self.choice)


class ModifierRoleDecisionDialog(FeedbackEnabledDialog):
    """One explicit preview for a legacy modifier-role decision."""

    feedback_workflow = "Review legacy modifier role"

    def __init__(self, candidate, family_choices, parent=None):
        super().__init__(parent)
        self.candidate = candidate
        self.setWindowTitle("Review Modifier Role")
        self.resize(760, 560)
        layout=QVBoxLayout(self)
        heading=QLabel(f"Decide how “{candidate.display}” should behave")
        heading.setObjectName("sectionTitle")
        heading.setWordWrap(True)
        layout.addWidget(heading)
        intro=QLabel(
            "Legacy names and aliases are evidence only. This decision changes "
            "the Tag’s reusable modifier role in System Knowledge; it does not "
            "move the Tag, change its Category, or edit any image or file."
        )
        intro.setWordWrap(True)
        intro.setObjectName("mutedLabel")
        layout.addWidget(intro)
        form=QFormLayout()
        self.stable_id=QLabel(candidate.tag_id)
        self.stable_id.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow("Stable ID",self.stable_id)
        form.addRow("Current Category",QLabel(candidate.category_label))
        current=RelationshipMigrationReportDialog._status_label(candidate)
        current_label=QLabel(current)
        current_label.setWordWrap(True)
        form.addRow("Current Authority",current_label)
        evidence=QLabel(RelationshipMigrationReportDialog._legacy_label(candidate))
        evidence.setWordWrap(True)
        evidence.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow("Legacy Evidence",evidence)
        layout.addLayout(form)

        self.action=QComboBox()
        self.action.setAccessibleName("Choose the reviewed Modifier Role decision")
        self.action.addItem("Choose a decision…","")
        self.action.addItem("Retain Modifier Role",RETAIN_MODIFIER_ROLE)
        self.action.addItem("Change Family",CHANGE_MODIFIER_FAMILY)
        self.action.addItem("Keep Ordinary",KEEP_ORDINARY)
        self.family=QComboBox()
        self.family.setAccessibleName("Choose the reusable modifier family")
        self.family.addItem("Choose a reusable family…","")
        for family_id,label in family_choices:
            self.family.addItem(label,family_id)
        self.decision_form=QFormLayout()
        self.decision_form.addRow("Decision",self.action)
        self.decision_form.addRow("Modifier Family",self.family)
        layout.addLayout(self.decision_form)
        self.consequence=QLabel()
        self.consequence.setWordWrap(True)
        self.consequence.setObjectName("mutedLabel")
        self.consequence.setAccessibleName("Modifier Role decision preview")
        layout.addWidget(self.consequence)
        recovery=QLabel(
            "Applying saves one stale-guarded relationship override with a "
            "recovery copy and semantic read-back. The exact prior reviewed "
            "decision remains in its audit history."
        )
        recovery.setWordWrap(True)
        recovery.setObjectName("mutedLabel")
        layout.addWidget(recovery)
        layout.addStretch(1)
        buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        self.apply_button=buttons.addButton(
            "Apply Decision",QDialogButtonBox.AcceptRole,
        )
        self.apply_button.setAccessibleName("Apply reviewed Modifier Role decision")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.action.currentIndexChanged.connect(self._decision_changed)
        self.family.currentIndexChanged.connect(self._refresh_preview)
        if candidate.reviewed_decision:
            index=self.action.findData(candidate.reviewed_decision)
            if index>=0:self.action.setCurrentIndex(index)
            if candidate.decision_family_id:
                family_index=self.family.findData(candidate.decision_family_id)
                if family_index>=0:self.family.setCurrentIndex(family_index)
        self._refresh_preview()

    def _preferred_family_id(self):
        for family_id in (
            self.candidate.decision_family_id,
            self.candidate.current_family_id,
            *self.candidate.suggested_family_ids,
        ):
            if family_id and self.family.findData(family_id)>=0:
                return family_id
        return ""

    def _decision_changed(self, *_):
        action=str(self.action.currentData() or "")
        family_required=action in {RETAIN_MODIFIER_ROLE,CHANGE_MODIFIER_FAMILY}
        self.decision_form.setRowVisible(self.family,family_required)
        if family_required and not self.family.currentData():
            preferred=self._preferred_family_id()
            if preferred:
                self.family.setCurrentIndex(self.family.findData(preferred))
        self._refresh_preview()

    def _refresh_preview(self, *_):
        action=str(self.action.currentData() or "")
        family_id=str(self.family.currentData() or "")
        family_label=self.family.currentText() if family_id else ""
        family_required=action in {RETAIN_MODIFIER_ROLE,CHANGE_MODIFIER_FAMILY}
        self.apply_button.setEnabled(bool(action) and (not family_required or bool(family_id)))
        if not action:
            text="Choose one reviewed outcome. Nothing changes until Apply Decision."
        elif action==KEEP_ORDINARY:
            text=(
                "Result: this stable Tag is explicitly ordinary. That decision "
                "overrides both lexical evidence and any inherited Category family."
            )
        elif action==RETAIN_MODIFIER_ROLE:
            text=(
                f"Result: this stable Tag keeps an explicit modifier role in “{family_label}”. "
                "The selected family becomes its reviewed role even if Category inheritance changes."
            )
        else:
            text=(
                f"Result: this stable Tag receives an explicit modifier role in “{family_label}”. "
                "Its Category remains unchanged."
            )
        self.consequence.setText(text)

    def requested_action(self):
        return str(self.action.currentData() or "")

    def requested_family_id(self):
        return (
            "" if self.requested_action()==KEEP_ORDINARY
            else str(self.family.currentData() or "")
        )


class RelationshipMigrationReportDialog(FeedbackEnabledDialog):
    """Bounded inventory and reviewed modifier-role decision entry point."""

    feedback_workflow = "Review relationship migration report"
    PAGE_SIZE = 200

    def __init__(
        self,
        assessment,
        modifier_inventory,
        parent=None,
        *,
        decision_service=None,
        relationship_review_count=None,
    ):
        super().__init__(parent)
        self.assessment = assessment
        self.modifier_inventory = modifier_inventory
        self.decision_service = decision_service
        self.relationship_review_count = (
            len(assessment.additions)
            if relationship_review_count is None
            else max(0, int(relationship_review_count))
        )
        self.changed = False
        self.review_relationships_requested = False
        self.setWindowTitle("Relationship Migration Review")
        self.resize(1180, 720)
        layout = QVBoxLayout(self)
        heading = QLabel("Review legacy relationship evidence")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        intro = QLabel(
            "Opening this report changes nothing. A separately applied modifier "
            "decision writes one reviewed System Knowledge relationship; Tags, "
            "Categories, Image Knowledge, captions, Metadata, and image files remain unchanged."
            if decision_service is not None else
            "This report inventories compatibility data by stable ID. It does not "
            "change Tags, Categories, relationships, Image Knowledge, captions, "
            "Metadata, or files."
        )
        intro.setWordWrap(True)
        intro.setObjectName("mutedLabel")
        layout.addWidget(intro)
        relationship_kinds = ", ".join(
            f"{kind.replace('_',' ').title()} {count:,}"
            for kind, count in assessment.counts_by_kind
        ) or "None"
        relationship_summary = QLabel(
            f"Legacy relationships: {assessment.legacy_total:,} found; "
            f"{assessment.already_represented:,} already represented; "
            f"{len(assessment.additions):,} additions require review. "
            f"{self.relationship_review_count:,} can be reviewed in the "
            "workspace's Needs Migration queue; remaining additions stay "
            "report-only until their authority path is implemented. "
            f"Inventory: {relationship_kinds}."
        )
        relationship_summary.setWordWrap(True)
        layout.addWidget(relationship_summary)
        issue_text = (
            "Structural issues: " + "; ".join(assessment.issues[:5])
            if assessment.issues else "No structural relationship issues were found."
        )
        self.relationship_issues = QLabel(issue_text)
        self.relationship_issues.setWordWrap(True)
        self.relationship_issues.setObjectName(
            "warningLabel" if assessment.issues else "mutedLabel"
        )
        layout.addWidget(self.relationship_issues)
        self.modifier_summary = QLabel()
        self.modifier_summary.setWordWrap(True)
        layout.addWidget(self.modifier_summary)
        self._update_summary()

        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setPlaceholderText(
            "Search stable IDs, Tags, Categories, legacy values, or families…"
        )
        self.search.setAccessibleName("Search legacy modifier candidates")
        self.status_filter = QComboBox()
        self.status_filter.setAccessibleName(
            "Filter modifier candidates by authority status"
        )
        for label, key in (
            ("All authority states", "all"),
            ("Needs review", "review"),
            ("Reviewed decisions", "reviewed"),
            ("Category-backed", "category_backed"),
            ("Ambiguous evidence", "ambiguous"),
            ("Family mismatch", "family_mismatch"),
            ("Stale decisions", "stale_decision"),
        ):
            self.status_filter.addItem(label, key)
        self.group_filter = QComboBox()
        self.group_filter.setAccessibleName(
            "Filter modifier candidates by legacy family"
        )
        self.group_filter.addItem("All legacy families", "")
        for group, count in self.modifier_inventory.counts_by_group:
            self.group_filter.addItem(
                f"{group.replace('_',' ').title()} — {count:,}", group,
            )
        filters.addWidget(self.search, 1)
        filters.addWidget(self.status_filter)
        filters.addWidget(self.group_filter)
        layout.addLayout(filters)

        count_row = QHBoxLayout()
        self.result_count = QLabel()
        self.result_count.setObjectName("mutedLabel")
        self.load_more = QPushButton("Load More…")
        self.load_more.setProperty("compact", True)
        self.load_more.hide()
        count_row.addWidget(self.result_count, 1)
        count_row.addWidget(self.load_more)
        layout.addLayout(count_row)
        self.model = IncrementalResultModel(
            (
                "SDXL Tag", "Stable ID", "Current Category", "Legacy Match",
                "Authority Status",
            ),
            page_size=self.PAGE_SIZE,
            parent=self,
        )
        self.results = QTreeView()
        self.results.setModel(self.model)
        self.results.setRootIsDecorated(False)
        self.results.setUniformRowHeights(True)
        self.results.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results.setSelectionMode(QAbstractItemView.SingleSelection)
        self.results.setAccessibleName("Legacy modifier migration candidates")
        header = self.results.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.resizeSection(1, 180)
        header.resizeSection(2, 245)
        header.resizeSection(3, 255)
        header.resizeSection(4, 220)
        layout.addWidget(self.results, 1)
        review_note = QLabel(
            "Select one candidate and choose Retain Modifier Role, Change Family, "
            "or Keep Ordinary. Each applied decision revalidates the source "
            "fingerprint and saves one recoverable canonical relationship."
            if decision_service is not None else
            "This report is read-only because no Modifier Role decision service is available."
        )
        review_note.setWordWrap(True)
        review_note.setObjectName("mutedLabel")
        layout.addWidget(review_note)
        self.decision_status=QLabel()
        self.decision_status.setWordWrap(True)
        self.decision_status.setObjectName("mutedLabel")
        self.decision_status.setAccessibleName("Modifier Role save status")
        layout.addWidget(self.decision_status)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self.review_relationships = buttons.addButton(
            f"Review {self.relationship_review_count:,} in Workspace…",
            QDialogButtonBox.ActionRole,
        )
        self.review_relationships.setEnabled(bool(self.relationship_review_count))
        self.review_relationships.setToolTip(
            "Open the workspace's Needs Migration queue. Each relationship "
            "still requires its own reviewed authority and scope decision."
        )
        self.review_relationships.clicked.connect(
            self._request_relationship_review,
        )
        self.review_selected=buttons.addButton(
            "Review Selected…",QDialogButtonBox.ActionRole,
        )
        self.review_selected.setEnabled(False)
        self.review_selected.setVisible(decision_service is not None)
        self.review_selected.clicked.connect(self._review_selected)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.search.textChanged.connect(self._refresh)
        self.status_filter.currentIndexChanged.connect(self._refresh)
        self.group_filter.currentIndexChanged.connect(self._refresh)
        self.load_more.clicked.connect(self.model.fetch_more)
        self.model.counts_changed.connect(self._counts_changed)
        self.results.selectionModel().selectionChanged.connect(
            self._selection_changed,
        )
        self.results.doubleClicked.connect(
            lambda *_:self._review_selected(),
        )
        self._refresh()

    def _request_relationship_review(self):
        self.review_relationships_requested = True
        self.accept()

    def _update_summary(self):
        inventory=self.modifier_inventory
        reviewed=inventory.reviewed_modifier_count+inventory.reviewed_ordinary_count
        self.modifier_summary.setText(
            f"Legacy modifier values: {inventory.matched_legacy_values:,} of "
            f"{inventory.total_legacy_values:,} match {len(inventory.candidates):,} "
            f"stable Tag records. Reviewed decisions: {reviewed:,}; "
            f"Category-backed without an explicit decision: "
            f"{inventory.category_backed_count:,}; Tags requiring review: "
            f"{inventory.review_required_count:,}. "
            f"{inventory.unmatched_legacy_values:,} remain unmatched, including "
            "potential exceptional non-Tag values."
        )

    @staticmethod
    def _status_label(candidate):
        if candidate.status == "reviewed_modifier":
            return (
                "Reviewed modifier — "
                f"{candidate.decision_family_label or candidate.decision_family_id}"
            )
        if candidate.status == "reviewed_ordinary":
            return "Reviewed ordinary Tag"
        if candidate.status == "stale_decision":
            return "Review required — prior decision evidence changed"
        if candidate.status == "category_backed":
            return (
                "Category-backed — "
                f"{candidate.current_family_label or candidate.current_family_id}"
            )
        if candidate.status == "family_mismatch":
            return "Review required — family mismatch"
        if candidate.status == "ambiguous":
            return "Review required — ambiguous evidence"
        return "Review required — no stable family decision"

    @staticmethod
    def _legacy_label(candidate):
        values=[]
        for match in candidate.matches:
            source=" / ".join(match.sources)
            values.append(
                f"{match.display} — {match.group.replace('_',' ').title()} ({source})"
            )
        return "; ".join(values)

    def _matching_rows(self):
        query=self.search.text().strip().casefold()
        status=str(self.status_filter.currentData() or "all")
        group=str(self.group_filter.currentData() or "")
        rows=[]
        for candidate in self.modifier_inventory.candidates:
            if status == "review" and not candidate.requires_review:
                continue
            if status == "reviewed" and candidate.status not in {
                "reviewed_modifier", "reviewed_ordinary",
            }:
                continue
            if status not in {"all", "review", "reviewed"}:
                if candidate.status != status:
                    continue
            if group and group not in candidate.legacy_groups:
                continue
            legacy=self._legacy_label(candidate)
            authority=self._status_label(candidate)
            suggestions=(
                ", ".join(candidate.suggested_family_labels)
                or "No exact stable family"
            )
            ambiguity="; ".join(candidate.ambiguity_reasons) or "None"
            haystack=" ".join((
                candidate.tag_id,candidate.display,candidate.category_id,
                candidate.category_label,legacy,authority,suggestions,ambiguity,
            )).casefold()
            if query and query not in haystack:
                continue
            tooltip=(
                f"Stable ID: {candidate.tag_id}\n"
                f"Current Category: {candidate.category_label} "
                f"({candidate.category_id})\n"
                f"Legacy evidence: {legacy}\n"
                f"Suggested stable families: {suggestions}\n"
                f"Reviewed decision: {candidate.reviewed_decision or 'None'}\n"
                f"Decision relationship: "
                f"{candidate.decision_relationship_id or 'None'}\n"
                f"Decision provenance: "
                f"{candidate.decision_layer or 'None'} / "
                f"{candidate.decision_source or 'None'}"
                f"{(' / ' + candidate.decision_reviewed_at) if candidate.decision_reviewed_at else ''}\n"
                f"Effective modifier family: "
                f"{candidate.effective_family_label or 'Ordinary'}\n"
                f"Ambiguity: {ambiguity}\n"
                f"Source fingerprint: {candidate.fingerprint}"
            )
            rows.append(IncrementalResultRow(
                candidate.tag_id,
                (
                    candidate.display,candidate.tag_id,candidate.category_label,
                    legacy,authority,
                ),
                tooltip,
                f"{candidate.display}; {candidate.tag_id}; "
                f"{candidate.category_label}; {legacy}; {authority}",
            ))
        return rows

    def _refresh(self, *_):
        self.model.set_rows(self._matching_rows())
        self._counts_changed(self.model.loaded_count,self.model.total_count)
        self._selection_changed()

    def _selected_candidate(self):
        index=self.results.currentIndex()
        stable_id=self.model.stable_id(index)
        return next(
            (
                item for item in self.modifier_inventory.candidates
                if item.tag_id==stable_id
            ),
            None,
        )

    def _selection_changed(self, *_):
        self.review_selected.setEnabled(
            self.decision_service is not None
            and self._selected_candidate() is not None
        )

    def _review_selected(self):
        candidate=self._selected_candidate()
        if candidate is None or self.decision_service is None:return
        dialog=ModifierRoleDecisionDialog(
            candidate,self.decision_service.family_choices(),self,
        )
        if dialog.exec()!=QDialog.Accepted:return
        try:
            preview=self.decision_service.preview(
                candidate.tag_id,candidate.fingerprint,
                dialog.requested_action(),
                family_id=dialog.requested_family_id(),
            )
            self.decision_service.apply(preview)
            self.modifier_inventory=self.decision_service.inventory()
        except Exception as exc:
            QMessageBox.warning(
                self,"Modifier Role Not Saved",
                f"The decision was not saved. System Knowledge was left or restored unchanged.\n\n{exc}",
            )
            try:self.modifier_inventory=self.decision_service.inventory()
            except Exception:pass
            self._update_summary(); self._refresh()
            return
        self.changed=True
        self._update_summary(); self._refresh()
        self.decision_status.setText(
            f"Saved the reviewed Modifier Role decision for {candidate.display}. "
            "Tags, Categories, Image Knowledge, captions, Metadata, and image files were unchanged."
        )

    def _counts_changed(self, shown, total):
        all_candidates=len(self.modifier_inventory.candidates)
        self.result_count.setText(
            f"Showing {shown:,} of {total:,} matching candidate Tags; "
            f"{all_candidates:,} total exact-match candidates."
        )
        self.load_more.setVisible(shown<total)
        self.load_more.setText(
            f"Load {min(self.PAGE_SIZE,total-shown):,} More…"
            if shown<total else "Load More…"
        )


class AllowedDetailsPolicyAuditDialog(FeedbackEnabledDialog):
    """Bounded, read-only review of direct Allowed Details policy owners."""

    feedback_workflow = "Allowed Details policy review"
    PAGE_SIZE = 200

    def __init__(self, service, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.categories = service.categories
        self.entries = service.audit_entries()
        self._entries_by_id = {entry.stable_id: entry for entry in self.entries}
        self.review_tag_id = ""
        self.setWindowTitle("Review Allowed Details Policies")
        self.resize(1080, 680)

        layout = QVBoxLayout(self)
        heading = QLabel("Review broad, narrow, and unreviewed reusable policies")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        intro = QLabel(
            "Each row is one direct Base Tag and detail-family policy. Inherited "
            "Tags are represented by their editable source owner instead of being "
            "repeated. Needs review means the stored scope is missing, invalid, or "
            "still compatibility data; it does not guess whether a combination "
            "makes semantic sense. This review changes nothing."
        )
        intro.setWordWrap(True)
        intro.setObjectName("mutedLabel")
        intro.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(intro)

        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(
            "Find a Base Tag, family, Category, source, or stable ID…"
        )
        self.search.setClearButtonEnabled(True)
        self.scope_filter = QComboBox()
        self.scope_filter.setAccessibleName("Filter Allowed Details policies")
        self.scope_filter.addItem("Needs review", "needs_review")
        self.scope_filter.addItem("All policies", "all")
        self.scope_filter.addItem("All values", "all_values")
        self.scope_filter.addItem("Narrow scopes", "narrow")
        self.scope_filter.addItem("Compatibility", "compatibility")
        self.scope_filter.addItem("No allowed details", "none")
        filters.addWidget(self.search, 1)
        filters.addWidget(self.scope_filter)
        layout.addLayout(filters)

        self.model = IncrementalResultModel(
            ("Base Tag", "Detail family", "Value scope", "Policy source", "Category"),
            page_size=self.PAGE_SIZE,
            parent=self,
        )
        self.table = QTreeView()
        self.table.setModel(self.model)
        self.table.setRootIsDecorated(False)
        self.table.setUniformRowHeights(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAccessibleName("Allowed Details direct policy audit")
        header = self.table.header()
        header.setMinimumSectionSize(100)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)

        result_row = QHBoxLayout()
        self.result_status = QLabel()
        self.result_status.setWordWrap(True)
        self.result_status.setObjectName("mutedLabel")
        self.result_status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.load_more = QPushButton("Load More…")
        self.load_more.setProperty("compact", True)
        self.load_more.clicked.connect(self.model.fetch_more)
        result_row.addWidget(self.result_status, 1)
        result_row.addWidget(self.load_more)
        layout.addLayout(result_row)

        self.policy_details = QLabel("Select one policy to inspect its exact authority.")
        self.policy_details.setWordWrap(True)
        self.policy_details.setObjectName("mutedLabel")
        self.policy_details.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.policy_details.setAccessibleName("Selected Allowed Details policy")
        layout.addWidget(self.policy_details)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self.review_button = buttons.addButton(
            "Review in Tag Details…", QDialogButtonBox.AcceptRole,
        )
        self.review_button.setProperty("primary", True)
        self.review_button.setToolTip(
            "Open this Base Tag in the one editor that saves its families and value scopes."
        )
        self.review_button.setEnabled(False)
        self.review_button.clicked.connect(self._review_selected)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self._refresh)
        self.search.textChanged.connect(lambda *_args: self._search_timer.start())
        self.scope_filter.currentIndexChanged.connect(self._refresh)
        self.table.selectionModel().selectionChanged.connect(
            lambda *_args: self._selection_changed()
        )
        self.table.doubleClicked.connect(lambda _index: self._review_selected())
        self.model.counts_changed.connect(self._counts_changed)
        self._refresh()

    def _category_label(self, category_id: str) -> str:
        if not category_id:
            return "Uncategorized"
        category = self.categories.get(category_id)
        return (
            self.categories.path_label(category_id)
            if category is not None else f"Unavailable ({category_id})"
        )

    @staticmethod
    def _source_label(entry) -> str:
        return {
            "canonical_user": "My reviewed policy",
            "canonical_default": "Shipped relationship",
            "compatibility": "Compatibility",
            "foundation": "Shipped foundation",
        }.get(entry.authority, entry.source_label or entry.authority or "Unknown")

    @staticmethod
    def _scope_label(entry) -> str:
        if entry.blocked_reason:
            return "Needs review"
        if entry.scope_mode == VALUE_SCOPE_UNREVIEWED:
            return "Needs review"
        if entry.scope_mode == VALUE_SCOPE_ALL:
            return "All values"
        if entry.scope_mode == VALUE_SCOPE_ONLY:
            suffix = (
                f" + {entry.exception_count:,} exception"
                f"{'s' if entry.exception_count != 1 else ''}"
                if entry.exception_count else ""
            )
            return f"Only {entry.selected_count:,} chosen{suffix}"
        if entry.scope_mode == VALUE_SCOPE_ALL_EXCEPT:
            suffix = (
                f" + {entry.exception_count:,} re-allowed"
                if entry.exception_count else ""
            )
            return f"All except {entry.selected_count:,}{suffix}"
        return "No allowed details"

    def _matches_filter(self, entry, filter_id: str) -> bool:
        if filter_id == "needs_review":
            return entry.needs_review
        if filter_id == "all_values":
            return entry.scope_mode == VALUE_SCOPE_ALL and not entry.blocked_reason
        if filter_id == "narrow":
            return entry.scope_mode in {VALUE_SCOPE_ONLY, VALUE_SCOPE_ALL_EXCEPT}
        if filter_id == "compatibility":
            return entry.authority == "compatibility"
        if filter_id == "none":
            return entry.scope_mode == "none" and not entry.blocked_reason
        return True

    def _refresh(self, *_args) -> None:
        selected_id = self.model.stable_id(self.table.currentIndex())
        query = self.search.text().strip().casefold()
        filter_id = str(self.scope_filter.currentData() or "all")
        rows = []
        for entry in self.entries:
            if not self._matches_filter(entry, filter_id):
                continue
            category_label = self._category_label(entry.category_id)
            source_label = self._source_label(entry)
            scope_label = self._scope_label(entry)
            search_text = " ".join((
                entry.display, entry.tag_id, entry.family_label, entry.family_id,
                category_label, source_label, entry.source_label,
                entry.provenance_source, entry.reviewed_at,
                entry.authority, entry.layer,
                entry.blocked_reason, *entry.relationship_ids,
            )).casefold()
            if query and query not in search_text:
                continue
            tooltip = (
                f"Base Tag {entry.display} ({entry.tag_id}); "
                f"{entry.family_label}: {scope_label}; {source_label}."
            )
            rows.append(IncrementalResultRow(
                entry.stable_id,
                (
                    entry.display,
                    entry.family_label,
                    scope_label,
                    source_label,
                    category_label,
                ),
                tooltip=tooltip,
                accessible_text=tooltip,
            ))
        self.model.set_rows(
            rows,
            preserve_count=self.model.loaded_count,
            ensure_ids=(selected_id,),
        )
        restored = self.model.index_for_id(selected_id)
        if restored.isValid():
            self.table.setCurrentIndex(restored)
        elif self.model.rowCount():
            self.table.setCurrentIndex(self.model.index(0, 0))
        self._selection_changed()

    def _selected_entry(self):
        stable_id = self.model.stable_id(self.table.currentIndex())
        return self._entries_by_id.get(stable_id)

    def _selection_changed(self) -> None:
        entry = self._selected_entry()
        self.review_button.setEnabled(entry is not None)
        if entry is None:
            self.policy_details.setText(
                "Select one policy to inspect its exact authority."
            )
            return
        scope_label = self._scope_label(entry)
        if entry.blocked_reason:
            consequence = entry.blocked_reason
        elif entry.scope_mode == VALUE_SCOPE_UNREVIEWED:
            consequence = (
                "The family is present, but the record does not say All, Only, "
                "or All except. Review the value scope before using it for new compositions."
            )
        elif entry.scope_mode == VALUE_SCOPE_ALL:
            consequence = (
                "All current and future reviewed values in this collection are allowed."
            )
        elif entry.scope_mode in {VALUE_SCOPE_ONLY, VALUE_SCOPE_ALL_EXCEPT}:
            consequence = (
                f"This policy stores {entry.selected_count:,} main branch/Tag decision"
                f"{'s' if entry.selected_count != 1 else ''} and "
                f"{entry.exception_count:,} nested or exact exception"
                f"{'s' if entry.exception_count != 1 else ''}."
            )
        else:
            consequence = "This Base Tag deliberately allows no reusable detail families."
        relationship_ids = ", ".join(entry.relationship_ids) or "none"
        provenance = entry.provenance_source or "not recorded"
        reviewed_at = entry.reviewed_at or "not recorded"
        self.policy_details.setText(
            f"Base Tag: {entry.display} ({entry.tag_id})  •  "
            f"Family: {entry.family_label} ({entry.family_id or 'none'})  •  "
            f"Scope: {scope_label}\n"
            f"Authority: {self._source_label(entry)}; layer: {entry.layer or 'none'}; "
            f"provenance: {provenance}; reviewed: {reviewed_at}; "
            f"relationship IDs: {relationship_ids}.\n"
            f"{consequence} Review in Tag Details uses the canonical saved policy writer."
        )

    def _counts_changed(self, shown: int, total: int) -> None:
        all_count = len(self.entries)
        if not total:
            self.result_status.setText(
                "No direct policies match this view. Inherited Tags remain represented "
                "by their editable source owner."
            )
        else:
            self.result_status.setText(
                f"Showing {shown:,} of {total:,} matching direct policy decision"
                f"{'s' if total != 1 else ''}; {all_count:,} total."
            )
        self.load_more.setVisible(shown < total)
        self.load_more.setText(
            f"Load {min(self.PAGE_SIZE, total - shown):,} More…"
            if shown < total else "Load More…"
        )

    def _review_selected(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        self.review_tag_id = entry.tag_id
        self.accept()


class RelationshipWorkspaceDialog(FeedbackEnabledDialog):
    """One concise editor for typed Tag, category, and character relationships."""

    feedback_workflow = "System Knowledge relationship workspace"

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
        self.service = RelationshipWorkspaceService(
            DEFAULTS_DIR / "relationships.json",
            RELATIONSHIPS_PATH,
            tags=knowledge,
        )
        self.current: KnowledgeRelationship | None = None
        self.current_entry = None
        self.review_tag_id = ""
        self.review_character_links = False
        self.review_conflict_rule_id = ""
        self._focus_tag_id = str(focus_tag_id or "")
        self.changed = False
        self._loading = False
        self._choice_values_cache = {}
        self._endpoint_choice_models = {}
        self._legacy_registry = self._relationship_legacy_registry()
        self._refresh_workspace_entries()
        self.setWindowTitle("Manage Relationships")
        self.resize(1120, 720)

        root = QVBoxLayout(self)
        intro = QLabel(
            "Organize names, meaning, compatibility, context, and training output in one place. "
            "Changes save as personal overrides; Image Knowledge, captions, Metadata, and images are unchanged."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Find a relationship or item…")
        self.search.setClearButtonEnabled(True)
        self.type_filter = QComboBox()
        self.type_filter.addItem("All types", "")
        for item in _TYPES:
            self.type_filter.addItem(item.label, item.kind)
        self.migration_filter = QComboBox()
        self.migration_filter.setAccessibleName(
            "Filter relationships by review state"
        )
        self.migration_filter.addItem("All review states", "")
        self.migration_filter.addItem("Needs review", "migration_required")
        self.migration_filter.addItem(
            "Compatibility active", "compatibility_projection"
        )
        self.migration_filter.addItem("Canonical only", "canonical")
        self.lifecycle_filter = QComboBox()
        self.lifecycle_filter.setAccessibleName(
            "Filter active relationships and disabled history"
        )
        self.lifecycle_filter.addItem("Active relationships", "active")
        self.lifecycle_filter.addItem("Disabled history", "disabled")
        self.lifecycle_filter.addItem("Active + history", "all")
        filters.addWidget(self.search, 1)
        filters.addWidget(self.type_filter)
        filters.addWidget(self.migration_filter)
        filters.addWidget(self.lifecycle_filter)
        left_layout.addLayout(filters)
        self.relationships = QListWidget()
        self.relationships.setAccessibleName("System Knowledge relationships")
        self.relationships.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.relationships.setTextElideMode(Qt.ElideRight)
        left_layout.addWidget(self.relationships, 1)
        self.relationship_count = QLabel()
        self.relationship_count.setObjectName("mutedLabel")
        self.relationship_count.setWordWrap(True)
        self.relationship_count.setAccessibleName("Relationship result count")
        left_layout.addWidget(self.relationship_count)
        self.policy_audit_button = QPushButton("Review Allowed Details Policies…")
        self.policy_audit_button.setToolTip(
            "Open the bounded table of direct Base Tag policies by family, value scope, source, Category, and stable identity."
        )
        self.policy_audit_button.setAccessibleName(
            "Review Allowed Details policies"
        )
        left_layout.addWidget(self.policy_audit_button)
        left_actions = QHBoxLayout()
        self.new_button = QPushButton("New Relationship…")
        self.migration_button = QPushButton("Review Migration…")
        left_actions.addWidget(self.new_button)
        left_actions.addWidget(self.migration_button)
        left_layout.addLayout(left_actions)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        form = QFormLayout()
        self.editor_form = form
        self.type_choice = QComboBox()
        for item in _TYPES:
            self.type_choice.addItem(item.label, item.kind)
            self.type_choice.setItemData(
                self.type_choice.count() - 1, item.tooltip, Qt.ToolTipRole
            )
        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText("Short optional label")
        self.scope_choice = QComboBox()
        for label, scope in _SCOPES:
            self.scope_choice.addItem(label, scope)
        self.scope_choice.setToolTip(
            "Whether this relationship applies to the image, character, object, or scene context."
        )
        self.pattern_edit = QLineEdit()
        self.pattern_edit.setPlaceholderText("Example: {color} hair")
        self.pattern_edit.setToolTip(
            "How a reviewed detail combines with the base Tag; braces name the selected detail dimension."
        )
        self.pattern_label = QLabel("Tag Pattern")
        self.pattern_label.setToolTip(self.pattern_edit.toolTip())
        self.enabled_check = QCheckBox("Enabled")
        self.enabled_check.setChecked(True)
        form.addRow("Relationship", self.type_choice)
        form.addRow("Label", self.label_edit)
        form.addRow("Applies To", self.scope_choice)
        form.addRow(self.pattern_label, self.pattern_edit)
        form.addRow("Status", self.enabled_check)
        self.endpoint_rows = [_EndpointRow() for _ in range(4)]
        self.endpoint_labels = []
        for row in self.endpoint_rows:
            label = QLabel()
            self.endpoint_labels.append(label)
            form.addRow(label, row)
        right_layout.addLayout(form)
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setObjectName("mutedLabel")
        self.preview.setAccessibleName("Relationship effect preview")
        right_layout.addWidget(self.preview)
        right_layout.addStretch(1)
        self.save_status = QLabel()
        self.save_status.setObjectName("mutedLabel")
        self.save_status.setWordWrap(True)
        self.save_status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        actions = QHBoxLayout()
        self.reset_button = QPushButton("Reset Personal Override…")
        self.review_tag_button = QPushButton("Review in Tag Details…")
        self.review_tag_button.hide()
        self.review_character_links_button = QPushButton("Review Character Tag Links…")
        self.review_character_links_button.hide()
        self.review_conflict_button = QPushButton("Review in Rules & Automation…")
        self.review_conflict_button.hide()
        self.save_button = QPushButton("Save Relationship")
        self.save_button.setProperty("primary", True)
        actions.addWidget(self.reset_button)
        actions.addWidget(self.save_status, 1)
        actions.addWidget(self.review_tag_button)
        actions.addWidget(self.review_character_links_button)
        actions.addWidget(self.review_conflict_button)
        actions.addWidget(self.save_button)
        right_layout.addLayout(actions)
        splitter.addWidget(right)
        splitter.setSizes((430, 690))

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._search_timer=QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self._reload_list)
        self.search.textChanged.connect(self._schedule_reload)
        self.search.textEdited.connect(self._clear_focus_tag_filter)
        self.type_filter.currentIndexChanged.connect(self._reload_list)
        self.migration_filter.currentIndexChanged.connect(self._reload_list)
        self.lifecycle_filter.currentIndexChanged.connect(self._reload_list)
        self.relationships.currentItemChanged.connect(self._selection_changed)
        self.type_choice.currentIndexChanged.connect(self._type_changed)
        self.label_edit.textChanged.connect(self._update_preview)
        self.scope_choice.currentIndexChanged.connect(self._update_preview)
        self.pattern_edit.textChanged.connect(self._update_preview)
        self.enabled_check.stateChanged.connect(self._update_preview)
        for row in self.endpoint_rows:
            row.choice.currentIndexChanged.connect(self._update_preview)
            row.text.textChanged.connect(self._update_preview)
        self.new_button.clicked.connect(self._new_relationship)
        self.save_button.clicked.connect(self._save)
        self.reset_button.clicked.connect(self._reset_override)
        self.review_tag_button.clicked.connect(self._review_tag_details)
        self.review_character_links_button.clicked.connect(
            self._review_character_tag_links
        )
        self.review_conflict_button.clicked.connect(self._review_conflict_rule)
        self.policy_audit_button.clicked.connect(
            self._audit_allowed_details_policies
        )
        self.migration_button.clicked.connect(self._review_migration)
        if initial_kind:
            self.type_filter.blockSignals(True)
            self.type_filter.setCurrentIndex(
                max(0, self.type_filter.findData(str(initial_kind)))
            )
            self.type_filter.blockSignals(False)
        if self._focus_tag_id:
            record = self.knowledge.tags.get(self._focus_tag_id)
            self.search.setPlaceholderText(
                "Focused on " + (
                    record.display if record is not None else self._focus_tag_id
                )
            )
        self._reload_list()

    def _schedule_reload(self,*_args):
        self._search_timer.start()

    def _clear_focus_tag_filter(self, *_args):
        if not self._focus_tag_id:
            return
        self._focus_tag_id = ""
        self.search.setPlaceholderText("Find a relationship or item…")
        self._schedule_reload()

    def _refresh_workspace_entries(self):
        self._workspace_entries=self.service.workspace_entries(
            self._legacy_registry,
        )
        self._workspace_entries_by_id={
            entry.relationship.id:entry for entry in self._workspace_entries
        }
        self.current_entry=None

    def _set_standard_editor_visible(self, visible):
        for field in (
            self.type_choice,self.label_edit,self.scope_choice,self.enabled_check,
        ):
            field.setVisible(visible)
            label=self.editor_form.labelForField(field)
            if label is not None:label.setVisible(visible)

    def _set_standard_editor_enabled(self, enabled):
        for field in (
            self.type_choice,self.label_edit,self.scope_choice,self.pattern_edit,
            self.enabled_check,*self.endpoint_rows,
        ):
            field.setEnabled(bool(enabled))

    def _relationship_legacy_registry(self):
        return KnowledgeRelationshipRegistry.from_legacy(
            self.knowledge,
            conflicts=ConflictRuleEngine.load(DEFAULTS_DIR / "conflicts.json"),
            characters=self.characters,
            kinds=(
                IDENTITY_ALIAS,
                SPECIFICITY,
                ENTAILMENT,
                SCOPED_CONSTRAINT,
                MODIFIER_VALUE,
                ALLOWED_DIMENSION,
                ENTITY_TAG,
                CHARACTER_SOURCE,
                ASSOCIATED_CHARACTER,
                VERSION_OF,
                SPECIES_TRAIT,
            ),
        )

    @staticmethod
    def _tag_details_only(relationship):
        return bool(
            relationship is not None
            and relationship.kind in {
                IDENTITY_ALIAS,
                SPECIFICITY,
                ENTAILMENT,
                MODIFIER_VALUE,
                ALLOWED_DIMENSION,
                ENTITY_TAG,
                CHARACTER_SOURCE,
                ASSOCIATED_CHARACTER,
                VERSION_OF,
            }
        )

    def _tag_details_review_id(self):
        relationship=self.current
        if self._tag_details_only(relationship):
            if relationship.kind==IDENTITY_ALIAS:
                targets=[
                    endpoint.id for endpoint in relationship.target_endpoints
                    if endpoint.kind=="tag" and endpoint.id in self.knowledge.tags
                ]
                return targets[0] if len(targets)==1 else ""
            if relationship.kind==ENTITY_TAG:
                targets=[
                    endpoint.id for endpoint in relationship.target_endpoints
                    if endpoint.kind=="tag" and endpoint.id in self.knowledge.tags
                ]
                return targets[0] if len(targets)==1 else ""
            if relationship.kind in {
                CHARACTER_SOURCE,ASSOCIATED_CHARACTER,VERSION_OF,
            }:
                source=relationship.source_endpoint
                if source is not None and source.kind in {"character","outfit"}:
                    record_kind="character" if source.kind=="character" else "outfit"
                    return self.characters.entity_tag_id(record_kind,source.id)
            source=relationship.source_endpoint
            if source is not None and source.kind=="tag":
                return source.id if source.id in self.knowledge.tags else ""
        kind=str(self.type_choice.currentData() or "")
        if relationship is None and kind in {IDENTITY_ALIAS,SPECIFICITY,ENTAILMENT}:
            data=self.endpoint_rows[1 if kind==IDENTITY_ALIAS else 0].choice.currentData()
            if data is not None and str(data[0])=="tag":
                tag_id=str(data[1])
                return tag_id if tag_id in self.knowledge.tags else ""
        return ""

    def _update_tag_details_handoff(self):
        kind=str(self.type_choice.currentData() or "")
        self.review_tag_button.setText(
            "Review Allowed Details…"
            if kind==ALLOWED_DIMENSION else
            "Review Other Names…"
            if kind==IDENTITY_ALIAS else
            "Review in Tag Details…"
        )
        self.review_tag_button.setVisible(
            kind in {
                IDENTITY_ALIAS,SPECIFICITY,ENTAILMENT,MODIFIER_VALUE,
                ALLOWED_DIMENSION,ENTITY_TAG,CHARACTER_SOURCE,
                ASSOCIATED_CHARACTER,VERSION_OF,
            }
            and bool(self._tag_details_review_id())
        )
        self.review_character_links_button.setVisible(
            kind in {ENTITY_TAG,CHARACTER_SOURCE,ASSOCIATED_CHARACTER,VERSION_OF}
            and not bool(self._tag_details_review_id())
        )
        self.review_conflict_button.setVisible(kind == SCOPED_CONSTRAINT)

    def _review_tag_details(self):
        tag_id=self._tag_details_review_id()
        if not tag_id:
            return
        self.review_tag_id=tag_id
        self.accept()

    def _review_character_tag_links(self):
        self.review_character_links=True
        self.accept()

    def _review_conflict_rule(self):
        self.review_conflict_rule_id = self.current.id if self.current is not None else ""
        self.accept()

    def _choice_values(self, kind: str):
        cached=self._choice_values_cache.get(kind)
        if cached is not None:
            return cached
        if kind == "tag":
            values=tuple(
                ("tag", item.id, item.display)
                for item in sorted(
                    self.knowledge.tags.values(), key=lambda value: value.display.casefold()
                )
            )
        elif kind == "category":
            values=tuple(
                ("category", item.id, self.categories.path_label(item.id))
                for item in sorted(
                    self.categories.all_categories(),
                    key=lambda value: self.categories.path_label(value.id).casefold(),
                )
            )
        elif kind == "modifier_dimension":
            values=(
                ("modifier_dimension", "color", "Color"),
                ("modifier_dimension", "hair_length", "Hair Length"),
                ("modifier_dimension", "species", "Species"),
            )
        elif kind == "modifier_value":
            values=tuple(
                ("modifier_value", item.id, item.display)
                for item in sorted(
                    self.knowledge.tags.values(), key=lambda value: value.display.casefold()
                )
            )
        elif kind in {
            "character_source","character","outfit",
        }:
            entity_groups = {
                "character_source": self.characters.series,
                "character": self.characters.characters,
                "outfit": self.characters.outfits,
            }
            values=tuple(
                (kind, item.id, item.name)
                for item in sorted(
                    entity_groups[kind].values(), key=lambda value: value.name.casefold()
                )
            )
        elif kind in {
            "character_entity",
            "character_or_outfit",
            "character_or_tag",
            "any_entity",
        }:
            values = []
            if kind in {"character_or_tag", "any_entity"}:
                values.extend(self._choice_values("tag"))
            if kind == "any_entity":
                values.extend(self._choice_values("category"))
            entity_kinds = (
                ("character", "outfit")
                if kind == "character_or_outfit"
                else ("character",)
                if kind == "character_or_tag"
                else ("character_source", "character", "outfit")
            )
            for entity_kind in entity_kinds:
                values.extend(self._choice_values(entity_kind))
            values=tuple(values)
        else:
            values=()
        self._choice_values_cache[kind]=values
        return values

    def _endpoint_choice_model(self,kind,optional):
        key=(str(kind),bool(optional))
        model=self._endpoint_choice_models.get(key)
        if model is None:
            model=_EndpointChoiceModel(
                kind,self._choice_values(kind),optional=optional,parent=self,
            )
            self._endpoint_choice_models[key]=model
        return model

    def _configure_endpoint(self, index, role, kind, optional, endpoint=None):
        label = self.endpoint_labels[index]
        row = self.endpoint_rows[index]
        label.setText(role)
        label.setVisible(True)
        row.setVisible(True)
        row.expected_kind = kind
        row.set_text_mode(kind == "alias_text")
        row.text.clear()
        if kind == "alias_text":
            row.text.setText(endpoint.label if endpoint is not None else "")
            return
        model=self._endpoint_choice_model(kind,optional)
        if row.choice.model() is not model:
            row.choice.setModel(model)
        if endpoint is not None:
            row.choice.setCurrentIndex(model.ensure_endpoint(endpoint))
        elif row.choice.count():
            row.choice.setCurrentIndex(0)
        else:
            row.choice.setCurrentIndex(-1)

    def _type_changed(self, *_args, endpoints=()):
        if self._loading:
            return
        choice = _TYPE_BY_KIND.get(str(self.type_choice.currentData()))
        if choice is None:
            return
        self._loading=True
        try:
            self.type_choice.setToolTip(choice.tooltip)
            pattern_visible = choice.kind == ALLOWED_DIMENSION
            self.pattern_label.setVisible(pattern_visible)
            self.pattern_edit.setVisible(pattern_visible)
            if not pattern_visible:
                self.pattern_edit.clear()
            self.scope_choice.setCurrentIndex(
                max(0, self.scope_choice.findData(choice.scope))
            )
            for index, row in enumerate(self.endpoint_rows):
                if index >= len(choice.endpoints):
                    self.endpoint_labels[index].hide()
                    row.hide()
                    continue
                role, kind, optional = choice.endpoints[index]
                endpoint = endpoints[index] if index < len(endpoints) else None
                self._configure_endpoint(index, role, kind, optional, endpoint)
        finally:
            self._loading=False
        if choice.kind in {
            SPECIFICITY,ENTAILMENT,MODIFIER_VALUE,ALLOWED_DIMENSION,
            ENTITY_TAG,CHARACTER_SOURCE,ASSOCIATED_CHARACTER,VERSION_OF,
        }:
            self.save_button.setEnabled(False)
            if choice.kind in {SPECIFICITY,ENTAILMENT}:
                self.save_status.setText(
                    "Broader Tag and Also Means are reviewed in Tag Details so "
                    "canonical authority and old-reader projections save or "
                    "recover together."
                )
            elif choice.kind in {MODIFIER_VALUE,ALLOWED_DIMENSION}:
                self.save_status.setText(
                    "Modifier composition and Allowed Details are reviewed in Tag "
                    "Details so canonical authority and old-reader projections save "
                    "or recover together."
                )
            else:
                self.save_status.setText(
                    "Character Knowledge relationships are reviewed through the linked "
                    "typed record in Tag Details so canonical authority and old-reader "
                    "projections save or recover together."
                )
        elif choice.kind == SCOPED_CONSTRAINT:
            self.save_button.setEnabled(False)
            self.save_status.setText(
                "Diagnostic conflict authoring is owned by Rules & Automation. "
                "This relationship remains visible here as read-only System Knowledge."
            )
        elif self.current is None:
            self.save_button.setEnabled(True)
            self.save_status.setText("Choose the relationship and items, then save.")
        self._update_tag_details_handoff()
        self._update_preview()

    def _reload_list(self, *_args, selected_id=None):
        self._search_timer.stop()
        selected_id = selected_id or (self.current.id if self.current else "")
        query = (
            "" if self._focus_tag_id
            else self.search.text().strip().casefold()
        )
        kind = str(self.type_filter.currentData() or "")
        migration_state = str(self.migration_filter.currentData() or "")
        lifecycle_state = str(self.lifecycle_filter.currentData() or "active")
        self.relationships.blockSignals(True)
        self.relationships.clear()
        matching_rows=[]
        for entry in self._workspace_entries:
            relationship = entry.relationship
            if kind and relationship.kind != kind:
                continue
            if migration_state and entry.migration_state != migration_state:
                continue
            is_disabled=bool(relationship.disabled)
            if self._focus_tag_id and not any(
                endpoint.kind == "tag" and endpoint.id == self._focus_tag_id
                for endpoint in relationship.endpoints
            ):
                continue
            choice = _TYPE_BY_KIND.get(relationship.kind)
            type_label = choice.label if choice else relationship.kind.replace("_", " ").title()
            display_type_label=(
                type_label
                if not relationship.label
                or relationship.label.casefold()==type_label.casefold()
                else f"{type_label}: {relationship.label}"
            )
            endpoint_text = (" → " if relationship.directed else " ↔ ").join(
                item.label or item.id for item in relationship.endpoints
            )
            authority_label=(
                "Personal"
                if entry.authority=="personal" else
                "Shipped Default"
                if entry.authority=="default" else
                "Legacy Compatibility"
            )
            review_label=(
                "Needs review"
                if entry.migration_state=="migration_required" else
                "Compatibility projection active"
                if entry.migration_state=="compatibility_projection" else
                "Canonical"
            )
            lifecycle_label="Disabled history" if is_disabled else "Active"
            searchable = " ".join(
                (
                    relationship.label,type_label,endpoint_text,relationship.id,
                    authority_label,review_label,lifecycle_label,
                )
            ).casefold()
            if query and query not in searchable:
                continue
            matching_rows.append((
                _TYPES.index(choice) if choice in _TYPES else len(_TYPES),
                endpoint_text.casefold(),relationship.id,entry,type_label,
                display_type_label,endpoint_text,authority_label,review_label,
                lifecycle_label,
            ))
        rows=[
            row for row in matching_rows
            if (
                lifecycle_state=="all"
                or (lifecycle_state=="disabled") == row[3].relationship.disabled
            )
        ]
        selected_row=0
        for (
            _type_order,_endpoint_order,_relationship_order,entry,type_label,
            display_type_label,endpoint_text,authority_label,review_label,
            lifecycle_label,
        ) in sorted(rows,key=lambda value:value[:3]):
            relationship=entry.relationship
            item = QListWidgetItem(
                f"{endpoint_text}\n{display_type_label}  ·  {authority_label}  ·  "
                f"{review_label}  ·  {lifecycle_label}"
            )
            item.setData(Qt.UserRole, relationship.id)
            migration_detail = (
                "\nCompatibility source: " + ", ".join(entry.compatibility_sources)
                if entry.compatibility_sources else ""
            )
            item.setToolTip(
                (choice.tooltip if choice else relationship.id) + migration_detail
            )
            self.relationships.addItem(item)
            if relationship.id == selected_id:
                selected_row = self.relationships.count() - 1
        total=len(matching_rows)
        active=sum(not row[3].relationship.disabled for row in matching_rows)
        disabled=total-active
        self.relationship_count.setText(
            f"Showing {len(rows):,} of {total:,} matching relationships. "
            f"{active:,} active; {disabled:,} disabled history."
        )
        if self.relationships.count():
            self.relationships.setCurrentRow(selected_row)
        self.relationships.blockSignals(False)
        if self.relationships.count():
            item=self.relationships.currentItem()
            relationship_id=str(item.data(Qt.UserRole))
            if (
                self.current is None
                or self.current_entry is None
                or self.current.id!=relationship_id
            ):
                self._selection_changed(item,None)
        else:
            self.current = None
            self.current_entry = None
            self._set_standard_editor_visible(False)
            self.pattern_label.hide()
            self.pattern_edit.hide()
            for label, row in zip(self.endpoint_labels, self.endpoint_rows):
                label.hide()
                row.hide()
            self.reset_button.hide()
            self.review_tag_button.hide()
            self.review_character_links_button.hide()
            self.review_conflict_button.hide()
            self.save_button.hide()
            self.preview.setText(
                "No relationships need migration."
                if migration_state == "migration_required"
                else "No disabled relationship history matches the current filters."
                if lifecycle_state == "disabled"
                else "No relationships match the current filters."
            )
            self.save_status.clear()

    def _selection_changed(self, current, _previous):
        if current is None:
            return
        relationship_id = str(current.data(Qt.UserRole))
        entry = self._workspace_entries_by_id.get(relationship_id)
        if entry is None:
            return
        relationship = entry.relationship
        self.current_entry = entry
        choice = _TYPE_BY_KIND.get(relationship.kind)
        if choice is None:
            self.current=relationship
            self._set_standard_editor_visible(False)
            self.label_edit.setText(relationship.label)
            self.pattern_edit.clear()
            self.pattern_label.hide(); self.pattern_edit.hide()
            for label,row in zip(self.endpoint_labels,self.endpoint_rows):
                label.hide(); row.hide()
            self.reset_button.hide()
            self.review_tag_button.hide()
            self.review_character_links_button.hide()
            connector=" → " if relationship.directed else " ↔ "
            endpoints=connector.join(
                item.label or item.id for item in relationship.endpoints
            )
            self.preview.setText(
                f"{relationship.kind.replace('_',' ').title()}: {endpoints}. "
                f"Stable relationship ID: {relationship.id}."
            )
            self.save_status.setText(
                "Review this Modifier Role through Review Migration."
                if relationship.kind==MODIFIER_ROLE else
                "This future relationship type is preserved but not editable here yet."
            )
            self.save_button.setEnabled(False)
            self.save_button.hide()
            return
        self._set_standard_editor_visible(True)
        self._set_standard_editor_enabled(True)
        self.review_tag_button.hide()
        self.review_character_links_button.hide()
        self.review_conflict_button.hide()
        self.save_button.show()
        self.current = relationship
        self._loading = True
        self.type_choice.setCurrentIndex(self.type_choice.findData(relationship.kind))
        self.label_edit.setText(relationship.label)
        self.pattern_edit.setText(dict(relationship.details).get("pattern", ""))
        scope_index = self.scope_choice.findData(relationship.scope)
        self.scope_choice.setCurrentIndex(scope_index)
        self.enabled_check.setChecked(not relationship.disabled)
        self._loading = False
        self._type_changed(endpoints=relationship.endpoints)
        self.scope_choice.setCurrentIndex(scope_index)
        self.reset_button.setVisible(
            relationship.id in self.service.user_store.relationships
        )
        tag_details_only=self._tag_details_only(relationship)
        editable = relationship.kind != SCOPED_CONSTRAINT and not tag_details_only and not (
            entry.authority == "compatibility"
            and relationship.source != "tag.relations.conflicts"
        )
        self.save_button.setEnabled(editable)
        if tag_details_only:
            self._update_tag_details_handoff()
            self._set_standard_editor_enabled(False)
            self.reset_button.hide()
            self.save_button.hide()
            if relationship.kind==IDENTITY_ALIAS:
                self.save_status.setText(
                    "This canonical or compatibility Other Name is visible here. "
                    "Review its canonical Tag in Tag Details so identity authority "
                    "and the old-reader projection stay atomic."
                )
            elif relationship.kind==SPECIFICITY:
                self.save_status.setText(
                    "This canonical or compatibility Broader Tag is visible here. "
                    "Review the detailed Tag in Tag Details so relationship "
                    "authority and the old-reader projection stay atomic."
                )
            elif relationship.kind==ENTAILMENT:
                self.save_status.setText(
                    "This canonical or compatibility Also Means relationship is "
                    "visible here. Review its source Tag in Tag Details so "
                    "relationship authority and the old-reader projection stay atomic."
                )
            elif relationship.kind==MODIFIER_VALUE:
                self.save_status.setText(
                    "This canonical or compatibility composition is visible here. "
                    "Review its named family slot in Tag Details so relationship "
                    "authority and the old-reader projection stay atomic."
                )
            elif relationship.kind in {
                ENTITY_TAG,CHARACTER_SOURCE,ASSOCIATED_CHARACTER,VERSION_OF,
            }:
                self.save_status.setText(
                    "This canonical or compatibility Character Knowledge relationship "
                    "is visible here. Review the linked typed record in Tag Details so "
                    "canonical authority and its old-reader projection save atomically."
                    if self._tag_details_review_id() else
                    "This Character Knowledge relationship is read-only here. Use Review "
                    "Character Tag Links first so the typed record has one reviewed Tag entry point."
                )
            else:
                self.save_status.setText(
                    "This canonical or compatibility Allowed Details policy is "
                    "visible here. Review the Base Tag in Tag Details so the "
                    "relationship policy and old-reader projection stay atomic."
                )
        elif relationship.kind == SCOPED_CONSTRAINT:
            self._set_standard_editor_enabled(False)
            self.reset_button.hide()
            self.save_button.hide()
            self.review_conflict_button.show()
            self.save_status.setText(
                "This canonical conflict family is read-only in System Knowledge. "
                "Review, revise, disable, or replace it in Rules & Automation."
            )
        elif not editable:
            self.save_status.setText(
                "This compatibility source is visible here but remains read-only until its source migration is implemented."
            )
        elif entry.authority == "compatibility":
            self.save_status.setText(
                "Choose Applies To, then save to migrate this inline conflict into canonical authority."
            )
        else:
            self.save_status.clear()
        self._update_preview()

    def _new_relationship(self):
        self.current = None
        self.current_entry = None
        self._set_standard_editor_visible(True)
        self._set_standard_editor_enabled(True)
        self.save_button.show()
        self._loading = True
        self.type_choice.setCurrentIndex(0)
        self.label_edit.clear()
        self.pattern_edit.clear()
        self.enabled_check.setChecked(True)
        self._loading = False
        self._type_changed()
        self.reset_button.hide()
        if self.type_choice.currentData() not in {
            SPECIFICITY,ENTAILMENT,MODIFIER_VALUE,ALLOWED_DIMENSION,
            ENTITY_TAG,CHARACTER_SOURCE,ASSOCIATED_CHARACTER,VERSION_OF,
            SCOPED_CONSTRAINT,
        }:
            self.save_button.setEnabled(True)
            self.save_status.setText("Choose the relationship and items, then save.")

    def _endpoints(self):
        choice = _TYPE_BY_KIND[str(self.type_choice.currentData())]
        result = []
        for index, (_role, kind, optional) in enumerate(choice.endpoints):
            row = self.endpoint_rows[index]
            if kind == "alias_text":
                value = row.text.text().strip()
                if not value:
                    if optional:
                        continue
                    raise ValueError("Enter the other name.")
                result.append(RelationshipEndpoint("alias_text", normalize_tag(value), value))
                continue
            data = row.choice.currentData()
            if data is None:
                if optional:
                    continue
                raise ValueError(f"Choose {self.endpoint_labels[index].text().casefold()}.")
            result.append(RelationshipEndpoint(str(data[0]), str(data[1]), str(data[2])))
        return tuple(result)

    def _candidate(self):
        choice = _TYPE_BY_KIND[str(self.type_choice.currentData())]
        endpoints = self._endpoints()
        current = self.current
        details = dict(current.details) if current is not None else {}
        if choice.kind == ALLOWED_DIMENSION:
            pattern = self.pattern_edit.text().strip()
            if not pattern:
                raise ValueError("Enter the Tag Pattern for this allowed detail.")
            details["pattern"] = pattern
        scope = str(self.scope_choice.currentData() or "")
        if choice.kind == SCOPED_CONSTRAINT and not scope:
            raise ValueError(
                "Choose whether these Contrary Values apply to the image, character, object, or context."
            )
        return KnowledgeRelationship(
            id=current.id if current is not None else f"user.{choice.kind}.{uuid4().hex}",
            kind=choice.kind,
            endpoints=endpoints,
            directed=choice.directed,
            scope=scope or choice.scope,
            label=self.label_edit.text().strip(),
            reviewed=True,
            source="user_review",
            details=tuple((str(key), str(value)) for key, value in details.items()),
            version=(current.version + 1) if current is not None else 1,
            layer="user",
            disabled=not self.enabled_check.isChecked(),
            extras=dict(current.extras) if current is not None else {},
        )

    def _update_preview(self, *_args):
        if self._loading:
            return
        choice = _TYPE_BY_KIND.get(str(self.type_choice.currentData()))
        if choice is None:
            return
        try:
            endpoints = self._endpoints()
            names = " and ".join(item.label or item.id for item in endpoints)
        except Exception:
            names = "the selected items"
        if choice.kind == SCOPED_CONSTRAINT:
            effect = "Possible Conflict: these values require review only when they describe the same selected scope."
        elif choice.kind == SPECIFICITY:
            effect = "Allowed Together: yes. Training Output can keep the detailed Tag and omit the broader Tag."
        elif choice.kind == ADVISORY_ASSOCIATION:
            effect = "Allowed Together: yes. This relationship suggests review and never confirms a fact automatically."
        else:
            effect = "Training Output: this reviewed relationship is available to compatible profiles and tools."
        pattern = (
            f" Pattern: {self.pattern_edit.text().strip()}."
            if choice.kind == ALLOWED_DIMENSION and self.pattern_edit.text().strip()
            else ""
        )
        self.preview.setText(f"{choice.label}: {names}.{pattern} {effect}")
        self._update_tag_details_handoff()

    def _save(self):
        try:
            candidate = self._candidate()
            if candidate.kind in {
                IDENTITY_ALIAS,SPECIFICITY,ENTAILMENT,MODIFIER_VALUE,ALLOWED_DIMENSION,
            }:
                raise ValueError(
                    "Review this relationship in Tag Details "
                    "so canonical authority and old-reader projections remain one "
                    "transaction."
                )
            if candidate.kind == SCOPED_CONSTRAINT:
                raise ValueError(
                    "Review diagnostic conflicts in Rules & Automation so one "
                    "canonical authoring surface owns scope and Allow when policy."
                )
            if candidate.kind in {
                ENTITY_TAG,CHARACTER_SOURCE,ASSOCIATED_CHARACTER,VERSION_OF,
            }:
                raise ValueError(
                    "Review this relationship through the typed record in Tag Details "
                    "so canonical authority and its old-reader projection remain one transaction."
                )
            expected = (
                relationship_fingerprint(self.current) if self.current is not None else None
            )
            self.service.save_override(
                candidate,
                expected_fingerprint=expected,
                compatibility_tags=self.knowledge,
                legacy_relationship=(
                    self.current
                    if self.current_entry is not None
                    and self.current_entry.authority == "compatibility"
                    else None
                ),
            )
        except Exception as exc:
            QMessageBox.warning(self, "Relationship Not Saved", str(exc))
            return
        self.changed = True
        self._legacy_registry = self._relationship_legacy_registry()
        self._refresh_workspace_entries()
        self.current = candidate
        self._reload_list(selected_id=candidate.id)
        self.save_status.setText(
            "Saved canonical authority and its compatibility projection with one recovery copy."
            if candidate.kind == SCOPED_CONSTRAINT
            else "Saved automatically with a recovery copy."
        )

    def _reset_override(self):
        if self.current is None or self.current.id not in self.service.user_store.relationships:
            return
        if self.current.kind in {
            SPECIFICITY,ENTAILMENT,MODIFIER_VALUE,ALLOWED_DIMENSION,
        }:
            QMessageBox.information(
                self,"Review in Tag Details",
                "Review this relationship in Tag Details so canonical "
                "authority and the old-reader projection stay synchronized.",
            )
            return
        if QMessageBox.question(
            self,
            "Reset personal override?",
            "Reset this personal override and reveal the reviewed default again? A recovery copy will be retained.",
        ) != QMessageBox.Yes:
            return
        relationship_id = self.current.id
        try:
            self.service.reset_override(
                relationship_id,
                expected_fingerprint=relationship_fingerprint(self.current),
                compatibility_tags=self.knowledge,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Override Not Reset", str(exc))
            return
        self.changed = True
        self._legacy_registry = self._relationship_legacy_registry()
        self._refresh_workspace_entries()
        self.current = None
        self._reload_list(selected_id=relationship_id)

    def _review_migration(self):
        registry = KnowledgeRelationshipRegistry.from_legacy(
            self.knowledge,
            categories=self.categories,
            conflicts=ConflictRuleEngine.load(DEFAULTS_DIR / "conflicts.json"),
            characters=self.characters,
            include_advisory=True,
        )
        assessment = self.service.assess_legacy_migration(registry)
        foundation = FoundationKnowledge(
            DEFAULTS_DIR / "modifiers.json", DEFAULTS_DIR / "concepts.json",
        )
        decision_service = ModifierRoleDecisionService(
            self.knowledge,self.categories,foundation,workspace=self.service,
        )
        relationship_review_count = sum(
            entry.migration_state == "migration_required"
            for entry in self._workspace_entries
        )
        report=RelationshipMigrationReportDialog(
            assessment,decision_service.inventory(),self,
            decision_service=decision_service,
            relationship_review_count=relationship_review_count,
        )
        report.exec()
        if report.changed:
            self.changed=True
            self._legacy_registry = self._relationship_legacy_registry()
            self._refresh_workspace_entries()
            selected_id=(self.current.id if self.current is not None else "")
            self._reload_list(selected_id=selected_id)
        if report.review_relationships_requested:
            self._focus_tag_id = ""
            self.search.clear()
            self.type_filter.setCurrentIndex(0)
            self.migration_filter.setCurrentIndex(
                self.migration_filter.findData("migration_required")
            )
            self._reload_list()

    def _audit_allowed_details_policies(self):
        try:
            foundation = FoundationKnowledge(
                DEFAULTS_DIR / "modifiers.json", DEFAULTS_DIR / "concepts.json",
            )
            service = AllowedDimensionPolicyService(
                self.knowledge,
                self.categories,
                foundation,
                default_path=DEFAULTS_DIR / "relationships.json",
                user_path=RELATIONSHIPS_PATH,
                workspace=self.service,
            )
            dialog = AllowedDetailsPolicyAuditDialog(service, self)
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Allowed Details Audit Unavailable",
                "The policy audit could not be opened and System Knowledge was "
                f"left unchanged.\n\n{exc}",
            )
            return
        dialog.exec()
        if dialog.review_tag_id:
            self.review_tag_id = dialog.review_tag_id
            self.accept()
