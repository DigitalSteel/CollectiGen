"""Selected synthetic reference uses the existing protected viewer without a library host."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from types import SimpleNamespace as NS
import pytest
from PIL import Image
from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtTest import QTest
from core.settings import AppSettings
from qt_app.generation_image_setup_dialog import ImageGenerationSetupDialog
from test_manager_generator_setup_ui import application, wait_until, capture


def make_review(tmp_path, monkeypatch, *, settings, policy=None, changed=False):
    monkeypatch.setattr("qt_app.generation_image_setup_dialog.content_safety_policy", lambda: policy)
    image=tmp_path/"generated-reference.png"
    Image.new("RGB", (640, 480), "blue").save(image)
    review=ImageGenerationSetupDialog((), (), settings=settings, suggestion_source=image,
        source_identity={"revision":1}, source_current_provider=lambda:{"revision":2 if changed else 1})
    review.show()
    return review,image


@pytest.mark.parametrize("blur", [True,False])
def test_settings_only_preview_uses_unknown_protection_and_local_reveal(tmp_path, monkeypatch, blur):
    app=application()
    review,image=make_review(tmp_path,monkeypatch,settings=AppSettings(gallery_blur_unknown=blur))
    wait_until(app,lambda:review._source_preview_task is None)
    view=review.source_preview
    assert view.path==image and view.rating=="unknown" and not view.checking
    assert not view.viewer.image_label.pixmap().isNull()
    assert view._requires_blur is blur
    assert bool(view.viewer.image_label.graphicsEffect()) is blur
    assert view.reveal_button.isVisible() is blur
    if blur:
        assert view.reveal_button.geometry().right() <= view.width()
        assert view.protection_status.width() >= 100
        capture(review,"setup-protected-reference")
        QTest.mouseClick(view.reveal_button,Qt.LeftButton)
        assert view._temporarily_revealed and view.viewer.image_label.graphicsEffect() is None
        capture(review,"setup-locally-revealed-reference")
        QTest.mouseClick(view.reveal_button,Qt.LeftButton)
        assert not view._temporarily_revealed and view.viewer.image_label.graphicsEffect() is not None
    review.reject()


def test_missing_settings_and_policy_retains_preview_refusal(tmp_path, monkeypatch):
    app=application();review,_=make_review(tmp_path,monkeypatch,settings=None)
    assert review._source_preview_task is None and review.source_preview.path is None
    assert "content safety" in review.source_preview.viewer.image_label.text()
    review.reject();app.processEvents()


def test_existing_policy_keeps_protection_authority(tmp_path, monkeypatch):
    app=application();calls=[]
    def state(path,**kwargs):
        calls.append(path)
        return {"rating":"explicit","basis":"Synthetic policy", "checking":False,"requires_blur":True,"blurred":True}
    review,image=make_review(tmp_path,monkeypatch,settings=AppSettings(gallery_blur_unknown=False),policy=NS(preview_state=state))
    wait_until(app,lambda:review._source_preview_task is None)
    assert calls==[image] and review.source_preview.rating=="explicit"
    assert review.source_preview._requires_blur and review.source_preview.viewer.image_label.graphicsEffect() is not None
    review.reject()


def test_changed_source_refuses_decoded_preview(tmp_path, monkeypatch):
    app=application();review,_=make_review(tmp_path,monkeypatch,settings=AppSettings(),changed=True)
    wait_until(app,lambda:review._source_preview_task is None)
    assert review.source_preview.path is None
    assert "changed" in review.source_preview_note.text()
    review.reject()


def test_dismissed_preview_ignores_late_delivery(tmp_path, monkeypatch):
    app=application();review,_=make_review(tmp_path,monkeypatch,settings=AppSettings())
    review.reject()
    assert QThreadPool.globalInstance().waitForDone(5000)
    app.processEvents()
    assert review._source_preview_task is None and review.source_preview.path is None
