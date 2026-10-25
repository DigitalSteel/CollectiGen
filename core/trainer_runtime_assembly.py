"""Bounded headless standalone runtime assembly from exact public artifacts.

No archive member executes. Console launchers and documented GUI/test/assets
are omitted with their exact identities; license/notice files are retained.
The reused sd-scripts source_sha256 means the canonical relative source content
digest, not an archive or local filesystem digest. Assembly remains a candidate.
"""
from __future__ import annotations

import base64
import csv
from dataclasses import dataclass
from email.parser import BytesParser
import gzip
import hashlib
import io
import os
from pathlib import Path
import re
import stat
import tarfile
import zipfile

from .training_file_inventory import (FileInventoryError, MAX_FILES, MAX_MANIFEST_BYTES,
    SoftwareInventoryService, _check_path, _hash_file, _scan, canonical)
from .training_source_io import _assert_unredirected, _identity
from .trainer_environment import (CHUNK_BYTES, TrainerEnvironmentError, TrainerProfile,
    TrainerEnvironmentService, _copy_file, _read_json, _relative, _verify_software, _write_json)
from .trainer_environment_pack import _check_zip_directory
from .trainer_runtime_acquisition import (RuntimeAcquisition, RuntimeAcquisitionPlan,
    _Budget, _progress, _verify)
from .trainer_runtime_catalog import HEADLESS_POLICY, RuntimeSoftwareCatalog


MAX_RATIO = 1000
MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_PAX_BYTES = 256 * 1024
OMITTED_SUFFIXES = ('.safetensors', '.ckpt', '.pt', '.pth.tar', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.mp4', '.mov', '.parquet')
HEADLESS_PYTHON = ('lib/test/', 'lib/idlelib/', 'lib/tkinter/', 'lib/ensurepip/', 'tcl/')


@dataclass(frozen=True)
class RuntimeAssembly:
    software_root: Path
    profile: TrainerProfile
    manifest_json: bytes

    def manifest(self):
        from .trainer_environment import _decode
        return _decode(self.manifest_json)


def _license(path):
    return bool(re.match(r'(?i)^(licen[cs]e|copying|copyright|notice)(?:[._-]|$)', Path(path).name))


def _exclusion(path, kind):
    folded = path.casefold()
    if _license(path): return ''
    if kind == 'python' and any(folded.startswith(prefix) for prefix in HEADLESS_PYTHON):
        return 'headless-python-gui-test-or-installer'
    if kind == 'wheel' and any(part in {'test', 'tests'} for part in folded.split('/')[:-1]):
        return 'headless-dependency-tests'
    if kind == 'wheel' and folded.startswith('cv2/data/haarcascade_') and folded.endswith('.xml'):
        return 'unused-opencv-classifier-asset'
    if folded.endswith(OMITTED_SUFFIXES): return 'headless-software-sample-or-media-asset'
    return ''


class _Names:
    def __init__(self): self.entries = {}; self.parents = set(); self.count = 0

    def add(self, path, directory=False, *, shared_directory=False):
        _relative(path)
        self.count += 1
        if self.count > MAX_FILES: raise TrainerEnvironmentError('Archive entry bound exceeded.')
        folded = path.casefold()
        if folded in self.entries and not (shared_directory and directory and self.entries[folded]):
            raise TrainerEnvironmentError('Archive paths collide or repeat.')
        parents = {parent.as_posix().casefold() for parent in Path(path).parents if parent.as_posix() != '.'}
        if any(self.entries.get(parent) is False for parent in parents):
            raise TrainerEnvironmentError('Archive file and directory paths collide.')
        if not directory and folded in self.parents:
            raise TrainerEnvironmentError('Archive file and directory paths collide.')
        self.entries[folded] = directory
        self.parents.update(parents)


def _entry_name(name, directory):
    if directory and name.endswith('/'): name = name[:-1]
    _relative(name)
    return name


def _record_name(name, artifact):
    # This reviewed Windows torchvision wheel uses backslashes only in RECORD.
    # ZIP membership remains canonical; normalize the metadata alias before
    # duplicate/membership/hash checks, never an extraction destination.
    if artifact['name'] == 'torchvision' and artifact['version'] == '0.22.0+cu128':
        name = name.replace('\\', '/')
    _relative(name)
    return name


def _capture_source(root, catalog, budget, progress):
    """Check exact membership before hashing the explicitly selected source."""
    _, actual, _ = _scan(root, budget.cancel, budget.deadline)
    if {name: identity[2] for name, identity in actual} != {row['path']: row['size_bytes'] for row in catalog.source_files}:
        raise FileInventoryError('Selected source membership differs from the pinned content catalog.')
    def observed(done, total, _label):
        _progress(progress, {'state': 'verifying_source', 'completed': done, 'total': total})
    inventory = SoftwareInventoryService().capture(root, should_cancel=budget.cancel, progress=observed)
    content = [{'path': name, 'size_bytes': identity[2], 'sha256': digest} for name, identity, digest in inventory.files]
    if content != list(catalog.source_files):
        raise FileInventoryError('Selected source bytes differ from the pinned content catalog.')
    return inventory


def _pax(raw):
    result, position = {}, 0
    while position < len(raw):
        space = raw.find(b' ', position, position + 20)
        if space < 0: raise TrainerEnvironmentError('Malformed bounded TAR metadata.')
        try: length = int(raw[position:space])
        except ValueError as exc: raise TrainerEnvironmentError('Malformed TAR metadata length.') from exc
        if length <= space - position + 2 or position + length > len(raw):
            raise TrainerEnvironmentError('Malformed TAR metadata extent.')
        record = raw[space + 1:position + length]
        if not record.endswith(b'\n') or b'=' not in record: raise TrainerEnvironmentError('Malformed TAR metadata record.')
        key, value = record[:-1].split(b'=', 1)
        key, value = key.decode('utf-8', 'strict'), value.decode('utf-8', 'strict')
        if key in result or key not in {'path', 'mtime', 'atime', 'ctime', 'uid', 'gid', 'uname', 'gname'}:
            raise TrainerEnvironmentError('Unsupported TAR metadata behavior.')
        result[key] = value; position += length
    return result


class _Assembler:
    def __init__(self, plan, root, budget, progress):
        self.plan, self.root, self.budget, self.progress = plan, root, budget, progress
        self.files, self.excluded, self.archive_files = [], [], []
        self.names = _Names()
        self.unpacked = 0

    def consume(self, stream, size, original, destination, artifact, reason='', expected_hash=None):
        if type(size) is not int or size < 0 or size > self.budget.limit:
            raise TrainerEnvironmentError('Archive member exceeds its size bound.')
        self.unpacked += size
        if self.unpacked > self.budget.limit: raise TrainerEnvironmentError('Combined archive size bound exceeded.')
        self.names.add(destination)
        path = self.root / destination
        output = None
        if not reason:
            _assert_unredirected(path); path.parent.mkdir(parents=True, exist_ok=True); _check_path(path.parent, directory=True)
            output = path.open('xb')
        digest, copied = hashlib.sha256(), 0
        try:
            while copied < size:
                self.budget.check()
                block = stream.read(min(CHUNK_BYTES, size - copied))
                if not block or len(block) > min(CHUNK_BYTES, size - copied):
                    raise TrainerEnvironmentError('Archive member is truncated or exceeds its declared size.')
                if output:
                    self.budget.check(len(block)); output.write(block)
                digest.update(block); copied += len(block)
            if output: output.flush(); os.fsync(output.fileno())
        finally:
            if output: output.close()
        sha = digest.hexdigest()
        if expected_hash is not None and sha != expected_hash:
            raise TrainerEnvironmentError('Wheel member hash differs from its complete RECORD.')
        evidence = {'artifact': artifact['filename'], 'archive_path': original, 'path': destination,
                    'size_bytes': size, 'sha256': sha}
        self.archive_files.append(evidence)
        if reason:
            self.excluded.append({**evidence, 'reason': reason})
        else:
            record = {'path': destination, 'size_bytes': size, 'sha256': sha,
                      'role': 'license' if _license(original) else 'python' if artifact['kind'] == 'python' else 'dependency',
                      'source_url': artifact['url'], 'license': artifact['license']}
            before = _check_path(path)
            if before[2] != size or _hash_file(path, before, self.budget.cancel, self.budget.deadline) != sha:
                raise TrainerEnvironmentError('Extracted software failed read-back verification.')
            self.files.append(record)
        _progress(self.progress, {'state': 'assembling', 'files': len(self.files), 'excluded': len(self.excluded)})

    def python(self, path, artifact):
        before = _verify(path, artifact, self.budget.cancel, self.budget.deadline)
        names, total, pending, headers = _Names(), 0, {}, 0
        with path.open('rb') as source:
            if _identity(os.fstat(source.fileno())) != before: raise TrainerEnvironmentError('Python archive changed.')
            with gzip.GzipFile(fileobj=source) as archive:
                while True:
                    self.budget.check()
                    headers += 1; total += 512
                    if headers > MAX_FILES or total > min(self.budget.limit, artifact['bytes'] * MAX_RATIO):
                        raise TrainerEnvironmentError('Python archive header or compression bound exceeded.')
                    header = archive.read(512)
                    if header == bytes(512):
                        tail = archive.read(10241)
                        if len(tail) > 10240 or len(tail) < 512 or any(tail) or pending:
                            raise TrainerEnvironmentError('Unsupported TAR ending or trailing payload.')
                        break
                    if len(header) != 512: raise TrainerEnvironmentError('Incomplete TAR header.')
                    member = tarfile.TarInfo.frombuf(header, 'utf-8', 'strict')
                    if member.type == tarfile.XHDTYPE:
                        if pending or not 0 <= member.size <= MAX_PAX_BYTES:
                            raise TrainerEnvironmentError('TAR metadata exceeds its bound.')
                        total += (member.size + 511) // 512 * 512
                        if total > min(self.budget.limit, artifact['bytes'] * MAX_RATIO):
                            raise TrainerEnvironmentError('Python archive metadata compression bound exceeded.')
                        raw = archive.read((member.size + 511) // 512 * 512)
                        if len(raw) != (member.size + 511) // 512 * 512:
                            raise TrainerEnvironmentError('Incomplete TAR metadata.')
                        pending = _pax(raw[:member.size]); continue
                    if member.type not in {tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE} or member.linkname:
                        raise TrainerEnvironmentError('TAR links and special entries are unsupported.')
                    name = _entry_name(pending.get('path', member.name), member.isdir()); pending = {}
                    names.add(name, member.isdir())
                    if name != 'python' and not name.startswith('python/'):
                        raise TrainerEnvironmentError('Standalone archive must have the explicit python root.')
                    if member.isdir():
                        if member.size: raise TrainerEnvironmentError('TAR directory has unexpected data.')
                        continue
                    relative = name[len('python/'):]
                    if not relative: raise TrainerEnvironmentError('Standalone root cannot be a file.')
                    total += (member.size + 511) // 512 * 512
                    if total > min(self.budget.limit, artifact['bytes'] * MAX_RATIO): raise TrainerEnvironmentError('Python archive compression bound exceeded.')
                    self.consume(archive, member.size, name, 'python/' + relative, artifact, _exclusion(relative, 'python'))
                    padding = (-member.size) % 512
                    if len(archive.read(padding)) != padding: raise TrainerEnvironmentError('Incomplete TAR padding.')
            if _identity(os.fstat(source.fileno())) != before or _check_path(path) != before:
                raise TrainerEnvironmentError('Python archive changed during extraction.')

    def wheel(self, path, artifact):
        before = _verify(path, artifact, self.budget.cancel, self.budget.deadline)
        with path.open('rb') as source:
            if _identity(os.fstat(source.fileno())) != before: raise TrainerEnvironmentError('Wheel changed before reading.')
            _check_zip_directory(source, before[2])
            with zipfile.ZipFile(source) as archive:
                names, entries = _Names(), {}
                for info in archive.infolist():
                    if info.orig_filename != info.filename:
                        raise TrainerEnvironmentError('Wheel contains a normalized or truncated archive path.')
                    name = _entry_name(info.orig_filename, info.is_dir()); names.add(name, info.is_dir())
                    mode = (info.external_attr >> 16) & 0o170000
                    if (info.flag_bits & 1 or mode not in {0, stat.S_IFREG, stat.S_IFDIR}
                            or info.external_attr & 0x400 or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                            or info.file_size > max(1, info.compress_size) * MAX_RATIO or info.file_size < 0):
                        raise TrainerEnvironmentError('Wheel contains linked, encrypted or over-compressed entries.')
                    if info.is_dir():
                        if info.file_size: raise TrainerEnvironmentError('Wheel directory contains data.')
                    else: entries[name] = info
                records = [name for name in entries if name.endswith('.dist-info/RECORD') and name.count('/') == 1]
                if len(records) != 1: raise TrainerEnvironmentError('Wheel requires one complete RECORD.')
                record_name = records[0]; dist = record_name.rsplit('/', 1)[0]
                def read_metadata(name):
                    if name not in entries or entries[name].file_size > MAX_METADATA_BYTES:
                        raise TrainerEnvironmentError('Required wheel metadata is absent or too large.')
                    with archive.open(entries[name]) as handle: return handle.read(MAX_METADATA_BYTES + 1)
                rows = {}
                for row in csv.reader(io.StringIO(read_metadata(record_name).decode('utf-8', 'strict'), newline='')):
                    if len(row) != 3: raise TrainerEnvironmentError('Wheel RECORD is incomplete or repeats files.')
                    name = _record_name(row[0], artifact)
                    if name in rows: raise TrainerEnvironmentError('Wheel RECORD is incomplete or repeats files.')
                    rows[name] = row[1:]
                if set(rows) != set(entries): raise TrainerEnvironmentError('Wheel RECORD does not cover the exact archive membership.')
                hashes = {}
                for name, (digest, size) in rows.items():
                    if name == record_name:
                        if digest or size: raise TrainerEnvironmentError('Wheel RECORD self-entry must be empty.')
                        hashes[name] = None; continue
                    if not digest.startswith('sha256=') or not size.isdecimal() or int(size) != entries[name].file_size:
                        raise TrainerEnvironmentError('Wheel RECORD lacks an exact SHA-256 or size.')
                    try: decoded = base64.b64decode(digest[7:] + '=' * (-len(digest[7:]) % 4), altchars=b'-_', validate=True)
                    except ValueError as exc: raise TrainerEnvironmentError('Invalid wheel RECORD digest.') from exc
                    if len(decoded) != 32 or base64.urlsafe_b64encode(decoded).rstrip(b'=').decode() != digest[7:]:
                        raise TrainerEnvironmentError('Invalid canonical wheel RECORD digest.')
                    hashes[name] = decoded.hex()
                metadata = BytesParser().parsebytes(read_metadata(dist + '/METADATA'))
                normalize = lambda value: re.sub(r'[-_.]+', '-', value).casefold()
                if (len(metadata.get_all('Name', [])) != 1 or len(metadata.get_all('Version', [])) != 1
                        or normalize(metadata['Name']) != normalize(artifact['name']) or metadata['Version'] != artifact['version']):
                    raise TrainerEnvironmentError('Wheel identity differs from its reviewed package version.')
                wheel = BytesParser().parsebytes(read_metadata(dist + '/WHEEL'))
                if (wheel.get_all('Wheel-Version', []) != ['1.0']
                        or wheel.get_all('Root-Is-Purelib', []) not in (['true'], ['false'])):
                    raise TrainerEnvironmentError('Unsupported wheel installation metadata.')
                def compatible(tag):
                    parts = tag.split('-')
                    if len(parts) != 3: return False
                    interpreter, abi, platform = parts
                    if platform not in {'any', 'win_amd64'}: return False
                    if interpreter in {'py3', 'py2.py3'}: return abi == 'none'
                    if interpreter == 'cp311': return abi in {'none', 'cp311', 'abi3'}
                    return abi == 'abi3' and interpreter.startswith('cp3') and interpreter[3:].isdecimal() and 2 <= int(interpreter[3:]) < 11
                if not any(compatible(tag) for tag in wheel.get_all('Tag', [])):
                    raise TrainerEnvironmentError('Wheel has no supported CPython 3.11 Windows x64 tag.')
                for name, info in entries.items():
                    relative, reason = name, _exclusion(name, 'wheel')
                    if name.split('/', 1)[0].endswith('.data'):
                        prefix, category, *rest = name.split('/')
                        if prefix != dist[:-10] + '.data' or not rest:
                            raise TrainerEnvironmentError('Unsupported wheel relocation.')
                        if category in {'purelib', 'platlib'}: relative = '/'.join(rest)
                        elif category == 'scripts': reason = '' if _license(name) else 'unused-console-entry-point'; relative = name
                        elif (artifact['name'] == 'sympy' and artifact['version'] == '1.14.0'
                                and name == 'sympy-1.14.0.data/data/share/man/man1/isympy.1'):
                            reason, relative = 'unused-console-manual-page', name
                        else: raise TrainerEnvironmentError('Unsupported wheel data/header relocation.')
                    with archive.open(info) as handle:
                        self.consume(handle, info.file_size, name, 'environment/Lib/site-packages/' + relative,
                                     artifact, reason, hashes[name])
                        if handle.read(1): raise TrainerEnvironmentError('Wheel member exceeds its declared size.')
            if _identity(os.fstat(source.fileno())) != before or _check_path(path) != before:
                raise TrainerEnvironmentError('Wheel changed during extraction.')


class TrainerRuntimeAssembly:
    _locked = TrainerEnvironmentService._locked

    def __init__(self, plan: RuntimeAcquisitionPlan):
        self.plan = RuntimeAcquisitionPlan.from_dict(plan.payload())
        self.root = Path(self.plan.payload()['destination'])

    def assemble(self, acquisition, destination, *, source_root, confirmed_fingerprint, storage_usage,
                 catalog=None, cancel=None, progress=None):
        self.plan.confirm(confirmed_fingerprint)
        payload = self.plan.payload()
        if 'catalog_sha256' in payload:
            if not isinstance(catalog, RuntimeSoftwareCatalog):
                raise TrainerEnvironmentError('The exact reviewed portable software catalog is required.')
            catalog = RuntimeSoftwareCatalog.from_dict(catalog.payload()); catalog.validate_plan(self.plan)
            if Path(source_root).absolute() != Path(payload['source_root']):
                raise TrainerEnvironmentError('The selected source root changed after the local review.')
        with self._locked():
            return self._assemble(acquisition, destination, source_root=source_root,
                                  storage_usage=storage_usage, catalog=catalog, cancel=cancel, progress=progress)

    def _assemble(self, acquisition, destination, *, source_root, storage_usage, catalog, cancel, progress):
        payload = self.plan.payload(); destination = Path(destination).absolute()
        candidate = Path(payload['candidate_workspace'])
        if destination.parent != candidate: raise TrainerEnvironmentError('Select a new assembly within the reviewed candidate workspace.')
        if not isinstance(acquisition, RuntimeAcquisition) or acquisition.plan_fingerprint != self.plan.fingerprint:
            raise TrainerEnvironmentError('Acquisition belongs to another reviewed plan.')
        paths = acquisition.paths()
        if (len(paths) != len(acquisition.artifacts) or set(paths) != {row['filename'] for row in self.plan.artifacts}
                or acquisition.attempt.parent != Path(payload['destination'])
                or any(path != acquisition.attempt / name for name, path in paths.items())):
            raise TrainerEnvironmentError('Acquisition paths do not match their exact retained attempt.')
        receipt = _read_json(acquisition.attempt / 'complete.json')
        if (set(receipt) != {'schema_version', 'kind', 'plan_fingerprint', 'artifacts', 'downloaded_bytes'}
                or receipt['schema_version'] != 1 or receipt['kind'] != 'trainer-runtime-acquired'
                or receipt['plan_fingerprint'] != self.plan.fingerprint or receipt['artifacts'] != list(self.plan.artifacts)
                or type(receipt['downloaded_bytes']) is not int or not 0 <= receipt['downloaded_bytes'] <= payload['download_bytes']):
            raise TrainerEnvironmentError('Acquisition has no matching complete receipt.')
        _assert_unredirected(candidate); candidate.mkdir(parents=True, exist_ok=True); _check_path(candidate, directory=True)
        budget = _Budget(self.plan, candidate, storage_usage, cancel)
        source_root = Path(source_root).absolute()
        declared = payload['source_inventory']
        if declared is not None:
            manifest_path = Path(declared['path']); before = _check_path(manifest_path)
            if before[2] > MAX_MANIFEST_BYTES: raise TrainerEnvironmentError('Source inventory exceeds its bound.')
            _verify(manifest_path, {'bytes': before[2], 'sha256': declared['sha256']}, cancel, budget.deadline)
            original = SoftwareInventoryService.load(source_root, manifest_path)
            derived = RuntimeSoftwareCatalog.from_plan(self.plan, [
                {'path': name, 'size_bytes': identity[2], 'sha256': digest} for name, identity, digest in original.files])
            if catalog is not None and (not isinstance(catalog, RuntimeSoftwareCatalog) or catalog.fingerprint != derived.fingerprint):
                raise TrainerEnvironmentError('Runtime catalog differs from the reviewed source content.')
            catalog = derived
        catalog.validate_plan(self.plan)
        # Reject unlisted source membership before opening file contents. The
        # current installation gets fresh identities, independent of its origin.
        inventory = _capture_source(source_root, catalog, budget, progress)
        _assert_unredirected(destination); destination.mkdir()
        software = destination / 'software'; software.mkdir()
        assembler = _Assembler(self.plan, software, budget, progress)
        try:
            local_inventory = destination / 'source-inventory.json'
            budget.check(len(canonical(inventory.payload())))
            SoftwareInventoryService.save(inventory, local_inventory)
            assembler.python(paths[payload['python']['filename']], payload['python'])
            for artifact in payload['wheels']: assembler.wheel(paths[artifact['filename']], artifact)
            for name, identity, digest in inventory.files:
                relative = 'source/' + name; assembler.names.add(relative); budget.check(identity[2])
                record = {'path': relative, 'size_bytes': identity[2], 'sha256': digest,
                          'role': 'license' if _license(name) else 'trainer', 'source_url': payload['source_origin'],
                          'license': payload['source_license']['id']}
                _copy_file(source_root / name, software / relative, record, cancel, budget.deadline)
                assembler.files.append(record)
            if _capture_source(source_root, catalog, budget, progress) != inventory:
                raise FileInventoryError('Selected source filesystem identity changed during assembly.')
            if declared is not None:
                _verify(manifest_path, {'bytes': before[2], 'sha256': declared['sha256']}, cancel, budget.deadline)
            torch = next((row for row in payload['wheels'] if row['name'] == 'torch'), None)
            if torch is None: raise TrainerEnvironmentError('The exact PyTorch backend is absent from the reviewed wheel set.')
            profile = TrainerProfile.from_dict({'schema_version': 1, 'profile_id': 'sdxl-headless-' + catalog.fingerprint,
                'python': {'version': payload['python']['version'], 'executable': 'python/python.exe',
                    'source_url': payload['python']['url'], 'source_sha256': payload['python']['sha256'], 'license': payload['python']['license']},
                'trainer': {'name': 'sd-scripts', 'revision': payload['source_revision'], 'source_url': payload['source_origin'],
                    'source_sha256': catalog.source_digest, 'license': payload['source_license']['id']},
                'backend': {'name': 'pytorch', 'version': torch['version'], 'platform': 'windows-x86_64'}, 'files': assembler.files})
            _verify_software(software, profile, cancel, budget.deadline)
            for artifact in self.plan.artifacts: _verify(paths[artifact['filename']], artifact, cancel, budget.deadline)
            manifest = {'schema_version': 1, 'kind': 'trainer-runtime-assembly-candidate', 'plan_fingerprint': self.plan.fingerprint,
                'headless_policy': HEADLESS_POLICY, 'catalog_sha256': catalog.fingerprint, 'catalog': catalog.payload(),
                'profile_sha256': profile.sha256, 'artifacts': list(self.plan.artifacts), 'source_inventory_sha256': inventory.fingerprint,
                'declared_source_inventory_sha256': declared['sha256'] if declared else None,
                'source_content_sha256': catalog.source_digest,
                'source_sha256_meaning': 'canonical relative source content inventory', 'files': list(profile.files),
                'archive_files': assembler.archive_files, 'excluded': assembler.excluded,
                'remaining_gates': ['non-mutating health', 'native execution and recovery', 'offline pack relocation']}
            budget.check(len(canonical(manifest)) + len(profile._canonical) + len(catalog._canonical))
            _write_json(destination / 'profile.json', profile.payload()); _write_json(destination / 'catalog.json', catalog.payload())
            _write_json(destination / 'assembly.json', manifest)
            return RuntimeAssembly(software, profile, canonical(manifest))
        except Exception as exc:
            try: _write_json(destination / 'failure.json', {'schema_version': 1, 'kind': 'trainer-runtime-assembly-failure',
                'plan_fingerprint': self.plan.fingerprint, 'state': 'cancelled' if isinstance(exc, InterruptedError) else 'failed',
                'error_type': type(exc).__name__[:100]})
            except (OSError, RuntimeError): pass
            raise
