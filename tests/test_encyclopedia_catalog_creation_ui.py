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
from suite_core.tests.test_encyclopedia_downloaded_defaults import content_font

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
@pytest.mark.parametrize('viewport', [(1280, 720), (1920, 1080)])
def test_visible_create_review_cancel_and_saved_completion(app, tmp_path, monkeypatch, theme, decision, viewport):
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
    window.resize(viewport[0] - 24, viewport[1] - 64)
    host = window.catalog_host
    global _DEBUG_HOST
    _DEBUG_HOST = host
    captures = []
    output = Path(os.environ.get('READER_CREATION_VISIBLE_CAPTURE', str(tmp_path/'captures'))) / (theme+'-'+decision+'-'+str(viewport[0]))
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
    reopened = None
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
        assert window.cancel_work_button.parentWidget() is window.start_card
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
            assert not window.start_card.isVisible()
            assert window.status.parentWidget() is window.centralWidget()
            assert 'Home' not in window.bar.visible_limits
            original_id = window.publication.catalog_id
            window.close()
            pump(app, lambda: host.executor.closed and not host.executor.close_held)
            reopened = create_window(settings_path=settings)
            reopened.resize(viewport[0] - 24, viewport[1] - 64)
            reopened.show(); app.processEvents()
            command(reopened, 'Open Tag Database')
            pump(app, lambda: not reopened.catalog_host.busy and reopened.publication is not None)
            pump(app, lambda: not reopened.cancel_work_button.isVisible())
            assert reopened.publication.catalog_id == original_id
            assert reopened.catalog_host.accepted_source.path == catalog
            assert not reopened.start_card.isVisible() and reopened.empty_database_action.isVisible()
            capture('saved-reopen', reopened)
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
        if reopened is not None:
            reopened.close()
            pump(app, lambda: reopened.catalog_host.executor.closed and not reopened.catalog_host.executor.close_held)
        app.processEvents()
        QFontDatabase.removeApplicationFont(font_id)
        (output/'EXECUTION.json').write_text(json.dumps({'theme': theme, 'decision': decision,
            'captures': captures, 'level': 'source_component',
            'native_file_dialog': 'synthetic folder choice substituted',
            'real_confirmation_buttons': True, 'ordinary_worker_retired': True,
            'packaged_or_full_journey_acceptance': False}, indent=2)+'\n', encoding='utf-8')


@pytest.mark.parametrize('theme', ['light', 'dark'])
@pytest.mark.parametrize('viewport', [(1280, 720), (1920, 1080)])
@pytest.mark.parametrize('outcome', ['first', 'refresh', 'cancel', 'refuse', 'close'])
def test_export_open_original_worker_feedback_and_recovery(app, tmp_path, monkeypatch, theme, viewport, outcome):
    from PySide6.QtGui import QFontDatabase, QFont
    import suite_core.encyclopedia.browser as browser
    from suite_core.tests.test_encyclopedia import fixture
    from suite_core.encyclopedia.publication import encode
    from suite_core.tests.test_encyclopedia_catalog_ui import click
    font_id = QFontDatabase.addApplicationFont('C:/Windows/Fonts/segoeui.ttf')
    assert font_id >= 0
    app.setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0], 10))
    apply_theme(app, theme)
    exported = tmp_path/'synthetic-export.json'; exported.write_bytes(encode(fixture(2)))
    original_bytes = exported.read_bytes()
    settings = tmp_path/'settings.json'
    window = create_window(settings_path=settings); host = window.catalog_host
    window.resize(viewport[0]-24, viewport[1]-64); window.show(); app.processEvents()
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *a: (str(exported), ''))
    output = os.environ.get('READER_EXPORT_CAPTURE')
    captures = []
    def capture(name):
        app.processEvents()
        if window.status.isVisible():
            assert window.status.height() >= window.status.heightForWidth(window.status.width())
        if output:
            dest = Path(output)/f'{theme}-{viewport[0]}-{outcome}'; dest.mkdir(parents=True, exist_ok=True)
            assert window.grab().save(str(dest/(name+'.png')))
            captures.append(name)
    if outcome != 'first':
        click(window, 'Open exported tag catalog'); pump(app, lambda: not host.busy)
        index = window.model.index(0, 0)
        QTest.mouseClick(window.table.viewport(), Qt.LeftButton, pos=window.table.visualRect(index).center())
    accepted, selection = window.publication, window._selected_id
    entered, release = Event(), Event()
    original_read = browser.read
    calls = []
    def delayed(path):
        calls.append(path); entered.set()
        assert release.wait(10)
        if outcome == 'refuse':
            raise ValueError('Synthetic refused export')
        return original_read(path)
    monkeypatch.setattr(browser, 'read', delayed)
    ticks = []; timer = QTimer(); timer.setInterval(20); timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start()
    try:
        capture('start')
        click(window, 'Open exported tag catalog' if outcome == 'first' else 'Refresh')
        pump(app, entered.is_set)
        QTest.qWait(1100); app.processEvents()
        assert len(ticks) >= 15 and max(b-a for a,b in zip(ticks, ticks[1:])) < .35
        assert host.busy and host.future is not None
        assert window.database_activity.isVisible() and window.database_elapsed.isVisible()
        assert '1s elapsed' in window.database_elapsed.text()
        assert window.cancel_work_button.isVisible() and window.cancel_work_button.isEnabled()
        assert window.publication is accepted and window._selected_id == selection
        assert not window.open_snapshot(exported) and len(calls) == 1
        assert 'Opening exported catalog' in window.status.text()
        capture('waiting')
        if outcome == 'cancel':
            QTest.mouseClick(window.cancel_work_button, Qt.LeftButton); app.processEvents()
            assert host.busy and 'current read' in window.status.text()
            capture('stopping')
        elif outcome == 'close':
            window.close(); app.processEvents()
            assert window.isVisible() and host._closing and window.cancel_close_button.isVisible()
            capture('close-held')
            QTest.mouseClick(window.cancel_close_button, Qt.LeftButton); app.processEvents()
            assert not host._closing and window.isVisible() and host.busy
            window.close(); app.processEvents()
            assert host._closing and not host.executor.closed
        release.set(); pump(app, lambda: not host.busy)
        if outcome == 'close':
            pump(app, lambda: host.executor.closed)
            assert not window.isVisible() and window.publication is accepted
        else:
            pump(app, lambda: not window.cancel_work_button.isVisible())
            assert not window.database_activity.isVisible() and not window.database_elapsed.isVisible()
            if outcome in ('cancel', 'refuse'):
                assert window.publication is accepted and window._selected_id == selection
                assert 'Previous' in window.status.text()
            else:
                assert window.publication is not None and host.accepted_source.kind == 'export'
                assert window._selected_id == selection
            capture('result')
            if outcome in ('cancel', 'refuse'):
                monkeypatch.setattr(browser, 'read', original_read)
                click(window, 'Refresh'); pump(app, lambda: not host.busy)
                assert not window.stale and window._selected_id == selection
                assert host.accepted_source.kind == 'export'
                capture('retry-ready')
        assert exported.read_bytes() == original_bytes and not settings.exists()
    finally:
        release.set(); timer.stop(); pump(app, lambda: not host.busy)
        window.close(); pump(app, lambda: host.executor.closed and not host.executor.close_held)
        QFontDatabase.removeApplicationFont(font_id)



@pytest.mark.parametrize('theme', ['light', 'dark'])
@pytest.mark.parametrize('viewport', [(1280, 720), (1920, 1080)])
def test_first_database_choices_keep_overflow_keyboard_and_loaded_policy(app, content_font, tmp_path, monkeypatch, theme, viewport):
    from suite_core.manager.command_bar import CommandBar
    from suite_core.tests.test_encyclopedia import fixture
    from suite_core.encyclopedia.publication import encode
    from suite_core.tests.test_encyclopedia_catalog_ui import click
    apply_theme(app, theme)
    original_init = CommandBar.__init__
    def configured_bar(bar, *args, **kwargs):
        original_init(bar, *args, **kwargs)
        bar.visible_limits['Home'] = 4
    monkeypatch.setattr(CommandBar, '__init__', configured_bar)
    settings = tmp_path / 'settings.json'
    window = create_window(settings_path=settings)
    host = window.catalog_host
    window.resize(viewport[0] - 24, viewport[1] - 64)
    output = os.environ.get('READER_FIRST_RUN_CAPTURE')
    def capture(name):
        app.processEvents(); window.start_body.activate()
        for label in (window.start_heading, window.empty_hint, window.status):
            if label.isVisible() and label.hasHeightForWidth():
                assert label.height() >= label.heightForWidth(label.width()), label.text()
        if output:
            dest = Path(output) / f'{theme}-{viewport[0]}'; dest.mkdir(parents=True, exist_ok=True)
            assert window.grab().save(str(dest / (name+'.png')))
    try:
        window.show(); app.processEvents(); window.bar.refresh_state()
        assert [c.key for c, button in window.bar._buttons if button.isVisible()] == [
            'encyclopedia:create-database', 'encyclopedia:open-database']
        assert window.bar.overflow.isVisible() and window.start_card.isVisible()
        assert not window.subtitle.isVisible() and window.status.parentWidget() is window.start_card
        assert not window.search.isVisible() and not window.browse_panes.isVisible()
        assert any(c.key == 'encyclopedia:defaults' for c in window.commands)
        capture('start')
        cancelled = []
        monkeypatch.setattr(QFileDialog, 'getExistingDirectory', lambda *args: (cancelled.append(True) or ''))
        button = next(button for c, button in window.bar._buttons if c.key == 'encyclopedia:open-database')
        button.setFocus(); QTest.keyClick(button, Qt.Key_Space); app.processEvents()
        assert cancelled == [True] and window.publication is None and not host.busy
        assert not settings.exists()
        exported = tmp_path / 'synthetic-export.json'; exported.write_bytes(encode(fixture(2)))
        original_bytes = exported.read_bytes()
        monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *args: (str(exported), ''))
        click(window, 'Open exported tag catalog')
        pump(app, lambda: not host.busy)
        assert window.publication is not None and host.accepted_source.kind == 'export'
        assert not window.start_card.isVisible() and window.subtitle.isVisible()
        assert window.status.parentWidget() is window.centralWidget()
        assert window.bar.visible_limits['Home'] == 4
        assert window.search.isVisible() and window.browse_panes.isVisible()
        assert exported.read_bytes() == original_bytes
        capture('export-open')
        index = window.model.index(0, 0)
        QTest.mouseClick(window.table.viewport(), Qt.LeftButton, pos=window.table.visualRect(index).center())
        selected = window._selected_id; accepted = window.publication
        bad = tmp_path / 'refused-export.json'; bad.write_text('{"not_a_catalog":true}', encoding='utf-8')
        monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *args: (str(bad), ''))
        click(window, 'Open exported tag catalog')
        pump(app, lambda: not host.busy)
        assert window.publication is accepted and window._selected_id == selected
        assert 'Previous view is retained' in window.status.text()
        assert exported.read_bytes() == original_bytes and window.bar.visible_limits['Home'] == 4
        capture('refused-retained')
    finally:
        window.close()
        pump(app, lambda: host.executor.closed and not host.executor.close_held)


def test_export_delivery_discards_changed_source_after_original_worker(app, tmp_path, monkeypatch):
    from dataclasses import replace
    import suite_core.encyclopedia.browser as browser
    from suite_core.tests.test_encyclopedia import fixture
    from suite_core.encyclopedia.publication import encode
    exported = tmp_path/'export.json'; exported.write_bytes(encode(fixture(2)))
    window = create_window(settings_path=tmp_path/'settings.json'); host = window.catalog_host
    assert window.open_snapshot(exported)
    pump(app, lambda: not host.busy)
    release, entered = Event(), Event()
    original_read = browser.read
    def delayed(path):
        entered.set(); assert release.wait(10)
        return original_read(path)
    monkeypatch.setattr(browser, 'read', delayed)
    try:
        assert window.open_snapshot(exported, refreshing=True)
        pump(app, entered.is_set)
        # Component injection represents an independently accepted replacement;
        # ordinary visible commands refuse overlap while the worker is held.
        replacement = replace(fixture(2), catalog_id='new-accepted-catalog')
        assert window.open_publication(replacement)
        accepted = host.accepted_source
        release.set(); pump(app, lambda: not host.busy)
        assert window.publication is replacement and host.accepted_source is accepted
        assert 'Earlier opening discarded' in window.status.text()
        assert not window.database_activity.isVisible() and not window.database_elapsed.isVisible()
    finally:
        release.set(); pump(app, lambda: not host.busy)
        window.close(); pump(app, lambda: host.executor.closed and not host.executor.close_held)
