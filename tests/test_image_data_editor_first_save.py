import copy
from unittest.mock import Mock
import pytest
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QDialog
from core import ImageRecord, TagDatabase, CharacterDatabase
from qt_app.status_notifications import FooterMessageBox
PASSIVE_INFORMATION=FooterMessageBox.information

@pytest.fixture
def editor_fixture(monkeypatch, tmp_path):
    import qt_app.image_data_editor as module
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app=QApplication.instance() or QApplication([])
    monkeypatch.setattr(module,"CATEGORIES_PATH",tmp_path/"categories.json")
    image=tmp_path/"raw.png"
    preview=QImage(8,8,QImage.Format_RGB32); preview.fill(0xff112233)
    assert preview.save(str(image))
    record=ImageRecord(image)
    record.annotation.source["synthetic_raw_generator"]={"raw":"exact payload", "unknown":{"future":17}}
    knowledge=TagDatabase(tmp_path/"tags.json"); knowledge.save()
    characters=CharacterDatabase(tmp_path/"characters"); characters.load()
    editor=module.ImageDataEditor(record,knowledge=knowledge,characters=characters,allow_ai=False,preview_image=preview)
    notices=[]
    monkeypatch.setattr(module.QMessageBox,"information",lambda *args,**kwargs:notices.append(args[1:]))
    monkeypatch.setattr(module.QMessageBox,"critical",Mock())
    editor.prose.setPlainText("A manually confirmed synthetic description.")
    yield module,editor,notices
    QDialog.done(editor,QDialog.Rejected); editor.deleteLater(); app.processEvents()

def metadata(monkeypatch,module,available):
    service=Mock(); service.is_available.return_value=available
    monkeypatch.setattr(module,"MetadataSyncService",Mock(return_value=service))
    return service

@pytest.mark.parametrize("available",[False,True])
def test_first_description_persists_without_metadata_preview(editor_fixture,monkeypatch,available):
    module,editor,notices=editor_fixture
    metadata(monkeypatch,module,available)
    preview=Mock(side_effect=AssertionError("first save must defer preview"))
    monkeypatch.setattr(module,"reconcile_embedded_with_preview",preview)
    before=editor.record.image_path.read_bytes()
    evidence=copy.deepcopy(editor.record.annotation.source)
    real_save=editor.record.save
    save=Mock(wraps=real_save); monkeypatch.setattr(editor.record,"save",save)
    assert editor._save_record()
    assert editor.persisted_changes
    assert save.call_args.kwargs["force"] is False
    saved=ImageRecord(editor.record.image_path)
    assert saved.annotation.plain_language==["A manually confirmed synthetic description."]
    assert saved.annotation.source["synthetic_raw_generator"]==evidence["synthetic_raw_generator"]
    assert editor.record.sidecar_path.exists()
    assert "A manually confirmed synthetic description." in editor.record.caption_path.read_text(encoding="utf-8")
    assert editor.record.image_path.read_bytes()==before
    preview.assert_not_called()
    assert bool(notices)==available
    if available:
        assert "were saved" in notices[0][1] and "left unchanged" in notices[0][1]

@pytest.mark.parametrize("existing",[False,True])
def test_save_failure_has_no_partial_write_or_success_notice(editor_fixture,monkeypatch,existing):
    module,editor,notices=editor_fixture
    if existing:
        editor.record.save(force=False,knowledge=editor.knowledge)
        editor._edit_snapshot=module.ImageRecordEditSnapshot.capture(editor.record)
    before={p:p.read_bytes() if p.exists() else None for p in (editor.record.image_path,editor.record.sidecar_path,editor.record.caption_path)}
    metadata(monkeypatch,module,True)
    monkeypatch.setattr(module,"reconcile_embedded_with_preview",Mock(return_value=True))
    original=copy.deepcopy(editor.record.annotation.to_dict())
    monkeypatch.setattr(editor.record,"save",Mock(side_effect=OSError("synthetic pre-write failure")))
    assert not editor._save_record()
    assert not editor.persisted_changes and not notices
    assert editor.record.annotation.to_dict()==original
    assert "not saved" in editor.save_status.text()
    for path,value in before.items():
        assert (path.read_bytes() if path.exists() else None)==value

def test_competing_first_sidecar_wins(editor_fixture,monkeypatch):
    module,editor,notices=editor_fixture
    metadata(monkeypatch,module,True)
    monkeypatch.setattr(module,"reconcile_embedded_with_preview",Mock(side_effect=AssertionError("no preview")))
    monkeypatch.setattr(editor,"_recover_stale_record",Mock(return_value=False))
    original_save=editor.record.save
    competitor=[]
    def race(**kwargs):
        current=ImageRecord(editor.record.image_path)
        current.annotation.plain_language=["Other writer owns this record."]
        current.save(force=False,knowledge=editor.knowledge)
        competitor.append((current.sidecar_path.read_bytes(),current.caption_path.read_bytes()))
        return original_save(**kwargs)
    monkeypatch.setattr(editor.record,"save",race)
    assert not editor._save_record()
    assert not editor.persisted_changes and not notices
    editor._recover_stale_record.assert_called_once()
    assert editor.record.sidecar_path.read_bytes()==competitor[0][0]
    assert editor.record.caption_path.read_bytes()==competitor[0][1]
    assert ImageRecord(editor.record.image_path).annotation.plain_language==["Other writer owns this record."]

@pytest.mark.parametrize("preview_failure",[False,True])
def test_existing_authority_preview_refusal_leaves_edits_pending(editor_fixture,monkeypatch,preview_failure):
    module,editor,notices=editor_fixture
    editor.record.save(force=False,knowledge=editor.knowledge)
    editor._edit_snapshot=module.ImageRecordEditSnapshot.capture(editor.record)
    before={p:p.read_bytes() for p in (editor.record.image_path,editor.record.sidecar_path,editor.record.caption_path)}
    original=copy.deepcopy(editor.record.annotation.to_dict())
    metadata(monkeypatch,module,True)
    if preview_failure:
        service=Mock(); service.preview.side_effect=ValueError("synthetic inspection failure")
        monkeypatch.setattr(module,"ImageKnowledgeReconciliationService",Mock(return_value=service))
        monkeypatch.setattr(module.QMessageBox,"warning",Mock())
        preview=Mock(wraps=module.reconcile_embedded_with_preview)
    else:
        # The real confirmation/cancel QTest is covered by test_image_save_confirmation_ui.
        preview=Mock(return_value=False)
    monkeypatch.setattr(module,"reconcile_embedded_with_preview",preview)
    save=Mock(side_effect=AssertionError("refused preview must not persist"))
    monkeypatch.setattr(editor.record,"save",save)
    assert not editor._save_record()
    preview.assert_called_once(); assert preview.call_args.kwargs["pending_editor_save"] is True
    assert not editor.persisted_changes and not notices
    assert editor.record.annotation.to_dict()==original
    assert editor.has_unsaved_changes()
    save.assert_not_called()
    for path,value in before.items(): assert path.read_bytes()==value


def test_visible_save_forwards_first_save_deferral_after_success(editor_fixture,monkeypatch,tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QMainWindow
    from qt_app.components import WindowStatusBar
    module,editor,notices=editor_fixture
    metadata(monkeypatch,module,True)
    monkeypatch.setattr(module,"reconcile_embedded_with_preview",Mock(side_effect=AssertionError("no first preview")))
    monkeypatch.setattr(module.QMessageBox,"information",PASSIVE_INFORMATION)
    parent=QMainWindow(); parent.setStatusBar(WindowStatusBar()); parent.resize(1920,1080)
    editor.setParent(parent,editor.windowFlags())
    parent.show(); editor.show(); QApplication.processEvents()
    QTest.mouseClick(editor.save_button,Qt.LeftButton)
    QApplication.processEvents()
    assert editor.result()==QDialog.Accepted and editor.persisted_changes
    footer=parent.statusBar().footer
    assert "Image Knowledge saved; Metadata unchanged" in footer.details.toPlainText()
    assert "were saved" in footer.details.toPlainText()
    assert ImageRecord(editor.record.image_path).annotation.plain_language==["A manually confirmed synthetic description."]
    import os
    if os.environ.get("FIRST_SAVE_CAPTURE_DIR"):
        from PySide6.QtGui import QFont,QFontDatabase
        from qt_app.theme import apply_theme
        from pathlib import Path
        if os.name=="nt":
            fonts=Path(os.environ.get("WINDIR","C:/Windows"))/"Fonts"
            for name in ("segoeui.ttf","segoeuib.ttf"): QFontDatabase.addApplicationFont(str(fonts/name))
            QApplication.instance().setFont(QFont("Segoe UI",10))
        apply_theme(QApplication.instance(),"dark",False)
        QApplication.processEvents()
        target=Path(os.environ["FIRST_SAVE_CAPTURE_DIR"]); target.mkdir(parents=True,exist_ok=True)
        assert parent.grab().save(str(target/"first-save-forwarded-footer-1920x1080.png"))
    editor.setParent(None,editor.windowFlags()); parent.close()
