"""Explicit signed update review over the existing acquisition service.

Only discovery and cache staging run here. Deployment apply/repair authority
stays with the original launcher and its retained transaction.
"""
from threading import Event, get_ident

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QGroupBox, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QPlainTextEdit

from core.manager_update_service import ManagerUpdateService, PreparedManagerUpdate, reviewed_stage_identity
from core.component_acquisition import ComponentOffer
from .resource_executor import ResourceExecutorOwner


class ManagerUpdateReviewPanel(QGroupBox):
    close_for_apply = Signal()

    def __init__(self, service, parent=None, *, executor=None, normal_entry_session=None):
        if type(service) is not ManagerUpdateService:
            raise ValueError('explicit_manager_update_service_required')
        super().__init__('Manager updates', parent)
        self.service = service
        self.session = normal_entry_session
        if self.session is not None:
            from .bootstrap_entry import NormalEntrySession
            if type(self.session) is not NormalEntrySession or self.session.state not in {'offered','ready'}:
                raise ValueError('original_normal_update_session_required')
            if getattr(self.session,'update_profile',None) is None:
                raise ValueError('original_update_host_profile_required')
        self.context = (service.current_version, service.channel, service.acquisition)
        self.executor = ResourceExecutorOwner() if executor is None else executor
        if type(self.executor) is not ResourceExecutorOwner:
            raise ValueError('existing_update_worker_owner_required')
        self.ui_owner = get_ident()
        self.future = self.offer = self.prepared = None
        self.cancelled = Event()
        self.closing = False
        self.operation = None
        self.message = 'Check for a signed update when ready. Nothing is checked automatically.'
        layout = QVBoxLayout(self)
        self.scope = QLabel(f'Manager {service.current_version} · {service.channel} channel')
        self.status = QLabel(); self.status.setObjectName('managerUpdateStatus')
        self.review = QPlainTextEdit(); self.review.setObjectName('managerUpdateReview')
        self.review.setReadOnly(True); self.review.setMaximumHeight(160)
        self.review.setAccessibleName('Signed Manager update notes')
        for label in (self.scope, self.status):
            label.setWordWrap(True); label.setTextFormat(Qt.PlainText); layout.addWidget(label)
        layout.addWidget(self.review)
        actions = QHBoxLayout()
        self.check = QPushButton('Check for updates')
        self.stage = QPushButton('Stage reviewed update')
        self.apply = QPushButton('Apply staged update')
        self.cancel = QPushButton('Cancel')
        for name, button in (('Check', self.check), ('Stage', self.stage), ('Apply', self.apply), ('Cancel', self.cancel)):
            button.setObjectName('managerUpdate' + name); actions.addWidget(button)
        layout.addLayout(actions)
        consequence = QLabel('Staging verifies a package in the update cache. It does not replace Manager. '
            'Apply and repair require the launcher’s separately qualified closure and recovery authority.')
        consequence.setWordWrap(True); consequence.setTextFormat(Qt.PlainText); layout.addWidget(consequence)
        self.check.clicked.connect(lambda: self.request('discover'))
        self.stage.clicked.connect(lambda: self.request('stage'))
        self.apply.clicked.connect(lambda: self.request('apply'))
        self.apply.setVisible(self.session is not None)
        self.cancel.clicked.connect(self.cancel_request)
        self.timer = QTimer(self); self.timer.setInterval(50); self.timer.timeout.connect(self.poll)
        self.render()

    @property
    def close_held(self):
        return self.future is not None or self.executor.close_held

    def context_matches(self):
        return self.context == (self.service.current_version, self.service.channel, self.service.acquisition)

    def request(self, operation):
        if not self.context_matches():
            self.offer = self.prepared = None
            self.review.clear()
            self.message = 'Update configuration changed. Reopen Manager with its exact installed version and channel.'
            self.render()
            return False
        if self.closing or self.close_held or operation not in ('discover', 'stage', 'apply'):
            return False
        if operation == 'apply' and (self.session is None or self.prepared is None):
            return False
        if operation == 'stage' and (self.offer is None or self.prepared is not None):
            return False
        self.cancelled = Event()
        cancelled, offer, prepared = self.cancelled, self.offer, self.prepared
        if operation == 'discover':
            self.offer = self.prepared = None
            self.review.clear()
        self.operation = operation
        self.message = 'Checking signed releases…' if operation == 'discover' else 'Verifying and staging the reviewed package…'
        def work():
            if get_ident() == self.ui_owner:
                raise RuntimeError('Update work must run outside the UI thread.')
            if operation == 'apply':
                self.service.revalidate(prepared)
                if cancelled.is_set(): raise InterruptedError('Apply review cancelled before submission.')
                return self.session.request_manager_update('apply', reviewed_stage_identity(prepared),
                    profile_sha256=self.session.update_profile['profile_sha256'])
            return (self.service.discover(should_cancel=cancelled.is_set) if operation == 'discover' else
                    self.service.prepare(offer, should_cancel=cancelled.is_set))
        self.future = self.executor.submit(work)
        self.timer.start(); self.render()
        return True

    def cancel_request(self):
        if self.future is not None and self.operation != 'apply':
            self.cancelled.set()
            self.message = 'Cancellation requested. Waiting for verified worker completion.'
            self.render()

    def poll(self):
        future = self.future
        # A completed Future alone does not prove the retained thread drained.
        if future is None or not future.done() or self.executor.close_held:
            return
        self.future = None
        if self.cancelled.is_set() or not self.context_matches():
            self.offer = self.prepared = None
            self.review.clear()
            self.message = ('Cancelled. Check again to review a fresh update.' if self.context_matches() else
                'Update configuration changed. Reopen Manager to review fresh evidence.')
        else:
            try:
                result = future.result()
                if self.operation == 'discover':
                    if result is not None and type(result) is not ComponentOffer:
                        raise ValueError('Invalid signed update offer.')
                    self.offer = result
                    self.message = 'No newer signed Manager update found.' if result is None else 'Signed update found. Review its version and notes before staging.'
                    if result is not None:
                        self.review.setPlainText(f'{self.service.current_version} → {result.release.software_version}\n'
                            f'{result.release.artifact.size_bytes:,} bytes\n\n{result.release_notes}')
                elif self.operation == 'apply':
                    if (type(result) is not dict or result.get('status') != 'reviewed'
                            or result.get('result') != {'action':'close_for_apply'}):
                        reason = result.get('result',{}).get('reason','Original launcher held Apply.') if type(result) is dict else 'Invalid launcher reply.'
                        raise ValueError(reason)
                    self.message = 'Apply accepted. Closing Manager through its normal cleanup before replacement.'
                    self.timer.stop(); self.render()
                    self.close_for_apply.emit()
                else:
                    if type(result) is not PreparedManagerUpdate or result.offer is not self.offer:
                        raise ValueError('Reviewed update identity changed.')
                    self.prepared = result
                    self.message = 'Package staged and signature, inventory and bytes verified. Manager has not been replaced.'
            except BaseException as error:
                self.offer = self.prepared = None; self.review.clear()
                self.message = 'Update review failed: ' + str(error)[:240] + '\nCheck again to obtain fresh evidence.'
        self.timer.stop(); self.render()

    def permit_close(self):
        if self.future is not None and self.operation == 'apply':
            self.message = 'Waiting for the original launcher Apply decision before closing.'
            self.render()
            return False
        self.closing = True
        self.cancel_request()
        self.executor.begin_close()
        self.poll()
        self.render()
        if self.close_held:
            return False
        self.timer.stop()
        return True

    def render(self):
        busy = self.close_held
        self.status.setText(self.message)
        self.check.setEnabled(not self.closing and not busy and self.context_matches())
        self.stage.setEnabled(not self.closing and not busy and self.context_matches() and self.offer is not None and self.prepared is None)
        self.apply.setEnabled(not self.closing and not busy and self.context_matches() and self.prepared is not None and self.session is not None)
        self.cancel.setEnabled(not self.closing and self.future is not None and self.operation != 'apply' and not self.cancelled.is_set())
