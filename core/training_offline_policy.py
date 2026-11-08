"""Explicit LPAC loading policy and read-only loopback-exemption observations.

Trusted Windows/controller policy only; no firewall changes, network requests,
broker grants or fallback. Finite snapshots do not attest all host mediation.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import re

from . import training_appcontainer as ac
from .training_process import TrainingProcessError, _bytes, _sha

PROFILE = 'windows-lpac-zero-capabilities-v1'
TOKEN_CONTROL = 'all-packages-denied-exact-package-allowed-v1'
MAX_EXEMPTIONS = 4096
BOUNDARIES = ('before_create', 'root_suspended', 'before_resume', 'loading_ready', 'after_exit')


class SidAndAttributes(C.Structure):
    _fields_ = [('sid', W.LPVOID), ('attributes', W.DWORD)]


class GenericMapping(C.Structure):
    _fields_ = [(name,W.DWORD) for name in ('read','write','execute','all')]


def _token_apis():
    apis = ac._apis()
    for name,arguments in (
        ('DuplicateTokenEx',[W.HANDLE,W.DWORD,W.LPVOID,C.c_int,C.c_int,C.POINTER(W.HANDLE)]),
        ('AccessCheck',[W.LPVOID,W.HANDLE,W.DWORD,C.POINTER(GenericMapping),W.LPVOID,
                        C.POINTER(W.DWORD),C.POINTER(W.DWORD),C.POINTER(W.BOOL)]),
    ):
        function = getattr(apis['advapi32'],name)
        function.argtypes,function.restype = arguments,W.BOOL
    return apis


def token_package_access(token,sid):
    """Check LPAC's package-access semantics on a query-only token duplicate.

    Fixed in-memory descriptors only; no impersonation, object opening or ACL
    mutation. The exact-package positive control prevents denial on API failure
    or an unusable token from qualifying. Class 46 is unavailable on some hosts.
    """
    if type(sid) is not str or not re.fullmatch(r'S-1-15-2(?:-\d+){7}',sid):
        raise TrainingProcessError('The package-access control needs an owned AppContainer SID.')
    apis = _token_apis(); duplicate = W.HANDLE()
    ac._check(apis['advapi32'].DuplicateTokenEx(token,8,None,2,2,C.byref(duplicate)))
    try:
        results = []
        for package in ('S-1-15-2-1',sid):
            descriptor = W.LPVOID()
            ac._check(apis['advapi32'].ConvertStringSecurityDescriptorToSecurityDescriptorW(
                'O:SYG:SYD:(A;;0x1;;;WD)(A;;0x1;;;'+package+')',1,C.byref(descriptor),None))
            try:
                mapping = GenericMapping(1,1,1,1)
                privileges = C.create_string_buffer(4096); size = W.DWORD(len(privileges))
                granted = W.DWORD(); allowed = W.BOOL()
                ac._check(apis['advapi32'].AccessCheck(descriptor,duplicate,1,C.byref(mapping),
                    privileges,C.byref(size),C.byref(granted),C.byref(allowed)))
                if not 8 <= size.value <= len(privileges) or W.DWORD.from_buffer(privileges).value != 0 or allowed.value not in (0,1) or granted.value != int(bool(allowed.value)):
                    raise TrainingProcessError('The package-access token control is inconsistent.')
                results.append(bool(allowed.value))
            finally:
                apis['kernel32'].LocalFree(descriptor)
        if results[1] is not True:
            raise TrainingProcessError('The exact-package positive access control did not pass.')
        return results[0] is False
    finally:
        apis['kernel32'].CloseHandle(duplicate)


def _apis():
    apis = ac._apis()
    apis['firewallapi'] = C.WinDLL('FirewallAPI.dll', use_last_error=True)
    for library, name, arguments, result in (
        ('firewallapi', 'NetworkIsolationGetAppContainerConfig', [C.POINTER(W.DWORD), C.POINTER(C.POINTER(SidAndAttributes))], W.DWORD),
        ('kernel32', 'GetProcessHeap', [], W.HANDLE),
        ('kernel32', 'HeapFree', [W.HANDLE, W.DWORD, W.LPVOID], W.BOOL),
        ('advapi32', 'IsValidSid', [W.LPVOID], W.BOOL),
        ('advapi32', 'EqualSid', [W.LPVOID, W.LPVOID], W.BOOL),
    ):
        function = getattr(apis[library], name)
        function.argtypes, function.restype = arguments, result
    return apis


def _exempt(count, rows, expected_sid, apis):
    if type(count) is not int or not 0 <= count <= MAX_EXEMPTIONS or (count and not rows):
        raise TrainingProcessError('Loopback configuration is unavailable or exceeds its bound.')
    matches = False
    for index in range(count):
        sid = rows[index].sid
        if not sid or not apis['advapi32'].IsValidSid(sid):
            raise TrainingProcessError('Loopback configuration contains an invalid identity.')
        matches |= bool(apis['advapi32'].EqualSid(sid, expected_sid))
    return matches


def loopback_exempt(expected_sid):
    """Inspect only membership of the owned SID; retain no other identities.

    The SDK owns the returned allocation shape. Free bounded returned SIDs and
    the array with the process heap. An invalid count holds loading and frees
    only the array: a bounded failure must never traverse an unbounded buffer.
    """
    apis = _apis()
    if not expected_sid or not apis['advapi32'].IsValidSid(expected_sid):
        raise TrainingProcessError('The owned profile SID is unavailable.')
    count = W.DWORD(); rows = C.POINTER(SidAndAttributes)()
    error = apis['firewallapi'].NetworkIsolationGetAppContainerConfig(C.byref(count), C.byref(rows))
    try:
        if error:
            raise TrainingProcessError('Windows loopback configuration could not be checked.')
        return _exempt(count.value, rows, expected_sid, apis)
    finally:
        if rows:
            heap = apis['kernel32'].GetProcessHeap()
            freed = True
            for index in range(count.value if count.value <= MAX_EXEMPTIONS else 0):
                if rows[index].sid:
                    freed = bool(apis['kernel32'].HeapFree(heap, 0, rows[index].sid)) and freed
            freed = bool(apis['kernel32'].HeapFree(heap, 0, rows)) and freed
            if not freed:
                raise TrainingProcessError('Windows loopback configuration cleanup is unverified.')


def require_lpac(process, sid):
    value = ac.token_profile(process, include_lpac=True)
    if value.get('least_privileged') is not True:
        raise TrainingProcessError('The owned process is not a least-privileged AppContainer.')
    token = ac.require_token({key: item for key, item in value.items() if key != 'least_privileged'}, sid)
    return {**token, 'policy': PROFILE, 'least_privileged': True, 'token_control': TOKEN_CONTROL}


def validate_receipt(value, process_fingerprint, sid_sha256, member_count):
    if type(member_count) is not int or not 1 <= member_count <= 8 or any(
        type(digest) is not str or not re.fullmatch('[0-9a-f]{64}', digest)
        for digest in (process_fingerprint, sid_sha256)
    ):
        raise TrainingProcessError('The least-privileged policy receipt binding is invalid.')
    token = {'profile': ac.PROFILE, 'appcontainer': True, 'capability_count': 0,
        'sid_sha256': sid_sha256, 'low_integrity': True, 'policy': PROFILE, 'least_privileged': True,
        'token_control': TOKEN_CONTROL}
    expected = {'schema_version': 1, 'policy': PROFILE, 'process_fingerprint': process_fingerprint,
        'sid_sha256': sid_sha256, 'boundaries': [{'boundary': phase, 'loopback_exempt': False} for phase in BOUNDARIES],
        'root_tokens': [token, token], 'member_tokens': [token] * member_count,
        'host_policy_unchanged_assumption': True, 'broker_isolation_verified': False,
        'offline_execution_verified': False, 'execution_ready': False}
    if _bytes(value) != _bytes(expected):
        raise TrainingProcessError('The least-privileged policy receipt is incomplete or changed.')
    return value


class LoadingPolicy:
    def __init__(self, profile):
        self.profile = profile
        self.boundaries = []
        self.root_tokens = []
        self.member_tokens = None

    def check(self, phase):
        self.profile.validate()
        if len(self.boundaries) >= len(BOUNDARIES) or phase != BOUNDARIES[len(self.boundaries)]:
            raise TrainingProcessError('The offline loading policy boundary is out of order.')
        if loopback_exempt(self.profile.sid):
            raise TrainingProcessError('The owned AppContainer has a loopback exemption; loading remains held.')
        self.boundaries.append({'boundary': phase, 'loopback_exempt': False})

    def receipt(self):
        sid_hash = _sha(self.profile.created['sid'].encode())
        value = {'schema_version': 1, 'policy': PROFILE, 'process_fingerprint': self.profile.plan.fingerprint,
            'sid_sha256': sid_hash, 'boundaries': list(self.boundaries),
            'root_tokens': list(self.root_tokens), 'member_tokens': list(self.member_tokens or ()),
            'host_policy_unchanged_assumption': True, 'broker_isolation_verified': False,
            'offline_execution_verified': False, 'execution_ready': False}
        return validate_receipt(value, self.profile.plan.fingerprint, sid_hash, len(value['member_tokens']))


class WindowsLpacJob(ac.WindowsAppContainerJob):
    def __init__(self, name, profile, policy):
        if type(policy) is not LoadingPolicy or policy.profile is not profile:
            raise TrainingProcessError('Bind the exact owned profile to its loading policy.')
        self.loading_policy = policy
        self.all_packages_policy = W.DWORD(1)  # PROCESS_CREATION_ALL_APPLICATION_PACKAGES_OPT_OUT
        super().__init__(name, profile)

    def creation_attributes(self):
        self.loading_policy.check('before_create')
        return (*super().creation_attributes(), (0x2000f, self.all_packages_policy))

    def spawn_suspended(self, command, cwd, environment):
        super().spawn_suspended(command, cwd, environment)
        self.loading_policy.root_tokens.append(require_lpac(self.process, self.profile.created['sid']))
        self.loading_policy.check('root_suspended')

    def resume(self):
        self.loading_policy.check('before_resume')
        self.loading_policy.root_tokens.append(require_lpac(self.process, self.profile.created['sid']))
        super().resume()

    def observe_tokens(self):
        tokens = super().observe_tokens()
        members = self.process_ids(); observations = []
        for pid in members:
            process = ac._check(self.api.OpenProcess(0x1000 | 0x100000, False, pid))
            try:
                member = W.BOOL()
                ac._check(self.api.IsProcessInJob(process, self.handle, C.byref(member)))
                if not member.value or self.api.WaitForSingleObject(process, 0) != 258:
                    raise TrainingProcessError('The observed LPAC member is no longer active.')
                observations.append(require_lpac(process, self.profile.created['sid']))
            finally:
                self.api.CloseHandle(process)
        if self.process_ids() != members or len(tokens) != len(observations):
            raise TrainingProcessError('The observed LPAC process membership changed.')
        self.loading_policy.check('loading_ready')
        self.loading_policy.member_tokens = tuple(observations)
        return tokens
