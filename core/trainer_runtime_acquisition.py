"""Explicit pinned public software acquisition; no resolver, installer or activation.

The confirmed fingerprint is supplied by the user's acquisition controller.
Constructing a plan or service never authorizes network access. Failed attempts
remain available; retry creates a new attempt and reuses only verified bytes.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import sys
import time
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from .training_file_inventory import MAX_BYTES, MAX_MANIFEST_BYTES, _check_path, _control, canonical
from .training_source_io import _assert_unredirected, _identity
from .trainer_environment import (CHUNK_BYTES, FREE_SPACE_RESERVE, TrainerEnvironmentError, TrainerEnvironmentService,
    _copy_file, _decode, _digest, _read_json, _relative, _source, _text, _write_json)


MAX_ARTIFACTS = 128
MAX_ACQUISITION_SECONDS = 4 * 60 * 60
PLAN_FIELDS = {'schema_version', 'status', 'scope', 'python', 'wheels', 'direct_packages',
    'lock_sha256', 'source_revision', 'source_origin', 'source_acquisition', 'download_bytes',
    'destination', 'candidate_workspace', 'storage_budget_bytes', 'storage_scope', 'qualification',
    'execution_ready', 'existing_model_folders_accessed', 'source_license', 'source_inventory', 'assembly_policy'}
PORTABLE_PLAN_FIELDS = PLAN_FIELDS | {'catalog_sha256', 'source_root'}


class RuntimeAcquisitionError(TrainerEnvironmentError):
    pass


def validate_artifact_set(python, wheels):
    """Shared exact artifact validation for stable catalogs and local reviews."""
    if type(wheels) is not list or not 0 < len(wheels) < MAX_ARTIFACTS:
        raise RuntimeAcquisitionError('A bounded exact wheel set is required.')
    names, filenames, total = set(), set(), 0
    for row, kind in [(python, 'python'), *((row, 'wheel') for row in wheels)]:
        license_field = 'license_url' if kind == 'python' else 'license_metadata_url'
        fields = {'kind', 'name', 'version', 'filename', 'url', 'sha256', 'bytes', 'license', license_field}
        if type(row) is not dict or set(row) != fields or row['kind'] != kind:
            raise RuntimeAcquisitionError('An artifact does not match the pinned software schema.')
        for field in ('name', 'version', 'license'): _text(row[field], field)
        _source(row['url'], 'artifact'); _source(row[license_field], 'license')
        _digest(row['sha256']); path = _relative(row['filename'])
        if (len(path.parts) != 1 or not row['filename'].endswith('.tar.gz' if kind == 'python' else '.whl')
                or row['filename'].casefold() in filenames or row['name'].casefold() in names
                or type(row['bytes']) is not int or not 0 < row['bytes'] <= MAX_BYTES):
            raise RuntimeAcquisitionError('Invalid, colliding or unbounded artifact.')
        filenames.add(row['filename'].casefold()); names.add(row['name'].casefold()); total += row['bytes']
    if python['version'].split('.')[:2] != ['3', '11']:
        raise RuntimeAcquisitionError('This headless assembly contract requires pinned CPython 3.11.')
    if total > MAX_BYTES: raise RuntimeAcquisitionError('Artifact total exceeds its software bound.')
    return total


@dataclass(frozen=True)
class RuntimeAcquisitionPlan:
    _canonical: bytes

    @classmethod
    def from_dict(cls, value):
        raw = canonical(value)
        if len(raw) > MAX_MANIFEST_BYTES: raise RuntimeAcquisitionError('Acquisition plan exceeds its bound.')
        value = _decode(raw)
        if (type(value) is not dict or set(value) not in (PLAN_FIELDS, PORTABLE_PLAN_FIELDS) or type(value['schema_version']) is not int
                or value['schema_version'] != 1 or value['status'] != 'review_required'
                or value['execution_ready'] is not False or value['existing_model_folders_accessed'] is not False):
            raise RuntimeAcquisitionError('Unsupported acquisition review schema.')
        total = validate_artifact_set(value['python'], value['wheels'])
        if (type(value['download_bytes']) is not int or value['download_bytes'] != total
                or type(value['storage_budget_bytes']) is not int or not total < value['storage_budget_bytes'] <= MAX_BYTES):
            raise RuntimeAcquisitionError('Acquisition totals or storage budget differ from the reviewed artifacts.')
        for field in ('scope', 'source_acquisition', 'storage_scope'): _text(value[field], field)
        for field in ('destination', 'candidate_workspace'):
            _text(value[field], field)
            if not Path(value[field]).is_absolute(): raise RuntimeAcquisitionError('Select absolute local output paths.')
            if any(Path(value[field]).is_relative_to(Path(root).absolute()) for root in (sys.prefix, sys.base_prefix)):
                raise RuntimeAcquisitionError('Trainer software must remain outside the app Python runtime.')
        _digest(value['lock_sha256']); _source(value['source_origin'], 'trainer source')
        if type(value['source_revision']) is not str or len(value['source_revision']) != 40 or any(c not in '0123456789abcdef' for c in value['source_revision']):
            raise RuntimeAcquisitionError('An exact sd-scripts revision is required.')
        if 'catalog_sha256' in value:
            _digest(value['catalog_sha256']); _text(value['source_root'], 'selected source root')
            if value['source_inventory'] is not None or not Path(value['source_root']).is_absolute():
                raise RuntimeAcquisitionError('Catalog-based setup requires an explicit local source root and fresh local inventory.')
        else:
            if (type(value['source_inventory']) is not dict or set(value['source_inventory']) != {'path', 'sha256'}
                    or not Path(value['source_inventory']['path']).is_absolute()):
                raise RuntimeAcquisitionError('An explicit source inventory is required.')
            _digest(value['source_inventory']['sha256'])
        license_info = value['source_license']
        if type(license_info) is not dict or set(license_info) != {'id', 'path', 'sha256', 'url'}:
            raise RuntimeAcquisitionError('Pinned source license evidence is required.')
        _text(license_info['id'], 'source license'); _text(license_info['path'], 'source license path')
        _digest(license_info['sha256']); _source(license_info['url'], 'source license')
        if 'catalog_sha256' in value and license_info['path'] != str(Path(value['source_root']) / 'LICENSE.md'):
            raise RuntimeAcquisitionError('The reviewed source license must be within the selected source root.')
        for field in ('qualification', 'direct_packages'):
            if type(value[field]) is not list or not value[field] or len(value[field]) > MAX_ARTIFACTS:
                raise RuntimeAcquisitionError('Invalid bounded acquisition overview.')
            for item in value[field]: _text(item, field)
        if not set(value['direct_packages']) <= {row['name'] for row in value['wheels']}:
            raise RuntimeAcquisitionError('Direct packages are absent from the exact wheel set.')
        if type(value['assembly_policy']) is not dict or set(value['assembly_policy']) != {'install', 'candidate', 'downloads', 'publication'}:
            raise RuntimeAcquisitionError('Assembly policy is unavailable.')
        for item in value['assembly_policy'].values(): _text(item, 'assembly policy')
        return cls(canonical(value))

    @property
    def fingerprint(self): return hashlib.sha256(self._canonical).hexdigest()

    def payload(self): return _decode(self._canonical)

    @property
    def artifacts(self):
        value = self.payload()
        return (value['python'], *value['wheels'])

    def confirm(self, fingerprint):
        if type(fingerprint) is not str or fingerprint != self.fingerprint:
            raise RuntimeAcquisitionError('Confirm the exact acquisition plan before creating files or requesting software.')


@dataclass(frozen=True)
class RuntimeAcquisition:
    plan_fingerprint: str
    attempt: Path
    artifacts: tuple[tuple[str, str], ...]

    def paths(self): return {name: Path(path) for name, path in self.artifacts}


class _Budget:
    """Caller reports the complete authorized storage scope; no hidden scans."""
    def __init__(self, plan, root, storage_usage, cancel):
        if not callable(storage_usage): raise RuntimeAcquisitionError('A current storage-scope accounting callback is required.')
        self.limit, self.root, self.usage, self.cancel = plan.payload()['storage_budget_bytes'], root, storage_usage, cancel
        self.deadline, self.written = time.monotonic() + MAX_ACQUISITION_SECONDS, 0
        self.initial = self._usage()
        self.check(0)

    def _usage(self):
        value = self.usage()
        if type(value) is not int or value < 0: raise RuntimeAcquisitionError('Storage usage is unavailable.')
        return value

    def check(self, extra=0):
        _control(self.cancel, self.deadline)
        if max(self._usage(), self.initial + self.written) + extra > self.limit:
            raise RuntimeAcquisitionError('The reviewed storage budget is exhausted; retained files remain available.')
        if shutil.disk_usage(self.root).free < extra + FREE_SPACE_RESERVE:
            raise RuntimeAcquisitionError('Storage is unavailable; retained files remain available.')
        self.written += extra


def _progress(callback, record):
    if callback:
        try: callback(dict(record))
        except Exception: pass


def _verify(path, artifact, cancel, deadline):
    before = _check_path(path)
    if before[2] != artifact['bytes']:
        raise RuntimeAcquisitionError('Artifact bytes do not match the exact reviewed plan.')
    digest, consumed = hashlib.sha256(), 0
    with Path(path).open('rb') as handle:
        if _identity(os.fstat(handle.fileno())) != before: raise RuntimeAcquisitionError('Artifact changed before reading.')
        while True:
            _control(cancel, deadline)
            block = handle.read(min(CHUNK_BYTES, artifact['bytes'] - consumed + 1))
            if not block: break
            consumed += len(block)
            if consumed > artifact['bytes']: raise RuntimeAcquisitionError('Artifact grew beyond its exact byte bound.')
            digest.update(block)
        if _identity(os.fstat(handle.fileno())) != before: raise RuntimeAcquisitionError('Artifact changed during reading.')
    if _check_path(path) != before or consumed != artifact['bytes'] or digest.hexdigest() != artifact['sha256']:
        raise RuntimeAcquisitionError('Artifact bytes do not match the exact reviewed plan.')
    return before


class _HTTPSRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        _source(newurl, 'artifact redirect')
        return super().redirect_request(request, fp, code, message, headers, newurl)


@contextmanager
def _open_https(url):
    _source(url, 'artifact')
    with build_opener(_HTTPSRedirect()).open(Request(url, headers={'User-Agent': 'CollectiGen-runtime-setup/1',
            'Accept-Encoding': 'identity'}), timeout=30) as response:
        if response.status != 200 or response.headers.get('Content-Encoding', 'identity') != 'identity':
            raise RuntimeAcquisitionError('The pinned artifact response has an unsupported encoding or status.')
        yield response


class TrainerRuntimeAcquisition:
    # Reuse the existing OS lease and abandoned-operation recovery behavior.
    # This method only requires self.root; no environment health is invoked.
    _locked = TrainerEnvironmentService._locked

    def __init__(self, plan: RuntimeAcquisitionPlan):
        self.plan = RuntimeAcquisitionPlan.from_dict(plan.payload())
        self.root = Path(self.plan.payload()['destination'])

    def acquire(self, *, confirmed_fingerprint, storage_usage, opener=None, offline_artifacts=None,
                retry_from=None, cancel=None, progress=None, retain_attempt=None):
        if retain_attempt is not None and not callable(retain_attempt):
            raise ValueError("The acquisition recovery recorder must be callable.")
        self.plan.confirm(confirmed_fingerprint)
        with self._locked():
            return self._acquire(storage_usage=storage_usage, opener=opener, offline_artifacts=offline_artifacts,
                                 retry_from=retry_from, cancel=cancel, progress=progress, retain_attempt=retain_attempt)

    def _acquire(self, *, storage_usage, opener, offline_artifacts, retry_from, cancel, progress, retain_attempt):
        root = Path(self.plan.payload()['destination'])
        _assert_unredirected(root); root.mkdir(parents=True, exist_ok=True); _check_path(root, directory=True)
        budget = _Budget(self.plan, root, storage_usage, cancel)
        selected = dict(offline_artifacts or {})
        expected = {row['filename']: row for row in self.plan.artifacts}
        if set(selected) - set(expected): raise RuntimeAcquisitionError('Offline artifacts include software outside this plan.')
        if retry_from is not None:
            previous = Path(retry_from).absolute()
            if previous.parent != root or not previous.name.startswith('attempt-'):
                raise RuntimeAcquisitionError('Retry requires an explicit retained attempt in this acquisition destination.')
            record = _read_json(previous / 'acquisition.json')
            if record != {'schema_version': 1, 'kind': 'trainer-runtime-acquisition', 'plan_fingerprint': self.plan.fingerprint}:
                raise RuntimeAcquisitionError('The retained attempt belongs to another acquisition plan.')
            for name in expected:
                candidate = previous / name
                _assert_unredirected(candidate)
                if candidate.exists(): selected.setdefault(name, candidate)
        attempt = root / ('attempt-' + uuid.uuid4().hex)
        _assert_unredirected(attempt); attempt.mkdir()
        _write_json(attempt / 'acquisition.json', {'schema_version': 1, 'kind': 'trainer-runtime-acquisition', 'plan_fingerprint': self.plan.fingerprint})
        completed, downloaded = [], 0
        try:
            # Required recovery persistence precedes the first artifact read or
            # download. A failed recorder must stop this attempt, not disappear
            # like an optional progress observation.
            if retain_attempt is not None:
                retain_attempt(attempt, self.plan.fingerprint)
            budget.check()
            for index, artifact in enumerate(self.plan.artifacts):
                name = artifact['filename']; output = attempt / name
                budget.check()
                if name in selected:
                    source = Path(selected[name]).absolute(); _verify(source, artifact, cancel, budget.deadline)
                    budget.check(artifact['bytes'])
                    def copy_control(): budget.check(); return False
                    _copy_file(source, output, {'size_bytes': artifact['bytes'], 'sha256': artifact['sha256']}, copy_control, budget.deadline)
                else:
                    partial = attempt / (name + '.partial')
                    digest, copied = hashlib.sha256(), 0
                    with (opener or _open_https)(artifact['url']) as stream, partial.open('xb') as destination:
                        while True:
                            budget.check()
                            block = stream.read(min(CHUNK_BYTES, artifact['bytes'] - copied + 1))
                            if type(block) is not bytes: raise RuntimeAcquisitionError('Artifact response is not a binary stream.')
                            if not block: break
                            if len(block) > CHUNK_BYTES or copied + len(block) > artifact['bytes']:
                                raise RuntimeAcquisitionError('Artifact response exceeded its exact byte limit.')
                            budget.check(len(block)); destination.write(block); digest.update(block); copied += len(block)
                            _progress(progress, {'state': 'downloading', 'artifact': name, 'artifact_bytes': copied,
                                'artifact_total': artifact['bytes'], 'completed': index, 'total': len(expected)})
                        destination.flush(); os.fsync(destination.fileno())
                    if copied != artifact['bytes'] or digest.hexdigest() != artifact['sha256']:
                        raise RuntimeAcquisitionError('Downloaded artifact failed exact size or SHA-256 verification.')
                    _verify(partial, artifact, cancel, budget.deadline)
                    # Exclusive publication; retain failed partials for an explicit new retry.
                    os.link(partial, output); partial.unlink(); downloaded += copied
                _verify(output, artifact, cancel, budget.deadline)
                completed.append((name, str(output)))
                _progress(progress, {'state': 'verified', 'artifact': name, 'completed': index + 1, 'total': len(expected)})
            receipt = {'schema_version': 1, 'kind': 'trainer-runtime-acquired', 'plan_fingerprint': self.plan.fingerprint,
                'artifacts': list(self.plan.artifacts), 'downloaded_bytes': downloaded}
            budget.check(len(canonical(receipt)))
            _write_json(attempt / 'complete.json', receipt)
            return RuntimeAcquisition(self.plan.fingerprint, attempt, tuple(completed))
        except Exception as exc:
            try: _write_json(attempt / 'failure.json', {'schema_version': 1, 'kind': 'trainer-runtime-acquisition-failure',
                'plan_fingerprint': self.plan.fingerprint, 'state': 'cancelled' if isinstance(exc, InterruptedError) else 'failed',
                'completed': len(completed), 'error_type': type(exc).__name__[:100]})
            except (OSError, RuntimeError): pass
            raise
