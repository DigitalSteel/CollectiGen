"""Responsive progress for work retained by the original standalone root.

Presentation only: no process discovery, cancellation, installation or health
authority. Workers publish phase messages; only the GUI thread touches widgets.
"""
from queue import SimpleQueue, Empty
from time import monotonic

from PySide6.QtCore import QEventLoop, QTimer, Qt
from PySide6.QtWidgets import QApplication, QDialog, QLabel

from qt_app.components import IndeterminateActivityBar
from suite_core.manager.ui_cards import workspace_heading, workspace_layout


class _PendingDialog(QDialog):
    def __init__(self):
        super().__init__()
        self.retired = False
        self.setObjectName('encyclopediaPendingTask')
        self.setWindowTitle('Tag Encyclopedia — finishing work')
        self.resize(540, 260)
        layout = workspace_layout(self)
        self.heading = workspace_heading('Finishing Reader tasks')
        layout.addWidget(self.heading)
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.PlainText)
        layout.addWidget(self.detail)
        self.activity = IndeterminateActivityBar(self)
        layout.addWidget(self.activity)
        self.elapsed = QLabel()
        self.elapsed.setAccessibleName('Time spent on the current task')
        layout.addWidget(self.elapsed)
        self.guidance = QLabel('Please keep this window open while this task finishes. Your catalogs and images are kept.')
        self.guidance.setWordWrap(True)
        layout.addWidget(self.guidance)

    def closeEvent(self, event):
        if self.retired:
            event.accept()
            return
        event.ignore()
        self.guidance.setText('Close is waiting for this task to finish safely. This window will close automatically.')

    def reject(self):
        # Escape must not dismiss the only progress surface or abandon workers.
        self.close()


class RootTaskProgress:
    def __init__(self, *, clock=monotonic):
        self.app = QApplication.instance() or QApplication([])
        self.clock = clock
        self.messages = SimpleQueue()
        self.dialog = None
        self.started = None
        self.finished = False
        self.previous_quit = self.app.quitOnLastWindowClosed()
        self.app.setQuitOnLastWindowClosed(False)

    def phase(self, heading, detail):
        """Worker-safe, content-free phase notification; no invented total."""
        if not self.finished:
            self.messages.put((heading, detail))

    def reader_ready(self):
        if not self.finished:
            self.messages.put(None)

    def poll(self):
        if self.finished:
            return
        while True:
            try:
                message = self.messages.get_nowait()
            except Empty:
                break
            if message is None:
                if self.dialog is not None:
                    self.dialog.activity.set_active(False)
                    self.dialog.hide()
                self.started = None
                continue
            heading, detail = message
            if self.dialog is None:
                self.dialog = _PendingDialog()
            self.started = self.clock()
            self.dialog.heading.setText(heading)
            self.dialog.detail.setText(detail)
            self.dialog.activity.set_active(True, heading)
            self.dialog.show()
        if self.started is not None and self.dialog is not None:
            seconds = max(0, int(self.clock() - self.started))
            self.dialog.elapsed.setText(f'{seconds // 60}m {seconds % 60:02d}s elapsed in this task')

    def wait(self, future, executor):
        """Keep painting until success AND physical worker settlement.

        Errors return to the existing retained-root recovery path immediately;
        they never certify worker retirement or retry an operation.
        """
        if self.finished:
            raise RuntimeError('Progress presentation already finished')
        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(50)
        failures = []

        def tick():
            try:
                self.poll()
                settled = future.done() and (future.cancelled() or future.exception() is not None or not executor.close_held)
            except BaseException as error:
                failures.append(error)
                loop.quit()
            else:
                if settled:
                    loop.quit()

        timer.timeout.connect(tick)
        timer.start()
        try:
            # Always enter the event loop: a synchronously completed future must
            # still wait for a separately retained worker to physically settle.
            QTimer.singleShot(0, tick)
            loop.exec()
            if failures:
                raise failures[0]
            if not future.done() or (not future.cancelled() and future.exception() is None and executor.close_held):
                raise RuntimeError('Root progress ended before original task settlement')
            return future.result()
        finally:
            timer.stop()

    def finish(self):
        self.finished = True
        try:
            if self.dialog is not None:
                self.dialog.retired = True
                self.dialog.activity.set_active(False)
                # Close before restoring quit behavior, so no queued app quit
                # can abort the existing recovery dialog after handoff.
                self.dialog.close()
        finally:
            self.app.setQuitOnLastWindowClosed(self.previous_quit)
