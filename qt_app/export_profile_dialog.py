from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from .status_notifications import FooterMessageBox as QMessageBox
from .components import bind_status_footer

from core import (
    EXPORT_PROFILE_FORMAT_VERSION,
    ExportProfile,
    ExportProfileStore,
    ExportProfileStoreError,
    ExportProfileValidationError,
)
from core.export_profile import (
    HIERARCHY_AS_CONFIRMED,
    HIERARCHY_INCLUDE_BROADER,
    HIERARCHY_KEEP_MOST_SPECIFIC,
)
from .modal_feedback import FeedbackEnabledDialog


_INCLUSION_FIELDS = (
    "include_character_names",
    "include_character_tags",
    "include_outfit_names",
    "include_outfit_tags",
    "include_outfit_prose",
    "include_object_identities",
    "include_object_tags",
    "include_global",
    "include_scene",
    "include_contexts",
    "include_sdxl",
    "include_uncategorized",
    "include_custom_groups",
    "include_plain_language",
)

_INCLUSION_LABELS = {
    "include_character_names": "Character identities and sources",
    "include_character_tags": "Character-owned SDXL Tags",
    "include_outfit_names": "Outfit names",
    "include_outfit_tags": "Outfit identity and reviewed component SDXL Tags",
    "include_outfit_prose": "Outfit descriptions",
    "include_object_identities": "Object identities",
    "include_object_tags": "Object-owned SDXL Tags",
    "include_global": "Global facts and visual style",
    "include_scene": "Scene facts",
    "include_contexts": "Secondary image contexts",
    "include_sdxl": "General SDXL Tags",
    "include_uncategorized": "Uncategorized SDXL Tags",
    "include_custom_groups": "Custom fact groups",
    "include_plain_language": "Plain Language Descriptions",
}

_HIERARCHY_CHOICES = (
    (
        HIERARCHY_KEEP_MOST_SPECIFIC,
        "Keep Most Specific",
        "Export only the most detailed compatible value for each known subject and context.",
    ),
    (
        HIERARCHY_INCLUDE_BROADER,
        "Include Broader Tags",
        "Export the detailed value and its reviewed broader Tags when the training vocabulary expects both.",
    ),
    (
        HIERARCHY_AS_CONFIRMED,
        "As Confirmed",
        "Keep compatible confirmed Tags as entered; useful for older profiles and investigation.",
    ),
)


def _one_line(value: str) -> str:
    return " ".join(value.split())


class ExportProfileManagerDialog(FeedbackEnabledDialog):
    """Edit user-owned projection profiles without changing image authority."""

    feedback_workflow = "Training export profile manager"

    def __init__(
        self,
        store: ExportProfileStore,
        record,
        characters,
        knowledge,
        maximum_content_rating: str,
        parent=None,
        *,
        current_profile_id: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.store = store
        self.record = record
        self.characters = characters
        self.knowledge = knowledge
        self.maximum_content_rating = maximum_content_rating
        self._current_profile: ExportProfile | None = None
        self.setWindowTitle("Manage Training Profiles")
        self.resize(1060, 760)

        root = QVBoxLayout(self)
        intro = QLabel(
            "Create named training projections from the complete confirmed Image "
            "Knowledge. Editing a profile never changes image facts, TXT, Metadata, "
            "source images, or file locations."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        self.issue_label = QLabel()
        self.issue_label.setWordWrap(True)
        self.issue_label.setObjectName("warningLabel")
        root.addWidget(self.issue_label)

        self.legacy_format_label = QLabel()
        self.legacy_format_label.setWordWrap(True)
        self.legacy_format_label.setObjectName("warningLabel")
        self.legacy_format_label.setAccessibleName(
            "Legacy training profile format warning"
        )
        root.addWidget(self.legacy_format_label)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 8, 0)
        left_layout.addWidget(QLabel("Profiles"))
        self.profile_list = QListWidget()
        self.profile_list.setAccessibleName("Training profiles")
        self.profile_list.currentItemChanged.connect(self._selection_changed)
        left_layout.addWidget(self.profile_list, 1)
        profile_actions = QHBoxLayout()
        self.duplicate_button = QPushButton("Duplicate Profile…")
        self.duplicate_button.clicked.connect(self._duplicate_profile)
        self.delete_button = QPushButton("Delete Profile…")
        self.delete_button.clicked.connect(self._delete_profile)
        profile_actions.addWidget(self.duplicate_button)
        profile_actions.addWidget(self.delete_button)
        left_layout.addLayout(profile_actions)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.description_edit = QTextEdit()
        self.description_edit.setMaximumHeight(72)
        self.target_family_edit = QLineEdit()
        self.target_version_edit = QLineEdit()
        self.objective_edit = QLineEdit()
        self.trigger_edit = QLineEdit()
        form.addRow("Profile Name", self.name_edit)
        form.addRow("Description", self.description_edit)
        form.addRow("Target Family", self.target_family_edit)
        form.addRow("Target Version", self.target_version_edit)
        form.addRow("Training Objective", self.objective_edit)
        form.addRow("Trigger Policy", self.trigger_edit)
        right_layout.addLayout(form)

        included_group = QGroupBox("Included Image Knowledge")
        included_layout = QGridLayout(included_group)
        self.inclusion_checks: dict[str, QCheckBox] = {}
        for index, field_name in enumerate(_INCLUSION_FIELDS):
            checkbox = QCheckBox(_INCLUSION_LABELS[field_name])
            checkbox.stateChanged.connect(self._update_preview)
            self.inclusion_checks[field_name] = checkbox
            included_layout.addWidget(checkbox, index // 2, index % 2)
        self.inclusion_checks["include_plain_language"].stateChanged.connect(
            self._plain_language_changed
        )
        right_layout.addWidget(included_group)

        behavior_group = QGroupBox("Projection Behavior")
        behavior_layout = QGridLayout(behavior_group)
        self.ordering_check = QCheckBox("Use reviewed System Knowledge order")
        self.aliases_check = QCheckBox("Use canonical aliases")
        self.automation_check = QCheckBox("Apply training-projection rules")
        for index, checkbox in enumerate(
            (self.ordering_check, self.aliases_check, self.automation_check)
        ):
            checkbox.stateChanged.connect(self._update_preview)
            behavior_layout.addWidget(checkbox, 0, index)
        hierarchy_label = QLabel("Tag Hierarchy")
        hierarchy_label.setToolTip(
            "How reviewed broader and more-specific Tags are written to training captions."
        )
        self.hierarchy_choice = QComboBox()
        self.hierarchy_choice.setAccessibleName("Tag hierarchy policy")
        for policy_id, label, tooltip in _HIERARCHY_CHOICES:
            self.hierarchy_choice.addItem(label, policy_id)
            self.hierarchy_choice.setItemData(
                self.hierarchy_choice.count() - 1,
                tooltip,
                Qt.ToolTipRole,
            )
        self.hierarchy_choice.setToolTip(_HIERARCHY_CHOICES[0][2])
        self.hierarchy_choice.currentIndexChanged.connect(
            self._hierarchy_policy_changed
        )
        behavior_layout.addWidget(hierarchy_label, 1, 0)
        behavior_layout.addWidget(self.hierarchy_choice, 1, 1, 1, 2)
        right_layout.addWidget(behavior_group)

        self.identity_label = QLabel()
        self.identity_label.setWordWrap(True)
        self.identity_label.setObjectName("mutedLabel")
        right_layout.addWidget(self.identity_label)
        right_layout.addWidget(QLabel("Exact preview for the selected image"))
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMaximumHeight(120)
        right_layout.addWidget(self.preview)
        self.hierarchy_status = QLabel()
        self.hierarchy_status.setWordWrap(True)
        self.hierarchy_status.setObjectName("mutedLabel")
        self.hierarchy_status.setAccessibleName("Tag hierarchy preview status")
        right_layout.addWidget(self.hierarchy_status)
        save_row = QHBoxLayout()
        self.save_status = QLabel(self)
        self.save_status.setObjectName("mutedLabel")
        self.save_status.setAccessibleName("Training profile save status")
        save_row.addStretch(1)
        self.save_button = QPushButton("Save Profile")
        self.save_button.setProperty("primary", True)
        self.save_button.clicked.connect(self._save_profile)
        save_row.addWidget(self.save_button)
        right_layout.addLayout(save_row)
        bind_status_footer(self, self.save_status, title="Training profile")
        splitter.addWidget(right)
        splitter.setSizes((300, 740))

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        for editor in (
            self.name_edit,
            self.description_edit,
            self.target_family_edit,
            self.target_version_edit,
            self.objective_edit,
            self.trigger_edit,
        ):
            if isinstance(editor, QTextEdit):
                editor.textChanged.connect(self._update_preview)
            else:
                editor.textChanged.connect(self._update_preview)

        self._reload_profiles(current_profile_id)

    def selected_profile_id(self) -> str | None:
        item = self.profile_list.currentItem()
        return str(item.data(Qt.UserRole)) if item is not None else None

    def _reload_profiles(self, selected_id: str | None = None) -> None:
        self.profile_list.blockSignals(True)
        self.profile_list.clear()
        selected_row = 0
        for row, profile in enumerate(self.store.all_profiles()):
            kind = (
                "My profile"
                if profile.profile_id in self.store.profiles_by_id
                else "CollectiGen profile"
            )
            legacy = profile.effective_format_version < EXPORT_PROFILE_FORMAT_VERSION
            legacy_suffix = "  ·  Legacy projection" if legacy else ""
            item = QListWidgetItem(
                f"{profile.name}  ·  v{profile.version}  ·  {kind}{legacy_suffix}"
            )
            item.setData(Qt.UserRole, profile.profile_id)
            item.setToolTip(profile.profile_id)
            self.profile_list.addItem(item)
            if profile.profile_id == selected_id:
                selected_row = row
        self.profile_list.blockSignals(False)
        issue_count = len(self.store.issues)
        shown_issues = self.store.issues[:3]
        self.issue_label.setText(
            (
                f"{issue_count:,} saved profile issue(s); showing "
                f"{len(shown_issues):,}:\n"
                + "\n".join(issue.message for issue in shown_issues)
            )
            if issue_count
            else ""
        )
        self.issue_label.setVisible(bool(self.store.issues))
        if self.profile_list.count():
            self.profile_list.setCurrentRow(selected_row)
            self._selection_changed(self.profile_list.currentItem(), None)

    def _selection_changed(self, current, _previous) -> None:
        if current is None:
            return
        self.save_status.clear()
        profile = self.store.resolve(current.data(Qt.UserRole))
        self._current_profile = profile
        self.name_edit.setText(profile.name)
        self.description_edit.setPlainText(profile.description)
        self.target_family_edit.setText(profile.target_model_family)
        self.target_version_edit.setText(profile.target_model_version)
        self.objective_edit.setText(profile.training_objective)
        self.trigger_edit.setText(profile.trigger_policy)
        for field_name, checkbox in self.inclusion_checks.items():
            checkbox.setChecked(bool(getattr(profile, field_name)))
        self.ordering_check.setChecked(profile.apply_system_ordering)
        self.aliases_check.setChecked(profile.canonicalize_aliases)
        self.automation_check.setChecked(profile.apply_automation_rules)
        hierarchy_index = self.hierarchy_choice.findData(profile.hierarchy_policy)
        self.hierarchy_choice.setCurrentIndex(max(0, hierarchy_index))
        editable = (
            profile.profile_id in self.store.profiles_by_id and self.store.writable
        )
        for widget in (
            self.name_edit,
            self.description_edit,
            self.target_family_edit,
            self.target_version_edit,
            self.objective_edit,
            self.trigger_edit,
            *self.inclusion_checks.values(),
            self.ordering_check,
            self.aliases_check,
            self.automation_check,
            self.hierarchy_choice,
        ):
            widget.setEnabled(editable)
        self.inclusion_checks["include_outfit_prose"].setEnabled(
            editable and self.inclusion_checks["include_plain_language"].isChecked()
        )
        self.save_button.setEnabled(editable)
        self.delete_button.setEnabled(editable)
        self.duplicate_button.setEnabled(self.store.writable)
        legacy = profile.effective_format_version < EXPORT_PROFILE_FORMAT_VERSION
        self.legacy_format_label.setText(
            (
                f"Legacy projection format {profile.effective_format_version}: assigned "
                "Outfit identity Tags are not emitted. Duplicate to the current "
                f"format {EXPORT_PROFILE_FORMAT_VERSION} before using this profile "
                "for new outfit training."
            )
            if legacy
            else ""
        )
        self.legacy_format_label.setVisible(legacy)
        self.duplicate_button.setText(
            "Duplicate to Current Format…" if legacy else "Duplicate Profile…"
        )
        self._update_preview()

    def _candidate(self) -> ExportProfile:
        if self._current_profile is None:
            raise RuntimeError("Select a training profile first.")
        inclusion = {
            field_name: checkbox.isChecked()
            for field_name, checkbox in self.inclusion_checks.items()
        }
        previous_inclusion = tuple(
            bool(getattr(self._current_profile, name)) for name in _INCLUSION_FIELDS
        )
        next_inclusion = tuple(inclusion[name] for name in _INCLUSION_FIELDS)
        strategy = self._current_profile.caption_strategy
        if next_inclusion != previous_inclusion:
            includes_tags = any(
                inclusion[name] for name in _INCLUSION_FIELDS if name != "include_plain_language"
            )
            includes_prose = inclusion["include_plain_language"]
            if includes_tags and includes_prose:
                strategy = "custom_sdxl_tags_then_plain_language"
            elif includes_tags:
                strategy = "custom_sdxl_tags_only"
            else:
                strategy = "custom_plain_language_only"
        return replace(
            self._current_profile,
            name=_one_line(self.name_edit.text()),
            description=_one_line(self.description_edit.toPlainText()),
            target_model_family=_one_line(self.target_family_edit.text()),
            target_model_version=_one_line(self.target_version_edit.text()),
            training_objective=_one_line(self.objective_edit.text()),
            trigger_policy=_one_line(self.trigger_edit.text()),
            caption_strategy=strategy,
            apply_system_ordering=self.ordering_check.isChecked(),
            canonicalize_aliases=self.aliases_check.isChecked(),
            apply_automation_rules=self.automation_check.isChecked(),
            hierarchy_policy=str(self.hierarchy_choice.currentData()),
            **inclusion,
        )

    def _hierarchy_policy_changed(self, index: int) -> None:
        tooltip = self.hierarchy_choice.itemData(index, Qt.ToolTipRole) or ""
        self.hierarchy_choice.setToolTip(str(tooltip))
        self._update_preview()

    def _plain_language_changed(self, state) -> None:
        if state != Qt.Checked.value:
            self.inclusion_checks["include_outfit_prose"].setChecked(False)
        self.inclusion_checks["include_outfit_prose"].setEnabled(
            bool(state)
            and self._current_profile is not None
            and self._current_profile.profile_id in self.store.profiles_by_id
            and self.store.writable
        )

    def _update_preview(self, *_args) -> None:
        if self._current_profile is None:
            return
        try:
            profile = self._candidate().with_maximum_content_rating(
                self.maximum_content_rating
            )
            projection = profile.project(
                self.record.annotation, self.characters, self.knowledge
            )
            value = projection.render(profile.separator)
            self.preview.setPlainText(
                value
                if value.strip()
                else "(This profile produces no training TXT for this image.)"
            )
            held = sum(item.action == "held" for item in projection.decisions)
            reduced = sum(
                item.action == "omitted" and bool(item.relationship_path)
                for item in projection.decisions
            )
            expanded = sum(
                item.source.endswith(" / Hierarchy")
                and item.action in {"emitted", "combined"}
                for item in projection.decisions
            )
            if held:
                self.hierarchy_status.setText(
                    f"Possible Conflict: {held:,} Tag value(s) are held for review; no value was chosen automatically."
                )
            elif reduced:
                self.hierarchy_status.setText(
                    f"Training Output: {reduced:,} broader Tag value(s) collapse into more-specific values."
                )
            elif expanded:
                self.hierarchy_status.setText(
                    f"Training Output: {expanded:,} reviewed broader Tag value(s) are included."
                )
            else:
                self.hierarchy_status.setText(
                    "Training Output: no owner-scoped hierarchy changes for this image."
                )
        except Exception as exc:
            self.preview.setPlainText(f"Preview unavailable: {exc}")
            self.hierarchy_status.clear()
        ownership = (
            "My profile"
            if self._current_profile.profile_id in self.store.profiles_by_id
            else "CollectiGen profile · duplicate this profile to customize it"
        )
        self.identity_label.setText(
            f"{ownership} · Stable ID: {self._current_profile.profile_id} · "
            f"Version {self._current_profile.version} · "
            f"Format {self._current_profile.effective_format_version}"
        )

    def _duplicate_profile(self) -> None:
        if self._current_profile is None:
            return
        default_name = f"{self._current_profile.name} copy"
        name, accepted = QInputDialog.getText(
            self,
            "Duplicate Training Profile",
            "Profile Name",
            text=default_name,
        )
        if not accepted:
            return
        try:
            result = self.store.duplicate(self._current_profile, name)
        except Exception as exc:
            QMessageBox.warning(self, "Profile Not Created", str(exc))
            return
        self._reload_profiles(result.profile.profile_id)
        self.save_status.setText(f"Created {result.profile.name} v{result.profile.version}.")

    def _save_profile(self) -> None:
        if (
            self._current_profile is None
            or self._current_profile.profile_id not in self.store.profiles_by_id
        ):
            return
        try:
            result = self.store.save_profile(
                self._candidate(),
                expected_fingerprint=self._current_profile.fingerprint(),
            )
        except ExportProfileValidationError as exc:
            QMessageBox.warning(self, "Profile Not Saved", str(exc))
            return
        except ExportProfileStoreError as exc:
            QMessageBox.warning(self, "Profile Not Saved", str(exc))
            self.store.load()
            self._reload_profiles(self._current_profile.profile_id)
            return
        except Exception as exc:
            QMessageBox.warning(self, "Profile Not Saved", str(exc))
            return
        self._reload_profiles(result.profile.profile_id)
        self.save_status.setText(
            f"Saved {result.profile.name} v{result.profile.version}."
            if result.changed
            else "No profile changes to save."
        )

    def _delete_profile(self) -> None:
        profile = self._current_profile
        if profile is None or profile.profile_id not in self.store.profiles_by_id:
            return
        answer = QMessageBox.question(
            self,
            "Delete Training Profile",
            f"Delete “{profile.name}”?\n\n"
            "Existing export manifests and TXT projection snapshots retain their "
            "exact saved profile. Image Knowledge, TXT, Metadata, and images remain unchanged.",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.store.delete_profile(
                profile.profile_id,
                expected_fingerprint=profile.fingerprint(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "Profile Not Deleted", str(exc))
            self.store.load()
            self._reload_profiles(profile.profile_id)
            return
        self._reload_profiles()
        self.save_status.setText(f"Deleted {profile.name}. A recovery copy was retained.")
