"""One supervised original Tag recovery surface; no replacement work owner."""

def run_tag_recovery(held):
    from PySide6.QtCore import QEventLoop, QTimer, Qt
    from PySide6.QtWidgets import QApplication, QDialog, QVBoxLayout, QLabel, QPushButton
    from .tag_database_ui import TagDatabasePane
    from .ui_cards import details_panel
    app = QApplication.instance() or QApplication([])
    owner, controller = held.owner, held.owner.tag_controller
    original = owner.window
    if original is None:
        class RecoveryWindow(QDialog):
            def closeEvent(self, event):
                if not controller.closed: controller.begin_close()
                controller.poll()
                if controller.close_held: event.ignore()
                else: event.accept()
        original = RecoveryWindow()
        original.setWindowTitle('CollectiGen needs attention')
        original.resize(1100, 850)
        layout = QVBoxLayout(original)
        pane = TagDatabasePane(controller, parent=original)
        layout.addWidget(pane)
        original.tag_database_pane = pane
        owner.window = original
    else:
        pane = original.tag_database_pane
        if pane.controller is not controller: raise ValueError('original_tag_controller_required')
        original.setEnabled(True)
        original.navigation.setCurrentRow(original.page_rows['Databases'])
    # Revoke only the failed shell's close intent; retain operation/Future/Event.
    controller.closed = False
    if controller.pending: controller.cancel()
    pane.render()
    banner = QLabel('Manager needs attention. Review the Tag work below before closing. '
        'Pending changes must finish safely; saved changes and recovery remain available.')
    banner.setObjectName('managerTagRecoveryReason')
    banner.setTextFormat(Qt.PlainText); banner.setWordWrap(True)
    pane.layout().insertWidget(0, banner)
    details = details_panel('Failure details', type(held.error).__name__ + ': ' + str(held.error)[:512])
    pane.layout().insertWidget(1, details)
    close = QPushButton('Close Manager'); close.setObjectName('managerTagRecoveryClose')
    close.clicked.connect(original.close); pane.layout().addWidget(close)
    panel = getattr(original, 'owned_runtime_panel', None)
    if panel is not None and held.admission is not None:
        if panel.binding is not held.admission.binding: raise ValueError('original_runtime_binding_required')
        panel.enter_recovery(type(held.error).__name__ + ': ' + str(held.error)[:512])
    held.dialog = original; held.app = app
    held.original_tag_pane = pane
    original.show()
    loop = QEventLoop()
    def refresh():
        pane.poll()
        if not original.isVisible() and owner.poll_cleanup(): loop.quit()
    timer = QTimer(original); timer.setInterval(100); timer.timeout.connect(refresh); timer.start()
    loop.exec()
    if not owner.closed: raise RuntimeError('tag_entry_recovery_still_held')
    timer.stop(); pane.timer.stop()
    if panel is not None: panel.timer.stop()
    held.dialog = None; held.app = None
    return 1
