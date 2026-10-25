"""Synthetic visible controls; source component evidence, not packaged acceptance."""
import json
import os
from pathlib import Path
from threading import Event
import time

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QToolButton
from qt_app.theme import apply_theme
from suite_core.encyclopedia.__main__ import create_window
from suite_core.tests.test_encyclopedia import app

_DEBUG_HOST = None


def pump(app, condition, seconds=20):
    end = time.monotonic() + seconds
    while not condition() and time.monotonic() < end:
        time.sleep(.005)  # Existing UI-test settle pattern yields the Python worker.
        app.processEvents()
        QTest.qWait(10)
    if not condition():
        host = _DEBUG_HOST
        if host is not None:
            state = {name: bool(getattr(host, name)) for name in ['_creation_confirming', '_closing', '_operation_jobs']}
            state.update(creation_phase=host._creation_phase, timer_active=host.timer.isActive(),
                future_present=host.future is not None, future_done=host.future.done() if host.future else None,
                executor_held=host.executor.close_held, controller_held=host.controller.close_held,
                status=host.window.status.text(), publication_present=host.window.publication is not None)
            target = host._test_capture_directory/'TIMEOUT.json'
            target.write_text(json.dumps(state, indent=2)+'\n', encoding='utf-8')
            host.window.grab().save(str(target.with_suffix('.png')))
        assert False, 'original synthetic operation did not settle; inspect TIMEOUT.json'


def command(window, label):
    window.bar.refresh_state()
    buttons = [b for b in window.bar.findChildren(QToolButton)
               if b.isVisible() and b.text() == label]
    assert len(buttons) == 1, label
    QTest.mouseClick(buttons[0], Qt.LeftButton)


@pytest.mark.parametrize('theme,decision', [('dark','create'), ('light','create'),
                                          ('dark','decline'), ('dark','cancel-review')])
def test_visible_create_review_cancel_and_saved_completion(app, tmp_path, monkeypatch, theme, decision):
    import core.encyclopedia_catalog_creation as creation
    assert Path(creation.__file__).resolve().parents[1] == Path(__file__).resolve().parents[1]
    from PySide6.QtGui import QFontDatabase, QFont
    font_id = QFontDatabase.addApplicationFont('C:/Windows/Fonts/segoeui.ttf')
    assert font_id >= 0
    apply_theme(app, theme)
    app.setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0], 10))
    settings = tmp_path / 'settings.json'
    settings.write_text('{"unknown_future_setting":{"preserve":true}}', encoding='utf-8')
    catalog = tmp_path / 'synthetic-new-catalog'
    monkeypatch.setattr(QFileDialog, 'getExistingDirectory', lambda *args: str(catalog))
    review_gate, create_gate = Event(), Event()
    reviewing, creating = Event(), Event()
    original_review, original_create = creation.prepare_empty_catalog, creation.confirm_empty_catalog
    def delayed_review(*args, **kwargs):
        reviewing.set()
        assert review_gate.wait(20)
        return original_review(*args, **kwargs)
    def delayed_create(*args, **kwargs):
        creating.set()
        assert create_gate.wait(20)
        return original_create(*args, **kwargs)
    monkeypatch.setattr(creation, 'prepare_empty_catalog', delayed_review)
    monkeypatch.setattr(creation, 'confirm_empty_catalog', delayed_create)
    window = create_window(settings_path=settings)
    window.resize(1440, 900)
    host = window.catalog_host
    global _DEBUG_HOST
    _DEBUG_HOST = host
    captures = []
    output = Path(os.environ.get('READER_CREATION_VISIBLE_CAPTURE', str(tmp_path/'captures'))) / (theme+'-'+decision)
    output.mkdir(parents=True, exist_ok=False)
    host._test_capture_directory = output
    import faulthandler
    stack_log = (output/'STACKS.log').open('w', encoding='utf-8')
    faulthandler.dump_traceback_later(8, repeat=True, file=stack_log)
    def capture(label, widget=window):
        target = output / (label+'.png')
        assert widget.grab().save(str(target))
        captures.append(str(target))
    seen = []
    timer = QTimer(window)
    timer.setInterval(30)
    def confirm_visible_modal():
        box = QApplication.activeModalWidget()
        if isinstance(box, QMessageBox):
            seen.append(box.windowTitle())
            capture('confirmation', box)
            button = box.button(QMessageBox.Yes if decision == 'create' else QMessageBox.No)
            if button is None:
                box.reject()
            else:
                QTest.mouseClick(button, Qt.LeftButton)
            timer.stop()
    timer.timeout.connect(confirm_visible_modal)
    try:
        window.show()
        app.processEvents()
        capture('entry')
        command(window, 'Create a database')
        pump(app, reviewing.is_set)
        QTest.qWait(1100)
        assert host.busy and window.database_activity.isVisible()
        assert window.database_elapsed.isVisible() and 'elapsed' in window.database_elapsed.text()
        assert not catalog.exists()
        capture('review-pending')
        if decision == 'cancel-review':
            assert window.cancel_work_button.isVisible()
            QTest.mouseClick(window.cancel_work_button, Qt.LeftButton)
        else:
            timer.start()
        review_gate.set()
        if decision == 'create':
            pump(app, creating.is_set)
            QTest.qWait(1100)
            assert host.busy and window.database_activity.isVisible()
            assert not catalog.exists()
            capture('creation-pending')
            create_gate.set()
            pump(app, lambda: not host.busy and window.publication is not None)
            pump(app, lambda: not window.cancel_work_button.isVisible())
            assert host.accepted_source.kind == 'native' and host.accepted_source.path == catalog
            assert (catalog/'data/user/catalog_identity.json').is_file()
            assert (catalog/'data/user/tags.json').is_file()
            assert not window.publication.snapshot.tags
            assert window.empty_database_action.isVisible()
            assert json.loads(settings.read_bytes())['unknown_future_setting'] == {'preserve': True}
            capture('created-open')
            assert seen == ['Create a tag database']
        else:
            pump(app, lambda: not host.busy)
            assert not catalog.exists() and window.publication is None
            assert json.loads(settings.read_bytes()) == {'unknown_future_setting': {'preserve': True}}
            assert not creating.is_set()
            assert seen == ([] if decision == 'cancel-review' else ['Create a tag database'])
            capture('cancelled')
    finally:
        faulthandler.cancel_dump_traceback_later()
        stack_log.close()
        timer.stop()
        review_gate.set()
        create_gate.set()
        pump(app, lambda: not host.busy)
        window.close()
        pump(app, lambda: host.executor.closed and not host.executor.close_held)
        app.processEvents()
        QFontDatabase.removeApplicationFont(font_id)
        (output/'EXECUTION.json').write_text(json.dumps({'theme': theme, 'decision': decision,
            'captures': captures, 'level': 'source_component',
            'native_file_dialog': 'synthetic folder choice substituted',
            'real_confirmation_buttons': True, 'ordinary_worker_retired': True,
            'packaged_or_full_journey_acceptance': False}, indent=2)+'\n', encoding='utf-8')
