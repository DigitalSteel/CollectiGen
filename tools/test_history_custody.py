"""Exact packaged history dialog/task checks over generated memory records.

No actual image, decision store, private/default application profile or Root is
opened. Bundled Python -I -B supplies only this admitted package's source/runtime.
"""
import argparse,json,time,sys
from pathlib import Path
from threading import Event
from types import SimpleNamespace as NS
from PySide6.QtCore import Qt,QTimer,QObject,Signal
from PySide6.QtGui import QImage,QFont,QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QDialog,QMessageBox,QWidget,QPushButton,QVBoxLayout
from shiboken6 import isValid
from core.background_jobs import BackgroundJobManager
from core.duplicate_finder import DuplicateDistinctDecision
from qt_app.theme import apply_theme
from qt_app.job_progress import bind_job_progress
import qt_app.duplicate_finder_dialog as ui

def wait(test,seconds=12):
    end=time.monotonic()+seconds
    while not test():
        assert time.monotonic()<end,'Owned generated operation did not settle'
        QApplication.processEvents();QTest.qWait(10)
    QApplication.processEvents()

class Decisions:
    def __init__(self,root,mode):
        self.rows=tuple(DuplicateDistinctDecision('generated-'+str(i),'2026-10-30T12:00:00+00:00','Generated comparison '+str(i),'first-'+str(i),root/('generated-first-'+str(i)+'.png'),'second-'+str(i),root/('generated-second-'+str(i)+'.png')) for i in (1,2))
        self.mode=mode;self.entered=Event();self.release=Event();self.attempts=[]
    def history(self):return self.rows
    def remove_key(self,key):
        self.attempts.append(key);self.entered.set();assert self.release.wait(20)
        if self.mode=='failure':raise OSError('Generated persistence error; no physical store used')
        self.rows=tuple(x for x in self.rows if x.key!=key)
        return self.mode!='absent'

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args();assert not args.output.exists();args.output.mkdir(parents=True)
    app=QApplication([]);app.setQuitOnLastWindowClosed(False);fid=QFontDatabase.addApplicationFont('C:/Windows/Fonts/segoeui.ttf');assert fid>=0;app.setFont(QFont(QFontDatabase.applicationFontFamilies(fid)[0],10));errors=[];previous_hook=sys.excepthook;sys.excepthook=lambda kind,error,tb:errors.append(kind.__name__+': '+str(error));warnings=[];previews=[];original_task=ui.AsyncCallTask
    def preview(fn):
        task=original_task(fn);previews.append(task);return task
    def pixels(_path):
        image=QImage(80,60,QImage.Format_RGB32);image.fill(Qt.darkCyan);return image
    ui.AsyncCallTask=preview;ui.QImage=pixels;ui.content_safety_policy=lambda:None;ui.QMessageBox.warning=lambda *a,**kw:warnings.append((a[1],a[2]));cases=[]
    try:
        for theme in ('light','dark'):
            apply_theme(app,theme)
            for mode in ('success','failure','absent','cancelled','default-cancel'):
                store=Decisions(args.output,mode);manager=BackgroundJobManager(limits={'io':1});d=ui.DistinctHistoryDialog(NS(settings=NS(gallery_blur_unknown=False),jobs=manager,duplicates=NS(decisions=store)));d.resize(1080,790);changed=[];finished=[];d.changed.connect(changed.append);d.finished.connect(finished.append);d.show();app.processEvents();warnings.clear();original_key=d._selected().key;assert d.remove.isEnabled()
                confirmations=[]
                def confirm():
                    box=QApplication.activeModalWidget();assert isinstance(box,QMessageBox);assert box.defaultButton().text()=='Cancel' and box.escapeButton().text()=='Cancel';confirmations.append(True)
                    if mode=='default-cancel':box.defaultButton().click()
                    else:
                        next(b for b in box.buttons() if b.text()=='Remove Mark').click()
                try:
                    QTimer.singleShot(20,confirm);d.remove.click();assert confirmations==[True]
                    if mode=='default-cancel':assert not store.attempts and d._task is None and not changed;cases.append(theme+'-default-confirmation-cancel');continue
                    wait(store.entered.is_set);task=d._task;assert task is not None
                    d.close();QTest.keyClick(d,Qt.Key_Escape);d.reject();d.done(QDialog.Accepted);app.processEvents();assert d.isVisible() and not finished and d._task is task and not changed
                    assert 'Wait for the accepted' in d.close_button.toolTip() and 'Stop this operation' not in d.close_button.toolTip();assert not d.remove.isEnabled()
                    assert d.grab().save(str(args.output/(theme+'-'+mode+'-pending.png')))
                    if mode=='cancelled':manager.cancel(task.job_id)
                    store.release.set();wait(lambda:d._task is None);assert store.attempts==[original_key] and not finished and d.isVisible()
                    if mode=='success':
                        assert changed==[original_key] and len(d.rows)==1;assert d.reason.text()=='Reason: '+d._selected().reason and d.remove.isEnabled();assert 'mark removed' in d.outcome.text()
                    elif mode=='failure':assert not changed and len(d.rows)==2 and warnings and 'not confirmed' in d.outcome.text()
                    elif mode=='absent':assert not changed and len(d.rows)==1 and 'already absent' in d.outcome.text()
                    else:assert not changed and 'not confirmed' in d.outcome.text() and len(store.rows)==1
                    assert 'Wait for the accepted' not in d.close_button.toolTip();assert d.grab().save(str(args.output/(theme+'-'+mode+'-outcome.png')));cases.append(theme+'-'+mode+'-custody-result')
                finally:
                    store.release.set();assert manager.shutdown(wait=True,timeout=3);wait(lambda:d._task is None);wait(lambda:all(t.is_finished() for t in previews));app.processEvents();assert not errors,errors
                    if isValid(d):d.close();assert not d.isVisible();d.deleteLater();app.processEvents()
        # Existing progress callers keep their default; overrides restore only
        # their owner-wired control on all three terminal outcomes.
        class Signals(QObject):
            complete=Signal(object);failed=Signal(str);cancelled=Signal()
        for override in (False,True):
            for terminal in ('complete','failed','cancelled'):
                owner=QWidget();QVBoxLayout(owner);button=QPushButton('Close',owner);button.setToolTip('Original owner hint');signals=Signals();job=NS(state='running',current=0,total=0,message='Generated phase',queue_wait_seconds=0,cancel_requested=False);task=NS(signals=signals,job_id='generated',name='Generated',manager=NS(get=lambda _id:job));kwargs=dict(cancel_tooltip='Wait for accepted result') if override else {};binding=bind_job_progress(owner,task,title='Generated operation',cancel_button=button,**kwargs);binding.refresh();assert button.toolTip()==('Wait for accepted result' if override else 'Stop this operation at its next safe boundary.')
                if terminal=='complete':signals.complete.emit(True)
                elif terminal=='failed':signals.failed.emit('Generated error')
                else:signals.cancelled.emit()
                assert binding.task is None and not binding.timer.isActive();assert button.toolTip()==('Original owner hint' if override else 'Stop this operation at its next safe boundary.');owner.close();owner.deleteLater();app.processEvents();cases.append('progress-'+str(override)+'-'+terminal)
        assert len(cases)==16 and not errors
        result=dict(passed=True,cases=cases,actual_history_dialog_and_confirmation=True,original_task_coordinator=True,pending_close_escape_reject_done_refused=True,accepted_result_observer_retained=True,exact_success_identity_once=True,remaining_selection_reason_action_synchronized=True,failure_and_cancellation_uncertainty_truthful=True,default_confirmation_cancel_preserved=True,optional_tooltip_terminal_restore_and_default_compatibility=True,all_owned_managers_and_previews_drained=True,images='generated in-memory pixels only',decision_store='inert generated records; no persistence operation',actual_parent_store_image_recovery_or_Root_execution=False,private_default_application_data_access=False)
        (args.output/'RESULT.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
    finally:sys.excepthook=previous_hook;app.quit()

if __name__=='__main__':main()
