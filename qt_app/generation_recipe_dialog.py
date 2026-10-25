"""Preview changes from one reusable generation setup before applying them."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QComboBox, QDialogButtonBox, QLabel, QPushButton, QTextEdit, QVBoxLayout
from .components import DisclosureButton
from .modal_feedback import FeedbackEnabledDialog
from core.generation_recipes import _display_label, preview_recipe


class GenerationRecipeDialog(FeedbackEnabledDialog):
    feedback_workflow = "Generation recipe"

    def __init__(self, recipe, current, *, resource_notes=(), recoverable_resource=None,
                 recoverable_resources=(),
                 locate_exact=None, parent=None):
        super().__init__(parent)
        self.recipe = recipe; self.current = current; self.selected_plan = None
        self.setWindowTitle("Apply Generation Setup: " + recipe["name"])
        self.resize(850, 450); self.setMaximumHeight(450)
        layout = QVBoxLayout(self)
        label = QLabel("Review how this setup changes your current request. Applying it creates no job; model and generator readiness are checked again before generation.")
        label.setWordWrap(True); layout.addWidget(label)
        has_positive = bool(recipe.get("prompt_parts")) if recipe.get("schema_version") == 3 else "prompts" in recipe
        has_negative = "negative_prompt" in recipe if recipe.get("schema_version") == 3 else "prompts" in recipe
        sides = "positive and negative prompts" if has_positive and has_negative else (
            "positive prompt" if has_positive else "negative prompt")
        self.prompts = QCheckBox("Also replace my " + sides)
        self.prompts.setEnabled(has_positive or has_negative)
        self.prompts.setVisible(has_positive or has_negative)
        if not (has_positive or has_negative):
            self.prompts.setToolTip("This setup was saved without prompt templates.")
        layout.addWidget(self.prompts)
        self.resource_notes = QLabel()
        self.resource_notes.setWordWrap(True)
        self.resource_notes.setTextFormat(Qt.PlainText)
        layout.addWidget(self.resource_notes)
        self.missing_resource_label = QLabel("Missing model component to locate")
        layout.addWidget(self.missing_resource_label)
        self.missing_resource_choice = QComboBox()
        self.missing_resource_choice.setAccessibleName("Missing model component to locate")
        layout.addWidget(self.missing_resource_choice)
        self.locate_exact_button = QPushButton("Locate Exact Copy…")
        self.locate_exact_button.setAutoDefault(False)
        self.locate_exact_button.clicked.connect(locate_exact or (lambda: None))
        layout.addWidget(self.locate_exact_button, 0, Qt.AlignLeft)
        self.location_boundary = QLabel(
            "Adding a verified location updates Models separately. Cancel leaves this request unchanged "
            "but does not undo an approved location update. No model is loaded or run here."
        )
        self.location_boundary.setWordWrap(True)
        self.location_boundary.setTextFormat(Qt.PlainText)
        layout.addWidget(self.location_boundary)
        self.location_saved = False
        self.preview = QTextEdit(); self.preview.setReadOnly(True)
        self.preview.setMinimumHeight(130); self.preview.setMaximumHeight(230)
        layout.addWidget(self.preview)
        self.exact = QTextEdit(); self.exact.setReadOnly(True)
        self.exact.setAccessibleName("Exact setup resource identities")
        self.exact.setMaximumHeight(170)
        self.show_exact = DisclosureButton("Show Exact Resource Details", "Hide Exact Resource Details", self.exact, self)
        self.show_exact.toggled.connect(self._resize_for_details)
        layout.addWidget(self.show_exact)
        layout.addWidget(self.exact)
        layout.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Apply | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Apply).setText("Apply Reviewed Setup")
        buttons.button(QDialogButtonBox.Apply).clicked.connect(self.apply)
        buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self.apply_button = buttons.button(QDialogButtonBox.Apply)
        self.set_resource_review(resource_notes, recoverable_resource,
                                 recoverable_resources=recoverable_resources,
                                 locate_available=locate_exact is not None)
        self.prompts.toggled.connect(self.refresh); self.refresh()

    def set_resource_review(self, notes, recoverable_resource=None, *,
                            recoverable_resources=(), locate_available=True):
        self.resource_notes.setText("\n".join(notes) or
            "Your generator and exact model files will be checked on Readiness before generation.")
        previous = self.selected_recoverable_resource()
        resources = tuple(recoverable_resources or ()) or ((recoverable_resource,) if recoverable_resource else ())
        self.missing_resource_choice.clear()
        for row in resources:
            identity, role = row[:2]
            label = row[2] if len(row) > 2 else role.replace("_", " ").title()
            self.missing_resource_choice.addItem(f"{role.replace('_', ' ').title()}: {label}", (identity, role))
        if previous:
            for index in range(self.missing_resource_choice.count()):
                if self.missing_resource_choice.itemData(index) == previous:
                    self.missing_resource_choice.setCurrentIndex(index)
                    break
        show_choice = len(resources) > 1 and locate_available
        self.missing_resource_label.setVisible(show_choice)
        self.missing_resource_choice.setVisible(show_choice)
        can_locate = bool(resources and locate_available)
        self.locate_exact_button.setVisible(can_locate)
        self.location_boundary.setVisible(can_locate or self.location_saved)

    def selected_recoverable_resource(self):
        return self.missing_resource_choice.currentData()

    def mark_location_saved(self):
        self.location_saved = True
        self.location_boundary.setText(
            "The exact location was saved in Models. Cancel leaves this request unchanged "
            "but does not undo that separate confirmed update. No model was loaded or run."
        )
        self.location_boundary.show()

    def set_location_rechecking(self, active):
        self.apply_button.setEnabled(not active)
        self.locate_exact_button.setEnabled(not active)
        if active:
            self.resource_notes.setText("Rechecking the saved setup's exact model location…")

    def refresh(self):
        result = preview_recipe(self.recipe, self.current, include_prompts=self.prompts.isChecked())
        self.selected_plan = result["plan"]
        lines = ["Review these changes before applying:"]
        exact_lines = ["Saved resource identities (readiness checks these again):"]
        def resource_label(row, context):
            compatibility = row.get("compatibility", ())
            family = compatibility[0] if isinstance(compatibility, (list, tuple)) and len(compatibility) == 1 else ""
            values = context.get("resource_display_labels", ())
            for item in values if isinstance(values, (list, tuple)) else ():
                if (isinstance(item, dict) and
                    (item.get("resource_id"), item.get("role"), item.get("family")) ==
                    (row.get("id"), row.get("role"), family)):
                    if _display_label(item.get("label")):
                        return item["label"]
            return row.get("name") or row.get("id")
        def resource_identity(rows):
            return tuple((row.get("id"), row.get("role"), row.get("hashes", {}).get("sha256"))
                         for row in rows or ())
        retained = []
        if self.recipe.get("schema_version") == 3:
            selected = set(resource_identity(self.recipe.get("resources", ())))
            remaining = set(resource_identity(result["plan"].request.to_dict().get("resources", ())))
            retained = [row for row in self.current.request.to_dict().get("resources", ())
                        if (row.get("id"), row.get("role"), row.get("hashes", {}).get("sha256"))
                        in remaining - selected]
            if retained:
                role_labels = {"lora": "LoRA", "vae": "VAE", "checkpoint": "Checkpoint"}
                names = [f"{role_labels.get(row['role'], row['role'].replace('_', ' ').title())}: "
                         f"{resource_label(row, self.current.target_context)}" for row in retained]
                summary = ", ".join(names[:3])
                if len(names) > 3:
                    summary += f" and {len(names) - 3} more"
                lines.append(f"Kept from this request (not supplied by the setup): {summary}.")
                lines.append("Apply keeps these components. Remove any you do not want in Request afterward.")
        for change in result["changes"]:
            key, before, after = change["setting"], change["before"], change["after"]
            if key == "resources":
                if resource_identity(before) != resource_identity(after):
                    old = ", ".join(resource_label(row, self.current.target_context) for row in before or ()) or "none"
                    new = ", ".join(resource_label(row, result["plan"].target_context) for row in after or ()) or "none"
                    lines.append(f"Model components: {old} → {new}")
                elif before != after:
                    lines.append("Model component details will use the saved setup's allowlisted values; inspect exact details if needed.")
            elif key == "stages":
                lines.append(f"Stages: {len(before or ())} → {len(after or ())}; current source inputs and seeds stay bound")
            elif key == "Finishing":
                lines.append(f"Finishing: {before.get('kind') if isinstance(before, dict) else 'none'} → "
                             f"{after.get('kind') if isinstance(after, dict) else 'none'}")
            elif key == "model_hash":
                lines.append("Exact model identity changes; inspect resource details before applying.")
            else:
                label = "Guidance" if key == "cfg_scale" else key.replace('_', ' ').title()
                lines.append(f"{label}: {before if before not in (None, '') else 'none'} → "
                             f"{after if after not in (None, '') else 'none'}")
        if len(lines) == 1: lines.append("No request settings change.")
        lines.extend(note for note in result["notes"] if not (
            retained and note.startswith("Only the selected exact components are updated.")
        ))
        self.preview.setPlainText("\n".join(lines))
        if self.recipe.get("settings", {}).get("model_hash") and not self.recipe.get("resources"):
            exact_lines.append("Saved model SHA-256: " + self.recipe["settings"]["model_hash"])
        for row in self.recipe.get("resources", ()):
            exact_lines.append(f"{row['role']}: {resource_label(row, result['plan'].target_context)}")
            exact_lines.append(f"Resource ID: {row['id']}")
            if row["id"] != "sha256:" + row["hashes"]["sha256"].removeprefix("sha256:"):
                exact_lines.append(f"SHA-256: {row['hashes']['sha256']}")
            if row.get("compatibility"):
                exact_lines.append("Recorded family: " + ", ".join(row["compatibility"]))
            if row.get("weights"):
                exact_lines.append("Recorded weights: " + ", ".join(
                    f"{name}={value}" for name, value in row["weights"].items()))
            exact_lines.append("")
        if len(exact_lines) == 1:
            exact_lines.append("No model resources saved in this setup.")
        self.exact.setPlainText("\n".join(exact_lines))

    def _resize_for_details(self, expanded):
        height = 620 if expanded else 450
        self.setMaximumHeight(height)
        self.resize(self.width(), height)

    def apply(self):
        self.refresh(); self.accept()
