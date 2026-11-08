"""Explicit, zero-capability Windows profiles for generated qualification.

Only newly owned staging/work directories receive grants. Profile cleanup is
bound to a receipt and an observed stopped Job; this does not enable training.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import os
import json
from pathlib import Path
import re
import stat
import threading
import time
from uuid import uuid4

from .training_process import TrainingProcessError, _bytes, _sha, _write_new, _read, _directory, _lease
from .training_process_windows import WindowsTrainingJob, _check
from .training_source_io import _assert_unredirected

PROFILE = 'windows-appcontainer-zero-capabilities-v1'
PREFIX = 'CollectiGen.Training.'
CREATED = 'appcontainer-created.json'
DELETED = 'appcontainer-deleted.json'
RESERVED = 'appcontainer-reserved.json'
DELETE_INTENT = 'appcontainer-delete-intent.json'


class SecurityCapabilities(C.Structure):
    _fields_ = [('sid', W.LPVOID), ('capabilities', W.LPVOID), ('count', W.DWORD), ('reserved', W.DWORD)]


def _apis():
    if os.name != 'nt':
        raise TrainingProcessError('AppContainer qualification requires Windows.')
    libraries = {name: C.WinDLL(name, use_last_error=True) for name in ('kernel32', 'advapi32', 'userenv', 'ole32')}
    signatures = {
        'kernel32': {'CloseHandle':([W.HANDLE],W.BOOL),'LocalFree':([W.LPVOID],W.LPVOID)},
        'ole32': {'CoTaskMemFree':([W.LPVOID],None)},
        'userenv': {
            'CreateAppContainerProfile':([W.LPCWSTR,W.LPCWSTR,W.LPCWSTR,W.LPVOID,W.DWORD,C.POINTER(W.LPVOID)],C.c_long),
            'DeleteAppContainerProfile':([W.LPCWSTR],C.c_long),
            'DeriveAppContainerSidFromAppContainerName':([W.LPCWSTR,C.POINTER(W.LPVOID)],C.c_long),
            'GetAppContainerFolderPath':([W.LPCWSTR,C.POINTER(W.LPWSTR)],C.c_long)},
        'advapi32': {
            'FreeSid':([W.LPVOID],W.LPVOID),
            'ConvertSidToStringSidW':([W.LPVOID,C.POINTER(W.LPWSTR)],W.BOOL),
            'OpenProcessToken':([W.HANDLE,W.DWORD,C.POINTER(W.HANDLE)],W.BOOL),
            'GetTokenInformation':([W.HANDLE,C.c_int,W.LPVOID,W.DWORD,C.POINTER(W.DWORD)],W.BOOL),
            'ConvertStringSecurityDescriptorToSecurityDescriptorW':([W.LPCWSTR,W.DWORD,C.POINTER(W.LPVOID),C.POINTER(W.DWORD)],W.BOOL),
            'GetSecurityDescriptorDacl':([W.LPVOID,C.POINTER(W.BOOL),C.POINTER(W.LPVOID),C.POINTER(W.BOOL)],W.BOOL),
            'GetSecurityDescriptorSacl':([W.LPVOID,C.POINTER(W.BOOL),C.POINTER(W.LPVOID),C.POINTER(W.BOOL)],W.BOOL),
            'GetSecurityDescriptorControl':([W.LPVOID,C.POINTER(W.WORD),C.POINTER(W.DWORD)],W.BOOL),
            'SetNamedSecurityInfoW':([W.LPWSTR,C.c_int,W.DWORD,W.LPVOID,W.LPVOID,W.LPVOID,W.LPVOID],W.DWORD),
            'GetNamedSecurityInfoW':([W.LPWSTR,C.c_int,W.DWORD,W.LPVOID,W.LPVOID,C.POINTER(W.LPVOID),C.POINTER(W.LPVOID),C.POINTER(W.LPVOID)],W.DWORD),
            'GetAclInformation':([W.LPVOID,W.LPVOID,W.DWORD,C.c_int],W.BOOL),
            'GetAce':([W.LPVOID,W.DWORD,C.POINTER(W.LPVOID)],W.BOOL)}}
    for library, entries in signatures.items():
        for name, (args, result) in entries.items():
            fn = getattr(libraries[library],name); fn.argtypes,fn.restype=args,result
    return libraries


def _sid_text(apis, sid):
    text = W.LPWSTR()
    _check(apis['advapi32'].ConvertSidToStringSidW(sid,C.byref(text)))
    try: return text.value
    finally: apis['kernel32'].LocalFree(text)


def token_profile(process, *, include_lpac=False):
    """Parent-side bounded token observation; never trusts a child's claim."""
    apis = _apis(); token = W.HANDLE()
    _check(apis['advapi32'].OpenProcessToken(process,10 if include_lpac else 8,C.byref(token)))
    try:
        def info(kind):
            raw=C.create_string_buffer(4096); size=W.DWORD()
            _check(apis['advapi32'].GetTokenInformation(token,kind,raw,len(raw),C.byref(size)))
            if not 0 < size.value <= len(raw): raise TrainingProcessError('Token observation exceeds its bound.')
            return raw
        is_container=W.DWORD.from_buffer(info(29)).value
        count=W.DWORD.from_buffer(info(30)).value
        sid_buffer=info(31); sid=W.LPVOID.from_buffer(sid_buffer).value
        integrity_buffer=info(25); integrity_sid=W.LPVOID.from_buffer(integrity_buffer).value
        identity=_sid_text(apis,sid) if sid else ''
        integrity=_sid_text(apis,integrity_sid)
        value = {'appcontainer':is_container==1,'capability_count':count,
                 'sid':identity,'low_integrity':integrity=='S-1-16-4096'}
        if include_lpac:
            from .training_offline_policy import token_package_access
            value['least_privileged'] = token_package_access(token,identity)
        return value
    finally: apis['kernel32'].CloseHandle(token)


def require_token(value, sid):
    if value != {'appcontainer':True,'capability_count':0,'sid':sid,'low_integrity':True}:
        raise TrainingProcessError('The owned process token does not match its zero-capability profile.')
    return {'profile':PROFILE,'appcontainer':True,'capability_count':0,
            'sid_sha256':_sha(sid.encode()),'low_integrity':True}


def _acl_entries(apis, acl):
    class Size(C.Structure):
        _fields_=[('count',W.DWORD),('used',W.DWORD),('free',W.DWORD)]
    size=Size();_check(apis['advapi32'].GetAclInformation(acl,C.byref(size),C.sizeof(size),2))
    if not 0 < size.used <= 4096: raise TrainingProcessError('The owned directory ACL is unavailable.')
    if size.count > 32:
        raise TrainingProcessError('The owned directory ACL exceeds its entry bound.')
    entries = []
    for index in range(size.count):
        ace = W.LPVOID()
        _check(apis['advapi32'].GetAce(acl,index,C.byref(ace)))
        length = C.c_ushort.from_address(ace.value + 2).value
        if not 4 <= length <= size.used:
            raise TrainingProcessError('The owned directory ACL entry is invalid.')
        entries.append(C.string_at(ace,length))
    # ACL allocation size/padding may change when Windows stores the ACL.
    # Every actual ACE, including its type, flags, mask and SID, must match.
    return tuple(entries)


def _grant_owned_directory(path, sid, *, writable, max_entries=20000):
    """Caller supplies one newly owned directory, never an installed/user tree."""
    if type(max_entries) is not int or not 1 <= max_entries <= 200000:
        raise TrainingProcessError('Owned staging needs a bounded entry inventory.')
    path=Path(path);_assert_unredirected(path);before=_directory(path)
    # Inherited ACL updates traverse this tree. Reject redirections and hard
    # links before touching it, so grants cannot reach an installed resource.
    entries = 0
    def traversal_error(error):
        raise TrainingProcessError('Owned staging cannot be completely checked before granting access.') from error
    for parent, directories, files in os.walk(path, followlinks=False, onerror=traversal_error):
        for name in directories + files:
            child = Path(parent)/name
            _assert_unredirected(child)
            state = child.stat(follow_symlinks=False)
            entries += 1
            if entries > max_entries or (not stat.S_ISDIR(state.st_mode) and
                    (not stat.S_ISREG(state.st_mode) or state.st_nlink != 1)):
                raise TrainingProcessError('Owned staging has unsupported links, files or size.')
    apis=_apis();descriptor=W.LPVOID();size=W.DWORD()
    # Preserve controller/OS ownership. Container writes carry a low mandatory
    # label only within the execution workspace; code/input grants are RX.
    access='FA' if writable else '0x1200a9'  # mapped FILE_GENERIC_READ | EXECUTE
    sddl=f'D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)(A;OICI;{access};;;{sid})'
    if writable:sddl+='S:(ML;OICI;NW;;;LW)'
    _check(apis['advapi32'].ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl,1,C.byref(descriptor),C.byref(size)))
    dacl=W.LPVOID();sacl=W.LPVOID();present=W.BOOL();defaulted=W.BOOL()
    try:
        _check(apis['advapi32'].GetSecurityDescriptorDacl(descriptor,C.byref(present),C.byref(dacl),C.byref(defaulted)))
        if writable:_check(apis['advapi32'].GetSecurityDescriptorSacl(descriptor,C.byref(present),C.byref(sacl),C.byref(defaulted)))
        flags=4 | 0x80000000 | (0x10 if writable else 0)
        error=apis['advapi32'].SetNamedSecurityInfoW(str(path),1,flags,None,None,dacl,sacl)
        if error:raise C.WinError(error)
        saved=W.LPVOID();actual_dacl=W.LPVOID();actual_sacl=W.LPVOID()
        error=apis['advapi32'].GetNamedSecurityInfoW(str(path),1,4 | (0x10 if writable else 0),None,None,C.byref(actual_dacl),C.byref(actual_sacl),C.byref(saved))
        if error:raise C.WinError(error)
        try:
            control = W.WORD(); revision = W.DWORD()
            _check(apis['advapi32'].GetSecurityDescriptorControl(saved,C.byref(control),C.byref(revision)))
            if not control.value & 0x1000:  # SE_DACL_PROTECTED
                raise TrainingProcessError('The owned directory DACL remains inheritable from its parent.')
            if _acl_entries(apis,actual_dacl)!=_acl_entries(apis,dacl) or (writable and _acl_entries(apis,actual_sacl)!=_acl_entries(apis,sacl)):
                raise TrainingProcessError('Owned directory permissions failed read-back verification.')
        finally:apis['kernel32'].LocalFree(saved)
        if _directory(path)!=before:raise TrainingProcessError('The owned directory changed while applying permissions.')
    finally:apis['kernel32'].LocalFree(descriptor)


def _validate_created(value):
    fields={'schema_version','profile','nonce','name','sid','owner_name','process_fingerprint','reservation_sha256','workspace_identity'}
    if type(value) is not dict or set(value)!=fields or type(value['schema_version']) is not int or value['schema_version']!=1 or value['profile']!=PROFILE:
        raise TrainingProcessError('The AppContainer receipt is invalid.')
    if not re.fullmatch('[0-9a-f]{32}',str(value['nonce'])) or value['name']!=PREFIX+value['nonce']:
        raise TrainingProcessError('The profile is not a uniquely owned training profile.')
    if not re.fullmatch(r'S-1-15-2(?:-\d+){7}',str(value['sid'])) or not re.fullmatch(r'Local\\CollectiGen\.Trainer\.[0-9a-f]{32}',str(value['owner_name'])):
        raise TrainingProcessError('The profile or process identity changed.')
    if any(not re.fullmatch('[0-9a-f]{64}',str(value[key])) for key in ('process_fingerprint','reservation_sha256')):
        raise TrainingProcessError('The AppContainer binding is unavailable.')
    identity = value['workspace_identity']
    if type(identity) is not list or len(identity) != 2 or any(type(part) is not int or part < 0 for part in identity):
        raise TrainingProcessError('The AppContainer workspace identity is invalid.')
    return value


class AppContainerWorkspace:
    """New controller-owned staging only; never wrap an existing directory."""
    def __init__(self, parent, name):
        parent = Path(parent).absolute()
        _directory(parent)
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}', name):
            raise TrainingProcessError('Choose one new qualification workspace name.')
        self.directory = parent/name
        self.directory.mkdir(exist_ok=False)
        self.identity = _directory(self.directory)
        self.children = {}
        for child in ('runtime', 'inputs', 'runs'):
            path = self.directory/child
            path.mkdir()
            self.children[child] = _directory(path)

    def validate(self):
        if _directory(self.directory) != self.identity or any(
                _directory(self.directory/name) != identity for name, identity in self.children.items()):
            raise TrainingProcessError('The owned qualification workspace changed.')


class AppContainerProfile:
    def __init__(self, workspace, plan):
        if type(workspace) is not AppContainerWorkspace:
            raise TrainingProcessError('A newly owned qualification workspace is required.')
        workspace.validate()
        plan.validate()
        if Path(plan.root) != workspace.directory/'runs':
            raise TrainingProcessError('The process plan is outside its owned qualification workspace.')
        self.workspace = workspace
        self.directory=workspace.directory;self.identity=workspace.identity
        self.plan=plan;self.sid=W.LPVOID();self.apis=_apis();self.created=None
        self.deleted = False
        self.owner_active = False
        self.owner_claimed_once = False
        self.mutex = threading.RLock()
        self.lease = _lease(self.directory)
        self.lease.__enter__()
        try:
            self._create()
        except BaseException:
            self.close()
            raise

    def _create(self):
        plan = self.plan
        nonce=uuid4().hex
        reserved={'schema_version':1,'profile':PROFILE,'nonce':nonce,'name':PREFIX+nonce,
                  'owner_name':plan.owner_name,'process_fingerprint':plan.fingerprint,
                  'workspace_identity':list(self.identity)}
        _write_new(self.directory/RESERVED,_bytes(reserved))
        result=self.apis['userenv'].CreateAppContainerProfile(reserved['name'],'CollectiGen training qualification',
            'Owned generated qualification profile',None,0,C.byref(self.sid))
        if result!=0:raise TrainingProcessError('A fresh AppContainer profile could not be created; existing profiles are never adopted.')
        # If receipt persistence fails, retain the reservation for explicit
        # recovery review. Never delete a profile whose ownership is ambiguous.
        self.created=_validate_created({**reserved,'sid':_sid_text(self.apis,self.sid),'reservation_sha256':_sha(_bytes(reserved))})
        _write_new(self.directory/CREATED,_bytes(self.created))
        self.capabilities=SecurityCapabilities(self.sid,None,0,0)

    def validate(self):
        if self.lease is None or self.deleted or not self.sid:
            raise TrainingProcessError('The owned AppContainer profile is no longer available.')
        self.workspace.validate()
        if _read(self.directory/CREATED) != _bytes(self.created) or any(
                (self.directory/name).exists() for name in (DELETED, DELETE_INTENT)):
            raise TrainingProcessError('The owned AppContainer receipt changed.')
        reserved = {key:value for key,value in self.created.items() if key not in {'sid','reservation_sha256'}}
        if _read(self.directory/RESERVED) != _bytes(reserved):
            raise TrainingProcessError('The owned AppContainer reservation changed.')

    def environment(self, values):
        self.validate()
        folder=W.LPWSTR()
        result=self.apis['userenv'].GetAppContainerFolderPath(self.created['sid'],C.byref(folder))
        if result!=0:raise TrainingProcessError('The owned AppContainer environment is unavailable.')
        try:return {**values,'LOCALAPPDATA':folder.value,'APPDATA':folder.value}
        finally:self.apis['ole32'].CoTaskMemFree(folder)

    def grant_staging(self, path, *, max_entries=20000):
        self.validate()
        path=Path(path)
        if path.parent!=self.directory or path.name not in {'runtime','inputs'}:
            raise TrainingProcessError('Grant only newly owned runtime or input staging directories.')
        if _directory(self.directory)!=self.identity:raise TrainingProcessError('The profile workspace changed.')
        _grant_owned_directory(path,self.created['sid'],writable=False,max_entries=max_entries)

    def grant_work(self, path):
        self.validate()
        if Path(path)!=self.plan.destination/'work':raise TrainingProcessError('The process workspace differs from its plan.')
        _grant_owned_directory(path,self.created['sid'],writable=True)

    def cleanup(self):
        with self.mutex:
            if self.owner_active:
                raise TrainingProcessError('The owned process owner is still active; retain its profile.')
            if self.lease is None:
                raise TrainingProcessError('Recover a closed profile using its saved receipt.')
            result = _recover_locked(self.directory,_sha(_bytes(self.created)))
            self.deleted = True
            return result

    def claim_owner(self):
        with self.mutex:
            self.validate()
            if self.owner_claimed_once:
                raise TrainingProcessError('The AppContainer already had a process owner; retry with a fresh workspace.')
            self.owner_active = True
            self.owner_claimed_once = True

    def release_owner(self):
        with self.mutex:
            self.owner_active = False

    def close(self):
        with self.mutex:
            if self.owner_active:
                raise TrainingProcessError('The owned process owner is still active; retain its profile.')
            if self.sid:self.apis['advapi32'].FreeSid(self.sid);self.sid=W.LPVOID()
            if self.lease is not None:
                self.lease.__exit__(None,None,None)
                self.lease = None

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        try:
            self.cleanup()
        finally:
            self.close()


def recover_profile(directory, expected_created_sha256):
    """Delete only the receipt-bound new profile after its named Job is absent."""
    directory = Path(directory)
    _directory(directory)
    with _lease(directory):
        return _recover_locked(directory, expected_created_sha256)


def _recover_locked(directory, expected_created_sha256):
    directory=Path(directory);before=_directory(directory)
    raw=_read(directory/CREATED)
    if _sha(raw)!=expected_created_sha256:raise TrainingProcessError('The AppContainer recovery receipt changed.')
    created=_validate_created(json.loads(raw))
    if tuple(created['workspace_identity']) != before:
        raise TrainingProcessError('The recovery receipt belongs to a different workspace.')
    reserved=_read(directory/RESERVED)
    if _sha(reserved)!=created['reservation_sha256'] or json.loads(reserved)!={key:value for key,value in created.items() if key not in {'sid','reservation_sha256'}}:
        raise TrainingProcessError('The AppContainer reservation changed.')
    if WindowsTrainingJob.active_named(created['owner_name'])!=0:
        raise TrainingProcessError('The owned process tree is still active; retain its profile.')
    deleted={'schema_version':1,'profile':PROFILE,'created_sha256':expected_created_sha256,'profile_deleted':True}
    if (directory/DELETED).exists():
        if _read(directory/DELETED)!=_bytes(deleted):raise TrainingProcessError('The cleanup receipt changed.')
        return deleted
    apis=_apis();sid=W.LPVOID()
    if apis['userenv'].DeriveAppContainerSidFromAppContainerName(created['name'],C.byref(sid))!=0:
        raise TrainingProcessError('The owned profile identity cannot be derived.')
    try:
        if _sid_text(apis,sid)!=created['sid']:raise TrainingProcessError('The profile SID differs from its receipt.')
    finally:apis['advapi32'].FreeSid(sid)
    if _directory(directory)!=before:raise TrainingProcessError('The cleanup workspace changed.')
    intent = _bytes({'schema_version':1,'profile':PROFILE,'created_sha256':expected_created_sha256})
    if (directory/DELETE_INTENT).exists():
        if _read(directory/DELETE_INTENT) != intent:
            raise TrainingProcessError('The cleanup intent changed.')
    else:
        _write_new(directory/DELETE_INTENT,intent)
    # Microsoft documents deletion of a nonexistent profile as success; retry
    # this exact owned name after an interrupted delete or terminal write.
    result=apis['userenv'].DeleteAppContainerProfile(created['name'])
    if result!=0:raise TrainingProcessError('Owned profile cleanup did not complete; retain its recovery receipt.')
    _write_new(directory/DELETED,_bytes(deleted))
    return deleted


class WindowsAppContainerJob(WindowsTrainingJob):
    def __init__(self,name,profile):
        profile.validate()
        if name!=profile.plan.owner_name:raise TrainingProcessError('The AppContainer process owner changed.')
        self.profile=profile;self.token_checks=[]
        self.created_job_success = False
        profile.claim_owner()
        try:
            super().__init__(name)
            self.created_job_success = True
        except BaseException:
            profile.release_owner()
            raise

    def close(self):
        if self.created_job_success and self.handle and self.active():
            self.terminate()
            deadline = time.monotonic() + 3
            while self.active():
                if time.monotonic() >= deadline:
                    raise TrainingProcessError('AppContainer tree stop is unverified; retain its owner and profile.')
                time.sleep(.01)
        super().close()
        self.profile.release_owner()

    def creation_attributes(self):
        self.profile.validate()
        return ((0x20009,self.profile.capabilities),)

    def spawn_suspended(self,command,cwd,environment):
        if tuple(command)!=self.profile.plan.command:raise TrainingProcessError('The AppContainer command changed.')
        self.profile.grant_work(cwd)
        super().spawn_suspended(command,cwd,self.profile.environment(environment))
        self.token_checks.append(require_token(token_profile(self.process),self.profile.created['sid']))

    def resume(self):
        self.profile.validate()
        self.token_checks.append(require_token(token_profile(self.process),self.profile.created['sid']))
        super().resume()

    def observe_tokens(self):
        """Observe only live members after synchronous controller helpers exit."""
        members = self.process_ids()
        observations = []
        for pid in members:
            process = _check(self.api.OpenProcess(0x1000 | 0x100000,False,pid))
            try:
                member = W.BOOL()
                _check(self.api.IsProcessInJob(process,self.handle,C.byref(member)))
                if not member.value or self.api.WaitForSingleObject(process,0) != 258:
                    raise TrainingProcessError('The observed AppContainer member is no longer active.')
                observations.append(require_token(token_profile(process),self.profile.created['sid']))
            finally:
                self.api.CloseHandle(process)
        if self.process_ids() != members:
            raise TrainingProcessError('The observed AppContainer membership changed.')
        return tuple(observations)
