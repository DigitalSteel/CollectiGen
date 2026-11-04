"""History result custody using the canonical coordinator's execution scope."""
from PySide6.QtCore import QObject,QTimer,Qt
from shiboken6 import isValid
from threading import Event

from .workers import CoordinatedCallTask


class HistoryTaskLifetime(QObject):
    """Retain one original return until its scoped worker physically drains."""

    def __init__(self,owner,manager,title,operation,on_drained,on_hold):
        super().__init__(owner)
        self.owner=owner;self.manager=manager;self.on_drained=on_drained;self.on_hold=on_hold
        self.scope=manager.create_execution_scope()
        self.receipt=dict(returned=False,value=None,error=None,job=None)
        self.stop_requested=Event()
        self.started=False;self.submitting=False;self.settled=False;self.blocked=False
        self._notice_text=None
        receipt,stop=self.receipt,self.stop_requested
        def work(job):
            receipt['job']=job;receipt['job_id']=job.id
            try:value=operation(job,lambda:stop.is_set() or job.cancel_requested)
            except BaseException as exc:
                receipt['error']=exc
                raise
            receipt['value']=value;receipt['returned']=True
            return value
        self.task=CoordinatedCallTask(manager,title,work,resource="io",execution_scope=self.scope)
        self.task.signals.complete.connect(lambda _:self._terminal("complete"),Qt.QueuedConnection)
        self.task.signals.failed.connect(lambda error:self._terminal("failed",error),Qt.QueuedConnection)
        self.task.signals.cancelled.connect(lambda:self._terminal("cancelled"),Qt.QueuedConnection)
        self.timer=QTimer(self);self.timer.setInterval(25);self.timer.timeout.connect(self.poll)

    def _notice(self,message):
        if message==self._notice_text:return
        self._notice_text=message
        if isValid(self.owner):
            try:self.on_hold(message)
            except Exception as exc:self.receipt['notice_error']=exc

    def start(self):
        if self.started:return
        self.started=True;self.submitting=True;self.timer.start()
        try:self.task.start()
        except Exception as exc:
            self.receipt['admission_error']=exc
            self._notice("History work could not be confirmed. Keep this window open; no retry will run.")
        finally:
            if self.task.job_id:self.receipt.setdefault('job_id',self.task.job_id)
            try:self.scope.pause_admission()
            except Exception as exc:
                self.receipt['scope_error']=exc;self.blocked=True;self.timer.stop()
                self._notice("History work status needs attention. Keep this window open; no retry will run.")
            self.submitting=False
        self.poll()

    def cancel(self):
        self.stop_requested.set()
        self._send_cancel()

    def _send_cancel(self):
        job_id=self.receipt.get('job_id')
        if not job_id or self.receipt.get('cancel_sent'):return
        self.receipt['cancel_sent']=True
        try:self.manager.cancel(job_id)
        except Exception as exc:self.receipt['cancel_error']=exc

    def _terminal(self,kind,error=""):
        self.receipt['terminal']=(kind,error)
        if self.settled:return
        self.poll()

    def poll(self):
        if (not isValid(self) or not isValid(self.owner) or not self.started
                or self.submitting or self.settled or self.blocked):return
        if self.stop_requested.is_set():self._send_cancel()
        try:drained=self.scope.physically_drained()
        except Exception as exc:
            self.receipt['scope_error']=exc;self.blocked=True;self.timer.stop()
            self._notice("History work status needs attention. Keep this window open; no retry will run.")
            return
        if not drained:
            if 'terminal' in self.receipt:
                self._notice("Finishing the original history work. Keep this window open until it finishes.")
            return
        if (not self.receipt['returned'] and self.receipt['error'] is None
                and 'terminal' not in self.receipt and 'admission_error' not in self.receipt):return
        self.settled=True;self.timer.stop()
        try:self.on_drained(self)
        except Exception as exc:
            self.receipt['presentation_error']=exc
            self._notice("The original history result is retained, but its display needs attention. Do not repeat the action.")
