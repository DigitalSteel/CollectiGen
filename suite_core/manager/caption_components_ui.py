"""Explicit Manager caption component review over the canonical worker service."""
from time import monotonic
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QMessageBox,
    QPlainTextEdit, QPushButton, QVBoxLayout)
from core.ai_interrogation import PLAIN_LANGUAGE_MODELS, MODEL_ACQUISITIONS
from qt_app.workers import CoordinatedCallTask
from qt_app.components import IndeterminateActivityBar


class CaptionComponentsDialog(QDialog):
    def __init__(self, runtime, settings, jobs, parent=None, *, allowed=lambda: True):
        super().__init__(parent)
        self.runtime, self.settings, self.jobs, self.allowed = runtime, settings, jobs, allowed
        self._task = None
        self._review = None
        self._closing = False
        self.setWindowTitle('Set Up Local Image Descriptions')
        self.resize(760, 540)
        body = QVBoxLayout(self)
        title = QLabel('Set up the selected caption model for Manager')
        title.setObjectName('sectionTitle'); title.setWordWrap(True); body.addWidget(title)
        model = PLAIN_LANGUAGE_MODELS.get(settings.plain_language_model)
        self.model_name = model[0] if model else settings.plain_language_model
        note = QLabel('Selected model: ' + self.model_name + '\nModels parent folder: '
                      + str(settings.ai_models_directory) + '\n\n'
                      'Check readiness, then review any required download. Downloads set up Manager only; '
                      'your Organizer and Video setup stays separate. Nothing is downloaded or analyzed on opening.')
        note.setTextFormat(Qt.PlainText); note.setWordWrap(True); body.addWidget(note)
        self.status = QLabel('Choose Check Readiness. You can close and continue authoring manually at any time.')
        self.status.setTextFormat(Qt.PlainText); self.status.setWordWrap(True); body.addWidget(self.status)
        self.activity = IndeterminateActivityBar(self)
        self.activity.setAccessibleName('Caption operation activity'); self.activity.hide()
        body.addWidget(self.activity)
        self.check_button = QPushButton('Check Readiness'); self.check_button.setProperty('primary', True)
        self.install_button = QPushButton('Install Caption Components...')
        self.model_button = QPushButton('Download Selected Model...')
        for button, operation in [(self.check_button, 'status'), (self.install_button, 'install-runtime'),
                                  (self.model_button, 'download-model')]:
            button.clicked.connect(lambda _checked=False, selected=operation: self.start_operation(selected))
            body.addWidget(button)
        self.install_button.setEnabled(False); self.model_button.setEnabled(False)
        self.details_button = QPushButton('Show Component Details'); self.details_button.setCheckable(True)
        body.addWidget(self.details_button)
        self.details = QPlainTextEdit(); self.details.setReadOnly(True); self.details.hide()
        self.details.setAccessibleName('Caption component readiness details')
        self.details_button.toggled.connect(self.details.setVisible); body.addWidget(self.details, 1)
        body.addStretch()
        actions = QHBoxLayout()
        self.cancel_button = QPushButton('Cancel Operation'); self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_operation); actions.addWidget(self.cancel_button)
        actions.addStretch()
        self.close_button = QPushButton('Return to Image Setup'); self.close_button.clicked.connect(self.reject)
        actions.addWidget(self.close_button); body.addLayout(actions)
        self.timer = QTimer(self); self.timer.setInterval(1000); self.timer.timeout.connect(self.tick)
        self._started = 0; self._message = ''
        self.details_button.setAutoDefault(False); self.cancel_button.setAutoDefault(False)
        self.set_primary(self.check_button)

    def set_primary(self, selected):
        for button in (self.check_button, self.install_button, self.model_button, self.close_button):
            button.setAutoDefault(False); button.setDefault(button is selected)
            button.setProperty('primary', button is selected)
            button.style().unpolish(button); button.style().polish(button); button.update()

    @property
    def close_held(self):
        return self._task is not None or self.runtime.close_held

    def start_operation(self, operation):
        if self.close_held or not self.allowed():
            self.status.setText('The original caption operation or Manager session must settle before continuing.')
            return
        if operation != 'status':
            if self._review is None:
                self.status.setText('Check readiness before reviewing a download.'); return
            hardware_ready = any(row['name'] == 'NVIDIA GPU' and row['installed']
                                 for row in self._review['components'])
            if not hardware_ready:
                self.status.setText('The reviewed caption profile requires a supported NVIDIA GPU. Continue manually or review another setup.'); return
            if operation == 'install-runtime':
                question = ('Install the reviewed local caption components for Manager?\n\n'
                            'This downloads the locked Python packages. It does not download model weights or analyze images.\n\n'
                            'The profile includes the official PyTorch CUDA build and NVIDIA runtime components under separate terms.')
            else:
                question = ('Download ' + self.model_name + ' into the selected models folder?\n\n'
                            + str(self._review.get('model_directory', self.settings.ai_models_directory)) + '\n\n'
                            'This may download large model files and any missing caption components. '
                            'Existing complete model files are retained until their replacement is ready. No image analysis starts.')
                terms = MODEL_ACQUISITIONS[self.settings.plain_language_model]
                question += ('\n\nSource: ' + terms.source_url + '\nLicense: ' + terms.license_name
                             + '\n' + terms.terms_notice + '\nTerms: ' + terms.license_url
                             + '\n\nThe official PyTorch CUDA build conveys NVIDIA runtime components under separate terms.')
            if QMessageBox.question(self, 'Review Caption Download', question,
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
            if not self.allowed():
                self.status.setText('The Manager session ended; no download was started.'); return
        token = self._review.get('review_token') if self._review is not None else None
        def work(job):
            if not self.allowed():
                raise InterruptedError('The Manager session ended.')
            options = dict(cancelled=lambda: job.cancel_requested or not self.allowed(), progress=job.update)
            if operation == 'status':
                return self.runtime.status(self.settings, **options)
            method = self.runtime.install_runtime if operation == 'install-runtime' else self.runtime.download_model
            return method(self.settings, token, **options)
        label = {'status': 'Checking local caption readiness', 'install-runtime': 'Installing caption components',
                 'download-model': 'Downloading selected caption model'}[operation]
        task = CoordinatedCallTask(self.jobs, label, work, resource='io', key='manager-caption-setup')
        self._task = task; self._started = monotonic(); self._message = label
        self._review = None
        self.check_button.setEnabled(False); self.install_button.setEnabled(False); self.model_button.setEnabled(False)
        self.cancel_button.setEnabled(True); self.set_primary(None)
        self.close_button.setText('Cancel and Return')
        self.activity.set_active(True, label); self.activity.show()
        self.status.setText(label + '...'); self.timer.start()
        task.signals.progress.connect(lambda _c, _t, message: self.progress(message))
        task.signals.complete.connect(lambda value, active=task: self.finished(active, operation, value))
        task.signals.failed.connect(lambda message, active=task: self.failed(active, str(message)))
        task.signals.cancelled.connect(lambda active=task: self.failed(active, 'Caption operation cancelled. Previous files and recovery remain with their original owner.'))
        try: task.start()
        except Exception as error: self.failed(task, str(error))

    def progress(self, message):
        self._message = message[:4096]; self.tick()

    def tick(self):
        if self._task is not None:
            self.status.setText(self._message + f' ({int(monotonic() - self._started)} seconds)')

    def cancel_operation(self):
        if self._task is not None and self._task.job_id:
            # Keep the result observer; task.cancel() intentionally detaches it.
            self.jobs.cancel(self._task.job_id)
            self.cancel_button.setEnabled(False)
            self._message = 'Cancelling; waiting for the caption process and downloads to stop'
            self.tick()

    def settled(self, task):
        if self._task is not task: return False
        self._task = None; self.timer.stop(); self.cancel_button.setEnabled(False)
        self.activity.set_active(False); self.activity.hide()
        self.close_button.setText('Return to Image Setup')
        self.check_button.setEnabled(self.allowed() and not self.runtime.close_held)
        return True

    def finished(self, task, operation, value):
        if not self.settled(task): return
        if self._closing and not self.runtime.close_held:
            super().reject(); return
        if not self.allowed():
            self.status.setText('The Manager session ended. Return without starting another operation.'); return
        if operation == 'status':
            self._review = value
            hardware_ready = any(row['name'] == 'NVIDIA GPU' and row['installed'] for row in value['components'])
            self.install_button.setEnabled(hardware_ready and any(not row['installed'] and row.get('kind') == 'package'
                for row in value['components']) and not value.get('runtime_issue'))
            model_missing = any(not row['installed'] and row.get('kind') == 'model' for row in value['components'])
            self.model_button.setEnabled(hardware_ready and model_missing and not value.get('runtime_issue'))
            missing = ', '.join(value.get('missing', [])[:6])
            self.status.setText('Ready for local image suggestions. Return to Image Setup and choose Suggest explicitly.'
                                if value['ready'] else 'Not ready: ' + (missing or 'review component details')
                                + '. Review the required setup below, or return and continue manually.')
            if not hardware_ready:
                self.status.setText('The reviewed caption profile requires a supported NVIDIA GPU. Nothing was installed. Return and continue manually if needed.')
            if value.get('runtime_issue'): self.status.setText(value['runtime_issue'])
            self.set_primary(self.close_button if value['ready'] or value.get('runtime_issue') else
                             self.model_button if self.model_button.isEnabled() else
                             self.install_button if self.install_button.isEnabled() else self.close_button)
            self.details.setPlainText('\n'.join(('Ready: ' if row['installed'] else 'Missing: ') + row['name']
                + (' - ' + str(row.get('detail', '')) if row.get('detail') else '') for row in value['components']))
        else:
            self.status.setText(str(value.get('message', 'Caption setup finished.')) + ' Checking the installed result...')
            self.start_operation('status')

    def failed(self, task, message):
        if not self.settled(task): return
        self._review = None; self.install_button.setEnabled(False); self.model_button.setEnabled(False)
        self.status.setText(message[:4096] + ' Check readiness before retrying; image setup edits are unchanged.')
        self.set_primary(self.check_button if self.check_button.isEnabled() else self.close_button)
        if self._closing and not self.runtime.close_held: super().reject()

    def reject(self):
        if self.close_held:
            self._closing = True; self.cancel_operation()
            self.status.setText('Cancelling caption work before returning. Keep this review open until it stops.')
            return
        super().reject()

    def closeEvent(self, event):
        if self.close_held:
            event.ignore(); self.reject()
        else: super().closeEvent(event)
