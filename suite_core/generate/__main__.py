"""Generate executable entry through independently authenticated Manager admission."""
import sys


def main(argv=None, *, session=None):
    if session is None:
        from suite_core.workflow_modules.entry import entry_session
        try:
            session = entry_session('collectigen.generate', argv=argv)
        except (ValueError, RuntimeError, OSError) as error:
            sys.stderr.write('Open Generate through Manager Applications. Launch authentication failed: ' + str(error) + '\n')
            return 2
    from suite_core.workflow_modules.window import require_session
    require_session(session, 'collectigen.generate')
    from PySide6.QtWidgets import QApplication
    from suite_core.generate.window import create_window
    from suite_core.generate.version import MODULE_ID, VERSION
    from qt_app.theme import apply_theme
    app = QApplication.instance() or QApplication(list(argv or []))
    app.setApplicationName(MODULE_ID)
    app.setApplicationVersion(VERSION)
    apply_theme(app, 'system')
    window = create_window(session)
    window.show()
    result = app.exec()
    return window.exit_code or result


if __name__ == '__main__':
    raise SystemExit(main())
