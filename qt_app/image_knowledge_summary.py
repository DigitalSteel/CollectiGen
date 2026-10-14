from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QGroupBox, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout,
    QScrollArea, QSizePolicy, QWidget,
)


class ImageKnowledgeSummaryWidget(QWidget):
    """Compact, read-only Image Knowledge summary with one canonical edit route."""

    edit_requested = Signal()

    TAG_PREVIEW_LIMIT = 12
    DESCRIPTION_PREVIEW_LIMIT = 2
    TAG_PREVIEW_CHARACTER_LIMIT = 320
    DESCRIPTION_PREVIEW_CHARACTER_LIMIT = 360
    EXPANDED_DETAIL_HEIGHT = 120

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._image_key = ""
        self._tags: tuple[str, ...] = ()
        self._descriptions: tuple[str, ...] = ()
        self._characters: tuple[str, ...] = ()
        self._source_overview = ""
        self._source_details = ""
        self._issues: tuple[str, ...] = ()
        self._show_all_tags = False
        self._show_all_descriptions = False
        self._show_source_details = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self._content = QWidget()
        self.scroll.setWidget(self._content)
        layout.addWidget(self.scroll)
        layout = QVBoxLayout(self._content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.status_label = QLabel()
        self.status_label.setTextFormat(Qt.PlainText)
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.status_label.setAccessibleName("Image Knowledge current state")
        layout.addWidget(self.status_label)

        self.edit_button = QPushButton("Edit Image Data…")
        self.edit_button.setProperty("primary", True)
        self.edit_button.setAccessibleName("Edit Image Data")
        self.edit_button.clicked.connect(self.edit_requested)
        layout.addWidget(self.edit_button)

        self.issues_label = QLabel()
        self.issues_label.setTextFormat(Qt.PlainText)
        self.issues_label.setWordWrap(True)
        self.issues_label.setObjectName("warningLabel")
        self.issues_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.issues_label.setAccessibleName("Image Knowledge issues")
        layout.addWidget(self.issues_label)

        self.tags_group, self.tags_preview, self.tags_disclosure, self.tags_expanded = self._preview_group(
            "Confirmed Tags", "Tags", self._toggle_tags,
        )
        layout.addWidget(self.tags_group)
        self.descriptions_group, self.descriptions_preview, self.descriptions_disclosure, self.descriptions_expanded = self._preview_group(
            "Confirmed Descriptions", "Descriptions", self._toggle_descriptions,
        )
        layout.addWidget(self.descriptions_group)

        self.characters_group = QGroupBox("Characters (0)")
        characters_layout = QVBoxLayout(self.characters_group)
        self.characters_label = QLabel("No confirmed characters")
        self.characters_label.setTextFormat(Qt.PlainText)
        self.characters_label.setWordWrap(True)
        self.characters_label.setAccessibleName("Confirmed characters")
        self.characters_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        characters_layout.addWidget(self.characters_label)
        layout.addWidget(self.characters_group)

        self.source_group = QGroupBox("Source Availability")
        source_layout = QVBoxLayout(self.source_group)
        self.source_overview_label = QLabel()
        self.source_overview_label.setTextFormat(Qt.PlainText)
        self.source_overview_label.setWordWrap(True)
        self.source_overview_label.setObjectName("mutedLabel")
        self.source_overview_label.setAccessibleName("Image source availability")
        self.source_overview_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        source_layout.addWidget(self.source_overview_label)
        layout.addWidget(self.source_group)

        self.source_details_group = QGroupBox("Source Details")
        details_layout = QVBoxLayout(self.source_details_group)
        self.source_details_button = QPushButton()
        self.source_details_button.setAccessibleName("Show source details")
        self.source_details_button.clicked.connect(self._toggle_source_details)
        details_layout.addWidget(self.source_details_button)
        self.source_details_text = QPlainTextEdit()
        self.source_details_text.setReadOnly(True)
        self.source_details_text.setMaximumHeight(110)
        self.source_details_text.setAccessibleName("Exact image source details")
        details_layout.addWidget(self.source_details_text)
        layout.addWidget(self.source_details_group)
        layout.addStretch(1)
        # Long unbroken facts must not impose their full word width on the
        # inspector. Full exact text remains available in the disclosures.
        for label in self._content.findChildren(QLabel):
            label.setMinimumWidth(0)
            label.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)

        self.setPlainText("Select an image to view its Image Knowledge summary.")

    @staticmethod
    def _values(values: Iterable[object] | object | None) -> tuple[str, ...]:
        if values is None:
            return ()
        if isinstance(values, str):
            return (values,) if values else ()
        return tuple(str(value) for value in values if str(value))

    def _preview_group(self, title: str, kind: str, callback):
        group = QGroupBox(f"{title} (0)")
        layout = QVBoxLayout(group)
        preview = QLabel()
        preview.setTextFormat(Qt.PlainText)
        preview.setWordWrap(True)
        preview.setTextInteractionFlags(Qt.TextSelectableByMouse)
        preview.setAccessibleName(title)
        layout.addWidget(preview)
        expanded = QPlainTextEdit()
        expanded.setReadOnly(True)
        expanded.setMaximumHeight(self.EXPANDED_DETAIL_HEIGHT)
        expanded.setAccessibleName(f"All {kind.lower()}")
        layout.addWidget(expanded)
        disclosure = QPushButton()
        disclosure.setAccessibleName(f"Show all {kind.lower()}")
        disclosure.clicked.connect(callback)
        layout.addWidget(disclosure)
        return group, preview, disclosure, expanded

    def setPlainText(self, message: str) -> None:
        """Show a loading, held, or error state without retaining another image's data."""
        self._image_key = ""
        self._tags = self._descriptions = self._characters = ()
        self._source_overview = self._source_details = ""
        self._issues = ()
        self._show_all_tags = self._show_all_descriptions = self._show_source_details = False
        self.status_label.setText(str(message))
        self.status_label.show()
        self.edit_button.hide()
        self.issues_label.hide()
        self.issues_label.clear()
        for label in (self.tags_preview,self.descriptions_preview,
                      self.characters_label,self.source_overview_label):
            label.clear()
        for detail in (self.tags_expanded,self.descriptions_expanded,self.source_details_text):
            detail.clear()
        for group in (
            self.tags_group, self.descriptions_group, self.characters_group,
            self.source_group, self.source_details_group,
        ):
            group.hide()

    def set_summary(
        self, *, image_key: object, status: str, tags=(), descriptions=(),
        characters=(), source_overview: str = "", source_details=(), issues=(),
    ) -> None:
        key = str(image_key or "")
        same_image = bool(key and key == self._image_key)
        if not same_image:
            self._show_all_tags = self._show_all_descriptions = False
            self._show_source_details = False
        self._image_key = key
        self._tags = self._values(tags)
        self._descriptions = self._values(descriptions)
        self._characters = self._values(characters)
        self._source_overview = str(source_overview or "")
        self._source_details = "\n".join(self._values(source_details))
        self._issues = self._values(issues)
        status_text=str(status or "").strip()
        self.status_label.setText(status_text)
        self.status_label.setVisible(bool(status_text))
        self.edit_button.show()
        self.tags_group.show()
        self.descriptions_group.show()
        self.characters_group.show()
        self.source_group.show()
        self.source_details_group.show()
        self.issues_label.setVisible(bool(self._issues))
        self.issues_label.setText("\n".join(f"• {issue}" for issue in self._issues))
        self._render()

    def _toggle_tags(self) -> None:
        self._show_all_tags = not self._show_all_tags
        self._render_tags()

    def _toggle_descriptions(self) -> None:
        self._show_all_descriptions = not self._show_all_descriptions
        self._render_descriptions()

    def _toggle_source_details(self) -> None:
        self._show_source_details = not self._show_source_details
        self._render_source_details()

    def _render(self) -> None:
        self._render_tags()
        self._render_descriptions()
        self.characters_group.setTitle(f"Characters ({len(self._characters):,})")
        self.characters_label.setText(
            ", ".join(self._characters) if self._characters else "No confirmed characters"
        )
        self.source_overview_label.setText(self._source_overview or "No sources inspected")
        self._render_source_details()

    def _render_tags(self) -> None:
        values, truncated = self._preview_values(
            self._tags, self.TAG_PREVIEW_LIMIT, self.TAG_PREVIEW_CHARACTER_LIMIT,
            separator="  •  ",
        )
        self.tags_group.setTitle(f"Confirmed Tags ({len(self._tags):,})")
        if not truncated:
            self._show_all_tags=False
        self.tags_preview.setText("  •  ".join(values) if values else "No confirmed Tags")
        self.tags_preview.setVisible(not self._show_all_tags)
        self.tags_expanded.setPlainText("\n".join(self._tags))
        self.tags_expanded.setVisible(self._show_all_tags)
        self.tags_disclosure.setVisible(truncated)
        self.tags_disclosure.setText(
            f"Show Less Tags ({len(self._tags):,})" if self._show_all_tags
            else f"Show All Tags ({len(self._tags):,})"
        )
        self.tags_disclosure.setAccessibleName(self.tags_disclosure.text())

    def _render_descriptions(self) -> None:
        values, truncated = self._preview_values(
            self._descriptions, self.DESCRIPTION_PREVIEW_LIMIT,
            self.DESCRIPTION_PREVIEW_CHARACTER_LIMIT, separator="\n\n",
        )
        self.descriptions_group.setTitle(f"Confirmed Descriptions ({len(self._descriptions):,})")
        if not truncated:
            self._show_all_descriptions=False
        self.descriptions_preview.setText(
            "\n\n".join(f"• {value}" for value in values)
            if values else "No confirmed Descriptions"
        )
        self.descriptions_preview.setVisible(not self._show_all_descriptions)
        self.descriptions_expanded.setPlainText("\n\n".join(self._descriptions))
        self.descriptions_expanded.setVisible(self._show_all_descriptions)
        self.descriptions_disclosure.setVisible(truncated)
        self.descriptions_disclosure.setText(
            f"Show Less Descriptions ({len(self._descriptions):,})" if self._show_all_descriptions
            else f"Show All Descriptions ({len(self._descriptions):,})"
        )
        self.descriptions_disclosure.setAccessibleName(self.descriptions_disclosure.text())

    @staticmethod
    def _preview_values(values: tuple[str, ...], unit_limit: int,
                        character_limit: int, *, separator: str) -> tuple[tuple[str, ...], bool]:
        preview = []
        used = 0
        for value in values:
            additional = len(value) + (len(separator) if preview else 0)
            if len(preview) >= unit_limit or used + additional > character_limit:
                if not preview and value:
                    preview.append(value[:max(0, character_limit - 1)] + "…")
                return tuple(preview), True
            preview.append(value)
            used += additional
        return tuple(preview), False

    def _render_source_details(self) -> None:
        has_details = bool(self._source_details)
        self.source_details_group.setVisible(has_details)
        self.source_details_button.setText(
            "Hide Source Details" if self._show_source_details else "Show Source Details"
        )
        self.source_details_button.setAccessibleName(self.source_details_button.text())
        self.source_details_text.setPlainText(self._source_details)
        self.source_details_text.setVisible(has_details and self._show_source_details)

    def toPlainText(self) -> str:
        """Return all summary text, including intentionally collapsed detail."""
        values = [self.status_label.text()] if self.status_label.text() else []
        if self._image_key:
            values.extend((
                "Confirmed Tags ({}): {}".format(len(self._tags), ", ".join(self._tags) or "None"),
                "Confirmed Descriptions ({}): {}".format(
                    len(self._descriptions), " | ".join(self._descriptions) or "None",
                ),
                "Characters ({}): {}".format(len(self._characters), ", ".join(self._characters) or "None"),
                f"Source Availability: {self._source_overview or 'No sources inspected'}",
                f"Source Details: {self._source_details or 'None'}",
            ))
            if self._issues:
                values.append("Issues: " + " | ".join(self._issues))
        return "\n".join(values)
