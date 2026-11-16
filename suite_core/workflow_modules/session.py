"""Child-only workflow session over the original native control channel.

Root must create/retain the channel and exclusive workspace grant. This module
neither creates IPC nor impersonates NormalEntrySession. A drain ACK records a
claim only: original Root native/process/job/output settlement releases leases.
"""
from hashlib import sha256
import math
import re

from suite_core.manager.bootstrap_entry import (
    _NormalWire, _binding_document, _wire_bytes, decode_control_frame,
)
from suite_core.workflow_modules.contracts import WorkflowWorkspaceOffer
from suite_core.workflow_modules.entry_binding import ModuleEntryBinding, require

MESSAGE_KINDS = frozenset({
    'workflow-offer', 'workflow-ready', 'workflow-ready-ack',
    'workflow-check', 'workflow-check-ack', 'workflow-closing', 'workflow-closing-ack',
    'workflow-drained-report', 'workflow-drained-ack',
    'workflow-return', 'workflow-return-ack',
})
RETURN_TARGETS = frozenset({'applications', 'connections', 'models', 'settings', 'description-settings', 'video-settings', 'video-recovery', 'trainer-settings'})
_SESSION_TOKEN = object()


def _identity(value):
    return sha256(_wire_bytes(value)).hexdigest()


def _lifetime(value):
    require(type(value) is str and re.fullmatch('[0-9a-f]{32}', value),
            'independent_workflow_lifetime_pin_required')
    return value


class ModuleEntrySession:
    """Strongly retains admission, channel and cleanup owners through failure."""
    @classmethod
    def accept(cls, real_channel, admitted_binding, *, expected_owner_generation=None,
               expected_attempt_id=None, expected_grant_id=None, timeout=1):
        from core.manager_launch_owner import WindowsControlChannel
        require(type(real_channel) is WindowsControlChannel and real_channel.transport is None,
                'original_child_native_channel_required')
        require(type(admitted_binding) is ModuleEntryBinding, 'typed_module_entry_binding_required')
        require(type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 30,
                'bounded_workflow_channel_timeout')
        # Explicit expected-lifetime callers retain exact comparison semantics.
        # The real entry can accept an authenticated parent grant; missing peer
        # or publisher pins still fail in admitted_binding.validate().
        lifetime_pins = (expected_owner_generation, expected_attempt_id, expected_grant_id)
        require(all(value is None for value in lifetime_pins) or all(value is not None for value in lifetime_pins),
                'complete_expected_workflow_lifetime_required')
        if expected_owner_generation is not None:
            expected_owner_generation, expected_attempt_id, expected_grant_id = map(_lifetime, lifetime_pins)
        program_pin = admitted_binding.validate()
        frame = decode_control_frame(real_channel.read_frame(timeout=timeout), message_kinds=MESSAGE_KINDS)
        require(frame['kind'] == 'workflow-offer' and frame['sequence'] == 1
                and {'binding', 'workspace'} <= set(frame['payload'])
                <= {'binding', 'workspace', 'worker_baseline', 'initial_draft'},
                'initial_workflow_offer_required')
        document = frame['payload']['binding']
        _binding_document(document)
        proof = real_channel.qualify_binding(document, admitted_binding,
            expected_peer_image_sha256=admitted_binding.expected_peer_image_sha256)
        require(frame['binding_sha256'] == proof.binding_sha256, 'workflow_offer_binding_digest_mismatch')
        offer = WorkflowWorkspaceOffer.from_mapping(frame['payload']['workspace'])
        if expected_owner_generation is None:
            # Only after signed own-package review AND the independent native
            # Manager peer proof above may this original peer issue its grant.
            expected_owner_generation = offer.owner_generation
            expected_attempt_id, expected_grant_id = offer.attempt_id, offer.grant_id
        selected = admitted_binding.selected
        require(offer.owner_generation == document['owner_generation'] == expected_owner_generation
                and offer.channel_epoch == document['channel_epoch'] == real_channel.epoch
                and offer.attempt_id == expected_attempt_id and offer.grant_id == expected_grant_id,
                'original_workflow_grant_required')
        require(offer.component_id == selected.component_id and offer.software_version == selected.version
                and offer.package_root == selected.directory
                and offer.inventory_sha256 == selected.inventory_sha256
                and offer.manifest_sha256 == selected.manifest_sha256
                and offer.code_root == admitted_binding.layout.code_root
                and offer.data_root == admitted_binding.layout.data_root
                and offer.workspace_root == admitted_binding.workspace_root
                and offer.program == admitted_binding.executable,
                'signed_workflow_workspace_binding_changed')
        require(document['entry']['executable_sha256'] == program_pin
                and offer.native_identity_sha256 == _identity(document['entry']),
                'original_workflow_child_birth_required')
        session = cls(_SESSION_TOKEN, admitted_binding, offer,
                      _NormalWire(real_channel, document, proof, timeout=timeout, message_kinds=MESSAGE_KINDS))
        if 'initial_draft' in frame['payload']:
            from core.generation_module_handoff import draft_descriptor
            require(offer.component_id == 'collectigen.generate', 'generate_handoff_component_required')
            session.initial_draft = draft_descriptor(frame['payload']['initial_draft'])
        if 'worker_baseline' in frame['payload']:
            from suite_core.manager.bootstrap_entry import check_training_baseline_offer, ProcessBinding
            baseline = frame['payload']['worker_baseline']
            require(type(baseline) is dict and type(baseline.get('version')) is str,
                    'bounded_workflow_worker_baseline_required')
            session.worker_baseline = check_training_baseline_offer(baseline,
                {'current_version': baseline['version'], 'channel': 'beta',
                 'trust_sha256': admitted_binding.expected_trust_sha256},
                ProcessBinding(**document['launcher']))
        session.wire.incoming = 1
        try:
            session._exchange('workflow-ready', {}, 'workflow-ready-ack', {'state': 'ready'})
            session.state = 'ready'
        except BaseException as error:
            session._hold(error)
            # Caller retains the original channel/native owner even if no session returns.
            raise
        return session

    def __init__(self, token, binding, offer, wire):
        require(token is _SESSION_TOKEN, 'native_workflow_accept_required')
        self.admitted_binding, self.offer, self.wire = binding, offer, wire
        self.state, self.error = 'offered', None
        self._cleanup_owners = []
        self.local_drain_reported = False
        self.worker_baseline = None
        self.initial_draft = None

    @property
    def component_id(self):
        return self.offer.component_id

    @property
    def workspace_root(self):
        # This property conveys location, never a replacement for assert_ready().
        return self.offer.workspace_root

    @property
    def lease_release_authorized(self):
        return False

    def _hold(self, error):
        self.state, self.error = 'held', error

    def _peer(self):
        channel = self.wire.channel
        require(channel.read_fault is None and channel.write_fault is None
                and channel.epoch == self.offer.channel_epoch
                and channel.api.WaitForSingleObject(channel.peer_handle, 0) == 258
                and channel.local_witness() == self.wire.document['entry']
                and channel.peer_witness() == self.wire.document['launcher'],
                'original_workflow_peer_lost_or_changed')

    def _scope(self):
        return {'component_id': self.component_id, 'attempt_id': self.offer.attempt_id,
                'grant_id': self.offer.grant_id}

    def _exchange(self, kind, payload, reply_kind, reply):
        with self.wire.lock:
            try:
                self._peer()
                self.wire.send(kind, {**self._scope(), **payload})
                frame = self.wire.receive(reply_kind)
                self._peer()
                require(frame['payload'] == {**self._scope(), **reply}, 'exact_workflow_grant_reply_required')
                return frame['payload']
            except BaseException as error:
                self._hold(error)
                raise

    def assert_ready(self):
        """Call at workflow admission/checkpoints; peer loss forbids new work."""
        with self.wire.lock:
            require(self.state == 'ready', 'workflow_session_not_ready')
            self._exchange('workflow-check', {}, 'workflow-check-ack', {'state': 'ready'})
        return self

    def retain_cleanup_owner(self, owner, *, is_drained):
        """Retain before physical work begins; never remove on a lost peer."""
        with self.wire.lock:
            self.assert_ready()
            require(owner is not None and callable(is_drained)
                    and all(item[0] is not owner for item in self._cleanup_owners),
                    'distinct_workflow_cleanup_owner_required')
            self._cleanup_owners.append((owner, is_drained))

    def begin_close(self):
        """Latch no-new-work before messaging. Caller starts original owner cleanup."""
        with self.wire.lock:
            if self.state in ('closing', 'drain-reported'):
                return
            if self.state == 'held':
                # Keep peer-failure evidence and every original local owner retained.
                return
            require(self.state == 'ready', 'workflow_close_state_required')
            self.state = 'closing'
            self._exchange('workflow-closing', {}, 'workflow-closing-ack', {'state': 'closing'})

    def report_local_drained(self):
        """A claimed local drain cannot release Root's native/workspace ownership."""
        with self.wire.lock:
            require(self.state == 'closing', 'workflow_closing_required')
            require(all(check() is True for _owner, check in self._cleanup_owners),
                    'workflow_original_cleanup_pending')
            self._exchange('workflow-drained-report', {'local_drained': True},
                           'workflow-drained-ack', {'state': 'recorded'})
            self.local_drain_reported = True
            self.state = 'drain-reported'
            # Keep channel and owners: Root still observes actual exit/drain.

    def request_return(self, target, *, resume_draft=None):
        """Request finite Manager navigation; grant no Manager mutation permission."""
        require(type(target) is str and target in RETURN_TARGETS, 'finite_workflow_return_target_required')
        with self.wire.lock:
            self.assert_ready()
            payload = {'target': target}
            if resume_draft is not None:
                from core.generation_module_handoff import draft_descriptor
                require(self.component_id == 'collectigen.generate', 'generate_handoff_component_required')
                payload['resume_draft'] = draft_descriptor(resume_draft)
            return self._exchange('workflow-return', payload,
                                  'workflow-return-ack', {**payload, 'state': 'recorded'})
