"""Lazy normal Models composition over the original registry, settings and jobs."""
from pathlib import Path


class ModelsWorkspaceHost:
    def __init__(self, settings_path):
        self.settings_path = Path(settings_path)
        self.registry_path = self.settings_path.parent / 'model_resources.json'
        self.jobs = self.dialog = None
        self.closing = False
        self.drained = False
        self.shutdown_started = False
        self.reopen_after_drain = False

    def __call__(self, parent):
        if self.reopen_after_drain and self.poll_close():
            self.jobs = self.dialog = None
            self.closing = self.shutdown_started = self.reopen_after_drain = False
            self.drained = False
        if self.closing: raise ValueError('Models workspace is closing.')
        if self.dialog is not None:
            return self.dialog
        from core.settings import AppSettings
        from core.model_resource_registry import ModelResourceRegistry
        from core.background_jobs import BackgroundJobManager
        from qt_app.model_workspace_dialog import ModelWorkspaceDialog
        # Use the stock pure parser, never load()'s automatic repair/write path.
        try:
            with self.settings_path.open('rb') as stream:
                raw = stream.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024: raise ValueError('Settings exceed review limits.')
            settings = AppSettings._from_bytes(raw)
        except FileNotFoundError:
            settings = AppSettings()
        roots = [p.root for p in settings.library_registry().profiles]
        roots.extend(str(getattr(settings, key, '')) for key in (
            'ingest_directory', 'problems_directory', 'recycle_directory', 'external_write_backup_directory')
            if getattr(settings, key, ''))
        self.jobs = self.jobs or BackgroundJobManager()
        self.dialog = ModelWorkspaceDialog(ModelResourceRegistry(self.registry_path), self.jobs,
            parent, protected_roots=roots, settings=settings, repository_review_only=True)
        self.dialog.setWindowTitle('Inspect public model repository')
        self.dialog.provider.setCurrentIndex(self.dialog.provider.findData('hugging_face'))
        self.dialog.query_kind.setCurrentIndex(self.dialog.query_kind.findData('repo_id'))
        return self.dialog

    def dialog_returned(self):
        # Original modal dialog cannot return while its coordinated task is held.
        # Keep its jobs alive; reuse them for a subsequent ordinary dialog.
        self.dialog = None

    @property
    def close_held(self):
        if self.closing:
            return not self.poll_close()
        return self.dialog is not None or bool(self.jobs is not None and self.jobs.active())

    def begin_close(self):
        self.closing = True
        if self.dialog is not None: self.dialog.reject()
        self.poll_close()

    def poll_close(self):
        if not self.closing: return False
        if self.dialog is not None and (self.dialog._task is not None or self.dialog.isVisible()):
            return False
        self.shutdown_started = self.jobs is not None
        self.drained = self.jobs is None or self.jobs.shutdown(wait=False)
        return self.drained

    def cancel_close(self):
        if self.shutdown_started and not self.poll_close():
            self.reopen_after_drain = True
            return
        if self.drained:
            # The old pool was shut down; the next explicit open creates a fresh
            # stock pool only after original physical drain.
            self.jobs = self.dialog = None
            self.shutdown_started = self.drained = False
        self.closing = False


def supervise_models_recovery(held):
    """Reuse original failed-entry window until exact Models jobs physically drain."""
    from PySide6.QtCore import QEventLoop, QTimer
    owner = held.owner
    window = owner.window
    if window is None: raise RuntimeError('Original Models recovery window required')
    window.setEnabled(True); window.show()
    window.statusBar().showMessage('Manager entry failed. Repository work is stopping; wait for original worker cleanup.')
    owner.models_workspace_host.begin_close()
    loop = QEventLoop()
    def poll():
        if not window.isVisible() and owner.poll_cleanup(): loop.quit()
    timer = QTimer(window); timer.setInterval(50); timer.timeout.connect(poll); timer.start()
    held.dialog = window; held.app = owner.app
    loop.exec(); timer.stop()
    if not owner.closed: raise RuntimeError('Models entry cleanup remains held')
    held.dialog = held.app = None
    return 1
