"""Dedicated hook-only registration exchange; normal inherited wire is unchanged.

SDK dispatch follows an original root registration ACK, never a name, pipe PID,
installed-file measurement or peer hash alone. No process/lease owner is created.
"""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
import re

from core.manager_launch_owner import LaunchHold, StockHookSelfTransfer
from core.manager_stock_hook_pipe import StockHookPipe, StockHookPeerProof

_HELD_HOOK_ENTRIES = []
_HOOK_MESSAGE_KINDS = frozenset({'stock-hook-hello', 'stock-hook-offer', 'stock-hook-transfer',
    'stock-hook-registered', 'stock-hook-transfer-retired', 'stock-hook-callback', 'stock-hook-callback-accepted'})


def _require(value, reason):
    if not value:
        raise LaunchHold(reason)


@dataclass(frozen=True)
class StockHookContext:
    transaction_id: str
    attempt_identity_sha256: str
    request_sha256: str
    epoch: str
    stage: str
    version: str

    def checked(self):
        _require(type(self.transaction_id) is str and re.fullmatch('[A-Za-z0-9_-]{1,64}', self.transaction_id)
            and all(type(v) is str and re.fullmatch('[0-9a-f]{64}', v) for v in
                    (self.attempt_identity_sha256, self.request_sha256))
            and type(self.epoch) is str and re.fullmatch('[0-9a-f]{32}', self.epoch)
            and self.stage in {'--veloapp-obsolete', '--veloapp-updated'}
            and type(self.version) is str and 0 < len(self.version) <= 256
            and not any(c.isspace() for c in self.version), 'exact_stock_hook_context_required')
        return self

    @property
    def digest(self):
        self.checked()
        return sha256(json.dumps(asdict(self), sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class _HookWire:
    def __init__(self, pipe, context):
        _require(type(pipe) is StockHookPipe and type(context) is StockHookContext,
                 'dedicated_stock_hook_wire_required')
        self.pipe, self.context = pipe, context.checked()
        self.incoming = self.outgoing = 0
        self.held = False

    def send(self, kind, payload):
        _require(not self.held, 'stock_hook_exchange_held')
        _require(kind in _HOOK_MESSAGE_KINDS and type(payload) is dict, 'exact_stock_hook_message_required')
        document = dict(protocol=1, sequence=self.outgoing + 1, kind=kind,
                        binding_sha256=self.context.digest, payload=payload)
        raw = json.dumps(document, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        try:
            self.pipe.write_frame(len(raw).to_bytes(4, 'little') + raw)
            self.outgoing += 1
        except BaseException:
            self.held = True
            raise

    def receive(self, kind):
        from suite_core.manager.bootstrap_entry import decode_control_frame, check_frame_sequence
        _require(not self.held, 'stock_hook_exchange_held')
        try:
            frame = decode_control_frame(self.pipe.read_frame(), message_kinds=_HOOK_MESSAGE_KINDS)
            check_frame_sequence(frame, binding_sha256=self.context.digest, next_sequence=self.incoming + 1)
            _require(frame['kind'] == kind and type(frame['payload']) is dict, 'exact_stock_hook_message_required')
            self.incoming += 1
            return frame['payload']
        except BaseException:
            self.held = True
            raise


class StockHookEntrySession:
    """One-shot client; root authenticates before self-transfer or stock SDK."""
    def __init__(self, pipe, anchor, proof, context, argv):
        from suite_core.manager.bootstrap_entry import classify_invocation
        invocation = classify_invocation(argv)
        _require(type(pipe) is StockHookPipe and type(proof) is StockHookPeerProof
            and pipe.peer_proof is proof and type(context) is StockHookContext
            and invocation.kind == 'hook' and invocation.argv == (context.stage, context.version),
            'authenticated_exact_stock_hook_entry_required')
        self.pipe, self.anchor, self.proof = pipe, anchor, proof
        self.context, self.argv = context.checked(), invocation.argv
        self.wire = _HookWire(pipe, context)
        self.transfer = StockHookSelfTransfer(pipe.api)
        self.state = 'authenticated'
        self.offer = self.ack = None

    def register(self):
        from core.manager_launch_owner import _native_witness
        _require(self.state == 'authenticated', 'stock_hook_registration_no_replay')
        self.state = 'registration_uncertain'
        self.pipe.verify_root(self.anchor, self.proof)
        local = _native_witness(self.pipe.api, self.pipe.api.GetCurrentProcess())
        self.wire.send('stock-hook-hello', dict(context=asdict(self.context), argv=list(self.argv), entry=local))
        offer = self.wire.receive('stock-hook-offer')
        self.offer = offer  # retain received authority before any fallible transfer
        _require(set(offer) == {'context', 'deployment_root', 'data_root', 'settings_path', 'owner_token',
                               'entry', 'launcher', 'updater', 'job_source_handle'}
            and offer['context'] == asdict(self.context)
            and offer['deployment_root'] == str(self.anchor.code_root)
            and offer['data_root'] == str(self.anchor.data_root)
            and offer['settings_path'] == str(self.anchor.settings_path)
            and offer['entry'] == local
            and offer['launcher'] == dict(pid=self.proof.server_pid,
                creation_filetime=self.proof.creation_filetime, executable_sha256=self.proof.bootstrap_sha256)
            and type(offer['owner_token']) is str and re.fullmatch('[0-9a-f]{32}', offer['owner_token'])
            and type(offer['job_source_handle']) is int and 0 < offer['job_source_handle'] < 2**63,
            'authenticated_stock_hook_deployment_or_attempt_mismatch')
        self.pipe.verify_root(self.anchor, self.proof)
        source = self.transfer.deposit(offer['updater'], offer['job_source_handle'])
        self.wire.send('stock-hook-transfer', dict(source_handle=source, entry=local))
        ack = self.wire.receive('stock-hook-registered')
        self.ack = ack
        _require(set(ack) == {'context', 'entry', 'owner_token', 'registered'}
            and ack == dict(context=asdict(self.context), entry=local,
                            owner_token=offer['owner_token'], registered=True), 'original_root_stock_hook_ack_required')
        self.pipe.verify_root(self.anchor, self.proof)
        self.transfer.retire_after_root_acceptance()
        self.wire.send('stock-hook-transfer-retired', dict(retired=True))
        self.state = 'registered'
        return ack

    def dispatch_sdk(self, sdk_factory):
        """Stock callbacks only; acknowledge observed callback before SDK exits."""
        _require(self.state == 'registered' and callable(sdk_factory), 'root_registered_stock_sdk_required')
        self.pipe.verify_root(self.anchor, self.proof)
        self.state = 'sdk_uncertain'
        def observed(version):
            _require(self.state == 'sdk_uncertain' and str(version) == self.context.version,
                     'exact_original_stock_sdk_callback_required')
            self.wire.send('stock-hook-callback', dict(stage=self.context.stage, version=str(version)))
            ack = self.wire.receive('stock-hook-callback-accepted')
            _require(ack == dict(accepted=True), 'original_root_callback_ack_required')
            self.state = 'callback_observed'
        app = sdk_factory().set_args(list(self.argv)).set_auto_apply_on_startup(False)
        name = ('on_before_update_fast_callback' if self.context.stage == '--veloapp-obsolete'
                else 'on_after_update_fast_callback')
        getattr(app, name)(observed).run()
        _require(self.state == 'callback_observed', 'stock_sdk_callback_not_observed')


class StockHookOwnerSession:
    """Root receives through its original updater, observes and registers before ACK."""
    def __init__(self, pipe, context, *, transport, request, recovery, admitted, job_source_handle):
        from core.manager_launch_owner import WindowsNormalTransport
        _require(type(pipe) is StockHookPipe and pipe.server and pipe.state == 'connected'
            and type(transport) is WindowsNormalTransport and pipe.api is transport.api
            and context.transaction_id == request.transaction_id
            and context.attempt_identity_sha256 == request.attempt_identity_sha256
            and context.request_sha256 == request.binding_sha256,
            'original_root_stock_hook_session_required')
        self.pipe, self.context, self.transport = pipe, context.checked(), transport
        self.owner, self.request, self.recovery = transport.owner, request, recovery
        self.admitted, self.job_source_handle = admitted, job_source_handle
        self.wire = _HookWire(pipe, context)
        self.state = 'connected'
        self.process = self.observation = self.role = None
        # Exact original transport/epoch references used by existing observation.
        pipe.transport, pipe.epoch, pipe.peer_handle = transport, context.epoch, None

    def register(self):
        from core.manager_launch_owner import WindowsControlChannel, _native_witness
        _require(self.state == 'connected', 'stock_hook_owner_registration_no_replay')
        self.state = 'registration_uncertain'
        hello = self.wire.receive('stock-hook-hello')
        _require(set(hello) == {'context', 'argv', 'entry'} and hello['context'] == asdict(self.context)
            and hello['argv'] == [self.context.stage, self.context.version]
            and type(hello['entry']) is dict and hello['entry'].get('pid') == self.pipe.peer_pid(),
            'actual_stock_hook_client_context_required')
        with self.owner.dispatch_lock:
            self.owner._maintenance_context(self.request, allow_pending=True)
            updater = self.owner.children[self.owner._stock_updater_role(self.request)]
            witness = _native_witness(self.transport.api, updater._handle)
            offer = dict(context=asdict(self.context), deployment_root=str(self.owner.root),
                data_root=str(self.owner.data_root), settings_path=str(self.admitted.settings_path),
                owner_token=self.owner.record['owner'], entry=hello['entry'],
                launcher=_native_witness(self.transport.api, self.transport.api.GetCurrentProcess()),
                updater=witness, job_source_handle=self.job_source_handle)
        self.wire.send('stock-hook-offer', offer)
        transferred = self.wire.receive('stock-hook-transfer')
        _require(set(transferred) == {'source_handle', 'entry'} and transferred['entry'] == hello['entry'],
                 'exact_stock_hook_self_transfer_required')
        with self.owner.dispatch_lock:
            self.role, self.process = WindowsControlChannel.duplicate_stock_hook_from_updater(
                self.pipe, self.request, transferred['source_handle'])
            self.observation = self.transport.observe_stock_hook_peer(self.pipe, self.process,
                self.request, (self.context.stage, self.context.version), recovery=self.recovery)
            _require(self.observation['hook'] == hello['entry'], 'actual_stock_hook_self_identity_changed')
            self.owner.register_stock_hook(self.role, self.process, self.request)
        self.wire.send('stock-hook-registered', dict(context=asdict(self.context), entry=hello['entry'],
                                                   owner_token=self.owner.record['owner'], registered=True))
        _require(self.wire.receive('stock-hook-transfer-retired') == dict(retired=True),
                 'stock_hook_remote_source_retirement_required')
        self.state = 'registered'
        return self.observation

    def observe_callback(self):
        _require(self.state == 'registered', 'registered_stock_hook_callback_required')
        callback = self.wire.receive('stock-hook-callback')
        _require(callback == dict(stage=self.context.stage, version=self.context.version)
            and self.process.poll() is None and self.transport.includes(self.process),
            'live_original_stock_hook_callback_required')
        self.wire.send('stock-hook-callback-accepted', dict(accepted=True))
        self.state = 'callback_observed'
        return callback


def run_stock_hook(argv, *, sdk_factory=None, expected_launcher_sha256=None):
    """Frozen hook-first route; environment contains locators, never root trust."""
    import os
    from suite_core.manager.bootstrap_entry import classify_invocation
    from core.manager_stock_hook_pipe import installed_hook_bootstrap_anchor
    invocation = classify_invocation(argv)
    _require(invocation.kind == 'hook' and invocation.argv == (invocation.stage, invocation.version)
        and invocation.stage in {'--veloapp-obsolete', '--veloapp-updated'}, 'stock_hook_transport_unqualified')
    raw = os.environ.pop('COLLECTIGEN_STOCK_HOOK_CONTEXT', None)
    _require(type(raw) is str and 0 < len(raw) <= 8192, 'stock_hook_transport_unqualified')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            _require(key not in value, 'duplicate_stock_hook_locator_key')
            value[key] = item
        return value
    descriptor = json.loads(raw, object_pairs_hook=unique)
    _require(type(descriptor) is dict and set(descriptor) == {'schema', 'settings_relative', 'bindings'}
        and type(descriptor['schema']) is int and descriptor['schema'] == 1
        and type(descriptor['bindings']) is dict and set(descriptor['bindings']) ==
            {'--veloapp-obsolete', '--veloapp-updated'}, 'exact_stock_hook_locator_required')
    selected = descriptor['bindings'][invocation.stage]
    _require(type(selected) is dict and set(selected) == {'pipe', 'context'}
        and type(selected['context']) is dict
        and set(selected['context']) == set(StockHookContext.__dataclass_fields__), 'exact_stock_hook_locator_required')
    context = StockHookContext(**selected['context']).checked()
    _require(invocation.argv == (context.stage, context.version), 'stock_hook_locator_stage_version_changed')
    anchor = installed_hook_bootstrap_anchor(descriptor['settings_relative'],
        expected_bootstrap_sha256=expected_launcher_sha256)
    pipe = StockHookPipe(selected['pipe'])
    retained = dict(pipe=pipe, anchor=anchor, context=context, session=None)
    _HELD_HOOK_ENTRIES.append(retained)  # before native connection/output
    pipe.open_client(anchor=anchor)
    proof = pipe.authenticate_root(anchor)
    session = StockHookEntrySession(pipe, anchor, proof, context, invocation.argv)
    retained['session'] = session
    session.register()
    if sdk_factory is None:
        import velopack
        sdk_factory = velopack.App
    try:
        session.dispatch_sdk(sdk_factory)
    except SystemExit as error:
        if session.state == 'callback_observed' and error.code in (0, None):
            pipe.close()
            _HELD_HOOK_ENTRIES.remove(retained)
        raise
    pipe.close()
    _HELD_HOOK_ENTRIES.remove(retained)
    return 0
