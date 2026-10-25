"""One-shot editor delivery without retaining a completed worker result frame."""
import weakref

from PySide6.QtCore import QTimer
from shiboken6 import isValid


def defer_editor_delivery(owner,deliver,payload):
    """Transfer a mutable envelope after the result callback has returned.

    The unbound receiver consumes the envelope before entering its modal loop.
    Neither the scheduled callback nor a destroyed page retains old snapshots.
    """
    reference=weakref.ref(owner)
    envelope=list(payload)
    def release(*_args):envelope.clear()
    owner.destroyed.connect(release)
    def dispatch():
        page=reference()
        try:
            if page is not None and isValid(page) and not getattr(page,"_shutdown_requested",False):
                deliver(page,envelope)
        finally:
            release()
            if page is not None and isValid(page):
                try:page.destroyed.disconnect(release)
                except (RuntimeError,TypeError):pass
    QTimer.singleShot(0,dispatch)
