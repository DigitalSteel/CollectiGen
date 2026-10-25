from __future__ import annotations

import importlib
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .components import bind_status_footer,counted_noun

from core import (
    BUG_REPORT_FEEDBACK_TYPES,
    BUG_REPORT_FIELD_HEADINGS,
    MAX_REPORT_SUMMARY,
    BugReportAttachment,
    BugReportDraft,
    BugReportError,
    FeedbackReportSummary,
    PreparedBugReport,
    discover_feedback_reports,
    export_bug_report_zip,
    export_feedback_archive,
    prepare_bug_report,
    record_report_export,
    save_bug_report,
)
from core.support_bundle import sanitized_log_excerpt, sanitized_settings
from .modal_feedback import FeedbackEnabledDialog


FEEDBACK_FIELD_PROMPTS = {
    "bug": (
        "What happened?",
        "What should have happened?",
        "List the steps that reproduce the problem.",
    ),
    "workflow": (
        "What are you trying to accomplish?",
        "What would make this workflow easier?",
        "Where do you get stuck, lose time, or become unsure?",
    ),
    "feature": (
        "What problem or goal are you trying to address?",
        "What feature or improvement would help?",
        "Give an example of how you would use it.",
    ),
}


def private_development_inbox(app_root: Path) -> Path | None:
    """Return the private-only destination when its excluded helper is present."""
    try:
        module = importlib.import_module("core.development_bug_inbox")
        path = module.development_bug_inbox(app_root)
    except (ImportError, AttributeError, OSError):
        return None
    return Path(path)


class BugReportPrivacyDialog(QDialog):
    """Exact local-save manifest and optional-attachment redaction review."""

    def __init__(
        self,
        prepared: PreparedBugReport,
        *,
        app_root: Path,
        development_inbox: Path | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Review Feedback Privacy")
        self.resize(980, 720)
        self._base = prepared
        self._prepared = prepared
        self._attachments = {item.name: item for item in prepared.attachments}
        self._text_overrides: dict[str, str] = {
            item.name: item.payload.decode("utf-8", errors="replace")
            for item in prepared.attachments
            if item.editable_text
        }
        self.saved_directory: Path | None = None
        self.zip_path: Path | None = None
        self.zip_error = ""
        self.tracking_warning = ""
        self.duplicate_of_report_id = ""
        self.report_id = prepared.report_id
        self.report_summary = str(prepared.report["report"]["summary"])
        self.report_text = prepared.human_readable()
        self._updating_text = False

        layout = QVBoxLayout(self)
        lead = QLabel(
            "Review the exact local report before saving. Optional attachments can be "
            "removed, and sanitized text can be edited further. Nothing is uploaded."
        )
        lead.setWordWrap(True)
        layout.addWidget(lead)

        splitter = QSplitter(Qt.Horizontal)
        attachment_group = QGroupBox("Optional Attachments")
        attachment_layout = QVBoxLayout(attachment_group)
        self.attachment_list = QListWidget()
        self.attachment_list.setAccessibleName("Optional feedback attachments")
        for attachment in prepared.attachments:
            item = QListWidgetItem(attachment.name)
            item.setToolTip(attachment.description)
            item.setData(Qt.UserRole, attachment.name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self.attachment_list.addItem(item)
        attachment_layout.addWidget(self.attachment_list)
        attachment_note = QLabel(
            "Uncheck an attachment to remove it. Text shown on the right is the exact "
            "saved text and may be edited before saving."
        )
        attachment_note.setWordWrap(True)
        attachment_note.setObjectName("mutedLabel")
        attachment_layout.addWidget(attachment_note)
        splitter.addWidget(attachment_group)

        self.preview_tabs = QTabWidget()
        report_page = QWidget()
        report_layout = QVBoxLayout(report_page)
        self.report_preview = QTextBrowser()
        self.report_preview.setReadOnly(True)
        self.report_preview.setAccessibleName("Human-readable report preview")
        report_layout.addWidget(self.report_preview)
        self.preview_tabs.addTab(report_page, "Readable Report")

        technical_page = QWidget()
        technical_layout = QVBoxLayout(technical_page)
        self.technical_preview = QPlainTextEdit()
        self.technical_preview.setReadOnly(True)
        self.technical_preview.setAccessibleName("Exact machine-readable report preview")
        technical_layout.addWidget(self.technical_preview)
        self.preview_tabs.addTab(technical_page, "Technical Details")

        manifest_page = QWidget()
        manifest_layout = QVBoxLayout(manifest_page)
        self.manifest_preview = QPlainTextEdit()
        self.manifest_preview.setReadOnly(True)
        self.manifest_preview.setAccessibleName("Exact privacy manifest")
        manifest_layout.addWidget(self.manifest_preview)
        self.preview_tabs.addTab(manifest_page, "Privacy Manifest")

        attachment_page = QWidget()
        attachment_page_layout = QVBoxLayout(attachment_page)
        self.attachment_detail = QLabel("Select an attachment to inspect it.")
        self.attachment_detail.setWordWrap(True)
        self.attachment_text = QPlainTextEdit()
        self.attachment_text.setAccessibleName("Editable sanitized attachment text")
        self.attachment_image = QLabel()
        self.attachment_image.setAlignment(Qt.AlignCenter)
        self.attachment_image.setMinimumHeight(180)
        self.attachment_image.hide()
        attachment_page_layout.addWidget(self.attachment_detail)
        attachment_page_layout.addWidget(self.attachment_text, 1)
        attachment_page_layout.addWidget(self.attachment_image, 1)
        self.preview_tabs.addTab(attachment_page, "Attachment")
        splitter.addWidget(self.preview_tabs)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        destination_group = QGroupBox("Save and Share")
        destination_layout = QVBoxLayout(destination_group)
        destination_row = QHBoxLayout()
        self._default_destination = Path(app_root) / "feedback"
        self.destination = QLineEdit(str(self._default_destination))
        self.destination.setPlaceholderText("Choose a local report folder")
        self.browse_button = QPushButton("Choose Folder…")
        self.browse_button.clicked.connect(self._browse_destination)
        destination_row.addWidget(self.destination, 1)
        destination_row.addWidget(self.browse_button)
        destination_layout.addLayout(destination_row)
        self.use_development_inbox = QCheckBox("Use Private Development Inbox")
        self._development_inbox = development_inbox
        self.use_development_inbox.setVisible(development_inbox is not None)
        self.use_development_inbox.toggled.connect(self._toggle_development_inbox)
        destination_layout.addWidget(self.use_development_inbox)
        self.development_notice = QLabel(
            "Development only: this saves into the repository’s ignored inbox. "
            "It is intended for maintainers working from a private source checkout "
            "and is not uploaded automatically."
        )
        self.development_notice.setWordWrap(True)
        self.development_notice.setObjectName("mutedLabel")
        self.development_notice.setVisible(development_inbox is not None)
        destination_layout.addWidget(self.development_notice)
        self.create_zip = QCheckBox("Create a Shareable ZIP")
        self.create_zip.setChecked(True)
        self.create_zip.setToolTip(
            "Creates one verified file that is easy to attach. It contains the "
            "readable report, technical details, privacy manifest, and only the "
            "optional attachments that remain checked."
        )
        destination_layout.addWidget(self.create_zip)
        self.share_note = QLabel(
            "The ZIP is created locally beside the report folder. CollectiGen does "
            "not upload it. The completion message can open the public GitHub "
            "feedback form; attach the ZIP there only after this review and only "
            "when every included item is safe to publish."
        )
        self.share_note.setWordWrap(True)
        self.share_note.setObjectName("mutedLabel")
        destination_layout.addWidget(self.share_note)
        layout.addWidget(destination_group)

        self.validation_status = QLabel("")
        self.validation_status.setWordWrap(True)
        self.validation_status.setObjectName("errorLabel")
        self.validation_status.setAccessibleName("Feedback report validation")
        self.validation_status.hide()
        layout.addWidget(self.validation_status)
        self.status = QLabel("", self)
        self.status.setWordWrap(True)
        self.status.setObjectName("errorLabel")
        self.status.setAccessibleName("Feedback report save status")
        bind_status_footer(self, self.status, title="Feedback report")

        buttons = QDialogButtonBox()
        self.copy_button = buttons.addButton("Copy Report", QDialogButtonBox.ActionRole)
        self.save_button = buttons.addButton(
            "Save Reviewed Report", QDialogButtonBox.AcceptRole
        )
        cancel = buttons.addButton(QDialogButtonBox.Cancel)
        self.copy_button.clicked.connect(self._copy_report)
        self.save_button.clicked.connect(self._save)
        cancel.clicked.connect(self.reject)
        layout.addWidget(buttons)

        self.attachment_list.currentItemChanged.connect(self._show_attachment)
        self.attachment_list.itemChanged.connect(lambda _item: self._refresh_previews())
        self.attachment_text.textChanged.connect(self._attachment_text_changed)
        if self.attachment_list.count():
            self.attachment_list.setCurrentRow(0)
        else:
            self.attachment_text.hide()
        self._refresh_previews()

    def _included_attachments(self) -> tuple[BugReportAttachment, ...]:
        values: list[BugReportAttachment] = []
        for index in range(self.attachment_list.count()):
            item = self.attachment_list.item(index)
            if item.checkState() != Qt.Checked:
                continue
            name = str(item.data(Qt.UserRole))
            attachment = self._attachments[name]
            payload = attachment.payload
            if attachment.editable_text:
                payload = self._text_overrides.get(name, "").encode("utf-8")
            values.append(
                BugReportAttachment(
                    name=name,
                    media_type=attachment.media_type,
                    payload=payload,
                    description=attachment.description,
                    editable_text=attachment.editable_text,
                )
            )
        return tuple(values)

    def _rebuild_prepared(self) -> PreparedBugReport:
        report = self._base.report["report"]
        draft = BugReportDraft(
            summary=report["summary"],
            current_context=report["current_context"],
            desired_result=report["desired_result"],
            steps_or_example=report["steps_or_example"],
            feedback_type=report["feedback_type"],
        )
        return prepare_bug_report(
            draft,
            self._base.report["context"],
            self._included_attachments(),
            report_id=self._base.report_id,
            created_at=self._base.created_at,
        )

    def _refresh_previews(self) -> None:
        try:
            self._prepared = self._rebuild_prepared()
        except BugReportError as exc:
            self._set_validation_status(str(exc))
            self.save_button.setEnabled(False)
            return
        self.save_button.setEnabled(True)
        self.validation_status.hide()
        self.report_id = self._prepared.report_id
        self.report_summary = str(self._prepared.report["report"]["summary"])
        self.report_text = self._prepared.human_readable()
        self.report_preview.setMarkdown(self.report_text)
        self.technical_preview.setPlainText(
            json.dumps(
                self._prepared.report,
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        self.manifest_preview.setPlainText(
            json.dumps(
                self._prepared.privacy_manifest,
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )

    def _show_attachment(self, current, _previous=None) -> None:
        self._updating_text = True
        try:
            self.attachment_text.clear()
            self.attachment_image.clear()
            self.attachment_image.hide()
            if current is None:
                self.attachment_detail.setText("Select an attachment to inspect it.")
                self.attachment_text.hide()
                return
            name = str(current.data(Qt.UserRole))
            attachment = self._attachments[name]
            self.attachment_detail.setText(
                f"{attachment.name} • {attachment.media_type} • "
                f"{len(attachment.payload):,} bytes\n{attachment.description}"
            )
            if attachment.editable_text:
                self.attachment_text.show()
                self.attachment_text.setReadOnly(False)
                self.attachment_text.setPlainText(self._text_overrides.get(name, ""))
            elif attachment.media_type == "image/png":
                self.attachment_text.hide()
                pixmap = QPixmap()
                if pixmap.loadFromData(attachment.payload, "PNG"):
                    self.attachment_image.setPixmap(
                        pixmap.scaled(
                            560,
                            360,
                            Qt.KeepAspectRatio,
                            Qt.SmoothTransformation,
                        )
                    )
                    self.attachment_image.show()
            else:
                self.attachment_text.show()
                self.attachment_text.setReadOnly(True)
                self.attachment_text.setPlainText("<binary attachment>")
        finally:
            self._updating_text = False

    def _attachment_text_changed(self) -> None:
        if self._updating_text:
            return
        item = self.attachment_list.currentItem()
        if item is None:
            return
        name = str(item.data(Qt.UserRole))
        attachment = self._attachments[name]
        if not attachment.editable_text:
            return
        self._text_overrides[name] = self.attachment_text.toPlainText()
        self._refresh_previews()

    def _browse_destination(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "Choose Feedback Folder",
            self.destination.text().strip() or str(Path.home()),
        )
        if selected:
            self.use_development_inbox.setChecked(False)
            self.destination.setText(selected)

    def _toggle_development_inbox(self, checked: bool) -> None:
        if checked and self._development_inbox is not None:
            self.destination.setText(str(self._development_inbox))
            self.destination.setEnabled(False)
            self.browse_button.setEnabled(False)
        else:
            self.destination.setText(str(self._default_destination))
            self.destination.setEnabled(True)
            self.browse_button.setEnabled(True)

    def _copy_report(self) -> None:
        self._refresh_previews()
        QApplication.clipboard().setText(self._prepared.human_readable())
        self._set_operation_status(
            "Human-readable report copied. Nothing was uploaded.", error=False
        )

    def _save(self) -> None:
        self._refresh_previews()
        destination_text = self.destination.text().strip()
        if not destination_text:
            self._set_validation_status("Choose a local report folder before saving.")
            return
        destination = Path(destination_text)
        try:
            saved = save_bug_report(self._prepared, destination)
        except (BugReportError, OSError) as exc:
            self._set_operation_status(
                "The report was not saved. Choose another writable folder or try again.\n"
                + str(exc),
                error=True,
            )
            return
        self.saved_directory = saved.directory
        self.duplicate_of_report_id = saved.duplicate_of_report_id
        if self.create_zip.isChecked():
            try:
                self.zip_path = export_bug_report_zip(
                    saved.directory,
                    destination
                    / f"CollectiGen_Report_{saved.directory.name}.zip",
                )
                package_sha256 = hashlib.sha256(
                    self.zip_path.read_bytes()
                ).hexdigest()
                try:
                    record_report_export(
                        saved.directory,
                        package_id=f"CFR-{package_sha256[:16].upper()}",
                        package_sha256=package_sha256,
                        package_kind="individual",
                    )
                except (BugReportError, OSError) as exc:
                    self.tracking_warning = str(exc)
            except (BugReportError, OSError) as exc:
                self.zip_error = str(exc)
        self.accept()

    def _set_validation_status(self, text: str) -> None:
        self.status.setText(text)
        self.status.hide()
        self.validation_status.setText(text)
        self.validation_status.show()

    def _set_operation_status(self, text: str, *, error: bool) -> None:
        self.validation_status.hide()
        self.status.setObjectName("errorLabel" if error else "mutedLabel")
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)
        self.status.setText(text)
        self.status.show()


class FeedbackArchiveDialog(FeedbackEnabledDialog):
    feedback_workflow = "Collected feedback archive"

    """Select local reports and create one verified, non-destructive archive."""

    def __init__(self, feedback_directory: str | Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export Collected Feedback")
        self.resize(900, 660)
        self.feedback_directory = Path(feedback_directory)
        self.archive_path: Path | None = None
        self.tracking_warning = ""
        self._reports, self._invalid = discover_feedback_reports(
            self.feedback_directory
        )
        self._reports_by_id = {item.report_id: item for item in self._reports}

        layout = QVBoxLayout(self)
        lead = QLabel(
            "Choose the reviewed reports to include in one shareable ZIP. "
            "Original report folders are preserved, and nothing is uploaded."
        )
        lead.setWordWrap(True)
        layout.addWidget(lead)

        selection_group = QGroupBox("Collected Feedback Reports")
        selection_layout = QVBoxLayout(selection_group)
        selection_actions = QHBoxLayout()
        select_all = QPushButton("Select All")
        clear_all = QPushButton("Clear All")
        select_all.clicked.connect(lambda: self._set_all(Qt.Checked))
        clear_all.clicked.connect(lambda: self._set_all(Qt.Unchecked))
        selection_actions.addWidget(select_all)
        selection_actions.addWidget(clear_all)
        selection_actions.addStretch(1)
        selection_layout.addLayout(selection_actions)
        self.report_list = QListWidget()
        self.report_list.setAccessibleName("Feedback reports selected for export")
        for report in self._reports:
            label = BUG_REPORT_FEEDBACK_TYPES.get(
                report.feedback_type, report.feedback_type
            )
            item = QListWidgetItem(
                f"{report.report_id} • {label} • {report.summary}"
            )
            item.setData(Qt.UserRole, report.report_id)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(
                Qt.Unchecked if report.export_count else Qt.Checked
            )
            if report.export_count:
                item.setText(
                    f"{item.text()} • Previously exported {report.export_count} time(s)"
                )
            item.setToolTip(
                f"Created: {report.created_at or 'Unknown'}\n"
                f"Optional attachments: {report.attachment_count}\n"
                f"Previous exports: {report.export_count}"
            )
            self.report_list.addItem(item)
        selection_layout.addWidget(self.report_list, 1)
        if not self._reports:
            empty = QLabel(
                f"No valid CBR report folders were found in {self.feedback_directory}."
            )
            empty.setWordWrap(True)
            empty.setObjectName("mutedLabel")
            selection_layout.addWidget(empty)
        if self._invalid:
            invalid = QLabel(
                f"{counted_noun(len(self._invalid),'report folder')} could not be read and will "
                "not be exported. Creating the archive will not change them."
            )
            invalid.setWordWrap(True)
            invalid.setObjectName("errorLabel")
            invalid.setToolTip("\n".join(self._invalid))
            selection_layout.addWidget(invalid)
        layout.addWidget(selection_group, 1)

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setAccessibleName("Feedback archive selection manifest")
        self.preview.setMaximumHeight(150)
        layout.addWidget(self.preview)

        output_group = QGroupBox("Archive Destination")
        output_layout = QHBoxLayout(output_group)
        default_name = (
            "CollectiGen_Feedback_Archive_"
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        )
        self.destination = QLineEdit(
            str(self.feedback_directory / default_name)
        )
        choose = QPushButton("Choose File…")
        choose.clicked.connect(self._browse)
        output_layout.addWidget(self.destination, 1)
        output_layout.addWidget(choose)
        layout.addWidget(output_group)

        self.validation_status = QLabel("")
        self.validation_status.setWordWrap(True)
        self.validation_status.setObjectName("errorLabel")
        self.validation_status.setAccessibleName("Feedback archive validation")
        self.validation_status.hide()
        layout.addWidget(self.validation_status)
        self.status = QLabel("", self)
        self.status.setWordWrap(True)
        self.status.setObjectName("errorLabel")
        self.status.setAccessibleName("Feedback archive export status")
        bind_status_footer(self, self.status, title="Feedback archive")

        buttons = QDialogButtonBox()
        self.export_button = buttons.addButton(
            "Create Reviewed Archive", QDialogButtonBox.AcceptRole
        )
        cancel = buttons.addButton(QDialogButtonBox.Cancel)
        self.export_button.clicked.connect(self._export)
        cancel.clicked.connect(self.reject)
        layout.addWidget(buttons)
        self.report_list.itemChanged.connect(lambda _item: self._refresh())
        self._refresh()

    def _selected(self) -> tuple[FeedbackReportSummary, ...]:
        selected: list[FeedbackReportSummary] = []
        for index in range(self.report_list.count()):
            item = self.report_list.item(index)
            if item.checkState() == Qt.Checked:
                selected.append(self._reports_by_id[str(item.data(Qt.UserRole))])
        return tuple(selected)

    def _set_all(self, state: Qt.CheckState) -> None:
        for index in range(self.report_list.count()):
            self.report_list.item(index).setCheckState(state)
        self._refresh()

    def _refresh(self) -> None:
        selected = self._selected()
        manifest = {
            "selected_report_count": len(selected),
            "reports": [
                {
                    "report_id": item.report_id,
                    "feedback_type": item.feedback_type,
                    "optional_attachments": item.attachment_count,
                    "previous_exports": item.export_count,
                }
                for item in selected
            ],
            "excluded": [
                "unselected or unreadable report folders",
                "maintainer tracking.json files",
                "existing individual or bulk ZIP exports",
            ],
        }
        self.preview.setPlainText(
            json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True)
        )
        self.export_button.setEnabled(bool(selected))

    def _browse(self) -> None:
        selected, _filter = QFileDialog.getSaveFileName(
            self,
            "Save Feedback Archive",
            self.destination.text().strip(),
            "ZIP archives (*.zip)",
        )
        if selected:
            path = Path(selected)
            if path.suffix.casefold() != ".zip":
                path = path.with_suffix(".zip")
            self.destination.setText(str(path))

    def _export(self) -> None:
        selected = self._selected()
        destination = self.destination.text().strip()
        if not destination:
            self._set_validation_status("Choose a ZIP destination.")
            return
        try:
            result = export_feedback_archive(
                [item.directory for item in selected],
                destination,
            )
        except (BugReportError, OSError) as exc:
            self.archive_path = None
            self._set_operation_status(
                "The feedback archive was not created. No original report was "
                f"changed.\n{exc}",
            )
            return
        self.archive_path = result.path
        tracking_errors: list[str] = []
        for item in selected:
            try:
                record_report_export(
                    item.directory,
                    package_id=result.archive_id,
                    package_sha256=result.sha256,
                    package_kind="bulk",
                )
            except (BugReportError, OSError) as exc:
                tracking_errors.append(f"{item.report_id}: {exc}")
        if tracking_errors:
            self.tracking_warning = "\n".join(tracking_errors)
        self.accept()

    def _set_validation_status(self, text: str) -> None:
        self.status.setText(text)
        self.status.hide()
        self.validation_status.setText(text)
        self.validation_status.show()

    def _set_operation_status(self, text: str) -> None:
        self.validation_status.hide()
        self.status.setText(text)
        self.status.show()


class BugReportDialog(QDialog):
    """Shared report form used by Help and error/recovery surfaces."""

    def __init__(
        self,
        *,
        app_root: Path,
        context_provider: Callable[[bool], Mapping[str, Any]],
        settings_provider: Callable[[], Mapping[str, Any]],
        screenshot_provider: Callable[[], bytes],
        logs_dir: Path,
        parent=None,
        summary: str = "",
        current_context: str = "",
        feedback_type: str = "bug",
        origin_context: str = "",
        screenshot_subject: str = "Current Window",
        screenshot_attachment_name: str = "current_window.png",
    ):
        super().__init__(parent)
        self.setWindowTitle("Feedback Report")
        self.resize(820, 690)
        self.app_root = Path(app_root)
        self.context_provider = context_provider
        self.settings_provider = settings_provider
        self.screenshot_provider = screenshot_provider
        self.screenshot_attachment_name = screenshot_attachment_name
        self.logs_dir = Path(logs_dir)
        self.saved_directory: Path | None = None
        self.zip_path: Path | None = None
        self.zip_error = ""
        self.tracking_warning = ""
        self.duplicate_of_report_id = ""
        self.report_id = ""
        self.report_summary = ""
        self.report_text = ""

        layout = QVBoxLayout(self)
        lead = QLabel(
            "Describe the problem, then review the exact privacy manifest before "
            "saving. Reports stay local and are never uploaded automatically."
        )
        lead.setWordWrap(True)
        layout.addWidget(lead)
        if origin_context:
            origin = QLabel(f"Reporting from: {origin_context}")
            origin.setObjectName("mutedLabel")
            origin.setAccessibleName("Feedback origin")
            origin.setWordWrap(True)
            layout.addWidget(origin)

        form = QFormLayout()
        self.summary = QLineEdit(summary)
        self.summary.setMaxLength(MAX_REPORT_SUMMARY)
        self.summary.setAccessibleName("Feedback summary")
        self.summary.setToolTip(
            f"Up to {MAX_REPORT_SUMMARY} characters. A shorter, safe version is "
            "included in the report folder and ZIP names."
        )
        self.feedback_type = QComboBox()
        self.feedback_type.setAccessibleName("Feedback type")
        for value, label in BUG_REPORT_FEEDBACK_TYPES.items():
            self.feedback_type.addItem(label, value)
        if feedback_type:
            index = self.feedback_type.findData(feedback_type)
            if index >= 0:
                self.feedback_type.setCurrentIndex(index)
        self.current_context = QPlainTextEdit(current_context)
        self.desired_result = QPlainTextEdit()
        self.steps_or_example = QPlainTextEdit()
        for editor in (
            self.current_context,
            self.desired_result,
            self.steps_or_example,
        ):
            editor.setMaximumHeight(115)
        form.addRow(f"Summary ({MAX_REPORT_SUMMARY} characters max)", self.summary)
        form.addRow("Feedback type", self.feedback_type)
        self.current_context_label = QLabel()
        self.desired_result_label = QLabel()
        self.steps_or_example_label = QLabel()
        form.addRow(self.current_context_label, self.current_context)
        form.addRow(self.desired_result_label, self.desired_result)
        form.addRow(self.steps_or_example_label, self.steps_or_example)
        layout.addLayout(form)
        self.feedback_type.currentIndexChanged.connect(
            self._update_feedback_prompts
        )
        self._update_feedback_prompts()

        optional = QGroupBox("Optional Diagnostic Details")
        optional_layout = QVBoxLayout(optional)
        self.include_screenshot = QCheckBox(
            f"Include {screenshot_subject} Screenshot"
        )
        self.include_selected_image = QCheckBox("Include Selected Image Identity")
        self.include_settings = QCheckBox("Include Sanitized Settings")
        self.include_logs = QCheckBox("Include Sanitized Recent Logs")
        for checkbox in (
            self.include_screenshot,
            self.include_selected_image,
            self.include_settings,
            self.include_logs,
        ):
            optional_layout.addWidget(checkbox)
        optional_note = QLabel(
            "All optional details start excluded. A screenshot can contain visible "
            "image pixels or private text. Before public submission, inspect the exact "
            "screenshot and remove it unless every Questionable or Explicit image stays "
            "fully blurred; opening the image editor may reveal its preview. "
            "Selected image identity contains its filename and a one-way path fingerprint, "
            "never its image bytes or library path. "
            "You can remove or further redact every optional text attachment in the next step."
        )
        optional_note.setWordWrap(True)
        optional_note.setObjectName("mutedLabel")
        optional_layout.addWidget(optional_note)
        layout.addWidget(optional)

        self.validation_status = QLabel("")
        self.validation_status.setWordWrap(True)
        self.validation_status.setObjectName("errorLabel")
        self.validation_status.setAccessibleName("Feedback form validation")
        self.validation_status.hide()
        layout.addWidget(self.validation_status)
        self.status = QLabel("", self)
        self.status.setWordWrap(True)
        self.status.setObjectName("errorLabel")
        self.status.setAccessibleName("Feedback report outcome")
        bind_status_footer(self, self.status, title="Feedback report")
        buttons = QDialogButtonBox()
        review = buttons.addButton("Review and Save…", QDialogButtonBox.AcceptRole)
        cancel = buttons.addButton(QDialogButtonBox.Cancel)
        review.clicked.connect(self._review)
        cancel.clicked.connect(self.reject)
        layout.addWidget(buttons)

    def _update_feedback_prompts(self) -> None:
        feedback_type = str(self.feedback_type.currentData() or "bug")
        headings = BUG_REPORT_FIELD_HEADINGS.get(
            feedback_type, BUG_REPORT_FIELD_HEADINGS["bug"]
        )
        prompts = FEEDBACK_FIELD_PROMPTS.get(
            feedback_type, FEEDBACK_FIELD_PROMPTS["bug"]
        )
        fields = (
            (self.current_context_label, self.current_context),
            (self.desired_result_label, self.desired_result),
            (self.steps_or_example_label, self.steps_or_example),
        )
        for (label, editor), heading, prompt in zip(fields, headings, prompts):
            shown_heading = (
                f"{heading} (optional)"
                if editor is self.steps_or_example
                else heading
            )
            label.setText(shown_heading)
            editor.setAccessibleName(shown_heading)
            editor.setPlaceholderText(prompt)

    def _draft(self) -> BugReportDraft:
        return BugReportDraft(
            summary=self.summary.text(),
            current_context=self.current_context.toPlainText(),
            desired_result=self.desired_result.toPlainText(),
            steps_or_example=self.steps_or_example.toPlainText(),
            feedback_type=str(self.feedback_type.currentData() or ""),
        )

    def _optional_attachments(self) -> tuple[BugReportAttachment, ...]:
        values: list[BugReportAttachment] = []
        if self.include_settings.isChecked():
            payload = sanitized_settings(dict(self.settings_provider()))
            values.append(
                BugReportAttachment(
                    "settings_sanitized.json",
                    "application/json",
                    (
                        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
                        + "\n"
                    ).encode("utf-8"),
                    "Allowlisted presentation and capability settings; paths, prompts, tags, and recent locations are excluded.",
                    editable_text=True,
                )
            )
        if self.include_logs.isChecked():
            logs = sanitized_log_excerpt(self.logs_dir, app_root=self.app_root)
            values.append(
                BugReportAttachment(
                    "recent_logs_sanitized.txt",
                    "text/plain",
                    logs.encode("utf-8"),
                    "Bounded recent logs with common account, email, and filesystem paths redacted.",
                    editable_text=True,
                )
            )
        if self.include_screenshot.isChecked():
            screenshot = bytes(self.screenshot_provider() or b"")
            if not screenshot:
                raise BugReportError("The current-window screenshot could not be captured.")
            values.append(
                BugReportAttachment(
                    self.screenshot_attachment_name,
                    "image/png",
                    screenshot,
                    "Current CollectiGen window; may contain visible images, unblurred "
                    "Questionable or Explicit content, or private text. Inspect this exact "
                    "screenshot before public submission.",
                    editable_text=False,
                )
            )
        return tuple(values)

    def _review(self) -> None:
        try:
            prepared = prepare_bug_report(
                self._draft(),
                self.context_provider(self.include_selected_image.isChecked()),
                self._optional_attachments(),
            )
        except (BugReportError, OSError) as exc:
            self._set_validation_status(str(exc))
            return
        privacy = BugReportPrivacyDialog(
            prepared,
            app_root=self.app_root,
            development_inbox=private_development_inbox(self.app_root),
            parent=self,
        )
        if privacy.exec() != QDialog.Accepted:
            self._set_operation_status("No report was saved.")
            return
        self.saved_directory = privacy.saved_directory
        self.zip_path = privacy.zip_path
        self.zip_error = privacy.zip_error
        self.tracking_warning = privacy.tracking_warning
        self.duplicate_of_report_id = privacy.duplicate_of_report_id
        self.report_id = privacy.report_id
        self.report_summary = privacy.report_summary
        self.report_text = privacy.report_text
        self.accept()

    def _set_validation_status(self, text: str) -> None:
        self.status.setText(text)
        self.status.hide()
        self.validation_status.setText(text)
        self.validation_status.show()

    def _set_operation_status(self, text: str) -> None:
        self.validation_status.hide()
        self.status.setText(text)
        self.status.show()
