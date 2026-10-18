"""Reviewed legacy launch, responsive preparation, explicit close-and-open."""
from time import monotonic
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QHBoxLayout
from qt_app.components import IndeterminateActivityBar
from .resource_executor import ResourceExecutorOwner
from .ui_cards import workspace_layout, workspace_heading, compact_action
from .bootstrap_entry import LegacyReaderPrepareNotSent


class _HandoffDialog(QDialog):
    def __init__(self, flow):
        super().__init__(flow.window)
        self.flow=flow
        self.setWindowTitle('Open Reader')
        self.setWindowModality(Qt.ApplicationModal)
        self.resize(560,300)
        layout=workspace_layout(self)
        layout.addWidget(workspace_heading('Open Reader '+flow.selected.version.split('-')[0]))
        self.detail=QLabel('This installation uses its original standalone launcher. Manager will close before the Reader opens, then reopen after you close the Reader. Catalogs and other settings are kept.')
        self.detail.setWordWrap(True);layout.addWidget(self.detail)
        self.activity=IndeterminateActivityBar(self);layout.addWidget(self.activity);self.activity.hide()
        self.elapsed=QLabel();layout.addWidget(self.elapsed);self.elapsed.hide()
        self.status=QLabel('First, check the saved installation and prepare its Reader connection.')
        self.status.setWordWrap(True);layout.addWidget(self.status)
        row=QHBoxLayout();layout.addLayout(row)
        row.addStretch()
        self.primary=compact_action(QPushButton('Prepare Reader'));self.primary.setDefault(True);row.addWidget(self.primary)
        self.cancel=compact_action(QPushButton('Cancel'));self.cancel.setAutoDefault(False);row.addWidget(self.cancel)
        self.primary.clicked.connect(flow.advance)
        self.cancel.clicked.connect(flow.cancel)
    def closeEvent(self,event):
        if self.flow.state in {'review','done'}:event.accept()
        else:
            event.ignore()
            self.flow.cancel()
    def reject(self):self.close()


class LegacyReaderHandoffFlow:
    def __init__(self,window,session):
        self.window,self.session=window,session
        self.executor=ResourceExecutorOwner()
        self.dialog=None
        self.state='review'
        self.future=None
        self.handoff_id=None
        self.cancel_pending=False
        self.started=None
        self.timer=QTimer(window);self.timer.setInterval(100);self.timer.timeout.connect(self.poll)

    @property
    def close_held(self):
        return self.executor.close_held or self.state in {'preparing','prepared','committing','cancelling','held'}

    def open(self,selected):
        if self.close_held:raise ValueError('The original legacy handoff is still pending.')
        if self.session.reader_selection.selected != selected:
            raise ValueError('Restart Manager to admit this Reader selection.')
        self.state='review';self.cancel_pending=False;self.handoff_id=None
        self.selected=selected
        self.dialog=_HandoffDialog(self);self.dialog.show()

    def _submit(self,operation,state,message):
        self.state=state;self.started=monotonic()
        self.dialog.status.setText(message)
        self.dialog.primary.setEnabled(False)
        self.dialog.cancel.setEnabled(operation!='commit')
        self.dialog.activity.show();self.dialog.elapsed.show()
        self.dialog.activity.set_active(True,message)
        self.future=self.executor.submit(self.session.request_legacy_reader,operation,self.handoff_id)
        self.timer.start()

    def advance(self):
        if self.state=='review':
            self._submit('prepare','preparing','Checking the original Reader and preparing its saved connection…')
        elif self.state=='prepared':
            self._submit('commit','committing','Preparing to close Manager and open the Reader…')

    def cancel(self):
        if self.state in {'review','done'}:
            self.state='done';self.dialog.close();return
        if self.state=='preparing':
            self.cancel_pending=True
            self.dialog.status.setText('Cancel requested. Waiting for the original preparation to settle safely…')
        elif self.state=='prepared':
            self._submit('cancel','cancelling','Cancelling the prepared Reader handoff…')
        # A sent commit or uncertain reply cannot be dismissed or replayed.

    def _refused(self,message):
        self.state='done';self.started=None;self.timer.stop()
        self.dialog.activity.set_active(False)
        self.dialog.activity.hide();self.dialog.elapsed.hide()
        self.dialog.primary.setEnabled(False)
        self.dialog.cancel.setText('Close');self.dialog.cancel.setEnabled(True)
        self.dialog.status.setText('The Reader could not be prepared. Manager stays open. '+message)

    def poll(self):
        if self.started is not None:
            seconds=max(0,int(monotonic()-self.started))
            self.dialog.elapsed.setText(f'{seconds // 60}m {seconds % 60:02d}s elapsed')
        if self.future is None or not self.future.done() or self.executor.close_held:return
        future,self.future=self.future,None
        try:
            result=future.result()
            if result['status']=='refused':
                self._refused(result['reason'])
                return
            self.handoff_id=result['handoff_id']
            self.dialog.activity.set_active(False)
            self.dialog.activity.hide();self.dialog.elapsed.hide()
            if self.state=='preparing':
                self.state='prepared'
                if self.cancel_pending:
                    self.cancel();return
                self.dialog.status.setText('Reader checks passed. Close Manager and open this Reader when ready.')
                self.dialog.primary.setText('Close Manager and open Reader')
                self.dialog.primary.setEnabled(True)
                self.dialog.cancel.setEnabled(True)
                self.started=None;self.timer.stop()
            elif self.state=='committing':
                if not result['committed'] or result['consumed']:raise ValueError('The Reader handoff was not committed.')
                self.state='closing'
                self.dialog.hide()
                if not self.window.close():
                    # Closing was declined/held. Cancel before another normal
                    # Close can accidentally execute a previously armed launch.
                    self.state='cancelling'
                    self.window.cancel_pending_close()
                    self.dialog.show()
                    self._submit('cancel','cancelling','Manager stayed open. Cancelling this Reader launch…')
                    return
                self.timer.stop()
                self.executor.begin_close()
            elif self.state=='cancelling':
                if result['committed'] or not result['consumed']:raise ValueError('Reader cancellation remains unconfirmed.')
                self.state='done';self.timer.stop();self.dialog.close()
        except LegacyReaderPrepareNotSent as error:
            self._refused(str(error))
        except BaseException as error:
            self.state='held';self.timer.stop()
            self.dialog.activity.set_active(False)
            self.dialog.activity.hide();self.dialog.elapsed.hide()
            self.dialog.primary.setEnabled(False);self.dialog.cancel.setEnabled(False)
            self.dialog.status.setText('The original Reader handoff needs attention. Keep Manager open; no replacement launch will be started. '+str(error))
            self.dialog.show()
