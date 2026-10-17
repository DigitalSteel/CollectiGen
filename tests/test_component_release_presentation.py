"""Synthetic presentation/visible notes route; no native root or user profile."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
from datetime import datetime, timezone
import ast, json
import pytest
from PySide6.QtWidgets import QApplication, QLabel, QToolButton, QComboBox, QTextBrowser, QDialogButtonBox
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFontDatabase, QFont
from PySide6.QtTest import QTest
from core.component_release_identity import display_component_version
from suite_core.manager.window import create_window, unconfigured, NOTES
from qt_app.theme import apply_theme


@pytest.fixture(scope='session')
def app():
    app=QApplication.instance() or QApplication([])
    font=Path(os.environ['SystemRoot'])/'Fonts/segoeui.ttf'
    assert QFontDatabase.addApplicationFont(str(font))>=0
    app.setFont(QFont('Segoe UI',10))
    return app


def test_source_metadata_notes_and_wire_versions_remain_coherent():
    from suite_core.manager import version as manager
    from suite_core.encyclopedia import version as encyclopedia
    from suite_core.media_organizer import version as organizer
    root=Path(__file__).resolve().parents[1]
    for directory,module,wire,display in [('manager',manager,'0.3.2-alpha.1','a.0.3.2'),('encyclopedia',encyclopedia,'0.1.0-alpha.8','a.0.1.8'),('media_organizer',organizer,organizer.SOFTWARE_VERSION,organizer.DISPLAY_VERSION)]:
        folder=root/'suite_core'/directory
        assert getattr(module,'SOFTWARE_VERSION',getattr(module,'VERSION',None))==wire
        assert module.DISPLAY_VERSION==display
        assert json.loads((folder/'MODULE_MANIFEST.json').read_bytes())['software_version']==wire
        metadata=json.loads((folder/'RELEASE_PRESENTATION.json').read_bytes())
        assert metadata['legacy_wire_version']==wire and metadata['display_version']==display
        assert display in (folder/'WHATS_NEW.md').read_text(encoding='utf-8').splitlines()[0]
    assert NOTES==(root/'suite_core/manager/WHATS_NEW.md').read_text(encoding='utf-8')


@pytest.mark.parametrize('theme',['light','dark'])
def test_manager_brand_and_visible_notes_route(app,theme):
    apply_theme(app,theme,False)
    window=create_window(unconfigured(datetime.now(timezone.utc)),theme=theme)
    window.resize(1920,1080);window.show();app.processEvents()
    captures=Path(__file__).resolve().parents[1]/'.tmp/model-chat-integration-20261010/display-ui-32'
    captures.mkdir(parents=True,exist_ok=True)
    try:
        labels=[w for w in window.findChildren(QLabel) if w.text()=='a.0.3.2']
        assert len(labels)==1 and labels[0].isVisible()
        assert labels[0].width()>=labels[0].fontMetrics().horizontalAdvance(labels[0].text())
        assert window.grab().save(str(captures/f'{theme}-manager.png'))
        tabs=window.bar.tabs
        index=next(i for i in range(tabs.count()) if tabs.tabText(i)=='Help')
        QTest.mouseClick(tabs,Qt.LeftButton,pos=tabs.tabRect(index).center());app.processEvents()
        button=next(w for w in window.bar.findChildren(QToolButton) if w.text()=="What's New" and w.isVisible())
        observed=[];errors=[]
        def inspect_modal():
            dialog=app.activeModalWidget()
            try:
                assert dialog is not None
                chooser=dialog.findChild(QComboBox);browser=dialog.findChild(QTextBrowser)
                assert 'a.0.3.2' in chooser.currentText()
                assert 'a.0.3.2' in browser.toPlainText()
                assert 'embedded 0.3.2-alpha.1' in browser.toPlainText()
                assert dialog.grab().save(str(captures/f'{theme}-notes.png'))
                observed.append(True)
                close=dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Close)
                QTest.mouseClick(close,Qt.LeftButton)
            except BaseException as exc:
                errors.append(exc)
                if dialog is not None:dialog.reject()
        QTimer.singleShot(50,inspect_modal)
        QTest.mouseClick(button,Qt.LeftButton)
        assert not errors,repr(errors)
        assert observed==[True]
    finally:
        window.close();window.deleteLater();app.processEvents()


def test_encyclopedia_title_uses_display_not_snapshot_or_wire_version(app):
    from suite_core.encyclopedia.browser import EncyclopediaWindow
    window=EncyclopediaWindow()
    try:assert window.windowTitle()=='Tag Encyclopedia a.0.1.8'
    finally:window.close();window.deleteLater();app.processEvents()
