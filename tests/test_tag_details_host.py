"""Synthetic original forms bound to an explicitly selected native catalog."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QPushButton, QWidget, QMessageBox

from core import EventBus
from core.encyclopedia_editing import EncyclopediaEditingSession
from qt_app.knowledge_page import _TagDetailsDialog, KnowledgePage
from qt_app.tag_details_host import TagDetailsEditorHost, prepare_tag_details_host
from suite_core.encyclopedia.editing_ui import create_category_workspace
from suite_core.tests.test_encyclopedia_editing_ui import modal_driver
from tests.test_encyclopedia_editing import editing_catalog


_EDITOR_VISUAL=None
_EDITOR_FONT_IDS=[]


def _editor_capture(window,name,controls=()):
    """Opt-in owned offscreen capture; no live modal tree traversal."""
    if _EDITOR_VISUAL is None:return
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QFontInfo
    output,records=_EDITOR_VISUAL
    path=output/f"{len(records)+1:02d}-{name}.png"
    assert window.width()<=1920 and window.height()<=1080
    assert window.grab().save(str(path))
    geometries=[]
    for label,control in controls:
        point=control.mapTo(window,QPoint(0,0))
        geometries.append({"label":label,"visible":control.isVisible(),"enabled":control.isEnabled(),
            "rect":[point.x(),point.y(),control.width(),control.height()],
            "inside_window":point.x()>=0 and point.y()>=0 and point.x()+control.width()<=window.width() and point.y()+control.height()<=window.height()})
    records.append({"name":name,"file":str(path),"size":[window.width(),window.height()],
        "viewport":[1920,1080],"dpr":window.devicePixelRatioF(),"font":QFontInfo(window.font()).family(),
        "controls":geometries,"geometry_scope":"named controls only; no live modal tree traversal"})


@pytest.fixture
def editor_visual(request,qt_test_scope):
    import os,json,hashlib
    from pathlib import Path
    from PySide6.QtGui import QFont,QFontDatabase,QFontMetrics
    from qt_app.theme import apply_theme
    global _EDITOR_VISUAL
    destination=os.environ.get("COLLECTIGEN_EDITOR_VISUAL_ROOT")
    if not destination:pytest.skip("Opt-in synthetic editor capture scope")
    theme=request.node.callspec.params["theme"]
    scenario=request.node.callspec.params["scenario"]
    output=Path(destination).resolve()/f"{theme}-{scenario}"
    assert Path(__file__).resolve().parents[1]/".tmp" in output.parents
    output.mkdir(parents=True,exist_ok=False)
    fonts=Path(os.environ.get("WINDIR","C:/Windows"))/"Fonts"
    if not _EDITOR_FONT_IDS:
        for filename in ("segoeui.ttf","segoeuib.ttf"):
            font_id=QFontDatabase.addApplicationFont(str(fonts/filename))
            assert font_id>=0
            _EDITOR_FONT_IDS.append(font_id)
    app=qt_test_scope.app
    app.setFont(QFont("Segoe UI",10));apply_theme(app,theme)
    assert QFontMetrics(app.font()).inFontUcs4(ord("A"))
    records=[];_EDITOR_VISUAL=(output,records)
    yield output
    _EDITOR_VISUAL=None
    (output/"report.json").write_text(json.dumps({"theme":theme,"scenario":scenario,
        "platform":os.environ.get("QT_QPA_PLATFORM"),"viewport":[1920,1080],
        "font_scope":"Stock font files registered in this isolated pytest process only",
        "fonts_sha256":{name:hashlib.sha256((fonts/name).read_bytes()).hexdigest() for name in ("segoeui.ttf","segoeuib.ttf")},
        "captures":records,"qualification":"offscreen component controls; no native shell or full modal-tree geometry qualification"},indent=2),encoding="utf-8")


@pytest.fixture
def selected_host(tmp_path, qt_test_scope, monkeypatch, request):
    adapter=editing_catalog(tmp_path)
    if getattr(request,"param",None) in {"policy","policy_merge"}:
        import json
        defaults,user=adapter.root/"data/defaults",adapter.root/"data/user"
        path=defaults/"categories.json";payload=json.loads(path.read_bytes())
        payload["categories"].append({"id":"modifier_color","name":"Color","semantic_role":"modifier_color","reusable_modifier_family":True})
        path.write_text(json.dumps(payload),encoding="utf-8")
        (defaults/"modifiers.json").write_text(json.dumps({"groups":{"color":["blue"]}}),encoding="utf-8")
        path=user/"tags.json";payload=json.loads(path.read_bytes())
        payload["tags"].append({"id":"synthetic-blue","display":"blue","category":"modifier_color"})
        path.write_text(json.dumps(payload),encoding="utf-8")
    if getattr(request,"param",None) in {"merge","policy_merge"}:
        import json
        path=adapter.root/"data/user/tags.json"
        payload=json.loads(path.read_bytes())
        payload["tags"].append({"id":"synthetic-fox-donor","display":"synthetic fox donor","category":"animals"})
        path.write_text(json.dumps(payload),encoding="utf-8")
    if getattr(request,"param",False):
        import json
        from core.character_database import CharacterDatabase
        user=adapter.root/"data/user"
        character_rows=([{"id":"synthetic-character","name":"Synthetic character",
            "tag_associations":[{"tag":"fox","status":"reviewed"}]}]
            if request.param=="references" else [])
        for name,schema,key,rows in (
            ("series.json",CharacterDatabase.SERIES_SCHEMA_VERSION,"series",[{"id":"synthetic-source","name":"Synthetic source","future":{"keep":True}}]),
            ("characters.json",CharacterDatabase.CHARACTERS_SCHEMA_VERSION,"characters",character_rows),
            ("outfits.json",CharacterDatabase.OUTFITS_SCHEMA_VERSION,"outfits",[])):
            (user/name).write_text(json.dumps({"schema_version":schema,key:rows}),encoding="utf-8")
    session = EncyclopediaEditingSession.open(adapter)
    owner = SimpleNamespace(knowledge=session.tags, events=EventBus(),
        settings=SimpleNamespace(ignored_plain_language_sentences=()),
        dataset=SimpleNamespace(index=None),
        begin_long_operation=lambda *_: (True, None), finish_long_operation=lambda *_: None)
    from core.tag_order_workspace import CategoryWorkspaceSession
    from core.character_tag_bridge import CharacterTagBridge
    def forbidden(*args, **kwargs):
        pytest.fail("Standalone preparation must never recover implicitly")
    monkeypatch.setattr(CategoryWorkspaceSession, "recover_interrupted", forbidden)
    monkeypatch.setattr(CharacterTagBridge, "recover_interrupted", forbidden)
    prepared = prepare_tag_details_host(session, owner)
    assert (session.root / "data/user/characters.json").exists()==bool(getattr(request,"param",False))
    pool = QThreadPool()
    root = qt_test_scope.show(QWidget())
    workspace = create_category_workspace(session, parent=root, pool=pool)
    host = TagDetailsEditorHost(session, owner, preparation=prepared,
        category_workspace=workspace, pool=pool, parent=root)
    host.test_warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: host.test_warnings.append(args[2]))
    monkeypatch.setattr(QMessageBox, "information", lambda *args: host.test_warnings.append(args[2]))
    # Native discard confirmation is an explicit synthetic acceptance decision;
    # no extra real-modal loop may trap test cleanup after a failed assertion.
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
    qt_test_scope.settle()
    assert not getattr(workspace, "_using_external_search", False)
    yield host, session, workspace, owner
    host.page.request_background_shutdown()
    assert pool.waitForDone(10000)


def test_original_new_tag_cancel_writes_nothing(selected_host, qt_test_scope):
    host, session, workspace, _ = selected_host
    before = session.tags.path.read_bytes()
    def drive(dialog):
        assert not host.close_ready()
        QTest.keyClicks(host.page.tag_name, "Unsaved synthetic new Tag")
        QTest.mouseClick(dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Cancel), Qt.LeftButton)
    timer, errors = modal_driver(qt_test_scope, _TagDetailsDialog, drive)
    if _EDITOR_VISUAL is not None and hasattr(host,"visual_commands"):
        QTest.mouseClick(host.visual_commands["New SDXL Tag"],Qt.LeftButton)
    else:host.new_tag()
    timer.stop()
    assert not errors
    assert session.tags.path.read_bytes() == before
    assert workspace.session is session.category_session
    assert host.close_ready()


def test_original_create_edit_save_reopen_retains_borrowed_authority(selected_host, qt_test_scope):
    host, session, workspace, _ = selected_host
    identities = (session.tags, session.categories, session.category_session)
    phase = ["create"]
    def drive_create(dialog):
        assert not host.test_warnings, host.test_warnings
        assert host.busy or not session.reload_required, host.page.tag_save_status.text()
        if phase[0] == "create":
            _editor_capture(dialog,"tag-new",[("Tag name",host.page.tag_name)])
            host.page.tag_name.setText("synthetic new wolf")
            host.page.tag_category.setCurrentIndex(host.page.tag_category.findData("animals"))
            phase[0] = "wait"
            button = next(b for b in dialog.findChildren(QPushButton) if b.text() == "Create SDXL Tag")
            QTest.mouseClick(button, Qt.LeftButton)
        elif session.tags.find("synthetic new wolf") is not None and not host.busy:
            _editor_capture(dialog,"tag-created",[("Tag name",host.page.tag_name)])
            QTest.mouseClick(dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Cancel), Qt.LeftButton)
    timer, errors = modal_driver(qt_test_scope, _TagDetailsDialog, drive_create)
    if _EDITOR_VISUAL is not None and hasattr(host,"visual_commands"):
        QTest.mouseClick(host.visual_commands["New SDXL Tag"],Qt.LeftButton)
    else:host.new_tag()
    timer.stop()
    assert not errors
    record = session.tags.find("synthetic new wolf")
    assert record is not None and record.category == "animals"
    assert (session.tags, session.categories, session.category_session) == identities
    assert workspace.tags is session.tags and workspace.session is session.category_session
    phase[0] = "edit"
    def drive_edit(dialog):
        if phase[0] == "edit":
            _editor_capture(dialog,"tag-edit-basics",[("Tag name",host.page.tag_name),("Meaning notes",host.page.tag_notes)])
            host.page.tag_notes.setPlainText("Saved synthetic editor note")
            phase[0] = "wait"
            button = next(b for b in dialog.findChildren(QPushButton) if b.text() == "Save Tag")
            QTest.mouseClick(button, Qt.LeftButton)
        elif not host.busy and session.tags.tags[record.id].notes == "Saved synthetic editor note":
            _editor_capture(dialog,"tag-edit-saved",[("Meaning notes",host.page.tag_notes)])
            QTest.mouseClick(dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Close), Qt.LeftButton)
    timer, errors = modal_driver(qt_test_scope, _TagDetailsDialog, drive_edit)
    if _EDITOR_VISUAL is not None and hasattr(host,"visual_commands"):
        host.visual_selection["tag"]=record.id
        QTest.mouseClick(host.visual_commands["Edit Selected Tag"],Qt.LeftButton)
    else:host.edit_tag(record.id)
    timer.stop()
    assert not errors
    reopened = EncyclopediaEditingSession.open(session.adapter)
    assert reopened.tags.tags[record.id].notes == "Saved synthetic editor note"
    assert workspace.session is identities[2]
    assert host.close_ready()


def test_standalone_requires_prepared_exact_native_authority(selected_host):
    host, session, workspace, owner = selected_host
    with pytest.raises(ValueError, match="prepared"):
        KnowledgePage(owner, editor_context=host.page.editor_context)
    session.reload_required = True
    with pytest.raises(RuntimeError):
        host.edit_tag("fox")


def test_category_saved_delivery_synchronizes_original_tag_services_and_holds_failure(selected_host, monkeypatch):
    host, session, workspace, _ = selected_host
    session.create_category("Synthetic synchronized category")
    workspace.session.save(preserve_history=True)
    workspace.routine_change_saved.emit({})
    assert not session.reload_required
    assert host.page._tag_alias_service is session.services["tag_alias_service"]
    assert host.page.categories is workspace.categories is session.categories
    errors = []
    host.unavailable.connect(errors.append)
    def fail(_workspace):
        raise RuntimeError("Synthetic changed authority at delivery")
    monkeypatch.setattr(session, "adopt_category_workspace_save", fail)
    workspace.routine_change_saved.emit({})
    assert session.reload_required and not workspace.isEnabled() and errors


def test_one_category_completion_owner_preserves_gui_adoption_failure(selected_host, monkeypatch):
    from suite_core.encyclopedia.editing_ui import EncyclopediaEditingBridge
    host, session, workspace, _ = selected_host
    bridge=EncyclopediaEditingBridge(session,category_workspace=workspace,tag_editor=host)
    saved=[]
    bridge.saved.connect(lambda: saved.append(True))
    session.create_category("Synthetic saved but GUI held category")
    workspace.session.save(preserve_history=True)
    def fail(*args):
        raise RuntimeError("Synthetic GUI adoption failure after core readback")
    monkeypatch.setattr(host.page,"_adopt_tag_save_authority",fail)
    workspace.routine_change_saved.emit({})
    assert session.reload_required and not workspace.isEnabled() and not saved
    workspace.routine_change_saved.emit({})
    assert session.reload_required and not saved


def test_native_operation_adoption_retains_tag_draft_and_all_shared_objects(selected_host):
    import copy
    from core.tag_record_save import save_tag_details
    from tests.test_encyclopedia_editing import tag_request
    host,session,workspace,_=selected_host
    host.page._ensure_tag_editor()
    host.page._load_tag_record_into_editor(session.tags.tags["fox"],load_examples=False)
    host.page.tag_notes.setPlainText("Synthetic retained Tag draft")
    objects=(session.tags,session.categories,session.category_session,session.characters)
    record=copy.deepcopy(session.tags.tags["fox"])
    record.seen_count+=1
    result=session.run_native_operation(lambda:save_tag_details(tag_request(session,record)),kind="modifier_policy")
    assert session.reload_required
    host._native_adopted(host.page,result)
    assert not session.reload_required
    assert host.page.tag_notes.toPlainText()=="Synthetic retained Tag draft"
    assert objects==(session.tags,session.categories,session.category_session,session.characters)
    assert workspace.characters is session.characters is host.page.characters
    session.require_editor_ready()


@pytest.mark.parametrize("selected_host",[True],indirect=True)
def test_original_character_service_and_picker_rebuild_retain_selected_admission(selected_host, qt_test_scope):
    from core.character_record_edit import CharacterRecordEditService
    from core.knowledge_editor_context import SelectedCharacterRecordEditService
    host,session,workspace,_=selected_host
    service=CharacterRecordEditService(session.characters,session.tags,session.categories)
    dialog=host._character_editor(service,session.tags,session.categories,
        kind="series",record_id="synthetic-source",parent=host.page)
    assert isinstance(dialog.service,SelectedCharacterRecordEditService)
    assert dialog._save_pool is host.page.pool
    result=dialog.service.update_series("synthetic-source",name="Synthetic renamed source",aliases=(),original_style_tags=())
    assert session.reload_required
    # The original Source picker performs exactly this base-service rebuild.
    dialog.service=CharacterRecordEditService(session.characters,session.tags,session.categories)
    assert isinstance(dialog.service,SelectedCharacterRecordEditService)
    assert dialog.service.editor_session is session and not session.reload_required
    assert session.characters.series["synthetic-source"].name=="Synthetic renamed source"
    assert workspace.session.characters is workspace.characters is session.characters
    receipt=session.root/"data/user/backups/category-workspace/synthetic/recovery.json"
    receipt.parent.mkdir(parents=True,exist_ok=True)
    receipt.write_text('{"status":"pending"}',encoding="utf-8")
    before=session.characters.series_path.read_bytes()
    with pytest.raises(RuntimeError,match="recovery"):
        dialog.service.update_series("synthetic-source",name="Must not persist",aliases=(),original_style_tags=())
    assert session.characters.series_path.read_bytes()==before


def test_missing_character_initialization_requires_explicit_visible_review(selected_host, monkeypatch):
    host,session,workspace,_=selected_host
    prompts=[]
    def decline(*args):
        prompts.append(args[1:3])
        return QMessageBox.No
    monkeypatch.setattr(QMessageBox,"question",decline)
    assert not host._review_character_initialization()
    assert prompts and "Initialize Character Knowledge" in prompts[0][0]
    assert not any(p.exists() for p in (session.characters.series_path,
        session.characters.characters_path,session.characters.outfits_path))
    assert workspace.session.characters is workspace.characters is session.characters
    assert workspace._merge_membership_tags(("fox",),require_selection=False) is False


def test_default_only_selection_has_honest_edit_admission(selected_host):
    from suite_core.encyclopedia.editing_ui import EncyclopediaEditingBridge
    host,session,workspace,_=selected_host
    bridge=EncyclopediaEditingBridge(session,category_workspace=workspace,tag_editor=host,
        selected_tag=lambda:"synthetic-default-only-id")
    command=next(c for c in bridge.commands if c.label=="Edit Selected Tag")
    assert not command.enabled()
    errors=[]
    bridge.unavailable.connect(errors.append)
    assert bridge.edit_tag() is False
    assert errors and "default override" in errors[0]


@pytest.mark.parametrize("selected_host",["references"],indirect=True)
def test_prepared_character_authority_preserves_original_removal_reference_check(selected_host):
    host,session,workspace,_=selected_host
    references=workspace.session._character_tag_references({"fox"})
    assert any("synthetic-character" in value for value in references)
    assert workspace.session.characters is workspace.characters is host.page.characters is session.characters


def test_selected_policy_failure_preserves_draft_without_unadmitted_reload(selected_host, monkeypatch):
    host,session,workspace,_=selected_host
    host.page._ensure_tag_editor()
    host.page._load_tag_record_into_editor(session.tags.tags["fox"],load_examples=False)
    host.page.tag_notes.setPlainText("Synthetic policy failure retains this Tag draft")
    def forbidden():
        pytest.fail("A selected writer hold cannot implicitly reload replacement authority")
    monkeypatch.setattr(host.page,"_reload_modifier_roles",forbidden)
    monkeypatch.setattr(host.page,"_refresh_tag_composition",forbidden)
    host.page._modifier_compatibility_save_failed("Synthetic catalog identity rejection")
    assert session.reload_required and not workspace.isEnabled()
    assert host.page.tag_notes.toPlainText()=="Synthetic policy failure retains this Tag draft"


@pytest.mark.parametrize("selected_host",["policy"],indirect=True)
@pytest.mark.parametrize("hold",["identity","recovery","closed"])
def test_actual_single_tag_policy_route_refuses_selected_hold_without_reload(selected_host,monkeypatch,hold):
    from uuid import uuid4
    from suite_core.catalog_identity_candidate import CatalogIdentity,encode_identity
    host,session,workspace,_=selected_host
    page=host.page;page._ensure_tag_editor()
    page._load_tag_record_into_editor(session.tags.tags["fox"],load_examples=False)
    page._tag_dialog_batch_edit=True
    page.tag_notes.setPlainText("Actual single-policy failure keeps this Tag draft")
    before={p:p.read_bytes() for p in (session.tags.path,session.authorities.relationship_user)}
    if hold=="identity":
        (session.root/"data/user/catalog_identity.json").write_bytes(encode_identity(CatalogIdentity(str(uuid4()),str(uuid4()))))
    elif hold=="closed":session._closed=True
    else:
        receipt=session.root/"data/user/backups/category-workspace/synthetic/recovery.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text('{"status":"pending"}',encoding="utf-8")
    def forbidden(*args,**kwargs):pytest.fail("Held ordinary policy route must not write or reload authority")
    monkeypatch.setattr(page._modifier_policy_service,"apply",forbidden)
    monkeypatch.setattr(page.tags,"load",forbidden)
    monkeypatch.setattr(page,"_reload_modifier_roles",forbidden)
    assert not page._save_reuse_policy(session.tags.tags["fox"],("modifier_color",),"Must not save",operation="manual_policy_edit")
    assert page.tag_notes.toPlainText()=="Actual single-policy failure keeps this Tag draft"
    assert page._tag_save_pending and session.reload_required
    assert all(p.read_bytes()==raw for p,raw in before.items())
    page._tag_dialog_batch_edit=False;page._tag_save_pending=False


@pytest.mark.parametrize("selected_host",["policy"],indirect=True)
@pytest.mark.parametrize("advanced",[False,True])
def test_visible_ordinary_policy_save_then_tag_draft_save_and_reopen(selected_host,qt_test_scope,advanced):
    from PySide6.QtCore import QTimer
    from qt_app.knowledge_page import ModifierValueScopeDialog
    from core.allowed_dimension_policy import VALUE_SCOPE_ONLY
    host,session,workspace,_=selected_host
    page=host.page;phase=["draft"]
    scope_errors=[]
    scope_timer=qt_test_scope.own_timer(QTimer())
    def scope_drive():
        scope=QApplication.activeModalWidget()
        if not isinstance(scope,ModifierValueScopeDialog):return
        try:
            scope.mode.setCurrentIndex(scope.mode.findData(VALUE_SCOPE_ONLY))
            scope.search.setText("blue")
            QTest.mouseClick(scope.check_matching,Qt.LeftButton)
            assert scope.save_button is not None and scope.save_button.isEnabled()
            _editor_capture(scope,"policy-value-scope",[("Save Allowed Values",scope.save_button)])
            QTest.mouseClick(scope.save_button,Qt.LeftButton)
        except Exception as error:
            scope_errors.append(error);scope.reject()
    scope_timer.setInterval(20);scope_timer.timeout.connect(scope_drive);scope_timer.start()
    def drive(dialog):
        if QApplication.activeModalWidget() is not dialog:return
        if phase[0]=="draft":
            assert page.tag_notes.isVisible()
            page.tag_notes.setFocus()
            QTest.keyClick(page.tag_notes,Qt.Key_A,Qt.ControlModifier)
            QTest.keyClicks(page.tag_notes,"Visible policy change retains this pending Tag note")
            phase[0]="policy"
            tabs=page.tag_editor_tabs.tabBar()
            QTest.mouseClick(tabs,Qt.LeftButton,pos=tabs.tabRect(1).center())
            QTest.mouseClick(page.tag_relationship_tasks,Qt.LeftButton,pos=page.tag_relationship_tasks.tabRect(2).center())
        elif phase[0]=="policy":
            editor=page.tag_allowed_modifier_editor
            assert editor.isVisible() and editor.isEnabled()
            _editor_capture(dialog,"policy-before",[("Allowed Details selector",editor),("Review scopes",page.tag_reuse_values)])
            index=editor.input.findData("modifier_color")
            assert index>=0,editor.choice_values()
            editor.input.setCurrentIndex(index)
            QTest.mouseClick(editor.add_button,Qt.LeftButton)
            assert not session.reload_required,page.tag_reuse_feedback.text()
            assert "modifier_color" in session.services["modifier_policy_service"].effective_for_tag("fox").groups
            assert page.tag_notes.toPlainText()=="Visible policy change retains this pending Tag note"
            assert page._tag_save_pending
            _editor_capture(dialog,"policy-saved-draft-retained",[("Policy feedback",page.tag_reuse_feedback)])
            if advanced:
                phase[0]="reset"
                QTest.mouseClick(page.tag_reuse_values,Qt.LeftButton)
                return
            phase[0]="save_tag"
        elif phase[0]=="reset":
            assert not scope_errors
            assert not session.reload_required,page.tag_reuse_feedback.text()
            policy=session.services["modifier_policy_service"].effective_for_tag("fox")
            assert policy.scope_for("modifier_color").mode==VALUE_SCOPE_ONLY
            assert page.tag_notes.toPlainText()=="Visible policy change retains this pending Tag note"
            QTest.mouseClick(page.tag_reuse_inherit,Qt.LeftButton)
            assert not session.reload_required,page.tag_reuse_feedback.text()
            assert "modifier_color" not in session.services["modifier_policy_service"].effective_for_tag("fox").groups
            editor=page.tag_allowed_modifier_editor
            editor.input.setCurrentIndex(editor.input.findData("modifier_color"))
            QTest.mouseClick(editor.add_button,Qt.LeftButton)
            assert not session.reload_required
            phase[0]="save_tag"
        elif phase[0]=="save_tag":
            _editor_capture(dialog,"policy-ready-tag-save",[("Policy feedback",page.tag_reuse_feedback)])
            phase[0]="wait"
            QTest.mouseClick(next(b for b in dialog.findChildren(QPushButton) if b.text()=="Save Tag"),Qt.LeftButton)
        elif not host.busy and not page._tag_save_pending:
            _editor_capture(dialog,"policy-tag-saved",[("Save status",page.tag_save_status)])
            QTest.mouseClick(dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Close),Qt.LeftButton)
    timer,errors=modal_driver(qt_test_scope,_TagDetailsDialog,drive)
    if _EDITOR_VISUAL is not None and hasattr(host,"visual_commands"):
        host.visual_selection["tag"]="fox"
        QTest.mouseClick(host.visual_commands["Edit Selected Tag"],Qt.LeftButton)
    else:host.edit_tag("fox")
    timer.stop();scope_timer.stop()
    assert not errors and not scope_errors
    reopened=EncyclopediaEditingSession.open(session.adapter)
    assert reopened.tags.tags["fox"].notes=="Visible policy change retains this pending Tag note"
    assert "modifier_color" in reopened.services["modifier_policy_service"].effective_for_tag("fox").groups


@pytest.mark.parametrize("selected_host",["merge"],indirect=True)
@pytest.mark.parametrize("hosting",["tag_page","category_only"])
def test_original_visible_merge_retains_selected_session_and_following_category_save(selected_host, qt_test_scope, hosting, monkeypatch):
    import time
    from qt_app.category_workspace import TagIdentityMergeDialog,CategoryCreationDialog
    host,session,workspace,_=selected_host
    if hosting=="category_only":
        from suite_core.encyclopedia.editing_ui import EncyclopediaEditingBridge
        workspace.editor_tag_page=None
        workspace.routine_change_saved.disconnect(host._category_saved)
        bridge=EncyclopediaEditingBridge(session,category_workspace=workspace)
        def forbidden(*args):pytest.fail("Category-only hosting cannot manufacture a Tag page adopter")
        monkeypatch.setattr(host.page,"_adopt_tag_save_authority",forbidden)
    objects=(session.tags,session.categories,session.category_session,session.characters)
    binding=session.tags.rule_pair_admission
    def merge(dialog):
        dialog.survivor.setCurrentIndex(dialog.survivor.findData("fox"))
        for box in dialog._resolution_boxes.values():box.setCurrentIndex(1)
        assert dialog.merge_button.isEnabled()
        _editor_capture(dialog,"merge-review",[("Merge Tags",dialog.merge_button),("Survivor",dialog.survivor)])
        QTest.mouseClick(dialog.merge_button,Qt.LeftButton)
    timer,errors=modal_driver(qt_test_scope,TagIdentityMergeDialog,merge)
    workspace.merge_tag_ids(("fox","synthetic-fox-donor"))
    timer.stop()
    assert not errors
    deadline=time.monotonic()+15
    while workspace.save_in_progress and time.monotonic()<deadline:
        qt_test_scope.settle();QTest.qWait(20);time.sleep(0.001)
    task=workspace._automatic_save_task
    assert not workspace.save_in_progress, {
        "state":getattr(task,"state",None),"error":getattr(task,"error",None),
        "finished":task.is_finished() if task is not None else None,
        "session_busy":session.busy,"reload_required":session.reload_required,
        "pool_active":host.page.pool.activeThreadCount(),"status":workspace.status.text()}
    assert not session.reload_required,workspace.status.text()
    assert session.tags.resolve_tag_id("synthetic-fox-donor")=="fox"
    assert "synthetic-fox-donor" not in session.tags.tags
    assert workspace.session is session.category_session is objects[2]
    assert workspace.sequence.session is session.category_session
    assert workspace.session.editor_adapter is session.adapter
    assert session.tags.rule_pair_admission == binding
    assert objects==(session.tags,session.categories,session.category_session,session.characters)
    _editor_capture(workspace.window(),"merge-saved",[("Workspace status",workspace.status)])
    phase=["create"]
    def create(dialog):
        if phase[0]=="create":
            QTest.keyClicks(dialog.name,"Synthetic category after verified merge")
            phase[0]="wait"
            QTest.mouseClick(dialog.create_button,Qt.LeftButton)
    timer,errors=modal_driver(qt_test_scope,CategoryCreationDialog,create)
    workspace.create_category_dialog()
    timer.stop()
    assert not errors and not session.reload_required
    reopened=EncyclopediaEditingSession.open(session.adapter)
    assert reopened.tags.resolve_tag_id("synthetic-fox-donor")=="fox"
    assert any(c.name=="Synthetic category after verified merge" for c in reopened.categories.categories.values())


@pytest.mark.parametrize("selected_host",["merge"],indirect=True)
@pytest.mark.parametrize("hold",["identity","recovery"])
def test_merge_native_writer_refuses_catalog_change_after_original_review(selected_host,hold):
    from uuid import uuid4
    from suite_core.catalog_identity_candidate import CatalogIdentity,encode_identity
    host,session,workspace,_=selected_host
    service=workspace._merge_service_factory(session.tags,session.categories,session.characters)
    plan=service.preview("fox",("synthetic-fox-donor",))
    resolutions={conflict.key:conflict.choices[0].key for conflict in plan.conflicts}
    before=session.tags.path.read_bytes()
    if hold=="identity":
        (session.root/"data/user/catalog_identity.json").write_bytes(encode_identity(CatalogIdentity(str(uuid4()),str(uuid4()))))
    else:
        receipt=session.root/"data/user/backups/category-workspace/synthetic/recovery.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text('{"status":"pending"}',encoding="utf-8")
    with pytest.raises((RuntimeError,ValueError)):
        service.apply("fox",("synthetic-fox-donor",),resolutions=resolutions)
    assert session.tags.path.read_bytes()==before
    assert not list((session.root/"data/user/backups/tag-identity-merge").glob("*/recovery.json"))


@pytest.mark.parametrize("selected_host",["merge"],indirect=True)
def test_saved_merge_adoption_failure_holds_without_duplicate_success_or_replay(selected_host,qt_test_scope,monkeypatch):
    import time
    from qt_app.category_workspace import TagIdentityMergeDialog
    from suite_core.encyclopedia.editing_ui import EncyclopediaEditingBridge
    host,session,workspace,_=selected_host
    bridge=EncyclopediaEditingBridge(session,category_workspace=workspace,tag_editor=host)
    saved=[]
    bridge.saved.connect(lambda:saved.append(True))
    def fail(*args):raise RuntimeError("Synthetic merge GUI adoption failure")
    monkeypatch.setattr(host.page,"_adopt_tag_save_authority",fail)
    def merge(dialog):
        dialog.survivor.setCurrentIndex(dialog.survivor.findData("fox"))
        for box in dialog._resolution_boxes.values():box.setCurrentIndex(1)
        QTest.mouseClick(dialog.merge_button,Qt.LeftButton)
    timer,errors=modal_driver(qt_test_scope,TagIdentityMergeDialog,merge)
    workspace.merge_tag_ids(("fox","synthetic-fox-donor"))
    timer.stop()
    assert not errors
    deadline=time.monotonic()+15
    while workspace.save_in_progress and time.monotonic()<deadline:
        qt_test_scope.settle();QTest.qWait(20);time.sleep(0.001)
    assert not workspace.save_in_progress
    assert session.reload_required and not workspace.isEnabled() and not saved
    receipts=list((session.root/"data/user/backups/tag-identity-merge").glob("*/recovery.json"))
    assert len(receipts)==1
    workspace.routine_change_saved.emit({"kind":"tag_identity_merge"})
    assert session.reload_required and not saved
    assert len(list((session.root/"data/user/backups/tag-identity-merge").glob("*/recovery.json")))==1


def test_original_visible_rename_preserves_old_name_identity(selected_host, qt_test_scope):
    host, session, workspace, _ = selected_host
    phase = ["edit"]
    def drive(dialog):
        prompt = QApplication.activeModalWidget()
        if isinstance(prompt, QMessageBox):
            QTest.mouseClick(next(b for b in prompt.buttons() if b.text() == "Not Now"), Qt.LeftButton)
            return
        if phase[0] == "edit":
            host.page.tag_name.setFocus()
            QTest.keyClick(host.page.tag_name, Qt.Key_A, Qt.ControlModifier)
            QTest.keyClicks(host.page.tag_name, "synthetic renamed fox")
            phase[0] = "wait"
            QTest.mouseClick(next(b for b in dialog.findChildren(QPushButton) if b.text() == "Save Tag"), Qt.LeftButton)
        elif not host.busy and session.tags.tags["fox"].display == "synthetic renamed fox":
            QTest.mouseClick(dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Close), Qt.LeftButton)
    timer, errors = modal_driver(qt_test_scope, _TagDetailsDialog, drive)
    host.edit_tag("fox")
    timer.stop()
    assert not errors
    reopened = EncyclopediaEditingSession.open(session.adapter)
    assert reopened.tags.tags["fox"].display == "synthetic renamed fox"
    assert reopened.services["tag_alias_service"].resolve("fox").owner_ids == ("fox",)
    assert workspace.session is session.category_session


def test_original_invalid_draft_discard_preserves_saved_record(selected_host, qt_test_scope):
    host, session, workspace, _ = selected_host
    original = session.tags.path.read_bytes()
    phase = ["edit"]
    def drive(dialog):
        buttons = dialog.findChildren(QPushButton)
        if phase[0] == "edit":
            host.page.tag_name.setFocus()
            QTest.keyClick(host.page.tag_name, Qt.Key_A, Qt.ControlModifier)
            QTest.keyClick(host.page.tag_name, Qt.Key_Backspace)
            phase[0] = "discard"
            QTest.mouseClick(next(b for b in buttons if b.text() == "Save Tag"), Qt.LeftButton)
        elif phase[0] == "discard":
            assert "Not saved" in host.page.tag_save_status.text()
            assert not host.close_ready()
            QTest.mouseClick(next(b for b in buttons if b.text() == "Discard Tag Draft"), Qt.LeftButton)
            assert host.page.tag_name.text() == "fox"
            phase[0] = "close"
        else:
            QTest.mouseClick(dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Close), Qt.LeftButton)
    timer, errors = modal_driver(qt_test_scope, _TagDetailsDialog, drive)
    host.edit_tag("fox")
    timer.stop()
    assert not errors
    assert session.tags.path.read_bytes() == original
    assert host.close_ready()


def test_original_save_failure_retains_draft_and_selected_recovery_hold(selected_host, qt_test_scope, monkeypatch):
    host, session, workspace, _ = selected_host
    original = session.tags.path.read_bytes()
    def fail(_request):
        raise OSError("Synthetic save failure")
    monkeypatch.setattr("core.encyclopedia_editing.save_tag_details", fail)
    phase = ["edit"]
    def drive(dialog):
        if phase[0] == "edit":
            host.page.tag_notes.setPlainText("Synthetic unsaved draft")
            phase[0] = "wait"
            QTest.mouseClick(next(b for b in dialog.findChildren(QPushButton) if b.text() == "Save Tag"), Qt.LeftButton)
        elif not host.busy and session.reload_required:
            assert session.reload_required
            assert host.page.tag_notes.toPlainText() == "Synthetic unsaved draft"
            QTest.mouseClick(next(b for b in dialog.findChildren(QPushButton) if b.text() == "Discard Tag Draft"), Qt.LeftButton)
            QTest.mouseClick(dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Close), Qt.LeftButton)
    timer, errors = modal_driver(qt_test_scope, _TagDetailsDialog, drive)
    host.edit_tag("fox")
    timer.stop()
    assert not errors
    assert session.tags.path.read_bytes() == original
    assert session.reload_required


@pytest.mark.parametrize("selected_host",["policy_merge"],indirect=True)
@pytest.mark.parametrize("theme",["light","dark"])
@pytest.mark.parametrize("scenario",["tags","categories","policy","merge"])
def test_offscreen_editor_visual_controls(selected_host,qt_test_scope,monkeypatch,editor_visual,theme,scenario):
    from PySide6.QtWidgets import QVBoxLayout,QHBoxLayout,QLabel
    from qt_app.category_workspace import CategoryCreationDialog,CategoryDetailsDialog
    from suite_core.encyclopedia.editing_ui import EncyclopediaEditingBridge
    host,session,workspace,_=selected_host
    root=workspace.window();root.resize(1920,1080)
    layout=QVBoxLayout(root);layout.addWidget(QLabel("Synthetic Tag Encyclopedia editor component host"))
    commands=QHBoxLayout();layout.addLayout(commands);layout.addWidget(workspace,1)
    selection={"tag":"fox","category":"animals"}
    bridge=EncyclopediaEditingBridge(session,category_workspace=workspace,tag_editor=host,
        selected_tag=lambda:selection["tag"],selected_category=lambda:selection["category"],parent=root)
    buttons={}
    for command in bridge.commands:
        button=QPushButton(command.label,root);button.clicked.connect(command.callback)
        commands.addWidget(button);buttons[command.label]=button
    host.visual_commands=buttons;host.visual_selection=selection
    root.show();qt_test_scope.settle()
    _editor_capture(root,"entry",[(name,button) for name,button in buttons.items()])
    if scenario=="tags":
        test_original_create_edit_save_reopen_retains_borrowed_authority(selected_host,qt_test_scope)
    elif scenario=="policy":
        test_visible_ordinary_policy_save_then_tag_draft_save_and_reopen(selected_host,qt_test_scope,True)
    elif scenario=="merge":
        test_original_visible_merge_retains_selected_session_and_following_category_save(selected_host,qt_test_scope,"tag_page",monkeypatch)
    else:
        phase=["create"]
        def create(dialog):
            if phase[0]=="create":
                _editor_capture(dialog,"category-new",[("Category name",dialog.name),("Create Category",dialog.create_button)])
                QTest.keyClicks(dialog.name,"Synthetic visually reviewed category")
                phase[0]="wait"
                QTest.mouseClick(dialog.create_button,Qt.LeftButton)
        timer,errors=modal_driver(qt_test_scope,CategoryCreationDialog,create)
        QTest.mouseClick(buttons["New Category"],Qt.LeftButton);timer.stop()
        assert not errors
        _editor_capture(root,"category-created",[("Workspace status",workspace.status)])
        phase[0]="edit";saved=[];bridge.saved.connect(lambda:saved.append(True))
        def edit(dialog):
            if phase[0]=="edit":
                _editor_capture(dialog,"category-edit",[("Category description",workspace.category_description)])
                QTest.mouseClick(workspace.category_description,Qt.LeftButton)
                QTest.keyClicks(workspace.category_description," Synthetic description edited through controls.")
                phase[0]="wait"
            elif saved and not workspace.save_in_progress:
                _editor_capture(dialog,"category-edit-saved",[("Category description",workspace.category_description)])
                QTest.mouseClick(dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Close),Qt.LeftButton)
        timer,errors=modal_driver(qt_test_scope,CategoryDetailsDialog,edit)
        QTest.mouseClick(buttons["Edit Selected Category"],Qt.LeftButton);timer.stop()
        assert not errors
        reopened=EncyclopediaEditingSession.open(session.adapter)
        assert "Synthetic description edited through controls." in reopened.categories.get("animals").description
    assert not session.reload_required
    assert host.page.pool.waitForDone(10000)
    _editor_capture(root,"returned-idle",[("Workspace status",workspace.status)])


def test_selected_editor_omits_private_promotion_without_import(selected_host,monkeypatch):
    import importlib
    host,session,workspace,_=selected_host
    original=importlib.import_module
    def guarded(name,*args,**kwargs):
        assert name not in {"qt_app.knowledge_promotion_dialog","core.knowledge_promotion"}
        return original(name,*args,**kwargs)
    monkeypatch.setattr(importlib,"import_module",guarded)
    host.page._populate_knowledge_more_menu()
    assert not any("Future Defaults" in action.text() for action in host.page.knowledge_more_menu.actions())
    monkeypatch.setattr(host.page,"_flush_tag_save",lambda:pytest.fail("Unavailable promotion must not flush drafts"))
    host.page.show_knowledge_promotion()
    assert not session.reload_required


def test_public_only_editor_import_and_original_optional_promotion(tmp_path):
    """Finite public source copy; never read/import private promotion sources."""
    import json,shutil,subprocess,sys
    from pathlib import Path
    from tools.public_source import load_manifest
    root=Path(__file__).resolve().parents[1]
    public=set(load_manifest(root/"tools/public-source-manifest.json")["files"])
    inventory=json.loads((root/"suite_core/encyclopedia/MODULE_MANIFEST.json").read_bytes())
    delivered=tmp_path/"public-editor"
    paths=((set(inventory["runtime_files"])|set(inventory["support_files"]))&public)-{"core/app_state.py"}
    assert {"qt_app/knowledge_page.py","qt_app/tag_details_host.py","core/knowledge_editor_context.py"}<=paths
    assert not {"core/knowledge_promotion.py","qt_app/knowledge_promotion_dialog.py"}&paths
    for name in sorted(paths):
        target=delivered/name;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(root/name,target)
    code='''
import sys,types,importlib,importlib.abc
from pathlib import Path
sys.path.insert(0,sys.argv[1])
class RejectApplicationOwner(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname=='core.app_state':
            raise AssertionError('Shared editor attempted whole-application import')
sys.meta_path.insert(0,RejectApplicationOwner())
assert not (Path(sys.argv[1])/'core/app_state.py').exists()
from qt_app.knowledge_page import KnowledgePage
from qt_app.tag_details_host import TagDetailsEditorHost
from suite_core.encyclopedia.editing_ui import EncyclopediaEditingBridge
page=types.SimpleNamespace(editor_context=types.SimpleNamespace(standalone=False))
assert KnowledgePage._knowledge_promotion_dialog_type(page) is None
assert not any(name in sys.modules for name in ('core.knowledge_promotion','qt_app.knowledge_promotion_dialog'))
name='qt_app.knowledge_promotion_dialog'
plugin=types.ModuleType(name);plugin.KnowledgePromotionDialog=object()
sys.modules[name]=plugin
assert KnowledgePage._knowledge_promotion_dialog_type(page) is plugin.KnowledgePromotionDialog
page.editor_context.standalone=True
assert KnowledgePage._knowledge_promotion_dialog_type(page) is None
del sys.modules[name]
page.editor_context.standalone=False
original=importlib.import_module
def broken(module):
    raise ModuleNotFoundError('synthetic installed plugin dependency failure',name='synthetic_dependency')
importlib.import_module=broken
try:
    KnowledgePage._knowledge_promotion_dialog_type(page)
except ModuleNotFoundError as error:
    assert error.name=='synthetic_dependency'
else:
    raise AssertionError('Installed plugin dependency failure was concealed')
importlib.import_module=original
for name in ('qt_app.knowledge_page','qt_app.tag_details_host','suite_core.encyclopedia.editing_ui'):
    assert Path(sys.modules[name].__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve())
assert not any(name=='torch' or name.startswith('torch.') for name in sys.modules)
assert 'core.app_state' not in sys.modules
'''
    result=subprocess.run([sys.executable,"-I","-B","-c",code,str(delivered)],capture_output=True,timeout=45)
    assert result.returncode==0,result.stderr.decode("utf-8",errors="replace")
