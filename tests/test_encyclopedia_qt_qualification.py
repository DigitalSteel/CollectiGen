"""Source Qt driver evidence only; no frozen/native launch/signature qualification."""
from hashlib import sha256
from pathlib import Path
from time import monotonic,sleep
from types import SimpleNamespace
import ast
import json

import pytest
from PySide6.QtWidgets import QDialog,QVBoxLayout,QPushButton,QMessageBox,QWidget
from PySide6.QtCore import QTimer,Qt
from PySide6.QtTest import QTest
from suite_core.tests.test_encyclopedia import app
from suite_core.tests.test_encyclopedia_downloaded_defaults import content_font
from tests.test_encyclopedia_enrollment import enrollment
from tests.test_encyclopedia_installation import no_external_actions
from tests.test_encyclopedia_entry_control import standalone,stock_spawn,connected,adapter_exchange
from tests.test_standalone_encyclopedia_update import add_schema_package
from core.encyclopedia_enrollment import StandaloneEncyclopediaEnrollment
from suite_core.manager.resource_executor import ResourceExecutorOwner
from suite_core.encyclopedia.root_recovery_ui import create_original_enrollment_dialog
from suite_core.encyclopedia.__main__ import create_window
from tools.encyclopedia_build.qt_qualification_driver import UiStep,QtQualificationDriver,enrollment_driver,reader_driver
from tools.encyclopedia_build import qt_qualification_recipe as recipe
from tools.encyclopedia_build.qt_qualification_driver import root_ordinary_close_retry_driver


def wait_driver(app,driver,timeout=15):
    end=monotonic()+timeout
    while driver.status=='running' and monotonic()<end:
        app.processEvents();sleep(.005)
    assert driver.status!='running',driver.output


@pytest.mark.parametrize('recover',[False,True])
def test_real_source_enrollment_driver_visible_open_or_recovery(app,content_font,enrollment,tmp_path,recover):
    f,context,acquisition,owner=enrollment
    adapter=StandaloneEncyclopediaEnrollment(context,acquisition,owner,channel='beta')
    owner.begin_standalone_custody(adapter.subject)
    executor=ResourceExecutorOwner()
    dialog=create_original_enrollment_dialog(adapter,executor);dialog.show();app.processEvents()
    before=f.database.read_bytes();lease=owner.fd
    driver=enrollment_driver(dialog,tmp_path/'driver',recover=recover,visible_grace=0,screenshots=False,timeout=5)
    try:
        wait_driver(app,driver)
        assert driver.status=='completed-ui-sequence',driver.reason
        assert f.database.read_bytes()==before and not owner._standalone_reader_creation_entered
        assert executor.closed
        if recover:
            assert dialog.panel.installed.selected is None and dialog.released
        else:
            assert dialog.backend.owner is owner and owner.fd==lease
        events=[json.loads(x) for x in (driver.output/'events.jsonl').read_text().splitlines()]
        assert any(x['event']=='action-dispatched' for x in events)
        assert events[-1]['native_health_claimed'] is False
    finally:
        driver.finish('held','source-test-cleanup')
        if not executor.closed:dialog.close()


def test_real_source_reader_driver_confirms_cancels_reconfirms_normal_close(app,content_font,standalone,stock_spawn,monkeypatch,tmp_path):
    f,boot,_=standalone
    add_schema_package(f,'0.1.0-alpha.9')
    root,client,_,_=connected(stock_spawn,monkeypatch);adapter_exchange(root,client)
    window=create_window(settings_path=f.settings,entry_control=client)
    window.resize(1920,1080);window.show();app.processEvents()
    driver=reader_driver(window,tmp_path/'driver',update=True,expected_version='0.1.0-alpha.9',
        visible_grace=0,screenshots=False,timeout=5)
    try:
        wait_driver(app,driver)
        assert driver.status=='completed-ui-sequence',driver.reason
        assert root.close_intent and boot.request is not None
        assert boot.owner.record['standalone_update']['phase']=='waiting_for_close'
        events=[json.loads(x) for x in (driver.output/'events.jsonl').read_text().splitlines()]
        assert len([x for x in events if x['event']=='modal-confirmed'])==2
        assert any(x.get('step')=='cancel-update' and x['event']=='observed' for x in events)
        # Fake native actor remains alive: driver did not manufacture health/drain/apply.
        assert stock_spawn[1]['code'] is None and stock_spawn[1]['members']==1
    finally:
        driver.finish('held','source-test-cleanup');window.close();app.processEvents()


def test_disabled_control_watchdog_retains_ui_without_dispatch(app,tmp_path):
    dialog=QDialog();layout=QVBoxLayout(dialog);button=QPushButton('Blocked',dialog)
    layout.addWidget(button);button.setEnabled(False);calls=[];button.clicked.connect(lambda:calls.append(True))
    dialog.show();app.processEvents()
    driver=QtQualificationDriver(dialog,[UiStep('blocked','click','Blocked',observed=lambda:bool(calls))],
        tmp_path/'driver',scenario='disabled',timeout=.1,visible_grace=0,screenshots=False)
    wait_driver(app,driver)
    assert driver.status=='held' and driver.reason=='step_timeout:blocked'
    assert dialog.isVisible() and calls==[]
    events=[json.loads(x) for x in (driver.output/'events.jsonl').read_text().splitlines()]
    assert not any(x['event']=='action-dispatched' for x in events)
    dialog.close()


@pytest.mark.parametrize('confirmation',['disabled','missing'])
def test_nested_modal_deadline_holds_without_late_confirmation_or_owner_close(app,tmp_path,confirmation):
    window=QDialog();layout=QVBoxLayout(window)
    button=QPushButton('Open confirmation',window);layout.addWidget(button)
    observed=[];yes_clicks=[];modals=[]
    def open_modal():
        modal=QMessageBox(QMessageBox.Question,'Bounded confirmation','Synthetic qualification only',
            QMessageBox.Yes|QMessageBox.No if confirmation=='disabled' else QMessageBox.No,window)
        modals.append(modal)
        yes=modal.button(QMessageBox.Yes)
        if yes is not None:
            yes.setEnabled(False);yes.clicked.connect(lambda:yes_clicks.append(True))
            # A late available button must not revive a held driver.
            QTimer.singleShot(200,lambda:yes.setEnabled(True))
        def observe_then_test_cleanup():
            observed.append(dict(status=driver.status,reason=driver.reason,
                nested=driver.in_tick,modal_visible=modal.isVisible(),owner_visible=window.isVisible()))
            # Test-only disposal AFTER observing retention. Driver never closes it.
            QTest.mouseClick(modal.button(QMessageBox.No),Qt.LeftButton,delay=0)
        QTimer.singleShot(280,observe_then_test_cleanup)
        modal.exec()  # Real nested loop inside the driver's synchronous QTest click.
    button.clicked.connect(open_modal)
    window.show();app.processEvents()
    driver=QtQualificationDriver(window,[UiStep('modal','click','Open confirmation',
        observed=lambda:False,modal_title='Bounded confirmation'),UiStep('close-owner','normal-close')],
        tmp_path/'driver',scenario='nested-'+confirmation,timeout=.1,visible_grace=0,screenshots=False)
    end=monotonic()+2
    while not observed and monotonic()<end:app.processEvents();sleep(.005)
    assert observed==[dict(status='held',reason='step_timeout:modal',nested=True,
        modal_visible=True,owner_visible=True)]
    assert driver.status=='held' and window.isVisible() and yes_clicks==[]
    events=[json.loads(x) for x in (driver.output/'events.jsonl').read_text().splitlines()]
    assert not any(x['event']=='modal-confirmed' or x.get('step')=='close-owner' for x in events)
    assert events[-1]['status']=='held' and modals[0].result()==QMessageBox.No
    window.close()


def test_source_reader_recovery_driver_uses_authenticated_status_and_close(app,content_font,standalone,stock_spawn,monkeypatch,tmp_path):
    from tests.test_encyclopedia_entry_control import activate_over_control
    boot,request,(root,client,_,_)=activate_over_control(standalone,stock_spawn,monkeypatch)
    adapter_exchange(root,client)
    window=create_window(settings_path=standalone[0].settings,entry_control=client)
    window.resize(1920,1080);window.show();app.processEvents()
    driver=reader_driver(window,tmp_path/'driver',recover=True,visible_grace=0,screenshots=False,timeout=5)
    try:
        wait_driver(app,driver)
        assert driver.status=='completed-ui-sequence',driver.reason
        assert root.after_close_operation=='recover' and root.close_intent
        assert boot.request is not None and boot.owner.record['standalone_update']['phase']=='applying'
        assert stock_spawn[1]['code'] is None and stock_spawn[1]['members']==1
    finally:
        driver.finish('held','source-test-cleanup');window.close();app.processEvents()


def test_native_mode_rejects_offscreen_before_actuation(app,tmp_path):
    from PySide6.QtGui import QGuiApplication
    if QGuiApplication.platformName() not in ('offscreen','minimal'):
        pytest.skip('This bounded test checks the actual offscreen source platform')
    dialog=QDialog();dialog.show();app.processEvents()
    driver=QtQualificationDriver(dialog,[UiStep('close','normal-close')],tmp_path/'driver',
        scenario='native-refusal',require_native_ui=True,screenshots=False)
    assert driver.status=='held' and driver.reason=='native_visible_platform_required'
    assert dialog.isVisible()
    dialog.close()


def test_reader_mode_reuses_original_readonly_admission_and_rejects_change(monkeypatch):
    from tools.encyclopedia_build.qt_qualification_driver import reader_update_mode
    from suite_core.encyclopedia import entry_binding
    admitted=SimpleNamespace(selected=SimpleNamespace(version='0.2.0-alpha.907'))
    control=SimpleNamespace(admitted=admitted)
    profile=SimpleNamespace(trust_bytes=b'public fixture',trust_sha256='a'*64)
    state={'phase':'activated','changed':False};calls=[]
    def readonly(argv,**trust):
        calls.append((argv,trust))
        return (SimpleNamespace(selected=None) if state['changed'] else admitted,
            SimpleNamespace(initial=SimpleNamespace(phase=state['phase'])))
    monkeypatch.setattr(entry_binding,'child_binding',readonly)
    assert not reader_update_mode(control,profile,('locator',),a_version='0.2.0-alpha.907')
    state['phase']='healthy'
    assert reader_update_mode(control,profile,('locator',),a_version='0.2.0-alpha.907')
    state['phase']='rolled_back'
    assert not reader_update_mode(control,profile,('locator',),a_version='0.2.0-alpha.907')
    state['changed']=True
    with pytest.raises(ValueError):reader_update_mode(control,profile,('locator',),a_version='0.2.0-alpha.907')
    assert calls[0][1]==dict(trust_bytes=b'public fixture',trust_sha256='a'*64)


def test_primary_once_visible_callback_runs_driver_to_original_source_settlement(app,content_font,enrollment,tmp_path):
    from suite_core.encyclopedia.root_recovery_ui import present_original_enrollment
    _,context,acquisition,owner=enrollment
    adapter=StandaloneEncyclopediaEnrollment(context,acquisition,owner,channel='beta')
    owner.begin_standalone_custody(adapter.subject)
    executor=ResourceExecutorOwner();drivers=[]
    def ready(dialog):
        assert dialog.isVisible()
        drivers.append(enrollment_driver(dialog,tmp_path/'driver',recover=True,
            visible_grace=0,screenshots=False,timeout=5))
    assert present_original_enrollment(adapter,executor,dialog_ready=ready) is None
    assert len(drivers)==1 and drivers[0].status=='completed-ui-sequence'
    assert executor.closed and owner.fd is None


def test_wrapper_keeps_custody_ahead_of_driver_and_admits_only_closed_roles():
    source=Path('tools/encyclopedia_build/qt_qualification_driver.py').read_bytes()
    for role in ('root','reader'):
        raw=recipe.render_qt_entry(role,source,a_version='0.2.0-alpha.907',b_version='0.2.0-alpha.908')
        tree=ast.parse(raw)
        main=next(n for n in tree.body if isinstance(n,ast.If) and isinstance(n.test,ast.Compare)
            and isinstance(n.test.left,ast.Name) and n.test.left.id=='__name__')
        assert isinstance(main.body[0],ast.If)
        assert isinstance(main.body[0].body[0],ast.Raise)
        assert 'encyclopedia-custody' in ast.unparse(main.body[0].test)
        compile(raw,'qualification-entry','exec',dont_inherit=True)
    with pytest.raises(ValueError):recipe.render_qt_entry('foreign',source,a_version='0.1.0',b_version='0.2.0')


def test_recipe_declares_qttest_without_mutating_ordinary_namespace(tmp_path,monkeypatch):
    observed={};original=lambda *a,**kw:observed.update(kw) or 'analysis'
    def build(role,namespace):
        assert role=='root'
        assert namespace['Analysis']([],hiddenimports=['PySide6.QtCore'],excludes=['tests'])=='analysis'
        return 'collection'
    monkeypatch.setattr(recipe,'_load_isolated_stock',lambda *a:SimpleNamespace(build_recipe=build))
    qualification=dict(purpose=recipe.PURPOSE,driver_sha256='a'*64,
        recipe_sha256=sha256(Path(recipe.__file__).read_bytes()).hexdigest(),
        base_recipe_sha256='b'*64,scenario='baseline',a_version='0.1.0',b_version='0.2.0')
    (tmp_path/'STAGE.json').write_text(json.dumps(dict(qualification_qt=qualification,
        build_rows=[dict(path='recipe_support.py',sha256='b'*64)])))
    namespace=dict(SPECPATH=str(tmp_path),Analysis=original)
    assert recipe.build_qt_recipe('root',namespace)=='collection'
    assert 'PySide6.QtTest' in observed['hiddenimports'] and namespace['Analysis'] is original


def test_isolated_recipe_retains_every_other_exclusion(tmp_path):
    raw=b"FORBIDDEN={'tests','core.app_state','qt_app.main_window','PySide6.QtTest'}\n"
    (tmp_path/'recipe_support.py').write_bytes(raw)
    config=dict(build_rows=[dict(path='recipe_support.py',sha256=sha256(raw).hexdigest())])
    isolated=recipe._load_isolated_stock(tmp_path,config)
    assert isolated.FORBIDDEN=={'tests','core.app_state','qt_app.main_window'}
    from tools.encyclopedia_build import recipe_support as ordinary
    assert 'PySide6.QtTest' in ordinary.FORBIDDEN


def test_derived_stage_is_exclusive_pinned_and_preserves_baseline(tmp_path):
    from tools import build_manager_distribution as stock
    from tools.prepare_encyclopedia_qualification_stage import verify_prepared_stage
    from tools.encyclopedia_build.profile_renderer import render_entry
    baseline=tmp_path/'baseline';output=tmp_path/'qualified'
    # Synthetic STAGE-only shape: no build, signature, native or source closure claim.
    source=b'def present_original_enrollment(a,b,*,dialog_ready=None): pass\ndef present_original_root_hold(a,*,dialog_ready=None): pass\n'
    def row(name,raw):return dict(path=name,bytes=len(raw),sha256=sha256(raw).hexdigest())
    source_name='suite_core/encyclopedia/root_recovery_ui.py'
    stock.write_new(baseline/'source'/source_name,source)
    generated=[]
    for role in ('root','reader'):
        name='encyclopedia_'+role+'_entry.py';raw=render_entry(role,bound=True)
        stock.write_new(baseline/'source'/name,raw);generated.append(row(name,raw))
    profile=b'INPUT={}\n';stock.write_new(baseline/'source/encyclopedia_fixture_profile.py',profile)
    generated.append(row('encyclopedia_fixture_profile.py',profile))
    base_recipe=Path('tools/encyclopedia_build/recipe_support.py').read_bytes()
    stock.write_new(baseline/'recipe_support.py',base_recipe)
    tools=b'{}\n';stock.write_new(baseline/'TOOLCHAIN.json',tools)
    config=dict(purpose='ENCYCLOPEDIA_QUALIFICATION_BUILD_ONLY',status='SOURCE_STAGE_ONLY_NOT_FROZEN',
        profile_bound=True,include_python_sources=False,source_rows=[row(source_name,source)],
        generated_rows=generated,build_rows=[row('recipe_support.py',base_recipe)],toolchain_sha256=sha256(tools).hexdigest())
    raw=stock.json_bytes(config);stock.write_new(baseline/'STAGE.json',raw)
    pin=sha256(raw).hexdigest()
    result=recipe.derive_qt_stage(baseline,pin,output,a_version='0.2.0-alpha.907',b_version='0.2.0-alpha.908')
    checked=verify_prepared_stage(output,result['stage_sha256'])
    assert checked['qualification_qt']['purpose']==recipe.PURPOSE
    assert (baseline/'STAGE.json').read_bytes()==raw
    assert (output/'recipe_support.py').read_bytes()==base_recipe
    assert result['built'] is False and result['native_launched'] is False and result['signed'] is False
    with pytest.raises(ValueError):
        recipe.derive_qt_stage(baseline,pin,output,a_version='0.2.0-alpha.907',b_version='0.2.0-alpha.908')


def ordinary_retry_widgets(app,*,refusals=0,disabled=None):
    """Real visible Qt controls with a source-only Close guard, no backend proof."""
    from suite_core.manager.encyclopedia_acquisition_ui import EncyclopediaAcquisitionPanel
    class Panel(EncyclopediaAcquisitionPanel):
        def __init__(self,parent):QWidget.__init__(self,parent)
        @property
        def close_held(self):return False
    class Dialog(QDialog):
        def closeEvent(self,event):
            calls.append('close')
            if calls.count('close')<=refusals:
                close.setEnabled(False);event.ignore()
            else:event.accept()
    calls=[];dialog=Dialog();layout=QVBoxLayout(dialog);panel=Panel(dialog);layout.addWidget(panel)
    status=QPushButton('Check app status',panel);close=QPushButton('Close Tag Encyclopedia',dialog)
    panel_layout=QVBoxLayout(panel);panel_layout.addWidget(status);layout.addWidget(close)
    def checked():
        calls.append('check');close.setEnabled(disabled!='close')
    status.clicked.connect(checked);close.clicked.connect(dialog.close)
    if disabled=='status':status.setEnabled(False)
    if disabled=='close':close.setEnabled(False)
    dialog.show();app.processEvents()
    return dialog,status,close,calls


@pytest.mark.parametrize('refusals',[0,1])
def test_root_ordinary_retry_visible_controls_complete_first_or_second_close(app,tmp_path,refusals):
    dialog,status,close,calls=ordinary_retry_widgets(app,refusals=refusals)
    driver=root_ordinary_close_retry_driver(dialog,tmp_path/'driver',visible_grace=0,screenshots=False)
    wait_driver(app,driver,timeout=2)
    assert driver.status=='completed-ui-sequence' and not dialog.isVisible()
    assert calls==(['check','close'] if refusals==0 else ['check','close','check','close'])
    events=[json.loads(line) for line in (driver.output/'events.jsonl').read_text().splitlines()]
    assert events[-1]['native_health_claimed'] is False
    assert all(e.get('step','') in ('check-status-1','close-root-1','check-status-2','close-root-2')
        for e in events if e['event']=='intent')


@pytest.mark.parametrize('disabled',['status','close'])
def test_root_ordinary_retry_disabled_control_deadline_never_dispatches_late(app,tmp_path,monkeypatch,disabled):
    from tools.encyclopedia_build import qt_qualification_driver as module
    clock=[0.0];monkeypatch.setattr(module,'monotonic',lambda:clock[0])
    dialog,status,close,calls=ordinary_retry_widgets(app,disabled=disabled)
    driver=root_ordinary_close_retry_driver(dialog,tmp_path/'driver',visible_grace=0,screenshots=False)
    for _ in range(5):driver.tick();app.processEvents()
    clock[0]=21;driver.tick()
    assert driver.status=='held' and dialog.isVisible() and 'close' not in calls
    before=list(calls);status.setEnabled(True);close.setEnabled(True)
    driver.tick();driver.answer_modal();app.processEvents()
    assert calls==before and driver.reason=='root_ordinary_close_retry_deadline'
    driver.finish('held','cleanup');dialog.hide()


def test_root_ordinary_retry_two_refusals_hold_without_third_close(app,tmp_path,monkeypatch):
    from tools.encyclopedia_build import qt_qualification_driver as module
    clock=[0.0];monkeypatch.setattr(module,'monotonic',lambda:clock[0])
    dialog,status,close,calls=ordinary_retry_widgets(app,refusals=5)
    driver=root_ordinary_close_retry_driver(dialog,tmp_path/'driver',visible_grace=0,screenshots=False)
    for _ in range(12):driver.tick();app.processEvents()
    assert calls==['check','close','check','close'] and dialog.isVisible()
    clock[0]=21;driver.tick();close.setEnabled(True);driver.tick();app.processEvents()
    assert driver.status=='held' and calls.count('close')==2
    dialog.hide()


def test_root_ordinary_retry_nested_modal_holds_original_widgets(app,tmp_path):
    dialog,status,close,calls=ordinary_retry_widgets(app)
    status.clicked.disconnect();observed=[];modals=[]
    def modal_check():
        modal=QMessageBox(QMessageBox.Warning,'Unexpected root modal','Synthetic source UI only',QMessageBox.Ok,dialog)
        modals.append(modal)
        def observe_and_test_dispose():
            observed.append((driver.status,driver.reason,modal.isVisible(),dialog.isVisible(),list(calls)))
            QTest.mouseClick(modal.button(QMessageBox.Ok),Qt.LeftButton)
        QTimer.singleShot(120,observe_and_test_dispose)
        modal.exec()
    status.clicked.connect(modal_check)
    driver=root_ordinary_close_retry_driver(dialog,tmp_path/'driver',visible_grace=0,screenshots=False)
    wait_driver(app,driver,timeout=2);app.processEvents()
    assert observed==[('held','unexpected_owned_modal',True,True,[])]
    assert dialog.isVisible() and calls==[]
    dialog.hide()


def test_root_ordinary_retry_cannot_complete_or_close_again_after_overall_deadline(app,tmp_path,monkeypatch):
    from tools.encyclopedia_build import qt_qualification_driver as module
    clock=[0.0];monkeypatch.setattr(module,'monotonic',lambda:clock[0])
    dialog,status,close,calls=ordinary_retry_widgets(app)
    def late_completion():clock[0]=21
    close.clicked.disconnect();close.clicked.connect(late_completion);close.clicked.connect(dialog.close)
    driver=root_ordinary_close_retry_driver(dialog,tmp_path/'driver',visible_grace=0,screenshots=False)
    for _ in range(5):driver.tick();app.processEvents()
    assert driver.status=='held' and driver.reason=='root_ordinary_close_retry_deadline'
    assert calls==['check','close']
    driver.tick();assert calls.count('close')==1


def test_root_retry_wrapper_selects_only_root_role_and_preserves_custody_first():
    source=Path('tools/encyclopedia_build/qt_qualification_driver.py').read_bytes()
    for role in ('root','reader'):
        raw=recipe.render_qt_entry(role,source,a_version='0.2.0-alpha.907',b_version='0.2.0-alpha.908',
            scenario='baseline-ordinary-close-retry');tree=ast.parse(raw)
        main=next(n for n in tree.body if isinstance(n,ast.If) and isinstance(n.test,ast.Compare)
            and isinstance(n.test.left,ast.Name) and n.test.left.id=='__name__')
        assert isinstance(main.body[0],ast.If) and isinstance(main.body[0].body[0],ast.Raise)
        recovery=next(n for n in main.body if isinstance(n,ast.FunctionDef) and n.name=='_recovery_ready')
        condition=recovery.body[0].orelse[0].test
        assert ast.literal_eval(condition.values[0].left)==role
        compile(raw,'root-only-qualification','exec',dont_inherit=True)


def test_actual_original_recovery_close_driver_finishes_acknowledged_exit(app,content_font,standalone,stock_spawn,monkeypatch,tmp_path):
    from tests.test_encyclopedia_entry_control import exchange
    from core.manager_launch_owner import LaunchHold
    from suite_core.encyclopedia.root_holds import OriginalRootHold
    from suite_core.encyclopedia.root_recovery_ui import present_original_root_hold
    boot,state,api=stock_spawn;root,client,_,_=connected(stock_spawn,monkeypatch)
    exchange(root,client,'close');state.update(code=0,members=0);root.after_reader_close()
    session=boot.owner._standalone_root_custody.custody_session;read=session.wire.channel.read_frame
    def acknowledged_before_exit(**kwargs):raw=read(**kwargs);state['custody_code']=None;return raw
    monkeypatch.setattr(session.wire.channel,'read_frame',acknowledged_before_exit)
    with pytest.raises(LaunchHold) as caught:boot.close()
    assert session.drain_sent and session.drain_acknowledged and not session.resources_closed
    frames=len(state['custody_frames']);state['custody_code']=0
    hold=OriginalRootHold(boot,root,ResourceExecutorOwner(),caught.value);hold.close_acknowledged=True
    drivers=[]
    def ready(dialog):drivers.append(root_ordinary_close_retry_driver(dialog,tmp_path/'driver',
        visible_grace=0,screenshots=False))
    present_original_root_hold(hold,dialog_ready=ready)
    assert len(drivers)==1 and drivers[0].status=='completed-ui-sequence'
    assert hold.released.is_set() and session.resources_closed and boot.owner.fd is None
    assert len(state['custody_frames'])==frames  # ACK/exit retry; no drain replay.
