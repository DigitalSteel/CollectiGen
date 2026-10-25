"""Modeless presentation for one explicitly qualified app-owned runtime binding.

No acquisition, native calls, settings writes, model actions or worker scheduler.
The existing binding owns publication, registry admission and retained processes.
"""
import json
import time
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QDialog, QDialogButtonBox, QCheckBox, QProgressBar, QSizePolicy)
from core.manager_ollama_runtime import ManagerOllamaRuntimeBinding
from core.ollama_cache_admission import OwnedCacheAdmission
from core.ollama_package_preparation import OllamaZipDescriptor
from suite_core.backend_contracts import CapabilityId
from suite_core.manager.ui_cards import compact_action


def qualification(binding, descriptor, allowed):
    if binding is None:
        return "Supply a qualified owned runtime package to enable installation."
    if type(binding) is not ManagerOllamaRuntimeBinding:
        raise ValueError("typed_owned_runtime_binding_required")
    if type(descriptor) is not OllamaZipDescriptor or allowed is not True:
        return "An exact qualified package descriptor and installation permission are required."
    try:
        manifest = json.loads(binding.plan.manifest_bytes)
        exact = (manifest['software_version'] == descriptor.version == binding.plan.registration.software_version
                 and manifest['archive_sha256'] == descriptor.archive_sha256
                 and CapabilityId('backend.metadata') in binding.plan.registration.capabilities)
    except (ValueError, KeyError, TypeError):
        exact = False
    return "" if exact else "Package descriptor, publication plan or metadata capability does not match."


class OwnedRuntimePanel(QWidget):
    def __init__(self, binding, descriptor, allowed, parent=None, registry_admitted=None):
        super().__init__(parent)
        self.binding = binding
        self.unavailable = qualification(binding, descriptor, allowed)
        self.registry_admitted = registry_admitted
        ticket = None if binding is None else binding.pending_ticket
        self.pending = None if ticket is None else ticket.operation.removeprefix('ollama_runtime_')
        completion = None if binding is None else binding.last_completion
        self.snapshot = None if completion is None else completion.snapshot
        self.dialog = None
        self.start_review = None
        self.changed = None
        self.recovery_mode = False
        self.recovery_retry_allowed = False
        self.recovery_finished = None
        self.first_failure = ((binding.worker_diagnostic or {}).get('cause_code') or completion.error
            if completion is not None and completion.error else None)
        self.helper_observation = None
        self._observation_identity = None
        self._observation_failed = False
        self._next_observation = 0
        self.recovery_claim = None
        self.recovery_claimed = False
        self.message = self.unavailable or (
            "Retained operation is still running; wait or cancel. Ownership remains retained." if ticket is not None else
            "Retained owned state restored. Check owned status; explicitly Stop any retained runner before close." if completion is not None else
            "Check owned status or review installation. No runner has been started.")
        body = QVBoxLayout(self)
        heading = QLabel("Manager-owned runner"); heading.setObjectName('cardTitle'); body.addWidget(heading)
        self.recovery_reason = QLabel(); self.recovery_reason.setWordWrap(True)
        self.recovery_reason.setTextFormat(Qt.PlainText); self.recovery_reason.hide(); body.addWidget(self.recovery_reason)
        self.status = QLabel(); self.status.setWordWrap(True); self.status.setTextFormat(Qt.PlainText); self.status.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum); body.addWidget(self.status)
        self.details = QLabel(); self.details.setWordWrap(True); self.details.setTextFormat(Qt.PlainText); self.details.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum); body.addWidget(self.details)
        self.details_toggle = compact_action(QPushButton('Details'))
        self.details_toggle.setCheckable(True); self.details_toggle.hide(); body.addWidget(self.details_toggle)
        self.recovery_details = QLabel(); self.recovery_details.setWordWrap(True)
        self.recovery_details.setTextFormat(Qt.PlainText); self.recovery_details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.recovery_details.hide(); body.addWidget(self.recovery_details)
        self.details_toggle.toggled.connect(self.recovery_details.setVisible)
        controls = QHBoxLayout()
        self.install = compact_action(QPushButton('Install new'))
        self.check = compact_action(QPushButton('Check owned status'))
        self.start = compact_action(QPushButton('Start owned'))
        self.stop = compact_action(QPushButton('Stop owned'))
        self.cancel = compact_action(QPushButton('Cancel operation'))
        self.recover = compact_action(QPushButton('Review recovery'))
        # Wrapping into two rows keeps every action reachable at narrow widths.
        for button in (self.install, self.check, self.start): controls.addWidget(button)
        controls.addStretch(); body.addLayout(controls)
        second = QHBoxLayout()
        for button in (self.stop, self.cancel, self.recover): second.addWidget(button)
        second.addStretch(); body.addLayout(second)
        self.helper_recover = compact_action(QPushButton('Check retained start attempt'))
        body.addWidget(self.helper_recover)
        self.progress = QProgressBar(); self.progress.setRange(0, 0); self.progress.hide(); body.addWidget(self.progress)
        models = QLabel('Model installation and Hermes agent actions are unavailable until separately qualified.')
        models.setWordWrap(True); body.addWidget(models)
        self.models_guidance = models
        self.install.clicked.connect(self.review_install)
        self.check.clicked.connect(lambda: self.submit('registry'))
        self.start.clicked.connect(lambda: self.submit('review_start'))
        self.stop.clicked.connect(self.review_stop)
        self.cancel.clicked.connect(self.cancel_operation)
        self.recover.clicked.connect(lambda: self.review_install(recovery=True))
        self.helper_recover.clicked.connect(self.review_helper_accounting)
        self.timer = QTimer(self); self.timer.setInterval(50); self.timer.timeout.connect(self.poll)
        if binding is not None: self.timer.start()
        self.render()

    @property
    def ready(self):
        return not self.unavailable and self.binding is not None and not self.binding.closed

    def enter_recovery(self, reason, *, retry_allowed=False, finished=None):
        """Re-present the same ticket and binding; never create a second owner."""
        self.recovery_mode = True
        self.recovery_retry_allowed = retry_allowed is True
        self.recovery_finished = finished
        if self.first_failure is None: self.first_failure = str(reason)[:512]
        self.details_toggle.show()
        self.render()

    @property
    def busy(self):
        return self.pending is not None or (self.binding is not None and self.binding.executor.close_held)

    @property
    def installed(self):
        return self.snapshot is not None and self.snapshot.registry is not None and self.binding.plan.registration in self.snapshot.registry.entries

    def render(self):
        available = self.ready and not self.busy and self.dialog is None and self.start_review is None
        owned = self.snapshot is not None and self.snapshot.ownership_held
        reconciled = self.snapshot is None or not self.snapshot.reconciliation_required
        helper_identity = None if not self.ready else self.binding.helper_accounting_identity
        # A verified running helper normally retains its accounting identity until
        # Stop. Only an incomplete start requires original-attempt recovery.
        helper_held = helper_identity is not None and not (
            owned and self.snapshot.state == 'running' and self.snapshot.process_pin is not None)
        self.install.setEnabled(available and reconciled and not self.installed and not owned and not (self.snapshot and self.snapshot.state == 'install_held'))
        self.check.setEnabled(available)
        self.start.setEnabled(available and reconciled and self.installed and not owned and not helper_held)
        self.stop.setEnabled(available and owned and self.snapshot.process_pin is not None)
        self.cancel.setEnabled(self.pending is not None)
        self.cancel.setVisible(self.pending is not None)
        self.recover.setVisible(self.snapshot is not None and self.snapshot.state == 'install_held')
        self.recover.setEnabled(available and not owned)
        self.helper_recover.setVisible(helper_held and not owned)
        self.helper_recover.setEnabled(available and helper_held and not owned)
        self.progress.setVisible(self.busy)
        self.status.setText(self.message)
        if self.snapshot is None:
            self.details.setText('Owned installation status unknown. Existing connections remain external instances.')
        else:
            state = ('Runner exited; accounting pending' if owned and self.snapshot.process_state=='exited' else
                     'Created runner retained; helper accounting unresolved; explicit Stop required' if helper_held and owned else
                     'Start attempt unresolved; check the retained attempt before another start or close' if helper_held else
                     'Owned registration unavailable; reconcile before another start' if self.snapshot.registry is None else
                     'Running at last verified start; current health not refreshed' if self.snapshot.state == 'running' else
                     'Runner alive at last observation; explicit Stop required' if owned and self.snapshot.process_state=='alive' else
                     'Ownership retained; process state unconfirmed; explicit Stop required' if owned else
                     'Installed; stopped' if self.installed else 'No registered owned installation')
            consequence=('Stop drains original accounting and stops any remaining owned descendants.'
                if self.snapshot.process_state=='exited' else
                'Stop terminates only the owned runner and its owned descendants.')
            self.details.setText(
                f'{state} · Ollama {self.snapshot.runtime_version}\n'
                f'{self.snapshot.endpoint} · PID {self.snapshot.pid if owned else "none retained"}\n'
                f'{consequence} External instances are never stopped.')
        if helper_held:
            self.status.setText(
                'Runner has exited; Manager retains its handles and unresolved accounting. Choose Stop to finish original accounting before another start or close.' if owned and self.snapshot.process_state=='exited' else
                'Manager retains the created runner and unresolved helper accounting. Stop the retained runner; accounting must finish before another start or close.' if owned else
                'Manager retains this unresolved start attempt. Check retained start attempt to reconcile its original reply; no child PID is confirmed.')
        if self.recovery_mode:
            self.recovery_reason.setText({
                'loopback_port_in_use':'Ollama could not start because its local address is already in use.',
                'retained_helper_reply_deadline':'Ollama could not finish starting because its startup reply was not confirmed.',
            }.get(self.first_failure, 'Ollama could not finish the requested operation.'))
            self.recovery_reason.show()
            self.install.hide(); self.check.hide(); self.recover.hide(); self.models_guidance.hide()
            self.start.setText('Retry starting Ollama')
            self.start.setVisible(self.recovery_retry_allowed and not owned and not helper_held)
            self.start.setEnabled(self.start.isEnabled() and self.recovery_retry_allowed)
            self.stop.setText('Stop background server and close'); self.stop.setVisible(owned)
            observation = self.helper_observation
            cancellable = (helper_held and not owned and observation is not None
                and observation.get('creation_entered') is False
                and observation.get('phase') in {'reserved','non_grant','non_creation_settled','local_unsent'})
            self.helper_recover.setText('Cancel start and close')
            self.helper_recover.setVisible(cancellable)
            self.helper_recover.setEnabled(available and cancellable)
            self.recovery_details.setText('Original failure: '+(self.first_failure or 'Unavailable')+'\n\n'+self.details.text()
                + ('\nRetained attempt: '+helper_identity if helper_identity else '')
                + ('\nObservation: '+str(observation) if observation else ''))
            self.details.hide()
            if self.busy:
                message = ('Checking the original start reply. No new start is requested.'
                    if self.pending == 'observe_helper_accounting' else self.message)
            elif owned:
                message = ('Ollama has exited. Stop finishes cleanup of this background server.'
                    if self.snapshot.process_state == 'exited' else
                    'The background server is still retained. Stop it to finish cleanup and close.')
            elif helper_held:
                message = ('Ollama was not started. Cancel this start to finish cleanup.' if cancellable else
                    'The original start is unconfirmed. Cleanup remains held while its reply is checked.')
                if self._observation_failed:
                    message = 'The original start could not be confirmed. Cleanup remains held; see Details.'
            else:
                message = ('Cleanup is complete. You can retry starting Ollama.' if self.recovery_retry_allowed and self.installed
                    else 'No background server is retained. Close to finish cleanup.')
            self.status.setText(message)
        if self.changed is not None: self.changed()

    def review_helper_accounting(self):
        if not self.ready or self.busy: return
        identity = self.binding.helper_accounting_identity
        if identity is None: return
        self.review('Cancel the original start' if self.recovery_mode else 'Check retained start attempt',
            'Check only the original retained start attempt and its authenticated reply. '
            'Manager may settle it only after the original owner proves process creation was never entered. '
            'This does not restart, adopt or stop a process. Unconfirmed evidence keeps ownership retained.\n'
            f'Retained attempt: {identity}',
            'Cancel start and close' if self.recovery_mode else 'Check original attempt', 'reconcile_helper_accounting', identity)

    def submit(self, action, **kwargs):
        if not self.ready or self.busy: return False
        try:
            ticket = self.binding.request(action, **kwargs)
            if ticket is None:
                self.message = 'Worker is still draining. Wait before requesting another action.'
                return False
            self.pending = action
            self.message = {'registry':'Checking saved owned registration…',
                'review_start':'Preparing exact start review…', 'install':'Installing and verifying owned registration…',
                'recover_install':'Reconciling the retained installation…', 'start':'Starting the reviewed owned runner…',
                'stop':'Stopping the retained owned runner…', 'reconcile_helper_accounting':'Checking the original retained start attempt.',
                'observe_helper_accounting':'Checking the original start reply.'}[action]
            return True
        except Exception as error:
            self.message = f'Request held: {type(error).__name__}. Check owned status before retrying.'
            return False
        finally: self.render()

    def poll(self):
        if self.binding is None: return
        try: completion = self.binding.poll()
        except Exception:
            # An unconfirmed worker must keep ownership held; no success projection.
            self.message = 'Worker outcome unconfirmed. Manager retains ownership; do not retry.'
            self.render(); return
        if completion is not None:
            action = self.pending; self.pending = None; self.snapshot = completion.snapshot
            if completion.error and action not in {'observe_helper_accounting','claim_recovery_ui'} and self.first_failure is None:
                diagnostic = self.binding.worker_diagnostic
                self.first_failure = (diagnostic or {}).get('cause_code') or completion.error
            if action == 'observe_helper_accounting':
                self._observation_failed = completion.error is not None
                if completion.error is None:
                    self.helper_observation = completion.value
                    if (getattr(self,'_observation_count',0)>=5 and self.helper_observation.get('phase')
                            in {'reserve_pending','transfer_pending'}):self._observation_failed=True
            if action == 'claim_recovery_ui':
                self.recovery_claimed = completion.error is None and completion.value is True
            if self.registry_admitted is not None and self.snapshot.registry is not None: self.registry_admitted(self.snapshot.registry)
            if completion.error:
                self.status.setToolTip(completion.error)
                self.message = {
                    'owned_runtime_outcome_unconfirmed': 'Action held: worker result unavailable. Check owned status; explicitly Stop any retained runner before another start.',
                    'owned_action_outcome_requires_reconciliation': 'Action held after cancellation. Check owned status or review recovery; explicitly Stop any retained runner.',
                    'partial_copy_retained_requires_review': 'Action held: partial copy retained. Keep these files for review; recovery cannot publish an incomplete copy.',
                    'loopback_port_in_use': 'Action held: the owned endpoint is in use. Existing instances will not be adopted or stopped.',
                    'reviewed_cache_identity_and_probe_required': 'Start held: this installation has a saved model-cache admission. Starting that cache requires its exact reviewed identity and separate qualification. Keep the admission and cached files intact.',
                }.get(completion.error, 'Action held: outcome unconfirmed. Check owned status or review recovery; explicitly Stop any retained runner.')
            elif action == 'review_start':
                self.status.setToolTip('')
                self.message = 'Review the exact owned start and local key creation before continuing.'
                self.start_review = completion.value
            else:
                self.status.setToolTip('')
                self.message = {'registry':'Owned registration checked; current process health not probed. Choose the next action.',
                    'install':'Owned runner installed and registration verified. Start remains a separate review.',
                    'recover_install':'Owned installation recovery verified. Start remains a separate review.',
                    'start':'Owned runner running; version and process ownership verified.',
                    'stop':'Owned runner stopped; retained process ownership released.', 'reconcile_helper_accounting':'Original start attempt reconciled; process creation was not entered. Ownership can now finish cleanup.'}.get(action, 'Owned state reconciled.')
            if (self.recovery_mode and completion.error is None and action in {'stop','reconcile_helper_accounting'}
                    and self.recovery_finished is not None):
                QTimer.singleShot(0, self.recovery_finished)
        if self.start_review is not None and not self.busy:
            value = self.start_review; self.start_review = None
            self.review_start(value)
        self.render()

        if self.recovery_mode and self.ready and not self.busy and self.dialog is None:
            if self.recovery_claim is not None:
                session,recovery_id=self.recovery_claim
                try:ticket=self.binding.request_recovery_ui(session,recovery_id)
                except Exception:
                    self.recovery_claim=None
                    self.message='The launcher could not confirm this recovery window. Cleanup remains held.'
                    self.render();return
                if ticket is not None:
                    self.recovery_claim=None;self.pending='claim_recovery_ui'
                    self.message='Confirming this recovery window with the launcher.'
                    self.render()
                return
            identity = self.binding.helper_accounting_identity
            if identity != self._observation_identity:
                self._observation_identity = identity; self.helper_observation = None; self._observation_failed = False
                self._observation_count = 0
            if (identity is not None and not (self.snapshot and self.snapshot.ownership_held)
                    and not self._observation_failed and getattr(self,'_observation_count',0)<5
                    and (self.helper_observation is None or self.helper_observation.get('phase') in {'reserve_pending','transfer_pending'})
                    and time.monotonic() >= self._next_observation):
                self._next_observation = time.monotonic()+1
                self._observation_count=getattr(self,'_observation_count',0)+1
                self.submit('observe_helper_accounting', reviewed_identity=identity)

    def review(self, title, text, accept_label, action, pin, *, keys=False, cache_admission=None):
        if self.dialog is not None or not self.ready or self.busy: return
        cache = None if cache_admission is None else OwnedCacheAdmission.from_mapping(cache_admission)
        dialog = QDialog(self); dialog.setWindowTitle(title); dialog.setMinimumWidth(460); dialog.resize(620, 380)
        layout = QVBoxLayout(dialog)
        label = QLabel(text); label.setWordWrap(True); label.setTextFormat(Qt.PlainText); layout.addWidget(label)
        consent = QCheckBox('Allow Ollama to create its persistent local keypair in this owned profile.') if keys else None
        if consent is not None: layout.addWidget(consent)
        cache_consent = QCheckBox('Use the reviewed existing cache for this start.') if cache is not None else None
        if cache_consent is not None:layout.addWidget(cache_consent)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        accept = buttons.addButton(accept_label, QDialogButtonBox.AcceptRole)
        def acceptance():
            accept.setEnabled((consent is None or consent.isChecked()) and
                (cache_consent is None or cache_consent.isChecked()))
        acceptance()
        if consent is not None: consent.toggled.connect(acceptance)
        if cache_consent is not None: cache_consent.toggled.connect(acceptance)
        layout.addWidget(buttons)
        self.dialog = dialog
        dialog.review_accept = accept; dialog.key_consent = consent; dialog.review_cancel = buttons.button(QDialogButtonBox.Cancel)
        dialog.cache_consent = cache_consent
        def confirmed():
            authorized = consent is not None and consent.isChecked()
            if (keys and not authorized) or (cache_consent is not None and not cache_consent.isChecked()):return
            dialog.accept()
            self.submit(action, reviewed_identity=pin, **({'key_generation_authorized':authorized} if keys else {}),
                **({'cache_admission':cache.mapping(),'reviewed_cache_identity':cache.identity} if cache is not None else {}))
        def finished(_):
            if self.dialog is dialog: self.dialog = None
            dialog.deleteLater(); self.render()
        accept.clicked.connect(confirmed); buttons.rejected.connect(dialog.reject); dialog.finished.connect(finished)
        dialog.show(); self.render()

    def review_install(self, *, recovery=False):
        if not self.ready or self.busy: return
        b = self.binding
        self.review('Review owned Ollama installation',
            f'Publish the prepared verified Ollama {b.plan.registration.software_version} package and save its owned registration.\n\n'
            f'Destination: {b.plan.destination}\nOwned data: {b.layout.data_root}\n'
            f'Required free bytes: {b.plan.free_disk_required_bytes}\nReview identity: {b.publication_identity}\n\n'
            'No download or automatic start. Existing connections and models remain separate. '
            'Cancellation may leave retained publication files; review recovery before retrying.',
            'Recover installation' if recovery else 'Install reviewed package',
            'recover_install' if recovery else 'install', b.publication_identity)

    def review_start(self, value):
        if not isinstance(value, dict) or not isinstance(value.get('lifecycle_identity'), str):
            self.message = 'Exact start review unavailable.'; return
        cache = None if value.get('cache_admission') is None else OwnedCacheAdmission.from_mapping(value['cache_admission'])
        cache_text = '' if cache is None else (
            f'\n\nExisting admitted cache: {cache.model_name}; {cache.model_size_bytes} bytes.\n'
            f'Cache review identity: {cache.identity}\n'
            'Start rechecks the completed receipt and every cached file, then verifies installed/loaded metadata. '
            'No download or inference is performed.')
        self.review('Review owned Ollama start',
            f'Start only the installed owned Ollama {self.binding.plan.registration.software_version} executable '
            f'at {self.binding.endpoint}.\n\n'
            f'Owned profile: {value["environment"].get("USERPROFILE", "unknown")}\n\n'
            'The first actual start can create a persistent, unencrypted Ed25519 private/public keypair in this local owned profile. '
            'No key contents are read by Manager. No model download, inference or agent execution. '
            'Manager must retain ownership until you explicitly Stop; Stop is forced termination.' + cache_text,
            'Start reviewed runner', 'start', value['lifecycle_identity'], keys=True,
            cache_admission=None if cache is None else cache.mapping())

    def review_stop(self):
        if self.snapshot is None or not self.snapshot.ownership_held: return
        consequence=(f'Finish accounting for retained runner PID {self.snapshot.pid} and stop any remaining owned descendants. '
            if self.snapshot.process_state=='exited' else
            f'Forcibly terminate only the retained Manager runner PID {self.snapshot.pid} and its owned descendants. ')
        self.review('Review owned Ollama stop',
            consequence+'Any work in this owned runner is interrupted. External connections are not owned and will not be stopped.',
            'Stop retained runner', 'stop', self.snapshot.process_pin)

    def cancel_operation(self):
        if self.pending is not None and self.binding.cancel():
            self.message = 'Cancellation requested; waiting for worker and reconciling any side effects. Ownership remains retained.'
        self.render()

    def permit_close(self, *, final=False):
        if self.dialog is not None:
            self.dialog.reject()
        if self.binding is None: return True
        if self.binding.close_held:
            self.message = 'Close held: wait or cancel the current operation, then explicitly Stop any retained owned runner.'
            self.render(); return False
        if not final: return True  # Other Manager owners must admit close first.
        try: complete = self.binding.begin_close()
        except Exception:
            self.message = 'Owned runtime cleanup unconfirmed; Manager stays open.'; self.render(); return False
        if complete: self.timer.stop()
        return complete
