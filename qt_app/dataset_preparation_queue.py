"""GUI-thread polling and exact-request routing for Dataset's serial queue."""
from dataclasses import replace
from pathlib import Path
import weakref

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QPushButton, QScrollArea, QVBoxLayout, QHBoxLayout, QWidget
from shiboken6 import isValid

from core import ImageRecord
from .components import compact_button, bind_status_footer
from .preparation_result_dialog import PreparationResultDialog


class DatasetPreparationQueue(QWidget):
    surface="Dataset"
    registry_name="dataset_preparation_work"
    busy_name="_preparation_task"
    prepare_name="prepare_images"

    def _set_status(self,message):self.owner.header.set_status(message)
    def _registry(self):return getattr(self.owner.state,self.registry_name)
    def _controls_changed(self):self.owner.update_selection_controls()
    def _hide_page_cancel(self):self.owner.cancel_projection_button.hide()
    def _history_action(self,paths,operation):self.owner.open_saved_history(paths=paths,operation_id=operation)

    def __init__(self,owner):
        super().__init__(owner)
        self.owner=owner
        self.coordinator=owner.state.preparation_requests
        self.entries={}
        self.button=compact_button(QPushButton("Preparation Tasks",owner))
        self.button.setAccessibleName(f"{self.surface} preparation task queue")
        self.button.clicked.connect(self.show_tasks)
        self.button.hide()
        self.dialog=QDialog(owner)
        self.dialog.setWindowTitle(f"{self.surface} Preparation Tasks")
        self.dialog.setWindowModality(Qt.NonModal)
        self.dialog.resize(780,460)
        layout=QVBoxLayout(self.dialog)
        note=QLabel("Requests run one at a time. Each keeps its original images and choices. Changed authority is held for review; waiting time has no predicted ETA.")
        note.setWordWrap(True);layout.addWidget(note)
        scroll=QScrollArea();scroll.setWidgetResizable(True)
        self.rows=QWidget();self.row_layout=QVBoxLayout(self.rows)
        self.row_layout.setAlignment(Qt.AlignTop)
        scroll.setWidget(self.rows);layout.addWidget(scroll,1)
        close=QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.dialog.reject);layout.addWidget(close)
        self.timer=QTimer(self);self.timer.setInterval(100)
        self.timer.timeout.connect(self.refresh)
        self.hide()
        for work in tuple(self._registry().values()):
            self.submit(work,restored=True)

    def submit(self,work,editor=None,*,restored=False):
        request=work.request
        entry={"work":work,"editor":weakref.ref(editor) if editor is not None else None,
               "delivery":work.delivery,"last_progress":None}
        row=QWidget();layout=QVBoxLayout(row);layout.setContentsMargins(6,6,6,6)
        if not hasattr(work,"display_number"):
            sequence=getattr(self.owner.state,"_dataset_preparation_sequence",0)+1
            self.owner.state._dataset_preparation_sequence=sequence
            work.display_number=sequence
        label=QLabel(f"Request {work.display_number} • {len(request.paths):,} image(s) • Queued")
        label.setWordWrap(True);label.setAccessibleName("Preparation request status")
        layout.addWidget(label)
        actions=QHBoxLayout()
        cancel=compact_button(QPushButton("Cancel Safely"));cancel.clicked.connect(lambda:self.cancel_request(request.id))
        review=compact_button(QPushButton("Review Outcomes"));review.clicked.connect(lambda:self.review(request.id));review.setEnabled(False)
        retry=compact_button(QPushButton("Review & Resubmit…"));retry.clicked.connect(lambda:self.resubmit(request.id));retry.hide()
        history=compact_button(QPushButton("History & Versions…"));history.clicked.connect(lambda:self.history(request.id));history.hide()
        dismiss=compact_button(QPushButton("Dismiss"));dismiss.clicked.connect(lambda:self.dismiss(request.id));dismiss.hide()
        for button in (cancel,review,retry,history,dismiss):actions.addWidget(button)
        actions.addStretch(1);layout.addLayout(actions)
        entry.update(row=row,label=label,cancel=cancel,review=review,retry=retry,history=history,
                     number=work.display_number,dismiss=dismiss)
        # Register all routing before submit can finish a fast synthetic request.
        self.entries[request.id]=entry;self.row_layout.addWidget(row)
        try:
            job=(self.coordinator.progress(request.id) if restored else
                 self.coordinator.submit(request,validate=work.validate,worker_factory=work.factory))
        except Exception:
            self.entries.pop(request.id);row.deleteLater();raise
        entry["job"]=job
        self._registry()[request.id]=work
        if editor is not None:
            editor._dataset_preparation_request_id=request.id
            editor.set_ai_busy(True,"Preparation queued; this request keeps its original image scope.")
            if isinstance(editor,QDialog):
                button=getattr(editor,"_dataset_request_button",None)
                if button is None:
                    button=compact_button(QPushButton("Preparation Task…",editor))
                    editor._dataset_request_button=button
                    bind_status_footer(editor,button,title="This preparation request")
                else:
                    try:button.clicked.disconnect()
                    except RuntimeError:pass
                reference=weakref.ref(editor)
                button.clicked.connect(lambda:self.show_editor_task(request.id,reference))
                button.setText(f"Preparation Task {work.display_number}…")
                button.setAccessibleName("Inspect or cancel this exact preparation request")
                button.show()
                entry["editor_task_button"]=button

        if job.state in {"queued","running","paused"}:setattr(self.owner,self.busy_name,self)
        self._hide_page_cancel() # Exact row actions replace ambiguous page-wide cancel.
        self.button.show();self.timer.start();self.refresh()
        self._controls_changed()
        return request.id

    def _editor(self,request_id):
        entry=self.entries[request_id];reference=entry["editor"]
        editor=reference() if reference is not None else None
        if (editor is None or not isValid(editor) or not editor.isVisible()
                or getattr(self.owner,"_active_editor",None) is not editor
                or getattr(editor,"_dataset_preparation_request_id",None)!=request_id):return None
        if str(Path(editor.record.image_path).absolute()) not in entry["work"].request.paths:return None
        return editor

    def refresh(self):
        pending=0
        for request_id,entry in tuple(self.entries.items()):
            job=entry["job"]
            if job.state in {"queued","running","paused"}:pending+=1
            elif entry["delivery"] is None:
                delivery=self.coordinator.take_result(request_id)
                if delivery is not None:
                    entry["delivery"]=delivery
                    entry["work"].delivery=delivery
                    self._completed(request_id)
            delivery=entry["delivery"]
            state=delivery.state if delivery is not None else job.state
            state_text={"queued":"Queued — waiting for serial capacity (ETA unknown)",
                        "running":"Running", "paused":"Paused", "held":"Held — review required",
                        "complete":"Finished — review outcomes", "cancelled":"Cancelled safely",
                        "failed":"Stopped — needs attention"}.get(state,state)
            if job.cancel_requested and delivery is None:state_text="Cancellation requested — waiting for safe boundary"
            detail=delivery.detail if delivery is not None else str(job.message or "")
            if delivery is not None and isinstance(delivery.result,dict):
                result=delivery.result
                errors=result.get("errors",())
                if errors:state_text=("Cancelled safely with issues — review outcomes" if state=="cancelled"
                                      else "Finished with issues — review outcomes")
                detail=detail or " • ".join(result.get("summary_lines",())[:2])
            count=len(entry["work"].request.paths)
            text=f"Request {entry['number']} • {count:,} image(s) • {state_text}"
            if entry.get("routing_error"):detail="Saved work kept; editor refresh needs attention. Review outcomes and reopen the image."
            full_text=text+("\n"+detail if detail else "")
            if "Task ETA:" in detail or "inference ETA:" in detail:
                from core.inference_timing import compact_inference_progress
                detail=compact_inference_progress(detail)
            if detail:text+="\n"+detail
            entry["label"].setText(text);entry["label"].setToolTip(full_text)
            entry["label"].setAccessibleDescription(full_text)
            entry["cancel"].setEnabled(delivery is None and not job.cancel_requested)
            entry["review"].setEnabled(delivery is not None)
            entry["retry"].setVisible(delivery is not None and (state in {"held","failed","cancelled"}
                or isinstance(delivery.result,dict) and bool(delivery.result.get("errors"))))
            entry["dismiss"].setVisible(delivery is not None)
            result=delivery.result if delivery is not None and isinstance(delivery.result,dict) else {}
            entry["history"].setVisible(bool(result.get("history_operation_id")))
            editor=self._editor(request_id)
            if editor is not None and delivery is None:
                progress=(job.current,job.total,job.message,state)
                if progress!=entry["last_progress"]:
                    if state=="queued":editor.set_ai_busy(True,"Preparation queued; waiting time is unknown.")
                    else:editor.set_preparation_progress(job.current,job.total,job.message or state_text)
                    entry["last_progress"]=progress
        self.button.setText(f"Preparation Tasks ({pending:,})")
        self.button.setToolTip(f"{pending:,} request(s) waiting or running; {len(self.entries)-pending:,} retained outcomes. Open exact scopes, Cancel Safely and recovery.")
        awaiting_delivery=any(entry["delivery"] is None for entry in self.entries.values())
        self._publish_status(pending,awaiting_delivery)
        if not pending and not awaiting_delivery:self.timer.stop()
        if not pending and not awaiting_delivery and getattr(self.owner,self.busy_name,None) is self:
            setattr(self.owner,self.busy_name,None)
            self._controls_changed()
            self.owner._notify_background_work_finished()
            self.timer.stop()

    def _publish_status(self,pending,awaiting_delivery):
        if not self.entries:
            self._set_status("Preparation outcomes dismissed.")
            return
        if pending:
            message=f"Preparation: {pending:,} request(s) waiting or running. Open Preparation Tasks for exact scope and cancellation."
        elif awaiting_delivery:
            message="Preparation results are arriving. Open Preparation Tasks for exact outcomes."
        else:
            attention=sum(
                bool(entry.get("routing_error"))
                or entry["delivery"].state in {"held","failed"}
                or isinstance(entry["delivery"].result,dict) and bool(entry["delivery"].result.get("errors"))
                for entry in self.entries.values()
            )
            cancelled=sum(entry["delivery"].state=="cancelled" for entry in self.entries.values())
            message=f"Preparation finished. Open Preparation Tasks for {len(self.entries):,} retained outcome(s)."
            if attention:message+=f" {attention:,} request(s) need attention."
            if cancelled:message+=f" {cancelled:,} request(s) were cancelled."
        self._set_status(message)

    def _completed(self,request_id):
        entry=self.entries[request_id];delivery=entry["delivery"]
        work=entry["work"];result=self._result_data(delivery)
        editor=self._editor(request_id)
        if editor is not None:
            if delivery.state=="complete" and not result.get("skip_followup_state_scan"):
                try:editor.refresh_ai_evidence(ImageRecord(editor.record.image_path))
                except Exception as exc:entry["routing_error"]=str(exc)
            else:editor.set_ai_busy(False)
            if not isValid(editor):editor=None
            errors=list(result.get("errors",()))
            memory_held=False
            if editor is not None:
                memory_held=self.owner._show_editor_memory_hold(editor,errors,result=result)
            if editor is not None and errors:
                if not memory_held:
                    editor.preparation_unavailable("This request needs attention. Open Preparation Tasks → Review Outcomes.")
            elif editor is not None and delivery.detail:editor.preparation_unavailable(delivery.detail)
        if delivery.state!="held" and not result.get("skip_followup_state_scan"):
            self._completion_updates(work.paths)

    def _completion_updates(self,paths):
        for path in paths:
            self.owner._readiness_cache.pop(path,None)
            self.owner._invalidate_inspection_cache(path)
        # Gallery invalidation never selects another image or replaces an editor.
        if not getattr(self.owner,"_shutdown_requested",False):
            self.owner.render_page()
            events=getattr(self.owner.state,"events",None)
            if events is not None:
                events.publish("image_data_changed",images=tuple(str(path) for path in paths),source="preparation")

    def show_editor_task(self,request_id,reference):
        editor=reference()
        if editor is None or not isValid(editor) or request_id not in self.entries:return
        dialog=QDialog(editor);dialog.setWindowTitle("This Preparation Request")
        layout=QVBoxLayout(dialog);label=QLabel();label.setWordWrap(True);layout.addWidget(label)
        buttons=QDialogButtonBox(QDialogButtonBox.Close)
        cancel=buttons.addButton("Cancel Safely",QDialogButtonBox.ActionRole)
        cancel.clicked.connect(lambda:self.cancel_request(request_id))
        review=buttons.addButton("Review Outcomes",QDialogButtonBox.ActionRole)
        review.clicked.connect(lambda:self.review(request_id,parent=dialog))
        buttons.rejected.connect(dialog.reject);layout.addWidget(buttons)
        def refresh():
            entry=self.entries.get(request_id)
            if entry is None:dialog.reject();return
            label.setText(entry["label"].text())
            cancel.setEnabled(entry["delivery"] is None and not entry["job"].cancel_requested)
            review.setEnabled(entry["delivery"] is not None)
        timer=QTimer(dialog);timer.setInterval(100);timer.timeout.connect(refresh)
        refresh();timer.start();dialog.resize(640,260);dialog.exec();timer.stop()

    def show_tasks(self):
        self.refresh();self.dialog.show();self.dialog.raise_();self.dialog.activateWindow()

    def cancel_request(self,request_id):
        self.coordinator.cancel(request_id);self.refresh()

    def cancel(self):
        for request_id,entry in self.entries.items():
            if entry["job"].state in {"running","paused"}:
                self.cancel_request(request_id);return
        for request_id,entry in self.entries.items():
            if entry["job"].state=="queued":self.cancel_request(request_id);return

    def cancel_all(self):
        for request_id,entry in tuple(self.entries.items()):
            if entry["delivery"] is None:self.coordinator.cancel(request_id)
        self.refresh()

    @staticmethod
    def _result_data(delivery):
        # Presentation may mark late cancellation, but never rewrite saved receipts.
        result=dict(delivery.result) if isinstance(delivery.result,dict) else {
            "processed":0,"errors":[delivery.detail] if delivery.detail else [],
            "summary_lines":[delivery.state]}
        if delivery.state=="cancelled":
            result["cancelled"]=True
        return result

    def review(self,request_id,*,parent=None):
        delivery=self.entries[request_id]["delivery"]
        if delivery is None:return
        result=self._result_data(delivery)
        dialog=PreparationResultDialog(result,parent=parent or self.dialog)
        dialog.exec()
        if dialog.review_requested:self.owner._review_preparation_paths(dialog.review_paths)

    def history(self,request_id):
        entry=self.entries[request_id];delivery=entry["delivery"]
        result=delivery.result if delivery is not None and isinstance(delivery.result,dict) else {}
        operation=result.get("history_operation_id")
        if operation:self._history_action(entry["work"].paths,operation)

    def resubmit(self,request_id):
        entry=self.entries[request_id];work=entry["work"]
        review=QDialog(self.dialog);review.setWindowTitle("Review Preparation Request Again")
        layout=QVBoxLayout(review)
        text=QLabel(f"Submit a NEW request for the original {len(work.paths):,} image(s). The original stages and quality choices remain selected, but current Setup settings and fresh authority will be captured. Memory-pressure consent is cleared. This never retries automatically or changes your current selection/draft.")
        text.setWordWrap(True);layout.addWidget(text)
        choices=QLabel("Stages: "+", ".join(name for name,enabled in (
            ("TXT/Metadata",work.options.read_external_evidence),("AI Tags",work.options.suggest_sdxl_tags),
            ("AI Descriptions",work.options.suggest_plain_language),("Fingerprints",work.create_fingerprints)) if enabled))
        choices.setWordWrap(True);layout.addWidget(choices)
        buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        submit=buttons.addButton("Submit New Request",QDialogButtonBox.AcceptRole)
        buttons.accepted.connect(review.accept);buttons.rejected.connect(review.reject);layout.addWidget(buttons)
        if review.exec()!=QDialog.Accepted:return
        getattr(self.owner,self.prepare_name)(options=replace(work.options,allow_memory_pressure=False),
            paths=work.paths,editor=self._editor(request_id),fingerprint_retry=work.create_fingerprints)

    def dismiss(self,request_id):
        entry=self.entries[request_id]
        if entry["delivery"] is None:return
        self.entries.pop(request_id)
        self._registry().pop(request_id,None)
        self.coordinator.forget(request_id)
        button=entry.get("editor_task_button")
        editor=self._editor(request_id) if request_id in self.entries else None
        if button is not None and isValid(button):
            reference=entry["editor"]
            target=reference() if reference is not None else None
            if target is not None and isValid(target) and getattr(target,"_dataset_preparation_request_id",None)==request_id:button.hide()
        entry["row"].deleteLater()
        if not self.entries:self.button.hide()
        self.refresh()
