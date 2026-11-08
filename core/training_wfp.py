"""Explicit generated-only WFP ownership. Never enables production training.

Only seven fresh persistent objects are managed; no enumeration, existing rules,
provider, callout, service, driver, global option or network capability is changed.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes as C
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from uuid import UUID, uuid4

from . import training_appcontainer as ac
from .training_process import TrainingProcessError, _bytes, _sha, _read, _write_new, _directory, _lease
from .training_process_windows import WindowsTrainingJob

POLICY = 'windows-lpac-wfp-generated-candidate-v1'
INTENT = 'wfp-intent.json'
INSTALLED = 'wfp-installed.json'
DELETE_INTENT = 'wfp-delete-intent.json'
DELETED = 'wfp-deleted.json'
LAYERS = (
    ('connect_v4', 'c38d57d1-05a7-4c33-904f-7fbceee60e82'),
    ('connect_v6', '4a72393b-319f-44bc-84c3-ba54dcb3b6b4'),
    ('receive_v4', 'e1cd9fe7-f4b5-4273-96c0-592e487b8650'),
    ('receive_v6', 'a3b42c97-9f04-4672-b87e-cee9c483257f'),
    ('listen_v4', '88bb5dad-76d7-4227-9c71-df0a3ed7be7e'),
    ('listen_v6', '7ac9de24-17dd-4814-b4bd-a9fbc95a321b'),
)
PACKAGE_CONDITION = '71bc78fa-f17c-4997-a602-6abb261f351c'


@dataclass(frozen=True)
class Plan:
    created_sha256: str
    sid: str
    keys: tuple[str, ...]

    def validate(self):
        if (type(self.created_sha256) is not str or not re.fullmatch('[0-9a-f]{64}', self.created_sha256)
                or type(self.sid) is not str or not re.fullmatch(r'S-1-15-2(?:-\d+){7}', self.sid)
                or any(int(n) > 0xffffffff for n in self.sid.split('-')[4:])
                or type(self.keys) is not tuple or len(self.keys) != 7 or len(set(self.keys)) != 7):
            raise TrainingProcessError('Invalid WFP ownership binding.')
        try:
            for key in self.keys:
                if type(key) is not str or str(UUID(key)) != key or UUID(key).version != 4:
                    raise ValueError('not a fresh GUID')
        except (ValueError, AttributeError) as error:
            raise TrainingProcessError('WFP requires seven distinct generated object identities.') from error
        return self

    def record(self):
        self.validate()
        return {'schema_version': 1, 'policy': POLICY, 'created_sha256': self.created_sha256,
                'sid': self.sid, 'keys': list(self.keys)}

    @property
    def digest(self):
        return _sha(_bytes(self.record()))

    @classmethod
    def read(cls, value):
        try:
            plan = cls(value['created_sha256'], value['sid'], tuple(value['keys'])).validate()
            if _bytes(value) != _bytes(plan.record()):
                raise ValueError('noncanonical intent')
        except (KeyError, TypeError, ValueError) as error:
            raise TrainingProcessError('Invalid WFP ownership intent.') from error
        return plan

    def objects(self):
        self.validate()
        common = {'flags': 1, 'provider': None, 'data': self.digest,
                  'description': 'Generated-only sandbox traffic qualification'}
        return ({**common, 'kind': 'sublayer', 'key': self.keys[0],
                 # Windows normalizes 0xffff to 0xfffe for this owned sublayer.
                 # Request/read back the exact supported value, never ignore drift.
                 'name': 'CollectiGen generated WFP sublayer', 'weight': 0xfffe},
                *({**common, 'kind': 'filter', 'key': key,
                   # Windows indexes connect/receive filters, not listen filters.
                   'flags': 1 if name.startswith('listen_') else 0x41,
                   'name': 'CollectiGen generated WFP '+name, 'layer': layer,
                   'sublayer': self.keys[0], 'weight': 0xffffffffffffffff,
                   'condition': PACKAGE_CONDITION, 'match': 0, 'sid': self.sid,
                   'action': 0x1001, 'context': 0}
                  for key, (name, layer) in zip(self.keys[1:], LAYERS)))


def _snapshot(plan, backend, *, complete):
    result = []
    for expected in plan.objects():
        observed = backend.get(expected)
        if observed is None:
            if complete:
                raise TrainingProcessError('An owned WFP block is missing; launch remains held.')
            result.append(None)
            continue
        fields = {k: v for k, v in observed.items() if k != 'filter_id'}
        if _bytes(fields) != _bytes(expected):
            changed = sorted(k for k in fields.keys() | expected.keys() if _bytes(fields.get(k)) != _bytes(expected.get(k)))
            raise TrainingProcessError('An owned WFP object differs from its intent ('+
                ', '.join(changed)+'); retain policy.')
        if expected['kind'] == 'filter' and (type(observed.get('filter_id')) is not int
                or not 0 < observed['filter_id'] < 2**64):
            raise TrainingProcessError('The owned WFP filter identity is unavailable.')
        if expected['kind'] == 'sublayer' and 'filter_id' in observed:
            raise TrainingProcessError('Unexpected WFP sublayer identity.')
        result.append(observed)
    return result


def _binding(directory, plan):
    before = _directory(directory)
    created_raw = _read(directory/ac.CREATED)
    created = ac._validate_created(json.loads(created_raw))
    if (plan.created_sha256 != _sha(created_raw) or plan.sid != created['sid']
            or tuple(created['workspace_identity']) != before
            or _read(directory/INTENT) != _bytes(plan.record())):
        raise TrainingProcessError('The WFP workspace/profile binding changed.')
    reserved = _read(directory/ac.RESERVED)
    if (_sha(reserved) != created['reservation_sha256'] or json.loads(reserved) !=
            {k: v for k, v in created.items() if k not in {'sid', 'reservation_sha256'}}):
        raise TrainingProcessError('The WFP profile reservation changed.')
    return created


class Owner:
    """Own policy under the existing exclusive profile lease; fresh install only."""
    def __init__(self, profile, backend):
        profile.validate()
        self.profile, self.backend = profile, backend
        self.directory = profile.directory
        self.plan = Plan(_sha(_bytes(profile.created)), profile.created['sid'],
                         tuple(str(uuid4()) for _ in range(7))).validate()
        _write_new(self.directory/INTENT, _bytes(self.plan.record()))
        self.backend.bind(self.plan)

    def install(self):
        self.profile.validate()
        _binding(self.directory, self.plan)
        if self.profile.owner_claimed_once or any((self.directory/name).exists()
                for name in (INSTALLED, DELETE_INTENT, DELETED)):
            raise TrainingProcessError('WFP installation cannot adopt or reuse an earlier run.')
        with self.backend.transaction():
            if any(row is not None for row in _snapshot(self.plan, self.backend, complete=False)):
                raise TrainingProcessError('An intended WFP identity already exists; never adopt it.')
            for expected in self.plan.objects():
                self.backend.add(expected)
            _snapshot(self.plan, self.backend, complete=True)
        rows = _snapshot(self.plan, self.backend, complete=True)
        _write_new(self.directory/INSTALLED, _bytes({'schema_version': 1,
            'intent_sha256': self.plan.digest, 'objects': rows, 'execution_ready': False}))
        return self.verify()

    def verify(self):
        self.profile.validate()
        _binding(self.directory, self.plan)
        if any((self.directory/name).exists() for name in (DELETE_INTENT, DELETED)):
            raise TrainingProcessError('WFP cleanup has started; launch remains held.')
        # The transaction provides one consistent view of all seven exact objects.
        with self.backend.transaction(read_only=True):
            rows = _snapshot(self.plan, self.backend, complete=True)
        receipt = {'schema_version': 1, 'intent_sha256': self.plan.digest,
                   'objects': rows, 'execution_ready': False}
        if _read(self.directory/INSTALLED) != _bytes(receipt):
            raise TrainingProcessError('Installed WFP evidence changed; launch remains held.')
        return receipt

    def cleanup(self):
        if self.profile.owner_active or self.profile.lease is None:
            raise TrainingProcessError('The WFP process owner is still active or closed; retain blocks.')
        return _cleanup_locked(self.directory, self.plan, self.backend)


def recover(directory, expected_intent_sha256, backend):
    """Exact-path recovery only; never scan the host for abandoned rules."""
    directory = Path(directory)
    with _lease(directory):
        raw = _read(directory/INTENT)
        if _sha(raw) != expected_intent_sha256:
            raise TrainingProcessError('The WFP recovery intent changed.')
        return _cleanup_locked(directory, Plan.read(json.loads(raw)), backend)


def _cleanup_locked(directory, plan, backend):
    created = _binding(directory, plan)
    backend.bind(plan)
    if WindowsTrainingJob.active_named(created['owner_name']) != 0:
        raise TrainingProcessError('The owned process tree is still active; retain WFP blocks.')
    intent = _bytes({'schema_version': 1, 'intent_sha256': plan.digest})
    if (directory/DELETE_INTENT).exists():
        if _read(directory/DELETE_INTENT) != intent:
            raise TrainingProcessError('The WFP cleanup intent changed.')
    else:
        _write_new(directory/DELETE_INTENT, intent)
    with backend.transaction():
        rows = _snapshot(plan, backend, complete=False)
        if WindowsTrainingJob.active_named(created['owner_name']) != 0:
            raise TrainingProcessError('The owned tree became active; retain WFP blocks.')
        for expected, observed in reversed(list(zip(plan.objects(), rows))):
            if observed is not None:
                backend.delete(expected)
    with backend.transaction(read_only=True):
        if any(row is not None for row in _snapshot(plan, backend, complete=False)):
            raise TrainingProcessError('WFP deletion read-back is incomplete; retain recovery evidence.')
    receipt = {'schema_version': 1, 'intent_sha256': plan.digest,
               'owned_objects_absent': True, 'tree_stopped': True}
    if (directory/DELETED).exists():
        if _read(directory/DELETED) != _bytes(receipt):
            raise TrainingProcessError('The WFP deletion receipt changed.')
    else:
        _write_new(directory/DELETED, _bytes(receipt))
    return receipt


# Windows SDK ABI: fwpmtypes.h / fwptypes.h. Explicit x64 ABI gate below.
U8, U16, U32, U64 = C.c_uint8, C.c_uint16, C.c_uint32, C.c_uint64


class Guid(C.Structure):
    _fields_ = [('data1', U32), ('data2', U16), ('data3', U16), ('data4', U8*8)]

    @classmethod
    def parse(cls, value):
        return cls.from_buffer_copy(UUID(value).bytes_le)

    def text(self):
        return str(UUID(bytes_le=bytes(self)))


class Display(C.Structure):
    _fields_ = [('name', C.c_wchar_p), ('description', C.c_wchar_p)]


class Blob(C.Structure):
    _fields_ = [('size', U32), ('data', C.POINTER(U8))]


class ValueUnion(C.Union):
    _fields_ = [('u8', U8), ('u16', U16), ('u32', U32), ('u64', C.POINTER(U64)), ('pointer', C.c_void_p)]


class Value(C.Structure):
    _anonymous_ = ('value',)
    _fields_ = [('type', U32), ('value', ValueUnion)]


class Condition(C.Structure):
    _fields_ = [('key', Guid), ('match', U32), ('value', Value)]


class Action(C.Structure):
    _fields_ = [('type', U32), ('key', Guid)]


class Context(C.Union):
    _fields_ = [('raw', U64), ('provider', Guid)]


class Filter(C.Structure):
    _fields_ = [('key', Guid), ('display', Display), ('flags', U32),
        ('provider', C.POINTER(Guid)), ('data', Blob), ('layer', Guid), ('sublayer', Guid),
        ('weight', Value), ('count', U32), ('conditions', C.POINTER(Condition)),
        ('action', Action), ('context', Context), ('reserved', C.POINTER(Guid)),
        ('filter_id', U64), ('effective_weight', Value)]


class Sublayer(C.Structure):
    _fields_ = [('key', Guid), ('display', Display), ('flags', U32),
        ('provider', C.POINTER(Guid)), ('data', Blob), ('weight', U16)]


class Session(C.Structure):
    _fields_ = [('key', Guid), ('display', Display), ('flags', U32), ('timeout_ms', U32),
               ('pid', U32), ('sid', C.c_void_p), ('username', C.c_wchar_p), ('kernel', C.c_int32)]


def sid_bytes(sid):
    parts = sid.split('-')
    if parts[:3] != ['S', '1', '15'] or not 1 <= len(parts[3:]) <= 15:
        raise TrainingProcessError('Unsupported generated capability/package SID.')
    return bytes([1, len(parts[3:])]) + (15).to_bytes(6, 'big') + b''.join(
        int(part).to_bytes(4, 'little') for part in parts[3:])


class WindowsBackend:
    """Get/add/delete only exact intended keys. No host enumeration API exists here."""
    def __init__(self):
        if os.name != 'nt' or C.sizeof(C.c_void_p) != 8:
            raise TrainingProcessError('The generated WFP experiment requires Windows x64.')
        if (C.sizeof(Filter), C.sizeof(Sublayer), C.sizeof(Condition), C.sizeof(Session)) != (200, 72, 40, 72):
            raise TrainingProcessError('The WFP SDK ABI layout differs from the reviewed layout.')
        self.api = C.WinDLL('fwpuclnt.dll', winmode=0x800, use_last_error=True)
        self.handle = C.c_void_p()
        self.in_transaction = False
        self.transaction_uncertain = False
        self.plan = None
        for name, args in {
            'FwpmEngineOpen0': [C.c_wchar_p, U32, C.c_void_p, C.POINTER(Session), C.POINTER(C.c_void_p)],
            'FwpmEngineClose0': [C.c_void_p],
            'FwpmEngineGetOption0': [C.c_void_p, U32, C.POINTER(C.POINTER(Value))],
            'FwpmTransactionBegin0': [C.c_void_p, U32],
            'FwpmTransactionCommit0': [C.c_void_p],
            'FwpmTransactionAbort0': [C.c_void_p],
            'FwpmSubLayerAdd0': [C.c_void_p, C.POINTER(Sublayer), C.c_void_p],
            'FwpmSubLayerGetByKey0': [C.c_void_p, C.POINTER(Guid), C.POINTER(C.POINTER(Sublayer))],
            'FwpmSubLayerDeleteByKey0': [C.c_void_p, C.POINTER(Guid)],
            'FwpmFilterAdd0': [C.c_void_p, C.POINTER(Filter), C.c_void_p, C.POINTER(U64)],
            'FwpmFilterGetByKey0': [C.c_void_p, C.POINTER(Guid), C.POINTER(C.POINTER(Filter))],
            'FwpmFilterDeleteByKey0': [C.c_void_p, C.POINTER(Guid)],
        }.items():
            fn = getattr(self.api, name)
            fn.argtypes, fn.restype = args, U32
        self.api.FwpmFreeMemory0.argtypes, self.api.FwpmFreeMemory0.restype = [C.POINTER(C.c_void_p)], None
        session = Session(key=Guid.parse(str(uuid4())), timeout_ms=3000)
        self.check(self.api.FwpmEngineOpen0(None, 10, None, C.byref(session), C.byref(self.handle)), 'open')

    @staticmethod
    def check(code, operation):
        if code:
            raise TrainingProcessError(f'WFP {operation} failed with Windows code 0x{code:08x}; retain recovery evidence.')

    def bind(self, plan):
        if type(plan) is not Plan:
            raise TrainingProcessError('Bind WFP to one exact generated ownership plan.')
        plan.validate()
        if self.plan is not None and self.plan != plan:
            raise TrainingProcessError('A WFP connection cannot change ownership plans.')
        self.plan = plan

    def require_object(self, expected):
        if self.plan is None or not any(_bytes(expected) == _bytes(row) for row in self.plan.objects()):
            raise TrainingProcessError('WFP access is limited to the seven exact intended objects.')

    def event_collection_enabled(self):
        """Read one readiness bit; never enable global event collection."""
        value = C.POINTER(Value)()
        self.check(self.api.FwpmEngineGetOption0(self.handle, 0, C.byref(value)), 'event availability read')
        try:
            if not value or value.contents.type != 3 or value.contents.u32 not in (0, 1):
                raise TrainingProcessError('WFP event availability is not a supported boolean.')
            return bool(value.contents.u32)
        finally:
            self.api.FwpmFreeMemory0(C.cast(C.byref(value), C.POINTER(C.c_void_p)))

    @contextmanager
    def transaction(self, *, read_only=False):
        if self.in_transaction or self.transaction_uncertain or not self.handle:
            raise TrainingProcessError('WFP transaction ownership is unavailable.')
        self.check(self.api.FwpmTransactionBegin0(self.handle, 1 if read_only else 0), 'transaction begin')
        self.in_transaction = True
        try:
            yield
            self.check(self.api.FwpmTransactionCommit0(self.handle), 'transaction commit')
        except BaseException as error:
            abort_code = self.api.FwpmTransactionAbort0(self.handle)
            if abort_code:
                self.transaction_uncertain = True
                raise TrainingProcessError(f'{error} WFP transaction abort also failed with '
                    f'Windows code 0x{abort_code:08x}; retain blocks and reconnect for exact recovery.') from error
            raise
        finally:
            self.in_transaction = False

    def add(self, expected):
        self.require_object(expected)
        if not self.in_transaction:
            raise TrainingProcessError('WFP additions require an owned transaction.')
        blob = (U8*32).from_buffer_copy(bytes.fromhex(expected['data']))
        common = dict(key=Guid.parse(expected['key']), display=Display(expected['name'], expected['description']),
                      flags=expected['flags'], data=Blob(32, blob))
        if expected['kind'] == 'sublayer':
            value = Sublayer(**common, weight=expected['weight'])
            self.check(self.api.FwpmSubLayerAdd0(self.handle, C.byref(value), None), 'sublayer add')
        else:
            sid = C.create_string_buffer(sid_bytes(expected['sid']))
            condition = Condition(Guid.parse(expected['condition']), expected['match'],
                                  Value(type=13, pointer=C.addressof(sid)))
            weight = U64(expected['weight'])
            value = Filter(**common, layer=Guid.parse(expected['layer']), sublayer=Guid.parse(expected['sublayer']),
                weight=Value(type=4, u64=C.pointer(weight)), count=1, conditions=C.pointer(condition),
                action=Action(type=expected['action']), context=Context(raw=0))
            identifier = U64()
            self.check(self.api.FwpmFilterAdd0(self.handle, C.byref(value), None, C.byref(identifier)), 'filter add')

    def get(self, expected):
        self.require_object(expected)
        kind = expected['kind']
        value = C.POINTER(Sublayer if kind == 'sublayer' else Filter)()
        key = Guid.parse(expected['key'])
        fn = self.api.FwpmSubLayerGetByKey0 if kind == 'sublayer' else self.api.FwpmFilterGetByKey0
        code = fn(self.handle, C.byref(key), C.byref(value))
        if code == (0x80320007 if kind == 'sublayer' else 0x80320003):
            return None
        self.check(code, kind+' read')
        try:
            if not value:
                raise TrainingProcessError('WFP returned an empty object.')
            row = value.contents
            if row.data.size != 32 or not row.data.data or row.provider:
                raise TrainingProcessError('The WFP object has unexpected ownership data.')
            result = {'kind': kind, 'key': row.key.text(), 'flags': row.flags, 'provider': None,
                'data': C.string_at(row.data.data, 32).hex(), 'name': row.display.name,
                'description': row.display.description}
            if kind == 'sublayer':
                return {**result, 'weight': row.weight}
            if (row.count != 1 or not row.conditions or row.weight.type != 4 or not row.weight.u64
                    or row.effective_weight.type != 4 or not row.effective_weight.u64
                    or row.reserved or bytes(row.action.key) != bytes(16)
                    or bytes(row.context) != bytes(16)):
                raise TrainingProcessError('The WFP filter structure differs from the intended block.')
            cond = row.conditions[0]
            sid = sid_bytes(expected['sid'])
            if cond.value.type != 13 or not cond.value.pointer or C.string_at(cond.value.pointer, 2) != sid[:2]:
                raise TrainingProcessError('The WFP package condition is missing or malformed.')
            if C.string_at(cond.value.pointer, len(sid)) != sid:
                raise TrainingProcessError('The WFP package SID changed.')
            if row.effective_weight.u64[0] != row.weight.u64[0]:
                raise TrainingProcessError('The WFP effective filter weight changed.')
            return {**result, 'weight': row.weight.u64[0], 'layer': row.layer.text(),
                'sublayer': row.sublayer.text(), 'condition': cond.key.text(), 'match': cond.match,
                'sid': expected['sid'], 'action': row.action.type, 'context': row.context.raw,
                'filter_id': row.filter_id}
        finally:
            self.api.FwpmFreeMemory0(C.cast(C.byref(value), C.POINTER(C.c_void_p)))

    def delete(self, expected):
        self.require_object(expected)
        if not self.in_transaction:
            raise TrainingProcessError('WFP deletion requires an owned transaction.')
        key = Guid.parse(expected['key'])
        fn = self.api.FwpmSubLayerDeleteByKey0 if expected['kind'] == 'sublayer' else self.api.FwpmFilterDeleteByKey0
        self.check(fn(self.handle, C.byref(key)), 'owned '+expected['kind']+' delete')

    def close(self):
        if self.handle:
            self.check(self.api.FwpmEngineClose0(self.handle), 'close')
            self.handle = C.c_void_p()
