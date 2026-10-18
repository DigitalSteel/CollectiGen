"""Visible standalone Ribbon routes over native synthetic catalogs."""
import time
import pytest

from suite_core.tests.test_encyclopedia import app
from tests.test_tag_catalog_connection import selected, connect
from tests.test_tag_database_pack_producer import reviewed_local_input


def click(window, label):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QToolButton
    window.bar.refresh_state()
    buttons = [b for b in window.bar.findChildren(QToolButton) if b.text() == label and b.isVisible()]
    if buttons:
        assert len(buttons) == 1, label
        QTest.mouseClick(buttons[0], Qt.LeftButton)
        return
    # Commands retain one named overflow when the window is narrow.
    overflow = window.bar.overflow
    assert overflow.isVisible(), label
    menu = overflow.menu()
    actions = [a for a in menu.actions() if a.text() == label]
    assert len(actions) == 1 and actions[0].isEnabled(), label
    from PySide6.QtCore import QTimer
    # InstantPopup enters a nested Qt loop before mouseClick returns.
    QTimer.singleShot(0, lambda: QTest.mouseClick(menu, Qt.LeftButton,
                                               pos=menu.actionGeometry(actions[0]).center()))
    QTest.mouseClick(overflow, Qt.LeftButton)


def settle(app, host):
    end = time.monotonic() + 20
    while host.busy and time.monotonic() < end:
        time.sleep(.005)  # Yield Python worker before Qt pumping.
        app.processEvents()
    app.processEvents()
    assert not host.busy


def test_visible_ribbon_opens_current_database_without_source_or_writes(app, selected, monkeypatch):
    from suite_core.encyclopedia.__main__ import create_window
    from PySide6.QtWidgets import QFileDialog
    settings, source, root = selected
    choice = connect(selected)
    review = choice.transactions.preview(__import__('suite_core.database_actions', fromlist=['DatabaseAction']).DatabaseAction.INSTALL_DEFAULT,
        base_release=choice.base_release, available_release=choice.available_release, pack=choice.pack, installed=False)
    choice.transactions.apply(review, confirmed_token=review.token)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*.json')}
    source.rename(source.with_name(source.name + '-unavailable'))
    monkeypatch.setattr(QFileDialog, 'getExistingDirectory', lambda *a: str(root))
    window = create_window(settings_path=settings)
    window.resize(1440, 900); window.show(); app.processEvents()
    assert window.publication is None
    click(window, 'Open Tag Database')
    settle(app, window.catalog_host)
    assert window.publication is not None and window.model.total_count > 0
    assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob('*.json')}
    term = window.publication.snapshot.tags[0].display
    window.search.setText(term)
    window.table.setCurrentIndex(window.model.index(0, 0))
    chosen = window._selected_id
    click(window, 'Copy Tag Name')
    assert app.clipboard().text() == window.lookup.get(chosen).tag.display
    click(window, 'Refresh')
    settle(app, window.catalog_host)
    assert window.search.text() == term and window._selected_id == chosen
    assert not window.identity.isVisible()
    assert not window.examples_table.isVisible()
    window.close(); app.processEvents()
    assert window.catalog_host.executor.closed


def test_offline_help_update_unavailable_does_not_block_browsing(app, tmp_path, monkeypatch):
    from suite_core.encyclopedia.__main__ import create_window
    from PySide6.QtWidgets import QMessageBox
    calls = []
    monkeypatch.setattr(QMessageBox, 'information', lambda *a: calls.append(a[1:]))
    window = create_window(settings_path=tmp_path / 'reader-settings.json')
    window.resize(1440, 900); window.show(); app.processEvents()
    window.bar.select_group('Help'); app.processEvents()
    click(window, "What's New")
    click(window, 'About / Version')
    click(window, 'Check for Updates')
    assert len(calls) == 2 and 'unavailable' in window.status.text()
    assert not window.catalog_host.busy
    assert not (tmp_path / 'reader-settings.json').exists()
    window.bar.select_group('Home'); app.processEvents()
    assert any(c.label == 'Open Tag Database' and c.available() for c in window.commands)
    window.close(); app.processEvents()


def test_visible_optional_content_reuses_original_review_and_apply(app, selected):
    from suite_core.encyclopedia.__main__ import create_window
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    settings, source, root = selected
    window = create_window(settings_path=settings)
    window.resize(1440, 900); window.show(); app.processEvents()
    click(window, 'Use optional default tags')
    host, pane = window.catalog_host, window.catalog_host.pane
    pane.source_directory.setText(str(source)); pane.catalog_directory.setText(str(root))
    QTest.mouseClick(pane.create_catalog, Qt.LeftButton)
    QTest.mouseClick(pane.review_connection, Qt.LeftButton)
    settle(app, host)
    assert not root.exists()
    QTest.mouseClick(pane.confirm_connection, Qt.LeftButton)
    QTest.mouseClick(pane.connect_catalog, Qt.LeftButton)
    settle(app, host)
    QTest.mouseClick(pane.preview, Qt.LeftButton)
    settle(app, host)
    assert host.controller.review.can_apply
    QTest.mouseClick(pane.confirm, Qt.LeftButton)
    QTest.mouseClick(pane.apply, Qt.LeftButton)
    settle(app, host)
    assert window.publication is not None and window.model.total_count > 0
    assert (root / 'tag_catalog_snapshot.json').exists()
    assert host.accepted_source.kind == 'native' and host.accepted_source.path == root
    # A native reader must refresh current data, even when its derived export is
    # damaged. This explicit synthetic corruption is never repaired by Open.
    export = root / 'tag_catalog_snapshot.json'
    export.write_bytes(b'corrupt derived cache')
    host.dialog.close(); app.processEvents()
    click(window, 'Refresh'); settle(app, host)
    assert window.model.total_count > 0 and export.read_bytes() == b'corrupt derived cache'
    host.dialog.close(); window.close(); app.processEvents()


def test_close_cancel_keeps_original_worker_and_help_until_physical_drain(app, tmp_path, monkeypatch):
    from threading import Event
    from suite_core.encyclopedia.__main__ import create_window
    from PySide6.QtWidgets import QFileDialog
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    entered, release = Event(), Event()
    window = create_window(settings_path=tmp_path / 'settings.json')
    original_executor = window.catalog_host.executor
    def read(root, *args):
        entered.set()
        assert release.wait(10)
        return None  # cancelled read must never be delivered
    monkeypatch.setattr(window.catalog_host.service, 'open_catalog', read)
    monkeypatch.setattr(QFileDialog, 'getExistingDirectory', lambda *a: str(tmp_path))
    window.resize(1440, 900); window.show(); app.processEvents()
    click(window, 'Open Tag Database')
    assert entered.wait(2)
    window.close(); app.processEvents()
    assert window.isVisible() and window.catalog_host._closing
    assert not window.help_commands.closing and not original_executor.closed
    # Close hasn't committed: offline Help and original timer remain retained.
    QTest.mouseClick(window.cancel_close_button, Qt.LeftButton)
    assert not window.catalog_host._closing
    assert window.catalog_host.executor is original_executor
    release.set()
    settle(app, window.catalog_host)
    assert window.publication is None
    window.close(); app.processEvents()
    assert original_executor.closed and window.help_commands.closing


def test_failed_entry_retains_exact_worker_owner(app, tmp_path, monkeypatch):
    from threading import Event
    from suite_core.encyclopedia.__main__ import main, _HELD_ENTRIES
    entered, release = Event(), Event()
    retained = []
    def probe(window, application):
        def read(root, *args):
            entered.set()
            assert release.wait(10)
            return None
        monkeypatch.setattr(window.catalog_host.service, 'open_catalog', read)
        window.catalog_host.open_database(tmp_path)
        assert entered.wait(2)
        retained.append(window)
        raise RuntimeError('injected entry failure')
    try:
        with pytest.raises(RuntimeError, match='injected') as caught:
            main([], settings_path=tmp_path / 'settings.json', entry_probe=probe)
        window = retained[0]
        assert caught.value.encyclopedia_entry_owner is window.catalog_host
        assert _HELD_ENTRIES[-1][0] is window
        assert not window.catalog_host.executor.closed
    finally:
        release.set()
        if retained:
            settle(app, retained[0].catalog_host)
            retained[0].close(); app.processEvents()
            _HELD_ENTRIES[:] = [r for r in _HELD_ENTRIES if r[0] is not retained[0]]


def test_saved_interrupted_import_reopens_and_visible_recovery_reloads_reader(app, selected, monkeypatch):
    from core import tag_catalog_connection as connection
    from suite_core.database_actions import DatabaseAction
    from suite_core.encyclopedia.__main__ import create_window
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    settings, _source, root = selected
    choice = connect(selected)
    before = {p.relative_to(root): p.read_bytes() for p in (root / 'data').rglob('*.json')}
    review = choice.transactions.preview(DatabaseAction.INSTALL_DEFAULT,
        base_release=choice.base_release, available_release=choice.available_release,
        pack=choice.pack, installed=False)
    atomic = connection.native._atomic_json
    def interrupt(path, value):
        if path.name == 'recovery.json' and value.get('status') == 'complete':
            raise SystemExit('synthetic durable completion interruption')
        return atomic(path, value)
    monkeypatch.setattr(connection.native, '_atomic_json', interrupt)
    with pytest.raises(SystemExit):
        choice.transactions.apply(review, confirmed_token=review.token)
    monkeypatch.setattr(connection.native, '_atomic_json', atomic)
    window = create_window(settings_path=settings)
    window.resize(1440, 900); window.show(); app.processEvents()
    click(window, 'Use optional default tags')
    host, pane = window.catalog_host, window.catalog_host.pane
    QTest.mouseClick(pane.open_connection, Qt.LeftButton)
    settle(app, host)
    assert host.controller.choice.observation_status == 'recovery_pending'
    assert pane.recover.isEnabled() and pane.receipts.count() == 1
    QTest.mouseClick(pane.recover, Qt.LeftButton)
    settle(app, host)
    assert not host.controller.error
    assert before == {p.relative_to(root): p.read_bytes() for p in (root / 'data').rglob('*.json')}
    assert window.publication is not None and window.model.total_count == 0
    host.dialog.close(); window.close(); app.processEvents()


def test_visible_saved_import_publication_retry_only_reloads(app, selected, monkeypatch):
    from suite_core.encyclopedia.__main__ import create_window
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    settings, _source, root = selected
    choice = connect(selected)
    window = create_window(settings_path=settings)
    window.resize(1440, 900); window.show(); app.processEvents()
    click(window, 'Use optional default tags')
    host, pane = window.catalog_host, window.catalog_host.pane
    QTest.mouseClick(pane.open_connection, Qt.LeftButton); settle(app, host)
    transactions = host.controller.choice.transactions
    reload = transactions.reload
    monkeypatch.setattr(transactions, 'reload', lambda **kwargs: (_ for _ in ()).throw(OSError('synthetic publication hold')))
    QTest.mouseClick(pane.preview, Qt.LeftButton); settle(app, host)
    QTest.mouseClick(pane.confirm, Qt.LeftButton)
    QTest.mouseClick(pane.apply, Qt.LeftButton); settle(app, host)
    assert host.controller.result.publication_error and not pane.apply.isEnabled()
    before = {p.relative_to(root): p.read_bytes() for p in (root / 'data').rglob('*.json')}
    receipts = {p: p.read_bytes() for p in (root / 'backups').rglob('recovery.json')}
    monkeypatch.setattr(transactions, 'reload', reload)
    monkeypatch.setattr(host.controller.choice.native_adapter, 'apply', lambda *a: pytest.fail('Retry cannot reimport'))
    QTest.mouseClick(pane.reload, Qt.LeftButton); settle(app, host)
    assert window.publication is not None and window.model.total_count > 0
    assert before == {p.relative_to(root): p.read_bytes() for p in (root / 'data').rglob('*.json')}
    assert receipts == {p: p.read_bytes() for p in (root / 'backups').rglob('recovery.json')}
    host.dialog.close(); window.close(); app.processEvents()


def test_real_source_entry_visible_database_open_and_normal_close(app, selected, monkeypatch):
    from suite_core.encyclopedia.__main__ import main
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QFileDialog
    settings, _source, root = selected
    connect(selected)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*.json')}
    monkeypatch.setattr(QFileDialog, 'getExistingDirectory', lambda *a: str(root))
    captured, failures = [], []
    def probe(window, application):
        captured.append(window)
        started = time.monotonic()
        timer = QTimer(window); timer.setInterval(20)
        def tick():
            if time.monotonic() - started > 20:
                failures.append('Entry database route timed out')
                window.catalog_host.cancel(); timer.stop(); application.exit(1)
            elif window.publication is not None and not window.catalog_host.busy:
                timer.stop(); window.close(); application.exit(0)
        timer.timeout.connect(tick); timer.start()
        QTimer.singleShot(0, lambda: click(window, 'Open Tag Database'))
    assert main([], settings_path=settings, entry_probe=probe) == 0
    assert not failures and captured[0].publication is not None
    assert captured[0].catalog_host.executor.closed
    assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob('*.json')}


@pytest.fixture
def catalog_readable_font(qt_test_scope):
    from PySide6.QtGui import QFont, QFontDatabase
    font_id = QFontDatabase.addApplicationFont('C:/Windows/Fonts/segoeui.ttf')
    assert font_id >= 0
    qt_test_scope.app.setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0], 10))
    try:
        yield
    finally:
        QFontDatabase.removeApplicationFont(font_id)


def _fully_visible_in(widget, container):
    from PySide6.QtCore import QPoint, QRect
    mapped = QRect(widget.mapTo(container, QPoint(0, 0)), widget.size())
    return (widget.isVisible() and container.rect().contains(mapped)
            and widget.visibleRegion().contains(widget.rect()))


@pytest.mark.parametrize('theme', ['light', 'dark'])
@pytest.mark.parametrize('viewport', [(1000, 740), (1800, 960)])
def test_visible_saved_reload_keeps_actions_and_completed_status_reachable(
        qt_test_scope, selected, tmp_path, theme, viewport, catalog_readable_font):
    from pathlib import Path
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from core.category_registry import CategoryRegistry
    from core.tag_database import TagDatabase
    from core.tag_default_acquisition import TagDefaultAcquisition, TAG_DATABASE_COMPONENT
    from tests.test_component_acquisition import service as acquisition
    from suite_core.encyclopedia.__main__ import create_window
    from qt_app.theme import apply_theme

    connect(selected)
    settings, source, root = selected
    categories = CategoryRegistry(root / 'data/defaults/categories.json', root / 'data/user/categories.json')
    category = categories.add('Visible reload checks'); categories.save_user()
    tags = TagDatabase(root / 'data/user/tags.json')
    tags.add_known_tag('visible_reload_probe', category=category.id, properties={'note': 'preserve'})
    tags.save_verified()
    protected = {p: p.read_bytes() for p in (root / 'data/user/tags.json', root / 'data/user/categories.json')}
    apply_theme(qt_test_scope.app, theme, False)
    # Admit the actual acquisition group without requesting network/content work.
    # This retains the same vertical competition as the frozen native capture.
    def enroll(host):
        return dict(service=TagDefaultAcquisition(acquisition=acquisition(tmp_path), component_id=TAG_DATABASE_COMPONENT),
                    observe_baseline=lambda: pytest.fail('No acquisition observation requested'),
                    package_component=None, preparation_directory=None, channel='beta')
    window = create_window(settings_path=settings, optional_content_factory=enroll)
    qt_test_scope.show(window, size=(1920, 1080))
    host = window.catalog_host
    captures = Path('dist/candidates/reader-catalog-reload-ui-20261017/source-captures')
    captures.mkdir(parents=True, exist_ok=True)
    stem = f'{theme}-{viewport[0]}x{viewport[1]}'
    try:
        click(window, 'Use optional default tags')
        qt_test_scope.show(host.dialog, size=viewport)
        pane = host.pane
        assert host.content_box.isVisible()
        QTest.mouseClick(pane.open_connection, Qt.LeftButton)
        settle(qt_test_scope.app, host)
        assert not host.controller.error
        assert pane.source_directory.text() == str(source)
        assert pane.catalog_directory.text() == str(root)
        assert not pane.connection_options.isChecked() and not pane.connection_box.isVisible()
        assert 'Verifying' not in window.status.text()
        assert host.content_status.text() == window.status.text() == host.controller.reason
        assert _fully_visible_in(pane.reload, host.dialog)
        assert _fully_visible_in(pane.status, host.dialog)
        assert host.dialog.grab().save(str(captures / f'{stem}-connected.png'))
        QTest.mouseClick(pane.connection_options, Qt.LeftButton)
        qt_test_scope.settle(2)
        assert pane.connection_box.isVisible()
        assert _fully_visible_in(pane.reload, host.dialog)
        assert _fully_visible_in(pane.status, host.dialog)
        assert host.dialog.grab().save(str(captures / f'{stem}-expanded.png'))
        # No scrollbar, picker, private handler or offscreen invocation is used.
        QTest.mouseClick(pane.reload, Qt.LeftButton)
        settle(qt_test_scope.app, host)
        assert not host.controller.error
        assert window.publication is not None and window.model.total_count > 0
        window.search.setText('visible_reload_probe'); QTest.qWait(250)
        assert window.model.rowCount() > 0
        assert all(p.read_bytes() == raw for p, raw in protected.items())
        assert 'Verifying' not in host.content_status.text()
        assert _fully_visible_in(pane.reload, host.dialog)
        assert host.dialog.grab().save(str(captures / f'{stem}-reloaded.png'))
    finally:
        if host.dialog is not None: host.dialog.close()
        window.close(); qt_test_scope.settle(2)


def test_saved_connection_failure_replaces_pending_status_and_retains_settings(
        qt_test_scope, tmp_path, monkeypatch, catalog_readable_font):
    from threading import Event
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from core import tag_catalog_connection
    from suite_core.encyclopedia.__main__ import create_window
    entered, release = Event(), Event()
    original = tag_catalog_connection.open_saved_connection
    def held(path):
        entered.set()
        assert release.wait(5), 'Test releases the original worker'
        return original(path)
    monkeypatch.setattr(tag_catalog_connection, 'open_saved_connection', held)
    window = create_window(settings_path=tmp_path / 'missing-settings.json')
    qt_test_scope.show(window, size=(1920, 1080))
    host = window.catalog_host
    try:
        click(window, 'Use optional default tags')
        qt_test_scope.show(host.dialog, size=(1000, 740))
        pane = host.pane
        QTest.mouseClick(pane.open_connection, Qt.LeftButton)
        assert entered.wait(2)
        host.poll(); qt_test_scope.settle(1)
        assert _fully_visible_in(pane.cancel, host.dialog)
        assert _fully_visible_in(pane.progress, host.dialog)
        release.set(); settle(qt_test_scope.app, host)
        assert host.controller.error
        assert pane.connection_box.isVisible()
        assert 'Verifying' not in window.status.text()
        assert host.content_status.text() == window.status.text() == host.controller.reason
        assert _fully_visible_in(pane.reload, host.dialog) and not pane.reload.isEnabled()
        assert not pane.cancel.isVisible()
    finally:
        release.set()
        if host.dialog is not None: host.dialog.close()
        window.close(); qt_test_scope.settle(2)
