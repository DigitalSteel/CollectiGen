"""Explicit SDXL local-input identity checks, without loading any component.

The public service contains all selected-record/file I/O in an owned helper.
Tokenizer identities cover four named files, never a directory, model identity,
semantic result or permission to run a third-party loader.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

from .model_resource_registry import (ModelResourceRegistry, ModelResourceError,
    ResourceRecoveryRequired, inspect_model_resource)
from .training_tokenizer_files import TOKENIZER_FILES, read_file as _read_tokenizer_file, layout as _layout
from .training_source_io import _assert_unredirected, _identity
from .training_preflight import (ContainedRuntimeQualificationService, PreflightReply,
    PREFLIGHT_SECONDS, runtime)
from .training_process import TrainingProcessError, _bytes, _sha

WORKER = runtime.ROOT / 'tools/training_local_components_worker.py'
SHA = re.compile(r'[0-9a-f]{64}\Z')
RESOURCE_ID = re.compile(r'sha256:[0-9a-f]{64}\Z')
MAX_TOKENIZER_BYTES = sum(limit for _, limit in TOKENIZER_FILES)
ROLES = ('base', 'vae', 'tokenizer_1', 'tokenizer_2')
STATUSES = {'not_checked', 'not_selected', 'identity_checked', 'unregistered',
    'declaration_mismatch', 'location_mismatch', 'unavailable', 'changed', 'invalid_layout'}
REGISTRY_STATUSES = {'checked', 'changed', 'unavailable', 'recovery_required'}
FALSE_FLAGS = ('tokenizer_semantics_verified', 'model_compatibility_verified',
    'offline_execution_verified', 'runtime_code_verified', 'execution_ready')


def _path(value):
    # Lexical validation only: the controller must not stat a selected path.
    if type(value) is not str or not value or len(value) > 32767 or '\0' in value or value.startswith(('\\\\', '//')) or not Path(value).is_absolute() or str(Path(os.path.abspath(value))) != value:
        raise ValueError('Select an absolute local path without parent segments.')
    return Path(value)


@dataclass(frozen=True)
class RegisteredModelSelection:
    resource_id: str
    path: str

    def validate(self):
        if type(self.resource_id) is not str or not RESOURCE_ID.fullmatch(self.resource_id):
            raise ValueError('Select a registered full SHA-256 model identity.')
        _path(self.path)


@dataclass(frozen=True)
class LocalComponentSelection:
    registry: str
    registry_revision: str
    base: RegisteredModelSelection
    tokenizer_1: str
    tokenizer_2: str
    vae: RegisteredModelSelection | None = None

    def validate(self):
        for path in (self.registry, self.tokenizer_1, self.tokenizer_2):
            _path(path)
        if type(self.registry_revision) is not str or not SHA.fullmatch(self.registry_revision):
            raise ValueError('Bind the reviewed model registry revision.')
        if type(self.base) is not RegisteredModelSelection or (self.vae is not None and type(self.vae) is not RegisteredModelSelection):
            raise ValueError('Select the base and optional VAE explicitly.')
        self.base.validate()
        if self.vae is not None:
            self.vae.validate()

    def roots(self):
        return (Path(self.registry).parent, Path(self.base.path).parent,
            Path(self.tokenizer_1), Path(self.tokenizer_2),
            *((Path(self.vae.path).parent,) if self.vae else ()))

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict or set(value) != set(cls.__dataclass_fields__):
            raise ValueError('Invalid local component selection.')
        def model(row):
            if type(row) is not dict or set(row) != {'resource_id', 'path'}:
                raise ValueError('Invalid selected model binding.')
            return RegisteredModelSelection(**row)
        result = cls(**{**value, 'base': model(value['base']),
            'vae': model(value['vae']) if value['vae'] is not None else None})
        result.validate()
        return result


def _row(status='not_checked', identity='', size=0, files=0):
    return {'status': status, 'identity': identity, 'bytes': size, 'files': files}


def _report(rows, registry_status, expected):
    checked = registry_status == 'checked' and all(row['status'] == 'identity_checked' or (role == 'vae' and row['status'] == 'not_selected') for role, row in rows.items())
    binding = _sha(_bytes({'schema_version': 1, 'format': 'sdxl-local-inputs-v1',
        'components': rows})) if checked else ''
    matches = not expected or binding == expected
    return {'schema_version': 1, 'kind': 'sdxl_local_component_check',
        'status': 'inputs_checked' if checked and matches else 'held',
        'registry_status': registry_status, 'components': rows,
        'binding_sha256': binding, 'expected_binding_matches': matches,
        'local_inputs_verified': checked and matches,
        **{key: False for key in FALSE_FLAGS}}


def validate_report(value, selection, expected):
    """Validate path-free results without any selected filesystem access."""
    if type(value) is not dict or set(value) != set(_report({}, 'checked', '')) or type(value['schema_version']) is not int or value['schema_version'] != 1 or value['kind'] != 'sdxl_local_component_check':
        raise ValueError('Invalid component report fields.')
    rows = value['components']
    if type(rows) is not dict or set(rows) != set(ROLES) or type(value['registry_status']) is not str or value['registry_status'] not in REGISTRY_STATUSES:
        raise ValueError('Invalid component report scope.')
    for role, row in rows.items():
        if type(row) is not dict or set(row) != set(_row()) or type(row['status']) is not str or row['status'] not in STATUSES or type(row['identity']) is not str or any(type(row[key]) is not int or row[key] < 0 for key in ('bytes', 'files')):
            raise ValueError('Invalid component identity fields.')
        if role == 'vae' and selection.vae is None:
            if row != _row('not_selected'):
                raise ValueError('An unselected VAE cannot acquire a binding.')
            continue
        if row['status'] == 'not_selected':
            raise ValueError('A required component cannot be omitted.')
        if row['status'] != 'identity_checked':
            if row != _row(row['status']):
                raise ValueError('Unchecked components cannot claim an identity.')
            continue
        if role in {'base', 'vae'}:
            if row['identity'] != getattr(selection, role).resource_id or row['files'] != 1 or row['bytes'] < 8:
                raise ValueError('The selected model identity changed.')
        elif not re.fullmatch(r'clip-files-v1:[0-9a-f]{64}', row['identity']) or row['files'] != len(TOKENIZER_FILES) or not 0 < row['bytes'] <= MAX_TOKENIZER_BYTES:
            raise ValueError('The tokenizer file identity is invalid.')
    calculated = _report(rows, value['registry_status'], expected)
    # Canonical bytes also distinguish bool from int and reject nonfinite JSON.
    if _bytes(value) != _bytes(calculated):
        raise ValueError('Component report readiness or fingerprint changed.')
    return value


def inspect_local_components(selection, *, expected_binding='', progress=None, model_observer=None, tokenizer_observer=None):
    """Blocking read-only primitive for the contained helper and synthetic tests."""
    selection.validate()
    if type(expected_binding) is not str or (expected_binding and not SHA.fullmatch(expected_binding)):
        raise ValueError('Invalid expected component binding.')
    rows = {role: _row() for role in ROLES}
    if selection.vae is None:
        rows['vae'] = _row('not_selected')
    registry = ModelResourceRegistry(selection.registry)
    try:
        _assert_unredirected(registry.path)
        state = registry.path.stat(follow_symlinks=False)
        if not stat.S_ISREG(state.st_mode) or int(getattr(state, 'st_file_attributes', 0)) & (0x1000 | 0x40000 | 0x400000):
            return _report(rows, 'unavailable', expected_binding)
        snapshot, revision = registry.snapshot()
    except ResourceRecoveryRequired:
        return _report(rows, 'recovery_required', expected_binding)
    except (OSError, RuntimeError, ValueError):
        return _report(rows, 'unavailable', expected_binding)
    if revision != selection.registry_revision:
        return _report(rows, 'changed', expected_binding)
    states = []
    for role in ('base', 'vae'):
        selected = getattr(selection, role)
        if selected is None:
            continue
        record = snapshot['resources'].get(selected.resource_id)
        if record is None:
            rows[role] = _row('unregistered')
        elif not any(d.get('role') == ('checkpoint' if role == 'base' else 'vae') and d.get('family', '').casefold() == 'sdxl' for d in record['declarations']):
            rows[role] = _row('declaration_mismatch')
        elif not any(Path(row['path']) == Path(selected.path) for row in record['locations']):
            rows[role] = _row('location_mismatch')
        else:
            try:
                kwargs = {'header_observer': lambda raw, role=role: model_observer(role, raw)} if model_observer else {}
                inspected = inspect_model_resource(selected.path, progress=progress, **kwargs)
                if inspected.resource_id != selected.resource_id or inspected.stored_identity() != record['identity']:
                    rows[role] = _row('changed')
                else:
                    rows[role] = _row('identity_checked', selected.resource_id, inspected.size, 1)
                    states.append((role, inspected.path, inspected.file_state))
            except (OSError, RuntimeError, ValueError):
                rows[role] = _row('unavailable')
    for role in ('tokenizer_1', 'tokenizer_2'):
        folder = Path(getattr(selection, role))
        blobs, identities, pending = {}, [], []
        try:
            _assert_unredirected(folder)
            state = folder.stat(follow_symlinks=False)
            if not stat.S_ISDIR(state.st_mode) or int(getattr(state, 'st_file_attributes', 0)) & (0x1000 | 0x40000 | 0x400000):
                raise ValueError('Unsupported tokenizer directory.')
            for name, limit in TOKENIZER_FILES:
                raw, identity = _read_tokenizer_file(folder / name, limit)
                blobs[name] = raw
                identities.append({'name': name, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
                pending.append((role, folder / name, identity))
            try:
                _layout(blobs)
            except (ValueError, ModelResourceError, UnicodeError):
                rows[role] = _row('invalid_layout')
                continue
            if tokenizer_observer is not None:
                tokenizer_observer(role, blobs)
            digest = _sha(_bytes({'format': 'clip-files-v1', 'files': identities}))
            rows[role] = _row('identity_checked', 'clip-files-v1:' + digest, sum(len(raw) for raw in blobs.values()), len(identities))
            states.extend(pending)
        except (OSError, RuntimeError, ValueError):
            rows[role] = _row('unavailable')
    for role, path, state in states:
        try:
            _assert_unredirected(path)
            if _identity(path.stat(follow_symlinks=False)) != state:
                rows[role] = _row('changed')
        except (OSError, RuntimeError):
            rows[role] = _row('changed')
    try:
        status = 'checked' if registry.snapshot()[1] == revision else 'changed'
    except ResourceRecoveryRequired:
        status = 'recovery_required'
    except (OSError, RuntimeError, ValueError):
        status = 'unavailable'
    return _report(rows, status, expected_binding)


class ComponentReply(PreflightReply):
    bytes_message = 'Verifying selected local component bytes'
    entries_message = 'Checking selected local component files'

    def decode(self, value, plan):
        return (validate_report(value, LocalComponentSelection.from_dict(self.request['selection']),
            self.request['expected_binding']),)


class ContainedLocalComponentService(ContainedRuntimeQualificationService):
    def check(self, selection, root, jobs, *, expected_binding='', should_cancel=None,
              progress=None, timeout_seconds=PREFLIGHT_SECONDS):
        selection.validate()
        if type(expected_binding) is not str or (expected_binding and not SHA.fullmatch(expected_binding)):
            raise ValueError('Invalid expected component binding.')
        root = Path(os.path.abspath(root))
        _path(str(root))
        if any(root.is_relative_to(path) for path in selection.roots()):
            raise TrainingProcessError('Keep component work outside all selected input locations.')
        request = {'schema_version': 1, 'nonce': uuid4().hex, 'operation': 'components',
            'selection': asdict(selection), 'expected_binding': expected_binding}
        # Keep the selected record/model/tokenizer paths solely in bounded memory.
        report, receipt = self._run(request, root, jobs, worker=WORKER,
            extra_code=(Path(__file__), Path(__file__).with_name('model_resource_registry.py'),
                        Path(__file__).with_name('training_tokenizer_files.py')),
            reply_type=ComponentReply, should_cancel=should_cancel, progress=progress,
            timeout_seconds=timeout_seconds)
        return {**report, 'preflight_receipt_sha256': _sha(_bytes(receipt))}
