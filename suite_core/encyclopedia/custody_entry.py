"""Transient standalone original-object custody; production entry stays disabled.

Uses the stock native birth, inherited channel and recovery lease/archive. This
route cannot launch readers, terminate processes, update code or reacquire by PID.
"""
from copy import deepcopy
import ctypes
from ctypes import wintypes as W
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys
import time

from core.manager_launch_owner import (LaunchHold, WindowsControlChannel, NativeChannelProof,
    DeploymentOwner, FailedNormalRecoveryAdmission, FailedStandaloneLifecycle,
    FailedStandalonePhysicalEvidence, _bytes, _native_witness, standalone_failure_lifecycle_pin)
from core.training_source_io import _assert_unredirected
from suite_core.manager.bootstrap_entry import _NormalWire, decode_control_frame

KINDS = frozenset({'custody-offer', 'custody-ready', 'custody-drain', 'custody-drained'})
ROLE = 'standalone-original-custody'
PHASES = frozenset({'creating', 'born', 'ready', 'drain-requested', 'drained'})
_HELD_CUSTODIANS = []


def _require(value, reason):
    if not value:
        raise LaunchHold(reason)


def _witness(value):
    return (type(value) is dict and set(value) == {'pid', 'creation_filetime', 'executable_sha256'}
        and type(value['pid']) is int and value['pid'] > 0
        and type(value['creation_filetime']) is int and value['creation_filetime'] > 0
        and type(value['executable_sha256']) is str
        and re.fullmatch('[0-9a-f]{64}', value['executable_sha256']))


def validate_custody_record(custody, record):
    _require(type(custody) is dict and set(custody) == {'role', 'phase', 'context', 'identity'}
        and custody['role'] == ROLE and custody['phase'] in PHASES,
        'exact_failed_custody_role_required')
    c = custody['context']
    common = {'schema', 'owner', 'job_name', 'root', 'data', 'bootstrap_sha256', 'bootstrap'}
    fields = common | {'subject_sha256'} if type(c) is dict and c.get('schema') == 1 else common | {
        'origin_kind', 'origin_sha256', 'settings_path', 'trust_sha256'}
    _require(type(c) is dict and set(c) == fields and type(c['schema']) is int and c['schema'] in (1, 2)
        and c['owner'] == record['owner'] and c['root'] == record['deployment']['root']
        and c['data'] == record['deployment']['data']
        and type(c['bootstrap_sha256']) is str and re.fullmatch('[0-9a-f]{64}', c['bootstrap_sha256'])
        and _witness(c['bootstrap']) and c['bootstrap']['executable_sha256'] == c['bootstrap_sha256']
        and c['job_name'] == record.get('native_scope', {}).get('job_name'),
        'exact_failed_custody_context_required')
    reader, enrollment = record.get('standalone_encyclopedia'), record.get('standalone_enrollment')
    if c['schema'] == 1:
        _require(type(reader) is dict and reader.get('binding') == c['subject_sha256'],
            'original_failed_custody_subject_required')
    else:
        _require(c['origin_kind'] in ('reader', 'enrollment')
            and type(c['origin_sha256']) is str and re.fullmatch('[0-9a-f]{64}', c['origin_sha256'])
            and type(c['trust_sha256']) is str and re.fullmatch('[0-9a-f]{64}', c['trust_sha256'])
            and type(c['settings_path']) is str and Path(c['settings_path']).is_relative_to(Path(c['data']))
            and record.get('native_scope', {}).get('scope_kind') == 'standalone_root',
            'original_root_custody_context_required')
        if c['origin_kind'] == 'reader':
            _require(type(reader) is dict and reader.get('binding') == c['origin_sha256'],
                'original_failed_custody_subject_required')
        else:
            _require(type(enrollment) is dict and enrollment.get('context_sha256') == c['origin_sha256']
                and (reader is None or (type(reader) is dict
                    and enrollment.get('handoff_binding_sha256') == reader.get('binding'))),
                'original_enrollment_custody_lineage_required')
    _require((custody['phase'] == 'creating' and custody['identity'] is None)
        or (_witness(custody['identity'])
            and custody['identity']['executable_sha256'] == c['bootstrap_sha256']),
        'original_failed_custody_identity_required')


def observe_job(api, job):
    """Query ONLY the retained original Job, with complete bounded PID coverage."""
    from core.training_process_windows import WindowsTrainingJob
    class Members(ctypes.Structure):
        _fields_ = [('assigned', W.DWORD), ('count', W.DWORD), ('ids', ctypes.c_size_t * 128)]
    _require(bool(job), 'original_custody_job_required')
    before = WindowsTrainingJob._active(api, job)
    members = Members()
    _require(api.QueryInformationJobObject(job, 3, ctypes.byref(members), ctypes.sizeof(members), None),
        'original_custody_job_query_failed')
    after = WindowsTrainingJob._active(api, job)
    ids = tuple(members.ids[:members.count]) if members.count <= 128 else ()
    _require(0 <= members.count <= 128 and members.assigned == members.count
        and before == after == members.count and len(set(ids)) == len(ids)
        and all(pid > 0 for pid in ids), 'complete_original_custody_job_scope_required')
    return {'active': after, 'pids': ids}


def _proof(channel, document):
    local, peer = channel.local_witness(), channel.peer_witness()
    root, custody = (local, peer) if channel.transport is not None else (peer, local)
    _require(document['bootstrap'] == root and document['context']['bootstrap'] == root
        and document['custodian'] == custody
        and root['executable_sha256'] == custody['executable_sha256'] == document['context']['bootstrap_sha256']
        and document['channel_epoch'] == channel.epoch, 'original_custody_native_peer_required')
    return NativeChannelProof(sha256(_bytes(document)).hexdigest(), channel.epoch,
        local['creation_filetime'], peer['creation_filetime'])


class CustodyRootSession:
    def __init__(self, transport, child):
        self.transport, self.child = transport, child
        self.owner = transport.owner
        self.context = self.identity = self.document = self.wire = None
        self.phase = 'new'
        self.job_slot = W.HANDLE()
        self.drain_sent = self.drain_acknowledged = self.resources_closed = False
        self.exit_confirmed = False

    def _persist(self, phase):
        record = deepcopy(self.owner.record)
        record['standalone_custody'] = {'role': ROLE, 'phase': phase,
            'context': self.context, 'identity': self.identity}
        self.owner._persist(record)
        self.phase = phase

    def start(self, environment):
        t, child = self.transport, self.child
        from core.manager_launch_owner import StandaloneEncyclopediaEnrollmentSubject
        subject = t.root_custody_subject
        self.owner._custody_subject_owned(subject)
        _require(t.custody is child and child.custody_parent is t and self.phase == 'new',
            'original_single_custody_birth_required')
        bootstrap = _native_witness(t.api, t.api.GetCurrentProcess())
        enrollment = type(subject) is StandaloneEncyclopediaEnrollmentSubject
        binding = subject.context if enrollment else subject.binding
        _require(bootstrap['executable_sha256'] == binding.bootstrap_sha256,
            'original_custody_bootstrap_image_required')
        self.context = {'schema': 2, 'owner': self.owner.record['owner'],
            'origin_kind': 'enrollment' if enrollment else 'reader',
            'origin_sha256': subject.context_sha256 if enrollment else subject.binding_sha256,
            'job_name': t.job_name, 'root': str(self.owner.root), 'data': str(self.owner.data_root),
            'settings_path': str(binding.settings_path), 'trust_sha256': binding.trust_sha256,
            'bootstrap_sha256': binding.bootstrap_sha256, 'bootstrap': bootstrap}
        self._persist('creating')
        child.endpoint_slots['custody_job'] = self.job_slot
        ok = t.api.DuplicateHandle(t.api.GetCurrentProcess(), t.job, t.api.GetCurrentProcess(),
            ctypes.byref(self.job_slot), 0x4, True, 0)  # JOB_OBJECT_QUERY only
        if self.job_slot.value:
            child.raw_endpoints['custody_job'] = self.job_slot.value
        _require(ok and self.job_slot.value, 'original_custody_job_duplication_held')
        env = dict(environment)
        env['COLLECTIGEN_CUSTODY_JOB'] = str(self.job_slot.value)
        argv = (str(self.owner.root / 'TagEncyclopedia.exe'), '--encyclopedia-custody')
        child._birth_suspended(argv, environment=env, assign_job=False,
            extra_handles=(self.job_slot.value,))
        self.identity = _native_witness(t.api, child.process._handle)
        _require(self.identity['executable_sha256'] == binding.bootstrap_sha256
            and self.identity['pid'] == child.process.pid and not t.includes(child.process),
            'original_outside_job_custodian_required')
        self._persist('born')
        self.document = {'protocol': 1, 'channel_epoch': child.channel.epoch,
            'context': self.context, 'bootstrap': bootstrap, 'custodian': self.identity,
            'job_query': self.job_slot.value}
        self.wire = _NormalWire(child.channel, self.document, _proof(child.channel, self.document),
            message_kinds=KINDS)
        child.process.resume()
        self.wire.send('custody-offer', {'binding': self.document})
        ready = self.wire.receive('custody-ready')
        _require(ready['payload'] == {'context_sha256': sha256(_bytes(self.context)).hexdigest()},
            'original_custody_ready_required')
        self._persist('ready')
        self.assert_ready()

    def assert_ready(self):
        t, child = self.transport, self.child
        _require(self.phase == 'ready' and self.owner._standalone_root_custody is t
            and t.custody_session is self and t.custody is child
            and child.custody_parent is t and child.process is not None
            and child.process.poll() is None and not t.includes(child.process)
            and _native_witness(t.api, child.process._handle) == self.identity
            and self.owner.record.get('standalone_custody') == {'role': ROLE, 'phase': 'ready',
                'context': self.context, 'identity': self.identity}, 'live_original_custody_required')

    def drain(self):
        if self.resources_closed:
            return
        _require(self.transport.custody_session is self and self.transport.custody is self.child,
            'original_custody_drain_required')
        _require(self.transport.job_active_count() == 0, 'custody_reader_scope_still_live')
        if not self.drain_sent:
            self.assert_ready()
            self._persist('drain-requested')
            self.drain_sent = True  # no replay after uncertain write
            self.wire.send('custody-drain', {})
        if not self.drain_acknowledged:
            reply = self.wire.receive('custody-drained')
            _require(reply['payload'] == {'originals_closed': True}, 'original_custody_drain_ack_required')
            self.drain_acknowledged = True
        if not self.exit_confirmed:
            # Authenticated ACK can arrive just before the child finishes exit.
            # Stock wait observes only this retained original creator handle;
            # timeout/query failure/nonzero exit leaves every resource retained.
            _require(self.child.process.wait(timeout=5) == 0, 'custodian_exit_unresolved')
            self.exit_confirmed = True
        # Positive exit closes the child's entire inherited handle table. Parent
        # copies and endpoints are retired separately and remembered on retry.
        self.child._settle_control_resources({})
        if self.child.process._handle:
            self.child.process.close_after_exit()
            self.child.process_info.process = self.child.process_info.thread = None
        self._persist('drained')
        self.resources_closed = True


class CustodyObserver:
    """Live provider retains original objects through every recovery failure."""
    def __init__(self, channel, job, document):
        self.channel, self.job, self.document = channel, job, document
        self.bootstrap_handle = channel.peer_handle
        self.original_job = job
        self.context = deepcopy(document['context'])
        self.identity = channel.local_witness()
        self.wire = _NormalWire(channel, document, _proof(channel, document), message_kinds=KINDS)
        self.ready = self.draining = self.originals_closed = False
        self.admission = self.recovery = self.disposition = None
        self.recovery_attempted = False
        self.drain_reply_entered = False
        self.drain_reply_sent = False
        self.hold = None

    @classmethod
    def from_inherited_entry(cls, *, bootstrap_sha256):
        _require(tuple(sys.argv[1:]) == ('--encyclopedia-custody',), 'fixed_compiled_custody_argv_required')
        _require(getattr(sys, 'frozen', False) and type(bootstrap_sha256) is str
            and re.fullmatch('[0-9a-f]{64}', bootstrap_sha256), 'compiled_custody_pin_required')
        retained = {'channel': None, 'job': None}
        _HELD_CUSTODIANS.append(retained)
        value = os.environ.get('COLLECTIGEN_CUSTODY_JOB', '')
        _require(value.isascii() and value.isdigit() and 0 < int(value) < 2**63,
            'inherited_original_custody_job_required')
        job = int(value)
        retained['job'] = job
        # Intake locators remain retained even if the stock endpoint conversion
        # fails before it can return a channel. They are never closure evidence.
        retained['intake'] = {key: os.environ.get(key) for key in
            ('COLLECTIGEN_CONTROL_READ', 'COLLECTIGEN_CONTROL_WRITE', 'COLLECTIGEN_LAUNCHER_QUERY')}
        channel = WindowsControlChannel.from_inherited_environment()
        # Retain originals before any admission query can fail. An admission
        # exception must keep this process alive; the compiled host owns that hold.
        retained['channel'] = channel
        _require(job != channel.peer_handle, 'distinct_original_custody_objects_required')
        _require(channel.api.SetHandleInformation(job, 1, 0), 'custody_job_noninheritance_held')
        os.environ.pop('COLLECTIGEN_CUSTODY_JOB')
        frame = decode_control_frame(channel.read_frame(), message_kinds=KINDS)
        _require(frame['kind'] == 'custody-offer' and frame['sequence'] == 1
            and type(frame['payload']) is dict and set(frame['payload']) == {'binding'},
            'original_custody_offer_required')
        document = frame['payload']['binding']
        _require(type(document) is dict and set(document) == {'protocol', 'channel_epoch', 'context',
            'bootstrap', 'custodian', 'job_query'} and document['protocol'] == 1
            and document['job_query'] == job and document['context']['bootstrap_sha256'] == bootstrap_sha256
            and Path(sys.executable).absolute() == Path(document['context']['root']) / 'TagEncyclopedia.exe',
            'compiled_custody_route_required')
        observer = cls(channel, job, document)
        retained['observer'] = observer
        _require(frame['binding_sha256'] == observer.wire.proof.binding_sha256,
            'original_custody_offer_binding_required')
        observer.wire.incoming = 1
        observer.accept_ready()
        return observer

    def accept_ready(self):
        _require(not self.ready and self.job and self.bootstrap_handle
            and self.channel.api.WaitForSingleObject(self.bootstrap_handle, 0) == 258,
            'live_original_custody_bootstrap_required')
        _require(observe_job(self.channel.api, self.job) == {'active': 0, 'pids': ()},
            'fresh_empty_original_root_job_required')
        self.wire.send('custody-ready', {'context_sha256': sha256(_bytes(self.context)).hexdigest()})
        self.ready = True

    def _closed_scope(self):
        _require(self.ready and not self.originals_closed and self.job == self.original_job and self.job
            and self.channel.peer_handle == self.bootstrap_handle
            and self.channel.api.WaitForSingleObject(self.bootstrap_handle, 0) == 0,
            'original_bootstrap_exit_required')
        observed = observe_job(self.channel.api, self.job)
        _require(observed == {'active': 0, 'pids': ()}, 'original_reader_job_still_live')
        return observed

    def recover_failed(self):
        if self.disposition is not None:
            self._close_originals()
            self.channel.close()
            self.hold = None
            return self.disposition
        self._closed_scope()
        self.recovery_attempted = True
        _require(not self.draining, 'normal_custody_drain_has_no_recovery_authority')
        if self.admission is None:
            root, data = Path(self.context['root']), Path(self.context['data'])
            recovery = DeploymentOwner(root, data_root=data)
            _assert_unredirected(recovery.marker)
            raw = recovery.marker.read_bytes()
            _require(len(raw) <= 1024 * 1024, 'custody_failed_marker_bound')
            record = json.loads(raw)
            validate_custody_record(record.get('standalone_custody'), record)
            _require(record['standalone_custody']['context'] == self.context
                and record['standalone_custody']['identity'] == self.identity
                and record['standalone_custody']['phase'] in {'born', 'ready', 'drain-requested'},
                'original_issued_custody_record_required')
            lifecycle = FailedStandaloneLifecycle(record['phase'], standalone_failure_lifecycle_pin(record))
            self.recovery = recovery
            marker_pin = sha256(raw).hexdigest()
            def verify(current, recovery_id, lease):
                _require(current == record, 'original_custody_failed_record_changed')
                observed = self._closed_scope()
                evidence = sha256(_bytes({'context': self.context, 'identity': self.identity,
                    'bootstrap_exited': True, 'job': observed, 'marker_sha256': marker_pin})).hexdigest()
                return FailedStandalonePhysicalEvidence(self.admission, recovery_id, lease, marker_pin,
                    evidence, lifecycle.lifecycle_sha256)
            self.admission = FailedNormalRecoveryAdmission(record['deployment'], record['owner'], marker_pin,
                ROLE, verify, standalone_failure=lifecycle)
        # On any exception these originals stay live. Existing archive failure
        # retains raw evidence/hold; a retry uses this same admission and objects.
        self.disposition = self.recovery.archive_failed_normal(self.admission)
        self._close_originals()
        self.channel.close()
        self.hold = None
        return self.disposition

    def _close_originals(self):
        if self.job:
            _require(self.channel.api.CloseHandle(self.job), 'custody_job_close_failed')
            self.job = None
        if self.channel.peer_handle:
            _require(self.channel.peer_handle == self.bootstrap_handle, 'original_custody_bootstrap_changed')
            _require(self.channel.api.CloseHandle(self.bootstrap_handle), 'custody_bootstrap_close_failed')
            self.channel.peer_handle = None
        self.originals_closed = True

    def step(self):
        if self.disposition is not None:
            return 'closed' if self.originals_closed and self.hold is None else 'held'
        if self.draining:
            return self._finish_drain()
        if self.originals_closed:
            return 'held'
        state = self.channel.api.WaitForSingleObject(self.bootstrap_handle, 0)
        _require(state in (0, 258), 'original_custody_bootstrap_wait_uncertain')
        if state == 0:
            if not self.recovery_attempted:
                try:
                    self._closed_scope()
                except LaunchHold:
                    return 'held'
                self.recovery_attempted = True
                try:
                    self.recover_failed()
                except BaseException as error:
                    self.hold = error
                    return 'held'
                return 'closed'
            return 'held'  # no automatic archival replay after failure
        try:
            frame = self.wire.receive('custody-drain', timeout=.1)
        except LaunchHold as error:
            if str(error) == 'control_channel_idle_timeout':
                return 'waiting'
            self.hold = error
            return 'held'
        _require(frame['payload'] == {} and observe_job(self.channel.api, self.job) == {'active': 0, 'pids': ()},
            'original_custody_drain_scope_required')
        self.draining = True
        return self._finish_drain()

    def _finish_drain(self):
        if not self.drain_reply_sent:
            if self.drain_reply_entered:
                return 'held'  # uncertain acknowledgement is never replayed
            self._close_originals()  # retry only still-open original slots
            self.drain_reply_entered = True
            self.wire.send('custody-drained', {'originals_closed': True})
            self.drain_reply_sent = True
        # A positively sent acknowledgement is not replayed when endpoint close
        # fails. The stock channel remembers each successfully retired fd.
        self.channel.close()
        self.hold = None
        return 'closed'

    def run(self):
        # Each wait is bounded; errors retain this live provider and its originals.
        while True:
            try:
                if self.step() == 'closed':
                    return 0
            except BaseException as error:
                self.hold = error
            time.sleep(.1)
