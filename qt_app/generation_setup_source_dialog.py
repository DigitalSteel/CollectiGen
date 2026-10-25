"""Choose the source of one canonical Generation Setup draft."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QRadioButton, QVBoxLayout,
)

from .components import BoundedSearchableComboBox


class GenerationSetupSourceDialog(QDialog):
    """Keep the first decision small; exact identities are an optional route."""

    def __init__(self, characters, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create a Generation Setup")
        self.resize(540, 230)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Write a reusable setup yourself, or start with suggested details "
            "from images tagged with a known Character or Outfit. "
            "Suggestions are optional and reviewed before saving."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.manual = QRadioButton("Write a setup manually")
        self.character = QRadioButton("Suggest from a Character")
        self.outfit = QRadioButton("Suggest from an Outfit")
        self.manual.setChecked(True)
        for option in (self.manual, self.character, self.outfit):
            layout.addWidget(option)
            option.toggled.connect(self._update_choice)
        self.identity = BoundedSearchableComboBox(
            placeholder="Type to find a known Character or Outfit…", max_visible=12,
        )
        self.identity.setAccessibleName("Character or Outfit for suggested setup")
        self.identity.currentIndexChanged.connect(self._update_continue)
        layout.addWidget(self.identity)
        self.note = QLabel()
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        layout.addStretch(1)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Continue")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._characters = None if callable(characters) else characters
        self._load_characters = characters if callable(characters) else None
        self._identity_error = False
        self._update_choice()

    def _update_choice(self):
        kind = self.source_kind
        if (kind != "manual" and self._characters is None
                and self._load_characters is not None and not self._identity_error):
            try:
                self._characters = self._load_characters()
            except (OSError, ValueError):
                self._identity_error = True
        if self._characters is None or kind == "manual":
            records = {}
        elif kind == "character":
            records = self._characters.characters
        else:
            records = self._characters.outfits
        self.identity.clear()
        self.identity.addItem("Choose a Character…" if kind == "character" else
                              "Choose an Outfit…", "")
        for record in sorted(records.values(), key=lambda item: (item.name.casefold(), item.id)):
            label = (self._characters.character_label(record.id) or record.name
                     if kind == "character" else record.name)
            self.identity.addItem(label, record.id, search_terms=record.aliases)
        self.identity.setVisible(kind != "manual")
        if kind == "manual":
            note = "You can add prompts, LoRAs, models and settings without an image source."
        elif self._identity_error:
            note = "Character and Outfit choices could not be read. You can still write a setup manually."
        elif not records:
            note = ("No known Character or Outfit of this type yet. Add one in Tag Encyclopedia, "
                    "or write a setup manually.")
        else:
            note = "Choose an exact identity; image-wide details are suggestions, not confirmed traits."
        self.note.setText(note)
        self._update_continue()

    def _update_continue(self):
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(
            self.source_kind == "manual" or bool(self.identity.currentData())
        )

    @property
    def source_kind(self):
        return "character" if self.character.isChecked() else (
            "outfit" if self.outfit.isChecked() else "manual"
        )

    @property
    def selected_identity(self):
        return self.identity.currentData() if self.source_kind != "manual" else None

    @property
    def characters(self):
        return self._characters
