"""Content-free, bounded diagnostics usable before Qt or application state loads.

Only developer-owned enum values and numeric OS errors enter the event schema.
Exception messages, tracebacks, file paths and arbitrary extra fields never do.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
from importlib import metadata
import json
import logging
from pathlib import Path
import platform
import re
import sys
from threading import RLock
from uuid import UUID, uuid4

from .diagnostic_io import rotating_handler, write_verified_zip

MAX_FILE_BYTES = 256 * 1024
BACKUPS = 3
MAX_RECORD_BYTES = 4096
MAX_EVENTS = 128
MAX_RECOVERY_STEPS = 8
DEPENDENCIES = ('PySide6', 'Pillow', 'numpy', 'onnxruntime', 'torch')
_VERSION = re.compile(r'[0-9]{1,4}(?:\.[0-9]{1,4}){0,3}(?:[-+][A-Za-z0-9.]{1,40})?')


class Component(str, Enum):
    CORE = 'core'
    MANAGER = 'manager'
    BOOTSTRAP = 'bootstrap'
    PACKAGING = 'packaging'
    DEPENDENCIES = 'dependencies'


class ErrorCode(str, Enum):
    OPERATION_FAILED = 'operation_failed'
    STARTUP_FAILED = 'startup_failed'
    DEPENDENCY_UNAVAILABLE = 'dependency_unavailable'
    STORAGE_UNAVAILABLE = 'storage_unavailable'
    INVALID_INPUT = 'invalid_input'
    MISSING_MODULE = 'missing_module'
    MISSING_DLL = 'missing_dll'
    MISSING_PROCEDURE = 'missing_procedure'


class Dependency(str, Enum):
    UNKNOWN = 'unknown'
    PYSIDE6 = 'PySide6'
    PILLOW = 'Pillow'
    NUMPY = 'numpy'
    ONNXRUNTIME = 'onnxruntime'
    TORCH = 'torch'
    SQLITE3 = 'sqlite3'
    SSL = 'ssl'
    PYTHON = 'python'
    WINDOWS_RUNTIME = 'windows_runtime'


class RecoveryAction(str, Enum):
    RETRY = 'retry'
    REPAIR = 'repair'
    FALLBACK = 'fallback'
    CANCEL = 'cancel'
    RESTART = 'restart'


class RecoveryOutcome(str, Enum):
    PENDING = 'pending'
    SUCCEEDED = 'succeeded'
    FAILED = 'failed'
    CANCELLED = 'cancelled'


_FAILURE_TYPES = frozenset({
    'OSError', 'PermissionError', 'FileNotFoundError', 'FileExistsError',
    'NotADirectoryError', 'IsADirectoryError', 'TimeoutError',
    'ConnectionError', 'ValueError', 'TypeError', 'RuntimeError',
    'ImportError', 'ModuleNotFoundError', 'MemoryError', 'KeyError',
    'IndexError', 'AssertionError', 'Exception',
})


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def _version(value):
    return value if type(value) is str and _VERSION.fullmatch(value) else 'unknown'


def _number(value):
    return value if type(value) is int and -(2**31) <= value < 2**32 else None


def _failure(exc):
    result, seen = [], set()
    while isinstance(exc, BaseException) and id(exc) not in seen and len(result) < 4:
        seen.add(id(exc))
        name = type(exc).__name__
        result.append({
            'type': name if name in _FAILURE_TYPES else 'Exception',
            'errno': _number(getattr(exc, 'errno', None)),
            'winerror': _number(getattr(exc, 'winerror', None)),
        })
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    return result


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')


def _decode(payload: bytes, *, limit: int):
    """Admit bounded JSON without discarded duplicate fields at any depth."""
    if type(payload) is not bytes or len(payload) > limit:
        raise ValueError('Invalid diagnostic payload size')

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate diagnostic field')
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError('Nonfinite diagnostic value')

    try:
        return json.loads(payload, object_pairs_hook=unique_object,
                          parse_constant=invalid_constant)
    except (ValueError, TypeError, RecursionError) as error:
        raise ValueError('Invalid diagnostic JSON') from error


def _valid_stamp(value):
    if type(value) is not str or len(value) > 40:
        return False
    try:
        return datetime.fromisoformat(value).utcoffset().total_seconds() == 0
    except (ValueError, AttributeError):
        return False


def _validate(record):
    """Reject unknown/future fields rather than attempting regex redaction."""
    if type(record) is not dict or set(record) != {
        'schema', 'component', 'build', 'correlation', 'code', 'failure',
        'count', 'first', 'last', 'recoveries', 'recovery_steps_dropped',
        'dependency', 'native_code',
    } or type(record['schema']) is not int or record['schema'] != 1:
        return False
    try:
        Component(record['component'])
        ErrorCode(record['code'])
        Dependency(record['dependency'])
        if record['native_code'] is not None and (type(record['native_code']) is not int or _number(record['native_code']) is None):
            return False
        if str(UUID(record['correlation'])) != record['correlation']:
            return False
        if _version(record['build']) != record['build']:
            return False
        if type(record['count']) is not int or not 1 <= record['count'] <= 2**31:
            return False
        if not all(_valid_stamp(record[key]) for key in ('first', 'last')):
            return False
        if record['first'] > record['last']:
            return False
        if type(record['failure']) is not list or not 1 <= len(record['failure']) <= 4:
            return False
        for failure in record['failure']:
            if type(failure) is not dict or set(failure) != {'type', 'errno', 'winerror'}:
                return False
            if failure['type'] not in _FAILURE_TYPES:
                return False
            if any(value is not None and (type(value) is not int or _number(value) is None)
                   for key, value in failure.items() if key != 'type'):
                return False
        if type(record['recoveries']) is not list or len(record['recoveries']) > MAX_RECOVERY_STEPS:
            return False
        if type(record['recovery_steps_dropped']) is not int or not 0 <= record['recovery_steps_dropped'] <= 2**31:
            return False
        for step in record['recoveries']:
            if type(step) is not dict or set(step) != {'action', 'outcome', 'at', 'failure'}:
                return False
            RecoveryAction(step['action'])
            RecoveryOutcome(step['outcome'])
            if not _valid_stamp(step['at']):
                return False
            # Use the same strict failure validation for an optional recovery error.
            if step['failure']:
                candidate = dict(record, failure=step['failure'], recoveries=[])
                if not _validate(candidate):
                    return False
            elif step['failure'] != []:
                return False
        return len(_json(record)) <= MAX_RECORD_BYTES
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return False


@dataclass(frozen=True)
class ExportPreview:
    """Exact immutable bytes for a visible preview; export never rereads sources."""
    files: tuple[tuple[str, bytes], ...]
    unavailable_files: int
    rejected_records: int
    storage_available: bool


def runtime_identity():
    versions = {}
    for name in DEPENDENCIES:
        try:
            versions[name] = _version(metadata.version(name))
        except Exception:
            versions[name] = 'unavailable'
    system = platform.system()
    machine = platform.machine().casefold()
    return {
        'python': '.'.join(str(n) for n in sys.version_info[:3]),
        'os': system if system in {'Windows', 'Linux', 'Darwin'} else 'unknown',
        'os_release': _version(platform.release()),
        'os_version': _version(platform.version()),
        'architecture': machine if machine in {'amd64', 'x86_64', 'arm64', 'aarch64', 'x86', 'i386', 'i686'} else 'unknown',
        'dependencies': versions,
    }


class Diagnostics:
    def __init__(self, root: Path, *, component: Component, build: str,
                 max_bytes: int = MAX_FILE_BYTES, backups: int = BACKUPS, recording: bool = True):
        if not isinstance(component, Component):
            raise ValueError('Use a declared Component')
        if type(max_bytes) is not int or not MAX_RECORD_BYTES + 2 <= max_bytes <= MAX_FILE_BYTES:
            raise ValueError('Invalid diagnostic storage bound')
        if type(backups) is not int or not 1 <= backups <= BACKUPS:
            raise ValueError('Invalid diagnostic retention')
        if type(recording) is not bool:
            raise ValueError('Invalid recording mode')
        self._recording = recording
        self.component, self.build = component, _version(build)
        self.path = Path(root) / 'logs' / ('diagnostics-' + component.value + '.jsonl')
        self.max_bytes, self.backups = max_bytes, backups
        self._lock = RLock()
        self._events = OrderedDict()
        self._active = {}
        self._handler = None if not recording or self.path.parent.is_symlink() or self.path.is_symlink() else rotating_handler(self.path, max_bytes=max_bytes, backups=backups)
        if self._handler is not None:
            self._handler.setFormatter(logging.Formatter('%(message)s'))

    @property
    def storage_available(self):
        return self._handler is not None and not self._handler.failed

    def _persist(self, record):
        if self._handler is None:
            return
        try:
            payload = _json(record)
            if len(payload) > MAX_RECORD_BYTES:
                return
            self._handler.handle(logging.LogRecord('diagnostics', logging.ERROR,
                '', 0, payload.decode('ascii'), (), None))
        except Exception:
            self._handler.failed = True

    def failure(self, exc: BaseException, *, code: ErrorCode = ErrorCode.OPERATION_FAILED,
                dependency: Dependency = Dependency.UNKNOWN, native_code: int | None = None):
        """Record original safe failure evidence; repeated unresolved failures fold."""
        try:
            if not self._recording:
                return None
            if not isinstance(exc, BaseException) or not isinstance(code, ErrorCode) or not isinstance(dependency, Dependency):
                return None
            if native_code is not None and _number(native_code) is None:
                return None
            failure = _failure(exc)
            key = (code.value, dependency.value, native_code, _json(failure))
            with self._lock:
                correlation = self._active.get(key)
                record = self._events.get(correlation)
                now = _stamp()
                if record is None:
                    correlation = str(uuid4())
                    record = dict(schema=1, component=self.component.value, build=self.build,
                        correlation=correlation, code=code.value, failure=failure,
                        dependency=dependency.value, native_code=native_code,
                        count=0, first=now, last=now, recoveries=[], recovery_steps_dropped=0)
                    self._events[correlation] = record
                    self._active[key] = correlation
                record['count'] = min(record['count'] + 1, 2**31)
                record['last'] = now
                self._events.move_to_end(correlation)
                if len(self._events) > MAX_EVENTS:
                    removed, _ = self._events.popitem(last=False)
                    self._active = {k: v for k, v in self._active.items() if v != removed}
                self._persist(record)
                return correlation
        except Exception:
            return None

    def recovery(self, correlation: str, *, action: RecoveryAction,
                 outcome: RecoveryOutcome, error: BaseException | None = None):
        """Only report success after the owner has actually confirmed its outcome."""
        try:
            if not self._recording:
                return False
            if not isinstance(action, RecoveryAction) or not isinstance(outcome, RecoveryOutcome):
                return False
            if error is not None and not isinstance(error, BaseException):
                return False
            if error is not None and outcome is not RecoveryOutcome.FAILED:
                return False
            with self._lock:
                record = self._events.get(correlation)
                if record is None:
                    return False
                if record['recoveries'] and record['recoveries'][-1]['outcome'] in {'succeeded', 'cancelled'}:
                    return False
                record['recoveries'].append(dict(action=action.value, outcome=outcome.value,
                                                at=_stamp(), failure=_failure(error)))
                if len(record['recoveries']) > MAX_RECOVERY_STEPS:
                    record['recoveries'].pop(0)
                    record['recovery_steps_dropped'] = min(record['recovery_steps_dropped'] + 1, 2**31)
                record['last'] = record['recoveries'][-1]['at']
                if outcome in {RecoveryOutcome.SUCCEEDED, RecoveryOutcome.CANCELLED}:
                    self._active = {k: v for k, v in self._active.items() if v != correlation}
                self._persist(record)
                return True
        except Exception:
            return False

    def preview(self):
        events, unavailable, rejected = OrderedDict(), 0, 0
        with self._lock:
            # Read only exact owned names, oldest first. Never enumerate arbitrary logs.
            for suffix in [f'.{n}' for n in range(self.backups, 0, -1)] + ['']:
                path = self.path.with_name(self.path.name + suffix)
                try:
                    if self.path.parent.is_symlink() or path.is_symlink():
                        unavailable += 1
                        continue
                    with path.open('rb') as stream:
                        raw = stream.read(self.max_bytes + 1)
                    if len(raw) > self.max_bytes:
                        rejected += 1
                        continue
                    for line in raw.splitlines():
                        try:
                            record = _decode(line, limit=MAX_RECORD_BYTES)
                            if not _validate(record):
                                raise ValueError()
                            events[record['correlation']] = record
                            events.move_to_end(record['correlation'])
                            if len(events) > MAX_EVENTS:
                                events.popitem(last=False)
                        except (ValueError, TypeError, RecursionError):
                            rejected += 1
                except FileNotFoundError:
                    continue
                except OSError:
                    unavailable += 1
            # Retain current evidence even if disk logging failed.
            for key, record in self._events.items():
                events[key] = record
                events.move_to_end(key)
                if len(events) > MAX_EVENTS:
                    events.popitem(last=False)
            files = (
                ('diagnostics.json', _json({'schema': 1, 'events': list(events.values())})),
                ('runtime.json', _json(runtime_identity())),
            )
            return ExportPreview(files, unavailable, rejected, self.storage_available)

    def close(self):
        with self._lock:
            if self._handler is not None:
                self._handler.close()
                self._handler = None


def export_preview(preview: ExportPreview, output: Path, *, selected: tuple[str, ...]):
    """User-triggered seam. Caller displays exact preview and user's selection first."""
    allowed = {'diagnostics.json', 'runtime.json'}
    if not isinstance(preview, ExportPreview) or not set(selected) <= allowed or len(set(selected)) != len(selected):
        raise ValueError('Invalid diagnostic selection')
    if type(preview.storage_available) is not bool or any(type(n) is not int or not 0 <= n <= 2**31 for n in (preview.unavailable_files, preview.rejected_records)):
        raise ValueError('Invalid preview status')
    files = [item for item in preview.files if item[0] in selected]
    # Preview objects are inspectable but not trusted on re-entry; validate their bytes.
    if set(name for name, _ in files) != set(selected) or len(files) != len(selected):
        raise ValueError('Incomplete diagnostic preview')
    for name, payload in files:
        data = _decode(payload, limit=MAX_EVENTS * MAX_RECORD_BYTES + 1024)
        if name == 'diagnostics.json':
            if type(data) is not dict or set(data) != {'schema', 'events'} or type(data['schema']) is not int or data['schema'] != 1 or type(data['events']) is not list or len(data['events']) > MAX_EVENTS or not all(_validate(row) for row in data['events']):
                raise ValueError('Invalid diagnostic preview')
        elif not _valid_runtime(data):
            raise ValueError('Invalid runtime preview')
    manifest = dict(schema=1, created_at=_stamp(),
        unavailable_files=preview.unavailable_files, rejected_records=preview.rejected_records,
        storage_available=preview.storage_available,
        excluded=['raw logs', 'exception messages', 'paths', 'credentials', 'prompts',
                  'chats', 'catalogs', 'media', 'settings', 'user data'],
        files=[dict(name=name, bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
               for name, payload in files])
    files.append(('manifest.json', _json(manifest)))
    destination = Path(output)
    # Do not replace an existing user file; the shared writer handles verification.
    if destination.exists():
        raise FileExistsError('Choose a new export destination')
    return write_verified_zip(destination, files)


def _valid_runtime(data):
    if type(data) is not dict or set(data) != {'python', 'os', 'os_release', 'os_version', 'architecture', 'dependencies'}:
        return False
    if data['os'] not in {'Windows', 'Linux', 'Darwin', 'unknown'} or data['architecture'] not in {'amd64', 'x86_64', 'arm64', 'aarch64', 'x86', 'i386', 'i686', 'unknown'}:
        return False
    if any(_version(data[k]) != data[k] for k in ('python', 'os_release', 'os_version')):
        return False
    versions = data['dependencies']
    return type(versions) is dict and set(versions) == set(DEPENDENCIES) and all(
        value == 'unavailable' or _version(value) == value for value in versions.values())
