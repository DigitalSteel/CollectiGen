"""Small, opt-in lifecycle support for synthetic Qt widget tests.

The helper deliberately owns only widgets and timers created or registered
inside a test scope.  It never performs a process-wide widget sweep or drains
all deferred-delete events, so unrelated tests can keep their own Qt state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic, sleep


SUPPORTED_VIEWPORT = (1920, 1080)


def _application():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@dataclass
class QtTestScope:
    """Bound widget ownership, bounded settling, and application restoration."""

    app: object = field(default_factory=_application)
    _initial_top_levels: set[int] = field(init=False, default_factory=set)
    _timers: list[object] = field(init=False, default_factory=list)
    _shown_windows: list[object] = field(init=False, default_factory=list)
    _palette: object = field(init=False)
    _font: object = field(init=False)
    _style_name: str = field(init=False)
    _style_sheet: str = field(init=False)

    def __enter__(self) -> "QtTestScope":
        from shiboken6 import getCppPointer, isValid

        self._initial_top_levels = {
            getCppPointer(widget)[0] for widget in self.app.topLevelWidgets()
            if isValid(widget)
        }
        self._palette = self.app.palette()
        self._font = self.app.font()
        self._style_name = self.app.style().objectName()
        self._style_sheet = self.app.styleSheet()
        return self

    def settle(self, rounds: int = 4) -> None:
        """Process ordinary queued work a small, fixed number of times."""
        from PySide6.QtCore import QEventLoop

        for _ in range(rounds):
            self.app.processEvents(QEventLoop.AllEvents, 25)

    def show(self, widget, *, size: tuple[int, int] = SUPPORTED_VIEWPORT):
        """Show a test-owned widget at the explicit supported viewport."""
        self._shown_windows.append(widget)
        widget.resize(*size)
        widget.show()
        self.settle()
        return widget

    def own_timer(self, timer):
        """Register a parentless timer whose lifetime belongs to this scope."""
        self._timers.append(timer)
        return timer

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        from PySide6.QtCore import QCoreApplication, QEvent, QTimer
        from PySide6.QtTest import QTest
        from shiboken6 import getCppPointer, isValid

        # Qt's top-level list also contains child dialogs, menus and internal
        # combo popups. They belong to their QObject parent, not to independent
        # test lifetimes: deleting a popup first can leave its native owner with
        # a dangling implementation pointer. Dispose only new ownership roots;
        # Qt disposes their complete trees. Children of pre-existing owners stay
        # outside this scope just like those owners themselves.
        candidates = {
            getCppPointer(widget)[0]: widget
            for widget in self.app.topLevelWidgets()
            if isValid(widget) and widget.parent() is None
            and getCppPointer(widget)[0] not in self._initial_top_levels
        }
        # show() explicitly opts a parented dialog into this scope. Discovery
        # alone cannot distinguish it from its pre-existing owner's lazy popup.
        candidates.update({
            getCppPointer(widget)[0]: widget for widget in self._shown_windows
            if isValid(widget) and getCppPointer(widget)[0] not in self._initial_top_levels
        })
        owned_windows = []
        for widget in candidates.values():
            parent = widget.parent()
            while parent is not None and isValid(parent):
                if getCppPointer(parent)[0] in candidates:
                    break
                parent = parent.parent()
            else:
                owned_windows.append(widget)
        # Qt does not specify topLevelWidgets() order. Application windows can
        # hold Python references to parentless Qt controls even though those
        # controls also appear as independent top levels. Retire those owners
        # before their controls, regardless of enumeration order.
        owned_windows.sort(key=lambda widget: type(widget).__module__.startswith("PySide6."))
        # A page can retain references to parentless controls that also appear
        # in Qt's top-level list. Let every window run its close handlers while
        # all of those controls and completion-driving timers are still alive.
        deferred = []
        for widget in owned_windows:
            try:
                if not isValid(widget):
                    continue
                if not widget.close():
                    deferred.append(widget)
            except RuntimeError:
                pass
        if deferred:
            deadline = monotonic() + 10.0
            while any(isValid(widget) and widget.isVisible() for widget in deferred):
                if monotonic() >= deadline:
                    names = [type(widget).__name__ for widget in deferred
                             if isValid(widget) and widget.isVisible()]
                    raise AssertionError(
                        "Test-owned window deferred close beyond 10 seconds: "
                        + ", ".join(names)
                    )
                QTest.qWait(20)
                sleep(0.001)

        for timer in reversed(self._timers):
            try:
                timer.stop()
                timer.deleteLater()
                QCoreApplication.sendPostedEvents(timer, QEvent.DeferredDelete)
            except RuntimeError:
                pass
        def trace_delete(event, widget):
            # Opt-in owned synthetic diagnostic; never dereference after destruction.
            import os, json
            path=os.environ.get("COLLECTIGEN_QT_DELETE_TRACE")
            if not path:
                return
            from PySide6.QtCore import QThread
            detail={"event":event,"type":type(widget).__name__,"valid":isValid(widget)}
            if isValid(widget):
                parent=widget.parent()
                detail.update(pointer=hex(getCppPointer(widget)[0]),name=widget.objectName(),
                    parent=hex(getCppPointer(parent)[0]) if parent is not None and isValid(parent) else None,
                    gui_thread=widget.thread()==self.app.thread(),current_gui=QThread.currentThread()==self.app.thread())
            with open(path,"a",encoding="utf-8") as stream:
                stream.write(json.dumps(detail)+"\n");stream.flush()
        for widget in owned_windows:
            try:
                if not isValid(widget):
                    continue
                trace_delete("root-before",widget)
                for timer in widget.findChildren(QTimer):
                    timer.stop()
                widget.deleteLater()
                trace_delete("root-posted",widget)
                if isValid(widget):
                    QCoreApplication.sendPostedEvents(widget, QEvent.DeferredDelete)
                trace_delete("root-delivered",widget)
            except RuntimeError:
                pass

        self.settle()
        if self._style_name and self.app.style().objectName() != self._style_name:
            self.app.setStyle(self._style_name)
        self.app.setPalette(self._palette)
        self.app.setFont(self._font)
        self.app.setStyleSheet(self._style_sheet)
