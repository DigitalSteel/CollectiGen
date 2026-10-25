"""Paged video records and an explicit, source-preserving Ingest handoff."""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal, QThreadPool
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout,
)

from core.video_asset import VideoAssetRecord, list_videos
from .workers import CancelableProgressCallTask


class VideoIngestDialog(QDialog):
    """Reuse the Ingest transaction coordinator; keep videos out of ImageRecord."""

    background_work_finished = Signal()
    imported = Signal(object)

    def __init__(self, state, ingest_root, *, service=None, receipt_path=None,
                 source_path=None, parent=None, operation_owner="ingest"):
        super().__init__(parent)
        self.setWindowTitle("Ingest Videos")
        self.resize(1100, 760)
        self.state = state
        self.operation_owner = operation_owner
        self.root = Path(ingest_root)
        self.service = service
        self.receipt_path = Path(receipt_path) if receipt_path else None
        self.source_path = Path(source_path) if source_path else None
        self._task = None
        self._player = None
        self._closing = False
        self._plan = None
        self._record = None
        self._selected_path = None
        self._offset = 0
        self._next_offset = None
        self._last_transfer_message = ""
        layout = QVBoxLayout(self)
        title = QLabel("Add verified video to Ingest" if service is not None else "Videos awaiting review")
        self.title_label = title
        title.setStyleSheet("font-size: 20px; font-weight: 600")
        layout.addWidget(title)
        explanation = QLabel(
            "Review the selected receipt, destination and privacy boundary before copying. "
            "The original clip remains available and the Ingest copy starts unreviewed."
            if service is not None else
            "Generated clips retain their exact timing, source and model lineage. "
            "Playback does not confirm knowledge or move a clip into a training collection."
        )
        explanation.setWordWrap(True)
        self.explanation_label = explanation
        layout.addWidget(explanation)
        splitter = QSplitter(Qt.Horizontal)
        self.rows = QListWidget()
        self.rows.currentItemChanged.connect(self._selection_changed)
        splitter.addWidget(self.rows)
        self.rows.setVisible(service is None)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        splitter.addWidget(self.details)
        splitter.setSizes([400, 680])
        layout.addWidget(splitter, 1)
        self.status = QLabel("Loading registered video records…")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.raw_record = QCheckBox("Show raw temporal record")
        self.raw_record.toggled.connect(self._show_record)
        layout.addWidget(self.raw_record)
        self.boundary = QCheckBox("I approve this exact transfer across the displayed privacy boundary")
        self.boundary.setVisible(False)
        self.boundary.toggled.connect(self._update_actions)
        layout.addWidget(self.boundary)
        actions = QHBoxLayout()
        self.previous_button = QPushButton("Previous 100")
        self.previous_button.clicked.connect(lambda: self.load_page(max(0, self._offset - 100)))
        self.next_button = QPushButton("Next 100")
        self.next_button.clicked.connect(lambda: self.load_page(self._next_offset or 0))
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(lambda: self.load_page(self._offset))
        self.play_button = QPushButton("Play Selected Video…")
        self.play_button.clicked.connect(self._play)
        self.copy_button = QPushButton("Copy Verified Video to Ingest")
        self.copy_button.clicked.connect(self._copy)
        self.copy_button.setVisible(service is not None)
        self.cancel_button = QPushButton("Cancel Safely")
        self.cancel_button.clicked.connect(self._cancel)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.reject)
        for browse in (self.previous_button, self.next_button, self.refresh_button):
            browse.setVisible(service is None)
        for button in (self.previous_button, self.next_button, self.refresh_button,
                       self.play_button, self.copy_button, self.cancel_button):
            actions.addWidget(button)
        actions.addStretch()
        actions.addWidget(self.close_button)
        layout.addLayout(actions)
        self._update_actions()
        QTimer.singleShot(0, self.preview_transfer if service is not None else self.load_page)

    def _update_actions(self, *_args):
        busy = self._task is not None or self._closing
        self.rows.setEnabled(not busy)
        self.raw_record.setEnabled(not busy and self._record is not None)
        self.previous_button.setEnabled(not busy and self._offset > 0)
        self.next_button.setEnabled(not busy and self._next_offset is not None)
        self.refresh_button.setEnabled(not busy and self.service is None)
        self.play_button.setEnabled(not busy and self._record is not None and self._record.supported
                                    and self._player is None)
        self.copy_button.setEnabled(not busy and self._plan is not None and self._plan.ready
                                    and (not self._plan.boundary_decision.blocked_by_default or self.boundary.isChecked()))
        self.cancel_button.setVisible(self._task is not None)
        self.cancel_button.setEnabled(self._task is not None and not self._closing)

    def _run(self, label, operation, completed):
        if self._task is not None or self._closing:
            return
        started, active = self.state.begin_long_operation(self.operation_owner, label, resource="io")
        if not started:
            self.status.setText(f"Finish the current operation before continuing: {active}")
            return
        self.status.setText(label)
        task = CancelableProgressCallTask(lambda progress, cancelled: operation(cancelled))
        self._task = task
        def finish(result=None, error=None):
            if task is not self._task:
                return
            self._task = None
            self.state.finish_long_operation(self.operation_owner)
            if not self._closing:
                if error is not None:
                    self.status.setText(error)
                else:
                    completed(result)
            self._update_actions()
            self.background_work_finished.emit()
            if self._closing and not self.has_active_background_work():
                QDialog.reject(self)
        task.signals.complete.connect(lambda result: finish(result))
        task.signals.failed.connect(lambda message: finish(error=message))
        task.signals.cancelled.connect(lambda: finish(error="Cancelled safely. Retained source files remain available."))
        self._update_actions()
        QThreadPool.globalInstance().start(task)

    def load_page(self, offset=0):
        def loaded(page):
            self._offset = page.offset
            self._next_offset = page.next_offset
            self._record = None
            self._selected_path = None
            self.rows.clear()
            for row in page.rows:
                item = QListWidgetItem(f"{row.path.name}\n{row.review_status.replace('_', ' ')}")
                item.setData(Qt.UserRole, str(row.path))
                item.setToolTip(str(row.path))
                self.rows.addItem(item)
            self.details.clear()
            if page.total == 0 and not page.issues:
                self.details.setPlainText(
                    "No video records are in this Ingest folder yet.\n\n"
                    "After a verified local video result is ready in Generate, review its exact receipt "
                    "and choose Copy Verified Video to Ingest. It will appear here as unreviewed "
                    "with timing and lineage preserved."
                )
            self.status.setText(
                (self._last_transfer_message + "\n" if self._last_transfer_message else "")
                + f"{page.total:,} registered video(s). Showing {len(page.rows):,}. "
                + " ".join(page.issues)
            )
        self._run("Reading video records…", lambda cancel: list_videos(
            self.root, offset=offset, limit=100, should_cancel=cancel), loaded)

    def _selection_changed(self, selected, _previous):
        if self._player is not None:
            self._player.close()
        if selected is None:
            self._record = None
            self._selected_path = None
            self._update_actions()
            return
        path = Path(selected.data(Qt.UserRole))
        self._record = None
        self._selected_path = path
        def loaded(record):
            if self._selected_path != path:
                return
            self._record = record
            self._show_record()
            self.status.setText("Ready for playback and lineage inspection." if record.supported
                                else "This record uses a newer schema. Its original fields are preserved; playback is unavailable.")
        self._run("Reading selected video lineage…", lambda _cancel: VideoAssetRecord.read(path), loaded)

    def _show_record(self, *_args):
        if self._record is None:
            return
        value = self._record.to_dict()
        if self.raw_record.isChecked() or not self._record.supported:
            self.details.setPlainText(json.dumps(value, ensure_ascii=False, indent=2))
            return
        asset = self._record.asset
        timing = asset.timing
        generation = value.get("generation", {})
        request = generation.get("request", {})
        if not isinstance(request, dict):
            request = {}
        source = generation.get("source_image")
        resources = generation.get("resource_bindings", [])
        if not isinstance(resources, list):
            resources = []
        resource_labels = [
            str(resource.get("role") or "resource")
            for resource in resources if isinstance(resource, dict)
        ]
        prompt = str(request.get("positive_prompt") or request.get("prompt") or "")
        motion = str(request.get("motion") or "")
        camera = str(request.get("camera") or "")
        goal = str(request.get("goal") or "")
        self.details.setPlainText(
            f"{self._selected_path.name}\n\nReview: {value.get('review_status', 'unknown').replace('_', ' ')}\n"
            f"Duration: {str(round(float(timing.duration), 3)) + ' seconds' if timing.duration is not None else 'unknown'}\nFrames: {timing.frame_count:,}\n"
            f"Nominal rate: {timing.nominal_fps.numerator}/{timing.nominal_fps.denominator} fps\n"
            f"Size: {asset.byte_length:,} bytes\nPrivacy: {asset.privacy_disposition}\n\n"
            f"Source: {'image-conditioned video; exact source retained in lineage' if source else 'text-to-video'}\n"
            + "Resource roles: " + (", ".join(resource_labels) or "unavailable")
            + f"\nGoal: {goal or 'not recorded'}\nMotion: {motion or 'not recorded'}"
            + f"\nCamera: {camera or 'not recorded'}\n\nPrompt: {prompt or 'not recorded'}\n\n"
            "Exact presentation timestamps, original runtime evidence, source/model identities and scoped knowledge "
            "are preserved in the temporal record. Use Show raw temporal record to inspect those fields.")

    def _play(self):
        if self._record is None or not self._record.supported or self._selected_path is None:
            return
        if self._player is not None:
            self._player.show()
            self._player.raise_()
            return
        from .video_preview_dialog import VideoPreviewDialog
        selected_path=self._selected_path;record=self._record
        factory=getattr(self.state,"dependency_service_factory",None)
        lifetime=getattr(self.state,"video_decoder_lifetime",None)
        def opened(binding=None):
            if self._closing or self._record is not record or self._selected_path!=selected_path:return
            if getattr(self.state,"dependency_service_factory",None) is not factory or getattr(self.state,"video_decoder_lifetime",None) is not lifetime:
                self.status.setText("Video runtime context changed. Choose Play again.");return
            runtime_options={}
            if binding is not None:runtime_options["runtime_binding"]=binding
            if lifetime is not None:runtime_options["decoder_lifetime"]=lifetime
            try:
                player = VideoPreviewDialog(selected_path, record.asset,
                                        allowed_root=self.root,
                                        settings=getattr(self.state, "settings", None), parent=self,**runtime_options)
            except (ValueError, RuntimeError) as exc:
                self.status.setText(f"Video playback unavailable: {exc}");return
            self._player = player
            self._update_actions()
            player.background_work_finished.connect(self._player_finished_work)
            player.finished.connect(lambda _result: self._clear_player(player))
            player.show()
        if factory is None:
            opened();return
        admit=getattr(self.state,"admit_video_runtime",None)
        if not callable(admit) or lifetime is None:
            self.status.setText("Video playback needs its admitted runtime/lifetime. Review Components in Setup.");return
        def prepared(cancelled):
            def check_interrupt():
                if cancelled():raise InterruptedError("Video playback opening cancelled")
            check_interrupt()
            if self.state.dependency_service_factory is not factory or self.state.video_decoder_lifetime is not lifetime:
                raise RuntimeError("Video runtime context changed. Choose Play again.")
            binding=admit(check_interrupt=check_interrupt)
            check_interrupt()
            return binding
        self._run("Checking Video runtime for playback.",prepared,opened)

    def _clear_player(self, player):
        if self._player is player:
            self._player = None
        self._update_actions()
        self._player_finished_work()

    def _player_finished_work(self):
        if not self.has_active_background_work():
            self.background_work_finished.emit()
            if self._closing:
                QDialog.reject(self)

    def preview_transfer(self):
        if self.service is None:
            return
        def loaded(plan):
            self._plan = plan
            self.details.setPlainText(
                f"Source: {plan.source}\nDestination: {plan.destination}\n\n"
                "The original clip and verified receipt remain available. The video and its temporal "
                "record are copied together, checked again and kept unreviewed.\n\n"
                + plan.boundary_decision.message + "\n" + plan.external_boundary.message
                + "\n\n" + "\n".join(plan.issues))
            self.boundary.setChecked(False)
            self.boundary.setVisible(bool(plan.boundary_decision.blocked_by_default))
            self.status.setText("Review this exact copy, then add it to Ingest." if plan.ready
                                else "Copy is held. Resolve the listed issue and reopen the verified result.")
        self._run("Verifying the retained video and receipt…", lambda cancel: self.service.preview(
            self.receipt_path, self.source_path, self.root, should_cancel=cancel), loaded)

    def _copy(self):
        if not self.copy_button.isEnabled():
            return
        plan = self._plan
        confirmed = self.boundary.isChecked()
        def complete(result):
            self.status.setText(result.message + ("\n" + "\n".join(result.issues) if result.issues else ""))
            self._plan = None
            if result.status in {"transferred", "already_present"}:
                self._last_transfer_message = result.message
                self.imported.emit(result.destination)
                self.service = None
                self.copy_button.hide()
                self.boundary.hide()
                self.title_label.setText("Videos awaiting review")
                self.explanation_label.setText(
                    "Generated clips retain their exact timing, source and model lineage. "
                    "Playback does not confirm knowledge or move a clip into a training collection."
                )
                self.rows.show()
                for browse in (self.previous_button, self.next_button, self.refresh_button):
                    browse.show()
                QTimer.singleShot(0, self.load_page)
            else:
                self.status.setText(self.status.text() + "\nReopen the verified result after resolving this issue.")
        self._run("Copying the verified video and temporal record…", lambda cancel: self.service.transfer(
            plan, boundary_confirmed=confirmed, should_cancel=cancel), complete)

    def _cancel(self):
        if self._task is not None:
            self._task.cancel()
            self.status.setText("Cancelling safely…")
            self.cancel_button.setEnabled(False)

    def has_active_background_work(self):
        return self._task is not None or bool(self._player and self._player.has_active_background_work())

    def request_background_shutdown(self):
        self._closing = True
        self._cancel()
        if self._player is not None:
            self._player.request_background_shutdown()
        self._update_actions()
        return self.has_active_background_work()

    def reject(self):
        if self.has_active_background_work():
            self.request_background_shutdown()
        else:
            if self._player is not None:
                self._player.close()
            super().reject()

    def closeEvent(self, event):
        if self.has_active_background_work():
            self.request_background_shutdown()
            event.ignore()
        else:
            super().closeEvent(event)
