"""Entry consistency and RPC over the stable owner's native normal transport.

Matching DTOs never admit I/O. Native qualification, lease, process retention
and Job scope belong to core.manager_launch_owner; this module adds no owner.
"""
from dataclasses import dataclass, replace
import hashlib
import json
import re
import time
import math

PROTOCOL = 1
MAX_FRAME = 65536
FAST_HOOKS = frozenset({'--veloapp-install', '--veloapp-updated',
                        '--veloapp-obsolete', '--veloapp-uninstall'})
MESSAGE_KINDS = frozenset({'offer','ready','helper-reserve','helper-grant',
    'helper-register','helper-registered','seal-request','sealed','local-drained',
    'local-report-accepted','hold','health','helper-status','helper-non-creation',
    'helper-settled','helper-drained','manager-update-request','manager-update-reply','recovery-visible','recovery-accepted','helper-held','component-stage-request','component-stage-reply'})

def check_update_request(payload):
    require(type(payload) is dict and set(payload)=={'schema','request_id','operation','profile_sha256','body'}
        and type(payload['schema']) is int and payload['schema']==1
        and type(payload['request_id']) is str and re.fullmatch('[0-9a-f]{32}',payload['request_id'])
        and payload['operation'] in {'configure','apply','repair'}
        and type(payload['profile_sha256']) is str and re.fullmatch('[0-9a-f]{64}',payload['profile_sha256'])
        and type(payload['body']) is dict, 'versioned_manager_update_request_required')
    return payload

def check_update_profile(profile, admitted):
    require(type(profile) is dict and set(profile)=={'schema','current_version','channel',
        'trust_sha256','transport_id','data_root','settings_path','cache_root','profile_sha256'}
        and type(profile['schema']) is int and profile['schema']==1
        and type(profile['current_version']) is str and profile['current_version']
        and profile['channel'] in {'stable','beta'}
        and type(profile['transport_id']) is str and 0<len(profile['transport_id'])<=128
        and all(type(profile[key]) is str and re.fullmatch('[0-9a-f]{64}',profile[key])
            for key in ('profile_sha256','trust_sha256'))
        and profile['data_root']==str(admitted.layout.data_root)
        and profile['settings_path']==str(admitted.settings_path), 'exact_update_host_profile_required')
    from pathlib import Path
    cache=Path(profile['cache_root']);zone=admitted.layout.data_root/'Manager/UpdateCache'
    require(cache.is_absolute() and cache.is_relative_to(zone) and cache!=zone,
        'deployment_owned_update_cache_required')
    fields={key:value for key,value in profile.items() if key!='profile_sha256'}
    require(hashlib.sha256(_wire_bytes(fields)).hexdigest()==profile['profile_sha256'],
        'update_host_profile_digest_changed')
    return profile

class EntryHold(ValueError):
    pass

def require(value, reason):
    if not value: raise EntryHold(reason)

def digest_argv(argv):
    require(type(argv) is tuple and len(argv)<=256 and all(type(x) is str and len(x)<=4096 and '\0' not in x for x in argv), 'bounded_explicit_argv_required')
    return hashlib.sha256(json.dumps(argv,ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()

@dataclass(frozen=True)
class Invocation:
    kind: str
    argv: tuple[str,...]
    stage: str | None = None
    version: str | None = None

def classify_invocation(argv):
    argv=tuple(argv);digest_argv(argv)
    hooks=[i for i,x in enumerate(argv) if x.lower().startswith('--veloapp-')]
    if not hooks:return Invocation('normal',argv)
    require(hooks==[0] and argv[0].lower() in FAST_HOOKS and len(argv)>=2,'unknown_mixed_or_misplaced_hook')
    require(0<len(argv[1])<=256 and not argv[1].startswith('-') and not any(c.isspace() for c in argv[1]),'explicit_hook_version_required')
    # Exact full argv/version is subsequently compared to transaction lineage.
    # Stock SDK retains semver parsing and dispatch; no custom hook is invoked.
    return Invocation('hook',argv,argv[0].lower(),argv[1])

@dataclass(frozen=True)
class ProcessBinding:
    pid: int
    creation_filetime: int
    executable_sha256: str

def check_process(value):
    require(type(value) is ProcessBinding and type(value.pid) is int and value.pid>0
        and type(value.creation_filetime) is int and value.creation_filetime>0
        and type(value.executable_sha256) is str and re.fullmatch('[0-9a-f]{64}',value.executable_sha256),'exact_native_process_binding_required')

@dataclass(frozen=True)
class EntryBinding:
    protocol: int
    owner_generation: str
    channel_epoch: str
    deployment_root: str
    data_root: str
    settings_path: str
    mode: str
    argv_sha256: str
    entry: ProcessBinding
    launcher: ProcessBinding

def check_binding(value):
    require(type(value) is EntryBinding and type(value.protocol) is int and value.protocol==PROTOCOL,'entry_protocol_mismatch')
    require(all(type(s) is str and re.fullmatch('[0-9a-f]{32}',s) for s in (value.owner_generation,value.channel_epoch)),'generation_and_epoch_required')
    require(all(type(s) is str and 0<len(s)<=4096 and '\0' not in s for s in (value.deployment_root,value.data_root,value.settings_path)),'bounded_roots_settings_required')
    require(type(value.mode) is str and value.mode in {'portable','installed'} and type(value.argv_sha256) is str and re.fullmatch('[0-9a-f]{64}',value.argv_sha256),'mode_and_argv_pin_required')
    check_process(value.entry);check_process(value.launcher)
    require(value.entry!=value.launcher,'distinct_launcher_and_entry_required')

@dataclass(frozen=True)
class ConsistencyCheck:
    reason: str = 'consistent_synthetic_contract'
    @property
    def production_authorized(self):return False

def check_entry_offer(offer, observed_binding, argv):
    check_binding(offer);check_binding(observed_binding)
    require(offer==observed_binding and digest_argv(tuple(argv))==offer.argv_sha256,'process_deployment_generation_or_argv_mismatch')
    return ConsistencyCheck()

@dataclass(frozen=True)
class HookLineage:
    transaction_id: str
    attempt_identity_sha256: str
    stage: str
    version: str
    argv_sha256: str
    updater: ProcessBinding
    hook: ProcessBinding

def check_hook_lineage(invocation, binding, lineage, observed_updater, *,
                       observed_transaction_id, observed_attempt_identity_sha256):
    check_binding(binding);check_process(observed_updater)
    require(type(invocation) is Invocation and invocation.kind=='hook' and type(lineage) is HookLineage,'hook_lineage_required')
    check_process(lineage.updater);check_process(lineage.hook)
    # Preserve the stock request schema and values, never re-encode as UUID.
    require(type(lineage.transaction_id) is str and re.fullmatch('[A-Za-z0-9_-]{1,64}',lineage.transaction_id)
        and lineage.transaction_id == observed_transaction_id
        and type(lineage.attempt_identity_sha256) is str and re.fullmatch('[0-9a-f]{64}',lineage.attempt_identity_sha256)
        and lineage.attempt_identity_sha256 == observed_attempt_identity_sha256
        and lineage.stage==invocation.stage and lineage.version==invocation.version
        and lineage.argv_sha256==digest_argv(invocation.argv)==binding.argv_sha256
        and lineage.hook==binding.entry and lineage.updater==observed_updater,'transaction_updater_or_hook_mismatch')
    return ConsistencyCheck()

@dataclass(frozen=True)
class SealContractState:
    """Pure race specification, not an owner, native receipt or authority.

    Native adapter must establish settlements and Job descendant accounting.
    Pending grants remain registrable after sealing starts. Cleanup hold is last.
    """
    reservations: tuple[tuple[str, str], ...] = ()
    sealing: bool = False
    sealed: bool = False
    cleanup_held: bool = False
    uncertain: bool = False

def advance_seal_contract(state, event, *, reservation=None,
                          positively_established=False, job_descendants_accounted=False):
    require(type(state) is SealContractState, 'exact_seal_contract_state_required')
    entries = dict(state.reservations)
    if event == 'grant':
        require(not state.sealing and not state.uncertain, 'new_grants_closed')
        require(type(reservation) is str and re.fullmatch('[0-9a-f]{32}',reservation)
                and reservation not in entries, 'unique_grant_required')
        entries[reservation] = 'granted'
    elif event == 'seal-request':
        require(not state.sealed and not state.cleanup_held, 'already_sealed')
        return replace(state, sealing=True)
    elif event in {'spawn', 'registered', 'not-created', 'uncertain'}:
        require(not state.sealed and not state.cleanup_held and reservation in entries,
                'outstanding_reservation_required')
        status = entries[reservation]
        if event == 'spawn':
            require(status == 'granted', 'spawn_requires_unsettled_grant')
            entries[reservation] = 'spawned'
        elif event == 'registered':
            require(status == 'spawned' and positively_established is True,
                    'retained_native_registration_required')
            entries[reservation] = 'registered'
        elif event == 'not-created':
            require(status == 'granted' and positively_established is True,
                    'positive_non_creation_required')
            entries[reservation] = 'not-created'
        else:
            require(status in {'granted', 'spawned'}, 'outstanding_uncertainty_required')
            return replace(state, uncertain=True)
    elif event == 'commit-sealed':
        require(state.sealing and not state.sealed and not state.uncertain
                and all(v in {'registered', 'not-created'} for v in entries.values())
                and job_descendants_accounted is True, 'unsettled_or_uncertain_scope')
        return replace(state, sealed=True)
    elif event == 'cleanup-hold':
        require(state.sealed and not state.uncertain, 'commit_sealed_before_cleanup_hold')
        return replace(state, cleanup_held=True)
    else:
        raise EntryHold('unknown_seal_contract_event')
    return replace(state, reservations=tuple(entries.items()))

def check_readiness(binding, actual_entry, observed_exit_code):
    check_binding(binding);check_process(actual_entry)
    require(binding.entry==actual_entry and observed_exit_code is None,'exited_or_foreign_entry_cannot_be_ready')
    return ConsistencyCheck()

@dataclass(frozen=True)
class HelperHandleOffer:
    role: str
    reservation: str
    source_entry: ProcessBinding
    source_handle: int
    handle_kind: str
    helper: ProcessBinding

def check_helper_offer(binding, offer):
    check_binding(binding)
    require(type(offer) is HelperHandleOffer and type(offer.role) is str and re.fullmatch('[A-Za-z0-9_.-]{1,64}',offer.role)
        and type(offer.reservation) is str and re.fullmatch('[0-9a-f]{32}',offer.reservation),'reserved_helper_role_required')
    check_process(offer.source_entry);check_process(offer.helper)
    require(offer.source_entry==binding.entry and type(offer.source_handle) is int and offer.source_handle>0
        and type(offer.handle_kind) is str and offer.handle_kind in {'original','duplicate'},'source_process_and_retained_handle_required')
    return ConsistencyCheck()

def check_sealed_scope(reported_roles, sealed_roles):
    for roles in (reported_roles,sealed_roles):
        require(type(roles) is tuple and 1<=len(roles)<=128 and all(type(r) is str and len(r)<=64 and
            (re.fullmatch('[A-Za-z0-9_.-]{1,64}',r) or re.fullmatch('hook:stock:[1-4]',r)) for r in roles)
            and len(roles)==len(set(roles)) and 'manager' in roles,'complete_role_scope_required')
    require(set(reported_roles)==set(sealed_roles),'omitted_or_foreign_helper_scope')
    return ConsistencyCheck()

def decode_control_frame(raw, *, message_kinds=None):
    # Trusted entry routes may reuse bounds/schema with their distinct protocol
    # kinds. The normal route retains its closed default whitelist and proof.
    kinds = MESSAGE_KINDS if message_kinds is None else message_kinds
    require(type(kinds) is frozenset and 1<=len(kinds)<=32 and all(type(kind) is str
        and re.fullmatch('[a-z][a-z0-9-]{0,63}',kind) for kind in kinds),'exact_control_message_kinds')
    require(type(raw) is bytes and 4<len(raw)<=MAX_FRAME+4,'bounded_frame_required')
    require(int.from_bytes(raw[:4],'little')==len(raw)-4,'frame_length_mismatch')
    def unique(pairs):
        value={}
        for k,v in pairs:
            require(k not in value,'duplicate_json_key');value[k]=v
        return value
    def constant(value):
        raise EntryHold('non_json_numeric_constant')
    try:frame=json.loads(raw[4:].decode('utf-8'),object_pairs_hook=unique,parse_constant=constant)
    except (ValueError,UnicodeError) as error:raise EntryHold('invalid_control_frame') from error
    require(type(frame) is dict and set(frame)=={'protocol','sequence','kind','binding_sha256','payload'}
        and type(frame['protocol']) is int and frame['protocol']==PROTOCOL
        and type(frame['sequence']) is int and frame['sequence']>0
        and type(frame['kind']) is str and frame['kind'] in kinds and type(frame['binding_sha256']) is str
        and re.fullmatch('[0-9a-f]{64}',frame['binding_sha256']) and type(frame['payload']) is dict,'control_frame_schema')
    return frame

def check_frame_sequence(frame, *, binding_sha256, next_sequence):
    require(frame['binding_sha256']==binding_sha256 and frame['sequence']==next_sequence,'stale_or_foreign_channel_frame')
    return ConsistencyCheck()


def _native():
    from core import manager_launch_owner
    return manager_launch_owner


def _wire_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('utf-8')


def _binding_document(document):
    require(type(document) is dict and set(document) == set(EntryBinding.__dataclass_fields__),
            'exact_entry_document_required')
    try:
        value = EntryBinding(**{**document, 'entry': ProcessBinding(**document['entry']),
                               'launcher': ProcessBinding(**document['launcher'])})
    except (TypeError, KeyError) as error:
        raise EntryHold('malformed_native_binding') from error
    check_binding(value)
    return value


def _snapshot(value):
    require(type(value) is dict and set(value) == {'generation', 'roles', 'roles_sha256'}
            and type(value['generation']) is int and value['generation'] >= 1
            and type(value['roles']) is list and value['roles'] == sorted(value['roles'])
            and type(value['roles_sha256']) is str
            and re.fullmatch('[0-9a-f]{64}', value['roles_sha256']), 'exact_scope_snapshot_required')
    check_sealed_scope(tuple(value['roles']), tuple(value['roles']))
    return json.loads(_wire_bytes(value))


class _NormalWire:
    """Serialized one-request-at-a-time framing; never owns a process/lease."""
    def __init__(self, channel, document, proof, *, timeout=1, message_kinds=None):
        from threading import RLock
        native = _native()
        require(type(channel) is native.WindowsControlChannel and
                type(proof) is native.NativeChannelProof and
                proof.source == 'original_inherited_native_handles' and
                proof.binding_sha256 == hashlib.sha256(_wire_bytes(document)).hexdigest()
                and proof.channel_epoch == document['channel_epoch'], 'native_channel_proof_required')
        require(type(timeout) in (int, float) and 0 < timeout <= 30, 'bounded_channel_timeout')
        self.channel, self.document, self.proof = channel, document, proof
        self.message_kinds = MESSAGE_KINDS if message_kinds is None else message_kinds
        # Validate the trusted route's set through the stock decoder; it is never
        # selected by an offer, environment field or received peer payload.
        require(type(self.message_kinds) is frozenset and 1 <= len(self.message_kinds) <= 32
                and all(type(k) is str and re.fullmatch('[a-z][a-z0-9-]{0,63}', k) for k in self.message_kinds),
                'exact_control_message_kinds')
        self.timeout, self.incoming, self.outgoing = timeout, 0, 0
        self.lock = RLock()
        self.received_pending = None

    def send(self, kind, payload):
        raw = _wire_bytes({'protocol': PROTOCOL, 'sequence': self.outgoing + 1,
                          'kind': kind, 'binding_sha256': self.proof.binding_sha256,
                          'payload': payload})
        framed = len(raw).to_bytes(4, 'little') + raw
        decode_control_frame(framed, message_kinds=self.message_kinds)
        self.channel.write_frame(framed, timeout=self.timeout)
        self.outgoing += 1

    def receive(self, kind=None, *, timeout=None):
        # Retain a complete decoded reply before fallible sequence bookkeeping.
        # Partial/invalid frames never become retained authenticated receipts.
        if self.received_pending is None:
            if timeout is not None:
                require(type(timeout) in {int,float} and math.isfinite(timeout) and 0<timeout<=30,
                        'bounded_retained_reply_wait_required')
            wait=self.timeout if timeout is None else min(self.timeout,timeout)
            self.received_pending = decode_control_frame(self.channel.read_frame(timeout=wait), message_kinds=self.message_kinds)
        frame = self.received_pending
        check_frame_sequence(frame, binding_sha256=self.proof.binding_sha256,
                             next_sequence=self.incoming + 1)
        require(kind is None or frame['kind'] == kind, 'unexpected_normal_rpc')
        self.incoming += 1
        self.received_pending = None
        return frame


class NormalEntrySession:
    """Child's qualified RPC session; strong references preserve local originals.

    This session never releases a deployment or terminates a helper. Creation
    callbacks must retain their original process immediately on physical birth.
    Callback failure is uncertain, never evidence that no child was created.
    """
    @classmethod
    def accept(cls, channel, admitted, *, expected_launcher_sha256, timeout=1):
        require(type(channel) is _native().WindowsControlChannel, 'native_channel_required')
        frame = decode_control_frame(channel.read_frame(timeout=timeout))
        require(frame['kind'] == 'offer' and frame['sequence'] == 1 and
                {'binding','scope'}.issubset(frame['payload'])
                and set(frame['payload']).issubset({'binding','scope','update_profile','reader_selection'}),
                'initial_normal_offer_required')
        document = frame['payload']['binding']
        binding = _binding_document(document)
        scope = _snapshot(frame['payload']['scope'])
        proof = channel.qualify_binding(document, admitted,
                                       expected_peer_image_sha256=expected_launcher_sha256)
        session = cls(channel, document, proof, timeout=timeout)
        require(frame['binding_sha256'] == proof.binding_sha256, 'offer_binding_digest_mismatch')
        session.wire.incoming = 1
        session.binding, session.admitted, session.scope = binding, admitted, scope
        session._peer()
        session.update_profile=(check_update_profile(frame['payload']['update_profile'],admitted)
            if 'update_profile' in frame['payload'] else None)
        session.reader_selection = None
        if 'reader_selection' in frame['payload']:
            from core.encyclopedia_connection import root_reader_selection
            selected = root_reader_selection(admitted.layout.code_root,admitted.settings_path)
            require(selected is not None and selected.context()==frame['payload']['reader_selection'],
                    'original_root_reader_offer_changed')
            session.reader_selection = selected
        return session

    def __init__(self, channel, document, proof, *, timeout=1):
        self.wire = _NormalWire(channel, document, proof, timeout=timeout)
        self.state, self.error = 'offered', None
        self.helpers, self.reservations = {}, {}
        self.pending_helpers = []
        self.helper_capabilities = []
        self.helper_outstanding = None
        self.seal = None

    def _peer(self):
        require(self.wire.channel.local_witness() == self.wire.document['entry'] and
                self.wire.channel.peer_witness() == self.wire.document['launcher'],
                'native_entry_peer_changed')

    def claim_recovery_ui(self, recovery_id, *, timeout=1):
        """Visibility notification on the original channel, with no action authority."""
        require(type(recovery_id) is str and re.fullmatch('[0-9a-f]{32}',recovery_id)
                and type(timeout) in {int,float} and 0<=timeout<=1,'bounded_recovery_visibility_required')
        with self.wire.lock:
            require(self.helper_outstanding is None and self.state in {'ready','sealing','sealed'},
                    'intact_original_recovery_channel_required')
            self._peer()
            payload={'recovery_id':recovery_id,'native_entry':self.wire.document['entry']}
            self.wire.send('recovery-visible',payload)
            frame=self.wire.receive(timeout=timeout)
            self._peer()
            require(frame['kind']=='recovery-accepted' and frame['payload']==payload,
                    'original_recovery_visibility_ack_required')
            return True

    def request_manager_update(self, operation, body, *, profile_sha256):
        """One authenticated correlated request; root owns every admission/write."""
        from uuid import uuid4
        require(self.state in {'offered','ready'}, 'live_unsealed_update_request_required')
        payload=check_update_request(dict(schema=1,request_id=uuid4().hex,
            operation=operation,profile_sha256=profile_sha256,body=body))
        with self.wire.lock:
            reply=self._exchange('manager-update-request',payload,{'manager-update-reply'})['payload']
            try:
                require(type(reply) is dict and set(reply)=={'schema','request_id','operation','profile_sha256','status','result'}
                    and reply['schema']==1 and type(reply['schema']) is int
                    and all(reply[key]==payload[key] for key in ('request_id','operation','profile_sha256'))
                    and reply['status'] in {'reviewed','held'} and type(reply['result']) is dict,
                    'correlated_manager_update_reply_required')
            except BaseException as error:
                self.state,self.error='held',error
                raise
        return reply

    def request_component_stage(self,operation,scope_id=None):
        require(operation in {'begin','check','release'} and self.state=='ready',
                'live_component_stage_request_required')
        payload={'schema':1,'operation':operation,'scope_id':scope_id,'component':'collectigen.tag-encyclopedia'}
        with self.wire.lock:
            reply=self._exchange('component-stage-request',payload,{'component-stage-reply'})['payload']
            require(type(reply) is dict and set(reply)=={'schema','operation','scope'}
                    and reply['schema']==1 and reply['operation']==operation
                    and type(reply['scope']) is dict
                    and set(reply['scope'])=={'scope_id','owner_generation','code_root','data_root'}
                    and reply['scope']['owner_generation']==self.binding.owner_generation
                    and reply['scope']['code_root']==str(self.admitted.layout.code_root)
                    and reply['scope']['data_root']==str(self.admitted.layout.data_root)
                    and (operation=='begin' or reply['scope']['scope_id']==scope_id),
                    'original_component_stage_reply_required')
            return reply['scope']

    def _exchange(self, kind, payload, expected, *, reply_timeout=None):
        require(self.helper_outstanding is None, 'helper_reply_outstanding')
        if reply_timeout is not None:
            require(type(reply_timeout) in {int,float} and math.isfinite(reply_timeout)
                    and 0<reply_timeout<=30,'bounded_entry_reply_deadline_required')
            require(getattr(self,'readiness_outstanding',None) is None,'original_readiness_reply_required')
            self.readiness_outstanding=dict(kind=kind,payload=payload,send_entered=False,
                deadline=time.monotonic()+reply_timeout,reply=None)
        try:
            self._peer()
            if reply_timeout is not None:self.readiness_outstanding['send_entered']=True
            self.wire.send(kind, payload)
            while True:
                try:
                    if reply_timeout is None:frame=self.wire.receive()
                    else:
                        remaining=self.readiness_outstanding['deadline']-time.monotonic()
                        require(remaining>0,'entry_readiness_reply_deadline')
                        frame=self.wire.receive(timeout=remaining)
                    break
                except BaseException as error:
                    idle=(reply_timeout is not None and type(error) is _native().LaunchHold
                        and str(error)=='control_channel_idle_timeout'
                        and self.wire.channel.read_fault is None and self.wire.received_pending is None)
                    if not idle:raise
                    self._peer()
            self._peer()
            require(frame['kind'] in expected, 'normal_rpc_not_accepted')
            if reply_timeout is not None:self.readiness_outstanding['reply']=frame
            return frame
        except BaseException as error:
            self.state, self.error = 'held', error
            raise

    def ready(self, *, timeout=30):
        with self.wire.lock:
            require(self.state == 'offered', 'entry_readiness_phase')
            frame = self._exchange('ready', {'native_entry': self.wire.channel.local_witness(),
                                            'scope': self.scope}, {'ready'},reply_timeout=timeout)
            require(set(frame['payload']) in ({'accepted'},{'accepted','scope'}) and frame['payload']['accepted'] is True,
                    'entry_readiness_rejected')
            if 'scope' in frame['payload']:
                refreshed = _snapshot(frame['payload']['scope'])
                require(refreshed['roles'] == self.scope['roles']
                    and refreshed['roles_sha256'] == self.scope['roles_sha256']
                    and refreshed['generation'] == self.scope['generation'] + 1,
                    'exact_successor_health_generation_required')
                self.scope = refreshed
            self.state = 'ready'

    def reserve_helper(self, role, program_sha256):
        with self.wire.lock:
            require(self.state == 'ready', 'entry_helper_grants_closed')
            frame = self._exchange('helper-reserve', {'role': role, 'program_sha256': program_sha256},
                                   {'helper-grant'})
            payload = frame['payload']
            require(set(payload) == {'reservation'} and type(payload['reservation']) is str
                    and re.fullmatch('[0-9a-f]{32}', payload['reservation']), 'exact_helper_grant_required')
            token = payload['reservation']
            require(token not in self.reservations, 'repeated_helper_grant')
            self.reservations[token] = (role, program_sha256)
            return token

    def register_helper(self, reservation, process):
        # Keep original before any HANDLE/witness query or channel operation.
        self.pending_helpers.append(process)
        with self.wire.lock:
            try:
                require(self.state in {'ready', 'sealing'} and reservation in self.reservations
                        and reservation not in self.helpers, 'outstanding_local_helper_required')
                role, program_pin = self.reservations[reservation]
                handle = int(process._handle)
                witness = _native()._native_witness(self.wire.channel.api, handle)
                check_process(ProcessBinding(**witness))
                require(witness['executable_sha256'] == program_pin, 'local_helper_program_mismatch')
                payload = {'reservation': reservation, 'source_handle': handle,
                           'native_helper': witness, 'source_entry': self.wire.channel.local_witness()}
                frame = self._exchange('helper-register', payload, {'helper-registered'})
                scope = _snapshot(frame['payload'])
                require(role in scope['roles'] and scope['generation'] > self.scope['generation']
                        and set(self.scope['roles']).issubset(scope['roles']), 'helper_scope_not_registered')
                self.scope = scope
                self.helpers[reservation] = process
                self.pending_helpers.remove(process)
                del self.reservations[reservation]
            except BaseException as error:
                self.state, self.error = 'held', error
                raise

    def create_helper(self, role, program_sha256, create_process):
        token = self.reserve_helper(role, program_sha256)
        try:
            process = create_process()  # Existing actor owns immediate physical retention.
        except BaseException as error:
            self.state, self.error = 'held', error
            raise
        self.register_helper(token, process)
        return process

    def begin_cleanup(self):
        with self.wire.lock:
            require(self.helper_outstanding is None, 'helper_reply_outstanding')
            if self.state in {'sealed', 'local_accepted'}: return True
            require(self.state in {'offered', 'ready', 'sealing'}, 'entry_cleanup_held')
            self.state = 'sealing'
            frame = self._exchange('seal-request', self.scope, {'sealed', 'hold'})
            if frame['kind'] == 'hold':
                require(frame['payload'] == {'reason': 'reservations_settling'}, 'unresolved_root_hold')
                return False
            require(not self.reservations, 'unsettled_local_reservations')
            seal = _snapshot(frame['payload'])
            require(seal == self.scope, 'sealed_scope_changed')
            self.seal, self.state = seal, 'sealed'
            return True

    def local_drained(self):
        with self.wire.lock:
            if self.state == 'local_accepted':
                self.retire_helper_copies()
                return True
            require(self.state == 'sealed' and not self.reservations and not self.pending_helpers,
                    'sealed_local_scope_required')
            if any(capability.close_held for capability in self.helper_capabilities): return False
            if any(process.poll() is None for process in self.helpers.values()): return False
            frame = self._exchange('local-drained', {'owner_token': self.binding.owner_generation,
                                    **self.seal}, {'local-report-accepted'})
            require(frame['payload'] == {'status': 'local_scope_accepted',
                    'generation': self.seal['generation'], 'roles_sha256': self.seal['roles_sha256']},
                    'local_scope_report_rejected')
            self.state = 'local_accepted'
            self.retire_helper_copies()
            return True


    def retire_helper_copies(self):
        """Retire entry-owned query copies only after accepted local closure."""
        with self.wire.lock:
            require(self.state == 'local_accepted', 'accepted_local_report_required')
            for capability in self.helper_capabilities:
                capability.retire_query_copies()

    def prepare_owned_helper_capability(self, native=None, reviewed=None, program_pin=None):
        """Bind the worker's one original native before birth, never a replacement."""
        with self.wire.lock:
            require(self.state=='ready' and not self.helper_capabilities and self.helper_outstanding is None,
                    'fresh_ready_normal_helper_binding_required')
            self._peer()
            require(native is not None and native.job is None and not native.logs
                    and getattr(native,'helper_capability',None) is None,
                    'fresh_original_native_before_birth_required')
            cap=EntryHelperAdapter(self,native,None,reviewed,program_pin)
            native.bind_helper_capability(cap)
            return cap


@dataclass(frozen=True)
class HelperOwnerSettlement:
    """Component-only DTO; native accounting must authenticate original issuance."""
    generation: tuple
    attempt_id: str
    context_sha256: str
    reservation: object
    disposition: str
    evidence_id: str


class EntryHelperAdapter:
    """Candidate-only helper bridge. Production construction remains disabled.

    Legacy injected accounting remains component-only. Wire component tests reuse
    the authenticated dispatcher with fake native boundaries. Neither constructor
    enables a production factory or grants actual cross-process qualification.
    """
    production_authorized = False

    @classmethod
    def for_wire_component_test(cls, session, native, reviewed, program_pin, *, duplicate_query=None):
        return cls(session, native, None, reviewed, program_pin, duplicate_query=duplicate_query)

    @classmethod
    def for_component_test(cls, session, native, accounting, reviewed, program_pin, *, duplicate_query=None):
        return cls(session, native, accounting, reviewed, program_pin, duplicate_query=duplicate_query)

    @classmethod
    def for_direct_runtime(cls, session, native, selection, command):
        from core.manager_direct_admission import DirectRuntimeSelection,direct_command
        require(type(selection) is DirectRuntimeSelection and selection.root==session.admitted.layout.code_root,
                'package_bound_direct_runtime_required')
        selection.validate()
        kind='version' if tuple(command)==(str(selection.selection.executable),'--version') else 'server'
        try:port=0 if kind=='version' else int(command[command.index('--port')+1])
        except (ValueError,IndexError):raise EntryHold('fixed_direct_command_required') from None
        require(tuple(command)==direct_command(selection.selection,kind,port),'fixed_direct_command_required')
        return cls(session,native,None,selection.context(kind,port),selection.program_sha256,direct_context=True)

    @classmethod
    def for_reader(cls,session,native,selection):
        from core.encyclopedia_connection import RootReaderSelection
        require(type(selection) is RootReaderSelection and selection is session.reader_selection
                and selection.root==session.admitted.layout.code_root,'original_root_reader_offer_required')
        return cls(session,native,None,selection.context(),selection.program_sha256,reader_context=True)

    def __init__(self, session, native, accounting, reviewed, program_pin, *, duplicate_query=None,direct_context=False,reader_context=False):
        from core.ollama_owned_windows import WindowsOwnedProcess
        from types import MappingProxyType
        require(type(session) is NormalEntrySession and type(native) is WindowsOwnedProcess,
                'original_typed_entry_and_runtime_required')
        required = ({'directory','inventory_sha256','version','program','settings_path'} if reader_context else
                    {'descriptor_sha256','runtime_inventory_sha256','runtime_version_sha256','model_resource_id','program',
                    'working_directory','environment_sha256','command_kind','port','argv_sha256'} if direct_context else
                    {'installation_id', 'lifecycle_identity', 'program','publication_manifest_sha256', 'prepared_inventory_sha256'})
        require(type(reviewed) is dict and set(reviewed) == required
                and all(type(v) is str and 0 < len(v) <= 4096 for v in reviewed.values()),
                'frozen_reviewed_helper_context_required')
        require(type(program_pin) is str and re.fullmatch('[0-9a-f]{64}', program_pin),
                'independent_helper_program_pin_required')
        self.session, self.native, self.accounting = session, native, accounting
        self.reviewed, self.program_pin = MappingProxyType(dict(reviewed)), program_pin
        self.role_base='owned-encyclopedia' if reader_context else 'owned-llamacpp' if direct_context else 'owned-ollama'
        self.generation = (session.binding.owner_generation, session.binding.channel_epoch,
                           session.wire.proof.binding_sha256)
        self.attempts, self.serial, self.error = {}, 0, None
        self._holds = set()
        self.duplicate_query = duplicate_query or self._duplicate_local_query
        session.helper_capabilities.append(self)

    def _same_generation(self):
        require(self.generation == (self.session.binding.owner_generation, self.session.binding.channel_epoch,
                self.session.wire.proof.binding_sha256), 'original_helper_capability_generation_changed')
        require(self.native.helper_capability is self, 'original_runtime_capability_changed')

    def _original_attempt(self, attempt):
        from core.ollama_owned_windows import HelperAttempt
        from types import MappingProxyType
        self._same_generation()
        require(type(attempt) is HelperAttempt and type(attempt.context) is MappingProxyType
                and self.native.helper_attempt is attempt
                and self.native._helper_attempt_capability is self,
                'original_native_helper_attempt_required')
        require(type(attempt.identity) is str and re.fullmatch('[0-9a-f]{32}', attempt.identity),
                'bounded_native_helper_attempt_required')
        context = dict(attempt.context)
        require(set(context) == set(self.reviewed) | {'helper_attempt_id'}
                and context['helper_attempt_id'] == attempt.identity
                and {k:v for k,v in context.items() if k != 'helper_attempt_id'} == dict(self.reviewed),
                'reviewed_helper_attempt_context_changed')
        return hashlib.sha256(json.dumps(context, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def reserve(self, context):
        with self.session.wire.lock:
            attempt = self.native.helper_attempt
            digest = self._original_attempt(attempt)
            require(attempt.context is context and attempt.identity not in self.attempts
                    and not self.native._helper_create_entered and self.native.job is None
                    and not self.native.logs,
                    'fresh_pre_birth_native_attempt_required')
            self.serial += 1
            slot = {'attempt': attempt, 'context_sha256': digest, 'generation': self.generation,
                    'role': self.role_base+'-' + (attempt.identity if self.accounting is None else str(self.serial)), 'phase': 'reserve_pending',
                    'reservation': None, 'copy': None, 'borrowed': None, 'evidence': None, 'retired': False}
            # Correlation and generation retained BEFORE any reserve I/O/ACK.
            self.attempts[attempt.identity] = slot
            if self.accounting is None and self.session.state != 'ready':
                require(self.session.state in {'sealing','sealed','local_accepted'}, 'entry_helper_grants_closed')
                slot['phase'] = 'local_unsent'
                raise EntryHold('helper_rejected_before_send_and_creation')
            self._holds.add(attempt.identity)
            try:
                if self.accounting is None:
                    return self._wire_reserve(slot)
                require(self.session.state == 'ready', 'entry_helper_grants_closed')
                token = self.accounting.reserve_attempt(self.session, slot, self.program_pin)
                require(type(token) is str and re.fullmatch('[0-9a-f]{32}', token), 'exact_helper_reservation_required')
                slot['reservation'], slot['phase'] = token, 'reserved'
                return token
            except BaseException as error:
                self.error = error
                raise

    def _slot(self, reservation):
        self._same_generation()
        values = [slot for slot in self.attempts.values() if slot['reservation'] == reservation and reservation is not None]
        require(len(values) == 1, 'exact_original_helper_reservation_required')
        slot = values[0]
        require(self._original_attempt(slot['attempt']) == slot['context_sha256'], 'original_helper_context_changed')
        return slot

    def _duplicate_local_query(self, borrowed, retain):
        import ctypes
        from ctypes import wintypes as W
        api = self.session.wire.channel.api
        copy = _native().RetainedNativeProcess(api, None)
        retain(copy)  # Retain placeholder before native call and all queries.
        handle = W.HANDLE()
        ok = api.DuplicateHandle(api.GetCurrentProcess(), W.HANDLE(borrowed._handle),
             api.GetCurrentProcess(), ctypes.byref(handle), 0x1000 | 0x100000, False, 0)
        copy._handle = handle.value
        require(ok and copy._handle, 'local_helper_query_duplicate_unconfirmed')
        return copy

    def register_suspended(self, reservation, borrowed):
        from core.ollama_owned_windows import BorrowedProcess
        with self.session.wire.lock:
            slot = self._slot(reservation)
            require(type(borrowed) is BorrowedProcess and slot['phase'] == 'reserved'
                    and self.native._helper_create_entered, 'born_suspended_original_required')
            require(type(borrowed._handle) is int and borrowed._handle > 0
                    and type(borrowed.pid) is int and borrowed.pid > 0, 'process_only_transfer_required')
            children = [child for child in self.native.logs if child._handle == borrowed._handle
                        and child.pid == borrowed.pid and child.job is self.native.job]
            require(len(children) == 1 and self.native.job.thread and children[0].poll() is None,
                    'retained_original_must_remain_suspended')
            slot['borrowed'], slot['phase'] = borrowed, 'transfer_pending'
            def retain(copy):
                require(slot['copy'] is None, 'one_local_query_copy_required')
                slot['copy'] = copy
                self.session.pending_helpers.append(copy)
            try:
                if self.accounting is None:
                    self._wire_transfer(slot, borrowed, cleanup_only=False)
                    require(self.native.job.thread and children[0].poll() is None,
                            'original_changed_before_registration_ack')
                    return
                copy = self.duplicate_query(borrowed, retain)
                require(slot['copy'] is copy and copy._handle and copy.pid == borrowed.pid,
                        'retained_query_copy_required')
                self.session.register_helper(reservation, copy)
                require(self.accounting.confirm_transfer(self.session, slot) is True,
                        'original_owner_transfer_confirmation_required')
                require(self.native.job.thread and children[0].poll() is None,
                        'original_changed_before_registration_ack')
                slot['phase'] = 'registered'
            except BaseException as error:
                self.error = error
                raise  # Runtime keeps root suspended; no resume/Stop here.

    def _pre_birth(self, slot):
        require(self._original_attempt(slot['attempt']) == slot['context_sha256']
                and not self.native._helper_create_entered and self.native.job is None
                and not self.native.logs and slot['copy'] is None,
                'positive_original_pre_birth_gate_required')

    def _authenticate_settlement(self, slot, evidence):
        # The injected fixture must validate its retained native-owner outcome,
        # exact attempt/context/generation/reservation and positive non-grant or
        # grant settlement. This method must NEVER accept a wire boolean alone.
        require(type(evidence) is HelperOwnerSettlement and evidence.generation == slot['generation']
                and evidence.attempt_id == slot['attempt'].identity
                and evidence.context_sha256 == slot['context_sha256']
                and evidence.disposition in {'grant_settled', 'non_grant'},
                'exact_original_owner_settlement_required')
        if evidence.disposition == 'non_grant':
            require(evidence.reservation is None and slot['reservation'] is None, 'positive_non_grant_required')
        else:
            require(type(evidence.reservation) is str and re.fullmatch('[0-9a-f]{32}', evidence.reservation)
                    and (slot['reservation'] is None or slot['reservation'] == evidence.reservation),
                    'original_grant_settlement_required')
        require(self.accounting.authenticate_settlement(self.session, slot, evidence) is True,
                'authenticated_original_owner_settlement_required')
        evidence_id = evidence.evidence_id
        require(type(evidence_id) is str and 0 < len(evidence_id) <= 256,
                'bounded_authenticated_settlement_id_required')
        # A lost reserve ACK can leave the slot token unknown while the session
        # already retains the original grant. Retire only its authenticated token.
        token = evidence.reservation
        if token is not None:
            local = self.session.reservations.get(token)
            require(local is None or local == (slot['role'], self.program_pin),
                    'authenticated_local_reservation_changed')
            self.session.reservations.pop(token, None)
            slot['reservation'] = token
        slot['evidence'], slot['phase'] = evidence, 'non_creation_settled'
        self._holds.discard(slot['attempt'].identity)
        return evidence_id

    def settle_non_creation(self, reservation, receipt):
        from core.ollama_owned_windows import PositiveNonCreation
        with self.session.wire.lock:
            slot = self._slot(reservation); self._pre_birth(slot)
            require(type(receipt) is PositiveNonCreation and receipt.reservation is self.native.helper_reservation
                    and receipt.reservation == reservation and receipt.context is slot['attempt'].context
                    and receipt.create_process_entered is False and receipt.reason == 'cancelled_before_create',
                    'original_native_non_creation_receipt_required')
            if self.accounting is None:
                self._wire_settle(slot)
                return
            evidence = self.accounting.settle_non_creation(self.session, slot, receipt)
            self._authenticate_settlement(slot, evidence)

    def reconcile_non_creation(self, attempt):
        from core.ollama_owned_windows import SettledNonCreation
        with self.session.wire.lock:
            self._original_attempt(attempt)
            slot = self.attempts.get(attempt.identity)
            require(slot is not None and slot['attempt'] is attempt, 'retained_original_attempt_required')
            self._pre_birth(slot)
            if self.accounting is None:
                if slot['phase'] == 'local_unsent':
                    return SettledNonCreation(attempt, 'local-unsent:'+attempt.identity)
                if slot.get('rpc') is not None:
                    self._wire_finish_rpc(slot)
                if slot['phase'] not in {'non_grant','non_creation_settled'}:
                    frame = self._wire_rpc(slot, 'helper-status', {}, {'helper-status'})
                    self._wire_apply(slot, frame)
                if slot['phase'] == 'reserved':
                    self._wire_settle(slot)
                if slot['phase'] in {'non_grant','non_creation_settled'}:
                    return SettledNonCreation(attempt, slot['wire_evidence'])
                return None
            # Read-only lookup: never reserve/settle/create replay.
            evidence = self.accounting.lookup_non_creation(self.session, slot)
            if evidence is None: return None
            evidence_id = self._authenticate_settlement(slot, evidence)
            return SettledNonCreation(attempt, evidence_id)

    def drained(self, reservation, borrowed):
        from core.ollama_owned_windows import BorrowedProcess
        with self.session.wire.lock:
            slot = self._slot(reservation)
            if self.accounting is None:
                self._wire_drained(slot, borrowed)
                return
            require(type(borrowed) is BorrowedProcess and borrowed == slot['borrowed']
                    and slot['phase'] in {'transfer_pending', 'registered', 'drain_pending', 'drained'},
                    'original_registered_transfer_required')
            children = [child for child in self.native.logs
                        if child._handle == borrowed._handle and child.pid == borrowed.pid]
            require(len(children) == 1 and children[0].job is self.native.job
                    and children[0].poll() is not None and children[0].job.active() == 0
                    and self.native.drained.is_set() and not self.native.drain_failed
                    and (self.native.drain is None or not self.native.drain.is_alive())
                    and slot['copy'] is not None and slot['copy'].poll() is not None,
                    'original_actor_and_output_drain_required')
            if slot['phase'] == 'transfer_pending':
                # Read-only confirmation of the original transfer; no register replay.
                require(self.accounting.confirm_transfer(self.session, slot) is True
                        and self.session.helpers.get(reservation) is slot['copy']
                        and slot['copy'] not in self.session.pending_helpers,
                        'original_transfer_reconciliation_required')
            slot['phase'] = 'drain_pending'
            require(self.accounting.confirm_drained(self.session, slot) is True,
                    'original_owner_drain_confirmation_required')
            slot['phase'] = 'drained'
            self._holds.discard(slot['attempt'].identity)

    def _envelope(self, slot):
        self._same_generation()
        require(self._original_attempt(slot['attempt']) == slot['context_sha256'], 'original_helper_context_changed')
        return {'attempt_id':slot['attempt'].identity,'context_sha256':slot['context_sha256'],
                'role':slot['role'],'program_sha256':self.program_pin,'reservation':slot['reservation']}

    def _wire_rpc(self, slot, kind, extra, expected, *, read_timeout=None):
        """One outstanding request, one retained reply; never resend after send entry."""
        rpc = slot.get('rpc')
        if rpc is None:
            require(self.session.helper_outstanding is None, 'helper_reply_outstanding')
            self.session._peer()
            rpc = {'kind':kind,'payload':dict(self._envelope(slot),**extra),
                   'expected':frozenset(expected)|{'helper-held'},'receipt':None,'send_entered':False,'broken':False,
                   'began_monotonic':time.monotonic()}
            slot['rpc'] = rpc
            self.session.helper_outstanding = slot
            rpc['send_entered'] = True
            try:
                self.session.wire.send(kind, rpc['payload'])
            except BaseException:
                rpc['broken'] = True
                raise
        require(rpc['kind'] == kind and not rpc['broken'], 'original_helper_stream_recovery_required')
        if rpc['receipt'] is None:
            try:
                frame = (self.session.wire.receive() if read_timeout is None else
                         self.session.wire.receive(timeout=read_timeout))
                # This immutable complete reply survives peer/validation/application failure.
                rpc['receipt'] = _wire_bytes(frame)
            except BaseException as error:
                idle = (type(error) is _native().LaunchHold and str(error) == 'control_channel_idle_timeout'
                        and not getattr(self.session.wire.channel,'read_fault',None))
                # A decoded frame retained before bookkeeping may be retried locally.
                if not idle and self.session.wire.received_pending is None: rpc['broken'] = True
                raise
        self.session._peer()
        frame = json.loads(rpc['receipt'])
        require(frame['kind'] in rpc['expected'] and frame['sequence'] == self.session.wire.incoming,
                'exact_helper_reply_required')
        return frame

    def _wire_apply(self, slot, frame):
        """Validate the retained authenticated correlation before local mutation."""
        rpc = slot.get('rpc')
        require(rpc is not None and rpc['receipt'] == _wire_bytes(frame)
                and self.session.helper_outstanding is slot, 'retained_original_helper_reply_required')
        payload=frame['payload'];common=self._envelope(slot)
        if frame['kind']=='helper-held':
            require(type(payload) is dict and set(payload)==set(common)|{'reason'}
                    and all(payload[key]==common[key] for key in common)
                    and type(payload['reason']) is str and 0<len(payload['reason'])<=256,
                    'exact_original_helper_hold_required')
            # An authenticated refusal is diagnostic evidence only. It cannot
            # grant, prove non-creation, settle, release a copy or authorize Resume.
            slot['root_hold']=payload['reason'];slot['last_reply']=rpc['receipt']
            slot['rpc']=None;self.session.helper_outstanding=None
            raise EntryHold('original_root_helper_hold')
        fields=set(common)|{'phase','helper_generation','disposition','scope','row_sha256','transfer_mode','native_helper'}
        require(type(payload) is dict and set(payload)==fields, 'exact_correlated_helper_reply_required')
        require(all(payload[k]==common[k] for k in ('attempt_id','context_sha256','role','program_sha256')),
                'helper_reply_correlation_changed')
        token=payload['reservation'];phase=payload['phase']
        require((slot['reservation'] is None or token==slot['reservation'])
                and (token is None or type(token) is str and re.fullmatch('[0-9a-f]{32}',token))
                and payload['disposition']==phase and type(payload['helper_generation']) is int
                and payload['helper_generation']>=0
                and slot.get('helper_generation',payload['helper_generation'])==payload['helper_generation']
                and type(payload['row_sha256']) is str
                and re.fullmatch('[0-9a-f]{64}',payload['row_sha256']), 'exact_committed_helper_disposition_required')
        expected_phases={'helper-reserve':{'granted','non_grant'},'helper-register':{'transferred'},
            'helper-non-creation':{'non_creation_settled'},'helper-drained':{'drained'},
            'helper-status':{'granted','transferred','drained','non_grant','non_creation_settled'}}
        require(phase in expected_phases[rpc['kind']], 'helper_reply_transition_changed')
        scope=_snapshot(payload['scope'])
        require(scope['generation']>=self.session.scope['generation'], 'helper_reply_scope_changed')
        removed=set(self.session.scope['roles'])-set(scope['roles'])
        for role in removed:
            previous=[(cap,value) for cap in self.session.helper_capabilities
                if type(cap) is EntryHelperAdapter and cap.session is self.session
                and cap.accounting is None and cap.generation==self.generation
                for value in cap.attempts.values() if value['role']==role]
            require(len(previous)==1 and previous[0][1]['phase']=='drained' and previous[0][1]['copy'] is not None
                    and previous[0][1].get('wire_evidence') and previous[0][1]['generation']==self.generation,
                    'authenticated_terminal_scope_retirement_required')
            original_cap,prior=previous[0];original_cap._same_generation();copy=prior['copy']
            require(not prior.get('retirement_entered') and not prior['retired'] and copy._handle
                    and prior.get('witness_copy') is copy and prior.get('witness_handle')==copy._handle
                    and copy.poll() is not None, 'original_terminal_query_retirement_required')
            prior['retirement_entered']=True
            copy.close_after_exit()
            prior['retired']=True
            require(self.session.helpers.get(prior['reservation']) is copy,'original_terminal_copy_required')
            del self.session.helpers[prior['reservation']]

        if phase in {'transferred','drained'}:
            copy=slot['copy']
            terminal_transition=(slot.get('transfer_mode')=='normal' and payload['transfer_mode']=='cleanup_only'
                and copy is not None and copy.poll() is not None)
            require(token is not None and copy is not None and copy._handle and slot.get('witness')==payload['native_helper']
                    and (payload['transfer_mode']==slot.get('transfer_mode') or terminal_transition) and slot['role'] in scope['roles'],
                    'original_local_transfer_receipt_required')
            slot['transfer_mode']=payload['transfer_mode']
            require(self.session.helpers.get(token) is None or self.session.helpers.get(token) is copy,
                    'local_helper_ownership_changed')
            require(self.session.reservations.get(token) in (None,(slot['role'],self.program_pin)),
                    'local_helper_reservation_changed')
            self.session.helpers[token]=copy
            if copy in self.session.pending_helpers: self.session.pending_helpers.remove(copy)
            self.session.reservations.pop(token,None)
            slot['phase']='registered' if phase=='transferred' else 'drained'
        elif phase=='granted':
            require(token is not None and slot['phase'] in {'reserve_pending','reserved'}, 'original_grant_phase_required')
            require(payload['transfer_mode'] is None and payload['native_helper'] is None,
                    'untransferred_grant_required')
            self.session.reservations[token]=(slot['role'],self.program_pin)
            slot['phase']='reserved'
        else:
            self._pre_birth(slot)
            require(payload['transfer_mode'] is None and payload['native_helper'] is None,
                    'unentered_settlement_required')
            require((phase=='non_grant' and token is None) or (phase=='non_creation_settled' and token is not None),
                    'positive_non_creation_disposition_required')
            self.session.reservations.pop(token,None)
            slot['phase']=phase
        slot['helper_generation']=payload['helper_generation']
        slot['reservation']=token
        self.session.scope=scope
        slot['wire_evidence']=payload['row_sha256']
        slot['last_reply']=rpc['receipt']
        slot['rpc']=None
        self.session.helper_outstanding=None
        if phase in {'drained','non_grant','non_creation_settled'}: self._holds.discard(slot['attempt'].identity)

    def _wire_finish_rpc(self, slot):
        rpc=slot.get('rpc')
        if rpc is not None:
            frame=self._wire_rpc(slot,rpc['kind'],{},rpc['expected'])
            self._wire_apply(slot,frame)

    def observe_original_reply(self, attempt):
        """Consume only a retained original reply; no new request or settlement.

        A zero-byte idle is a bounded observation tick. Validation applies the
        already authenticated receipt, preserving the original send and holds.
        """
        with self.session.wire.lock:
            self._original_attempt(attempt)
            slot=self.attempts.get(attempt.identity)
            require(slot is not None and slot['attempt'] is attempt and self.accounting is None,
                    'original_wire_attempt_observation_required')
            rpc=slot.get('rpc')
            if rpc is not None:
                require(rpc['send_entered'] and not rpc['broken'],'original_helper_stream_recovery_required')
                try:
                    frame=self._wire_rpc(slot,rpc['kind'],{},rpc['expected'],read_timeout=.1)
                    self._wire_apply(slot,frame)
                except BaseException as error:
                    idle=(type(error) is _native().LaunchHold and str(error)=='control_channel_idle_timeout'
                          and not self.session.wire.channel.read_fault and not rpc['broken'])
                    if not idle:raise
            phase='root_held' if slot.get('root_hold') else slot['phase']
            return {'phase':phase,'creation_entered':bool(self.native._helper_create_entered),
                    'terminal':phase in {'drained','non_grant','non_creation_settled','local_unsent'}}

    def reconcile_transfer(self, reservation):
        """Explicit intact-channel receipt recovery; this method never grants Resume."""
        with self.session.wire.lock:
            require(self.accounting is None, 'wire_helper_capability_required')
            slot=self._slot(reservation)
            require(slot['copy'] is not None and slot.get('witness') is not None,
                    'original_pending_transfer_required')
            self._wire_finish_rpc(slot)
            require(slot['phase'] in {'registered','drained'},'original_transfer_receipt_required')
            frame=self._wire_rpc(slot,'helper-status',{}, {'helper-status'})
            self._wire_apply(slot,frame)
            return slot['phase'] in {'registered','drained'}

    def _wire_reserve(self, slot):
        frame=self._wire_rpc(slot,'helper-reserve',{'context':dict(slot['attempt'].context),'creation':'not_entered'}, {'helper-grant'})
        self._wire_apply(slot,frame)
        require(slot['phase']=='reserved','helper_positive_non_grant')
        return slot['reservation']

    def wait_for_original_reserve(self, attempt, *, cancelled=lambda:False, timeout=30):
        """Read the same pre-birth RPC within one cancellable request budget.

        Native zero-byte idle is a poll tick, never a new reserve. Deadline,
        cancellation, partial/EOF and authentication faults retain the attempt.
        """
        require(type(timeout) in {int,float} and math.isfinite(timeout) and 0<=timeout<=30,
                'bounded_retained_reply_wait_required')
        with self.session.wire.lock:
            self._original_attempt(attempt)
            slot=self.attempts.get(attempt.identity)
            require(slot is not None and slot['attempt'] is attempt and self.accounting is None,
                    'original_wire_reserve_required')
            self._pre_birth(slot)
            rpc=slot.get('rpc')
            require(slot['phase']=='reserve_pending' and rpc is not None
                    and rpc['kind']=='helper-reserve' and rpc['send_entered'] and not rpc['broken'],
                    'original_outstanding_reserve_required')
            deadline=rpc['began_monotonic']+timeout
            while slot.get('rpc') is not None:
                require(not cancelled(),'retained_helper_reply_cancelled')
                remaining=deadline-time.monotonic()
                require(remaining>0,'retained_helper_reply_deadline')
                try:
                    frame=self._wire_rpc(slot,'helper-reserve',{}, {'helper-grant'},read_timeout=remaining)
                    self._wire_apply(slot,frame)
                except BaseException as error:
                    intact_idle=(type(error) is _native().LaunchHold and str(error)=='control_channel_idle_timeout'
                        and self.session.wire.channel.read_fault is None and not rpc['broken']
                        and rpc['receipt'] is None and self.session.wire.received_pending is None)
                    if not intact_idle:raise
            require(slot['phase']=='reserved' and slot['reservation'] is not None,
                    'helper_positive_non_grant')
            return slot['reservation']

    def wait_for_original_transfer(self, reservation, *, cancelled=lambda:False, timeout=30):
        """Finish only the original suspended-child registration; never resend or Resume."""
        require(type(timeout) in {int,float} and math.isfinite(timeout) and 0<=timeout<=30,
                'bounded_retained_reply_wait_required')
        with self.session.wire.lock:
            slot=self._slot(reservation)
            require(self.accounting is None and slot['copy'] is not None and slot.get('witness') is not None,
                    'original_pending_transfer_required')
            rpc=slot.get('rpc')
            require(slot['phase']=='transfer_pending' and rpc is not None
                    and rpc['kind']=='helper-register' and rpc['send_entered'] and not rpc['broken'],
                    'original_outstanding_transfer_required')
            deadline=rpc['began_monotonic']+timeout
            while slot.get('rpc') is not None:
                require(not cancelled(),'retained_helper_reply_cancelled')
                remaining=deadline-time.monotonic()
                require(remaining>0,'retained_helper_reply_deadline')
                try:
                    frame=self._wire_rpc(slot,'helper-register',{}, {'helper-registered'},read_timeout=remaining)
                    self._wire_apply(slot,frame)
                except BaseException as error:
                    intact_idle=(type(error) is _native().LaunchHold and str(error)=='control_channel_idle_timeout'
                        and self.session.wire.channel.read_fault is None and not rpc['broken']
                        and rpc['receipt'] is None and self.session.wire.received_pending is None)
                    if not intact_idle:raise
            require(slot['phase']=='registered','original_transfer_receipt_required')
            from core.ollama_owned_windows import BorrowedProcess
            borrowed=slot['borrowed']
            require(type(borrowed) is BorrowedProcess and self.native._helper_create_entered,
                    'original_changed_before_registration_ack')
            children=[child for child in self.native.logs if child._handle==borrowed._handle
                      and child.pid==borrowed.pid and child.job is self.native.job]
            require(len(children)==1 and self.native.job.thread and children[0].poll() is None,
                    'original_changed_before_registration_ack')
            return True

    def _wire_settle(self, slot):
        self._pre_birth(slot)
        frame=self._wire_rpc(slot,'helper-non-creation',{'creation':'not_entered'},{'helper-settled'})
        self._wire_apply(slot,frame)

    def _wire_transfer(self, slot, borrowed, *, cleanup_only):
        slot['borrowed']=borrowed
        def retain(copy):
            require(slot['copy'] is None,'one_local_query_copy_required')
            slot['copy']=copy;self.session.pending_helpers.append(copy)
        if slot['copy'] is None:
            duplicate=self.duplicate_query(borrowed,retain)
            require(duplicate is slot['copy'],'original_returned_query_copy_required')
        copy=slot['copy']
        require(copy is not None and copy._handle and copy.pid==borrowed.pid,'retained_query_copy_required')
        witness=slot.get('witness') if cleanup_only else None
        if witness is not None:
            require(slot.get('witness_handle')==copy._handle and slot.get('witness_copy') is copy
                    and copy.poll() is not None,'original_terminal_query_copy_required')
        else:
            witness=_native()._native_witness(self.session.wire.channel.api,copy._handle)
        check_process(ProcessBinding(**witness))
        require(witness['pid']==borrowed.pid and witness['executable_sha256']==self.program_pin,
                'local_helper_program_mismatch')
        require(slot.get('witness',witness)==witness,'original_native_helper_identity_changed')
        slot['witness']=witness;slot['witness_handle']=copy._handle;slot['witness_copy']=copy
        slot['transfer_mode']='cleanup_only' if cleanup_only else 'normal'
        slot['phase']='transfer_pending'
        frame=self._wire_rpc(slot,'helper-register',{'source_handle':copy._handle,'native_helper':witness,
            'source_entry':self.session.wire.channel.local_witness(),'cleanup_only':cleanup_only},{'helper-registered'})
        self._wire_apply(slot,frame)

    def _wire_drained(self, slot, borrowed):
        from core.ollama_owned_windows import BorrowedProcess
        require(type(borrowed) is BorrowedProcess and self.native._helper_create_entered,
                'retained_born_root_required')
        children=[child for child in self.native.logs if child._handle==borrowed._handle and child.pid==borrowed.pid]
        require(len(children)==1 and children[0].job is self.native.job and children[0].poll() is not None
                and children[0].job.active()==0 and self.native.drained.is_set() and not self.native.drain_failed
                and (self.native.drain is None or not self.native.drain.is_alive()),'original_actor_and_output_drain_required')
        require(slot['borrowed'] in (None,borrowed),'original_borrowed_root_changed')
        require(not slot.get('root_hold'),'original_root_helper_hold')
        self._wire_finish_rpc(slot) # received registration ACK applied once; never replay registration
        if slot['phase'] in {'reserved','transfer_pending'} and slot.get('rpc') is None:
            self._wire_transfer(slot,borrowed,cleanup_only=True)
        require(slot['phase'] in {'registered','drained'} and slot['copy'] is not None
                and slot['copy'].poll() is not None,'original_registered_transfer_required')
        if slot['phase']=='drained':return
        frame=self._wire_rpc(slot,'helper-drained',{'root_exited':True,'private_job_zero':True,'output_drained':True},{'helper-drained'})
        self._wire_apply(slot,frame)

    @property
    def close_held(self):
        # Read the runtime latch passively; never recurse through helper_close_held.
        return bool(self._holds or self.native.helper_accounting_held)

    def retire_query_copies(self):
        with self.session.wire.lock:
            require(self.session.state == 'local_accepted' and not self.close_held,
                    'accepted_local_helper_drain_required')
            for slot in self.attempts.values():
                if slot['copy'] is not None and not slot['retired']:
                    slot['copy'].close_after_exit()
                    slot['retired'] = True


_HELD_NORMAL_ROUTES = []


class NormalOwnerRoute:
    """Root RPC dispatch using the one existing DeploymentOwner and transport.

    No acquisition, new owner, PID adoption, recovery clearing or implicit kill.
    Root bootstrap supplies its held owner and independently admitted image pins.
    """
    @classmethod
    def spawn(cls, owner, admitted, *, manager_executable, manager_sha256,
              helper_pins, environment, timeout=1, abort_scope=None, manager_update_host=None,reader_selection=None):
        from pathlib import Path
        from .entry_binding import validate_normal_launch
        checked = validate_normal_launch(admitted.restart_arguments, executable=manager_executable)
        require(checked == admitted, 'pre_spawn_complete_binding_changed')
        digest = hashlib.sha256()
        with Path(manager_executable).open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''): digest.update(chunk)
        require(digest.hexdigest() == manager_sha256, 'pre_spawn_manager_image_changed')
        # Sole owner retains the native child/Job before any route construction.
        options = {'environment': environment}
        if abort_scope is not None: options['abort_scope'] = abort_scope
        _, channel = _native().WindowsNormalTransport.spawn(owner,
            (str(manager_executable), *admitted.restart_arguments), **options)
        return cls(owner, channel, admitted, manager_sha256=manager_sha256,
                   helper_pins=helper_pins, timeout=timeout, manager_update_host=manager_update_host,reader_selection=reader_selection)

    def __init__(self, owner, channel, admitted, *, manager_sha256, helper_pins, timeout=1,
                 manager_update_host=None, successor_selection=None, peer_role='manager', successor_ready=None,reader_selection=None):
        native = _native()
        require(type(owner) is native.DeploymentOwner and type(channel) is native.WindowsControlChannel
                and channel.transport is owner.native_transport, 'same_native_owner_required')
        require(type(helper_pins) is dict and len(helper_pins) <= 127
                and all(type(role) is str and re.fullmatch('[A-Za-z0-9_.-]{1,64}', role)
                        and role != 'manager' and type(pin) is str and re.fullmatch('[0-9a-f]{64}', pin)
                        for role, pin in helper_pins.items()), 'independent_helper_program_pins_required')
        self.owner, self.channel, self.helper_pins = owner, channel, dict(helper_pins)
        self.peer_role, self.successor_selection = peer_role, successor_selection
        self.successor_ready = successor_ready
        if successor_selection is not None:
            from core.manager_successor_admission import SignedSuccessorSelection
            require(type(successor_selection) is SignedSuccessorSelection
                and successor_selection.owner is owner and successor_selection.admitted == admitted
                and successor_selection.executable_sha256 == manager_sha256
                and peer_role in {'replacement','restored','fresh_restored'} and not helper_pins
                and callable(successor_ready)
                and manager_update_host is None and owner.children.get(peer_role) is not None
                and channel.peer_handle == owner.children[peer_role]._handle,
                'original_signed_successor_channel_required')
            successor_selection.revalidate()
        else:
            require(peer_role == 'manager', 'normal_manager_role_required')
        require(successor_selection is not None or owner.record['deployment'] == {'root': str(admitted.layout.code_root),
                'data': str(admitted.layout.data_root), 'entry_binding': {
                    'mode': 'portable', 'settings_path': str(admitted.settings_path),
                    'argv_sha256': digest_argv(admitted.restart_arguments), 'manager_sha256': manager_sha256}},
                'committed_normal_deployment_binding_mismatch')
        document = {'protocol': PROTOCOL, 'owner_generation': owner.record['owner'],
                    'channel_epoch': channel.epoch, 'deployment_root': str(admitted.layout.code_root),
                    'data_root': str(admitted.layout.data_root), 'settings_path': str(admitted.settings_path),
                    'mode': 'portable', 'argv_sha256': digest_argv(admitted.restart_arguments),
                    'entry': channel.peer_witness(), 'launcher': channel.local_witness()}
        _binding_document(document)
        proof = channel.qualify_binding(document, admitted, expected_peer_image_sha256=manager_sha256)
        self.wire = _NormalWire(channel, document, proof, timeout=timeout)
        self.scope, self.seal = _snapshot(owner.registration_snapshot()), None
        self.state, self.grants, self.error = 'offered', {}, None
        self.physical_drain_acknowledged = False
        self.closure_receipt = None
        self.manager_update_handler = None
        self.manager_update_profile_sha256 = None
        self.manager_update_request_ids = set()
        self.manager_update_profile = None
        offer={'binding':document,'scope':self.scope}
        self.reader_helper_selection = reader_selection
        if reader_selection is not None:
            require(successor_selection is None,'reader_successor_admission_unqualified')
            offer['reader_selection'] = owner.admit_helper_wire_reader(reader_selection).context
        if manager_update_host is not None:
            from core.manager_update_service import ManagerUpdateHostBinding
            require(type(manager_update_host) is ManagerUpdateHostBinding, 'explicit_update_host_binding_required')
            self.manager_update_profile=check_update_profile(manager_update_host.profile(admitted),admitted)
            self.manager_update_profile_sha256=self.manager_update_profile['profile_sha256']
            self.manager_update_handler=manager_update_host.handle
            offer['update_profile']=self.manager_update_profile
        self.wire.send('offer', offer)

    def _live_peer(self):
        return (self.channel.peer_witness() == self.wire.document['entry'] and
                self.channel.local_witness() == self.wire.document['launcher'] and
                self.owner.children[getattr(self,'peer_role','manager')].poll() is None)

    def observe_recovery_ui(self, timeout=.1):
        """Bounded visibility-only observation while normal dispatch stays held.

        Native proof/stream faults remain holds. The notice conveys no grant,
        drain, stop, retry, release or terminal authority.
        """
        require(type(timeout) in {int,float} and 0<timeout<=1,'bounded_recovery_visibility_required')
        with self.owner.dispatch_lock,self.wire.lock:
            require(self.state=='held' and self._live_peer() and not self.channel.read_fault
                    and not self.channel.write_fault,'intact_original_recovery_channel_required')
            if getattr(self,'recovery_ui_id',None) is not None:return self.recovery_ui_id
            try:frame=self.wire.receive(timeout=timeout)
            except BaseException as error:
                if type(error) is _native().LaunchHold and str(error)=='control_channel_idle_timeout' and not self.channel.read_fault:
                    return None
                raise
            payload=frame['payload']
            require(frame['kind']=='recovery-visible' and type(payload) is dict
                    and set(payload)=={'recovery_id','native_entry'} and type(payload['recovery_id']) is str
                    and re.fullmatch('[0-9a-f]{32}',payload['recovery_id'])
                    and payload['native_entry']==self.wire.document['entry'] and self._live_peer(),
                    'exact_original_recovery_visibility_required')
            self.recovery_ui_id=payload['recovery_id']
            self.wire.send('recovery-accepted',payload)
            return self.recovery_ui_id

    def handle_next(self):
        with self.owner.dispatch_lock, self.wire.lock:
            require(self.state != 'held', 'normal_dispatch_recovery_required')
            try:
                return self._handle_next()
            except BaseException as error:
                # Only a native-established zero-byte idle timeout can be polled.
                # Reviewed idle-read successor supplies this positive distinction.
                # Consumed partial frames/EOF still latch native and root holds.
                if type(error) is _native().LaunchHold and str(error) == 'control_channel_idle_timeout':
                    return None
                self.state, self.error = 'held', error
                if self not in _HELD_NORMAL_ROUTES: _HELD_NORMAL_ROUTES.append(self)
                raise

    def _handle_next(self):
        with self.owner.dispatch_lock, self.wire.lock:
            require(self._live_peer(), 'exited_or_foreign_entry')
            frame = self.wire.receive()
            kind, payload = frame['kind'], frame['payload']
            require(self._live_peer(), 'exited_or_foreign_entry')
            if kind in {'helper-reserve','helper-register','helper-status','helper-non-creation','helper-drained'} and 'attempt_id' in payload:
                try:
                    if self.owner._helper_wire_route is None:
                        selection=getattr(self,'owned_helper_selection',None)
                        direct=getattr(self,'direct_helper_selection',None)
                        reader=getattr(self,'reader_helper_selection',None)
                        require((selection is not None or direct is not None or reader is not None) and kind=='helper-reserve','trusted_owned_helper_selection_required')
                        if direct is not None:
                            require(selection is None,'one_explicit_runtime_admission_required')
                            admission=self.owner.admit_helper_wire_direct(direct)
                            admissions={'owned-llamacpp':admission}
                        elif selection is not None:
                            from core.manager_ollama_selection import SelectedOwnedPackage,pinned_json
                            from core import ollama_owned_executor
                            require(type(selection) is SelectedOwnedPackage,'trusted_owned_helper_selection_required')
                            document=pinned_json(selection.selection_path,selection.selection_pin,256*1024)
                            pinned_json(document['prepared_receipt'],document['prepared_receipt_sha256'],4*1024*1024)
                            require(selection.registry_reader.read()==selection.plan.expected_registry,'owned_helper_selection_changed')
                            admission=self.owner.admit_helper_wire_package(selection.layout,selection.plan,
                                dict(selection.environment),executor_module=ollama_owned_executor)
                            admissions={'owned-ollama':admission}
                        else:
                            admissions={}
                        if reader is not None:
                            admissions['owned-encyclopedia']=self.owner.admit_helper_wire_reader(reader)
                        self.owner.prepare_helper_wire(self,admissions,entry_module=__import__(__name__,fromlist=['NormalOwnerRoute']))
                        self.owner._helper_wire_sequence=frame['sequence']-1
                    reply_kind, reply_payload = self.owner.dispatch_helper_wire(self, frame)
                    self.wire.send(reply_kind, reply_payload)
                    return kind
                except BaseException as original_error:
                    # A correlated hold notice has no startup/settlement authority.
                    # Include deferred package admission failures, preserving the
                    # first cause even if the intact-channel notice itself fails.
                    common={'attempt_id','context_sha256','role','program_sha256','reservation'}
                    if common.issubset(payload):
                        try:self.wire.send('helper-held',{key:payload[key] for key in common}|{
                            'reason':type(original_error).__name__+': '+str(original_error)[:200]})
                        except BaseException as notice_error:self.recovery_notice_error=notice_error
                    raise
            if kind == 'component-stage-request':
                require(type(payload) is dict and set(payload)=={'schema','operation','scope_id','component'}
                        and type(payload['schema']) is int and payload['schema']==1
                        and payload['component']=='collectigen.tag-encyclopedia'
                        and payload['operation'] in {'begin','check','release'},'component_stage_schema')
                scope=self.owner.component_stage_scope(self,payload['operation'],payload['scope_id'])
                self.wire.send('component-stage-reply',{'schema':1,'operation':payload['operation'],'scope':scope})
                return kind
            if kind == 'recovery-visible':
                require(self.state in {'ready','sealing','sealed'} and type(payload) is dict
                        and set(payload)=={'recovery_id','native_entry'}
                        and type(payload['recovery_id']) is str and re.fullmatch('[0-9a-f]{32}',payload['recovery_id'])
                        and payload['native_entry']==self.wire.document['entry'],
                        'exact_original_recovery_visibility_required')
                require(getattr(self,'recovery_ui_id',None) in (None,payload['recovery_id']),
                        'original_recovery_visibility_replay_required')
                self.recovery_ui_id=payload['recovery_id']
                self.wire.send('recovery-accepted',payload)
                return kind
            if kind == 'manager-update-request':
                request=check_update_request(payload)
                require(self.state in {'offered','ready'} and callable(self.manager_update_handler)
                    and request['profile_sha256']==self.manager_update_profile_sha256
                    and request['request_id'] not in self.manager_update_request_ids
                    and len(self.manager_update_request_ids)<32, 'original_update_host_request_required')
                # Consume before callback: an uncertain operation cannot be replayed.
                self.manager_update_request_ids.add(request['request_id'])
                result=self.manager_update_handler(self,request)
                require(type(result) is dict and set(result)=={'status','result'}
                    and result['status'] in {'reviewed','held'} and type(result['result']) is dict,
                    'qualified_update_host_reply_required')
                self.wire.send('manager-update-reply',{key:request[key] for key in
                    ('schema','request_id','operation','profile_sha256')} | result)
            elif kind == 'ready':
                if type(payload) is dict and 'scope' in payload: _snapshot(payload['scope'])
                if type(payload) is dict and 'native_entry' in payload:
                    check_process(ProcessBinding(**payload['native_entry']))
                require(self.state == 'offered' and payload == {'native_entry': self.wire.document['entry'],
                        'scope': self.scope}, 'exact_normal_readiness_required')
                self.channel.transport.assert_accounted_live_scope()
                if getattr(self,'successor_selection',None) is None:
                    self.owner.normal_ready(self.owner.record['owner'])
                else:
                    self.successor_selection.revalidate()
                    require(self.owner.record['phase'] in {'replacement_wait_health','replacement_normal'},
                        'original_successor_readiness_phase')
                self.state = 'ready'
                reply = {'accepted': True}
                if getattr(self,'successor_selection',None) is not None:
                    self.successor_ready(self)
                    self.scope = _snapshot(self.owner.registration_snapshot())
                    reply['scope'] = self.scope
                self.wire.send('ready', reply)
            elif kind == 'helper-reserve':
                require(self.state == 'ready' and set(payload) == {'role', 'program_sha256'}
                        and type(payload['role']) is str and payload['role'] in self.helper_pins
                        and payload['program_sha256'] == self.helper_pins[payload['role']],
                        'unadmitted_helper_program')
                token = self.owner.reserve_helper(payload['role'], payload['program_sha256'],
                                                 verify_entry=lambda subject: subject is self.owner and self._live_peer())
                self.grants[token] = {**payload, 'helper_generation': self.owner.record['helper_generation']}
                self.wire.send('helper-grant', {'reservation': token})
            elif kind == 'helper-register':
                require(self.state in {'ready', 'sealing'} and set(payload) ==
                        {'reservation', 'source_handle', 'native_helper', 'source_entry'} and
                        type(payload['reservation']) is str and payload['reservation'] in self.grants and
                        payload['source_entry'] == self.wire.document['entry'] and
                        type(payload['source_handle']) is int and payload['source_handle'] > 0,
                        'reserved_original_source_required')
                witness = ProcessBinding(**payload['native_helper'])
                check_process(witness)
                token = payload['reservation']
                duplicate = self.channel.duplicate_from_peer(payload['source_handle'])
                # Do not query duplicate between native duplication and pending retention.
                self.owner.register_reserved_helper(token, duplicate, verify_transfer=lambda subject, offer, process:
                    subject is self.owner and self._live_peer() and offer == self.grants[token]
                    and offer['helper_generation'] == subject.record['helper_generation']
                    and _native()._native_witness(self.channel.api, process._handle) == payload['native_helper']
                    and payload['native_helper']['executable_sha256'] == offer['program_sha256']
                    and self.channel.transport.includes(process))
                del self.grants[token]
                self.scope = _snapshot(self.owner.registration_snapshot())
                self.channel.transport.assert_accounted_live_scope()
                self.wire.send('helper-registered', self.scope)
            elif kind == 'seal-request':
                require(self.state in {'offered', 'ready', 'sealing'}, 'normal_seal_phase')
                self.owner.stop_helper_grants()  # New grants stop before any settlement check.
                self.state = 'sealing'
                current = _snapshot(self.owner.registration_snapshot())
                require(payload == current, 'stale_cleanup_scope')
                if self.owner.record['reservations'] or self.owner.pending:
                    self.wire.send('hold', {'reason': 'reservations_settling'})
                    return kind
                self.channel.transport.assert_accounted_live_scope()
                self.seal = {**self.owner.cleanup_hold(), 'roles': current['roles']}
                require(_snapshot(self.seal) == current, 'committed_seal_mismatch')
                self.state = 'sealed'
                self.wire.send('sealed', self.seal)
            elif kind == 'local-drained':
                require(type(payload) is dict and set(payload) == {'owner_token', 'generation', 'roles_sha256', 'roles'},
                        'exact_local_report_fields_required')
                _snapshot({key: payload[key] for key in ('generation', 'roles_sha256', 'roles')})
                require(self.state == 'sealed' and payload == {'owner_token': self.owner.record['owner'],
                        **self.seal}, 'exact_local_seal_report_required')
                self.channel.transport.assert_accounted_live_scope()
                accepted = self.owner.accept_local_report(payload, verify_entry=lambda subject, report, seal:
                    subject is self.owner and self._live_peer() and report == payload
                    and seal == {key: self.seal[key] for key in ('generation', 'roles_sha256')}
                    and all(process.poll() is not None for role, process in self.owner.children.items()
                            if role != getattr(self,'peer_role','manager')) and self.channel.transport.job_active_count() == 1)
                self.state = 'local_accepted'
                self.wire.send('local-report-accepted', accepted)
            else:
                raise EntryHold('unexpected_root_normal_rpc')
            return kind

    def finish_physical_drain(self, *, timeout=1):
        with self.owner.dispatch_lock, self.wire.lock:
            return self._finish_physical_drain(timeout=timeout)

    def _finish_physical_drain(self, *, timeout):
        if self.state == 'closed':
            return self.closure_receipt
        self._acknowledge_physical_drain(timeout=timeout)
        # The same owner captures terminal evidence and remembers each successful
        # close. Retrying must not poll a process or Job that it already closed.
        self.owner.settle_normal_closed_resources()
        receipt = self.owner.release_closed()
        self.closure_receipt = receipt
        self.state = 'closed'
        return receipt

    def _acknowledge_physical_drain(self, *, timeout):
        require(self.state == 'local_accepted', 'accepted_local_report_required')
        if not self.physical_drain_acknowledged:
            self.owner.children[getattr(self,'peer_role','manager')].wait(timeout=timeout)
            self.channel.transport.assert_accounted_live_scope()
            require(self.channel.transport.job_active_count() == 0, 'job_scope_still_live')
            self.owner.acknowledge_drain(self.owner.record['owner'], tuple(self.owner.children), complete_scope=True)
            self.physical_drain_acknowledged = True

    def retain_for_maintenance(self, *, timeout=1):
        """Positive native drain without retiring original handles or lease.

        Not a normal closure receipt. Stock hook transport remains separately
        unqualified by DeploymentOwner; no apply follows implicitly.
        """
        with self.owner.dispatch_lock, self.wire.lock:
            self.owner._owned()
            if self.state != 'maintenance_ready':
                self._acknowledge_physical_drain(timeout=timeout)
                self.state = 'maintenance_ready'
            require(self.owner.record['phase'] == 'cleanup_hold'
                    and self.owner.record['drain'] is not None, 'original_maintenance_drain_required')
            return hashlib.sha256(self.owner.raw).hexdigest()

    def finish_retained_normal_drain(self):
        """Ordinary close with a configured host but no accepted Apply review."""
        with self.owner.dispatch_lock,self.wire.lock:
            require(self.state=='maintenance_ready' and self.physical_drain_acknowledged
                and 'maintenance' not in self.owner.record,'ordinary_retained_drain_required')
            self.owner.settle_normal_closed_resources()
            self.closure_receipt=self.owner.release_closed()
            self.state='closed'
            return self.closure_receipt
