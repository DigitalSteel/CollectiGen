from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,QComboBox,QDialog,QDialogButtonBox,QFormLayout,QHeaderView,QLabel,
    QTextEdit,QTreeWidget,QTreeWidgetItem,QVBoxLayout,
)

from .components import counted_noun


class ReusableValueAuthorityDecisionDialog(QDialog):
    """Capture one explicit retain-or-retire decision for a lexical list."""

    def __init__(self,family,parent=None):
        super().__init__(parent)
        self.family=family
        self.setWindowTitle("Review Legacy Value Authority")
        self.resize(980,650); self.setMinimumSize(820,520)
        layout=QVBoxLayout(self)
        intro=QLabel(
            f"Decide the authority of {{{family.name}}} after reviewing every "
            "current string and consumer. Retaining records a distinct lexical "
            "wildcard type. Retirement is available only when no current rule "
            "uses the list; neither choice changes Tags or Image Knowledge."
        )
        intro.setWordWrap(True); layout.addWidget(intro)
        form=QFormLayout()
        self.decision_box=QComboBox()
        self.decision_box.setAccessibleName("Legacy value authority decision")
        self.decision_box.addItem(
            "Retain as a distinct lexical wildcard type","retain_distinct",
        )
        if not family.consumers:
            self.decision_box.addItem(
                "Retire this unused legacy list","retire_unused",
            )
        form.addRow("Decision",self.decision_box)
        self.rationale_edit=QTextEdit()
        self.rationale_edit.setAccessibleName("Legacy authority rationale")
        self.rationale_edit.setPlaceholderText(
            "Record why this list has a distinct lexical role or why it is safe to retire."
        )
        self.rationale_edit.setMaximumHeight(90)
        form.addRow("Rationale",self.rationale_edit)
        layout.addLayout(form)
        self.value_tree=QTreeWidget()
        self.value_tree.setAccessibleName("Legacy authority value review")
        self.value_tree.setHeaderLabels((
            "Reviewed","Legacy string","Current authority outcome",
        ))
        self.value_tree.setRootIsDecorated(False)
        for value in family.values:
            item=QTreeWidgetItem((
                "",value.value,value.outcome.replace("_"," ").title(),
            ))
            item.setData(1,Qt.UserRole,value.value)
            item.setFlags(item.flags()|Qt.ItemIsUserCheckable)
            item.setCheckState(0,Qt.Unchecked)
            self.value_tree.addTopLevelItem(item)
        header=self.value_tree.header()
        header.setSectionResizeMode(0,QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1,QHeaderView.Stretch)
        header.setSectionResizeMode(2,QHeaderView.ResizeToContents)
        layout.addWidget(self.value_tree,1)
        self.consumer_status=QLabel(
            f"Consumers to review: {len(family.consumers):,} current "
            f"Automation rule{'s' if len(family.consumers)!=1 else ''}"
        )
        self.consumer_status.setWordWrap(True)
        self.consumer_status.setObjectName("mutedLabel")
        self.consumer_status.setAccessibleName("Legacy value consumers reviewed")
        layout.addWidget(self.consumer_status)
        self.consumer_tree=QTreeWidget()
        self.consumer_tree.setAccessibleName("Legacy authority consumer review")
        self.consumer_tree.setHeaderLabels(("Current consumer",))
        self.consumer_tree.setRootIsDecorated(False)
        for consumer in family.consumers or ("No current Automation rule consumer",):
            self.consumer_tree.addTopLevelItem(QTreeWidgetItem((consumer,)))
        self.consumer_tree.header().setSectionResizeMode(0,QHeaderView.Stretch)
        self.consumer_tree.setMaximumHeight(150)
        layout.addWidget(self.consumer_tree)
        self.review_assertion=QCheckBox(
            "I reviewed every listed value and consumer and confirm this decision."
        )
        self.review_assertion.setAccessibleName(
            "Confirm complete legacy authority review"
        )
        layout.addWidget(self.review_assertion)
        self.ready_status=QLabel()
        self.ready_status.setWordWrap(True)
        self.ready_status.setObjectName("mutedLabel")
        self.ready_status.setAccessibleName("Legacy authority decision readiness")
        layout.addWidget(self.ready_status)
        buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        self.apply_button=buttons.addButton(
            "Apply Reviewed Authority Decision…",QDialogButtonBox.AcceptRole,
        )
        self.apply_button.setProperty("primary",True)
        self.apply_button.setAccessibleName(
            "Apply reviewed legacy value authority decision"
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.value_tree.itemChanged.connect(self._update_ready)
        self.rationale_edit.textChanged.connect(self._update_ready)
        self.review_assertion.toggled.connect(self._update_ready)
        self.decision_box.currentIndexChanged.connect(self._update_ready)
        self._update_ready()

    def decision(self) -> str:
        return str(self.decision_box.currentData() or "")

    def rationale(self) -> str:
        return " ".join(self.rationale_edit.toPlainText().split())

    def reviewed_values(self) -> tuple[str,...]:
        return tuple(
            str(item.data(1,Qt.UserRole))
            for index in range(self.value_tree.topLevelItemCount())
            if (item:=self.value_tree.topLevelItem(index)).checkState(0)==Qt.Checked
        )

    def _update_ready(self,*_):
        expected=self.value_tree.topLevelItemCount()
        reviewed=len(self.reviewed_values())
        rationale_ready=len(self.rationale())>=20
        ready=bool(
            expected and reviewed==expected and rationale_ready
            and self.review_assertion.isChecked()
        )
        self.apply_button.setEnabled(ready)
        self.ready_status.setText(
            f"{reviewed:,} of {expected:,} value(s) reviewed · "
            f"rationale {'recorded' if rationale_ready else 'needs at least 20 characters'} · "
            f"decision: {self.decision_box.currentText()}"
        )


class ReusableValueSemanticReviewDialog(QDialog):
    """Require one explicit identity decision for every unscoped legacy value."""

    def __init__(self,preview,parent=None):
        super().__init__(parent)
        self.preview=preview
        self.setWindowTitle("Review Stable Tag Meanings")
        self.resize(1060,680); self.setMinimumSize(880,540)
        layout=QVBoxLayout(self)
        intro=QLabel(
            f"Confirm what each {{{preview.family.name}}} string means in every "
            "listed Automation consumer. Check a row only when the legacy text "
            "and proposed stable Tag are the same reusable identity. This review "
            "does not change Tags, rules, Image Knowledge, or files."
        )
        intro.setWordWrap(True); layout.addWidget(intro)
        self.summary=QLabel()
        self.summary.setObjectName("mutedLabel")
        self.summary.setAccessibleName("Stable Tag meaning review summary")
        layout.addWidget(self.summary)
        self.value_tree=QTreeWidget()
        self.value_tree.setAccessibleName("Stable Tag meaning decisions")
        self.value_tree.setHeaderLabels((
            "Reviewed","Legacy string","Proposed stable Tag","Identity basis",
        ))
        self.value_tree.setRootIsDecorated(False)
        for value in preview.family.values:
            stable=(
                f"{value.labels[0]} ({value.stable_ids[0]})"
                if value.outcome=="stable_tag" and len(value.stable_ids)==1
                else "Held — no unique live stable Tag"
            )
            item=QTreeWidgetItem((
                "",value.value,stable,
                value.identity_basis.replace("_"," ").title() or "Unresolved",
            ))
            item.setData(1,Qt.UserRole,value.value)
            if value.outcome=="stable_tag" and len(value.stable_ids)==1:
                item.setFlags(item.flags()|Qt.ItemIsUserCheckable)
                item.setCheckState(0,Qt.Unchecked)
            else:item.setDisabled(True)
            self.value_tree.addTopLevelItem(item)
        header=self.value_tree.header()
        for column in (0,1,3):
            header.setSectionResizeMode(column,QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2,QHeaderView.Stretch)
        layout.addWidget(self.value_tree,1)
        parity_label=QLabel(
            "Every current consumer and value remains lexical. These examples "
            "show the exact projection behavior that the later companion record "
            "must preserve."
        )
        parity_label.setWordWrap(True); layout.addWidget(parity_label)
        self.parity_tree=QTreeWidget()
        self.parity_tree.setAccessibleName("Stable Tag meaning consumer examples")
        self.parity_tree.setHeaderLabels((
            "Consumer","Legacy value","Example input","Current result",
        ))
        self.parity_tree.setRootIsDecorated(False)
        for case in preview.parity_cases:
            self.parity_tree.addTopLevelItem(QTreeWidgetItem((
                case.consumer,case.legacy_value,", ".join(case.input_tags),
                ", ".join(case.before_projection),
            )))
        parity_header=self.parity_tree.header()
        for column in (0,1):
            parity_header.setSectionResizeMode(column,QHeaderView.ResizeToContents)
        for column in (2,3):
            parity_header.setSectionResizeMode(column,QHeaderView.Stretch)
        layout.addWidget(self.parity_tree,1)
        buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        self.confirm_button=buttons.addButton(
            "Build Reviewed Compatibility Preview…",
            QDialogButtonBox.AcceptRole,
        )
        self.confirm_button.setProperty("primary",True)
        self.confirm_button.setAccessibleName(
            "Confirm every stable Tag meaning"
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.value_tree.itemChanged.connect(self._update_ready)
        self._update_ready()

    def reviewed_values(self) -> tuple[str,...]:
        return tuple(
            str(item.data(1,Qt.UserRole))
            for index in range(self.value_tree.topLevelItemCount())
            if (item:=self.value_tree.topLevelItem(index)).checkState(0)==Qt.Checked
        )

    def _update_ready(self,*_):
        eligible=sum(
            bool(self.value_tree.topLevelItem(index).flags()&Qt.ItemIsUserCheckable)
            for index in range(self.value_tree.topLevelItemCount())
        )
        reviewed=len(self.reviewed_values())
        self.summary.setText(
            f"{reviewed:,} of {eligible:,} stable Tag meaning(s) confirmed · "
            f"{len(self.preview.family.consumers):,} active consumer rule(s)"
        )
        self.confirm_button.setEnabled(bool(eligible and reviewed==eligible))


class ReusableValueMigrationPreviewDialog(QDialog):
    """Review proposed companion stable-ID bindings before one guarded save."""

    def __init__(self,preview,parent=None):
        super().__init__(parent)
        self.preview=preview
        self.setWindowTitle("Review Legacy Compatibility")
        self.resize(1100,700); self.setMinimumSize(900,560)
        layout=QVBoxLayout(self)
        intro=QLabel(
            f"Review proposed stable Tag bindings for {{{preview.family.name}}}. "
            "Reviewing changes nothing. If every value is resolved, Apply saves "
            "only companion identity metadata; legacy matching strings remain "
            "exact, and no Tags, templates, or Image Knowledge are changed."
        )
        intro.setWordWrap(True); layout.addWidget(intro)
        self.summary=QLabel(
            f"{counted_noun(len(preview.family.values),'legacy string')} · "
            f"{counted_noun(len(preview.proposed_bindings),'stable-ID candidate')} · "
            f"{counted_noun(len(preview.family.consumers),'active consumer rule')} · "
            f"{counted_noun(len(preview.held_reasons),'hold')}"
        )
        self.summary.setObjectName("mutedLabel")
        self.summary.setAccessibleName("Legacy compatibility preview summary")
        layout.addWidget(self.summary)
        self.value_tree=QTreeWidget()
        self.value_tree.setAccessibleName("Legacy wildcard compatibility review")
        self.value_tree.setHeaderLabels((
            "Legacy string","Stable Tag candidate","Identity basis","Status",
        ))
        self.value_tree.setRootIsDecorated(False)
        binding_by_value={item[0]:item for item in preview.proposed_bindings}
        for value in preview.family.values:
            binding=binding_by_value.get(value.value)
            if binding:
                stable=f"{binding[2]} ({binding[1]})"
                basis=binding[3].replace("_"," ").title()
                status="Candidate — review required"
            else:
                stable="Held"
                basis=value.identity_basis.replace("_"," ").title() or "Unresolved"
                status=value.outcome.replace("_"," ").title()
            item=QTreeWidgetItem((value.value,stable,basis,status))
            item.setToolTip(
                0,
                f"{preview.family.origin}; {preview.family.classification_label}; "
                f"{status}",
            )
            self.value_tree.addTopLevelItem(item)
        header=self.value_tree.header()
        header.setSectionResizeMode(0,QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1,QHeaderView.Stretch)
        header.setSectionResizeMode(2,QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3,QHeaderView.ResizeToContents)
        layout.addWidget(self.value_tree,1)
        parity_label=QLabel(
            "Representative rule parity — proposed stable IDs remain companion "
            "metadata, so lexical matching and projection must stay exact."
        )
        parity_label.setWordWrap(True); layout.addWidget(parity_label)
        self.parity_tree=QTreeWidget()
        self.parity_tree.setAccessibleName("Legacy rule before and after parity")
        self.parity_tree.setHeaderLabels((
            "Consumer rule","Example input","Legacy result",
            "Stable-ID companion result","Parity",
        ))
        self.parity_tree.setRootIsDecorated(False)
        for case in preview.parity_cases:
            self.parity_tree.addTopLevelItem(QTreeWidgetItem((
                case.consumer,", ".join(case.input_tags),
                ", ".join(case.before_projection),
                ", ".join(case.after_projection),
                "Unchanged" if case.unchanged else "Held — changed",
            )))
        parity_header=self.parity_tree.header()
        for column in (0,4):
            parity_header.setSectionResizeMode(column,QHeaderView.ResizeToContents)
        for column in (1,2,3):
            parity_header.setSectionResizeMode(column,QHeaderView.Stretch)
        layout.addWidget(self.parity_tree,1)
        held_text=(
            "Held: "+"  •  ".join(preview.held_reasons[:3])
            +(
                f"  •  +{len(preview.held_reasons)-3:,} more; inspect the held rows above."
                if len(preview.held_reasons)>3 else ""
            )
            if preview.held_reasons else
            "All previewed mappings preserve current behavior. Apply will "
            "revalidate current rules and Tag authority before one verified "
            "user-layer save with backup and exact undo."
        )
        self.held_status=QLabel(held_text)
        self.held_status.setWordWrap(True)
        self.held_status.setObjectName(
            "warningLabel" if preview.held_reasons else "mutedLabel"
        )
        self.held_status.setAccessibleName("Compatibility migration held reasons")
        self.held_status.setToolTip("\n".join(preview.held_reasons))
        layout.addWidget(self.held_status)
        fingerprint=QLabel(f"Plan fingerprint: {preview.plan_fingerprint}")
        fingerprint.setObjectName("mutedLabel")
        fingerprint.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(fingerprint)
        buttons=QDialogButtonBox(QDialogButtonBox.Close)
        self.apply_button=None
        if preview.ready_for_later_review:
            self.apply_button=buttons.addButton(
                "Apply Stable Bindings…",QDialogButtonBox.AcceptRole,
            )
            self.apply_button.setProperty("primary",True)
            self.apply_button.setAccessibleName(
                "Apply reviewed stable Tag bindings"
            )
            buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
