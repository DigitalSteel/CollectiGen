"""Visible Home restore preview on a generated saved native connection only."""
from pathlib import Path
from time import monotonic, sleep
import json
import os
import pytest
from tests.test_completed_pack_host import connected, contained_temp, selected, reviewed_local_input, data
from suite_core.tests.test_encyclopedia import app
from suite_core.tests.test_encyclopedia_catalog_ui import click, catalog_readable_font

ACTIVE_QT_SCOPE=None

@pytest.fixture(autouse=True)
def owned_c3_widget_scope(request):
    global ACTIVE_QT_SCOPE
    if "catalog_readable_font" in request.fixturenames:
        ACTIVE_QT_SCOPE=request.getfixturevalue("qt_test_scope")
    yield
    ACTIVE_QT_SCOPE=None


def show_window(window,size):
    assert ACTIVE_QT_SCOPE is not None
    return ACTIVE_QT_SCOPE.show(window,size=size)


RUN=Path(os.environ.get('COLLECTIGEN_C3_EVIDENCE',str(Path(__file__).resolve().parents[2]/'.tmp/suite06c3-20261026-0923')))


def settle(app,host):
    end=monotonic()+150
    while host.busy and monotonic()<end:
        sleep(.005);app.processEvents()
    app.processEvents()
    assert not host.busy,host.controller.reason


def push(app,dialog,button):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    if dialog.scroll.widget().isAncestorOf(button):dialog.scroll.ensureWidgetVisible(button)
    app.processEvents();assert button.isVisible() and button.isEnabled(),button.text()
    assert dialog.rect().contains(button.mapTo(dialog,button.rect().center())),button.text()
    QTest.mouseClick(button,Qt.LeftButton);app.processEvents()


def capture(dialog,name):
    folder=RUN/'captures';folder.mkdir(parents=True,exist_ok=True)
    target=folder/(name+'.png');attempt=1
    while target.exists():
        attempt+=1;target=folder/(name+f'-attempt{attempt}.png')
    assert dialog.grab().save(str(target))


@pytest.mark.parametrize('size',[(1000,540),(1600,930),(1920,1080)])
@pytest.mark.parametrize('theme',['light','dark'])
def test_visible_home_compare_restore_reopen(app,connected,monkeypatch,size,theme,catalog_readable_font):
    from suite_core.encyclopedia.__main__ import create_window
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QToolButton,QCheckBox
    from qt_app.theme import apply_theme
    apply_theme(app,theme,False)
    c,root,receipt,before,settings=connected
    window=create_window(settings_path=settings);show_window(window,size);app.processEvents()
    host=window.catalog_host
    calls=[]
    def forbidden(*a,**k):calls.append(True);raise AssertionError('History must not configure optional downloads')
    host.optional_content_factory=forbidden
    click(window,'Review completed user import…')
    dialog=host.restore_dialog;assert dialog is not None
    dialog.resize(*size);app.processEvents()
    capture(dialog,f'{theme}-{size[0]}-start')
    assert not dialog.restore.isEnabled() and not calls
    push(app,dialog,dialog.open_saved);capture(dialog,f'{theme}-{size[0]}-opening');settle(app,host)
    assert host.controller.choice is not None and not host.controller.error,host.controller.error
    push(app,dialog,dialog.refresh);capture(dialog,f'{theme}-{size[0]}-listing');settle(app,host)
    assert dialog.imports.count()==2 and dialog.imports.currentData() is None
    QTest.mouseClick(dialog.imports,Qt.LeftButton);QTest.keyClick(dialog.imports,Qt.Key_End);QTest.keyClick(dialog.imports,Qt.Key_Return);app.processEvents()
    assert dialog.imports.currentData() is not None
    push(app,dialog,dialog.review_button)
    assert dialog.progress.isVisible();capture(dialog,f'{theme}-{size[0]}-comparing')
    settle(app,host)
    assert not host.controller.error,host.controller.error
    assert dialog.areas.topLevelItemCount()>0 and not dialog.confirm.isChecked()
    capture(dialog,f'{theme}-{size[0]}-comparison')
    # Expert comparison uses the same reviewed transaction, no alternate authority.
    toggle=next(b for b in dialog.findChildren(QToolButton) if b.text()=='Compare exact saved values')
    push(app,dialog,toggle)
    assert 'host-added' in dialog.current.toPlainText() or dialog.current.toPlainText()!=dialog.previous.toPlainText()
    capture(dialog,f'{theme}-{size[0]}-details')
    assert not dialog.restore.isEnabled()
    # Click the actual checkbox indicator; center text is not the check target.
    QTest.mouseClick(dialog.confirm,Qt.LeftButton,pos=dialog.confirm.rect().topLeft()+__import__('PySide6.QtCore',fromlist=['QPoint']).QPoint(8,dialog.confirm.height()//2));app.processEvents()
    assert dialog.confirm.isChecked() and dialog.restore.isEnabled()
    push(app,dialog,dialog.restore)
    assert dialog.progress.isVisible();capture(dialog,f'{theme}-{size[0]}-restoring')
    settle(app,host)
    assert not host.controller.error,host.controller.error
    assert host.controller.result.native_saved and data(root)==before
    assert window.publication==host.controller.result.publication
    assert 'Restoration saved' in dialog.status.text() and not calls
    capture(dialog,f'{theme}-{size[0]}-saved')
    inverse=host.controller.result.receipt
    assert json.loads(inverse.read_bytes())['status']=='restored' and receipt.exists()
    dialog.close();window.close();app.processEvents();settle(app,host)
    # Saved startup uses the real native reader entry, not an injected publication.
    from PySide6.QtWidgets import QFileDialog
    monkeypatch.setattr(QFileDialog,'getExistingDirectory',lambda *a:str(root))
    reopened=create_window(settings_path=settings);show_window(reopened,size);app.processEvents()
    click(reopened,'Open Tag Database');settle(app,reopened.catalog_host)
    assert reopened.publication is not None and data(root)==before
    capture(reopened,f'{theme}-{size[0]}-reopened')
    reopened.close();app.processEvents();settle(app,reopened.catalog_host)


def test_native_comparison_is_read_only_and_complete(connected):
    import core.knowledge_pack as native
    c,root,receipt,_before,_settings=connected
    from tests.test_completed_pack_host import evidence
    frozen=evidence(root)
    listing=native.list_completed_knowledge_pack_imports(root,catalog_identity_binding=c.choice.native_adapter.identity_binding)
    assert len(listing)==1 and listing[0].receipt_relative==receipt.relative_to(root).as_posix()
    review=c.choice.native_adapter.review_completed_import(receipt)
    parts=native.compare_completed_knowledge_pack_import(root,review)
    assert parts and all(p.before==p.proposed for p in parts)
    assert any(p.current!=p.proposed for p in parts)
    assert evidence(root)==frozen



def test_read_only_presentation_never_creates_an_absent_catalog(connected,tmp_path):
    import core.knowledge_pack as native
    c,root,receipt,_before,_settings=connected
    absent=tmp_path/'absent-catalog'
    review=c.choice.native_adapter.review_completed_import(receipt)
    with pytest.raises(RuntimeError):native.list_completed_knowledge_pack_imports(absent,catalog_identity_binding=review.catalog_identity_binding)
    with pytest.raises(RuntimeError):native.compare_completed_knowledge_pack_import(absent,review)
    assert not absent.exists()


def test_comparison_refuses_later_correction_without_writes(connected):
    import core.knowledge_pack as native
    from tests.test_completed_pack_host import evidence
    c,root,receipt,_before,_settings=connected
    review=c.choice.native_adapter.review_completed_import(receipt)
    path=root/'data/user/tags.json';v=json.loads(path.read_bytes());v['later']='retain';path.write_text(json.dumps(v),encoding='utf-8')
    frozen=evidence(root)
    with pytest.raises(RuntimeError):native.compare_completed_knowledge_pack_import(root,review)
    assert evidence(root)==frozen



def start_preview(app,connected):
    from suite_core.encyclopedia.__main__ import create_window
    from qt_app.theme import apply_theme
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    apply_theme(app,'light',False)
    window=create_window(settings_path=connected[4]);show_window(window,(1000,740));app.processEvents()
    click(window,'Review completed user import…');host=window.catalog_host;dialog=host.restore_dialog
    push(app,dialog,dialog.open_saved);settle(app,host)
    push(app,dialog,dialog.refresh);settle(app,host)
    QTest.mouseClick(dialog.imports,Qt.LeftButton);QTest.keyClick(dialog.imports,Qt.Key_End);QTest.keyClick(dialog.imports,Qt.Key_Return);app.processEvents()
    push(app,dialog,dialog.review_button);settle(app,host)
    assert host.controller.completed_review is not None,host.controller.error
    return window,host,dialog


def confirm_review(app,dialog):
    from PySide6.QtCore import Qt,QPoint
    from PySide6.QtTest import QTest
    QTest.mouseClick(dialog.confirm,Qt.LeftButton,pos=QPoint(8,dialog.confirm.height()//2));app.processEvents()
    assert dialog.confirm.isChecked() and dialog.restore.isEnabled()


def test_visible_interrupted_restore_reopens_and_recovers(app,connected,monkeypatch,catalog_readable_font):
    import core.knowledge_pack as native
    from suite_core.encyclopedia.__main__ import create_window
    c,root,receipt,before,settings=connected
    window,host,dialog=start_preview(app,connected)
    value=json.loads(receipt.read_bytes());targets={Path(row['target']) for row in value['files'] if row['expected_before']!=row['expected_after']}
    real=native._atomic_bytes;fired=[]
    def interrupted(path,payload):
        real(path,payload)
        if Path(path) in targets and not fired:fired.append(True);raise OSError('visible inverse interrupted')
    confirm_review(app,dialog)
    with monkeypatch.context() as patch:
        patch.setattr(native,'_atomic_bytes',interrupted)
        push(app,dialog,dialog.restore);settle(app,host)
    assert fired and host.controller.result is not None and not host.controller.result.native_saved
    inverse=host.controller.result.receipt
    assert dialog.recover.isVisible() and dialog.recover.isEnabled()
    assert 'interrupted restoration' in dialog.status.text()
    capture(dialog,'recovery-pending')
    dialog.close();window.close();app.processEvents();settle(app,host)
    reopened=create_window(settings_path=settings);show_window(reopened,(1000,740));app.processEvents()
    click(reopened,'Review completed user import…');h=reopened.catalog_host;d=h.restore_dialog
    push(app,d,d.open_saved);settle(app,h)
    assert h.controller.choice.completed_inverse_receipts==(inverse,)
    assert not h.controller.choice.recovery_receipts
    push(app,d,d.recover);capture(d,"recovering");settle(app,h)
    assert h.controller.result.native_saved and not h.controller.error,h.controller.error
    assert data(root)==before and 'Reader refreshed' in d.status.text()
    assert len(list((root/'backups/knowledge-pack-import').glob('completed-pack-undo-*/recovery.json')))==1
    capture(d,'recovery-saved')
    d.close();reopened.close();app.processEvents();settle(app,h)


@pytest.mark.parametrize('mode',['refused','exception'])
def test_visible_reader_delivery_retry_never_replays_restore(app,connected,monkeypatch,catalog_readable_font,mode):
    window,host,d=start_preview(app,connected);root=connected[1]
    real=window.open_publication
    def fail(*a,**k):
        if mode=='exception':raise ValueError('injected reader delivery exception')
        return False
    confirm_review(app,d)
    with monkeypatch.context() as patch:
        patch.setattr(window,'open_publication',fail)
        push(app,d,d.restore);settle(app,host)
    assert host.controller.result.native_saved and not host.reader_delivery_ok
    assert 'Reader refreshed' not in d.status.text() and d.reload.isVisible()
    inverse=host.controller.result.receipt
    capture(d,'reader-delivery-'+mode)
    push(app,d,d.reload);capture(d,"reader-retrying-"+mode);settle(app,host)
    assert host.reader_delivery_ok and 'Reader refreshed' in d.status.text(),d.status.text()
    assert list((root/'backups/knowledge-pack-import').glob('completed-pack-undo-*/recovery.json'))==[inverse]
    d.close();window.close();app.processEvents();settle(app,host)


@pytest.mark.parametrize('phase',['before','after','parent-close'])
def test_visible_cancel_and_close_keep_original_worker(app,connected,monkeypatch,catalog_readable_font,phase):
    import core.knowledge_pack as native
    from threading import Event,Thread
    from PySide6.QtCore import QTimer
    window,host,d=start_preview(app,connected);root=connected[1]
    entered=Event();release=Event();ticks=[];real=native.undo_completed_knowledge_pack_import
    original_factory=host.executor._thread_factory
    if phase=='before':
        def factory(**options):
            target=options.pop('target')
            def wait_then_run():entered.set();assert release.wait(10);target()
            return Thread(target=wait_then_run,**options)
        monkeypatch.setattr(host.executor,'_thread_factory',factory)
    else:
        def delayed(*a,**k):entered.set();assert release.wait(10);return real(*a,**k)
        monkeypatch.setattr(native,'undo_completed_knowledge_pack_import',delayed)
    frozen=data(root);confirm_review(app,d)
    try:
        push(app,d,d.restore);assert entered.wait(2)
        QTimer.singleShot(0,lambda:ticks.append('responsive'));app.processEvents();assert ticks
        push(app,d,d.cancel);assert host.controller.close_held
        assert 'Cancellation requested' in d.status.text()
        if phase=='parent-close':
            assert not window.close();app.processEvents();assert window.isVisible()
        else:
            assert not d.close();app.processEvents();assert d.isVisible()
        capture(d,'cancel-'+phase+'-held')
    finally:release.set()
    settle(app,host)
    if phase=='before':
        assert data(root)==frozen and host.controller.result is None
        assert not list((root/'backups/knowledge-pack-import').glob('completed-pack-undo-*'))
    else:
        assert host.controller.result.native_saved and data(root)==connected[3]
    assert not host.controller.close_held
    if phase!='parent-close':capture(d,'cancel-'+phase+'-settled')
    d.close();window.close();app.processEvents();settle(app,host)


def test_component_scope_and_controller_retirement_reset_preview(app,connected,catalog_readable_font):
    from core import tag_catalog_connection as connection
    from suite_core.manager.tag_database_controller import TagDatabaseController
    window,host,d=start_preview(app,connected)
    confirm_review(app,d)
    fresh=connection.open_saved_connection(connected[4]).choice
    host.controller.replace_choice(0,fresh)
    app.processEvents();d.render()
    assert not d.confirm.isChecked() and not d.restore.isEnabled()
    assert d.imports.count()==1 and d.areas.topLevelItemCount()==0
    old=host.controller
    host._clear_read_only_authorities();app.processEvents()
    assert host.restore_dialog is None and old.closed
    host.controller=TagDatabaseController(executor=host.executor,settings_path=connected[4],apply_authorized=True,
        before_baseline_change=host.invalidate_default_baseline)
    click(window,'Review completed user import…');replacement=host.restore_dialog
    assert replacement.controller is host.controller and replacement.controller is not old
    assert replacement.open_saved.isEnabled()
    replacement.close();window.close();app.processEvents();settle(app,host)


def test_visible_unknown_delivery_keeps_close_held_without_fake_progress(app,connected,catalog_readable_font):
    from concurrent.futures import Future
    window,host,d=start_preview(app,connected);c=host.controller;real=c.executor;frozen=data(connected[1])
    class Lost:
        close_held=False
        def submit(self,*args):
            future=Future();future.set_exception(RuntimeError('unconfirmed completion delivery'));return future
    c.executor=Lost();confirm_review(app,d)
    push(app,d,d.restore)
    end=monotonic()+3
    while c.pending and monotonic()<end:app.processEvents();sleep(.005)
    app.processEvents()
    assert c.faulted and c.close_held and 'Completion is unconfirmed' in d.status.text()
    assert not d.progress.isVisible() and not d.close()
    assert data(connected[1])==frozen
    capture(d,'unknown-close-held')
    # The exact failure double launched nothing; production unknown holds are retained.
    c.executor=real;c._unconfirmed=False;c.faulted=False
    d.close();window.close();app.processEvents();settle(app,host)



@pytest.mark.parametrize('phase',['opening','listing','comparing'])
def test_visible_read_phase_cancel_remains_responsive(app,connected,monkeypatch,catalog_readable_font,phase):
    from suite_core.encyclopedia.__main__ import create_window
    from PySide6.QtCore import Qt,QTimer
    from PySide6.QtTest import QTest
    from threading import Thread,Event
    window=create_window(settings_path=connected[4]);show_window(window,(1000,740));app.processEvents()
    click(window,'Review completed user import…');h=window.catalog_host;d=h.restore_dialog
    if phase!='opening':push(app,d,d.open_saved);settle(app,h)
    if phase=='comparing':
        push(app,d,d.refresh);settle(app,h)
        QTest.mouseClick(d.imports,Qt.LeftButton);QTest.keyClick(d.imports,Qt.Key_End);QTest.keyClick(d.imports,Qt.Key_Return);app.processEvents()
    entered=Event();release=Event();ticks=[];frozen=data(connected[1])
    def factory(**options):
        target=options.pop('target')
        def delayed():entered.set();assert release.wait(10);target()
        return Thread(target=delayed,**options)
    monkeypatch.setattr(h.executor,'_thread_factory',factory)
    try:
        button={'opening':d.open_saved,'listing':d.refresh,'comparing':d.review_button}[phase]
        push(app,d,button);assert entered.wait(2)
        QTimer.singleShot(0,lambda:ticks.append(True));app.processEvents();assert ticks
        assert d.progress.isVisible() and d.progress.active
        assert 'background admission' not in d.status.text()
        capture(d,'read-'+phase+'-waiting')
        push(app,d,d.cancel);assert 'Cancellation requested' in d.status.text()
    finally:release.set()
    settle(app,h)
    assert data(connected[1])==frozen and not d.restore.isEnabled()
    d.close();window.close();app.processEvents();settle(app,h)


def test_visible_later_correction_holds_comparison(app,connected,monkeypatch,catalog_readable_font):
    window,h,d=start_preview(app,connected)
    path=connected[1]/'data/user/tags.json';value=json.loads(path.read_bytes());value['later_correction']='retain';path.write_text(json.dumps(value),encoding='utf-8')
    frozen=data(connected[1])
    push(app,d,d.refresh);settle(app,h)
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    QTest.mouseClick(d.imports,Qt.LeftButton);QTest.keyClick(d.imports,Qt.Key_End);QTest.keyClick(d.imports,Qt.Key_Return);app.processEvents()
    push(app,d,d.review_button);settle(app,h)
    assert h.controller.error and not d.restore.isEnabled() and data(connected[1])==frozen
    capture(d,'later-correction-held')
    d.close();window.close();app.processEvents();settle(app,h)


def test_complete_preview_budget_holds_without_truncation(connected,tmp_path):
    import core.knowledge_pack as native
    from tests.test_completed_pack_host import evidence
    c,root,receipt,_before,_settings=connected
    path=root/'data/user/tags.json';value=json.loads(path.read_bytes());value['future_blob']='x'*(2300*1024);path.write_text(json.dumps(value),encoding='utf-8')
    source=tmp_path/'personal-source'
    tags=source/'data/user/tags.json';payload=json.loads(tags.read_bytes());payload['tags'].append({'id':'budget-added','display':'Budget addition','category':'new-category'});tags.write_text(json.dumps(payload),encoding='utf-8')
    pack=native.export_knowledge_pack(source,source/'budget.zip');plan=native.plan_knowledge_pack_import(root,pack)
    result=native.import_knowledge_pack(root,pack,plan=plan);latest=Path(result['backup']['path'])/'recovery.json'
    review=c.choice.native_adapter.review_completed_import(latest);frozen=evidence(root)
    with pytest.raises(RuntimeError,match='bound'):native.compare_completed_knowledge_pack_import(root,review)
    assert evidence(root)==frozen



def test_component_goal_switch_preserves_the_existing_review(app,connected,catalog_readable_font):
    window,h,d=start_preview(app,connected)
    reviewed=h.controller.completed_review
    assert not h.show_defaults() and h.controller.completed_review is reviewed
    assert not h.prepare_editing() and h.controller.completed_review is reviewed
    assert 'before editing' in window.status.text()
    if hasattr(h,'create_database'):
        assert not h.create_database() and h.controller.completed_review is reviewed
        assert 'before creating' in window.status.text()
    d.close();app.processEvents()
    sentinel=object();h.controller.review=sentinel
    assert not h.show_completed_import() and h.controller.review is sentinel
    h.controller.review=None
    window.close();app.processEvents();settle(app,h)


@pytest.mark.parametrize('kind',['recover','reload'])
def test_visible_recovery_and_reload_delayed_cancel(app,connected,monkeypatch,catalog_readable_font,kind):
    """Every original-worker phase remains responsive before native admission."""
    from threading import Event,Thread
    from PySide6.QtCore import QTimer
    import core.knowledge_pack as native
    window,h,d=start_preview(app,connected);root=connected[1]
    confirm_review(app,d)
    with monkeypatch.context() as patch:
        if kind=='recover':
            original=native._atomic_bytes;fired=[]
            targets={Path(v['target']) for v in json.loads(connected[2].read_bytes())['files'] if v['expected_before']!=v['expected_after']}
            def interrupted(path,payload):
                original(path,payload)
                if Path(path) in targets and not fired:
                    fired.append(True);raise OSError('delayed recovery fixture interruption')
            patch.setattr(native,'_atomic_bytes',interrupted)
        else:
            patch.setattr(window,'open_publication',lambda *a,**k:False)
        push(app,d,d.restore);settle(app,h)
    inverse=h.controller.result.receipt;frozen=data(root);receipt_bytes=inverse.read_bytes()
    button=d.recover if kind=='recover' else d.reload
    entered=Event();release=Event();ticks=[]
    def factory(**options):
        target=options.pop('target')
        def delayed():entered.set();assert release.wait(10);target()
        return Thread(target=delayed,**options)
    with monkeypatch.context() as patch:
        patch.setattr(h.executor,'_thread_factory',factory)
        try:
            push(app,d,button);assert entered.wait(2)
            QTimer.singleShot(0,lambda:ticks.append(True));app.processEvents();assert ticks
            assert d.progress.isVisible() and d.progress.active and 'elapsed:' in d.status.text()
            assert ('Recovering interrupted restoration' if kind=='recover' else 'Reloading saved catalog') in d.status.text()
            capture(d,'delayed-'+kind)
            push(app,d,d.cancel);assert 'Cancellation requested' in d.status.text()
        finally:release.set()
        settle(app,h)
    assert data(root)==frozen and inverse.read_bytes()==receipt_bytes
    # A fresh explicit retry uses the same pending/saved authority, never Undo again.
    push(app,d,button);settle(app,h)
    assert h.reader_delivery_ok and data(root)==connected[3]
    assert list((root/'backups/knowledge-pack-import').glob('completed-pack-undo-*/recovery.json'))==[inverse]
    d.close();window.close();app.processEvents();settle(app,h)
