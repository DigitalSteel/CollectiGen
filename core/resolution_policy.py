"""Read-only preferred-resolution policy and decoded-dimension inspection.

The data in this module is rebuildable machine-local evidence.  It is not
Image Knowledge and it never authorizes or performs a pixel mutation.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import math
from pathlib import Path
import sqlite3
import stat as stat_module
import threading
from typing import Callable, Iterable, Sequence

from PIL import Image

from .training_source_io import _assert_unredirected


MAX_DECODED_PIXELS = 40_000_000


class ResolutionState(str, Enum):
    BELOW_PREFERRED = "Below Preferred"
    WITHIN_PREFERRED = "Within Preferred"
    ABOVE_PREFERRED = "Above Preferred"
    DIMENSIONS_UNAVAILABLE = "Dimensions Unavailable"


class ResizeReadiness(str, Enum):
    READY = "Ready"
    ANIMATED_OR_MULTIFRAME = "Animated or Multi-frame"
    UNSUPPORTED_FORMAT = "Unsupported Format"
    DIMENSIONS_UNAVAILABLE = "Dimensions Unavailable"


@dataclass(frozen=True)
class ResizeTarget:
    width: int
    height: int
    scale: float
    target_megapixels: float

    @property
    def megapixels(self) -> float:
        return self.width * self.height / 1_000_000


@dataclass(frozen=True)
class ResolutionPolicy:
    lower_enabled: bool = True
    lower_mp: float = 3.0
    upscale_target_mp: float = 4.0
    upper_enabled: bool = True
    upper_mp: float = 9.0
    downscale_target_mp: float = 6.0

    def __post_init__(self) -> None:
        if type(self.lower_enabled) is not bool or type(self.upper_enabled) is not bool:
            raise ValueError("Resolution direction switches must be explicit booleans")
        lower_values = (self.lower_mp, self.upscale_target_mp)
        upper_values = (self.upper_mp, self.downscale_target_mp)
        if self.lower_enabled and not all(_positive_finite(value) for value in lower_values):
            raise ValueError("Enabled lower cutoff and upscale target must be finite and positive")
        if self.upper_enabled and not all(_positive_finite(value) for value in upper_values):
            raise ValueError("Enabled upper cutoff and downscale target must be finite and positive")
        if self.lower_enabled and self.upscale_target_mp <= self.lower_mp:
            raise ValueError("Upscale target must be above the enabled lower cutoff")
        if self.upper_enabled and self.downscale_target_mp >= self.upper_mp:
            raise ValueError("Downscale target must be below the enabled upper cutoff")
        if self.lower_enabled and self.upper_enabled:
            if self.lower_mp >= self.upper_mp:
                raise ValueError("Enabled lower cutoff must be below the upper cutoff")
            if self.upscale_target_mp > self.upper_mp:
                raise ValueError("Upscale target must not exceed the enabled upper cutoff")
            if self.downscale_target_mp < self.lower_mp:
                raise ValueError("Downscale target must not be below the enabled lower cutoff")

    def classify(self, width: int | None, height: int | None) -> ResolutionState:
        if not _positive_dimension(width) or not _positive_dimension(height):
            return ResolutionState.DIMENSIONS_UNAVAILABLE
        megapixels = width * height / 1_000_000
        if self.lower_enabled and megapixels < self.lower_mp:
            return ResolutionState.BELOW_PREFERRED
        if self.upper_enabled and megapixels > self.upper_mp:
            return ResolutionState.ABOVE_PREFERRED
        return ResolutionState.WITHIN_PREFERRED

    def target_for(self, width: int | None, height: int | None) -> ResizeTarget | None:
        state = self.classify(width, height)
        if state is ResolutionState.BELOW_PREFERRED:
            return calculate_target_dimensions(width, height, self.upscale_target_mp)
        if state is ResolutionState.ABOVE_PREFERRED:
            return calculate_target_dimensions(width, height, self.downscale_target_mp)
        return None


def _positive_finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _positive_dimension(value: object) -> bool:
    return type(value) is int and value > 0


def calculate_target_dimensions(width: int | None, height: int | None, target_mp: float) -> ResizeTarget:
    if not _positive_dimension(width) or not _positive_dimension(height):
        raise ValueError("Source dimensions must be positive integers")
    if not _positive_finite(target_mp):
        raise ValueError("Target megapixels must be finite and positive")
    scale = math.sqrt(target_mp * 1_000_000 / (width * height))
    return ResizeTarget(
        width=max(1, round(width * scale)),
        height=max(1, round(height * scale)),
        scale=scale,
        target_megapixels=float(target_mp),
    )


@dataclass(frozen=True)
class FileIdentity:
    path: str
    size: int
    mtime_ns: int
    ctime_ns: int
    device: int
    inode: int

    @classmethod
    def read(cls, path: Path) -> "FileIdentity":
        source = path.absolute()
        _assert_unredirected(source)
        stat = source.stat(follow_symlinks=False)
        if not stat_module.S_ISREG(stat.st_mode):
            raise RuntimeError(f"{source.name} is not a regular source image file")
        return cls(
            path=str(source),
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            ctime_ns=stat.st_ctime_ns,
            device=stat.st_dev,
            inode=stat.st_ino,
        )


@dataclass(frozen=True)
class _DimensionEvidence:
    width: int | None
    height: int | None
    format: str | None
    mode: str | None
    frames: int | None
    exif_orientation: int | None
    has_icc_profile: bool
    readiness: ResizeReadiness
    readiness_reason: str | None
    error: str | None


@dataclass(frozen=True)
class ResolutionRecord:
    path: Path
    identity: FileIdentity | None
    width: int | None
    height: int | None
    megapixels: float | None
    state: ResolutionState
    target: ResizeTarget | None
    format: str | None
    mode: str | None
    frames: int | None
    exif_orientation: int | None
    has_icc_profile: bool
    resize_readiness: ResizeReadiness
    resize_readiness_reason: str | None
    error: str | None
    cache_hit: bool = False

    @property
    def dimensions_available(self) -> bool:
        return self.width is not None and self.height is not None

    @property
    def resize_ready(self) -> bool:
        return self.resize_readiness is ResizeReadiness.READY

    def with_policy(self, policy: ResolutionPolicy) -> "ResolutionRecord":
        return replace(
            self,
            state=policy.classify(self.width, self.height),
            target=policy.target_for(self.width, self.height),
        )


class DimensionCache:
    """Best-effort SQLite cache; database errors degrade to cache misses."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else None
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        try:
            if self.db_path is not None:
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(
                str(self.db_path) if self.db_path is not None else ":memory:",
                check_same_thread=False,
            )
            self._connection.execute(
                """CREATE TABLE IF NOT EXISTS dimensions (
                    path TEXT PRIMARY KEY, size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL, ctime_ns INTEGER NOT NULL,
                    device TEXT NOT NULL, inode TEXT NOT NULL,
                    width INTEGER, height INTEGER, format TEXT, mode TEXT,
                    frames INTEGER, exif_orientation INTEGER,
                    has_icc_profile INTEGER NOT NULL, readiness TEXT NOT NULL,
                    readiness_reason TEXT, error TEXT
                )"""
            )
            self._connection.commit()
        except (OSError, sqlite3.Error):
            self.close()

    @property
    def available(self) -> bool:
        return self._connection is not None

    def get(self, identity: FileIdentity) -> _DimensionEvidence | None:
        with self._lock:
            if self._connection is None:
                return None
            try:
                row = self._connection.execute(
                    """SELECT width,height,format,mode,frames,exif_orientation,
                              has_icc_profile,readiness,readiness_reason,error
                       FROM dimensions WHERE path=? AND size=? AND mtime_ns=?
                       AND ctime_ns=? AND device=? AND inode=?""",
                    (identity.path, identity.size, identity.mtime_ns, identity.ctime_ns,
                     str(identity.device), str(identity.inode)),
                ).fetchone()
            except sqlite3.Error:
                self.close()
                return None
        if row is None:
            return None
        try:
            evidence = _DimensionEvidence(
                width=row[0], height=row[1], format=row[2], mode=row[3], frames=row[4],
                exif_orientation=row[5], has_icc_profile=row[6] == 1,
                readiness=ResizeReadiness(row[7]), readiness_reason=row[8], error=row[9],
            )
        except (TypeError, ValueError):
            return None
        return evidence if _valid_cached_evidence(evidence, row[6]) else None

    def put(self, identity: FileIdentity, evidence: _DimensionEvidence) -> None:
        with self._lock:
            if self._connection is None:
                return
            try:
                self._connection.execute(
                    """INSERT OR REPLACE INTO dimensions VALUES
                       (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (identity.path, identity.size, identity.mtime_ns, identity.ctime_ns,
                     str(identity.device), str(identity.inode), evidence.width, evidence.height,
                     evidence.format, evidence.mode, evidence.frames,
                     evidence.exif_orientation, int(evidence.has_icc_profile),
                     evidence.readiness.value, evidence.readiness_reason, evidence.error),
                )
                self._connection.commit()
            except sqlite3.Error:
                self.close()

    def close(self) -> None:
        with self._lock:
            connection, self._connection = self._connection, None
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass

    def __enter__(self) -> "DimensionCache":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class ResolutionInspector:
    SUPPORTED_RESIZE_FORMATS = frozenset({"BMP", "JPEG", "PNG", "TIFF", "WEBP"})

    def __init__(self, policy: ResolutionPolicy | None = None, cache: DimensionCache | None = None) -> None:
        self.policy = policy or ResolutionPolicy()
        self.cache = cache

    def inspect_one(self, path: str | Path) -> ResolutionRecord:
        source = Path(path)
        try:
            identity = FileIdentity.read(source)
        except (OSError, RuntimeError) as exc:
            return self._record(source, None, _unavailable_evidence(exc))

        cached = self.cache.get(identity) if self.cache is not None else None
        if cached is not None:
            return self._record(source, identity, cached, cache_hit=True)

        # One retry gives a concurrently replaced source a bounded chance to settle.
        for attempt in range(2):
            evidence = self._decode(source)
            try:
                after = FileIdentity.read(source)
            except (OSError, RuntimeError) as exc:
                return self._record(source, None, _unavailable_evidence(exc))
            if identity == after:
                if self.cache is not None:
                    self.cache.put(identity, evidence)
                return self._record(source, identity, evidence)
            identity = after
            if attempt == 0:
                cached = self.cache.get(identity) if self.cache is not None else None
                if cached is not None:
                    return self._record(source, identity, cached, cache_hit=True)

        evidence = _DimensionEvidence(
            width=None, height=None, format=None, mode=None, frames=None,
            exif_orientation=None, has_icc_profile=False,
            readiness=ResizeReadiness.DIMENSIONS_UNAVAILABLE,
            readiness_reason="Source changed during inspection",
            error="Source changed during inspection",
        )
        return self._record(source, identity, evidence)

    def inspect(
        self,
        paths: Iterable[str | Path],
        cancelled: Callable[[], bool] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> list[ResolutionRecord]:
        sources = tuple(Path(path) for path in paths)
        records: list[ResolutionRecord] = []
        total = len(sources)
        for source in sources:
            if cancelled is not None and cancelled():
                break
            records.append(self.inspect_one(source))
            if progress is not None:
                progress(len(records), total, source.name)
            if cancelled is not None and cancelled():
                break
        return records

    def _decode(self, path: Path) -> _DimensionEvidence:
        try:
            with Image.open(path) as image:
                if image.width * image.height > MAX_DECODED_PIXELS:
                    raise ValueError("The decoded image exceeds the 40 megapixel inspection limit")
                image.load()
                width, height = image.size
                format_name = image.format.upper() if image.format else None
                mode = image.mode
                frames = int(getattr(image, "n_frames", 1))
                orientation = image.getexif().get(274)
                if orientation in {5, 6, 7, 8}:
                    width, height = height, width
                has_icc = bool(image.info.get("icc_profile"))
        except (OSError, ValueError, SyntaxError) as exc:
            return _unavailable_evidence(exc)

        if frames > 1:
            readiness = ResizeReadiness.ANIMATED_OR_MULTIFRAME
            reason = f"{frames} frames require an explicit animation-aware resize path"
        elif format_name not in self.SUPPORTED_RESIZE_FORMATS:
            readiness = ResizeReadiness.UNSUPPORTED_FORMAT
            reason = f"{format_name or 'Unknown'} is not enabled for resizing"
        else:
            readiness = ResizeReadiness.READY
            reason = None
        return _DimensionEvidence(
            width=width, height=height, format=format_name, mode=mode, frames=frames,
            exif_orientation=orientation, has_icc_profile=has_icc,
            readiness=readiness, readiness_reason=reason, error=None,
        )

    def _record(
        self,
        path: Path,
        identity: FileIdentity | None,
        evidence: _DimensionEvidence,
        cache_hit: bool = False,
    ) -> ResolutionRecord:
        state = self.policy.classify(evidence.width, evidence.height)
        return ResolutionRecord(
            path=path, identity=identity, width=evidence.width, height=evidence.height,
            megapixels=(evidence.width * evidence.height / 1_000_000
                        if evidence.width is not None and evidence.height is not None else None),
            state=state, target=self.policy.target_for(evidence.width, evidence.height),
            format=evidence.format, mode=evidence.mode, frames=evidence.frames,
            exif_orientation=evidence.exif_orientation,
            has_icc_profile=evidence.has_icc_profile,
            resize_readiness=evidence.readiness,
            resize_readiness_reason=evidence.readiness_reason,
            error=evidence.error, cache_hit=cache_hit,
        )


def _unavailable_evidence(exc: BaseException) -> _DimensionEvidence:
    return _DimensionEvidence(
        width=None, height=None, format=None, mode=None, frames=None,
        exif_orientation=None, has_icc_profile=False,
        readiness=ResizeReadiness.DIMENSIONS_UNAVAILABLE,
        readiness_reason="The source could not be decoded",
        error=f"{type(exc).__name__}: {exc}",
    )


def _valid_cached_evidence(evidence: _DimensionEvidence, raw_icc: object) -> bool:
    if raw_icc not in (0, 1):
        return False
    if evidence.exif_orientation is not None and (
        type(evidence.exif_orientation) is not int or evidence.exif_orientation not in range(1, 9)
    ):
        return False
    if evidence.readiness_reason is not None and not isinstance(evidence.readiness_reason, str):
        return False
    if evidence.error is not None and not isinstance(evidence.error, str):
        return False
    if evidence.width is None or evidence.height is None:
        return (
            evidence.width is None
            and evidence.height is None
            and evidence.readiness is ResizeReadiness.DIMENSIONS_UNAVAILABLE
            and bool(evidence.error)
        )
    return (
        _positive_dimension(evidence.width)
        and _positive_dimension(evidence.height)
        and evidence.width * evidence.height <= MAX_DECODED_PIXELS
        and type(evidence.frames) is int
        and evidence.frames > 0
        and (evidence.format is None or isinstance(evidence.format, str))
        and (evidence.mode is None or isinstance(evidence.mode, str))
        and evidence.readiness is not ResizeReadiness.DIMENSIONS_UNAVAILABLE
        and evidence.error is None
    )


def filter_records(
    records: Iterable[ResolutionRecord],
    policy: ResolutionPolicy,
    states: Sequence[ResolutionState],
) -> list[ResolutionRecord]:
    """Reclassify cached evidence and return records in the requested states."""

    wanted = frozenset(states)
    return [updated for record in records if (updated := record.with_policy(policy)).state in wanted]
