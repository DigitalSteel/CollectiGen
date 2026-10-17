"""Source pane evidence only; no Manager window, setup service or user data."""
from dataclasses import replace
import json
import os
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget, QVBoxLayout

from core.manager_dependency_status import (DependencyObservation, DependencyRoute,
    DependencyState, DependencyStatusSnapshot)
from qt_app.components import StatusActionPanel
from qt_app.theme import apply_theme
from suite_core.manager.dependency_status_ui import DependencyStatusPanel


@pytest.fixture
def app():
    from tools.qt_test_support import QtTestScope
    from qt_app.confidence import STATUS_COLORS
    original = dict(STATUS_COLORS)
    with QtTestScope() as scope:
        if not QFontDatabase.families() and os.name == 'nt':
            assert QFontDatabase.addApplicationFont(str(Path(os.environ['WINDIR']) / 'Fonts/segoeui.ttf')) >= 0
        scope.app.setFont(QFont('Segoe UI', 10))
        try:
            yield scope.app
        finally:
            STATUS_COLORS.clear()
            STATUS_COLORS.update(original)


def row(**changes):
    return replace(DependencyObservation('runner', 'Local AI runner',
        route=DependencyRoute.OWNED_RUNTIME, route_available=True), **changes)


def test_visible_action_dispatches_once_per_revision_and_keeps_widget(app):
    panel = DependencyStatusPanel(DependencyStatusSnapshot('one', (row(),)))
    seen = []
    panel.route_requested.connect(lambda *values: seen.append(values))
    panel.show()
    app.processEvents()
    button = panel.cards['runner'].action_button
    QTest.mouseClick(button, Qt.LeftButton)
    QTest.mouseClick(button, Qt.LeftButton)
    assert seen == [('runner', 'owned-runtime')] and not button.isEnabled()
    panel.set_snapshot(panel.snapshot)
    assert not button.isEnabled()
    panel.set_snapshot(DependencyStatusSnapshot('two', (row(route=DependencyRoute.BACKEND_CONNECTION),)))
    assert panel.cards['runner'].action_button is button and button.isEnabled()
    QTest.mouseClick(button, Qt.LeftButton)
    assert seen[-1] == ('runner', 'backend-connection') and len(seen) == 2
    panel.close()


def test_empty_busy_removed_and_unavailable_have_no_dispatch(app):
    panel = DependencyStatusPanel()
    seen = []
    panel.route_requested.connect(lambda *values: seen.append(values))
    assert panel.empty.text().startswith('No dependency status connected.')
    for revision, value in [('busy', row(state=DependencyState.BUSY)),
                            ('missing-route', row(route_available=False))]:
        panel.set_snapshot(DependencyStatusSnapshot(revision, (value,)))
        assert not panel.cards['runner'].action_button.isEnabled()
        panel._dispatch('runner')  # Component stale-call guard, not journey evidence.
    panel.set_snapshot(DependencyStatusSnapshot('removed'))
    panel._dispatch('runner')
    assert seen == [] and not panel.cards
    panel.close()


def test_revision_change_required_and_text_stays_plain(app):
    value = row(component='<b>Literal component</b>')
    panel = DependencyStatusPanel(DependencyStatusSnapshot('one', (value,)))
    with pytest.raises(ValueError):
        panel.set_snapshot(DependencyStatusSnapshot('one', (replace(value, state=DependencyState.BUSY),)))
    assert panel.cards['runner'].title_label.textFormat() == Qt.PlainText
    assert panel.snapshot.observations == (value,)
    panel.close()


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_1080p_source_presentation(app, tmp_path, theme):
    apply_theme(app, theme, color_blind_mode=True)
    surface = QWidget()
    surface.resize(1920, 1080)
    layout = QVBoxLayout(surface)
    stock = StatusActionPanel('Library view', 'Ready', 'Continue from the saved view.',
        'Browse Saved Library', None, primary=True, action_width=150)
    layout.addWidget(stock)  # Closest mature action/status pattern, same capture.
    rows = (
        row(detail='Open the existing runner workspace to explicitly check owned status.'),
        row(key='connection', component='Backend connection', state=DependencyState.BLOCKED,
            route=DependencyRoute.BACKEND_CONNECTION, blocking_reason='The last connection attempt failed. Retry from connection settings.', observed_at='Synthetic observation'),
        row(key='reader', component='Tag Encyclopedia reader', state=DependencyState.RESTART,
            route=DependencyRoute.READER_CONNECTION, blocking_reason='A saved connection is waiting for restart review; user data is retained.'),
        row(key='busy', component='Retained operation', state=DependencyState.BUSY,
            detail='The original owner is still draining its worker.'),
        row(key='unknown', component='Unconnected component', route=None, route_available=False))
    panel = DependencyStatusPanel(DependencyStatusSnapshot(theme, rows))
    layout.addWidget(panel, 1)
    surface.show()
    app.processEvents()
    for card in panel.cards.values():
        assert card.action_button.width() >= card.action_button.sizeHint().width()
        assert card.detail_label.height() >= card.detail_label.heightForWidth(card.detail_label.width())
        assert card.action_panel.geometry().left() >= card.card.geometry().right()
    capture = tmp_path / ('dependencies-' + theme + '.png')
    assert surface.grab().save(str(capture))
    print('DEPENDENCY_SOURCE_CAPTURE=' + str(capture))
    surface.close()
    app.processEvents()
