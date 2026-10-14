"""Application-owned Qt event driver for explicit local qualification builds.

Only visible widgets and the public normal QWidget Close route are actuated.
Observations are UI evidence; stock owners alone determine native health/drain.
No process, channel, signature, service action or health provider is replaced.
"""
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
import json
import re
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Qt, QEvent
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QAbstractButton, QMessageBox, QToolButton


def _text(value):
    return ' '.join(value.replace('&', '').split())


def reader_update_mode(control,profile,argv,*,a_version):
    """Revalidate readonly signed admission; no new pipe/process/owner or request."""
    from suite_core.encyclopedia.entry_binding import child_binding
    admitted,binding=child_binding(tuple(argv),trust_bytes=profile.trust_bytes,
        trust_sha256=profile.trust_sha256)
    if admitted!=control.admitted:
        raise ValueError('original_qualification_reader_admission_changed')
    return admitted.selected.version==a_version and binding.initial.phase=='healthy'


@dataclass(frozen=True)
class UiStep:
    name: str
    action: str
    label: str = ''
    ready: object = None
    observed: object = None
    modal_title: str | None = None

    def __post_init__(self):
        if (re.fullmatch('[a-z0-9-]{1,64}', self.name) is None
                or self.action not in ('click', 'command', 'help', 'normal-close', 'wait')
                or self.ready is not None and not callable(self.ready)
                or self.observed is not None and not callable(self.observed)):
            raise ValueError('closed_qualification_ui_step_required')


class QtQualificationDriver(QObject):
    """One bounded timer sequence in the owning app; failure leaves UI retained."""
    def __init__(self, window, steps, output_root, *, scenario, timeout=60,
            visible_grace=.5, require_native_ui=False, screenshots=True):
        super().__init__(window)
        if (not steps or any(type(s) is not UiStep for s in steps)
                or len(steps)>32 or len({s.name for s in steps})!=len(steps)
                or type(timeout) not in (int,float) or not 0<timeout<=180
                or type(visible_grace) not in (int,float) or not 0<=visible_grace<=5
                or re.fullmatch('[a-z0-9-]{1,64}', scenario) is None):
            raise ValueError('bounded_qualification_driver_required')
        from suite_core.discovery.component_staging import guard
        self.window, self.steps, self.scenario = window, tuple(steps), scenario
        self.timeout, self.grace, self.screenshots = timeout, visible_grace, screenshots
        self.started=monotonic(); self.deadline=self.started+timeout
        self.index=0; self.fired=False; self.in_tick=False
        self.status='running'; self.reason=''; self.expected_modal=None
        self.output=(Path(output_root)/ (scenario+'-'+uuid4().hex)).absolute()
        guard(self.output)
        self.output.mkdir(parents=True,exist_ok=False)
        self.log=(self.output/'events.jsonl').open('x',encoding='utf-8')
        self.timer=QTimer(self); self.timer.setInterval(20); self.timer.timeout.connect(self.tick)
        self.modal_timer=QTimer(self); self.modal_timer.setInterval(20)
        self.modal_timer.timeout.connect(self.answer_modal)
        window.installEventFilter(self)
        QApplication.instance().lastWindowClosed.connect(self.window_hidden)
        self.record('started', platform=QGuiApplication.platformName(), native_health_claimed=False)
        if require_native_ui and QGuiApplication.platformName() in ('offscreen','minimal'):
            self.finish('held','native_visible_platform_required')
        else:
            self.timer.start(); self.modal_timer.start()

    def record(self,event,**values):
        if not self.log.closed:
            self.log.write(json.dumps(dict(event=event,scenario=self.scenario,
                elapsed_ms=round((monotonic()-self.started)*1000),**values),sort_keys=True)+'\n')
            self.log.flush()

    def finish(self,status,reason=''):
        if self.status!='running': return
        self.status,self.reason=status,reason
        self.timer.stop(); self.modal_timer.stop()
        self.record('finished',status=status,reason=reason,native_health_claimed=False)
        self.log.close()
        # Never quit an app, close a held owner, terminate a process or replay work.

    def _capture(self,name):
        if self.screenshots and self.window.isVisible():
            if not self.window.grab().save(str(self.output/(name+'.png'))):
                raise RuntimeError('qualification_capture_failed')

    def _advance(self):
        step=self.steps[self.index]
        self.record('observed',step=step.name)
        self._capture(step.name)
        self.index+=1; self.fired=False; self.deadline=monotonic()+self.timeout
        if self.index==len(self.steps): self.finish('completed-ui-sequence')

    def window_hidden(self):
        # Last-window quit can precede the next timer; record only an already-fired
        # GUI action's observed completion, never infer process or owner settlement.
        if self.status=='running' and self.fired:
            try:
                step=self.steps[self.index]
                if step.observed is not None and step.observed(): self._advance()
            except Exception as error:
                self.finish('held',type(error).__name__)

    def eventFilter(self,watched,event):
        if watched is self.window and event.type()==QEvent.Hide:
            self.window_hidden()
        return False  # Observation only: never accept, veto or alter a product event.

    def _button(self,label):
        matches=[b for b in self.window.findChildren(QAbstractButton)
            if _text(b.text())==_text(label) and b.isVisible() and b.isEnabled()]
        if len(matches)>1: raise ValueError('ambiguous_qualification_control')
        return matches[0] if matches else None

    def _command(self,label):
        bar=self.window.bar
        matches=[b for b in bar.findChildren(QToolButton)
            if _text(b.text())==_text(label) and b.isVisible() and b.isEnabled()]
        if len(matches)>1: raise ValueError('ambiguous_qualification_command')
        if matches:
            QTest.mouseClick(matches[0],Qt.LeftButton); return True
        overflow=bar.overflow
        if not overflow.isVisible() or not overflow.isEnabled(): return False
        menu=overflow.menu()
        actions=[a for a in menu.actions() if _text(a.text())==_text(label) and a.isEnabled()]
        if len(actions)>1: raise ValueError('ambiguous_qualification_overflow')
        if not actions:return False
        action=actions[0]
        def select():
            if self.within_deadline() and menu.isVisible() and action.isEnabled():
                QTest.mouseClick(menu,Qt.LeftButton,pos=menu.actionGeometry(action).center())
        QTimer.singleShot(0,select)
        QTest.mouseClick(overflow,Qt.LeftButton); return True

    def _perform(self,step):
        if not self.within_deadline():return False
        if step.action=='wait':return True
        if step.action=='normal-close':
            self.window.close()  # Public normal QCloseEvent; all product guards remain.
            return True
        if step.action=='help':
            tabs=self.window.bar.tabs
            choices=[i for i in range(tabs.count()) if tabs.tabData(i)=='Help']
            if len(choices)!=1 or not tabs.isVisible() or not tabs.isEnabled():return False
            QTest.mouseClick(tabs,Qt.LeftButton,pos=tabs.tabRect(choices[0]).center());return True
        if step.action=='command':return self._command(step.label)
        button=self._button(step.label)
        if button is None:return False
        QTest.mouseClick(button,Qt.LeftButton);return True

    def within_deadline(self):
        if self.status!='running':return False
        if monotonic()>self.deadline:
            self.finish('held','step_timeout:'+self.steps[self.index].name)
            return False
        return True

    def answer_modal(self):
        # This independent timer also runs while _perform is blocked in exec().
        # Check even without an expected/available modal, and before any dispatch.
        if not self.within_deadline() or self.expected_modal is None:return
        owned=[w for w in QApplication.instance().topLevelWidgets()
            if isinstance(w,QMessageBox) and w.isVisible() and (w.parentWidget() is self.window
                or w.parentWidget() is not None and self.window.isAncestorOf(w.parentWidget()))]
        if not owned:return
        if len(owned)!=1 or owned[0].windowTitle()!=self.expected_modal:
            self.finish('held','unexpected_owned_modal');return
        button=owned[0].button(QMessageBox.Yes)
        if button is None or not button.isVisible() or not button.isEnabled():return
        if not self.within_deadline():return
        title=self.expected_modal
        self.expected_modal=None
        QTest.mouseClick(button,Qt.LeftButton,delay=0)
        self.record('modal-confirmed',title=title)

    def tick(self):
        if self.status!='running' or self.in_tick:return
        self.in_tick=True
        try:
            if not self.within_deadline():return
            if monotonic()-self.started<self.grace:return
            step=self.steps[self.index]
            if self.fired:
                if step.observed is None or step.observed():self._advance()
                return
            if not self.window.isVisible() or step.ready is not None and not step.ready():return
            self.fired=True  # Set before nested modal event loops; never double-submit.
            self.expected_modal=step.modal_title
            self.record('intent',step=step.name,action=step.action)
            if not self._perform(step):
                self.fired=False;self.expected_modal=None;return
            self.record('action-dispatched',step=step.name,action=step.action)
            if self.status=='running' and self.fired and (step.observed is None or step.observed()):
                self._advance()
        except Exception as error:
            self.finish('held',type(error).__name__+': '+str(error)[:256])
        finally:self.in_tick=False


def _panel(dialog):
    from suite_core.manager.encyclopedia_acquisition_ui import EncyclopediaAcquisitionPanel
    panels=dialog.findChildren(EncyclopediaAcquisitionPanel)
    if len(panels)!=1:raise ValueError('one_original_qualification_panel_required')
    return panels[0]


def enrollment_driver(dialog, output_root, *, recover=False, **options):
    panel=_panel(dialog)
    idle=lambda:not panel.close_held
    installed=lambda:idle() and panel.installed is not None and panel.installed.selected is not None
    steps=[UiStep('refresh','click','Check Again',idle,idle),
        UiStep('install','click','Install',idle,installed,modal_title='Install Tag Encyclopedia')]
    if recover:
        steps += [UiStep('technical-details','click','Technical Details',idle,lambda:panel.technical.isChecked()),
            UiStep('check-status','click','Check app status',idle,idle),
            UiStep('confirm-recovery','click',panel.recovery_confirm.text(),idle,
            lambda:panel.recovery_confirm.isChecked()),
            UiStep('recover','click','Recover installation',idle,
                lambda:idle() and panel.installed is not None and panel.installed.selected is None),
            UiStep('close-setup','click','Close',idle,lambda:not dialog.isVisible())]
    else:
        steps.append(UiStep('open-reader','click','Open',idle,
            lambda:not dialog.isVisible()))
    return QtQualificationDriver(dialog,steps,output_root,
        scenario='enrollment-recovery' if recover else 'enrollment-open',**options)


def reader_driver(window, output_root, *, update=False, recover=False, expected_version=None, **options):
    host=window.catalog_host
    idle=lambda:not host.busy
    steps=[]
    if update:
        available=lambda:idle() and host.entry_notice is not None and host.entry_notice.status=='available' and (
            expected_version is None or host.entry_notice.version==expected_version)
        request=lambda:idle() and host.entry_request is not None and host.entry_request is host.entry_check.pending_request
        steps=[UiStep('help','help',ready=idle),
            UiStep('check-first','command','Check for Updates',idle,available),
            UiStep('confirm-first','command','Review Available Update',idle,request,'Review Tag Encyclopedia update'),
            UiStep('cancel-update','command','Cancel Pending Update',idle,
                lambda:idle() and host.entry_request is None),
            UiStep('check-second','command','Check for Updates',idle,available),
            UiStep('confirm-second','command','Review Available Update',idle,request,'Review Tag Encyclopedia update')]
    elif recover:
        steps=[UiStep('help','help',ready=idle),
            UiStep('status','command','Check Update / Recovery Status',idle,
                lambda:idle() and host.entry_status is not None and host.entry_status.phase=='applying'),
            UiStep('confirm-recovery','command','Recover Interrupted Update',idle,
                lambda:idle() and host.entry_last_action=='recovery_waiting_for_close','Recover interrupted update')]
    steps.append(UiStep('normal-close','normal-close',ready=idle,observed=lambda:not window.isVisible()))
    return QtQualificationDriver(window,steps,output_root,
        scenario='reader-update' if update else 'reader-recovery' if recover else 'reader-close',**options)


def root_recovery_driver(dialog,output_root,**options):
    panel=_panel(dialog); idle=lambda:not panel.close_held
    steps=[UiStep('check-status','click','Check app status',idle,idle),
        UiStep('confirm-recovery','click',panel.recovery_confirm.text(),idle,
            lambda:panel.recovery_confirm.isChecked()),
        UiStep('recover','click','Recover installation',idle,idle),
        UiStep('check-recovered','click','Check app status',idle,idle),
        UiStep('run-prior','click','Run reader to finish setup',idle,idle),
        UiStep('close-root','click','Close Tag Encyclopedia',idle,lambda:not dialog.isVisible())]
    return QtQualificationDriver(dialog,steps,output_root,scenario='root-recovery',**options)


class RootOrdinaryCloseRetryDriver(QtQualificationDriver):
    """Two ordinary visible Close attempts; no recovery/backend authority."""
    def __init__(self,dialog,output_root,**options):
        if 'timeout' in options:raise ValueError('fixed_root_ordinary_close_budget_required')
        self.overall_deadline=monotonic()+20
        panel=_panel(dialog);idle=lambda:not panel.close_held
        def refused_first_close():
            if not dialog.isVisible():return True
            buttons=[b for b in dialog.findChildren(QAbstractButton)
                if _text(b.text())=='Close Tag Encyclopedia' and b.isVisible()]
            if len(buttons)!=1:raise ValueError('exact_root_close_control_required')
            return idle() and not buttons[0].isEnabled()
        steps=[UiStep('check-status-1','click','Check app status',idle,idle),
            UiStep('close-root-1','click','Close Tag Encyclopedia',idle,refused_first_close),
            UiStep('check-status-2','click','Check app status',idle,idle),
            UiStep('close-root-2','click','Close Tag Encyclopedia',idle,lambda:not dialog.isVisible())]
        super().__init__(dialog,steps,output_root,scenario='root-ordinary-close-retry',timeout=20,**options)
        self.deadline=min(self.deadline,self.overall_deadline)

    def within_deadline(self):
        if self.status=='running' and monotonic()>self.overall_deadline:
            self.finish('held','root_ordinary_close_retry_deadline')
            return False
        return super().within_deadline()

    def _advance(self):
        if not self.within_deadline():return
        if self.steps[self.index].name in ('close-root-1','close-root-2') and not self.window.isVisible():
            self.record('observed',step=self.steps[self.index].name)
            self.finish('completed-ui-sequence');return
        super()._advance()
        if self.status=='running':self.deadline=min(self.deadline,self.overall_deadline)

    def window_hidden(self):
        if self.status=='running' and not self.window.isVisible():
            if not self.within_deadline():return
            if self.fired and self.steps[self.index].name in ('close-root-1','close-root-2'):
                self.record('observed',step=self.steps[self.index].name)
                self.finish('completed-ui-sequence')
            else:self.finish('held','unsolicited_root_dialog_hide')
            return
        super().window_hidden()

    def answer_modal(self):
        if not self.within_deadline():return
        modals=[w for w in QApplication.instance().topLevelWidgets()
            if w is not self.window and w.isVisible()
            and (w.parentWidget() is self.window or self.window.isAncestorOf(w))
            and (isinstance(w,QMessageBox) or w.isModal())]
        if modals:
            self.finish('held','unexpected_owned_modal');return
        super().answer_modal()


def root_ordinary_close_retry_driver(dialog,output_root,**options):
    return RootOrdinaryCloseRetryDriver(dialog,output_root,**options)
