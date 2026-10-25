from __future__ import annotations

from PySide6.QtWidgets import QApplication,QDialog,QMainWindow,QMessageBox as _QMessageBox,QWidget

from .accessibility import announce_accessibly
from .components import WindowStatusBar,window_status_footer


def _single_ok_notice(buttons) -> bool:
    """Return whether a static message has one acknowledgment and no choice."""
    return buttons == _QMessageBox.StandardButton.Ok


def _usable_parent_and_window(parent):
    """Resolve a live top-level window while retaining a safe native parent."""
    usable_parent=None
    window=None
    if isinstance(parent,QWidget):
        try:
            window=parent.window()
            usable_parent=parent
        except RuntimeError:
            # A deleted Qt wrapper is not a usable native parent. An active
            # window may still provide the current operation's visible owner.
            window=None
    app=QApplication.instance()
    if window is None and app is not None:
        window=app.activeWindow()
    return usable_parent,window


def _footer_for_window(window):
    """Return the app footer when the resolved window can safely own one."""
    if window is None:
        return None
    if isinstance(window,QMainWindow) and not isinstance(
        window.statusBar(),WindowStatusBar,
    ):
        return None
    return window_status_footer(window)


def _notice_text(title,text) -> str:
    """Keep the supplied title and full detail in one selectable footer value."""
    title=str(title or "")
    text=str(text or "")
    if title and text:
        return f"{title}\n{text}"
    return title or text


def _nearest_parent_window(dialog):
    """Find the closest still-live window above a completed dialog."""
    parent=dialog.parentWidget()
    while parent is not None:
        try:
            window=parent.window()
        except RuntimeError:
            return None
        if window is not dialog:
            return window
        parent=parent.parentWidget()
    return None


def _forward_dialog_notice(dialog) -> None:
    """Forward the dialog's last passive notice once after the dialog finishes."""
    pending=getattr(dialog,"_footer_passive_notice",None)
    if pending is None:
        return
    dialog._footer_passive_notice=None
    message,severity,summary=pending
    window=_nearest_parent_window(dialog)
    footer=_footer_for_window(window)
    if footer is None:
        return
    if summary is None:
        footer.show_message(message,severity=severity)
    else:
        footer.show_message(message,severity=severity,summary=summary)
    announce_accessibly(
        footer.notice,message,assertive=severity in {"warning","error"},
    )
    if isinstance(window,QDialog):
        _remember_dialog_notice(window,message,severity,summary)


def _remember_dialog_notice(dialog,message,severity,summary=None) -> None:
    """Keep only the latest passive notice for one close-time parent handoff."""
    dialog._footer_passive_notice=(message,severity,summary)
    if getattr(dialog,"_footer_notice_forwarding_connected",False):
        return
    dialog._footer_notice_forwarding_connected=True
    dialog.finished.connect(lambda _result:_forward_dialog_notice(dialog))


def _publish_footer_notice(footer,window,title,text,*,severity: str,summary=None) -> None:
    """Publish one passive notice with selectable text and accessible urgency."""
    message=_notice_text(title,text)
    if summary is None:
        footer.show_message(message,severity=severity)
    else:
        footer.show_message(message,severity=severity,summary=summary)
    announce_accessibly(
        footer.notice,message,assertive=severity in {"warning","error"},
    )
    if isinstance(window,QDialog):
        _remember_dialog_notice(window,message,severity,summary)


class FooterMessageBox(_QMessageBox):
    """Route passive static notices to the window footer and retain real dialogs."""

    @staticmethod
    def information(
        parent,title,text,
        buttons=_QMessageBox.StandardButton.Ok,
        defaultButton=_QMessageBox.StandardButton.NoButton,
        *,summary=None,
    ):
        """Show a passive single-OK information notice in the owning footer."""
        native_parent,window=_usable_parent_and_window(parent)
        footer=_footer_for_window(window) if _single_ok_notice(buttons) else None
        if footer is None:
            return _QMessageBox.information(
                native_parent,title,text,buttons,defaultButton,
            )
        _publish_footer_notice(footer,window,title,text,severity="info",summary=summary)
        return _QMessageBox.StandardButton.Ok

    @staticmethod
    def warning(
        parent,title,text,
        buttons=_QMessageBox.StandardButton.Ok,
        defaultButton=_QMessageBox.StandardButton.NoButton,
        *,summary=None,
    ):
        """Show a passive single-OK warning in the owning footer with emphasis."""
        native_parent,window=_usable_parent_and_window(parent)
        footer=_footer_for_window(window) if _single_ok_notice(buttons) else None
        if footer is None:
            return _QMessageBox.warning(
                native_parent,title,text,buttons,defaultButton,
            )
        _publish_footer_notice(footer,window,title,text,severity="warning",summary=summary)
        return _QMessageBox.StandardButton.Ok

    @staticmethod
    def critical(
        parent,title,text,
        buttons=_QMessageBox.StandardButton.Ok,
        defaultButton=_QMessageBox.StandardButton.NoButton,
        *,summary=None,
    ):
        """Show a passive single-OK error in the owning footer with urgency."""
        native_parent,window=_usable_parent_and_window(parent)
        footer=_footer_for_window(window) if _single_ok_notice(buttons) else None
        if footer is None:
            return _QMessageBox.critical(
                native_parent,title,text,buttons,defaultButton,
            )
        _publish_footer_notice(footer,window,title,text,severity="error",summary=summary)
        return _QMessageBox.StandardButton.Ok
