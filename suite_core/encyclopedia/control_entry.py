"""Encyclopedia-only RPC over stock original inherited native handles.

No injected native-proof/provider parameter, new pipe protocol or owner. Native
qualification happens at both entries before application command dispatch.
"""
from threading import Event
from hashlib import sha256
import uuid
import time
import math

from core.manager_launch_owner import (WindowsNormalTransport, WindowsControlChannel, LaunchHold,
    _native_witness, _bytes)
from release_contract import sha256_file
from suite_core.discovery.component_staging import require
from suite_core.manager.bootstrap_entry import _NormalWire, _binding_document, digest_argv, decode_control_frame
from .entry_binding import reader_binding, child_binding

KINDS = frozenset({'offer', 'ready', 'encyclopedia-request', 'encyclopedia-reply', 'hold'})
OPS = frozenset({'check', 'confirm', 'cancel', 'status', 'retry-connection', 'recover', 'close'})


class _ControlChild:
    """Original child facade remembers only root-issued physical terminal state."""
    def __init__(self, actor):
        self.actor = actor
        self.pid = actor.process.pid

    def poll(self):
        terminal = self.actor.owner._standalone_control_terminal
        if terminal is not None and terminal.actor is self.actor:
            self.actor.owner._standalone_encyclopedia_owned(self.actor.subject)
            require(terminal.payload['children']['encyclopedia'] is self.actor.process,
                    'original_terminal_process_changed')
            return terminal.payload['exit_code']
        return self.actor.process.poll()


class StandaloneControlActor:
    """Canonical reader facade backed by stock NUL-output/control-pipe owner."""
    def __init__(self, owner, subject):
        self.owner, self.subject = owner, subject
        self.transport = self.channel = self.admitted = self.process = None
        self.drained = Event()
        self.drain = None
        self.stdout = None
        self.job = self
        self.session = None
        self.child = None

    def spawn_command(self, command, cwd, environment, **kwargs):
        self.owner._standalone_encyclopedia_owned(self.subject)
        self.admitted = reader_binding(self.subject.binding)
        require(command == (str(self.admitted.selected.program),) and cwd == self.admitted.selected.program.parent,
                'original_selected_reader_command_required')
        self.process, self.channel = WindowsNormalTransport.spawn_standalone(self.owner, self.subject,
            self.admitted.command, environment=environment)
        # The stock creation path uses NUL for all standard streams; no stdout
        # worker exists. Control endpoints have separate exact terminal cleanup.
        self.drained.set()
        self.child = _ControlChild(self)
        return self.child

    def identity(self, child, program):
        require(child is self.child and program == self.admitted.selected.program, 'original_control_reader_required')
        terminal = self.owner._standalone_control_terminal
        if terminal is not None and terminal.actor is self:
            self.owner._standalone_encyclopedia_owned(self.subject)
            return terminal.payload['proof'].identity()
        return self.owner._bound_native_witness('encyclopedia').identity()

    def check_identity(self, child, identity, program):
        require(child is self.child and self.owner.children.get('encyclopedia') is self.process
                and program == self.admitted.selected.program and self.identity(child, program) == identity,
                'original_control_reader_changed')
        if child.poll() is None:
            require(_native_witness(self.transport.api, self.process._handle) == identity
                    and self.transport.includes(self.process), 'live_original_control_peer_changed')

    def active(self):
        return self.transport.job_active_count()

    def poll(self):
        return self.process.poll() if self.process is not None else None

    def close(self, child=None):
        require(child is self.child and child is not None, 'original_control_child_cleanup_required')
        self.owner.retire_standalone_control_peer(self.subject, self, self.process)

    def terminate_owned(self, child):
        # A separately reviewed native termination adapter is not present in this
        # leaf path. Normal Close and retained failure/recovery stay available.
        raise LaunchHold('standalone_control_explicit_stop_unqualified')


def binding_document(channel, admitted, generation):
    document = {'protocol': 1, 'owner_generation': generation, 'channel_epoch': channel.epoch,
        'deployment_root': str(admitted.layout.code_root), 'data_root': str(admitted.layout.data_root),
        'settings_path': str(admitted.settings_path), 'mode': 'portable',
        'argv_sha256': digest_argv(admitted.restart_arguments), 'entry': channel.peer_witness(),
        'launcher': channel.local_witness()}
    _binding_document(document)
    return document


def check_request(payload):
    require(type(payload) is dict and set(payload) == {'id', 'operation', 'body'}
            and type(payload['id']) is str and len(payload['id']) == 32
            and all(c in '0123456789abcdef' for c in payload['id'])
            and type(payload['operation']) is str and payload['operation'] in OPS and type(payload['body']) is dict,
            'closed_encyclopedia_request_required')
    expected = {'manifest_sha256'} if payload['operation'] == 'confirm' else {'request_sha256'} if payload['operation'] in (
        'cancel', 'retry-connection', 'recover') else set()
    require(set(payload['body']) == expected, 'exact_encyclopedia_operation_body_required')
    for value in payload['body'].values():
        require(type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value),
                'exact_encyclopedia_operation_pin_required')
    return payload


class EncyclopediaRootControl:
    def __init__(self, backend):
        actor = backend.owner._standalone_control_actor
        require(type(actor) is StandaloneControlActor and actor.subject is backend.subject
                and actor.channel is not None and actor.transport is backend.owner.native_transport,
                'actual_original_control_actor_required')
        self.backend, self.actor = backend, actor
        self.document = binding_document(actor.channel, actor.admitted, backend.owner.record['owner'])
        proof = actor.channel.qualify_binding(self.document, actor.admitted,
            expected_peer_image_sha256=sha256_file(actor.admitted.selected.program))
        self.wire = _NormalWire(actor.channel, self.document, proof, message_kinds=KINDS)
        self.seen = set()
        self.ready = self.close_intent = False
        self.after_close_operation = 'apply'
        self.attempt_generation = backend.owner.record['standalone_encyclopedia']['attempt']['generation']
        actor.session = self
        self.wire.send('offer', {'binding': self.document, 'context_sha256': actor.admitted.context_sha256,
            'inventory_sha256': actor.admitted.selected.inventory_sha256})

    def _live(self):
        self.backend.owner._standalone_encyclopedia_owned(self.backend.subject)
        require(self.actor is self.backend.owner._standalone_control_actor and self.actor.process.poll() is None
                and self.actor.channel.peer_witness() == self.document['entry']
                and self.actor.channel.local_witness() == self.document['launcher'], 'original_control_peer_changed')

    def accept_ready(self, *, overall_timeout=30):
        require(type(overall_timeout) in (int,float) and math.isfinite(overall_timeout)
                and 0 < overall_timeout <= 120, 'bounded_original_ready_wait_required')
        require(not self.ready, 'original_control_ready_already_accepted')
        deadline = time.monotonic() + overall_timeout
        while True:
            self._live()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LaunchHold('original_control_ready_deadline')
            try:
                frame = self.wire.receive('ready', timeout=min(remaining,30))
                break
            except LaunchHold as error:
                if (type(error) is not LaunchHold or str(error) != 'control_channel_idle_timeout'
                        or self.actor.channel.read_fault is not None
                        or getattr(self.actor.channel,'write_fault',None) is not None):
                    raise
                # Stock zero-byte idle is not a broken/partial frame. Keep the
                # original offer, sequence, peer and wire; never resend READY.
        self._live()
        require(frame['payload'] == {'context_sha256': self.actor.admitted.context_sha256}, 'original_control_context_changed')
        self.ready = True

    def handle_next(self):
        """Run on the existing retained executor; network work never belongs to Qt."""
        self._live()
        require(self.ready, 'original_control_ready_required')
        payload = self.wire.receive('encyclopedia-request')['payload']
        require(type(payload) is dict and type(payload.get('id')) is str and len(payload['id']) == 32
                and all(c in '0123456789abcdef' for c in payload['id']), 'correlatable_control_request_required')
        try:
            request = check_request(payload)
        except ValueError:
            result = {'status': 'held', 'reason': 'unsupported_operation_or_body'}
            self._live()
            self.wire.send('encyclopedia-reply', {'id': payload['id'], 'result': result})
            return result
        require(request['id'] not in self.seen and len(self.seen) < 256, 'replayed_or_exhausted_control_request')
        self.seen.add(request['id'])
        try:
            result = self._dispatch(request)
        except (ValueError, LaunchHold, OSError, InterruptedError):
            result = {'status': 'held', 'reason': 'operation_held', 'operation': request['operation']}
        self._live()
        self.wire.send('encyclopedia-reply', {'id': request['id'], 'result': result})
        return result

    def _dispatch(self, request):
        operation, body = request['operation'], request['body']
        backend = self.backend
        if operation == 'check':
            notice = backend.update_check.check()
            result = {'status': notice.status, 'reason': notice.reason}
            if notice.offer is not None:
                notes = notice.offer.release_notes
                require(len(notes.encode('utf-8')) <= 32768, 'release_notes_exceed_control_profile')
                result.update(version=notice.offer.release.software_version, manifest_sha256=notice.offer.release.manifest_sha256,
                    package_sha256=notice.offer.release.artifact.sha256, notes=notes,
                    route='standalone' if notice.route == 'manual_package_review' else notice.route)
        elif operation == 'confirm':
            offer = backend.acquisition._offers.get(body['manifest_sha256'])
            require(offer is not None, 'current_root_retained_offer_required')
            confirmed = backend.confirm_update(offer, confirmed=True)
            result = {'status': 'waiting_for_close', 'request_sha256': sha256(_bytes(confirmed.identity)).hexdigest()}
        elif operation in ('cancel', 'retry-connection', 'recover'):
            original = backend.request
            require(original is not None and sha256(_bytes(original.identity)).hexdigest() == body['request_sha256'],
                    'original_root_retained_request_required')
            if operation == 'cancel':
                backend.cancel_update(original); result = {'status': 'cancelled_before_apply'}
            elif operation == 'retry-connection':
                saved = backend.retry_connection(original); result = {'status': 'saved' if saved.connection_saved else 'save_pending'}
            else:
                require(backend.owner.record.get('standalone_update', {}).get('phase') == 'applying',
                        'original_target_recovery_operation_required')
                self.after_close_operation = 'recover'
                result = {'status': 'recovery_waiting_for_close'}
        elif operation == 'close':
            # The GUI remains visible while awaiting this authenticated reply.
            # Capture stock original-peer visibility before ACK permits hiding;
            # normal exit/Job drain still produce the separate health receipt.
            require(backend.poll_reader() is False, 'original_reader_exited_before_close_ack')
            require(backend.subject.reader._visible_seen is True, 'original_reader_visible_evidence_required')
            self.close_intent = True; result = {'status': 'close_requested'}
        elif operation == 'status':
            original = backend.request
            phase = backend.owner.record.get('standalone_update', {}).get('phase')
            require((original is None and phase is None) or (original is not None
                and phase in ('waiting_for_close','applying','recovering')), 'original_status_request_held')
            result = {'status': 'waiting' if backend.close_held else 'idle', 'phase': phase,
                'current_version': backend.binding.current().selected.version,
                'request_sha256': None if original is None else sha256(_bytes(original.identity)).hexdigest()}
        else:
            raise ValueError('unknown_original_control_operation')
        return result

    def after_reader_close(self):
        require(self.close_intent and self.actor.channel.read_fault is None, 'authenticated_normal_control_close_required')
        attempt = self.backend.owner.record['standalone_encyclopedia']['attempt']
        require(attempt['generation'] == self.attempt_generation
                and attempt['selected'] == self.actor.admitted.selected.mapping(),
                'exact_original_control_reader_attempt_required')
        require(self.backend.poll_reader(), 'original_reader_physical_drain_required')
        attempt = self.backend.owner.record['standalone_encyclopedia']['attempt']
        require(attempt.get('outcome') == 'normal_close', 'normal_control_reader_exit_required')
        if self.backend.request is not None:
            phase = self.backend.owner.record.get('standalone_update', {}).get('phase')
            if self.after_close_operation == 'recover':
                require(phase == 'applying', 'original_target_recovery_operation_required')
                return self.backend.recover_update(self.backend.request, confirmed=True)
            if phase == 'waiting_for_close':
                return self.backend.apply_confirmed(self.backend.request)
            if phase == 'applying':
                return self.backend.finish_target_health(self.backend.request)
            if phase == 'recovering':
                return self.backend.finish_recovery_health(self.backend.request)
            raise ValueError('unknown_original_control_operation_phase')
        return None


class EncyclopediaChildControl:
    @classmethod
    def from_inherited_entry(cls, argv, *, trust_bytes, trust_sha256):
        admitted, root_binding = child_binding(argv, trust_bytes=trust_bytes, trust_sha256=trust_sha256)
        channel = WindowsControlChannel.from_inherited_environment()
        try:
            offer = decode_control_frame(channel.read_frame(), message_kinds=KINDS)
            require(offer['kind'] == 'offer' and offer['sequence'] == 1
                    and set(offer['payload']) == {'binding', 'context_sha256', 'inventory_sha256'}, 'original_control_offer_required')
            document = offer['payload']['binding']; _binding_document(document)
            require(offer['payload']['context_sha256'] == admitted.context_sha256
                    and offer['payload']['inventory_sha256'] == admitted.selected.inventory_sha256,
                    'signed_current_control_offer_changed')
            proof = channel.qualify_binding(document, admitted, expected_peer_image_sha256=root_binding.bootstrap_sha256)
            require(offer['binding_sha256'] == proof.binding_sha256, 'original_control_offer_digest_changed')
            wire = _NormalWire(channel, document, proof, message_kinds=KINDS)
            wire.incoming = 1
            wire.send('ready', {'context_sha256': admitted.context_sha256})
            result = object.__new__(cls)
            result.admitted, result.wire = admitted, wire
            result.outstanding = None
            result.outstanding_sent = False
            return result
        except BaseException:
            channel.close()
            raise

    def request(self, operation, body=None, *, overall_timeout=120):
        require(type(overall_timeout) in (int, float) and math.isfinite(overall_timeout)
                and 0 < overall_timeout <= 180, 'bounded_control_reply_wait_required')
        with self.wire.lock:
            require(self.wire.channel.local_witness() == self.wire.document['entry']
                    and self.wire.channel.peer_witness() == self.wire.document['launcher'], 'original_control_peer_changed')
            if self.outstanding is None:
                payload = check_request({'id': uuid.uuid4().hex, 'operation': operation, 'body': body or {}})
                self.outstanding = payload  # retain before potentially ambiguous write
                self.outstanding_sent = False
                self.wire.send('encyclopedia-request', payload)
                self.outstanding_sent = True
            else:
                require(self.outstanding['operation'] == operation and self.outstanding['body'] == (body or {}),
                        'original_control_reply_still_pending')
            return self.resume_outstanding(overall_timeout=overall_timeout)

    def resume_outstanding(self, *, overall_timeout=120):
        require(type(overall_timeout) in (int, float) and math.isfinite(overall_timeout)
                and 0 < overall_timeout <= 180, 'bounded_control_reply_wait_required')
        with self.wire.lock:
            require(self.outstanding is not None and self.outstanding_sent, 'original_control_send_unconfirmed')
            deadline = time.monotonic() + overall_timeout
            while True:
                require(self.wire.channel.local_witness() == self.wire.document['entry']
                        and self.wire.channel.peer_witness() == self.wire.document['launcher'], 'original_control_peer_changed')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LaunchHold('original_control_reply_pending_timeout')
                try:
                    reply = self.wire.receive('encyclopedia-reply', timeout=min(remaining, 30))['payload']
                except LaunchHold as error:
                    if str(error) == 'control_channel_idle_timeout':
                        continue  # zero bytes only; never resend or invent a reply
                    raise
                require(type(reply) is dict and set(reply) == {'id', 'result'}
                        and reply['id'] == self.outstanding['id'] and type(reply['result']) is dict,
                        'original_control_reply_changed')
                self.outstanding = None
                self.outstanding_sent = False
                return reply['result']
