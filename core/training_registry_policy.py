"""Generated-loading candidate: LPAC with exactly the registryRead capability.

No production training authority, network capability or host permission change.
Keep the ordinary and zero-capability owners and their receipts separate.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import re

from . import training_appcontainer as ac
from . import training_offline_policy as base
from .training_process import TrainingProcessError, _bytes, _sha
from .training_process_windows import WindowsTrainingJob

PROFILE = 'windows-lpac-registry-read-candidate-v1'
CAPABILITY = 'registryRead'
# Bind SDK derivation and every token to the reviewed named application SID.
CAPABILITY_SID = 'S-1-15-3-1024-1065365936-1281604716-3511738428-1654721687-432734479-3232135806-4053264122-3456934681'


def _apis():
    apis = ac._apis()
    apis['kernelbase'] = C.WinDLL('kernelbase', use_last_error=True)
    for library, name, args, result in (
        ('kernelbase', 'DeriveCapabilitySidsFromName', [W.LPCWSTR,
            C.POINTER(C.POINTER(W.LPVOID)), C.POINTER(W.DWORD),
            C.POINTER(C.POINTER(W.LPVOID)), C.POINTER(W.DWORD)], W.BOOL),
        ('advapi32', 'IsValidSid', [W.LPVOID], W.BOOL),
        ('advapi32', 'GetLengthSid', [W.LPVOID], W.DWORD),
    ):
        function = getattr(apis[library], name)
        function.argtypes, function.restype = args, result
    return apis


def derive():
    """Copy the exact SDK application SID before freeing both returned arrays."""
    apis = _apis()
    groups, capabilities = C.POINTER(W.LPVOID)(), C.POINTER(W.LPVOID)()
    group_count, capability_count = W.DWORD(), W.DWORD()
    success = apis['kernelbase'].DeriveCapabilitySidsFromName(CAPABILITY, C.byref(groups),
        C.byref(group_count), C.byref(capabilities), C.byref(capability_count))
    try:
        ac._check(success)
        if group_count.value != 1 or capability_count.value != 1 or not groups or not capabilities:
            raise TrainingProcessError('Registry-read derivation returned an unexpected shape.')
        for rows in (groups, capabilities):
            if not rows[0] or not apis['advapi32'].IsValidSid(rows[0]):
                raise TrainingProcessError('Registry-read derivation returned an invalid SID.')
        if ac._sid_text(apis, capabilities[0]) != CAPABILITY_SID or apis['advapi32'].GetLengthSid(capabilities[0]) != 48:
            raise TrainingProcessError('The derived registry-read capability identity changed.')
        return C.string_at(capabilities[0], 48)
    finally:
        for rows, count in ((groups, group_count.value), (capabilities, capability_count.value)):
            if rows:
                # Malformed SDK counts must not cause unbounded pointer traversal.
                for index in range(count if 0 <= count <= 4 else 0):
                    if rows[index]: apis['kernel32'].LocalFree(rows[index])
                apis['kernel32'].LocalFree(rows)


class TokenGroups(C.Structure):
    _fields_ = [('count', W.DWORD), ('groups', base.SidAndAttributes * 1)]


def token_record(sid_sha256, attributes):
    if type(sid_sha256) is not str or not re.fullmatch('[0-9a-f]{64}', sid_sha256):
        raise TrainingProcessError('The registry-read package binding is invalid.')
    if type(attributes) is not int or attributes & 4 == 0 or attributes & ~7:
        raise TrainingProcessError('Registry-read capability attributes are disabled or unsupported.')
    return {'profile': PROFILE, 'appcontainer': True, 'capability_count': 1,
        'sid_sha256': sid_sha256, 'low_integrity': True, 'least_privileged': True,
        'token_control': base.TOKEN_CONTROL,
        'capabilities': [{'name': CAPABILITY, 'sid': CAPABILITY_SID, 'attributes': attributes}]}


def validate_token(value, sid_sha256):
    try:
        attributes = value['capabilities'][0]['attributes']
        expected = token_record(sid_sha256, attributes)
        if _bytes(value) != _bytes(expected): raise ValueError('different token')
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise TrainingProcessError('The registry-read token evidence changed.') from error
    return value


def observe(process, profile):
    value = ac.token_profile(process, include_lpac=True)
    if _bytes(value) != _bytes({'appcontainer': True, 'capability_count': 1,
            'sid': profile.created['sid'], 'low_integrity': True, 'least_privileged': True}):
        raise TrainingProcessError('The process differs from its registry-read LPAC profile.')
    apis = _apis(); token = W.HANDLE()
    ac._check(apis['advapi32'].OpenProcessToken(process, 8, C.byref(token)))
    try:
        raw = C.create_string_buffer(4096); size = W.DWORD()
        ac._check(apis['advapi32'].GetTokenInformation(token, 30, raw, len(raw), C.byref(size)))
        offset = TokenGroups.groups.offset
        if not offset + C.sizeof(base.SidAndAttributes) <= size.value <= len(raw):
            raise TrainingProcessError('Registry-read token data exceeds its bound.')
        if W.DWORD.from_buffer(raw).value != 1:
            raise TrainingProcessError('The registry-read token has additional or missing capabilities.')
        row = base.SidAndAttributes.from_buffer(raw, offset)
        low = C.addressof(raw) + offset + C.sizeof(base.SidAndAttributes)
        high = C.addressof(raw) + size.value
        address = row.sid or 0
        if not low <= address <= high - 48 or C.c_ubyte.from_address(address + 1).value != 10:
            raise TrainingProcessError('The registry-read SID is outside its returned token buffer.')
        if not apis['advapi32'].IsValidSid(address) or ac._sid_text(apis, address) != CAPABILITY_SID:
            raise TrainingProcessError('The observed registry-read capability identity changed.')
        return token_record(_sha(profile.created['sid'].encode()), row.attributes)
    finally:
        apis['kernel32'].CloseHandle(token)


def validate_receipt(value, process_fingerprint, sid_sha256, member_count):
    if type(member_count) is not int or not 1 <= member_count <= 8 or type(process_fingerprint) is not str or not re.fullmatch('[0-9a-f]{64}', process_fingerprint):
        raise TrainingProcessError('The registry-read policy binding is invalid.')
    try:
        roots, members = value['root_tokens'], value['member_tokens']
        if type(roots) is not list or len(roots) != 2 or type(members) is not list or len(members) != member_count:
            raise ValueError('token count')
        for token in [*roots, *members]: validate_token(token, sid_sha256)
        expected = {'schema_version': 1, 'policy': PROFILE, 'process_fingerprint': process_fingerprint,
            'sid_sha256': sid_sha256, 'boundaries': [{'boundary': phase, 'loopback_exempt': False} for phase in base.BOUNDARIES],
            'root_tokens': roots, 'member_tokens': members,
            'host_policy_unchanged_assumption': True, 'broker_isolation_verified': False,
            'offline_execution_verified': False, 'execution_ready': False}
        if _bytes(value) != _bytes(expected): raise ValueError('policy mismatch')
    except (KeyError, TypeError, ValueError) as error:
        raise TrainingProcessError('The registry-read policy receipt is incomplete or changed.') from error
    return value


class LoadingPolicy(base.LoadingPolicy):
    """Reuse the exact five ordered, read-only exemption checks."""
    def receipt(self):
        sid_hash = _sha(self.profile.created['sid'].encode())
        value = {'schema_version': 1, 'policy': PROFILE, 'process_fingerprint': self.profile.plan.fingerprint,
            'sid_sha256': sid_hash, 'boundaries': list(self.boundaries),
            'root_tokens': list(self.root_tokens), 'member_tokens': list(self.member_tokens or ()),
            'host_policy_unchanged_assumption': True, 'broker_isolation_verified': False,
            'offline_execution_verified': False, 'execution_ready': False}
        return validate_receipt(value, self.profile.plan.fingerprint, sid_hash, len(value['member_tokens']))


class WindowsRegistryReadJob(ac.WindowsAppContainerJob):
    def __init__(self, name, profile, policy):
        if type(policy) is not LoadingPolicy or policy.profile is not profile:
            raise TrainingProcessError('Bind the registry-read policy to its exact owned profile.')
        self.loading_policy = policy
        self.buffer = C.create_string_buffer(derive())
        self.entry = base.SidAndAttributes(C.cast(self.buffer, W.LPVOID), 4)
        self.capabilities = ac.SecurityCapabilities(profile.sid, C.cast(C.pointer(self.entry), W.LPVOID), 1, 0)
        self.opt_out = W.DWORD(1)
        super().__init__(name, profile)

    def creation_attributes(self):
        self.loading_policy.check('before_create')
        return ((0x20009, self.capabilities), (0x2000f, self.opt_out))

    def spawn_suspended(self, command, cwd, environment):
        if tuple(command) != self.profile.plan.command:
            raise TrainingProcessError('The registry-read command changed.')
        self.profile.grant_work(cwd)
        WindowsTrainingJob.spawn_suspended(self, command, cwd, self.profile.environment(environment))
        token = observe(self.process, self.profile)
        self.token_checks.append(token); self.loading_policy.root_tokens.append(token)
        self.loading_policy.check('root_suspended')

    def resume(self):
        self.loading_policy.check('before_resume')
        token = observe(self.process, self.profile)
        self.token_checks.append(token); self.loading_policy.root_tokens.append(token)
        WindowsTrainingJob.resume(self)

    def observe_tokens(self):
        members = self.process_ids(); observations = []
        if not 1 <= len(members) <= 8:
            raise TrainingProcessError('Registry-read process membership exceeds its bound.')
        for pid in members:
            process = ac._check(self.api.OpenProcess(0x1000 | 0x100000, False, pid))
            try:
                member = W.BOOL()
                ac._check(self.api.IsProcessInJob(process, self.handle, C.byref(member)))
                if not member.value or self.api.WaitForSingleObject(process, 0) != 258:
                    raise TrainingProcessError('The observed registry-read process is no longer active.')
                observations.append(observe(process, self.profile))
            finally:
                self.api.CloseHandle(process)
        if self.process_ids() != members:
            raise TrainingProcessError('The observed registry-read process membership changed.')
        self.loading_policy.check('loading_ready')
        self.loading_policy.member_tokens = tuple(observations)
        return tuple(observations)
