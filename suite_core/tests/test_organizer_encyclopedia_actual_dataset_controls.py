"""Original shell/Dataset/gallery/correction controls; disposable offline fixtures.

Offscreen Qt evidence only: no compiled activation, user library, model, or Apply.
"""
import hashlib, json, os
import pytest
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractButton, QComboBox, QDialogButtonBox, QFileDialog, QInputDialog, QMessageBox, QTabBar

from tests.organizer_synthetic_shell import synthetic_shell_admission
from suite_core.tests.test_encyclopedia_editing_ui import modal_driver
from suite_core.tests.test_encyclopedia_catalog_ui import click as click_browser
from suite_core.tests.test_encyclopedia_downloaded_defaults import content_font


def wait(scope, predicate, label, timeout=25):
    deadline=monotonic()+timeout
    while not predicate() and monotonic()<deadline:
        scope.settle(1);sleep(.01)
    assert predicate(),label() if callable(label) else label


def button(parent,label):
    return next(b for b in parent.findChildren(QAbstractButton)
                if b.isVisible() and b.text().replace("&","")==label)


@pytest.mark.parametrize("theme",["light","dark"])
def test_visible_encyclopedia_handoff_uses_actual_dataset_review_without_apply(
        synthetic_shell_admission,tmp_path,monkeypatch,qt_test_scope,content_font,theme):
    from core import AppSettings, AppState, ImageRecord, knowledge_pack
    from core.app_state import _load_knowledge
    from suite_core.catalog_identity_candidate import CatalogIdentity,encode_identity
    from tests.test_dataset_restart_scan_reuse import _bound_dataset
    from qt_app.main_window import TagManagerWindow
    from qt_app.dataset_page import DatasetPage
    from qt_app.image_identity_correction_widget import ImageIdentityCorrectionWidget
    from suite_core.media_organizer.entry import make_window_type
    from suite_core.media_organizer.organizer_recovery_entry import with_dataset_recovery
    from suite_core.media_organizer.hosted_encyclopedia_overlay import with_hosted_encyclopedia_examples
    from suite_core.media_organizer.organizer_workflow_corrections import with_workflow_corrections
    from qt_app.theme import apply_theme
    apply_theme(qt_test_scope.app,theme,False)
    root=synthetic_shell_admission
    monkeypatch.setenv("TAG_MANAGER_UI_TEST","1")
    monkeypatch.delenv("TAG_MANAGER_PROFILE_NAVIGATION",raising=False)
    knowledge=_load_knowledge()
    tag=knowledge.add_known_tag("synthetic fox",category="colors")
    tag.aliases=["old synthetic fox"]
    knowledge._rebuild_lookup();knowledge.save()
    identity=CatalogIdentity(str(uuid4()),str(uuid4()))
    (root/"data/user/catalog_identity.json").write_bytes(encode_identity(identity))
    library=tmp_path/"disposable-library";library.mkdir()
    image=library/"synthetic.png";Image.new("RGB",(32,32),(30,70,110)).save(image)
    record=ImageRecord(image)
    record.annotation.sdxl_tags=["synthetic fox","old synthetic fox"]
    record.annotation.tag_identities={"synthetic fox":tag.id,"old synthetic fox":tag.id}
    record.modified=True;record.save_sidecar(force=True)
    nonmatching=library/"nonmatching.png";Image.new("RGB",(32,32),(110,70,30)).save(nonmatching)
    unrelated=ImageRecord(nonmatching)
    unrelated.annotation.sdxl_tags=["blue"]
    unrelated.annotation.tag_identities={"blue":"blue"}
    unrelated.modified=True;unrelated.save_sidecar(force=True)
    settings=AppSettings(ai_models_directory=str(root/"data/models"))
    settings_path=root/"data/user/settings.json"
    settings.save(settings_path)
    state=AppState(settings=settings)
    dataset,manager=_bound_dataset(state,library,[image,nonmatching],root/"data/cache")
    manager.analyze()  # Exactly the two supplied fixtures; no directory inventory scan.
    base=make_window_type(TagManagerWindow,current_application=lambda:qt_test_scope.app,settings_path=settings_path)
    cls=with_dataset_recovery(with_workflow_corrections(with_hosted_encyclopedia_examples(base)))
    window=qt_test_scope.show(cls(settings=settings,state=state,genuinely_new_profile=False),size=(1920,1080))
    browser=None
    def choice(dialog,expected):
        combo=dialog.findChild(QComboBox)
        assert combo.currentText()==expected
        QTest.mouseClick(dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok),Qt.LeftButton)
    monkeypatch.setattr(QFileDialog,"getExistingDirectory",lambda *args:str(root))
    originals={p:p.read_bytes() for p in (image,image.with_suffix(".tags.json"),nonmatching,nonmatching.with_suffix(".tags.json"))}
    destination=os.environ.get("COLLECTIGEN_ACTUAL_DATASET_EVIDENCE_ROOT")
    output=None
    if destination:
        output=Path(destination).resolve()/theme
        assert Path(__file__).resolve().parents[2]/".tmp" in output.parents
        output.mkdir(parents=True,exist_ok=False)
    def capture(widget,name):
        if output is not None:
            qt_test_scope.settle()
            assert widget.grab().save(str(output/(name+".png")))
    try:
        assert type(window.dataset_page) is DatasetPage
        wait(qt_test_scope,lambda:window.dataset_page._dataset_search_results_ready(),"Dataset indexed fixture")
        tabs=window.command_bar.tabs
        group=next(i for i in range(tabs.count()) if tabs.tabText(i)=="Knowledge")
        QTest.mouseClick(tabs,Qt.LeftButton,pos=tabs.tabRect(group).center())
        timer,errors=modal_driver(qt_test_scope,QInputDialog,lambda d:choice(d,"Browse a native Tag Database"))
        QTest.mouseClick(button(window,"Tag Encyclopedia"),Qt.LeftButton);timer.stop();assert not errors,errors
        browser=window._native_encyclopedia_route.browser
        click_browser(browser,"Open Tag Database")
        wait(qt_test_scope,lambda:not browser.catalog_host.busy and browser.catalog_host.organizer_receiver is not None,
             lambda:("Original native admission/hosted gallery enrollment",browser.status.text()))
        entry=window._hosted_encyclopedia_entry
        from suite_core.media_organizer.encyclopedia_image_gallery import HostedEncyclopediaImageGallery
        assert entry.browser is browser and type(entry.gallery) is HostedEncyclopediaImageGallery
        browser.resize(1920,1080)
        qt_test_scope.settle()
        assert entry.attachment.binding is entry.binding
        assert browser.catalog_host.organizer_receiver.host is window
        index=next(browser.model.index(row,0) for row in range(browser.model.rowCount())
                   if browser.model.index(row,0).data()=="synthetic fox")
        QTest.mouseClick(browser.table.viewport(),Qt.LeftButton,pos=browser.table.visualRect(index).center())
        qt_test_scope.settle()
        assert browser.examples_button.isEnabled()
        QTest.mouseClick(browser.examples_button,Qt.LeftButton)
        wait(qt_test_scope,lambda:entry.binding.example_queries_drained() and bool(entry.gallery.gallery.cards),
             lambda:("Original Find Examples completion",browser.examples_status.text(),entry.gallery.page_status.text()))
        assert set(entry.gallery._references)=={str(image)}
        assert nonmatching not in [Path(value) for value in entry.gallery._references]
        assert entry.pool.waitForDone(5000)
        qt_test_scope.settle()
        profile=state.dataset.registry.get(next(iter(entry.gallery._references.values())).library_id)
        assert profile is not None
        for card in entry.gallery.gallery.cards.values():
            assert card.library_label.text()==profile.name
            assert profile.stable_id not in card.library_label.text()
            assert "Library ID: "+profile.stable_id in card.library_label.toolTip()
            assert card.library_label.fontMetrics().horizontalAdvance(card.library_label.text()) <= card.library_label.width()
        capture(browser,"actual-find-examples-result")
        assert {p:p.read_bytes() for p in originals}==originals
        original_content=browser._organizer_original_content
        assert browser.bar.isVisible(), ("Original reader hidden after hosted attachment",browser.isVisible(),
            original_content.isVisible(),original_content.isHidden(),browser.bar.isHidden(),
            [(b.text(),b.isVisible()) for b in browser.bar.findChildren(QAbstractButton) if "Organizer" in b.text()])
        click_browser(browser,"Find Matching Images in Organizer")
        wait(qt_test_scope,lambda:not browser.catalog_host.busy and window.dataset_page.search.text()=="synthetic fox",
             "Original visible Dataset search")
        page=window.dataset_page
        assert window.stack.currentWidget() is page and page.search.isVisible()
        stage_tabs=next(t for t in page.findChildren(QTabBar)
                        if any(t.tabText(i).startswith("Partially Prepared") for i in range(t.count())))
        partial=next(i for i in range(stage_tabs.count()) if stage_tabs.tabText(i).startswith("Partially Prepared"))
        QTest.mouseClick(stage_tabs,Qt.LeftButton,pos=stage_tabs.tabRect(partial).center())
        wait(qt_test_scope,lambda:page._dataset_search_task is None and page._dataset_search_results_ready(),
             "Original accepted search completion in explicitly selected preparation stage")
        assert set(page.filtered)=={image} and nonmatching not in page.filtered
        wait(qt_test_scope,lambda:not state.jobs.active()
             and "0 active" in window.global_status_strip.overall_label.text()
             and not getattr(window,"_activity_queue_rows",()),"Original search/coordinator activity settled")
        assert page.gallery is not None and image in page.filtered
        capture(window,"actual-dataset-search")
        capture(browser,"actual-native-reader-hosted-gallery")
        assert {p:p.read_bytes() for p in originals}==originals
        timer,errors=modal_driver(qt_test_scope,QInputDialog,lambda d:choice(d,"old synthetic fox"))
        click_browser(browser,"Review Image Tag Names");timer.stop();assert not errors,errors
        wait(qt_test_scope,lambda:not browser.catalog_host.busy and page._image_tag_maintenance_dialog is not None
             and page._image_tag_maintenance_dialog.isVisible(),"Original correction review dialog")
        correction=page.image_identity_correction
        assert type(correction) is ImageIdentityCorrectionWidget
        assert correction.isVisible() and correction._matching_scope["stable_ids"]==(tag.id,)
        assert correction._matching_scope["source_names"]==("old synthetic fox",)
        assert correction._matching_preview is None and not correction.has_active_background_work()
        assert "did not start a scan" in correction.matching_status.text()
        assert not correction.matching_apply_button.isEnabled()
        capture(page._image_tag_maintenance_dialog,"actual-correction-scope-no-scan")
        assert {p:p.read_bytes() for p in originals}==originals
        QTest.mouseClick(correction.matching_audit_button,Qt.LeftButton)
        wait(qt_test_scope,lambda:not correction.has_active_background_work() and correction._matching_preview is not None,
             "Original read-only matching review completion")
        assert correction._matching_preview.will_change==1
        assert correction.matching_tree.topLevelItemCount()==1
        assert correction.matching_apply_button.isEnabled()
        capture(page._image_tag_maintenance_dialog,"actual-matching-preview")
        assert {p:p.read_bytes() for p in originals}==originals
        declined=[]
        def decline(dialog):
            assert dialog.windowTitle()=="Apply matching-image link corrections"
            declined.append(True)
            capture(dialog,"actual-apply-confirmation-declined")
            QTest.mouseClick(dialog.button(QMessageBox.No),Qt.LeftButton)
        timer,errors=modal_driver(qt_test_scope,QMessageBox,decline)
        QTest.mouseClick(correction.matching_apply_button,Qt.LeftButton);timer.stop();assert not errors,errors
        assert declined==[True] and not correction.has_active_background_work()
        assert {p:p.read_bytes() for p in originals}==originals
        QTest.mouseClick(page._image_tag_maintenance_dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Close),Qt.LeftButton)
        assert not page._image_tag_maintenance_dialog.isVisible()
        if output is not None:
            (output/"RESULT.json").write_text(json.dumps({
                "qualification":"Original shell, Dataset/gallery/correction controls with original native sender; offscreen Qt, disposable two-image fixture. No Apply/native package claim.",
                "viewport":[window.width(),window.height()],"qt_platform":os.environ.get("QT_QPA_PLATFORM"),
                "registered_font":qt_test_scope.app.font().family(),"theme":theme,
                "native_browser_viewport":[browser.width(),browser.height()],
                "friendly_library_badge":profile.name,"full_library_id_in_tooltip_only":True,"visible_search_gallery_contains_fixture":True,
                "search_task_drained_and_spec_accepted":True,"nonmatching_fixture_excluded":True,
                "captured_activity_footer_idle":True,
                "captured_activity_label":window.global_status_strip.overall_label.text(),
                "original_find_examples_completed":True,"matching_example_count":1,
                "preparation_stage":"Partially Prepared, explicitly selected through original visible tab; stock search preserves stage",
                "scope_accepted":True,"read_only_preview_will_change":1,"apply_declined":True,
                "image_and_sidecar_unchanged":True,
                "source_sha256":{name:hashlib.sha256((Path(__file__).resolve().parents[2]/name).read_bytes()).hexdigest()
                  for name in ("suite_core/media_organizer/entry.py","suite_core/media_organizer/encyclopedia_native_entry.py",
                    "suite_core/media_organizer/organizer_recovery_entry.py","suite_core/media_organizer/encyclopedia_entry.py",
                    "suite_core/media_organizer/encyclopedia_handoff_entry.py","suite_core/media_organizer/encyclopedia_image_gallery.py","suite_core/encyclopedia/catalog_ui.py",
                    "suite_core/encyclopedia/browser.py","qt_app/main_window.py","qt_app/dataset_page.py",
                    "qt_app/image_identity_correction_widget.py")},
                "runner_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "synthetic_before_sha256":{p.name:hashlib.sha256(raw).hexdigest() for p,raw in originals.items()},
                "synthetic_after_sha256":{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in originals}},indent=2)+"\n",encoding="utf-8")
    finally:
        if browser is not None:browser.close()
        window.close();qt_test_scope.settle()
        wait(qt_test_scope,lambda:not window.isVisible(),"Original guarded shell Close")
        assert state.shutdown_background_jobs(wait=True,timeout=5)
