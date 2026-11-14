"""Export decisions and retained writer lifetime, using generated loaded services."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from types import SimpleNamespace as NS
from threading import Event
from time import monotonic
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QWidget
from core.background_jobs import BackgroundJobManager
from core.generation_prompt_catalog import PromptCatalog, catalog_from_plan
from core.generation_planning import load_plan_with_digest
from qt_app.generation_prompt_catalog_actions import PromptCatalogExportDialog
from test_generation_prompt_catalog import payload


def application():
    app = QApplication.instance() or QApplication([])
    if not getattr(app, "_catalog_font_loaded", False):
        font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/segoeui.ttf"
        if font.is_file():
            families = QFontDatabase.applicationFontFamilies(QFontDatabase.addApplicationFont(str(font)))
            assert families
            app.setFont(QFont(families[0], 10))
        app.setStyle("Fusion")
        app._catalog_font_loaded = True
    return app


def capture(widget, name):
    app = QApplication.instance()
    app.processEvents()
    root = os.environ.get("COLLECTIGEN_SETUP_CAPTURES")
    if root:
        Path(root).mkdir(parents=True, exist_ok=True)
        assert widget.grab().save(str(Path(root) / (name + ".png")))


def wait(app, predicate):
    end = monotonic() + 15
    while not predicate():
        assert monotonic() < end
        app.processEvents(); QTest.qWait(10)


def test_export_uses_stock_save_and_retains_original_writer_during_close(tmp_path, monkeypatch):
    app = application()
    jobs = BackgroundJobManager(); page = QWidget(); page.state = NS(jobs=jobs)
    target = tmp_path / "catalog.json"
    dialog = PromptCatalogExportDialog(page, PromptCatalog.from_payload(payload()))
    entered, release = Event(), Event()
    import qt_app.generation_prompt_catalog_actions as actions
    stock = actions.save_plan
    def delayed(path, plan):
        entered.set()
        assert release.wait(10)
        return stock(path, plan)
    monkeypatch.setattr(actions, "_ready", lambda _: True)
    monkeypatch.setattr(actions, "capture_prompt_catalog", lambda *_: dialog.catalog)
    page.tags, page.characters = object(), object()
    monkeypatch.setattr(actions, "save_plan", delayed)
    monkeypatch.setattr(actions.QFileDialog, "getSaveFileName", lambda *_: (str(target), ""))
    dialog.show(); capture(dialog, "catalog-export-review")
    QTest.mouseClick(dialog.save_button, Qt.LeftButton)
    wait(app, entered.is_set)
    original = dialog._task
    capture(dialog, "catalog-export-writing")
    dialog.close()
    assert dialog.isVisible() and dialog._task is original and dialog.has_active_background_work()
    release.set()
    wait(app, lambda: dialog._task is None)
    assert not dialog.isVisible()
    plan, _ = load_plan_with_digest(target)
    assert catalog_from_plan(plan).payload == payload()
    jobs.shutdown(wait=True); page.close()


def test_export_refuses_changed_review_and_preserves_existing_file(tmp_path, monkeypatch):
    app = application()
    jobs = BackgroundJobManager(); page = QWidget(); page.state = NS(jobs=jobs)
    page.tags, page.characters = object(), object()
    catalog = PromptCatalog.from_payload(payload())
    dialog = PromptCatalogExportDialog(page, catalog)
    target = tmp_path / "preserve.json"; target.write_bytes(b"existing unrelated file")
    import qt_app.generation_prompt_catalog_actions as actions
    monkeypatch.setattr(actions, "_ready", lambda _: True)
    monkeypatch.setattr(actions, "capture_prompt_catalog", lambda *_: catalog)
    monkeypatch.setattr(actions.QFileDialog, "getSaveFileName", lambda *_: (str(target), ""))
    dialog.show(); QTest.mouseClick(dialog.save_button, Qt.LeftButton)
    wait(app, lambda: dialog._task is None)
    assert target.read_bytes() == b"existing unrelated file"
    assert "Replacing an existing generation plan requires its exact loaded digest token" in dialog.status.text()
    capture(dialog, "catalog-export-preserved-file-error")
    other = payload(); other["tags"][0]["display"] = "changed Tag"
    monkeypatch.setattr(actions, "capture_prompt_catalog", lambda *_: PromptCatalog.from_payload(other))
    QTest.mouseClick(dialog.save_button, Qt.LeftButton)
    assert dialog._task is None and "Knowledge changed" in dialog.status.text()
    dialog.close(); jobs.shutdown(wait=True); page.close()
