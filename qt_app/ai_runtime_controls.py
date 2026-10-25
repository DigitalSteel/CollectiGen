"""Memory policy controls backed by the one application-owned AI runtime cache."""
from __future__ import annotations
from datetime import datetime, timezone

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QComboBox, QFormLayout, QGroupBox, QLabel, QPushButton, QSpinBox


def runtime_policy_text(settings) -> str:
    mode = getattr(settings, "ai_runtime_mode", "on_demand")
    if mode == "on_demand":
        return "AI memory: On demand. Models are released after each run."
    minutes = getattr(settings, "ai_runtime_idle_minutes", 10)
    prefix = "Preload after startup" if mode == "preload" else "Keep warm"
    return (f"AI memory: {prefix}. Compatible models may stay ready for {minutes} idle minutes "
            "within the memory budget. Other GPU work can release them; saved AI results are unchanged.")


class AIRuntimeMemoryPanel(QGroupBox):
    changed = Signal()

    def __init__(self, settings, state=None, parent=None):
        super().__init__("AI Model Memory", parent)
        self.state = state
        layout = QFormLayout(self)
        self.mode = QComboBox()
        for label, value in (("On Demand", "on_demand"), ("Keep Warm (Recommended)", "keep_warm"),
                             ("Preload After Startup", "preload")):
            self.mode.addItem(label, value)
        self.mode.setAccessibleName("AI model memory policy")
        self.idle = QSpinBox(); self.idle.setRange(1, 120); self.idle.setSuffix(" minutes")
        self.idle.setAccessibleName("Release AI after idle time")
        self.budget = QSpinBox(); self.budget.setRange(0, 131072); self.budget.setSingleStep(256)
        self.budget.setSuffix(" MiB"); self.budget.setSpecialValueText("Automatic for this machine")
        self.budget.setAccessibleName("AI memory budget")
        self.budget.setToolTip("An expert upper limit for retained model memory. Zero uses a conservative hardware budget. A model that cannot be retained can still run on demand.")
        layout.addRow("Memory Policy", self.mode)
        layout.addRow("Release After Idle", self.idle)
        layout.addRow("Memory Budget", self.budget)
        self.guidance = QLabel("Keep warm avoids repeated model loading. Preload loads only enabled, installed, Ready models after startup work settles. It does not analyze images. Save AI Configuration applies these choices.")
        self.guidance.setWordWrap(True); layout.addRow(self.guidance)
        self.status = QLabel("Not loaded")
        self.status.setWordWrap(True); self.status.setAccessibleName("Loaded AI models")
        layout.addRow("Current Memory", self.status)
        self.free = QPushButton("Free AI Memory")
        self.free.setAccessibleDescription("Release loaded AI models through background work. Saved AI results and configuration are preserved.")
        self.free.clicked.connect(self._free_memory); layout.addRow(self.free)
        self.set_values({
            "ai_runtime_mode": getattr(settings, "ai_runtime_mode", "on_demand"),
            "ai_runtime_idle_minutes": getattr(settings, "ai_runtime_idle_minutes", 10),
            "ai_runtime_memory_budget_mb": getattr(settings, "ai_runtime_memory_budget_mb", 0),
        })
        self.mode.currentIndexChanged.connect(self._changed)
        self.idle.valueChanged.connect(self._changed)
        self.budget.valueChanged.connect(self._changed)
        self._timer = QTimer(self); self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh_status); self._timer.start()
        self.refresh_status()

    def values(self):
        return {"ai_runtime_mode": self.mode.currentData(), "ai_runtime_idle_minutes": self.idle.value(),
                "ai_runtime_memory_budget_mb": self.budget.value()}

    def set_values(self, values):
        self.mode.setCurrentIndex(max(0, self.mode.findData(values["ai_runtime_mode"])))
        self.idle.setValue(values["ai_runtime_idle_minutes"])
        self.budget.setValue(values["ai_runtime_memory_budget_mb"])
        self.idle.setEnabled(self.mode.currentData() != "on_demand")
        self.budget.setEnabled(self.mode.currentData() != "on_demand")

    def _changed(self, *_):
        self.idle.setEnabled(self.mode.currentData() != "on_demand")
        self.budget.setEnabled(self.mode.currentData() != "on_demand")
        self.changed.emit()

    def refresh_status(self):
        manager = getattr(self.state, "ai_runtimes", None)
        self.free.setEnabled(callable(getattr(self.state, "free_ai_memory", None)))
        if manager is None:
            self.status.setText("Not loaded")
            return
        values = manager.snapshot()
        lines = []
        for value in values:
            key = getattr(value, "key", None)
            name = getattr(value, "display_name", "") or getattr(key, "model_id", "AI model")
            state = str(getattr(value, "state", "Not loaded"))
            device = str(getattr(value, "device_label", ""))
            memory = str(getattr(value, "memory_mode", ""))
            detail = ", ".join(item for item in (device, memory) if item)
            error = str(getattr(value, "last_error", ""))
            expiry = str(getattr(value, "idle_expires_at", ""))
            if expiry and state == "Ready in memory":
                try:
                    seconds = max(0, (datetime.fromisoformat(expiry) - datetime.now(timezone.utc)).total_seconds())
                    detail += (", " if detail else "") + f"releases in {max(1, int(seconds / 60 + 0.999))} min"
                except (ValueError, TypeError):
                    pass
            lines.append(f"{name}: {state}" + (f" ({detail})" if detail else "") + (f" — {error}" if error else ""))
        self.status.setText("\n".join(lines) or "Not loaded")

    def _free_memory(self):
        release = getattr(self.state, "free_ai_memory", None)
        if callable(release):
            release()
            self.status.setText("Memory release requested. Active work will finish safely first.")


def observe_preparation_memory(parent,settings,options):
    """Review an observation asynchronously, then require explicit task consent."""
    import copy
    from PySide6.QtCore import QThreadPool,Qt
    from PySide6.QtWidgets import QDialog,QDialogButtonBox,QVBoxLayout,QMessageBox
    from core.ai_runtime_residency import preparation_memory_review
    from .workers import AsyncCallTask
    observation={}
    dialog=QDialog(parent);dialog.setWindowTitle("Check Task VRAM")
    layout=QVBoxLayout(dialog)
    label=QLabel("Checking free dedicated VRAM and selected model estimates. No models or images are loaded.")
    label.setWordWrap(True);layout.addWidget(label)
    buttons=QDialogButtonBox(QDialogButtonBox.Cancel);layout.addWidget(buttons)
    buttons.rejected.connect(dialog.reject)
    snapshot=copy.copy(settings)
    task=AsyncCallTask(lambda:preparation_memory_review(snapshot,options))
    def complete(value):
        observation["text"]=value;dialog.accept()
    def failed(_message):
        observation["text"]="Free VRAM / estimated requirement unavailable. Capacity cannot be verified; running anyway may exhaust memory, slow down or fail. Model/runtime readiness is still required. External cancel/unload is unavailable."
        dialog.accept()
    task.signals.complete.connect(complete);task.signals.failed.connect(failed)
    dialog.rejected.connect(task.cancel)
    QTimer.singleShot(0,lambda:QThreadPool.globalInstance().start(task))
    if dialog.exec()!=QDialog.Accepted:return None
    return observation["text"]


def review_preparation_memory_override(parent,settings,options,*,scope_description="Current reviewed preparation scope") -> bool:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMessageBox
    text=observe_preparation_memory(parent,settings,options)
    if text is None:return False
    warning=QMessageBox(QMessageBox.Warning,"Run This Preparation Anyway?","Scope: "+scope_description+"\n\n"+text+"\n\nAllow this exact preparation task to run despite insufficient or unverified VRAM? Other preparations retain the safe default.",parent=parent)
    warning.setTextFormat(Qt.PlainText)
    run=warning.addButton("Run Anyway / Parallel",QMessageBox.AcceptRole)
    cancel=warning.addButton("Keep Safe Default",QMessageBox.RejectRole)
    warning.setDefaultButton(cancel);warning.setEscapeButton(cancel)
    warning.exec()
    return warning.clickedButton() is run


def memory_hold_detail(errors):
    """Recognize the residency owner's hold, omitting per-image filename prefixes."""
    markers=("Insufficient verified dedicated GPU headroom", "Available dedicated GPU memory could not be verified")
    details=[]
    for error in errors:
        text=str(error)
        for marker in markers:
            if marker in text:
                detail=text[text.index(marker):]
                if detail not in details:details.append(detail)
                break
    return "\n".join(details)



def ai_result_headline(result):
    """Keep result state truthful across the AI dialog and gallery return."""
    if result.history_interrupted:
        return "AI run needs saved-history recovery"
    if result.cancelled:
        return "AI run cancelled"
    errors=[*result.errors,*(error for outcome in result.outcomes for error in outcome.errors)]
    if memory_hold_detail(errors):
        return "AI held for GPU memory"
    counts=result.outcome_counts()
    unfinished=sum(counts.get(key,0) for key in ("failed","partial","conflicted"))
    if unfinished or errors:
        if (counts.get("failed",0) or errors) and not any(
            counts.get(key,0) for key in ("completed","skipped","partial","conflicted")
        ):
            return "AI run failed"
        return "AI run needs attention"
    return "AI run complete"


class TaskMemoryHoldPanel(QGroupBox):
    """Visible recovery for one held request; consent is never a saved setting."""
    def __init__(self,parent=None):
        super().__init__("AI held for GPU memory",parent)
        layout=QFormLayout(self)
        self.detail=QLabel();self.detail.setWordWrap(True)
        self.detail.setAccessibleName("GPU memory hold and requested image scope")
        layout.addRow(self.detail)
        self.other_failures=QLabel();self.other_failures.setWordWrap(True)
        self.other_failures.setAccessibleName("Other preparation failures")
        self.other_failures.hide();layout.addRow(self.other_failures)
        self.results_button=QPushButton("Review All Preparation Outcomes...")
        self.results_button.hide();layout.addRow(self.results_button)
        self.results_button.clicked.connect(self._show_outcomes)
        self._result_report=None
        from PySide6.QtWidgets import QHBoxLayout
        row=QHBoxLayout()
        self.recheck=QPushButton("Recheck VRAM")
        self.retry=QPushButton("Retry Safely")
        self.override=QPushButton("Run Anyway / Parallel...")
        self.dismiss=QPushButton("Dismiss Hold")
        for button in (self.recheck,self.retry,self.override,self.dismiss):row.addWidget(button)
        layout.addRow(row)
        self.recheck.clicked.connect(self._recheck)
        self.retry.clicked.connect(lambda:self._retry(False))
        self.override.clicked.connect(lambda:self._retry(True))
        self.dismiss.clicked.connect(self.clear)
        self._request=None;self.hide()

    def clear(self):
        self._request=None;self._result_report=None
        self.other_failures.hide();self.results_button.hide();self.hide()

    def report_failures(self,errors,result=None):
        import copy
        others=tuple(dict.fromkeys(str(error) for error in errors if not memory_hold_detail([error])))
        self.other_failures.setText(
            f"Other preparation failures ({len(others):,}):\n"+"\n".join(others[:3])
            +(f"\n{len(others)-3:,} more; review all outcomes for full details." if len(others)>3 else "")
            if others else ""
        )
        self.other_failures.setVisible(bool(others))
        self._result_report=copy.deepcopy(result) if result is not None else None
        self.results_button.setVisible(result is not None)

    def _show_outcomes(self):
        if self._result_report is None:return
        from .preparation_result_dialog import PreparationResultDialog
        dialog=PreparationResultDialog(self._result_report,parent=self)
        # This report belongs to an already-open image editor. Closing it
        # returns to that draft; it must not open another editor/review queue.
        dialog.review_button.hide()
        dialog.exec()

    def hold(self,detail,settings,options,*,scope_description,retry,is_current,settings_snapshot=None):
        import copy
        from dataclasses import replace
        self.other_failures.hide();self.results_button.hide();self._result_report=None
        self._request=(settings,copy.deepcopy(vars(settings) if settings_snapshot is None else settings_snapshot),replace(options,allow_memory_pressure=False),retry,is_current,scope_description)
        self.detail.setText("Scope: "+scope_description+"\n\n"+detail+
            "\n\nRequest held. Completed results remain saved; editor changes remain staged. "
            "Retry is manual. Estimates are uncertain. No external apps or active tasks are cancelled or unloaded.")
        for button in (self.recheck,self.retry,self.override):button.setEnabled(True)
        self.show()

    def _current(self):
        request=self._request
        if request is None:return False
        settings,snapshot,options,retry,is_current,scope=request
        if vars(settings)!=snapshot or not is_current():
            self.detail.setText("Request changed. No retry started. Review the current image scope and AI settings, then start a new request. Editor changes are retained.")
            for button in (self.recheck,self.retry,self.override):button.setEnabled(False)
            return False
        return True

    def _recheck(self):
        if not self._current():return
        request=self._request
        text=observe_preparation_memory(self,request[0],request[2])
        if self._request is not request or not self._current():return
        if text is not None:
            self.detail.setText("Scope: "+request[5]+"\n\n"+text+"\n\nNo inference started. Choose Retry Safely or review Run Anyway explicitly.")

    def _retry(self,override):
        from dataclasses import replace
        if not self._current():return
        request=self._request
        if override and not review_preparation_memory_override(self,request[0],request[2],scope_description=request[5]):return
        if self._request is not request or not self._current():return
        callback=request[3];options=replace(request[2],allow_memory_pressure=bool(override))
        self.clear()
        callback(options)


def show_editor_preparation_memory_hold(owner,editor,errors,retry,*,result=None):
    """Shared editor recovery; the existing page remains the task/write owner."""
    detail=memory_hold_detail(errors)
    request=getattr(editor,"_memory_preparation_request",None)
    if not detail and result is not None and getattr(editor,"_memory_hold_feedback_pending",False):
        # Admission hides the previous hold panel, but its bound footer label
        # survives. Replace that notice with this request's actual outcome.
        unfinished=bool(errors) or any(
            outcome.get("status") in {"failed","partial","conflicted"}
            for outcome in result.get("outcomes",()) if isinstance(outcome,dict)
        )
        title=("Image preparation paused safely" if result.get("cancelled") else
               "Image preparation needs attention" if unfinished else
               "Image preparation complete")
        editor.save_status.setText(title+". Image draft retained."+
            (" Review preparation outcomes for remaining problems." if unfinished else ""))
        editor.save_status.show()
        editor._memory_hold_feedback_pending=False
        from .components import refresh_status_footer
        refresh_status_footer(editor)
    if not detail or request is None or not hasattr(editor,"memory_hold"):return False
    options,paths,fingerprints,settings_snapshot=request
    controls=getattr(editor,"preparation_controls",None)
    baseline=controls.options() if controls is not None else None
    editor.memory_hold.hold(detail,owner.settings,options,
        scope_description=f"{len(paths):,} image(s); original preparation stages and quality unchanged",
        settings_snapshot=settings_snapshot,
        retry=lambda current:retry(current,paths,fingerprints),
        is_current=lambda:editor is owner._active_editor and tuple(paths)==(editor.record.image_path,) and all(path.is_file() for path in paths) and (controls is None or controls.options()==baseline),
    )
    editor.memory_hold.report_failures(errors,result)
    editor._memory_hold_feedback_pending=True
    editor.save_status.setText("AI held for GPU memory. Review the visible memory choices; image draft retained.")
    editor.save_status.show()
    return True
