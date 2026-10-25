from __future__ import annotations

from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QScrollArea,
    QFrame,
    QVBoxLayout,
    QWidget,
)
from .status_notifications import FooterMessageBox as QMessageBox
from .components import bind_status_footer, guard_scrollable_value_controls

from core.conflict_policy import (
    ALLOW_ALL,
    ALLOW_ANY,
    CONFIRMED_OWNER_SEPARATION,
    REVIEWED_EXCEPTION_TAGS,
    SUBJECT_COUNT,
    ConflictAllowCondition,
    ConflictAllowPolicy,
    ConflictSubjectPolicy,
    validate_unique_conflict_pairs,
)
from core.conflict_rules import ConflictRuleEngine
from core.knowledge_relationships import (
    SCOPED_CONSTRAINT,
    KnowledgeRelationship,
    KnowledgeRelationshipRegistry,
    RelationshipEndpoint,
)
from core.paths import DEFAULTS_DIR, RELATIONSHIPS_PATH
from core.relationship_workspace import (
    RelationshipWorkspaceService,
    relationship_fingerprint,
)
from .modal_feedback import FeedbackEnabledDialog
from .rule_tag_editor import RuleTagEditor


_SCOPES = (
    ("Image-wide", "image"),
    ("Same subject / character", "same_character"),
    ("Same owned object / assignment", "same_object"),
    ("Same context", "same_context"),
)

_SCOPE_HELP = {
    "image": "Compare Tags across the image's main facts, including different owners. "
    "Each secondary context is checked separately. Example: solo and group can conflict image-wide.",
    "same_character": "Compare Tags within each character. Short hair and long hair on one "
    "character can conflict; one on each of two characters is valid. Conflicting unassigned "
    "Tags still need review, and uncertain ownership is shown as ambiguous.",
    "same_object": "Compare Tags within one object, outfit or owned assignment context. "
    "Different objects are checked separately. Conflicting unassigned Tags remain ambiguous. "
    "Example: glasses and goggles assigned to the same eyewear object.",
    "same_context": "Compare Tags within each saved context, such as a background depiction. "
    "Different contexts and the image's main facts are not combined. "
    "Example: conflicting traits inside one depicted scene.",
}


def conflict_workspace_entries(knowledge, defaults_dir, user_path, *, checkpoint=None):
    """One authoring inventory, including disabled overrides and compatibility rules."""
    service = RelationshipWorkspaceService(
        defaults_dir / "relationships.json", user_path, tags=knowledge,
    )
    legacy = KnowledgeRelationshipRegistry.from_legacy(
        knowledge,
        conflicts=ConflictRuleEngine.load(defaults_dir / "conflicts.json", tags=knowledge),
        kinds=(SCOPED_CONSTRAINT,),
        # This inventory consumes relationship rows, never registry name lookup.
        build_tag_name_index=False,
    )
    if checkpoint is not None:
        checkpoint()
    entries = service.workspace_entries(legacy,kinds=(SCOPED_CONSTRAINT,))
    return service, entries


class ConflictRuleWorkspaceDialog(FeedbackEnabledDialog):
    """Canonical authoring surface for symmetric diagnostic conflict families."""

    feedback_workflow = "Diagnostic conflict rule authoring"

    def __init__(
        self,
        knowledge,
        categories,
        parent=None,
        *,
        focus_relationship_id: str = "",
        focus_tag_id: str = "",
        start_new: bool = False,
    ) -> None:
        super().__init__(parent)
        self.knowledge = knowledge
        self.categories = categories
        self.service = RelationshipWorkspaceService(
            DEFAULTS_DIR / "relationships.json",
            RELATIONSHIPS_PATH,
            tags=knowledge,
        )
        self.current: KnowledgeRelationship | None = None
        self.current_entry = None
        self.changed = False
        self._loading = False
        self._focus_relationship_id = str(focus_relationship_id or "")
        self._focus_tag_id = str(focus_tag_id or "")
        self.setWindowTitle("New Conflict Rule" if start_new else "Edit Conflict Rule")
        self.resize(820, 800)

        root = QVBoxLayout(self)
        intro = QLabel("Choose which Tags conflict and when the combination is allowed. "
                       "Saving changes this reusable rule; image facts stay unchanged.")
        intro.setWordWrap(True)
        root.addWidget(intro)
        self.editor_scroll = QScrollArea()
        self.editor_scroll.setWidgetResizable(True)
        self.editor_scroll.setFrameShape(QFrame.NoFrame)
        self.editor_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        root.addWidget(self.editor_scroll, 1)
        body = QWidget()
        self.editor_scroll.setWidget(body)
        content = QVBoxLayout(body)
        content.setContentsMargins(4, 4, 12, 4)

        def note(text, layout=content):
            label = QLabel(text)
            label.setWordWrap(True)
            label.setObjectName("mutedLabel")
            layout.addWidget(label)
            return label

        def heading(text):
            label = QLabel(text)
            label.setObjectName("sectionTitle")
            content.addWidget(label)

        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText("Example: Contrary hair lengths")
        self.label_edit.setAccessibleName("Conflict rule name")
        form = QFormLayout()
        form.addRow("Rule name", self.label_edit)
        self.enabled_check = QCheckBox("Enable this rule")
        self.enabled_check.setChecked(True)
        form.addRow("", self.enabled_check)
        content.addLayout(form)

        heading("1. Which Tags conflict?")
        note("Choose at least two Tags. Any pair in this group can flag a conflict; "
             "the image does not need to contain every Tag.")
        self.members = RuleTagEditor(knowledge, categories, relation_label="Conflicting")
        self.members.add_button.setText("Add Conflicting Tag…")
        content.addWidget(self.members)

        heading("2. Where should they be compared?")
        self.scope_choice = QComboBox()
        self.scope_choice.setAccessibleName("Where conflicting Tags are compared")
        for label, value in _SCOPES:
            self.scope_choice.addItem(label, value)
        content.addWidget(self.scope_choice)
        self.scope_explanation = note("")
        self.scope_explanation.setAccessibleName("Matching mode explanation and example")
        note("These are built-in matching modes, not saved Tag groups. "
             "Choose the Tags above and customize this rule's exceptions below.")

        self.subject_toggle = QPushButton("Edit Subject Clues…")
        self.subject_toggle.setCheckable(True)
        content.addWidget(self.subject_toggle, 0, Qt.AlignLeft)
        self.subject_panel = QWidget()
        subject_layout = QVBoxLayout(self.subject_panel)
        subject_layout.setContentsMargins(0, 0, 0, 0)
        content.addWidget(self.subject_panel)
        self.subject_panel.hide()
        self.subject_toggle.toggled.connect(self.subject_panel.setVisible)
        note("When conflicting Tags have no assigned character, these exact Tag names "
             "decide whether this rule reports a definite or ambiguous conflict. "
             "They never override explicit character assignments or ignore a conflict. "
             "Changes apply to this rule only.", subject_layout)
        note("Single-subject clues — any one suggests one subject:", subject_layout)
        self.single_subject_tags = RuleTagEditor(
            knowledge, categories, relation_label="Single-subject clue", names=True,
        )
        subject_layout.addWidget(self.single_subject_tags)
        note("Count clues — exactly one suggests one subject; two or more are ambiguous:", subject_layout)
        self.single_count_tags = RuleTagEditor(
            knowledge, categories, relation_label="Single-count clue", names=True,
        )
        subject_layout.addWidget(self.single_count_tags)
        note("Blockers — any one prevents the single-subject assumption:", subject_layout)
        self.multiple_subject_tags = RuleTagEditor(
            knowledge, categories, relation_label="Multiple-subject blocker", names=True,
        )
        subject_layout.addWidget(self.multiple_subject_tags)
        self.restore_subject_defaults = QPushButton("Use Default Clues")
        self.restore_subject_defaults.clicked.connect(self._restore_subject_clues)
        subject_layout.addWidget(self.restore_subject_defaults, 0, Qt.AlignLeft)
        note("Blockers and multiple count clues take precedence even if solo is present. "
             "No clue means uncertain ownership. Use exceptions below when a combination "
             "should actually be allowed.", subject_layout)

        self.exceptions_toggle = QPushButton("Add Exceptions…")
        self.exceptions_toggle.setCheckable(True)
        self.exceptions_toggle.setAccessibleName("Show or hide conflict rule exceptions")
        content.addWidget(self.exceptions_toggle, 0, Qt.AlignLeft)
        self.exceptions_panel = QWidget()
        exceptions = QVBoxLayout(self.exceptions_panel)
        exceptions.setContentsMargins(0, 0, 0, 0)
        content.addWidget(self.exceptions_panel)
        self.exceptions_panel.hide()
        self.exceptions_toggle.toggled.connect(self.exceptions_panel.setVisible)
        note("3. Allow the combination when…", exceptions)
        self.allow_logic = QComboBox()
        self.allow_logic.addItem("Any condition below is met (OR)", ALLOW_ANY)
        self.allow_logic.addItem("Every condition below is met (AND)", ALLOW_ALL)
        self.allow_logic.setAccessibleName("How to combine exception conditions")
        exceptions.addWidget(self.allow_logic)

        self.owner_separation = QCheckBox("The conflicting Tags have confirmed separate owners")
        exceptions.addWidget(self.owner_separation)
        note("Example: short hair belongs to one character and long hair to another. "
             "A subject count alone cannot establish who owns each Tag.", exceptions)
        self.subject_count_enabled = QCheckBox("The reviewed subject count is within these bounds")
        exceptions.addWidget(self.subject_count_enabled)
        self.subject_minimum = QSpinBox()
        self.subject_minimum.setRange(0, 999)
        self.subject_minimum.setSpecialValueText("No minimum")
        self.subject_minimum.setValue(2)
        self.subject_maximum = QSpinBox()
        self.subject_maximum.setRange(0, 999)
        self.subject_maximum.setSpecialValueText("No maximum")
        self.subject_minimum.setAccessibleName("Minimum reviewed subject count")
        self.subject_maximum.setAccessibleName("Maximum reviewed subject count")
        bounds = QHBoxLayout()
        bounds.addWidget(QLabel("Minimum"))
        bounds.addWidget(self.subject_minimum)
        bounds.addWidget(QLabel("Maximum"))
        bounds.addWidget(self.subject_maximum)
        bounds.addStretch(1)
        exceptions.addLayout(bounds)
        note("Uses confirmed count Tags such as solo or 2girls, not the number of character "
             "records. For character/object matching, unknown ownership still needs "
             "review even when the count fits.", exceptions)
        note("These exception Tags are confirmed:", exceptions)
        self.exception_tag_logic = QComboBox()
        self.exception_tag_logic.addItem("At least one exception Tag (OR)", ALLOW_ANY)
        self.exception_tag_logic.addItem("Every exception Tag (AND)", ALLOW_ALL)
        self.exception_tag_logic.setAccessibleName("How to match exception Tags")
        exceptions.addWidget(self.exception_tag_logic)
        self.exception_tags = RuleTagEditor(knowledge, categories, relation_label="Exception")
        self.exception_tags.add_button.setText("Add Exception Tag…")
        exceptions.addWidget(self.exception_tags)
        note("Example: two-tone hair can allow two hair colors. Only confirmed Tags "
             "in the evaluated context count; unreviewed suggestions do not.", exceptions)

        self.details_toggle = QPushButton("Rule Details…")
        self.details_toggle.setCheckable(True)
        content.addWidget(self.details_toggle, 0, Qt.AlignLeft)
        self.details_panel = QWidget()
        details = QVBoxLayout(self.details_panel)
        details.setContentsMargins(0, 0, 0, 0)
        self.rule_context_status = note("", details)
        self.rule_context_status.setAccessibleName("Conflict rule authority and readiness")
        self.reset_button = QPushButton("Reset Personal Override…")
        details.addWidget(self.reset_button, 0, Qt.AlignLeft)
        content.addWidget(self.details_panel)
        self.details_panel.hide()
        self.details_toggle.toggled.connect(self.details_panel.setVisible)
        content.addStretch(1)

        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setAccessibleName("Diagnostic conflict rule preview")
        root.addWidget(self.preview)
        self.save_status = QLabel(self)
        self.save_status.setWordWrap(True)
        self.save_status.setObjectName("mutedLabel")
        bind_status_footer(self, self.save_status, title="Conflict rule")
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.save_button = buttons.addButton("Save Conflict Rule", QDialogButtonBox.AcceptRole)
        self.save_button.setProperty("primary", True)
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.reset_button.clicked.connect(self._reset_override)
        for control in (
            self.label_edit, self.scope_choice, self.enabled_check, self.allow_logic,
            self.owner_separation, self.subject_count_enabled, self.subject_minimum,
            self.subject_maximum, self.exception_tag_logic,
        ):
            signal = getattr(control, "textChanged", None) or getattr(
                control, "currentIndexChanged", None
            ) or getattr(control, "stateChanged", None) or getattr(control, "valueChanged", None)
            signal.connect(self._update_preview)
        self.members.changed.connect(self._update_preview)
        self.exception_tags.changed.connect(self._update_preview)
        for editor in (self.single_subject_tags, self.single_count_tags, self.multiple_subject_tags):
            editor.changed.connect(self._update_preview)
        self._refresh_entries()
        if start_new:
            self._new_rule()
        elif self._focus_relationship_id:
            entry = self._entries_by_id.get(self._focus_relationship_id)
            if entry is None:
                raise ValueError("This conflict rule is no longer available. Refresh the rule list and select it again.")
            self._load_entry(entry)
        else:
            raise ValueError("Select a conflict rule in Rules & Automation, or choose New Conflict Rule.")
        self.label_edit.setFocus()
        guard_scrollable_value_controls(body)

    def _legacy_registry(self):
        return KnowledgeRelationshipRegistry.from_legacy(
            self.knowledge,
            conflicts=ConflictRuleEngine.load(
                DEFAULTS_DIR / "conflicts.json", tags=self.knowledge,
            ),
            kinds=(SCOPED_CONSTRAINT,),
        )

    def _refresh_entries(self):
        self._entries = tuple(
            entry
            for entry in self.service.workspace_entries(self._legacy_registry())
            if entry.relationship.kind == SCOPED_CONSTRAINT
        )
        self._entries_by_id = {
            entry.relationship.id: entry for entry in self._entries
        }

    def _display_name(self, tag_id: str) -> str:
        record = self.knowledge.tags.get(str(tag_id))
        return record.display if record is not None else str(tag_id)

    def _load_entry(self, entry):
        self.current_entry = entry
        self.current = entry.relationship
        relationship = self.current
        policy = ConflictAllowPolicy.from_payload(
            relationship.extras.get("allow_when")
        )
        self._loading = True
        try:
            self.label_edit.setText(relationship.label)
            index = self.scope_choice.findData(relationship.scope)
            if index < 0:
                self.scope_choice.addItem(f"Unsupported matching mode: {relationship.scope}", relationship.scope)
                index = self.scope_choice.count() - 1
            self.scope_choice.setCurrentIndex(index)
            self.enabled_check.setChecked(not relationship.disabled)
            self._set_subject_clues(ConflictSubjectPolicy.from_payload(
                relationship.extras.get("subject_matching")
            ))
            self.members.set_values(
                endpoint.id for endpoint in relationship.endpoints
                if endpoint.kind == "tag"
            )
            self.allow_logic.setCurrentIndex(
                max(0, self.allow_logic.findData(policy.logic))
            )
            exception = policy.condition(REVIEWED_EXCEPTION_TAGS)
            self.exception_tags.set_values(exception.tag_ids if exception else ())
            self.exception_tag_logic.setCurrentIndex(
                max(0,self.exception_tag_logic.findData(
                    exception.tag_logic if exception else ALLOW_ANY
                ))
            )
            self.owner_separation.setChecked(
                policy.condition(CONFIRMED_OWNER_SEPARATION) is not None
            )
            count = policy.condition(SUBJECT_COUNT)
            self.subject_count_enabled.setChecked(count is not None)
            self.subject_minimum.setValue(
                int(count.minimum_subjects or 0) if count else 2
            )
            self.subject_maximum.setValue(
                int(count.maximum_subjects or 0) if count else 0
            )
        finally:
            self._loading = False
        self.reset_button.setVisible(
            relationship.id in self.service.user_store.relationships
        )
        self._set_rule_context(
            f"Stable ID: {relationship.id} · {relationship.source} / "
            f"{relationship.layer} authority · version {relationship.version}."
        )
        self.exceptions_toggle.setChecked(bool(policy.conditions))
        self._update_preview()

    def _new_rule(self):
        self.current = None
        self.current_entry = None
        self._loading = True
        try:
            self.label_edit.clear()
            self.scope_choice.setCurrentIndex(1)
            self.enabled_check.setChecked(True)
            self._set_subject_clues(ConflictSubjectPolicy())
            self.members.set_values((self._focus_tag_id,) if self._focus_tag_id else ())
            self.allow_logic.setCurrentIndex(0)
            self.exception_tags.set_values(())
            self.exception_tag_logic.setCurrentIndex(0)
            self.owner_separation.setChecked(False)
            self.subject_count_enabled.setChecked(False)
            self.subject_minimum.setValue(2)
            self.subject_maximum.setValue(0)
        finally:
            self._loading = False
        self.reset_button.hide()
        self.exceptions_toggle.setChecked(False)
        self._set_rule_context(
            "Choose at least two stable SDXL Tags. Nothing changes until Save Conflict Rule."
        )
        self._update_preview()

    def _set_subject_clues(self, policy):
        self.single_subject_tags.set_values(policy.single_tags)
        self.single_count_tags.set_values(policy.count_tags)
        self.multiple_subject_tags.set_values(policy.multiple_tags)

    def _subject_policy(self):
        return ConflictSubjectPolicy.from_payload({
            "schema_version": 1,
            "single_tags": list(self.single_subject_tags.values()),
            "count_tags": list(self.single_count_tags.values()),
            "multiple_tags": list(self.multiple_subject_tags.values()),
        })

    def _restore_subject_clues(self):
        self._set_subject_clues(ConflictSubjectPolicy())
        self._update_preview()

    def _policy(self) -> ConflictAllowPolicy:
        conditions = []
        exception_ids = self.exception_tags.values()
        if exception_ids:
            conditions.append(
                ConflictAllowCondition(
                    REVIEWED_EXCEPTION_TAGS,
                    tag_ids=exception_ids,
                    tag_logic=str(
                        self.exception_tag_logic.currentData() or ALLOW_ANY
                    ),
                )
            )
        if self.owner_separation.isChecked():
            conditions.append(ConflictAllowCondition(CONFIRMED_OWNER_SEPARATION))
        if self.subject_count_enabled.isChecked():
            minimum = self.subject_minimum.value() or None
            maximum = self.subject_maximum.value() or None
            conditions.append(
                ConflictAllowCondition(
                    SUBJECT_COUNT,
                    minimum_subjects=minimum,
                    maximum_subjects=maximum,
                )
            )
        return ConflictAllowPolicy(
            str(self.allow_logic.currentData() or ALLOW_ANY), tuple(conditions)
        )

    def _candidate(self) -> KnowledgeRelationship:
        if self.scope_choice.currentData() not in dict((value, label) for label, value in _SCOPES):
            raise ValueError("Choose a supported matching mode before saving this rule.")
        members = self.members.values()
        if len(members) < 2:
            raise ValueError("Choose at least two contrary SDXL Tags.")
        if len(members) != len(set(members)):
            raise ValueError("A conflict family cannot repeat one SDXL Tag.")
        exception_ids = set(self.exception_tags.values())
        overlap = exception_ids.intersection(members)
        if overlap:
            raise ValueError(
                "A contrary member cannot also be its own reviewed exception Tag: "
                + ", ".join(self._display_name(tag_id) for tag_id in sorted(overlap))
            )
        policy = self._policy()
        # Reparse the serialized form so the same strict contract guards UI and
        # imported relationship records.
        policy = ConflictAllowPolicy.from_payload(policy.to_dict())
        current = self.current
        extras = dict(current.extras) if current is not None else {}
        subject_policy = self._subject_policy()
        if subject_policy != ConflictSubjectPolicy() or "subject_matching" in extras:
            extras["subject_matching"] = subject_policy.to_dict()
        if policy.conditions:
            extras["allow_when"] = policy.to_dict()
        else:
            extras.pop("allow_when", None)
        endpoints = tuple(
            RelationshipEndpoint("tag", tag_id, self._display_name(tag_id))
            for tag_id in members
        )
        candidate = KnowledgeRelationship(
            id=(
                current.id if current is not None
                else f"user.scoped_constraint.{uuid4().hex}"
            ),
            kind=SCOPED_CONSTRAINT,
            endpoints=endpoints,
            directed=False,
            scope=str(self.scope_choice.currentData() or "same_character"),
            label=self.label_edit.text().strip() or "Contrary Values",
            reviewed=True,
            source="user_review",
            details=current.details if current is not None else (),
            version=(current.version + 1) if current is not None else 1,
            layer="user",
            disabled=not self.enabled_check.isChecked(),
            extras=extras,
        )
        if not candidate.disabled:
            validate_unique_conflict_pairs(
                candidate.id,
                (endpoint.id for endpoint in candidate.endpoints),
                (
                    (
                        entry.relationship.id,
                        (
                            endpoint.id
                            for endpoint in entry.relationship.endpoints
                            if endpoint.kind == "tag"
                        ),
                    )
                    for entry in self._entries
                    if not entry.relationship.disabled
                ),
            )
        return candidate

    def _update_preview(self, *_args):
        if self._loading:
            return
        self.subject_minimum.setEnabled(self.subject_count_enabled.isChecked())
        self.subject_maximum.setEnabled(self.subject_count_enabled.isChecked())
        self.allow_logic.setEnabled(
            bool(
                self.exception_tags.values()
                or self.owner_separation.isChecked()
                or self.subject_count_enabled.isChecked()
            )
        )
        self.exception_tag_logic.setEnabled(bool(self.exception_tags.values()))
        is_character = self.scope_choice.currentData() == "same_character"
        self.subject_toggle.setVisible(is_character)
        self.subject_panel.setVisible(is_character and self.subject_toggle.isChecked())
        clue_editors = (self.single_subject_tags, self.single_count_tags, self.multiple_subject_tags)
        for editor in clue_editors:
            editor.set_excluded_ids(value for other in clue_editors if other is not editor for value in other.values())
        self.members.set_excluded_ids(self.exception_tags.values())
        self.exception_tags.set_excluded_ids(self.members.values())
        names = [self._display_name(tag_id) for tag_id in self.members.values()]
        scope = self.scope_choice.currentText().casefold()
        self.scope_explanation.setText(_SCOPE_HELP.get(
            self.scope_choice.currentData(),
            "This matching mode is not supported by this editor. It has been preserved; "
            "choose a supported mode explicitly before saving.",
        ))
        policy = self._policy()
        count = len(policy.conditions)
        self.exceptions_toggle.setText(
            f"Exceptions ({count})…" if count else "Add Exceptions…"
        )
        try:
            ConflictAllowPolicy.from_payload(policy.to_dict())
            subject_policy = self._subject_policy()
            validation = "" if len(names) >= 2 else "Choose at least two conflicting Tags. "
        except ValueError as exc:
            validation = str(exc) + " "
        supported = self.scope_choice.currentData() in _SCOPE_HELP
        self.save_button.setEnabled(not validation and supported)
        if not self.enabled_check.isChecked():
            result = "Disabled: this rule will not flag conflicts."
        elif validation:
            result = validation
        else:
            shown = ", ".join(names[:4])
            if len(names) > 4:
                shown += f", and {len(names) - 4} more"
            result = f"Flag any pair from {shown} using {scope} matching."
        if is_character:
            customized = self._subject_policy() != ConflictSubjectPolicy() if not validation else False
            self.subject_toggle.setText("Edit Subject Clues (Custom)…" if customized else "Edit Subject Clues…")
        self.preview.setText(
            f"{result}\nAllow when: {policy.summary(self._display_name)}. "
            "No image or projection is changed."
        )

    def _save(self):
        try:
            candidate = self._candidate()
            expected = (
                relationship_fingerprint(self.current)
                if self.current is not None else None
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
            QMessageBox.warning(self, "Conflict Rule Not Saved", str(exc))
            return
        self.changed = True
        self._refresh_entries()
        self.current = candidate
        self._load_entry(self._entries_by_id[candidate.id])
        self.rule_context_status.show()
        self.save_status.show()
        self.save_status.setText(
            "Saved canonical conflict authority and retired compatibility pairs "
            "with verified read-back and one recovery copy. No image was changed."
        )
        self.accept()

    def _set_rule_context(self, text: str) -> None:
        self.rule_context_status.setText(text)
        self.rule_context_status.show()
        self.save_status.setText(text)
        self.save_status.hide()

    def _reset_override(self):
        if (
            self.current is None
            or self.current.id not in self.service.user_store.relationships
        ):
            return
        if QMessageBox.question(
            self,
            "Reset personal conflict override?",
            "Reset this personal override and reveal the reviewed default again? "
            "A recovery copy will be retained; no image will change.",
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
            QMessageBox.warning(self, "Conflict Override Not Reset", str(exc))
            return
        self.changed = True
        self._refresh_entries()
        self.current = None
        restored = self._entries_by_id.get(relationship_id)
        if restored is None:
            self.accept()
        else:
            self._load_entry(restored)
