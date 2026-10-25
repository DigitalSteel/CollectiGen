"""Standalone policy on stock Setup widgets/search, preserving supported drafts."""
from unittest.mock import Mock
from pathlib import Path
import os
from dataclasses import asdict
from functools import partial
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from core.settings import AppSettings
from core.dependencies import DependencyService
from qt_app.pages import SetupPage
from suite_core.media_organizer.organizer_workflow_corrections import _restrict_setup_execution


@pytest.fixture
def setup(qt_test_scope, tmp_path):
    if os.environ.get("COLLECTIGEN_ORGANIZER_FIX_CAPTURE_DIR"):
        from PySide6.QtGui import QFont, QFontDatabase
        font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "segoeui.ttf"
        assert QFontDatabase.addApplicationFont(str(font)) >= 0
        qt_test_scope.app.setFont(QFont("Segoe UI", 10))
    settings = AppSettings(ai_models_directory=str(tmp_path / 'models'))
    settings.save = Mock()
    page = SetupPage(settings, dependency_service_factory=partial(DependencyService, capability_profile='organizer-image'),
                     defer_expensive_initialization=True)
    qt_test_scope.show(page)
    return page, settings, qt_test_scope


def test_policy_hides_unsupported_rows_tabs_and_search_targets_but_preserves_analysis(setup):
    page, settings, scope = setup
    snapshots = asdict(settings)
    _restrict_setup_execution(page); scope.settle()
    for key in ('generation', 'training'):
        button = page.workflow_default_buttons[key]
        assert button.isHidden() and not button.isEnabled()
        layout = button.parentWidget().layout(); row = layout.getItemPosition(layout.indexOf(button))[0]
        assert layout.itemAtPosition(row, 0).widget().isHidden()
    for tabs, group, key in ((page.ai_sections, 'ai', 'generation'), (page.component_sections, 'components', 'training')):
        index = page._subsection_indexes[group][key]
        assert not tabs.isTabVisible(index) and not tabs.isTabEnabled(index)
    capture = os.environ.get("COLLECTIGEN_ORGANIZER_FIX_CAPTURE_DIR")
    if capture:
        directory = Path(capture); directory.mkdir(parents=True, exist_ok=True)
        page.grab().save(str(directory / "setup-supported-defaults.png"))
    targets = page.navigator.destinations()
    labels = {target.label for target in targets}
    assert 'Local video setup' not in labels and 'Trainer software' not in labels
    assert all(page.workflow_default_buttons[key].isEnabled() for key in ('prepare', 'automation', 'analysis'))
    assert any(target.control is page.ai_memory.mode for target in targets)
    assert any(target.control is page.joy_prompt for target in targets)
    assert any(target.control is page.dependency_panel.joy_manage_button for target in targets)
    assert asdict(settings) == snapshots and not settings.save.called
    # Idempotence keeps the same controls/destinations rather than rebuilding editors.
    identities = tuple(id(target) for target in targets)
    _restrict_setup_execution(page)
    assert tuple(id(target) for target in page.navigator.destinations()) == identities


def test_visible_search_reaches_supported_memory_draft_without_revealing_execution(setup):
    page, settings, scope = setup
    _restrict_setup_execution(page)
    search = page.navigator.search
    QTest.mouseClick(search, Qt.LeftButton); QTest.keyClicks(search, 'AI memory policy'); scope.settle()
    QTest.keyClick(search, Qt.Key_Return); scope.settle()
    assert page.ai_memory.mode.isVisible()
    capture = os.environ.get("COLLECTIGEN_ORGANIZER_FIX_CAPTURE_DIR")
    if capture: page.grab().save(str(Path(capture) / "setup-supported-memory.png"))
    before = page.ai_memory.mode.currentIndex()
    QTest.keyClick(page.ai_memory.mode, Qt.Key_Down); scope.settle()
    assert page.ai_memory.mode.currentIndex() != before and page.has_unsaved_changes()
    assert not settings.save.called
    search.selectAll(); QTest.keyClicks(search, 'Local video setup'); scope.settle()
    assert not any(page.navigator.section_list.item(i).data(Qt.UserRole).control is page.video_setup_button
                   for i in range(page.navigator.section_list.count()))
    assert not page.ai_sections.isTabVisible(page._subsection_indexes['ai']['generation'])


def test_unfiltered_shared_setup_retains_execution_destinations(setup):
    page, settings, scope = setup
    assert page.ai_sections.isTabVisible(page._subsection_indexes['ai']['generation'])
    assert page.component_sections.isTabVisible(page._subsection_indexes['components']['training'])
    assert any(target.control is page.video_setup_button for target in page.navigator.destinations())
    assert page.workflow_default_buttons['generation'].isEnabled()


def test_composed_window_applies_policy_when_stock_setup_materializes(setup):
    from PySide6.QtWidgets import QWidget
    from suite_core.media_organizer.organizer_workflow_corrections import with_workflow_corrections
    page, settings, scope = setup
    class Shell(QWidget):
        def __init__(self):
            super().__init__(); self.startup_cancelled = True
        def _ensure_page(self, index):
            assert index == 6
            self.setup_page = page
            return "canonical materialization"
    window = with_workflow_corrections(Shell)()
    scope.show(window)
    assert window._ensure_page(6) == "canonical materialization"
    assert window.setup_page is page
    assert not page.ai_sections.isTabVisible(page._subsection_indexes['ai']['generation'])
    assert page.ai_sections.isTabVisible(page._subsection_indexes['ai']['descriptions'])
