"""Exact coordinated work stays visible in the window that started it."""
import time

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QProgressBar, QPushButton

from .components import ElidingLabel, IndeterminateActivityBar, bind_status_footer, set_progress_bar


class WindowJobProgress(QObject):
    """One reusable presentation binding; the existing manager owns all work."""

    def __init__(self, owner):
        super().__init__(owner)
        self.owner=owner
        self.task=None
        self.phase=ElidingLabel(parent=owner)
        self.phase.setAccessibleName("Current operation and phase")
        self.bar=QProgressBar(owner)
        self.bar.setAccessibleName("Current operation progress")
        self.activity=IndeterminateActivityBar(owner)
        self.stop=QPushButton("Cancel Safely",owner)
        self.stop.clicked.connect(self.cancel)
        self._external_cancel=None
        self._started=0.0
        self._title=""
        self._unit="items"
        for widget in (self.phase,self.bar,self.activity,self.stop):widget.hide()
        self.binding=bind_status_footer(owner,self.phase,self.bar,self.activity,self.stop,
            follow_visibility=True)
        self.timer=QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.refresh)

    def attach(self,task,*,title,unit,cancel_button=None):
        if self.task is not None and self.task is not task:
            previous=self.task.manager.get(self.task.job_id) if self.task.job_id else None
            if previous is None or previous.state in {"queued","running","paused"}:
                raise RuntimeError("The window already has an active operation progress owner.")
        self.task=task
        self._title=str(title or task.name)
        self._unit=str(unit or "items")
        self._started=time.monotonic()
        self._external_cancel=cancel_button
        task.signals.complete.connect(lambda *_args:self.finish(task))
        task.signals.failed.connect(lambda *_args:self.finish(task))
        task.signals.cancelled.connect(lambda:self.finish(task))
        self.refresh()
        self.timer.start()
        return self

    def refresh(self):
        task=self.task
        if task is None:return
        job=task.manager.get(task.job_id) if task.job_id else None
        if job is not None and job.state in {"complete","cancelled","failed"}:
            self.finish(task);return
        elapsed=max(0,int(time.monotonic()-self._started))
        elapsed_text=f"{elapsed//60}m {elapsed%60:02d}s" if elapsed>=60 else f"{elapsed}s"
        current,total=(job.current,job.total) if job is not None else (0,0)
        detail=str(job.message or self._title) if job is not None else self._title
        waiting=job is not None and job.state=="queued"
        stopping=job is not None and job.cancel_requested
        prefix="Waiting for background capacity" if waiting else "Stopping at a safe boundary" if stopping else self._title
        text=prefix if detail==prefix else prefix+" · "+detail
        if any(marker in detail for marker in ("WD inference ETA:","JoyCaption inference ETA:","Task ETA:")):
            from core.inference_timing import compact_inference_progress
            text=prefix+" \u2022 "+compact_inference_progress(detail)
        self.phase.setText(text)
        self.phase.setToolTip(prefix+" • "+detail)
        self.phase.setAccessibleDescription(prefix+" • "+detail)
        scope="Elapsed "+elapsed_text
        queue_seconds=max(0,int(getattr(job,"queue_wait_seconds",0))) if job is not None else 0
        if queue_seconds:scope+=f" • Background queue wait: {queue_seconds}s (excluded from inference ETA)"
        description=prefix+" • "+detail+"\n"+scope
        set_progress_bar(self.bar,current,total,detail=description,unit=self._unit,scope=scope,
            unknown_text=scope+" · Total not yet known")
        self.bar.setAccessibleDescription(description)
        self.phase.show();self.bar.show()
        self.activity.set_active(not bool(total),description)
        self.activity.setVisible(not bool(total))
        self.stop.setVisible(self._external_cancel is None)
        self.stop.setEnabled(not stopping)
        if self._external_cancel is not None:
            self._external_cancel.setToolTip("Stop this operation at its next safe boundary.")

    def cancel(self):
        task=self.task
        if task is not None and task.job_id:
            # Cancel the job, retaining its observer and the owner's final result.
            task.manager.cancel(task.job_id)
            self.refresh()

    def finish(self,task):
        if self.task is not task:return
        self.task=None
        self.timer.stop()
        self.activity.set_active(False)
        for widget in (self.phase,self.bar,self.activity,self.stop):widget.hide()


def bind_job_progress(owner,task,*,title,unit="items",cancel_button=None):
    """Bind before starting a task; reuse the owner's one visible footer row.

    Optional ``cancel_button`` is already wired by its owning workflow; otherwise
    the shared row supplies cancellation without detaching completion delivery.
    Callers continue to own safe closing, errors, results and subsequent steps.
    """
    binding=getattr(owner,"_window_job_progress",None)
    if binding is None:
        binding=owner._window_job_progress=WindowJobProgress(owner)
    return binding.attach(task,title=title,unit=unit,cancel_button=cancel_button)
