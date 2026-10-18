"""Typed Organizer creation over the original retained helper/Job lifecycle."""
import os
from core.encyclopedia_connection import OwnedEncyclopedia, native_reader, EncyclopediaHold
from core.organizer_connection import OrganizerConnection

_ENVIRONMENT_OVERRIDES=frozenset({'PYTHONHOME','PYTHONPATH','QT_PLUGIN_PATH',
                                'QT_QPA_PLATFORM_PLUGIN_PATH','QML2_IMPORT_PATH'})


class OwnedOrganizer(OwnedEncyclopedia):
    """Reuse original polling, birth identity, normal exit and output drain."""
    def __init__(self, session, reader_owner, *, factory=native_reader):
        from suite_core.manager.bootstrap_entry import NormalEntrySession
        if type(session) is not NormalEntrySession or type(reader_owner) is not OwnedEncyclopedia or reader_owner.normal_entry_session is not session:
            raise EncyclopediaHold('original_organizer_session_required')
        super().__init__(factory=factory,launch_allowed=False,normal_entry_session=session)
        self.reader_owner=reader_owner
        self.reason='No Organizer is running.'

    def selection_for(self, selected):
        from core.manager_launch_owner import RootOrganizerSelection
        session=self.normal_entry_session
        selection=getattr(session,'organizer_selection',None)
        if (type(selected) is not OrganizerConnection or type(selection) is not RootOrganizerSelection
                or selection.selected!=selected):
            raise EncyclopediaHold('Restart Manager to admit this saved Organizer connection.')
        selection.validate()
        return selection

    @property
    def available(self):
        from core.manager_launch_owner import RootOrganizerSelection
        return type(getattr(self.normal_entry_session,'organizer_selection',None)) is RootOrganizerSelection

    def launch(self,selected):
        self.reader_owner.assert_idle();self.assert_idle()
        selection=self.selection_for(selected)
        self._health=None;self._visible_seen=self._stopped=False
        native=self.factory();self.native,self.selected=native,selection
        try:
            from suite_core.manager.bootstrap_entry import EntryHelperAdapter
            adapter=EntryHelperAdapter.for_organizer(self.normal_entry_session,native,selection)
            native.bind_helper_capability(adapter)
            environment={key:value for key,value in os.environ.items() if key.upper() not in _ENVIRONMENT_OVERRIDES}
            self.child=native.spawn_command(selection.command,selected.directory,environment,helper_context=selection.context())
            self.identity=native.identity(self.child,selection.program)
            self.reason='Organizer process started. Close its window normally when finished.'
        except BaseException:
            self.reason='Organizer startup failed. Its original owner is retained until cleanup is verified.'
            raise

    def poll(self):
        if self.native is not None and self.child is None and getattr(self.native,'helper_close_held',False):
            self.reason='Organizer startup accounting is unconfirmed. Review the original startup recovery before retrying.'
            return False
        result=super().poll()
        self.reason=self.reason.replace('Reader','Organizer').replace('reader','Organizer')
        return result

    def native_health(self,selected):
        self.poll()
        if (self.close_held or self._health is None
                or self._health is not getattr(self.normal_entry_session,'organizer_selection',None)
                or self._health.selected!=selected):return False
        selection=self.selection_for(selected)
        return self._health is selection

    def begin_close(self):
        # Ordinary Close never terminates a child or fabricates a health receipt.
        return self.poll() and not self.close_held

    def cancel_close(self):return None

    def finish_close(self):return self.begin_close()

    @property
    def startup_recovery_available(self):
        return (self.native is not None and self.child is None
                and getattr(self.native,'helper_accounting_identity',None) is not None
                and not getattr(self.native,'_helper_create_entered',True))

    def reconcile_startup(self,reviewed_identity,*,cancelled=lambda:False):
        """Stock positive non-creation evidence; durable receipt before latch release."""
        from uuid import uuid4
        from suite_core.discovery.component_staging import guard,atomic,canonical,require
        native=self.native
        require(native is not None and self.child is None and self.startup_recovery_available,
                'original_organizer_non_creation_required')
        session=self.normal_entry_session
        directory=session.admitted.layout.data_root/'Manager/OrganizerAcquisition/HelperRecovery'
        guard(directory);directory.mkdir(parents=True,exist_ok=True)
        path=directory/(uuid4().hex+'.json')
        def record(evidence):
            require(self.native is native and evidence.attempt.identity==reviewed_identity,
                    'original_helper_attempt_changed')
            payload={'schema':1,'component':'collectigen.media-organizer',
                'helper_attempt_id':evidence.attempt.identity,'evidence_id':evidence.evidence_id,
                'phase':'helper_non_creation_resolved'}
            raw=canonical(payload)
            require(not path.exists(),'new_helper_recovery_receipt_required')
            atomic(path,payload);require(path.read_bytes()==raw,'helper_recovery_readback')
        result=native.reconcile_helper_non_creation(reviewed_identity,record=record,cancelled=cancelled)
        require(not native.helper_close_held,'original_helper_recovery_still_held')
        self.poll()
        return result
