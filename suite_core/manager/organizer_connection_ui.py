"""Isolated Applications card; injected trust/service/serial owner, no startup binding."""
from concurrent.futures import CancelledError
from threading import Event
from datetime import datetime, timedelta, timezone
from PySide6.QtWidgets import QFrame,QVBoxLayout,QHBoxLayout,QLabel,QLineEdit,QPushButton,QFileDialog
from core.component_release_identity import display_component_version

class OrganizerConnectionHost:
    """Explicit cached publisher inputs and retained connection-only workspace."""
    def __init__(self, *, store, publisher_inputs, executor_factory=None,
                 review=None, observe_version=None, clock=None, metadata_refresh=None, metadata_state=None, metadata_can_refresh=True):
        from .resource_executor import ResourceExecutorOwner
        from core.organizer_connection import review_existing_package, observe_installed_version
        self.store=store;self.publisher_inputs=None if publisher_inputs is None else dict(publisher_inputs)
        self.executor_factory=executor_factory or ResourceExecutorOwner
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        self.review=review or review_existing_package
        def observe(selected,inputs):
            values={k:v for k,v in inputs.items() if k!='expected_version'}
            return observe_installed_version(selected,binding_revision=selected.binding_revision,
                                             now=self.clock(),**values)
        self.observe_version=observe_version or observe
        self.metadata_refresh=metadata_refresh
        self.metadata_state=metadata_state;self.metadata_can_refresh=metadata_can_refresh
        self.card=None;self.resume_requested=False;self.parent=None

    def create_card(self,parent):
        if self.card is not None:raise ValueError('organizer_workspace_already_retained')
        self.parent=parent
        self.card=OrganizerConnectionCard(store=self.store,executor=self.executor_factory(),
            review=self.review,observe_version=self.observe_version,publisher_inputs=self.publisher_inputs,
            clock=self.clock,metadata_refresh=self.metadata_refresh,metadata_state=self.metadata_state,
            metadata_can_refresh=self.metadata_can_refresh,parent=parent)
        return self.card

    @property
    def close_held(self):return self.card is not None and self.card.close_held

    def begin_close(self):
        self.resume_requested=False
        if self.card is not None:self.card.begin_close()

    def cancel_close(self):self.resume_requested=True

    def poll(self):
        if self.card is None:return None
        self.card.poll()
        if self.resume_requested and not self.card.closing and not self.card.executor.closed:
            self.resume_requested=False
            return None
        if not self.resume_requested or self.card.close_held:return None
        old=self.card
        # Preserve the newest selection/trust, not the original factory snapshot.
        self.publisher_inputs=None if old.publisher_inputs is None else dict(old.publisher_inputs)
        self.metadata_state=old.metadata_state;self.metadata_can_refresh=old.metadata_can_refresh
        path=old.directory.text()
        self.card=None;new=self.create_card(self.parent)
        new.directory.setText(path)
        self.resume_requested=False
        return old,new

class OrganizerConnectionCard(QFrame):
    def __init__(self, *, store, executor, review, observe_version, publisher_inputs=None, clock=None, metadata_refresh=None, metadata_state=None, metadata_can_refresh=True, parent=None):
        super().__init__(parent)
        self.store,self.executor,self.review,self.observe_version = store,executor,review,observe_version
        self.publisher_inputs = None if publisher_inputs is None else dict(publisher_inputs)
        self.metadata_refresh=metadata_refresh
        self.metadata_state=metadata_state;self.metadata_can_refresh=metadata_can_refresh
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        self.submission_epoch=None;self.closing=False;self.observed_at=None;self.stale=False
        self.cancel_intent_epoch=None
        self.epoch = 0; self.future = None; self.reviewed = self.observation = None
        self.saved = self.snapshot = None; self.cancel_event = None; self.operation = None
        self.setObjectName('visualCard')
        self.setProperty('componentKey','organizer-connection')
        from suite_core.manager.ui_cards import card_style, compact_action
        card_style(self, compact=True)
        layout=QVBoxLayout(self);title=QLabel('Media Organizer');title.setObjectName('cardTitle');layout.addWidget(title)
        description=QLabel('Connect the Organizer folder you already extracted.');description.setWordWrap(True);layout.addWidget(description)
        self.directory=QLineEdit();self.directory.setPlaceholderText('Extracted Organizer folder');layout.addWidget(self.directory)
        self.status=QLabel('Installed version unknown.');self.status.setWordWrap(True);layout.addWidget(self.status)
        row=QHBoxLayout();layout.addLayout(row)
        self.choose=QPushButton('Choose folder');self.check=QPushButton('Review package');self.save=QPushButton('Save connection');self.cancel=QPushButton('Cancel review')
        self.reload=QPushButton('Reload saved')
        self.refresh_release=QPushButton('Refresh release info')
        for button in (self.choose,self.check,self.save,self.cancel):row.addWidget(button)
        for button in (self.choose,self.check,self.save,self.cancel,self.reload):compact_action(button)
        layout.addWidget(self.reload)
        layout.addWidget(self.refresh_release)
        self.refresh_release.setVisible(callable(self.metadata_refresh))
        self.directory.textChanged.connect(self.selection_changed)
        self.choose.clicked.connect(self.choose_folder);self.check.clicked.connect(self.begin_review);self.save.clicked.connect(self.begin_save);self.cancel.clicked.connect(self.cancel_review)
        self.reload.clicked.connect(self.begin_reload)
        self.refresh_release.clicked.connect(self.begin_metadata_refresh)
        self.refresh_controls()

    @property
    def close_held(self):return self.future is not None or self.executor.close_held

    def refresh_controls(self):
        busy=self.close_held
        closed=self.closing or self.executor.closed
        self.check.setEnabled(not busy and bool(self.directory.text()) and self.publisher_inputs is not None)
        self.choose.setEnabled(self.operation != 'save');self.directory.setEnabled(self.operation != 'save')
        self.save.setEnabled(not busy and self.reviewed is not None and self.observation is not None and self.reviewed!=self.saved)
        self.cancel.setEnabled(busy and self.operation in ('review','reload','refresh'))
        self.reload.setEnabled(not busy and self.publisher_inputs is not None)
        self.refresh_release.setEnabled(not busy and callable(self.metadata_refresh) and self.metadata_can_refresh)
        if not busy and self.publisher_inputs is None:self.status.setText('Release verification data is unavailable. Use Refresh release info when connected; installed version is unknown.' if callable(self.metadata_refresh)
            else 'Release verification data is unavailable. Installed version unknown.')
        if closed:
            for control in (self.choose,self.directory,self.check,self.save,self.cancel,self.reload,self.refresh_release):control.setEnabled(False)
            self.status.setText('Connection workspace closed. Wait for the worker to finish before reopening it.')
        elif self.stale:self.save.setEnabled(False)
        if not closed and not busy and not self.metadata_can_refresh and self.publisher_inputs is None:
            self.status.setText('The release cache needs recovery. Refresh cannot fix it. Keep the cache files unchanged and report this issue.' if self.metadata_state=='cache_recovery_required'
                else 'Release verification is held. Keep cached files unchanged; this Manager cannot resolve this state yet.')

    def selection_changed(self):
        self.cancel_intent_epoch=None
        self.epoch+=1;self.reviewed=self.observation=None
        if self.cancel_event:self.cancel_event.set()
        self.status.setText('Installed version unknown. Review this folder to check it.');self.refresh_controls()

    def replace_publisher_inputs(self, inputs):
        if self.operation=='save':raise ValueError('connection_save_settlement_pending')
        self.cancel_intent_epoch=None;self.epoch+=1
        if self.cancel_event:self.cancel_event.set()
        self.publisher_inputs=None if inputs is None else dict(inputs)
        self.reviewed=self.observation=None
        self.status.setText('Installed version unknown. Review the package again.');self.refresh_controls()

    def choose_folder(self):
        path=QFileDialog.getExistingDirectory(self,'Choose extracted Organizer folder')
        if path:self.directory.setText(path)

    def begin_review(self):
        if self.closing or self.executor.closed:return
        if self.close_held or self.publisher_inputs is None:return
        self.epoch+=1;epoch=self.epoch;directory=self.directory.text();inputs=dict(self.publisher_inputs)
        self.reviewed=self.observation=None;self.cancel_event=Event();cancel=self.cancel_event
        self.operation='review';self.status.setText('Checking package…')
        def work():
            snapshot=self.store.observe()
            if cancel.is_set():raise CancelledError()
            selected=self.review(directory, binding_revision=snapshot.digest or 'new', **inputs)
            if cancel.is_set():raise CancelledError()
            observation=self.observe_version(selected,inputs)
            if cancel.is_set():raise CancelledError()
            return epoch,snapshot,selected,observation
        self.submission_epoch=epoch;self.future=self.executor.submit(work);self.refresh_controls()

    def begin_reload(self):
        if self.closing or self.executor.closed:return
        if self.close_held or self.publisher_inputs is None:return
        self.epoch+=1;epoch=self.epoch;inputs=dict(self.publisher_inputs)
        self.reviewed=self.observation=None;self.cancel_event=Event();cancel=self.cancel_event
        self.operation='reload';self.status.setText('Checking saved connection…')
        def work():
            snapshot=self.store.observe();selected=self.store.selected(snapshot)
            if cancel.is_set():raise CancelledError()
            if selected is None:return epoch,snapshot,None,None
            checked=self.review(selected.directory,expected=selected,binding_revision=selected.binding_revision,**inputs)
            observation=self.observe_version(checked,inputs)
            if cancel.is_set():raise CancelledError()
            return epoch,snapshot,checked,observation
        self.submission_epoch=epoch;self.future=self.executor.submit(work);self.refresh_controls()

    def cancel_review(self):
        if self.operation not in ('review','reload','refresh'):return
        self.epoch+=1;self.reviewed=self.observation=None;self.cancel_event.set()
        self.cancel_intent_epoch=self.epoch
        self.status.setText('Review canceled. Waiting for the worker to finish.');self.refresh_controls()

    def begin_save(self):
        if self.closing or self.executor.closed or self.stale:return
        if self.close_held or self.reviewed is None:return
        selected,snapshot=self.reviewed,self.snapshot;inputs=dict(self.publisher_inputs);epoch=self.epoch
        self.operation='save';self.status.setText('Saving connection…')
        def work():
            checked=self.review(selected.directory,expected=selected,binding_revision=selected.binding_revision,**inputs)
            saved=self.store.save(checked,expected=snapshot)
            current=self.store.selected(saved)
            try:observation=self.observe_version(current,inputs)
            except Exception:observation=None
            return epoch,saved,current,observation
        self.submission_epoch=epoch;self.future=self.executor.submit(work);self.refresh_controls()

    def begin_metadata_refresh(self):
        if self.closing or self.executor.closed or self.close_held or not callable(self.metadata_refresh) or not self.metadata_can_refresh:return
        self.epoch+=1;epoch=self.epoch
        self.reviewed=self.observation=None;self.cancel_intent_epoch=None
        self.cancel_event=Event();cancel=self.cancel_event
        self.operation='refresh';self.status.setText('Refreshing release information…')
        self.submission_epoch=epoch
        self.future=self.executor.submit(lambda:(epoch,*self.metadata_refresh(cancel.is_set)))
        self.refresh_controls()

    def poll(self):
        self.expire_observation()
        if self.future is None or not self.future.done() or self.executor.close_held:return
        future,self.future=self.future,None;operation=self.operation;self.operation=None
        submission_epoch,self.submission_epoch=self.submission_epoch,None
        try:
            if submission_epoch!=self.epoch:
                try:future.result()
                except BaseException:pass
                if self.cancel_intent_epoch==self.epoch:
                    self.cancel_intent_epoch=None
                    self.status.setText('Review canceled. Installed version unknown.')
                return
            result=future.result()
            if operation=='refresh':
                epoch,inputs,state=result
                if epoch!=self.epoch:return
                self.metadata_state=state;self.metadata_can_refresh=getattr(state,'can_refresh',True)
                self.replace_publisher_inputs(inputs)
                self.status.setText('Release information is ready. Review the selected folder.' if inputs is not None
                    and state!='refresh_unavailable_cached_verified' else 'Refresh unavailable. Cached release information is still verified; you can review the folder.' if inputs is not None
                    else 'Release information is unavailable. Try Refresh release info when connected.')
                return
            epoch,snapshot,selected,observation=result
            if epoch!=self.epoch:return
            self.snapshot,self.reviewed,self.observation=snapshot,selected,observation
            if selected is None:
                self.status.setText('No connection saved. Choose the extracted Organizer folder.');return
            self.directory.blockSignals(True);self.directory.setText(str(selected.directory));self.directory.blockSignals(False)
            if operation in ('save','reload'):self.saved=selected
            self.stale=False;self.observed_at=None
            if observation is None:
                self.status.setText('Connection saved. Installed version could not be checked. Reload saved to try again.');return
            self.observed_at=getattr(observation,'observed_at',None) or self.clock()
            suffix='. Connection saved.' if operation=='save' else '. Saved connection checked.' if operation=='reload' else '. Save this connection to keep it.'
            self.status.setText('Installed '+display_component_version('collectigen.media-organizer',observation.raw_version)+suffix)
        except CancelledError:self.status.setText('Review canceled. Installed version unknown.')
        except Exception:
            self.reviewed=self.observation=None
            if operation=='refresh':
                self.publisher_inputs=None
                self.status.setText('Could not refresh release information. Try again when connected.')
            else:self.status.setText('Package unavailable or changed. Choose the extracted folder and review it again.')
        finally:
            self.expire_observation()
            self.refresh_controls()

    def begin_close(self):
        if self.operation in ('review','reload','refresh'):self.cancel_review()
        self.closing=True
        self.executor.begin_close()
        self.refresh_controls()

    def expire_observation(self):
        if self.observation is None or self.observed_at is None or self.stale:return
        lifetime=getattr(self.observation,'valid_for',timedelta(seconds=30))
        if self.clock()-self.observed_at>=lifetime:
            self.stale=True
            if not self.close_held and not self.closing:
                self.status.setText('Last observed '+display_component_version('collectigen.media-organizer',self.observation.raw_version)+'. This check is stale; review the package again.')
            self.refresh_controls()
