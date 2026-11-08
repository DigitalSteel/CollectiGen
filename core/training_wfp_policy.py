"""Generated LPAC capability experiment gated by exact persistent WFP ownership."""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import re

from . import training_appcontainer as ac
from . import training_offline_policy as base
from . import training_registry_policy as registry
from . import training_wfp as wfp
from .training_process import TrainingProcessError, _bytes, _sha
from .training_process_windows import WindowsTrainingJob

INTERNET_SID = 'S-1-15-3-1'
CAPABILITIES = ((registry.CAPABILITY, registry.CAPABILITY_SID), ('internetClient', INTERNET_SID))


def internet_capability():
    """Verify the named SDK application capability before enabling it in a token."""
    apis = registry._apis()
    groups, capabilities = C.POINTER(W.LPVOID)(), C.POINTER(W.LPVOID)()
    group_count, capability_count = W.DWORD(), W.DWORD()
    success = apis['kernelbase'].DeriveCapabilitySidsFromName('internetClient', C.byref(groups),
        C.byref(group_count), C.byref(capabilities), C.byref(capability_count))
    try:
        ac._check(success)
        if group_count.value != 1 or capability_count.value != 1 or not groups or not capabilities:
            raise TrainingProcessError('Internet capability derivation returned an unexpected shape.')
        for rows in (groups, capabilities):
            if not rows[0] or not apis['advapi32'].IsValidSid(rows[0]):
                raise TrainingProcessError('Internet capability derivation returned an invalid SID.')
        if (apis['advapi32'].GetLengthSid(capabilities[0]) != 16
                or ac._sid_text(apis, capabilities[0]) != INTERNET_SID):
            raise TrainingProcessError('The derived Internet capability identity changed.')
        result = C.string_at(capabilities[0], 16)
        if result != wfp.sid_bytes(INTERNET_SID):
            raise TrainingProcessError('The Internet capability bytes differ from their identity.')
        return result
    finally:
        for rows, count in ((groups, group_count.value), (capabilities, capability_count.value)):
            if rows:
                for index in range(count if 0 <= count <= 4 else 0):
                    if rows[index]:
                        apis['kernel32'].LocalFree(rows[index])
                apis['kernel32'].LocalFree(rows)


class TokenGroups(C.Structure):
    _fields_ = [('count', W.DWORD), ('groups', base.SidAndAttributes*2)]


def token_record(sid, capabilities):
    if type(capabilities) is not list or len(capabilities) != 2:
        raise TrainingProcessError('Exactly two reviewed WFP candidate capabilities are required.')
    expected = []
    for name, identity in CAPABILITIES:
        matches = [row for row in capabilities if type(row) is dict and row.get('sid') == identity]
        if len(matches) != 1:
            raise TrainingProcessError('The WFP candidate capability set changed.')
        attributes = matches[0].get('attributes')
        if type(attributes) is not int or not attributes & 4 or attributes & ~7:
            raise TrainingProcessError('A WFP candidate capability is disabled or expanded.')
        row = {'name': name, 'sid': identity, 'attributes': attributes}
        if _bytes(row) != _bytes(matches[0]):
            raise TrainingProcessError('The WFP capability record changed.')
        expected.append(row)
    return {'profile': wfp.POLICY, 'appcontainer': True, 'low_integrity': True,
            'least_privileged': True, 'token_control': base.TOKEN_CONTROL,
            'sid_sha256': _sha(sid.encode()), 'capability_count': 2, 'capabilities': expected}


def observe(process, profile):
    value = ac.token_profile(process, include_lpac=True)
    if _bytes(value) != _bytes({'appcontainer': True, 'capability_count': 2,
            'sid': profile.created['sid'], 'low_integrity': True, 'least_privileged': True}):
        raise TrainingProcessError('The process differs from its LPAC/WFP candidate token.')
    apis = registry._apis()
    token = W.HANDLE()
    ac._check(apis['advapi32'].OpenProcessToken(process, 8, C.byref(token)))
    try:
        raw = C.create_string_buffer(4096)
        size = W.DWORD()
        ac._check(apis['advapi32'].GetTokenInformation(token, 30, raw, len(raw), C.byref(size)))
        offset = TokenGroups.groups.offset
        low = offset + C.sizeof(base.SidAndAttributes)*2
        if not low <= size.value <= len(raw) or W.DWORD.from_buffer(raw).value != 2:
            raise TrainingProcessError('The WFP candidate capability buffer is outside its bound.')
        rows = []
        for index in range(2):
            entry = base.SidAndAttributes.from_buffer(raw, offset+C.sizeof(base.SidAndAttributes)*index)
            address = entry.sid or 0
            if not C.addressof(raw)+low <= address <= C.addressof(raw)+size.value-8:
                raise TrainingProcessError('A WFP capability SID points outside its token buffer.')
            length = 8 + 4*C.c_ubyte.from_address(address+1).value
            if length not in (16, 48) or address+length > C.addressof(raw)+size.value:
                raise TrainingProcessError('The WFP capability SID length changed.')
            if not apis['advapi32'].IsValidSid(address):
                raise TrainingProcessError('The WFP candidate token contains an invalid SID.')
            identity = ac._sid_text(apis, address)
            matches = [name for name, sid in CAPABILITIES if sid == identity]
            if len(matches) != 1 or C.string_at(address, length) != wfp.sid_bytes(identity):
                raise TrainingProcessError('An unreviewed WFP capability appeared in the token.')
            rows.append({'name': matches[0], 'sid': identity, 'attributes': entry.attributes})
        return token_record(profile.created['sid'], rows)
    finally:
        apis['kernel32'].CloseHandle(token)


class WindowsWfpJob(ac.WindowsAppContainerJob):
    def __init__(self, name, profile, owner):
        if type(owner) is not wfp.Owner or owner.profile is not profile:
            raise TrainingProcessError('Bind the WFP job to its exact policy owner.')
        owner.verify()
        self.wfp_owner = owner
        self.buffers = (C.create_string_buffer(registry.derive()), C.create_string_buffer(internet_capability()))
        self.entries = (base.SidAndAttributes*2)(*(base.SidAndAttributes(C.addressof(buf), 4) for buf in self.buffers))
        self.capabilities = ac.SecurityCapabilities(profile.sid, C.cast(self.entries, W.LPVOID), 2, 0)
        self.opt_out = W.DWORD(1)
        self.boundaries = []
        self.member_tokens = None
        super().__init__(name, profile)

    def check(self, phase):
        self.wfp_owner.verify()
        if base.loopback_exempt(self.profile.sid):
            raise TrainingProcessError('The WFP test profile has an unexpected loopback exemption.')
        self.boundaries.append({'phase': phase, 'wfp_verified': True, 'loopback_exempt': False})

    def creation_attributes(self):
        self.check('before_create')
        return ((0x20009, self.capabilities), (0x2000f, self.opt_out))

    def spawn_suspended(self, command, cwd, environment):
        if tuple(command) != self.profile.plan.command:
            raise TrainingProcessError('The WFP candidate command changed.')
        self.profile.grant_work(cwd)
        WindowsTrainingJob.spawn_suspended(self, command, cwd, self.profile.environment(environment))
        self.token_checks.append(observe(self.process, self.profile))
        self.check('root_suspended')

    def resume(self):
        self.check('before_resume')
        self.token_checks.append(observe(self.process, self.profile))
        WindowsTrainingJob.resume(self)

    def observe_tokens(self):
        members = self.process_ids()
        if not 1 <= len(members) <= 8:
            raise TrainingProcessError('WFP process membership exceeds its bound.')
        rows = []
        for pid in members:
            process = ac._check(self.api.OpenProcess(0x1000 | 0x100000, False, pid))
            try:
                member = W.BOOL()
                ac._check(self.api.IsProcessInJob(process, self.handle, C.byref(member)))
                if not member.value or self.api.WaitForSingleObject(process, 0) != 258:
                    raise TrainingProcessError('An observed WFP process is not an active owned member.')
                rows.append(observe(process, self.profile))
            finally:
                self.api.CloseHandle(process)
        if self.process_ids() != members:
            raise TrainingProcessError('WFP process membership changed during observation.')
        self.check('members_observed')
        self.member_tokens = tuple(rows)
        return tuple(rows)


def validate_token(value, sid_sha256):
    try:
        if type(sid_sha256) is not str or not re.fullmatch('[0-9a-f]{64}', sid_sha256):
            raise ValueError('package hash')
        expected = token_record('', value['capabilities'])
        expected['sid_sha256'] = sid_sha256
        if _bytes(expected) != _bytes(value):
            raise ValueError('token changed')
    except (KeyError, TypeError, ValueError) as error:
        raise TrainingProcessError('The WFP loading token evidence changed.') from error
    return value


def validate_loading_receipt(value, fingerprint, sid_sha256, member_count):
    try:
        if (type(fingerprint) is not str or not re.fullmatch('[0-9a-f]{64}', fingerprint)
                or type(member_count) is not int or not 1 <= member_count <= 8):
            raise ValueError('process binding')
        intent = wfp.Plan.read(value['intent'])
        if _sha(intent.sid.encode()) != sid_sha256:
            raise ValueError('package binding')
        installed = value['installed']
        objects = installed['objects']
        if type(objects) is not list or len(objects) != 7:
            raise ValueError('object count')
        ids = set()
        for expected, row in zip(intent.objects(), objects):
            if _bytes(expected) != _bytes({key: item for key, item in row.items() if key != 'filter_id'}):
                raise ValueError('filter policy')
            if expected['kind'] == 'filter':
                identity = row.get('filter_id')
                if type(identity) is not int or not 0 < identity < 2**64 or identity in ids:
                    raise ValueError('filter identity')
                ids.add(identity)
            elif 'filter_id' in row:
                raise ValueError('sublayer identity')
        expected_installed = {'schema_version': 1, 'intent_sha256': intent.digest,
                              'objects': objects, 'execution_ready': False}
        deleted = {'schema_version': 1, 'intent_sha256': intent.digest,
                   'owned_objects_absent': True, 'tree_stopped': True}
        roots, members = value['root_tokens'], value['member_tokens']
        if type(roots) is not list or len(roots) != 2 or type(members) is not list or len(members) != member_count:
            raise ValueError('token count')
        for token in [*roots, *members]:
            validate_token(token, sid_sha256)
        expected = {'schema_version': 1, 'policy': wfp.POLICY, 'process_fingerprint': fingerprint,
            'sid_sha256': sid_sha256, 'intent': intent.record(), 'installed': expected_installed, 'deleted': deleted,
            'boundaries': [{'phase': phase, 'wfp_verified': True, 'loopback_exempt': False} for phase in
                ('before_create', 'root_suspended', 'before_resume', 'members_observed', 'after_exit')],
            'root_tokens': roots, 'member_tokens': members, 'broker_isolation_verified': False,
            'offline_execution_verified': False, 'execution_ready': False}
        if _bytes(expected) != _bytes(value):
            raise ValueError('receipt changed')
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise TrainingProcessError('The WFP loading policy receipt is incomplete or changed.') from error
    return value


def loading_receipt(job, installed, deleted):
    owner = job.wfp_owner
    sid_hash = _sha(owner.plan.sid.encode())
    value = {'schema_version': 1, 'policy': wfp.POLICY, 'process_fingerprint': job.profile.plan.fingerprint,
        'sid_sha256': sid_hash, 'intent': owner.plan.record(), 'installed': installed, 'deleted': deleted,
        'boundaries': list(job.boundaries), 'root_tokens': list(job.token_checks),
        'member_tokens': list(job.member_tokens or ()), 'broker_isolation_verified': False,
        'offline_execution_verified': False, 'execution_ready': False}
    return validate_loading_receipt(value, job.profile.plan.fingerprint, sid_hash, len(value['member_tokens']))
