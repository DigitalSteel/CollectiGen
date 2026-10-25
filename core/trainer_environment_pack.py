"""Verified offline packs for one reviewed, software-only trainer profile.

Packs contain standalone Python, trainer source, dependencies and license files.
No model/dataset acquisition, discovery, extraction commands or app activation
exist here. Imported health receipts are never trusted: staging requires a new
local health probe, and active-generation selection remains explicit.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import struct
import time
import uuid
import zipfile

from .training_file_inventory import (
    MAX_BYTES, MAX_FILES, MAX_MANIFEST_BYTES, MAX_SECONDS, _check_path, _control, canonical,
)
from .training_source_io import _assert_unredirected, _identity
from .trainer_environment import (
    software_phase_seconds,
    CHUNK_BYTES, TrainerEnvironmentError, TrainerEnvironmentService, TrainerProfile,
    _decode, _relative,
)


PACK_MANIFEST = "collectigen-trainer-pack.json"
MAX_COMPRESSION_RATIO = 1000
MAX_CENTRAL_DIRECTORY_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class TrainerPackInfo:
    profile_id: str
    profile_sha256: str
    file_count: int
    byte_count: int


def _check_zip_directory(handle, archive_size):
    """Bound the central directory before ZipFile allocates entry objects."""
    if archive_size > MAX_BYTES + 3 * MAX_MANIFEST_BYTES:
        raise TrainerEnvironmentError("Trainer pack exceeds its archive-size bound.")
    handle.seek(max(0, archive_size - 65557))
    tail = handle.read(65557)
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or len(tail) - offset < 22:
        raise TrainerEnvironmentError("Trainer pack has no valid ZIP directory.")
    _, disk, directory_disk, disk_entries, count, size, start, comment = struct.unpack_from("<4s4H2LH", tail, offset)
    end = archive_size - len(tail) + offset
    if disk or directory_disk or disk_entries != count or len(tail) - offset != 22 + comment:
        raise TrainerEnvironmentError("Trainer pack has an unsupported ZIP layout.")
    # Writers may emit ZIP64 records even when the classic fields still fit.
    # Accept that standard trailer only with exact geometry and agreeing fields.
    required64 = count == 0xFFFF or size == 0xFFFFFFFF or start == 0xFFFFFFFF
    locator = b""
    if end >= 20:
        handle.seek(end - 20)
        locator = handle.read(20)
    if required64 or locator[:4] == b"PK\x06\x07":
        if len(locator) != 20:
            raise TrainerEnvironmentError("Trainer pack has an invalid ZIP64 directory.")
        legacy_count, legacy_size, legacy_start = count, size, start
        signature, disk64, record_start, disks = struct.unpack("<4sLQL", locator)
        if signature != b"PK\x06\x07" or disk64 or disks != 1 or record_start > end - 76:
            raise TrainerEnvironmentError("Trainer pack has an invalid ZIP64 locator.")
        handle.seek(record_start)
        record = handle.read(56)
        if len(record) != 56:
            raise TrainerEnvironmentError("Trainer pack has an incomplete ZIP64 directory.")
        signature, record_size, _, _, disk, directory_disk, disk_entries, count, size, start = struct.unpack("<4sQ2H2L4Q", record)
        if (signature != b"PK\x06\x06" or record_size != 44 or disk or directory_disk
                or disk_entries != count or record_start + 56 != end - 20):
            raise TrainerEnvironmentError("Trainer pack has an unsupported ZIP64 directory.")
        if any(old != sentinel and old != current for old, sentinel, current in (
                (legacy_count, 0xFFFF, count), (legacy_size, 0xFFFFFFFF, size),
                (legacy_start, 0xFFFFFFFF, start))):
            raise TrainerEnvironmentError("Trainer pack has conflicting ZIP directory fields.")
        end = record_start
    if count > MAX_FILES + 1 or size > MAX_CENTRAL_DIRECTORY_BYTES or start + size != end:
        raise TrainerEnvironmentError("Trainer pack exceeds its ZIP directory bounds.")
    handle.seek(0)


def _entry(info):
    if info.orig_filename != info.filename:
        raise TrainerEnvironmentError("Trainer pack contains a normalized or truncated archive path.")
    _relative(info.orig_filename)
    mode = (info.external_attr >> 16) & 0o170000
    if (info.is_dir() or info.flag_bits & 1 or mode not in {0, stat.S_IFREG}
            or info.external_attr & 0x400 or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
            or info.file_size < 0 or info.compress_size < 0
            or info.file_size > max(1, info.compress_size) * MAX_COMPRESSION_RATIO):
        raise TrainerEnvironmentError("Trainer pack contains a linked, encrypted, unsupported or over-compressed entry.")


def _copy_entry(package, info, record, output, cancel, deadline):
    digest, copied = hashlib.sha256(), 0
    with package.open(info) as source:
        while True:
            _control(cancel, deadline)
            block = source.read(min(CHUNK_BYTES, record["size_bytes"] - copied + 1))
            if not block:
                break
            copied += len(block)
            if copied > record["size_bytes"]:
                raise TrainerEnvironmentError("Trainer pack exceeded its declared software size.")
            if output is not None:
                output.write(block)
            digest.update(block)
    if copied != record["size_bytes"] or digest.hexdigest() != record["sha256"]:
        raise TrainerEnvironmentError(f"Trainer pack software verification failed: {record['path']}.")


class TrainerEnvironmentPack:
    def __init__(self, service: TrainerEnvironmentService):
        self.service = service

    def _manifest(self, package):
        infos = package.infolist()
        if len(infos) > MAX_FILES + 1:
            raise TrainerEnvironmentError("Trainer pack exceeds its file-count bound.")
        seen, by_name = set(), {}
        for info in infos:
            _entry(info)
            folded = info.filename.casefold()
            if folded in seen:
                raise TrainerEnvironmentError("Trainer pack contains colliding paths.")
            seen.add(folded)
            by_name[info.filename] = info
        manifest = by_name.get(PACK_MANIFEST)
        if manifest is None or manifest.file_size > MAX_MANIFEST_BYTES:
            raise TrainerEnvironmentError("Trainer pack has no bounded manifest.")
        with package.open(manifest) as source:
            raw = source.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) != manifest.file_size:
            raise TrainerEnvironmentError("Trainer pack manifest size changed.")
        payload = _decode(raw)
        if (type(payload) is not dict or set(payload) != {"schema_version", "kind", "profile", "profile_sha256"}
                or type(payload["schema_version"]) is not int or payload["schema_version"] != 1
                or payload["kind"] != "collectigen-trainer-software-pack"
                or canonical(payload) != raw):
            raise TrainerEnvironmentError("Unsupported trainer pack manifest.")
        profile = TrainerProfile.from_dict(payload["profile"])
        if payload["profile_sha256"] != profile.sha256 or profile.sha256 != self.service.profile.sha256:
            raise TrainerEnvironmentError("Trainer pack does not match the exact reviewed profile.")
        expected = {item["path"] for item in profile.files}
        if PACK_MANIFEST in expected or set(by_name) != expected | {PACK_MANIFEST}:
            raise TrainerEnvironmentError("Trainer pack contains missing or unlisted files.")
        for record in profile.files:
            if by_name[record["path"]].file_size != record["size_bytes"]:
                raise TrainerEnvironmentError("Trainer pack sizes do not match the exact software profile.")
        return by_name

    def _consume(self, archive, consumer):
        archive = Path(archive).absolute()
        before = _check_path(archive)
        try:
            with archive.open("rb") as handle:
                if _identity(os.fstat(handle.fileno())) != before:
                    raise TrainerEnvironmentError("Trainer pack changed before reading.")
                _check_zip_directory(handle, before[2])
                with zipfile.ZipFile(handle) as package:
                    result = consumer(package, self._manifest(package))
                if _identity(os.fstat(handle.fileno())) != before or _check_path(archive) != before:
                    raise TrainerEnvironmentError("Trainer pack changed while reading.")
            return result
        except (zipfile.BadZipFile, NotImplementedError, UnicodeError, EOFError, struct.error) as exc:
            raise TrainerEnvironmentError("Trainer pack is unreadable or damaged.") from exc

    def inspect(self, archive, *, cancel=None):
        deadline = time.monotonic() + software_phase_seconds(self.service.profile)
        def consume(package, by_name):
            for record in self.service.profile.files:
                _copy_entry(package, by_name[record["path"]], record, None, cancel, deadline)
            profile = self.service.profile
            return TrainerPackInfo(profile.profile_id, profile.sha256, len(profile.files), profile.byte_count)
        return self._consume(archive, consume)

    def import_pack(self, archive, *, cancel=None, activate=False):
        """Fresh transactional generation and health; a pack cannot confer health."""
        with self.service._locked():
            def populate(root, should_cancel, deadline):
                def consume(package, by_name):
                    for record in self.service.profile.files:
                        path = root / record["path"]
                        _assert_unredirected(path)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        _check_path(path.parent, directory=True)
                        with path.open("xb") as output:
                            _copy_entry(package, by_name[record["path"]], record, output, should_cancel, deadline)
                            output.flush()
                            os.fsync(output.fileno())
                self._consume(archive, consume)
                _control(should_cancel, deadline)
                return deadline
            return self.service._stage(populate, cancel=cancel, activate=activate)

    def export(self, generation_id, destination, *, cancel=None):
        """Create a new pack; verify read-back before exclusive publication."""
        destination = Path(destination).absolute()
        if destination.is_relative_to(self.service.root):
            raise TrainerEnvironmentError("Export trainer packs outside managed environment storage.")
        _assert_unredirected(destination)
        _check_path(destination.parent, directory=True)
        if destination.exists():
            raise TrainerEnvironmentError("Trainer pack export never overwrites an existing destination.")
        with self.service._locked():
            generation = self.service.verify_generation(generation_id, cancel=cancel)
            deadline = time.monotonic() + software_phase_seconds(self.service.profile)
            partial = destination.parent / f".{destination.name}-{uuid.uuid4().hex}.partial"
            with partial.open("xb") as handle:
                with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as package:
                    package.writestr(PACK_MANIFEST, canonical({"schema_version": 1,
                        "kind": "collectigen-trainer-software-pack", "profile": self.service.profile.payload(),
                        "profile_sha256": self.service.profile.sha256}))
                    for record in self.service.profile.files:
                        source = generation.software_root / record["path"]
                        before = _check_path(source)
                        copied, digest = 0, hashlib.sha256()
                        with source.open("rb") as input_file, package.open(record["path"], "w", force_zip64=True) as output:
                            if _identity(os.fstat(input_file.fileno())) != before:
                                raise TrainerEnvironmentError("Trainer software changed before export.")
                            while True:
                                _control(cancel, deadline)
                                block = input_file.read(min(CHUNK_BYTES, record["size_bytes"] - copied + 1))
                                if not block:
                                    break
                                copied += len(block)
                                if copied > record["size_bytes"]:
                                    raise TrainerEnvironmentError("Trainer software grew during export.")
                                output.write(block)
                                digest.update(block)
                            if _identity(os.fstat(input_file.fileno())) != before:
                                raise TrainerEnvironmentError("Trainer software changed during export.")
                        if (_check_path(source) != before or copied != record["size_bytes"]
                                or digest.hexdigest() != record["sha256"]):
                            raise TrainerEnvironmentError("Trainer software failed export verification.")
                handle.flush()
                os.fsync(handle.fileno())
            _control(cancel, deadline)
            self.service.verify_generation(generation_id, cancel=cancel)
            result = self.inspect(partial, cancel=cancel)
            # Verification above owns its own budget; cancellation still wins
            # immediately before exclusive publication.
            _control(cancel, time.monotonic() + software_phase_seconds(self.service.profile))
            _assert_unredirected(destination)
            # Hard-link publication is atomic and fails if the destination was
            # created concurrently. The retained partial is a recovery artifact.
            os.link(partial, destination)
            if self.inspect(destination, cancel=cancel) != result:
                raise TrainerEnvironmentError("Trainer pack publication failed read-back verification.")
            # Only this operation's temporary link is removed after verified
            # publication; failed partials and all generations remain retained.
            partial.unlink()
            return result
