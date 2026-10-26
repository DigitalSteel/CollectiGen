"""Explicit hosted entry and stock-pool close guard; no catalog/library discovery."""
from PySide6.QtCore import QObject, QEvent, QTimer, QThreadPool, Qt
from PySide6.QtWidgets import QSplitter

from .hosted_encyclopedia_overlay import CompositionBoundaryError


class HostedEncyclopediaEntry(QObject):
    """Host supplies admission/open/task/GUI routes; coordinator supplies drain."""
    def __init__(self, host, *, open_browser, admitted_publication, task_factory,
                 post_to_gui, selected_original_adapter=None):
        super().__init__(host)
        if admitted_publication is None or not all(callable(value) for value in (
                open_browser, task_factory, post_to_gui)):
            raise CompositionBoundaryError('Explicit original hosted enrollment required')
        if not callable(getattr(host.state.jobs, 'create_execution_scope', None)):
            raise CompositionBoundaryError('Original coordinator scoped execution ownership required')
        self.host = host
        self.open_browser = open_browser
        self.publication = admitted_publication
        self.selected_original_adapter = selected_original_adapter
        self.task_factory, self.post_to_gui = task_factory, post_to_gui
        self.binding = None
        self.browser = self.gallery = self.attachment = self.pool = None
        self._closing = None
        self._timer = QTimer(self)
        self._timer.setInterval(25)
        self._timer.timeout.connect(self._retry_close)

    def available(self):
        try:
            self.host.organizer._check_context()
            return self._closing is None
        except Exception:
            return False

    def open(self):
        if not self.available():
            return False
        if (self.browser is not None and self.attachment is not None
                and not self.attachment.detached and not self.attachment.binding._closed):
            self.browser.show()
            self.browser.raise_()
            self.browser.activateWindow()
            return True
        # No reads/opening happen until this command is explicitly invoked.
        try:
            self.attachment = self.host.open_hosted_encyclopedia_browser(
                open_browser=self._choose_browser, admitted_publication=self.publication,
                task_factory=self.task_factory, post_to_gui=self.post_to_gui,
                gallery_factory=self._install_gallery,
                selected_original_adapter=self.selected_original_adapter)
        except Exception:
            self.host.statusBar().showMessage('Hosted Encyclopedia could not open. Review the approved catalog and library connection; no image was changed.', 12000)
            return False
        return True

    def _choose_browser(self):
        browser = self.open_browser()
        if getattr(browser, '_organizer_hosted_entry', None) not in (None, self):
            raise CompositionBoundaryError('Browser is retained by another hosted entry')
        return browser

    def retire(self):
        """Explicit reselection may release only this drained original enrollment."""
        if self._closing is self.host:
            return False
        if self.browser is not None:
            if not self.prepare_close(self.browser):return False
            self.browser.close()
            if self.browser.isVisible():return False
            if self.gallery is not None:self.gallery.detach()
            if self.attachment is not None:self.attachment.detach()
            self.browser.removeEventFilter(self)
            if getattr(self.browser, '_organizer_hosted_entry', None) is self:
                self.browser._organizer_hosted_entry=None
        self._timer.stop()
        self._closing=None
        return True

    def _install_gallery(self, browser, binding, organizer):
        from .encyclopedia_image_gallery import HostedEncyclopediaImageGallery
        if binding.execution_scope is None:
            raise CompositionBoundaryError('Original coordinator scoped execution ownership required')
        if self.gallery is not None:
            self.gallery.detach()
        if getattr(browser, '_organizer_hosted_entry', None) not in (None, self):
            raise CompositionBoundaryError('Browser is retained by another hosted entry')
        previous = getattr(browser, '_organizer_original_content', None)
        if previous is not None:
            old = browser.takeCentralWidget()
            previous.setParent(None)
            old.deleteLater()
        else:
            previous = browser.takeCentralWidget()
            browser._organizer_original_content = previous
        gallery = HostedEncyclopediaImageGallery(browser, binding, organizer)
        # The stock thumbnail engine is unchanged. A scoped stock Qt pool is
        # required because a shared global pool cannot prove this entry's drain.
        pool = QThreadPool(gallery)
        pool.setMaxThreadCount(2)
        gallery.gallery.card_grid.pool = pool
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(previous)
        splitter.addWidget(gallery)
        splitter.setSizes([800, 800])
        browser.setCentralWidget(splitter)
        browser._organizer_hosted_entry = self
        browser.installEventFilter(self)
        browser.resize(1600, 900)
        self.browser, self.gallery, self.pool, self.binding = browser, gallery, pool, binding

    def _drained(self):
        try:
            return (self.binding is not None and self.binding.example_queries_drained()
                    and (self.pool is None or self.pool.waitForDone(0)))
        except Exception:
            return False

    def prepare_close(self, target):
        if self.browser is None:
            return True
        if self._closing is None:
            self._closing = target
            self.binding.pause_example_queries()
            self.browser.cancel_examples()
            self.browser.examples_button.setEnabled(False)
            if self.pool is not None:
                self.pool.clear()  # Only this entry's queued stock thumbnails.
        elif target is self.host:
            self._closing = self.host  # Parent Close retains precedence over reader Close.
        if self._drained():
            self._timer.stop()
            return True
        self.browser.examples_status.setText('Closing is waiting for the original example query and local thumbnail workers to finish. No library data is changed.')
        self._timer.start()
        return False

    def _retry_close(self):
        if self._closing is not None and self._drained():
            target = self._closing
            self._timer.stop()
            target.close()  # Native shell retains all other close decisions.

    def finish_host_close(self, accepted):
        self._timer.stop()
        self._closing = None
        if accepted:
            if self.gallery is not None:
                self.gallery.detach()
            if self.browser is not None:
                self.browser.close()
        elif self.browser is not None:
            self.binding.resume_example_queries()
            self.browser._examples_readiness()

    def eventFilter(self, watched, event):
        if watched is self.browser and event.type() == QEvent.Close:
            if self._closing is self.host:
                event.ignore()
                return True
            if not self.prepare_close(self.browser):
                event.ignore()
                return True
            self._timer.stop()
            self._closing = None
        return super().eventFilter(watched, event)
