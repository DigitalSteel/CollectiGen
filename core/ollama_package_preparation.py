"""Ollama Windows x64 package preparation only; no installation or execution.

Explicit reviewed descriptor and scratch roots required. A successful receipt
means Package prepared, installation incomplete. All failure stages are retained.
"""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import zipfile

from core.application_update import ApplicationUpdateService
from release_contract import ReleaseArtifact, ReleaseContractError, safe_zip_members, verify_artifact
from suite_core.discovery.component_staging import guard

ASSET = 'ollama-windows-amd64.zip'
HEX = re.compile(r'[0-9a-f]{64}\Z')
VERSION = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z')
RESERVED = {'con', 'prn', 'aux', 'nul', *(f'com{x}' for x in range(1, 10)), *(f'lpt{x}' for x in range(1, 10))}

class Hold(ValueError):
    pass

def require(ok, reason):
    if not ok:
        raise Hold(reason)

def pin(value):
    require(type(value) is str and HEX.fullmatch(value), 'exact_sha256_required')
    return value

def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()

@dataclass(frozen=True)
class ArchiveLimits:
    members: int
    unpacked_bytes: int
    member_bytes: int

    def __post_init__(self):
        require(all(type(v) is int and v > 0 for v in (self.members, self.unpacked_bytes, self.member_bytes)), 'explicit_positive_archive_limits_required')
        require(self.members <= 8192 and self.member_bytes <= self.unpacked_bytes <= 32 * 1024**3, 'archive_limits_out_of_contract')

@dataclass(frozen=True)
class OllamaZipDescriptor:
    version: str
    archive_sha256: str
    archive_bytes: int
    release_metadata_sha256: str
    review_reference: str
    limits: ArchiveLimits

    def __post_init__(self):
        require(type(self.version) is str and len(self.version) <= 64 and VERSION.fullmatch(self.version), 'exact_stable_ollama_version_required')
        pin(self.archive_sha256)
        pin(self.release_metadata_sha256)
        require(type(self.archive_bytes) is int and 0 < self.archive_bytes <= 4 * 1024**3, 'explicit_bounded_archive_size_required')
        require(type(self.review_reference) is str and 0 < len(self.review_reference) <= 512 and not any(ord(c) < 32 for c in self.review_reference), 'review_reference_required')
        require(type(self.limits) is ArchiveLimits, 'ollama_archive_limits_required')

    @property
    def release_url(self):
        return f'https://github.com/ollama/ollama/releases/tag/v{self.version}'

    @property
    def download_url(self):
        return f'https://github.com/ollama/ollama/releases/download/v{self.version}/{ASSET}'

    @property
    def provenance(self):
        return 'reviewed-upstream-release; not a CollectiGen signed release'

    @property
    def descriptor_pin(self):
        return fingerprint(asdict(self))

    def transport_inputs(self):
        # Public explicit-cache seam only: never download_and_prepare/_queue.
        from core.application_update import ReleaseAssetSource
        return (ReleaseAssetSource(ASSET, self.download_url, self.archive_bytes),
                ReleaseArtifact(ASSET, 'ollama-upstream-windows-x64-zip', self.archive_sha256, self.archive_bytes, ''))


@dataclass(frozen=True)
class PreparedFile:
    relative_path: str
    size_bytes: int
    sha256: str

@dataclass(frozen=True)
class PreparedOllamaPackage:
    descriptor_pin: str
    archive_sha256: str
    stage_directory: str
    payload_directory: str
    files: tuple[PreparedFile, ...]
    inventory_sha256: str
    receipt_path: str
    completion_label: str = 'Package prepared, installation incomplete'


class PreparationCleanupError(OSError):
    """A verified package receipt exists, but the exact scratch lock remains."""
    def __init__(self, prepared_receipt, cleanup_error):
        super().__init__('Package prepared, installation incomplete; scratch lock cleanup failed.')
        self.prepared_receipt = prepared_receipt
        self.cleanup_error = cleanup_error


def _windows_parts(name):
    require(type(name) is str and 0 < len(name) <= 1024, 'bounded_member_name_required')
    parts = (name[:-1] if name.endswith('/') else name).split('/')
    require(0 < len(parts) <= 32 and all(parts), 'noncanonical_member_path')
    for part in parts:
        require(part not in {'.', '..'} and len(part) <= 255 and not part.endswith((' ', '.'))
                and not any(ord(c) < 32 or c in '<>:"\\|?*' for c in part)
                and part.split('.')[0].casefold() not in RESERVED, 'unsafe_windows_member')
    return tuple(parts)


def _members(archive, limits):
    infos = archive.infolist()
    require(0 < len(infos) <= limits.members, 'archive_member_bound')
    require(all(i.filename == i.orig_filename for i in infos), 'normalized_or_truncated_member_name')
    safe_zip_members([i.filename for i in infos])
    kinds, directories = {}, set()
    for info in infos:
        parts = _windows_parts(info.filename)
        name = '/'.join(parts)
        kinds[name.casefold()] = info.is_dir()
        directories.update('/'.join(parts[:i]) for i in range(1, len(parts)))
        if info.is_dir():
            directories.add(name)
        mode = stat.S_IFMT(info.external_attr >> 16)
        require(mode in (0, stat.S_IFDIR if info.is_dir() else stat.S_IFREG), 'archive_special_file')
        require(not info.flag_bits & 1 and info.compress_type in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}, 'archive_encrypted_or_unsupported_compression')
        require(0 <= info.file_size <= limits.member_bytes and (not info.is_dir() or info.file_size == 0), 'archive_member_size_bound')
    require(len({name.casefold() for name in directories}) == len(directories), 'case_colliding_parent_directory')
    require(sum(i.file_size for i in infos) <= limits.unpacked_bytes, 'archive_total_size_bound')
    for key in kinds:
        parts = key.split('/')
        require(not any('/'.join(parts[:i]) in kinds and not kinds['/'.join(parts[:i])] for i in range(1, len(parts))), 'file_directory_collision')
    require(any(i.filename == 'ollama.exe' and not i.is_dir() and i.file_size > 0 for i in infos), 'exact_root_ollama_entry_required')
    return infos, directories


def _identity(path):
    guard(path)
    info = path.stat()
    return info.st_dev, info.st_ino


def _exclusive_bytes(path, raw):
    guard(path)
    with path.open('xb') as stream:
        require(stream.write(raw) == len(raw), 'short_metadata_write')
        stream.flush()
        os.fsync(stream.fileno())
    guard(path)
    require(path.read_bytes() == raw, 'metadata_readback_mismatch')


def prepare_ollama_package(descriptor, transport, *, cache_root, stage_directory,
                           progress=None, should_cancel=None):
    """Disk-backed one-pass extraction plus staged read-back; Windows scratch only.

    progress(phase, current, total, detail), phases download/extract/verify.
    Exclusive stage/cache-lock paths hold collisions. No failed stage is removed.
    Caller retains descriptor authorization and ownership of explicit scratch.
    Check-before-use guards are not hostile-race/handle-based qualification.
    """
    require(os.name == 'nt', 'windows_preparation_only')
    require(type(descriptor) is OllamaZipDescriptor and type(transport) is ApplicationUpdateService, 'reviewed_descriptor_and_application_transport_required')
    cache_root, stage = Path(cache_root), Path(stage_directory)
    guard(cache_root); guard(stage)
    require(cache_root.is_dir() and stage.parent.is_dir(), 'existing_explicit_scratch_parents_required')
    require(not cache_root.is_relative_to(stage) and not stage.is_relative_to(cache_root), 'cache_stage_overlap')
    # Check collision before transport. mkdir is exclusive even if preflight races.
    require(not stage.exists(), 'stage_destination_exists')
    stage.mkdir()
    identity = _identity(stage)
    marker = stage / 'PREPARATION.json'
    marker_raw = json.dumps({'schema':1, 'kind':'ollama-package-preparation',
        'descriptor_pin':descriptor.descriptor_pin, 'scope':'package-only',
        'status':'preparing-or-incomplete'}, sort_keys=True).encode()
    _exclusive_bytes(marker, marker_raw)
    payload = stage / 'payload'
    cache = cache_root / descriptor.descriptor_pin
    guard(cache)
    cache.mkdir(exist_ok=True)
    cache_identity = _identity(cache)
    lock = cache / 'preparation.lock'
    lock_raw = descriptor.descriptor_pin.encode()
    _exclusive_bytes(lock, lock_raw)  # no stale lock deletion/adoption
    lock_identity = _identity(lock)

    def check():
        guard(marker); guard(lock)
        require(_identity(lock) == lock_identity, 'cache_lock_changed')
        require(_identity(stage) == identity and marker.read_bytes() == marker_raw, 'stage_owner_changed')
        require(_identity(cache) == cache_identity and lock.read_bytes() == lock_raw, 'cache_owner_changed')
        if should_cancel and should_cancel():
            raise InterruptedError('Ollama package preparation cancelled; incomplete stage retained.')

    def report(phase, current, total, detail):
        if progress:
            progress(phase, current, total, detail)

    primary_error = None
    prepared_receipt = None
    try:
        check()
        source, artifact = descriptor.transport_inputs()
        for name in (ASSET, ASSET + '.part', ASSET + '.part.rejected'):
            guard(cache / name)
        archive_path = transport.download_artifact(source, artifact, cache_directory=cache,
            should_cancel=should_cancel,
            progress=lambda current,total,name:report('download',current,total,name))
        check()
        archive_path = Path(archive_path)
        guard(archive_path)
        require(archive_path == cache / ASSET, 'transport_archive_binding_changed')
        verify_artifact(archive_path, artifact)
        archive_stamp = archive_path.stat()
        with zipfile.ZipFile(archive_path) as archive:
            infos, directories = _members(archive, descriptor.limits)
            check()
            payload.mkdir()
            for directory in sorted(directories, key=lambda name:(name.count('/'),name.casefold())):
                check()
                target = payload.joinpath(*directory.split('/'))
                guard(target)
                target.mkdir(exist_ok=True)
            files, copied = [], 0
            total = sum(i.file_size for i in infos)
            for info in infos:
                if info.is_dir():
                    continue
                check()
                target = payload.joinpath(*_windows_parts(info.filename))
                guard(target)
                digest, count = sha256(), 0
                # Exactly one decompression pass. Source CRC is checked by ZipFile.
                with archive.open(info) as incoming, target.open('xb') as outgoing:
                    while True:
                        check()
                        block = incoming.read(64 * 1024)
                        if not block:
                            break
                        count += len(block)
                        require(count <= info.file_size and count <= descriptor.limits.member_bytes, 'observed_member_size_bound')
                        require(outgoing.write(block) == len(block), 'short_payload_write')
                        digest.update(block)
                        copied += len(block)
                        report('extract',copied,total,info.filename)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                require(count == info.file_size, 'observed_member_size_mismatch')
                files.append(PreparedFile(info.filename,count,digest.hexdigest()))
        check()
        now = archive_path.stat()
        require((now.st_dev,now.st_ino,now.st_size,now.st_mtime_ns) ==
                (archive_stamp.st_dev,archive_stamp.st_ino,archive_stamp.st_size,archive_stamp.st_mtime_ns), 'archive_changed_during_extraction')
        files = tuple(sorted(files,key=lambda item:item.relative_path.casefold()))
        expected_files = {item.relative_path for item in files}
        observed_files, observed_dirs, pending = set(), set(), [payload]
        # Only inspect this newly owned payload, never profile/model directories.
        while pending:
            check()
            directory = pending.pop()
            guard(directory)
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    guard(path)
                    name = path.relative_to(payload).as_posix()
                    if entry.is_dir(follow_symlinks=False):
                        require(name in directories, 'unexpected_staged_directory')
                        observed_dirs.add(name); pending.append(path)
                    else:
                        require(entry.is_file(follow_symlinks=False) and name in expected_files, 'unexpected_staged_file')
                        observed_files.add(name)
        require(observed_files == expected_files and observed_dirs == directories, 'staged_inventory_mismatch')
        verified = 0
        stamps = {}
        def stamp(path):
            guard(path)
            info = path.stat()
            return info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns
        for name in directories:
            path = payload.joinpath(*name.split('/'))
            stamps[path] = stamp(path)
        stamps[payload] = stamp(payload)
        for item in files:
            check()
            path = payload.joinpath(*item.relative_path.split('/'))
            guard(path)
            stamps[path] = stamp(path)
            digest, count = sha256(), 0
            with path.open('rb') as stream:
                while block := stream.read(64 * 1024):
                    check()
                    count += len(block)
                    require(count <= item.size_bytes, 'staged_size_mismatch')
                    digest.update(block)
                    verified += len(block)
                    report('verify',verified,total,item.relative_path)
            require(count == item.size_bytes and digest.hexdigest() == item.sha256, 'staged_content_mismatch')
        check()
        require(all(stamp(path) == before for path,before in stamps.items()), 'staged_changed_during_readback')
        inventory_pin = fingerprint([asdict(item) for item in files])
        receipt = PreparedOllamaPackage(descriptor.descriptor_pin,descriptor.archive_sha256,
            str(stage),str(payload),files,inventory_pin,str(stage/'PACKAGE_PREPARED.json'))
        raw = json.dumps(asdict(receipt),sort_keys=True).encode()
        temporary = stage / 'PACKAGE_PREPARED.pending'
        _exclusive_bytes(temporary, raw)
        check()
        require(all(stamp(path) == before for path,before in stamps.items()), 'staged_changed_before_completion')
        complete = Path(receipt.receipt_path)
        guard(complete)
        require(not complete.exists(), 'completion_receipt_collision')
        # Windows rename refuses an existing destination; never replace another file.
        temporary.rename(complete)
        guard(complete)
        require(complete.read_bytes() == raw, 'completion_receipt_readback_mismatch')
        prepared_receipt = receipt
        return receipt
    except (zipfile.BadZipFile, ReleaseContractError, NotImplementedError) as error:
        primary_error = Hold('Ollama package preparation failed; incomplete stage retained: ' + str(error))
        raise primary_error from error
    except BaseException as error:
        primary_error = error
        raise
    finally:
        # Keep the primary failure/cause; never relabel a prepared package incomplete.
        try:
            guard(lock)
            if lock.exists() and _identity(lock) == lock_identity and lock.read_bytes() == lock_raw:
                lock.unlink()
        except Exception as cleanup_error:
            if primary_error is not None:
                primary_error.preparation_cleanup_error = cleanup_error
                primary_error.add_note('Scratch lock cleanup also failed; lock and stage retained.')
            else:
                raise PreparationCleanupError(prepared_receipt, cleanup_error) from cleanup_error
