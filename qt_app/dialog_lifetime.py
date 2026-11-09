"""Minimal Qt owner completion boundary, independent of optional Feedback UI."""
from PySide6.QtWidgets import QDialog


def learning_dialog_choices(owner):
    """Snapshot authored Qt choices without validating or reading their sources.

    Only controls in this window participate; independent Library/search/note
    windows keep their own custody. Callers separately include semantic source,
    plan and result identity. This function starts no work and saves no state.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (QAbstractButton,QAbstractSpinBox,QAbstractSlider,QComboBox,
        QLineEdit,QPlainTextEdit,QTextEdit,QTreeWidget,QTreeWidgetItemIterator)

    window=owner.window()
    def controls(kind):
        return (widget for widget in owner.findChildren(kind) if widget.window() is window)
    values=[]
    for widget in controls(QLineEdit):values.append((id(widget),widget.text()))
    for widget in controls(QAbstractButton):
        if widget.isCheckable():values.append((id(widget),widget.isChecked()))
    for widget in controls(QComboBox):
        values.append((id(widget),widget.currentIndex(),repr(widget.currentData())))
    for widget in controls(QAbstractSpinBox):values.append((id(widget),widget.text()))
    for widget in controls(QAbstractSlider):values.append((id(widget),widget.value()))
    for kind in (QPlainTextEdit,QTextEdit):
        for widget in controls(kind):
            if not widget.isReadOnly():values.append((id(widget),widget.toPlainText()))
    for tree in controls(QTreeWidget):
        rows=[]
        iterator=QTreeWidgetItemIterator(tree)
        while iterator.value() is not None:
            item=iterator.value()
            rows.append((item is tree.currentItem(),item.isSelected(),
                tuple((item.text(col),item.checkState(col),repr(item.data(col,Qt.UserRole)))
                      for col in range(tree.columnCount()))))
            iterator+=1
        values.append((id(tree),tuple(rows)))
    return tuple(values)


class LearningOwnerDialog(QDialog):
    """Honor marked child Learning state/work before an ordinary dialog result."""

    def done(self, result: int) -> None:
        # QDialog accept/reject/done can finish without a child's closeEvent.
        # Preserve its note/work authority before hiding or emitting a result.
        if any(child.property("collectigenLearningLibraryOwner")
               or child.property("collectigenLearningSearchOwner")
               for child in self.findChildren(QDialog)):
            # Ordinary component dialogs need no Learning runtime dependency.
            from .learning_help_search import close_learning_libraries
            if not close_learning_libraries(self):
                return
        super().done(result)


def settle_learning_action(owner, context, *, is_current=None, expected_context=None):
    """Settle reading before one fresh UI action, without executing that action.

    Callers supply a side-effect-free, bounded immutable choice/source snapshot.
    Their original service still owns validation, writes, outcomes and recovery.
    An optional original-source predicate and pre-confirmation snapshot prevent
    retargeting; refusal never queues work or marks a request stale.
    """
    from shiboken6 import isValid
    if getattr(owner,"_collectigen_learning_action_busy",False):return False
    def readers():
        return tuple(child for child in owner.findChildren(QDialog)
                     if isValid(child) and (child.property("collectigenLearningLibraryOwner")
                                            or child.property("collectigenLearningSearchOwner")))
    def notice(text):
        from .components import window_status_footer
        footer=window_status_footer(owner)
        owner._collectigen_learning_notice=(footer,text)
        footer.show_message(text)
    owner._collectigen_learning_action_busy=True
    try:
        before=context()
        if ((is_current is not None and not is_current())
                or expected_context is not None and before!=expected_context):
            notice("The original source or choice changed. Review it before choosing another action; nothing will replay.")
            return False
        marked=readers()
        if marked:
            from .learning_help_search import close_learning_libraries
        if marked and not close_learning_libraries(owner):
            notice("Finish or save independent reading, then choose the action again. No new work is queued.")
            return False
        if (any(child.isVisible() or child not in marked for child in readers())
                or before!=context() or is_current is not None and not is_current()):
            notice("Reading, source or choices changed while settling. Review the original scope and choose again; nothing will replay.")
            return False
        return True
    except Exception as error:
        notice("Reading could not settle. Keep the original draft/evidence and review this problem before another action.\n\n"+str(error))
        return False
    finally:
        owner._collectigen_learning_action_busy=False


def clear_learning_action_notice(owner):
    """Clear only this owner's resolved reading notice after a known outcome."""
    from shiboken6 import isValid
    notice=getattr(owner,"_collectigen_learning_notice",None)
    if notice is None or not isValid(owner):return False
    if any(isValid(child) and child.isVisible() and (
            child.property("collectigenLearningLibraryOwner")
            or child.property("collectigenLearningSearchOwner"))
            for child in owner.findChildren(QDialog)):
        return False
    owner._collectigen_learning_notice=None
    footer,text=notice
    if isValid(footer) and footer.details.toPlainText()==text:
        footer.show_message("")
        return True
    return False


def add_confirmation_reading(dialog, help_button, *, label="Read Guide…"):
    """Add stock Help/F1 that declines this prompt before opening its guide.

    The guide belongs to the original owner, never to a disappearing consent
    prompt. The returned predicate lets standard-button callers return No.
    """
    from PySide6.QtGui import QKeySequence,QShortcut
    from PySide6.QtWidgets import QMessageBox
    requested=[False]
    read=dialog.addButton(label,QMessageBox.HelpRole)
    def read_instead():
        requested[0]=True
        dialog.reject()
        help_button.show_help()
    read.clicked.connect(read_instead)
    shortcut=QShortcut(QKeySequence("F1"),dialog)
    shortcut.activated.connect(read_instead)
    dialog._confirmation_reading_shortcut=shortcut
    return lambda:requested[0]


def question_with_learning(owner,title,text,buttons=None,default_button=None):
    """Stock Yes/No confirmation with an explicit no-consent reading exit."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMessageBox
    dialog=QMessageBox(owner)
    dialog.setWindowTitle(title)
    dialog.setIcon(QMessageBox.Question)
    dialog.setTextFormat(Qt.PlainText)
    dialog.setText(text)
    dialog.setStandardButtons(QMessageBox.Yes|QMessageBox.No if buttons is None else buttons)
    if default_button is not None:dialog.setDefaultButton(default_button)
    button=getattr(owner,"_context_help_button",None)
    read=add_confirmation_reading(dialog,button) if button is not None else lambda:False
    result=dialog.exec()
    return QMessageBox.No if read() else result
