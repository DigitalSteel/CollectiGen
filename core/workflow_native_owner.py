"""One original Generate/Train child, shared writer and retained control pump.

No desktop automation or process discovery is used. Original suspended creation,
helper accounting, physical Job/output drain and signed selection remain stock.
"""
from threading import Thread, Event, RLock

from core.encyclopedia_connection import EncyclopediaHold, native_reader
from core.organizer_native_owner import _organizer_environment
from core.workflow_connection import WorkflowConnection, workflow_component
from core.workflow_root_selection import RootWorkflowSelection


class WorkflowWorkspaceOwner:
    def __init__(self, session, reader_owner, *, factory=native_reader):
        from suite_core.manager.bootstrap_entry import NormalEntrySession
        from core.encyclopedia_connection import OwnedEncyclopedia
        if (type(session) is not NormalEntrySession or type(reader_owner) is not OwnedEncyclopedia
                or reader_owner.normal_entry_session is not session):
            raise EncyclopediaHold('original_workflow_session_required')
        self.session, self.reader_owner, self.factory = session, reader_owner, factory
        self.native = self.child = self.identity = self.selection = self.actor = self.worker = None
        self._stop = Event()
        self._lock = RLock()
        self.error = None
        self.diagnostics = None
        self.failure_reference = None
        self.last_health = None
        self.return_target = None
        self.pending_generate_draft = self._launch_draft = None
        self._visible_seen = False
        self.root_image_scope = None
        self.reason = 'No Generate or Train work is running.'

    @property
    def close_held(self):
        return self.native is not None or self.worker is not None and self.worker.is_alive()

    def assert_idle(self):
        with self._lock:
            self.poll()
            if self.close_held:
                raise EncyclopediaHold('Close Generate or Train and wait for its work to finish before changing shared settings or models.')

    def selection_for(self, selected):
        if type(selected) is not WorkflowConnection:
            raise EncyclopediaHold('typed_workflow_connection_required')
        workflow_component(selected.component_id)
        selection = getattr(self.session, 'workflow_selections', {}).get(selected.component_id)
        if type(selection) is not RootWorkflowSelection or selection.selected != selected:
            raise EncyclopediaHold('Restart Manager to admit this saved workflow app.')
        selection.validate()
        return selection

    def available(self, component_id):
        workflow_component(component_id)
        return type(getattr(self.session, 'workflow_selections', {}).get(component_id)) is RootWorkflowSelection

    def launch(self, selected):
        from suite_core.manager.bootstrap_entry import EntryHelperAdapter
        from suite_core.workflow_modules.owner_route import WorkflowControlActor
        with self._lock:
            self.assert_idle()
            self.reader_owner.assert_idle()
            selection = self.selection_for(selected)
            self.last_health = self.error = self.return_target = None
            self._visible_seen = False
            self._stop = Event()
            # Retain before the first fallible reservation/native operation.
            native = self.factory()
            self.native, self.selection = native, selection
            try:
                adapter = EntryHelperAdapter.for_workflow(self.session, native, selection)
                native.bind_helper_capability(adapter)
                from core.workflow_native_channel import WorkflowImageScope
                # Retain before reserve I/O: an uncertain grant/ACK must not
                # discard the original Manager/Root image handles.
                self.root_image_scope = WorkflowImageScope(self.session.wire.channel,
                    selected.component_id, custodian=native,
                    owner_generation=self.session.binding.owner_generation)
                control = native.bind_workflow_control(selected.component_id)
                environment = {key: value for key, value in _organizer_environment().items()
                    if not key.upper().startswith(('COLLECTIGEN_CONTROL_', 'COLLECTIGEN_LAUNCHER_QUERY'))}
                self.child = native.spawn_command(selection.command, selected.directory, environment,
                                                  helper_context=selection.context())
                self.identity = native.identity(self.child, selection.program)
                self._launch_draft = self.pending_generate_draft if selected.component_id == 'collectigen.generate' else None
                self.actor = WorkflowControlActor(control, selection, initial_draft=self._launch_draft)
                self.worker = Thread(target=self._pump, name='collectigen-workflow-control', daemon=True)
                self.worker.start()
                self.reason = 'Opening the workflow. Shared model and settings edits stay held until it closes safely.'
            except BaseException as error:
                self.error = error
                self.reason = 'Workflow startup needs attention. The original process and workspace hold are retained.'
                raise

    def _pump(self):
        try:
            self.actor.start()
            while not self._stop.is_set() and not self.actor.waiting_for_native_drain:
                self.actor.handle_next(timeout=0.25)
        except BaseException as error:
            self._record_actor_failure(error)
            self.error = error
            self.reason = 'Workflow connection needs attention. Close the app normally; original cleanup remains owned.'

    def _record_actor_failure(self, error):
        """Retain the first actor snapshot through physical cleanup and relaunch."""
        try:
            snapshot = self.actor.first_failure
            if snapshot is None:
                return
            from suite_core.diagnostics import Diagnostics, Component
            if self.diagnostics is None:
                profile = getattr(self.session, 'update_profile', {})
                self.diagnostics = Diagnostics(self.selection.workspace_root,
                    component=Component.MANAGER, build=profile.get('current_version', 'unknown'))
            correlation = self.diagnostics.failure(error, workflow=snapshot)
            if correlation is not None:
                self.failure_reference = correlation
        except Exception:
            # Diagnostics never changes admission, failure state or drain authority.
            pass

    def poll(self):
        with self._lock:
            native, child, actor = self.native, self.child, self.actor
            if native is None:
                return True
            # An uncertain entered birth cannot be proved absent from UI state.
            if child is None:
                return False
            native.check_identity(child, self.identity, self.selection.program)
            if child.poll() is None and not self._visible_seen:
                from core.encyclopedia_connection import _visible_reader_window
                visible = _visible_reader_window(child.pid)
                native.check_identity(child, self.identity, self.selection.program)
                self._visible_seen = visible and child.poll() is None
            if child.poll() is None or native.job.active() or not native.drained.is_set():
                if actor is not None and actor.state == 'ready':
                    if self._launch_draft is not None and self.pending_generate_draft == self._launch_draft:
                        self.pending_generate_draft = self._launch_draft = None
                    self.reason = 'The workflow has started. Its window may take a moment to appear. Keep Manager open; close the workflow normally when finished.'
                return False
            self._stop.set()
            worker = self.worker
            if worker is not None and worker.is_alive():
                # Bounded control reads let the retained pump finish without a
                # GUI thread wait, forced termination or lost owner reference.
                return False
            code = child.poll()
            native.close(child)
            if actor is not None:
                actor.close_after_native()
                self.return_target = actor.requested_return
                if actor.requested_resume_draft is not None:
                    self.pending_generate_draft = dict(actor.requested_resume_draft)
                if code == 0 and self._visible_seen and actor.local_drain_claimed and actor.error is None and self.error is None:
                    self.last_health = self.selection
            # native.close has completed process/Job/output/control cleanup and
            # the original helper-drained ACK; actor bookkeeping is also settled.
            if self.root_image_scope is not None:
                self.root_image_scope.close()
                self.root_image_scope = None
            self.native = self.child = self.identity = self.selection = self.actor = self.worker = None
            self.reason = f'Workflow closed (exit {code}); original process, Job and output are drained.'
            return True

    def native_health(self, selected):
        self.poll()
        if self.close_held or self.last_health is None or self.last_health.selected != selected:
            return False
        return self.selection_for(selected) is self.last_health

    def begin_close(self):
        return self.poll() and not self.close_held

    def cancel_close(self):
        return None

    def finish_close(self):
        return self.begin_close()
