"""Staged rule lists using the same Tag chips and palette as Image Data."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

from core.tag_collection import normalize_tag
from .modal_feedback import FeedbackEnabledDialog
from .tag_editor import FlowLayout, TagChip, TagPalette, category_details, decode_tag_drag_ids


class RuleTagPickerDialog(FeedbackEnabledDialog):
    feedback_workflow = "Rule Tag selection"

    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self._values = list(editor.values())
        self.setWindowTitle(f"Edit {editor.relation_label} Tags")
        self.resize(900, 700)
        root = QVBoxLayout(self)
        notice = QLabel("Add Tags from the palette, or remove them from this list. "
                        "Use This List stages your choices in the rule; Cancel leaves it unchanged.")
        notice.setWordWrap(True)
        root.addWidget(notice)
        self.selected = RuleTagEditor(editor.knowledge, editor.categories,
                                      relation_label=editor.relation_label, names=editor.names)
        self.selected.set_values(self._values)
        self.selected.set_excluded_ids(editor._excluded)
        self.selected.literal_validator = editor.literal_validator
        self.selected.add_button.hide()
        self.selected.changed.connect(self._selection_changed)
        selection_scroll = QScrollArea()
        selection_scroll.setWidgetResizable(True)
        selection_scroll.setWidget(self.selected)
        selection_scroll.setMaximumHeight(180)
        root.addWidget(selection_scroll)
        self.palette = TagPalette(editor.knowledge, editor.categories, target_label="rule list")
        root.addWidget(self.palette, 1)
        self.palette.tag_requested.connect(self._add)
        self.palette.create_requested.connect(self._add_literal)
        self.palette.show_present_requested.connect(
            lambda name: self.status.setText(f"{name} is already in this list.")
        )
        row = QHBoxLayout()
        self.add_selected = QPushButton("Add Selected Tags")
        self.add_selected.clicked.connect(self._add_selected)
        row.addWidget(self.add_selected)
        row.addStretch(1)
        root.addLayout(row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        use = buttons.addButton("Use This List", QDialogButtonBox.AcceptRole)
        use.setProperty("primary", True)
        use.clicked.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self._selection_changed()

    def _selection_changed(self, *_):
        self._values = list(self.selected.values())
        if hasattr(self, "palette"):
            self.palette.set_state(present=[self.editor.display(value) for value in self._values])

    def _add(self, name):
        record = self.editor.knowledge.find(name)
        if record is None:
            return
        value = record.display if self.editor.names else record.id
        if value in self.editor._excluded:
            self.status.setText("This Tag is already used in an incompatible list. Remove it there first.")
            return
        self.selected.set_values((*self._values, value))
        self._selection_changed()
        self.status.clear()

    def _add_literal(self, name):
        if not self.editor.names:
            self.status.setText("Choose a known Tag. New reusable Tags can be created in Knowledge.")
            return
        value = normalize_tag(name)
        if callable(self.editor.literal_validator) and not self.editor.literal_validator(value):
            self.status.setText("Choose a known Tag or a template using a defined reusable value.")
            return
        if not value or value in self.editor._excluded:
            self.status.setText("Choose a non-empty Tag name that is not in another matching-clue list.")
            return
        self.selected.set_values((*self._values, value))
        self._selection_changed()
        self.status.setText("Exact Tag name added to this rule only. No System Knowledge Tag was created.")

    def _add_selected(self):
        for name in self.palette.selected_values():
            self._add(name)

    def values(self):
        return tuple(self._values)


class RuleTagEditor(QWidget):
    changed = Signal(object)
    PAGE_SIZE = 80

    def __init__(self, knowledge, categories, *, relation_label, names=False, parent=None):
        super().__init__(parent)
        self.knowledge, self.categories = knowledge, categories
        self.relation_label, self.names = relation_label, names
        self._values, self._excluded = [], set()
        self.literal_validator = None
        self._limit = self.PAGE_SIZE
        self.setAcceptDrops(True)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.container = QWidget()
        self.flow = FlowLayout(self.container)
        self.chip_scroll = QScrollArea()
        self.chip_scroll.setWidgetResizable(True)
        self.chip_scroll.setWidget(self.container)
        self.chip_scroll.setMinimumHeight(55)
        self.chip_scroll.setMaximumHeight(130)
        root.addWidget(self.chip_scroll)
        self.add_button = QPushButton("Edit Tags…")
        self.add_button.setProperty("compact", True)
        self.add_button.setAccessibleName(f"Edit {relation_label} Tags")
        self.add_button.clicked.connect(self._choose)
        root.addWidget(self.add_button, 0, Qt.AlignLeft)
        self.load_more = QPushButton("Show More Tags")
        self.load_more.clicked.connect(self._show_more)
        root.addWidget(self.load_more, 0, Qt.AlignLeft)
        self.set_values(())

    def values(self):
        return tuple(self._values)

    def display(self, value):
        record = self.knowledge.tags.get(value) if not self.names else self.knowledge.find(value)
        return value if self.names or record is None else record.display

    def set_excluded_ids(self, values):
        self._excluded = set(values)

    def set_values(self, values):
        self._values = list(dict.fromkeys(values))
        self._limit = self.PAGE_SIZE
        self._render()

    def _show_more(self):
        self._limit += self.PAGE_SIZE
        self._render()

    def _render(self):
        self.load_more.setVisible(len(self._values) > self._limit)
        self.load_more.setText(f"Show More Tags ({max(0, len(self._values) - self._limit)})")
        self.flow.clear()
        self._chips = []
        for value in self._values[:self._limit]:
            name = self.display(value)
            record = self.knowledge.find(name) if self.names else self.knowledge.tags.get(value)
            label, color, tooltip = category_details(name, self.knowledge, self.categories)
            chip = TagChip(name, category_label=label, color=color, tooltip=tooltip,
                           editable=False, relationship_label=self.relation_label,
                           stable_id=record.id if record else "")
            chip.set_knowledge_context(self.knowledge, self.categories)
            chip.remove_requested.connect(lambda _, key=value: self._remove(key))
            self.flow.addWidget(chip)
            self._chips.append(chip)
        if not self._values:
            label = QLabel("No Tags selected")
            label.setObjectName("mutedLabel")
            self.flow.addWidget(label)

    def _remove(self, value):
        self.set_values(item for item in self._values if item != value)
        self.changed.emit(self.values())

    def _choose(self):
        dialog = RuleTagPickerDialog(self)
        if dialog.exec() == QDialog.Accepted:
            self.set_values(dialog.values())
            self.changed.emit(self.values())

    def _drag_values(self, event):
        ids = decode_tag_drag_ids(event.mimeData(), self.knowledge)
        return tuple(self.knowledge.tags[key].display if self.names else key for key in ids
                     if key in self.knowledge.tags)

    def dragEnterEvent(self, event):
        values = self._drag_values(event)
        if values and not self._excluded.intersection(values):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        values = self._drag_values(event)
        if not values or self._excluded.intersection(values):
            event.ignore()
            return
        self.set_values((*self._values, *values))
        self.changed.emit(self.values())
        event.acceptProposedAction()
