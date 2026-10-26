"""Standalone prompt authoring; source analysis is explicit and never target-driven."""
from copy import deepcopy
import json
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QTabWidget, QTextEdit, QVBoxLayout, QWidget)

from core.generation_prompt_profiles import PROFILES, render_prompt
from shiboken6 import isValid


class OrganizerPromptDialog(QDialog):
    def __init__(self, *, source, checkpoints=(), settings=None, jobs=None,
                 source_current_provider=None, suggestion_service=None, task_factory=None,
                 export_writer=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Suggest Prompt from Image")
        self.resize(980, 760)
        self.setMinimumSize(720, 560)
        self.source = deepcopy(source)
        self.settings, self.jobs = settings, jobs
        self.source_current_provider = source_current_provider
        self.suggestion_service, self.task_factory = suggestion_service, task_factory
        self.export_writer = export_writer
        self._task = None
        self._closing = False
        self._draining_tasks = []
        self._edits, self._key, self._loading = {}, "manual", False
        self.draft = None
        self.analysis = deepcopy(source["analysis"])
        self._candidates = []
        self._initial_analysis = deepcopy(self.analysis)
        self._initial_review_texts = []
        self._review_index = -1
        layout = QVBoxLayout(self)
        title = QLabel("Create an editable prompt from this image")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        goal = QLabel("Choose a checkpoint and writing guide, then review saved text or request local suggestions. "
                      "Copy or export your draft; nothing generates or changes Image Knowledge.")
        goal.setWordWrap(True)
        layout.addWidget(goal)
        form = QFormLayout()
        self.checkpoint = QComboBox()
        self.checkpoint.addItem("Manual draft — no registered checkpoint selected", None)
        for row in checkpoints:
            self.checkpoint.addItem(row["label"], deepcopy(row))
        self.profile = QComboBox()
        self.profile.addItem("Keep my text — no model-specific writing guide", None)
        labels = {"sdxl-base-1.0": "SDXL Base 1.0", "anima-base": "Anima Base",
                  "anima-aesthetic": "Anima Aesthetic", "krea2-raw": "Krea 2 RAW",
                  "krea2-turbo": "Krea 2 Turbo", "zimage-base": "Z-Image Base", "zimage-turbo": "Z-Image Turbo"}
        for profile in PROFILES:
            self.profile.addItem(labels[profile.id], profile.id)
        form.addRow("Target checkpoint", self.checkpoint)
        form.addRow("Writing guide", self.profile)
        layout.addLayout(form)
        self.policy = QLabel("Manual authoring. Model and adapter compatibility are not assessed.")
        self.policy.setWordWrap(True)
        layout.addWidget(self.policy)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        review = QWidget()
        review_layout = QVBoxLayout(review)
        row = QHBoxLayout()
        self.goal = QComboBox()
        for label, kinds in [("General description", ("other",)), ("Character traits", ("character",)),
                              ("Scene", ("scene",)), ("Pose / action", ("pose",)), ("Style", ("style",))]:
            self.goal.addItem(label, kinds)
        self.analyze = QPushButton("Suggest Text from Image…")
        self.cancel_analysis = QPushButton("Cancel Analysis")
        self.cancel_analysis.setEnabled(False)
        row.addWidget(self.goal)
        row.addWidget(self.analyze)
        row.addWidget(self.cancel_analysis)
        review_layout.addLayout(row)
        self.candidate = QComboBox()
        review_layout.addWidget(self.candidate)
        self.provenance = QLabel("Saved knowledge remains separate from uncertain local suggestions.")
        self.provenance.setWordWrap(True)
        review_layout.addWidget(self.provenance)
        self.review_text = QTextEdit()
        review_layout.addWidget(self.review_text, 1)
        self.use_reviewed = QPushButton("Use Reviewed Text in Prompt")
        review_layout.addWidget(self.use_reviewed, 0, Qt.AlignRight)
        self.tabs.addTab(review, "Review image text")
        output = QWidget()
        output_layout = QVBoxLayout(output)
        output_layout.addWidget(QLabel("Positive prompt — editable"))
        self.positive = QTextEdit()
        output_layout.addWidget(self.positive, 1)
        output_layout.addWidget(QLabel("Negative prompt — editable; target support may be unknown"))
        self.negative = QTextEdit()
        output_layout.addWidget(self.negative, 1)
        transfers = QHBoxLayout()
        self.copy_positive = QPushButton("Copy Positive")
        self.copy_negative = QPushButton("Copy Negative")
        self.export_text = QPushButton("Export Plain Text…")
        for button in (self.copy_positive, self.copy_negative, self.export_text):transfers.addWidget(button)
        output_layout.addLayout(transfers)
        self.tabs.addTab(output, "Prompt draft")
        original = QWidget()
        original_layout = QVBoxLayout(original)
        original_layout.addWidget(QLabel("Saved generation records — read-only, separate from this draft"))
        self.original = QTextEdit()
        self.original.setReadOnly(True)
        self.original.setPlainText(json.dumps(source.get("recorded_metadata"), ensure_ascii=False, indent=2))
        original_layout.addWidget(self.original)
        self.tabs.addTab(original, "Saved generation records")
        details = QTextEdit()
        details.setReadOnly(True)
        self.details = details
        self.tabs.addTab(details, "Evidence")
        self.status = QLabel("Review saved image text or request suggestions. Manual editing is always available.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        # Enter belongs to the focused editor/selector, never an implicit action.
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)
        self.checkpoint.currentIndexChanged.connect(self._target_changed)
        self.profile.currentIndexChanged.connect(self._target_changed)
        self.positive.textChanged.connect(self._remember)
        self.negative.textChanged.connect(self._remember)
        self.candidate.currentIndexChanged.connect(self._show_candidate)
        self.analyze.clicked.connect(self._analyze)
        self.cancel_analysis.clicked.connect(self._cancel)
        self.use_reviewed.clicked.connect(self._use_reviewed)
        self.copy_positive.clicked.connect(lambda: self._copy("positive"))
        self.copy_negative.clicked.connect(lambda: self._copy("negative"))
        self.export_text.clicked.connect(self._export)
        self.analyze.setEnabled(jobs is not None and settings is not None and bool(source.get("path")))
        for part in self.analysis["parts"]:
            self._add_candidate(dict(part, source_label=part.get("source_label", "Saved reviewed Image Knowledge"),
                                     model_id=part.get("model_id", "saved-image-knowledge"),
                                     image_identity=deepcopy(source["identity"])))
        self._initial_review_texts = [part["text"] for part in self._candidates]
        self._drain_timer = QTimer(self)
        self._drain_timer.setInterval(100)
        self._drain_timer.timeout.connect(self._poll_draining)
        owner = self.parentWidget()
        if owner is not None:
            owner.destroyed.connect(self._owner_destroyed)
        self.destroyed.connect(lambda *_: self._cancel_on_destroy())
        self._target_changed()
        if source.get("registry_issue"):
            self.status.setText("Registered targets unavailable: " + source["registry_issue"] + ". Continue drafting manually.")

    def _remember(self):
        if not self._loading:
            self._edits[self._key] = {"positive": self.positive.toPlainText(), "negative": self.negative.toPlainText()}

    def _target_changed(self):
        self._remember()
        selected, profile_id = self.checkpoint.currentData(), self.profile.currentData()
        self._key = f"{selected['checkpoint_sha256'] if selected else 'manual'}:{profile_id or 'manual'}"
        self.draft = None
        generated = ", ".join(part["text"] for part in self.analysis["parts"] if part["selected"])
        if profile_id:
            profile = next(p for p in PROFILES if p.id == profile_id)
            # Explicit advisory choice, never inferred registry compatibility or runtime admission.
            target = dict(selected or {}, model_id=profile.model_id, variant=profile.variant,
                          adapter_id="unqualified-manual-authoring" if selected else "manual-guide-only",
                          profile_binding="user_selected_advice_unverified")
            if selected is None:
                target["mode"] = "guide-only"
            try:
                self.draft = render_prompt(self.analysis, target, profile_id,
                    recorded_metadata=self.source.get("recorded_metadata"))
                generated = self.draft["outputs"]["positive"]
                self.policy.setText(("Writing guide chosen by you; checkpoint match and adapter support are unverified. " if selected else
                     "Guide-only advice; no checkpoint selected and no execution qualification. ") +
                    "Documented model negative support: " + self.draft["negative_model_support"] +
                    "; effective adapter support: " + self.draft["negative_capability"] +
                    ". Copy/export is manual transfer, not execution approval. Writing guidance does not establish output quality.")
                self.details.setPlainText(json.dumps({"profile": self.draft["profile"], "target": target,
                    "analysis": self.analysis}, ensure_ascii=False, indent=2))
            except (ValueError, TypeError) as exc:
                self.policy.setText("Writing guide unavailable: " + str(exc) + " Continue editing manually.")
        else:
            self.policy.setText("Manual authoring. Select a registered checkpoint and optional writing guide; "
                                "no model or adapter compatibility is assumed.")
            self.details.setPlainText(json.dumps({"checkpoint": selected, "analysis": self.analysis}, ensure_ascii=False, indent=2))
        values = self._edits.get(self._key, {"positive": generated, "negative": ""})
        self._loading = True
        self.positive.setPlainText(values["positive"])
        self.negative.setPlainText(values["negative"])
        self._loading = False

    def _add_candidate(self, part):
        if len(self._candidates) >= 32:return
        self._candidates.append(deepcopy(part))
        self.candidate.addItem(f"{part['kind']}: {part.get('source_label', 'uncertain suggestion')}")
        self._show_candidate(self.candidate.currentIndex())

    def _show_candidate(self, index):
        if 0 <= self._review_index < len(self._candidates):
            self._candidates[self._review_index]["edited_text"] = self.review_text.toPlainText()
        self._review_index = index
        exists = 0 <= index < len(self._candidates)
        self.use_reviewed.setEnabled(exists)
        if exists:
            part = self._candidates[index]
            self.review_text.setPlainText(part.get("edited_text", part["text"]))
            self.provenance.setText(f"{part.get('source_label', 'Uncertain suggestion')} · {part.get('model_id', 'unknown')}\n"
                                    "Edit and deliberately use this text. Original evidence remains retained.")
        else:self.review_text.clear()

    def _use_reviewed(self):
        index = self.candidate.currentIndex()
        if not 0 <= index < len(self._candidates):return
        text = self.review_text.toPlainText()
        if not text.strip() or len(text.encode('utf-8')) > 16384:
            self.status.setText("Use nonempty reviewed text of at most 16 KiB.");return
        if len(self.analysis["parts"]) >= 32:
            self.status.setText("This retained review is full. Continue editing the prompt manually.");return
        candidate = deepcopy(self._candidates[index])
        part = dict(candidate, id=f"reviewed-{len(self.analysis['parts'])}", text=text,
                    original_text=candidate["text"], reviewed=True, selected=True)
        self.analysis["parts"].append(part)
        # Explicit Use appends to the current authored prompt; target changes never overwrite it.
        self.positive.setPlainText(self.positive.toPlainText() + (", " if self.positive.toPlainText() else "") + text)
        self._remember()
        self._target_changed()
        self.tabs.setCurrentIndex(1)
        self.status.setText("Reviewed text added to this draft. Other target edits and original metadata are retained.")

    def _source_current(self):
        if callable(self.source_current_provider) and self.source_current_provider() != self.source["identity"]:
            raise ValueError("Source changed. Reopen the image before requesting more suggestions; your draft is retained.")

    def _analyze(self):
        if not self._delivery_live() or self._task is not None or self.has_active_background_work():return
        try:
            self._source_current()
            if self.suggestion_service is None:
                from core.generation_image_suggestions import suggest_image_setup_parts
                service = suggest_image_setup_parts
            else:service = self.suggestion_service
            if self.task_factory is None:
                from qt_app.workers import CoordinatedCallTask
                factory = CoordinatedCallTask
            else:factory = self.task_factory
            kinds = tuple(self.goal.currentData())
            def work(job):
                self._source_current()
                result = service(self.source["path"], self.settings, selected_kinds=kinds,
                                 cancelled=lambda: job.cancel_requested)
                self._source_current()
                return result
            task = factory(self.jobs, "Suggest prompt text from one image", work, resource="gpu", key=f"organizer-prompt:{id(self)}")
            self._task = task
            self.analyze.setEnabled(False)
            self.cancel_analysis.setEnabled(True)
            task.signals.complete.connect(lambda result: self._complete(task, result))
            task.signals.failed.connect(lambda error: self._finished(task, "Suggestion unavailable: " + str(error)))
            task.signals.cancelled.connect(lambda: self._finished(task, "Analysis cancelled. Your authored text is retained."))
            self.status.setText("Analyzing this image locally. Cancel stops at the next safe boundary.")
            task.start()
        except Exception as exc:
            self._finished(self._task, "Suggestion unavailable: " + str(exc))

    def _complete(self, task, result):
        if self._task is not task or not self._delivery_live():return
        task._organizer_terminal = True
        try:
            self._source_current()
            for proposal in result.proposals:
                if proposal.kind not in {"character", "scene", "pose", "style", "other"} or not proposal.text.strip():continue
                self._add_candidate({"kind": proposal.kind, "text": proposal.text,
                    "origin": "inferred", "reviewed": False, "selected": False,
                    "source_label": proposal.source_label, "model_id": proposal.model_id,
                    "image_identity": deepcopy(self.source["identity"])})
            self.tabs.setCurrentIndex(0)
            self._finished(task, "Uncertain suggestions are ready. Edit and use each deliberately; your prompt is unchanged.")
        except Exception as exc:self._finished(task, str(exc))

    def _finished(self, task, message):
        if self._task is not task or not self._delivery_live():return
        if task is not None:task._organizer_terminal = True
        self._task = None
        self.analyze.setEnabled(self.jobs is not None and self.settings is not None and bool(self.source.get("path")))
        self.cancel_analysis.setEnabled(False)
        self.status.setText(message)

    def _delivery_live(self):
        if not isValid(self) or self._closing:return False
        owner = self.parentWidget()
        return owner is None or isValid(owner) and not getattr(owner, "_organizer_prompt_closing", False)

    @staticmethod
    def _task_drained(task):
        if getattr(task, "_organizer_terminal", False):return True
        job_id = getattr(task, "job_id", "")
        job = getattr(getattr(task, "manager", None), "jobs", {}).get(job_id)
        return job is not None and job.state in {"complete", "failed", "cancelled"}

    def has_active_background_work(self):
        return self._task is not None or any(not self._task_drained(task) for task in self._draining_tasks)

    def _cancel(self):
        if self._task is not None:
            task, self._task = self._task, None
            task.cancel()  # Unobserve and request cancellation; coordinator still owns actual drain.
            self._draining_tasks.append(task)
            self.analyze.setEnabled(False)
            self.cancel_analysis.setEnabled(False)
            self.status.setText("Analysis cancellation requested; waiting for worker drain. Your draft is retained.")
            self._drain_timer.start()

    def _poll_draining(self):
        self._draining_tasks = [task for task in self._draining_tasks if not self._task_drained(task)]
        if self._draining_tasks:return
        self._drain_timer.stop()
        if self._delivery_live():
            self.analyze.setEnabled(self.jobs is not None and self.settings is not None and bool(self.source.get("path")))
            self.status.setText("Analysis stopped after worker drain. Your draft is retained.")

    def request_background_shutdown(self):
        self._closing = True
        self._cancel()

    def resume_after_cancelled_shutdown(self):
        self._closing = False
        self._poll_draining()

    def _cancel_on_destroy(self):
        self._closing = True
        if self._task is not None:
            task, self._task = self._task, None
            task.cancel()
            self._draining_tasks.append(task)

    def _owner_destroyed(self, *_):
        self._cancel_on_destroy()

    def has_unsaved_changes(self):
        self._remember()
        if self.analysis != self._initial_analysis:return True
        if any(values["positive"] or values["negative"] for values in self._edits.values()):return True
        for index, part in enumerate(self._candidates):
            current = self.review_text.toPlainText() if index == self._review_index else part.get("edited_text", part["text"])
            if index >= len(self._initial_review_texts) or current != self._initial_review_texts[index]:return True
        return False

    def confirm_draft_loss(self):
        if not self.has_unsaved_changes():return True
        return QMessageBox.question(self, "Close Prompt Draft?",
            "This session retains prompt edits for every target and pending image-text reviews. "
            "Copy or export what you need before closing. Discard the whole open session?",
            QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Cancel) == QMessageBox.Discard

    def finish_parent_close(self):
        self.request_background_shutdown()
        QDialog.reject(self)  # Parent has explicitly reviewed the entire retained session.

    def _copy(self, side):
        text = getattr(self, side).toPlainText()
        if len(text.encode('utf-8')) > 16384:
            self.status.setText("Trim this prompt to at most 16 KiB before copying.");return
        QApplication.clipboard().setText(text)
        self.status.setText(f"Copied {side} prompt. Manual transfer does not establish target support.")

    def _export(self):
        name, _ = QFileDialog.getSaveFileName(self, "Export Prompt Plain Text", "prompt.txt", "Text files (*.txt)")
        if not name:return
        try:
            from core.organizer_prompt_export import export_new_prompt_text
            writer = self.export_writer or export_new_prompt_text
            writer(Path(name), self.positive.toPlainText(), self.negative.toPlainText(),
                   policy=self.policy.text(), protected_paths=(self.source.get("path"),))
            self.status.setText("Exported and verified plain text. Retained analysis and metadata stay in this open draft.")
        except Exception as exc:self.status.setText("Export unavailable: " + str(exc) + " Your draft is retained.")

    def reject(self):
        if not self.confirm_draft_loss():return
        self.request_background_shutdown()
        super().reject()
