"""Manager composition over the current configured, already loaded Ollama model.

No automatic connection, model load, server stop, credentials or persistence.
Only immutable accepted observations cross from the UI owner to its chat worker.
"""
from datetime import datetime, timezone
from threading import Lock

from core.manager_ollama_service import ExistingRunnerModelService
from suite_core.discovery.ollama_readonly import ReadRoute, ReadState
from .metadata_binding import ManagerMetadataBinding
from .model_chat_controller import ChatTarget, ModelChatController, ollama_event
from .resource_executor import ResourceExecutorOwner


class ManagerChatHost:
    def __init__(self, *, executor=None, clock=None):
        self.executor = executor or ResourceExecutorOwner()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.controller = ModelChatController(self, self.executor)
        self.panel = None
        self.closed = False
        self._lock = Lock()
        self._snapshot = (None, None, True)
        self._outcome_unknown = False

    @property
    def close_held(self):
        return self.controller.close_held

    @property
    def busy(self):
        return self.controller.close_held

    def update(self, binding, report, *, blocked=False):
        with self._lock:
            previous = self._snapshot
            self._snapshot = (binding, report, bool(blocked))
            if report is not previous[1]:
                self._outcome_unknown = False
        targets = self.targets()
        if self.panel is not None and not self.panel.controller.closed:
            existing = tuple(self.panel.models.itemData(i) for i in range(1, self.panel.models.count()))
            if targets != existing:
                self.panel.set_targets(targets)
        return targets

    def targets(self):
        with self._lock:
            binding, report, blocked = self._snapshot
            unknown = self._outcome_unknown
        if self.closed or type(binding) is not ManagerMetadataBinding or report is None:
            return ()
        if not binding.scope.accepts(report): return ()
        try:
            ExistingRunnerModelService(binding).validate_chat_scope()
            installed = report.observation(ReadRoute.INSTALLED)
            loaded = report.observation(ReadRoute.LOADED)
            version = report.observation(ReadRoute.VERSION)
            fresh = all(item.state is ReadState.AVAILABLE and
                item.freshness_at(self.clock(), report.limits).value == 'fresh'
                for item in (installed, loaded, version))
            loaded_ids = {(row.name, row.digest): row.context_length for row in loaded.models or ()}
            choices = []
            seen = set()
            for row in installed.models or ():
                if not row.digest or not row.size_bytes or (row.name, row.digest) in seen: continue
                seen.add((row.name, row.digest))
                context = loaded_ids.get((row.name, row.digest))
                ready = fresh and not blocked and not unknown and type(context) is int and context >= 512
                choices.append(ChatTarget(binding.scope.connection.backend_id.value, row.name,
                    row.digest, ready,
                    'Refresh the configured backend and choose an already loaded model. '
                    'After Stop or an interrupted request, refresh before sending again.',
                    min(4096, context) if type(context) is int and context >= 512 else 4096))
            return tuple(choices[:100])
        except Exception:
            return ()

    def open_panel(self, parent):
        from .model_chat_ui import ModelChatPanel
        if self.closed: raise ValueError('Manager chat is closing.')
        if self.panel is None or self.controller.closed:
            if self.close_held: raise ValueError('Wait for the previous chat request to finish.')
            self.controller = ModelChatController(self, self.executor)
            self.panel = ModelChatPanel(self.controller, self.targets(), parent)
        self.panel.show(); self.panel.raise_(); self.panel.activateWindow()
        return self.panel

    def stream_chat(self, request, cancelled):
        with self._lock:
            binding, report, blocked = self._snapshot
        if blocked or request.target not in self.targets() or not request.target.ready:
            raise ValueError('Refresh and qualify the selected model before sending.')
        service = ExistingRunnerModelService(binding)
        # Reuse the service's exact configured registration and endpoint policy.
        service.validate_chat_scope()
        payload = request.ollama_payload()
        payload['think'] = False
        def interrupted():
            with self._lock:
                current_binding, current_report, current_blocked = self._snapshot
            return (cancelled() or self.closed or current_binding != binding
                or current_report is not report or current_blocked)
        stream = None
        complete = False
        try:
            stream = service.api.stream_chat(payload, cancelled=interrupted,
                seconds=request.timeout_seconds, limit=1024*1024)
            for value in stream:
                service.validate_chat_scope()
                if interrupted(): raise ValueError('Chat admission changed.')
                event = ollama_event(value)
                if event.done: complete = True
                yield event
        finally:
            if stream is not None and hasattr(stream, 'close'): stream.close()
            if not complete or cancelled():
                with self._lock: self._outcome_unknown = True

    def poll(self):
        self.controller.poll()

    def begin_close(self):
        self.closed = True
        if self.panel is not None: self.panel.close()
        else: self.controller.begin_close()
        self.poll()

    def finish_close(self):
        self.poll()
        if self.close_held: return False
        self.executor.begin_close()
        if self.panel is not None: self.panel.timer.stop()
        return not self.executor.close_held


def supervise_chat_recovery(held):
    """Keep original window/panel visible until the actual chat worker drains."""
    from PySide6.QtCore import QEventLoop, QTimer, Qt
    from PySide6.QtWidgets import QLabel
    from .ui_cards import details_panel
    host, owner = held.owner.chat_host, held.owner
    original = owner.window
    if original is None: raise RuntimeError('original_chat_window_required')
    original.setEnabled(True); original.show()
    panel = host.panel
    if panel is not None:
        note = QLabel('Manager needs attention. This chat request is stopping; '
            'keep Manager open until it finishes safely.')
        note.setTextFormat(Qt.PlainText); note.setWordWrap(True)
        panel.layout().insertWidget(0, note)
        panel.layout().insertWidget(1, details_panel('Failure details',
            type(held.error).__name__ + ': ' + str(held.error)[:512]))
        panel.show()
    if held.admission is not None:
        original.owned_runtime_panel.enter_recovery(type(held.error).__name__ + ': ' + str(held.error)[:512])
    host.begin_close()
    original.statusBar().showMessage('Manager entry failed; chat request ownership is retained until completion.')
    loop = QEventLoop()
    def refresh():
        host.poll()
        if not original.isVisible() and owner.poll_cleanup(): loop.quit()
    timer = QTimer(original); timer.setInterval(50); timer.timeout.connect(refresh); timer.start()
    held.dialog = original; held.app = owner.app
    loop.exec(); timer.stop()
    if not owner.closed: raise RuntimeError('chat_entry_cleanup_held')
    held.dialog = None; held.app = None
    return 1
