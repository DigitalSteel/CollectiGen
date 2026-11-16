"""Manager-side actor for one original Root-reserved workflow helper.

The helper reservation is the exclusive workspace grant identifier. Root must
serialize those finite workflow reservations; this actor cannot create, release,
or reconstruct a lease. Local drain is a claim, never physical settlement.
"""
from dataclasses import dataclass
from hashlib import sha256
import math
from pathlib import Path

from core.workflow_native_channel import WorkflowNativeChannel, require
from core.workflow_root_selection import RootWorkflowSelection
from suite_core.manager.bootstrap_entry import EntryHelperAdapter, _NormalWire, _wire_bytes
from suite_core.workflow_modules.contracts import WorkflowWorkspaceOffer
from suite_core.workflow_modules.session import MESSAGE_KINDS, RETURN_TARGETS


@dataclass(frozen=True)
class _ModuleChannelBinding:
    """Stock wire comparison inputs derived only from an original selection."""
    layout: object
    settings_path: Path
    restart_arguments: tuple[str, ...]


class WorkflowControlActor:
    def __init__(self, control, selection, *, timeout=1, initial_draft=None):
        require(type(control) is WorkflowNativeChannel and type(selection) is RootWorkflowSelection,
                'original_typed_workflow_route_required')
        require(type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 30,
                'bounded_workflow_owner_timeout')
        self.control, self.selection, self.native = control, selection, control.native
        self.cap = getattr(self.native, 'helper_capability', None)
        require(type(self.cap) is EntryHelperAdapter, 'original_workflow_entry_adapter_required')
        self.session = self.cap.session
        self.attempt, self.reservation = self.native.helper_attempt, self.native.helper_reservation
        self.child = control.child
        self.state, self.error = 'prepared', None
        self.phase, self.first_failure = 'startup', None
        self.local_drain_claimed = False
        self.requested_return = None
        self.requested_resume_draft = None
        self.accepting_work = True
        self.wire = None
        self.initial_draft = None
        if initial_draft is not None:
            from core.generation_module_handoff import draft_descriptor
            require(selection.component_id == 'collectigen.generate', 'generate_handoff_component_required')
            self.initial_draft = draft_descriptor(initial_draft)
        self.original_slot = self._original()
        self.job = self.child.job
        context, program_pin = selection.helper_snapshot()
        require(context == dict(self.cap.reviewed) and program_pin == self.cap.program_pin,
                'root_workflow_review_changed')
        self.admitted = _ModuleChannelBinding(self.session.admitted.layout,
            selection.workspace_root / 'generation-settings.json', selection.command[1:])
        channel = control.channel
        local, peer = channel.local_witness(), channel.peer_witness()
        import json
        document = dict(protocol=1, owner_generation=self.session.binding.owner_generation,
            channel_epoch=channel.epoch, deployment_root=str(selection.root), data_root=str(selection.data_root),
            settings_path=str(self.admitted.settings_path), mode='portable',
            argv_sha256=sha256(json.dumps(self.admitted.restart_arguments,
                ensure_ascii=False, separators=(',', ':')).encode()).hexdigest(), entry=peer, launcher=local)
        proof = control.parent_proof(document, self.admitted, expected_child_image_sha256=program_pin)
        selected = selection.selected
        self.offer = WorkflowWorkspaceOffer(1, 1, selection.component_id, selected.version,
            selection.root, selection.data_root, selected.directory, selection.workspace_root,
            selected.inventory_sha256, selected.manifest_sha256, document['owner_generation'],
            channel.epoch, self.attempt.identity, self.reservation, sha256(_wire_bytes(peer)).hexdigest(),
            'exclusive-writer')
        self.wire = _NormalWire(channel, document, proof, timeout=timeout, message_kinds=MESSAGE_KINDS)

    @property
    def grant_release_authorized(self):
        return False

    def _original(self):
        require(self.control.native is self.native and self.native.workflow_control is self.control
                and self.cap is self.native.helper_capability
                and self.selection is getattr(self.cap, 'workflow_selection', None)
                and self.selection is getattr(self.session, 'workflow_selections', {}).get(self.selection.component_id)
                and self.control.component_id == self.selection.component_id
                and self.cap.role_base == self.selection.helper_role
                and self.native.helper_attempt is self.attempt
                and self.native.helper_reservation == self.reservation
                and self.control.child is self.child and self.child is not None
                and self.child in self.native.logs and self.child.job is self.native.job
                and self.child._handle == self.control.channel.peer_handle
                and not self.native.start_failed and not self.control.closed,
                'original_retained_workflow_route_changed')
        slot = self.cap._slot(self.reservation)
        borrowed = slot.get('borrowed')
        require(slot.get('phase') == 'registered' and slot.get('attempt') is self.attempt
                and borrowed is not None and borrowed._handle == self.child._handle
                and borrowed.pid == self.child.pid
                and self.selection._context() == dict(self.cap.reviewed),
                'original_registered_workflow_attempt_required')
        self.session._peer()
        root_channel = self.session.wire.channel
        require(root_channel.read_fault is None and root_channel.write_fault is None
                and root_channel.api.WaitForSingleObject(root_channel.peer_handle, 0) == 258,
                'original_workflow_root_peer_lost')
        return slot

    def _peer(self):
        self._original()
        channel = self.control.channel
        require(channel.read_fault is None and channel.write_fault is None
                and channel.api.WaitForSingleObject(channel.peer_handle, 0) == 258,
                'original_workflow_child_peer_lost')
        if self.wire is not None:
            require(channel.local_witness() == self.wire.document['launcher']
                    and channel.peer_witness() == self.wire.document['entry']
                    and channel.epoch == self.offer.channel_epoch,
                    'original_workflow_native_identity_changed')

    def _hold(self, error):
        try:
            self._retain_failure(error)
        except Exception:
            pass  # Diagnostic observation cannot mask the original hold.
        self.accepting_work = False
        self.state, self.error = 'held', error

    def _retain_failure(self, error):
        if self.first_failure is None:
            from suite_core.diagnostics import WORKFLOW_FAILURE_CODES, valid_workflow_failure
            # Map the error to a developer-owned token. Never retain its message.
            try:
                code = str(error)
            except Exception:
                code = 'other'
            channel = self.control.channel
            snapshot = dict(phase=self.phase, state=self.state,
                error=code if code in WORKFLOW_FAILURE_CODES else 'other',
                incoming=self.wire.incoming, outgoing=self.wire.outgoing,
                read_bytes=getattr(channel, '_read_progress', 0),
                pending_frame=self.wire.received_pending is not None,
                read_fault=channel.read_fault is not None, write_fault=channel.write_fault is not None,
                local_drain=self.local_drain_claimed)
            if valid_workflow_failure(snapshot):
                self.first_failure = snapshot

    def _scope(self):
        return dict(component_id=self.offer.component_id, attempt_id=self.offer.attempt_id,
                    grant_id=self.offer.grant_id)

    def start(self):
        with self.wire.lock:
            require(self.state == 'prepared', 'single_workflow_offer_required')
            try:
                self.phase = 'offer'
                self._peer()
                require(self.session.state == 'ready', 'original_manager_not_ready_for_workflow')
                self.state = 'offer-sending'
                payload = {'binding': self.wire.document, 'workspace': self.offer.mapping()}
                if self.initial_draft is not None:
                    payload['initial_draft'] = dict(self.initial_draft)
                baseline = getattr(self.session, 'training_baseline', None)
                if baseline is not None:
                    from suite_core.manager.bootstrap_entry import check_training_baseline_offer
                    payload['worker_baseline'] = check_training_baseline_offer(
                        baseline, self.session.update_profile, self.session.binding.entry)
                self.wire.send('workflow-offer', payload)
                self.state = 'offered'
            except BaseException as error:
                self._hold(error)
                raise
        return self

    def begin_close(self):
        """Latch no new work; do not terminate UI or release original owners."""
        with self.wire.lock:
            self.accepting_work = False
            if self.state in ('offered', 'ready'):
                self.state = 'closing'

    def _dispatch(self, frame):
        kind, payload = frame['kind'], frame['payload']
        scope = self._scope()
        extras = ({'local_drained'} if kind == 'workflow-drained-report'
                  else {'target'} if kind == 'workflow-return' else set())
        if kind == 'workflow-return' and 'resume_draft' in payload:
            extras.add('resume_draft')
        require(set(payload) == set(scope) | extras and all(payload[key] == value for key, value in scope.items()),
                'exact_original_workflow_request_required')
        if kind == 'workflow-ready':
            require(self.state in ('offered', 'closing'), 'single_workflow_ready_required')
            ready = self.accepting_work and self.session.state == 'ready' and self.state == 'offered'
            self.state = 'ready' if ready else 'closing'
            self.wire.send('workflow-ready-ack', {**scope, 'state': self.state})
        elif kind == 'workflow-check':
            require(self.state in ('ready', 'closing'), 'workflow_check_state_required')
            ready = self.accepting_work and self.session.state == 'ready' and self.state == 'ready'
            if not ready:
                self.accepting_work, self.state = False, 'closing'
            self.wire.send('workflow-check-ack', {**scope, 'state': 'ready' if ready else 'closing'})
        elif kind == 'workflow-closing':
            require(self.state in ('ready', 'closing'), 'workflow_close_state_required')
            self.accepting_work, self.state = False, 'closing'
            self.wire.send('workflow-closing-ack', {**scope, 'state': 'closing'})
        elif kind == 'workflow-drained-report':
            require(self.state == 'closing' and payload['local_drained'] is True,
                    'workflow_local_drain_claim_required')
            self.accepting_work, self.local_drain_claimed = False, True
            self.state = 'drain-reported'
            self.wire.send('workflow-drained-ack', {**scope, 'state': 'recorded'})
        elif kind == 'workflow-return':
            require(self.state == 'ready' and self.accepting_work and self.session.state == 'ready'
                    and type(payload['target']) is str and payload['target'] in RETURN_TARGETS,
                    'finite_ready_workflow_return_required')
            self.requested_return = payload['target']
            reply = {**scope, 'target': payload['target'], 'state': 'recorded'}
            if 'resume_draft' in payload:
                from core.generation_module_handoff import draft_descriptor
                require(self.selection.component_id == 'collectigen.generate', 'generate_handoff_component_required')
                self.requested_resume_draft = draft_descriptor(payload['resume_draft'])
                reply['resume_draft'] = dict(self.requested_resume_draft)
            self.wire.send('workflow-return-ack', reply)
        else:
            raise RuntimeError('workflow_child_request_kind_required')

    def poll(self, *, timeout=None):
        """Service at most one request. Intact idle is quiet; malformed I/O holds."""
        from core.manager_launch_owner import LaunchHold
        with self.wire.lock:
            if self.state == 'awaiting-native-drain':
                return False
            require(self.state in ('offered', 'ready', 'closing', 'drain-reported'),
                    'workflow_owner_not_pollable')
            try:
                self.phase = 'peer'
                self._original()
                # Expected physical exit after the acknowledged local claim is
                # a handoff to native drain, never EOF-as-success or lease release.
                if (self.state == 'drain-reported' and self.local_drain_claimed
                        and self.control.channel.api.WaitForSingleObject(self.child._handle, 0) == 0
                        and self.child.poll() is not None):
                    self.state = 'awaiting-native-drain'
                    return False
                self._peer()
                try:
                    self.phase = 'receive'
                    frame = self.wire.receive(idle_timeout=timeout)
                except LaunchHold as error:
                    channel = self.control.channel
                    if (str(error) == 'control_channel_idle_timeout'
                            and channel.read_fault is None and channel.write_fault is None):
                        return False
                    raise
                self._peer()
                self.phase = 'dispatch'
                self._dispatch(frame)
                return True
            except BaseException as error:
                if (str(error) in {'control_channel_eof', 'control_channel_eof_or_unavailable',
                                   'original_workflow_child_peer_lost'}
                        and self.state == 'drain-reported' and self.local_drain_claimed
                        and self.control.channel.api.WaitForSingleObject(self.child._handle, 0) == 0
                        and self.child.poll() is not None):
                    # Child may exit between the live check and pipe read after
                    # its drain ACK. Retain the EOF evidence and require actual
                    # native settlement; this is never EOF alone as success.
                    self.state = 'awaiting-native-drain'
                    return False
                self._hold(error)
                raise

    def handle_next(self, *, timeout=1):
        return self.poll(timeout=timeout)

    @property
    def waiting_for_native_drain(self):
        return self.state == 'awaiting-native-drain'

    @property
    def is_drained(self):
        return self.state == 'native-drained'

    def close_after_native(self):
        """Observe completed original native.close; release no lease/handle here.

        OwnedWorkflow must stop/join its retained pump before endpoint closure.
        Protocol failure can physically drain, but remains in error and does not
        become a successful application/health result.
        """
        with self.wire.lock:
            slot = self.cap.attempts.get(self.attempt.identity)
            require(self.native.workflow_control is self.control and self.control.closed
                    and self.control.child is self.child and self.native.job is self.job
                    and self.child not in self.native.logs and self.child._handle is None
                    and self.job.handle is None and self.job.process is None
                    and self.native.drained.is_set()
                    and (self.native.drain is None or not self.native.drain.is_alive())
                    and self.native.helper_reservation is None and not self.native.helper_accounting_held
                    and slot is self.original_slot and slot.get('phase') == 'drained',
                    'original_workflow_native_settlement_required')
            self.accepting_work = False
            self.state = 'native-drained'
            return True
