"""Synthetic owner-evidence setup transfer through stock Organizer producers."""
import json

import os

from pathlib import Path

from types import SimpleNamespace

import pytest

from PySide6.QtCore import QPoint, Qt

from PySide6.QtGui import QColor, QImage

from PySide6.QtTest import QTest

from core.annotation import CharacterAnnotation, ImageAnnotation

from core.generation_recipes import load_recipe

from core.generation_character_suggestions import qualified_look_relationship_ids

from core.image_record import _path_fingerprint

from core.knowledge_relationships import (
    SCOPED_CONSTRAINT, KnowledgeRelationship, KnowledgeRelationshipRegistry,
    RelationshipEndpoint,
)

from core.relationship_evaluator import RelationshipEvaluator

from qt_app.generation_character_setup_dialog import CharacterOutfitSetupDialog

class _Jobs:
    def __init__(self):
        self.calls = 0

    def submit_observed(self, _name, call, listener, **_kwargs):
        self.calls += 1
        job = SimpleNamespace(id=f"job-{self.calls}", state="queued", result=None,
                              error="", cancel_requested=False, update=lambda *_args: None)
        try:
            job.result = call(job)
            job.state = "complete"
        except Exception as exc:
            job.error = str(exc)
            job.state = "failed"
        listener(job)
        return job, ""

class _Index:
    def __init__(self, path):
        self.paths = tuple(path) if isinstance(path, (tuple, list)) else (path,)
        self.token = ("synthetic", 1)

    def evidence_snapshot_token(self):
        return self.token

    def all_records(self):
        return tuple(SimpleNamespace(path=str(path), sidecar_exists=True)
                     for path in self.paths)

    def connect(self):
        return SimpleNamespace(close=lambda: None)

    def get_if_fresh(self, _path, _db):
        return True

class _Dataset:
    scope_library_ids = ("library-a",)

    def __init__(self, path):
        self.index = _Index(path)

    def scoped_profile_managers(self):
        return ((SimpleNamespace(stable_id="library-a", name="Portraits"), object()),)

    def library_availability(self, _library_id):
        return "current"

    def identity_for_path(self, path):
        return SimpleNamespace(library_id="library-a", key="library-a:" + Path(path).name)

    def storage_availability(self, _path):
        return SimpleNamespace(readable=True)

def _source(tmp_path):
    path = tmp_path / "portrait.png"
    path.write_bytes(b"synthetic pixels")
    annotation = ImageAnnotation(
        image=path.name,
        characters=[CharacterAnnotation(slot=1, character_id="selene",
                                        tags={"appearance": ["silver hair", "blue eyes"]})],
        sdxl_tags=["Selene"],
        plain_language=["A portrait in a garden"],
        file_identity={"sha256": "a" * 64},
    )
    sidecar = path.with_suffix(".tags.json")
    sidecar.write_text(json.dumps(annotation.to_dict()), encoding="utf-8")
    return path, sidecar

def _dialog(qt_test_scope, tmp_path, path):
    return qt_test_scope.show(CharacterOutfitSetupDialog(
        _Jobs(), tmp_path / "saved", dataset=_Dataset(path),
        owner_kind="character", owner_id="selene", owner_name="Selene",
        representative_names=("Selene",), parent=None,
    ), size=(980, 820))

def _show_task(dialog, title):
    index = next(index for index in range(dialog.task_tabs.count())
                 if dialog.task_tabs.tabText(index) == title)
    assert dialog.task_tabs.isTabVisible(index)
    tab = dialog.task_tabs.tabBar()
    QTest.mouseClick(tab, Qt.LeftButton, pos=tab.tabRect(index).center())
    assert dialog.task_tabs.currentIndex() == index
    return dialog.task_tabs.widget(index)

def _choose(dialog, text):
    item = next(dialog.suggestion_tree.topLevelItem(index)
                for index in range(dialog.suggestion_tree.topLevelItemCount())
                if dialog.suggestion_tree.topLevelItem(index).text(1) == text)
    dialog.suggestion_tree.setCurrentItem(item)
    _check_item(dialog.suggestion_tree, item)
    return item

def test_organizer_owner_setup_exports_only_from_eligible_current_scope(qt_test_scope, tmp_path, monkeypatch):
    from PySide6.QtCore import QTimer
    _readable_font(qt_test_scope)
    from PySide6.QtWidgets import QFileDialog, QMainWindow, QMessageBox
    from suite_core.media_organizer.entry import make_window_type

    path, sidecar = _source(tmp_path)
    original = sidecar.read_bytes()
    dataset = _Dataset(path)
    eligible = SimpleNamespace(stable_id="library-a", name="Portraits", root=str(tmp_path), role="network_shared")
    restricted = SimpleNamespace(stable_id="private", name="Restricted", root="unused", role="local_only")
    dataset.scoped_profile_managers = lambda: ((eligible, None), (restricted, None))
    storage = tmp_path / "saved"
    dialog = qt_test_scope.show(CharacterOutfitSetupDialog(
        _Jobs(), storage, dataset=dataset, owner_kind="character", owner_id="selene",
        owner_name="Selene", representative_names=("Selene",), transfer_only=True,
    ), size=(980, 820))
    assert dialog.library_tree.topLevelItemCount() == 1
    item = dialog.library_tree.topLevelItem(0)
    assert item.checkState(0) == Qt.Unchecked
    with pytest.raises(ValueError, match="Image Suggestions"):
        dialog.verify_transfer_scope()
    assert dialog.jobs.calls == 0
    _show_task(dialog, "Image Suggestions")
    _check_item(dialog.library_tree, item)
    QTest.mouseClick(dialog.find_button, Qt.LeftButton)
    _choose(dialog, "silver hair")
    QTest.mouseClick(dialog.use_details_button, Qt.LeftButton)
    QTest.mouseClick(dialog.review_button, Qt.LeftButton)
    QTest.mouseClick(dialog.save_button, Qt.LeftButton)
    assert dialog.saved_path is not None
    saved = dialog.saved_path.read_bytes()
    assert dialog.grab().save(str(tmp_path / "reviewed-owner-setup.png"))

    host = qt_test_scope.show(QMainWindow())
    organizer = make_window_type(QMainWindow, current_application=lambda: qt_test_scope.app)
    output = tmp_path / "transfer.json"
    callback_errors = []
    def confirm_export():
        try:
            review = qt_test_scope.app.activeModalWidget()
            assert review.windowTitle() == "Review Setup Export for Manager"
            assert review.format.count() == 1 and not review.include_prompts.isChecked()
            assert review.grab().save(str(tmp_path / "review-setup-export.png"))
            _check_box(review.include_prompts)
            QTest.mouseClick(review.export_button, Qt.LeftButton)
        except BaseException as exc:
            callback_errors.append(str(exc))
            modal = qt_test_scope.app.activeModalWidget()
            if modal is not None: modal.reject()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args, **_kwargs: (str(output), ""))
    # Cancel settles the review without opening a file picker.
    picks = []
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_a, **_k: (picks.append("pick"), (str(output), ""))[1])
    QTimer.singleShot(0, lambda: qt_test_scope.app.activeModalWidget().reject())
    organizer.export_character_outfit_setup_for_manager(host, dialog, storage)
    assert not picks and not output.exists() and dialog.saved_path.read_bytes() == saved
    QTimer.singleShot(0, confirm_export)
    organizer.export_character_outfit_setup_for_manager(host, dialog, storage)
    assert load_recipe(output)["prompt_parts"] == [{"kind": "character", "text": "silver hair"}]
    assert str(path) not in output.read_text(encoding="utf-8")

    assert sidecar.read_bytes() == original
    assert "library-a" not in output.read_text(encoding="utf-8")
    assert "source_capture" not in output.read_text(encoding="utf-8")
    exported = output.read_bytes()
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _parent, _title, text: warnings.append(text))
    QTimer.singleShot(0, confirm_export)
    organizer.export_character_outfit_setup_for_manager(host, dialog, storage)
    assert warnings and output.read_bytes() == exported
    assert dialog.saved_path.read_bytes() == saved
    eligible.role = "local_only"
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _parent, _title, text: warnings.append(text))
    organizer.export_character_outfit_setup_for_manager(host, dialog, storage)
    assert warnings and "library scope" in warnings[-1]
    assert not callback_errors, callback_errors
    assert dialog.saved_path.read_bytes() == saved and sidecar.read_bytes() == original


def _readable_font(scope):
    from PySide6.QtGui import QFont, QFontDatabase
    ident = QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
    assert ident >= 0
    scope.app.setFont(QFont(QFontDatabase.applicationFontFamilies(ident)[0],10))


def _check_item(tree, item):
    from PySide6.QtWidgets import QStyle, QStyleOptionViewItem
    tree.scrollToItem(item)
    index=tree.indexFromItem(item,0)
    option=QStyleOptionViewItem()
    tree.itemDelegate().initStyleOption(option,index)
    option.rect=tree.visualRect(index)
    rect=tree.style().subElementRect(QStyle.SE_ItemViewItemCheckIndicator,option,tree)
    assert rect.isValid() and not rect.isEmpty()
    QTest.mouseClick(tree.viewport(),Qt.LeftButton,pos=rect.center())
    assert item.checkState(0)==Qt.Checked


def test_all_in_one_owner_authoring_keeps_existing_scope(qt_test_scope,tmp_path):
    path,_sidecar=_source(tmp_path)
    dialog=_dialog(qt_test_scope,tmp_path,path)
    assert not dialog._transfer_only
    assert dialog.library_tree.topLevelItem(0).checkState(0)==Qt.Checked
    dialog.verify_transfer_scope()
    dialog.reject()


def test_existing_organizer_owner_entry_exports_reviewed_setup(monkeypatch,tmp_path,qt_test_scope):
    import time
    from types import MethodType
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QMainWindow,QFileDialog
    from core.character_database import CharacterDatabase
    from core.tag_database import TagDatabase
    from core.settings import AppSettings
    from core.generation_execution import SwarmLocalAdapter
    from qt_app import main_window
    from qt_app.generation_saved_work_dialog import SetupExportReviewDialog
    from suite_core.media_organizer.entry import make_window_type
    _readable_font(qt_test_scope)
    profile=tmp_path/'profile';profile.mkdir()
    tags=TagDatabase(profile/'tags.json')
    tag=tags.add_known_tag('Selene',category='character_identity')
    owners=CharacterDatabase(profile)
    owner=owners.add_character('Selene',knowledge_tag={'tag_id':tag.id,'status':'reviewed','basis':'preferred_name'})
    image,sidecar=_source(tmp_path)
    annotation=json.loads(sidecar.read_text(encoding='utf-8'))
    annotation['characters'][0]['character_id']=owner.id
    sidecar.write_text(json.dumps(annotation),encoding='utf-8')
    original=sidecar.read_bytes()
    dataset=_Dataset(image)
    eligible=SimpleNamespace(stable_id='library-a',name='Selected synthetic library',root=str(tmp_path),role='cloud_synced')
    dataset.scoped_profile_managers=lambda:((eligible,None),)
    host=qt_test_scope.show(QMainWindow(),size=(1200,900))
    host.settings=AppSettings()
    host.state=SimpleNamespace(jobs=_Jobs(),dataset=dataset,knowledge=tags)
    Organizer=make_window_type(QMainWindow,current_application=lambda:qt_test_scope.app)
    host.export_character_outfit_setup_for_manager=MethodType(Organizer.export_character_outfit_setup_for_manager,host)
    monkeypatch.setattr(main_window,'USER_DATA_DIR',profile)
    empty=tmp_path/'empty-defaults';empty.mkdir()
    monkeypatch.setattr(main_window,'DEFAULTS_DIR',empty)
    monkeypatch.setattr(main_window,'RELATIONSHIPS_PATH',profile/'relationships.json')
    import core.paths as paths
    monkeypatch.setattr(paths,'MODEL_RESOURCES_PATH',tmp_path/'models.json')
    monkeypatch.setattr(SwarmLocalAdapter,'__init__',lambda *_a,**_k:pytest.fail('Setup export must not access a generator'))
    output=tmp_path/'owner-setup-for-manager.json'
    monkeypatch.setattr(QFileDialog,'getSaveFileName',lambda *_a,**_k:(str(output),''))
    phase=['choose'];errors=[];deadline=time.monotonic()+45
    timer=qt_test_scope.own_timer(QTimer());timer.setInterval(25)
    def drive():
        try:
            assert time.monotonic()<deadline,phase[0]
            setup=next((x for x in qt_test_scope.app.topLevelWidgets() if isinstance(x,CharacterOutfitSetupDialog) and x.isVisible()),None)
            review=next((x for x in qt_test_scope.app.topLevelWidgets() if isinstance(x,SetupExportReviewDialog) and x.isVisible()),None)
            if phase[0]=='choose' and setup is not None:
                assert setup._transfer_only and setup.library_tree.topLevelItemCount()==1
                item=setup.library_tree.topLevelItem(0);assert item.checkState(0)==Qt.Unchecked
                _show_task(setup,'Image Suggestions');_check_item(setup.library_tree,item)
                assert setup.grab().save(str(tmp_path/'01-owner-choose-scope.png'))
                phase[0]='find';QTest.mouseClick(setup.find_button,Qt.LeftButton)
            elif phase[0]=='find' and setup is not None and setup._review_task is None:
                assert setup._active_report is not None,setup.suggestion_status.text()
                _choose(setup,'silver hair')
                assert setup.grab().save(str(tmp_path/'02-owner-reviewed-evidence.png'))
                QTest.mouseClick(setup.use_details_button,Qt.LeftButton)
                QTest.mouseClick(setup.review_button,Qt.LeftButton)
                phase[0]='save';QTest.mouseClick(setup.save_button,Qt.LeftButton)
            elif phase[0]=='save' and review is not None:
                assert review.format.count()==1 and not review.include_prompts.isChecked()
                assert review.grab().save(str(tmp_path/'03-owner-export-review.png'))
                _check_box(review.include_prompts)
                phase[0]='export';QTest.mouseClick(review.export_button,Qt.LeftButton)
        except BaseException as exc:
            errors.append(str(exc));timer.stop()
            modal=qt_test_scope.app.activeModalWidget()
            if modal is not None:modal.reject()
    timer.timeout.connect(drive);timer.start()
    # Exact existing owner-entry handler in a generated source host. It opens
    # the real producer and export review; no native Tag/Reader route is used.
    main_window.TagManagerWindow.open_character_outfit_setup(host,'character',owner.id,characters=owners)
    timer.stop()
    assert not errors,errors
    assert phase[0]=='export' and output.is_file()
    assert load_recipe(output)['prompt_parts']==[{'kind':'character','text':'silver hair'}]
    assert sidecar.read_bytes()==original
    assert str(image) not in output.read_text(encoding='utf-8')
    import os
    pointer=os.environ.get('COLLECTIGEN_GEN13_EXPORT_POINTER')
    if pointer:Path(pointer).write_text(str(output),encoding='utf-8')
    assert host.close()


def _check_box(box):
    from PySide6.QtWidgets import QStyle,QStyleOptionButton
    option=QStyleOptionButton();box.initStyleOption(option)
    rect=box.style().subElementRect(QStyle.SE_CheckBoxIndicator,option,box)
    assert rect.isValid() and not rect.isEmpty()
    QTest.mouseClick(box,Qt.LeftButton,pos=rect.center())
    assert box.isChecked()
