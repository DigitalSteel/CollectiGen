"""Bounded local MP4 decode evidence and one-frame-at-a-time preview reads.

PyAV is loaded only when a caller invokes these functions. Passing ``av_module``
keeps contract checks testable without acquiring a decoder at import time.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import closing
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
from queue import Empty, Full, Queue
import stat
import struct
import subprocess
import sys
from threading import Event, Lock, Thread
from time import monotonic
from typing import Callable, Iterator

from .training_source_io import _assert_unredirected, _identity
from .video_generation import (
    MAX_FRAMES, MAX_PIXELS, Rational, TemporalTiming, VideoGenerationRequest,
    VideoSourceAsset, duration_from_frame_pts,
)


MAX_VIDEO_BYTES = 1024 * 1024 * 1024
MAX_FRAME_BYTES = 128 * 1024 * 1024
MAX_DECODE_SECONDS = 120.0
MAX_PROTOCOL_HEADER = 16 * 1024 * 1024


class VideoDecodeError(ValueError):
    """A local clip cannot be admitted as the exact reviewed video output."""


class VideoDecoderLifetime:
    """One host's decoder uses; mutation blocks starts until actual owners drain.

    The caller retains this object with its AppState. It selects no runtime and
    never cancels, joins, mutates a component, or creates a process itself.
    """

    def __init__(self):
        self._lock = Lock()
        self._blocked = False
        self._uses = set()

    def acquire(self):
        with self._lock:
            self._drained()
            if self._blocked:
                raise VideoDecodeError("Video components are changing. Retry after Setup finishes.")
            use = _DecoderUse(self)
            self._uses.add(use)
            return use

    def _drained(self):
        self._uses.difference_update(use for use in tuple(self._uses) if use.drained())
        return not self._uses

    @property
    def active(self):
        with self._lock:
            return not self._drained()

    @property
    def drained(self):
        return not self.active

    def begin_mutation(self):
        """Atomically refuse new uses; true only after every prior owner drains."""
        with self._lock:
            self._blocked = True
            return self._drained()

    def end_mutation(self):
        """Reopen starts after the host finishes or aborts component mutation."""
        with self._lock:
            self._blocked = False


class _DecoderUse:
    def __init__(self, lifetime):
        self.lifetime = lifetime
        self.child = None
        self.threads = ()
        self.finished = False

    def owners(self, *, child=None, threads=()):
        with self.lifetime._lock:
            self.child, self.threads = child, tuple(threads)

    def finish(self):
        with self.lifetime._lock:
            self.finished = True

    def drained(self):
        # A completed generator/cleanup attempt is not proof that its process or
        # pipe readers exited. Keep the host mutation hold until actual teardown.
        try:
            return (self.finished and (self.child is None or self.child.poll() is not None)
                    and all(not thread.is_alive() for thread in self.threads))
        except Exception:
            return False


@dataclass(frozen=True)
class VideoVerification:
    asset: VideoSourceAsset
    decoder_evidence: dict[str, object]


@dataclass(frozen=True)
class VideoPreviewFrame:
    pts: int
    width: int
    height: int
    rgb: bytes


def _decoder(module):
    if module is not None:
        return module
    try:
        import av  # type: ignore[import-not-found]
    except ImportError as exc:
        raise VideoDecodeError("Local video decoding is unavailable. Install the reviewed PyAV dependency in Setup.") from exc
    return av


def _read_exact(stream, count: int) -> bytes:
    parts = bytearray()
    while len(parts) < count:
        part = stream.read(count - len(parts))
        if not part:
            raise EOFError("The owned decoder ended before its complete result.")
        parts.extend(part)
    return bytes(parts)


def _runtime_payload(binding, *, check=None):
    # The binding's owner has already imported its service. Do not import that
    # service here: its legacy import activates a process-wide optional site.
    owner = sys.modules.get("core.dependencies")
    if owner is None or type(binding) is not getattr(owner, "VideoRuntimeBinding", None):
        raise VideoDecodeError("An admitted immutable Video runtime binding is required.")
    if check is not None:
        check()
    payload = {key: str(value) if isinstance(value, Path) else value
               for key, value in asdict(binding).items()}
    from .video_decoder_worker import validate_runtime_payload
    validate_runtime_payload(payload, check=check)
    service = owner.DependencyService
    if check is not None:
        check()
    profile = service._optional_profile_contract(validate_runtime=False).get("profiles", {}).get(binding.profile_id)
    if (not isinstance(profile, dict) or profile.get("support") != "supported"
            or service._profile_sha256(profile) != binding.reviewed_profile_sha256):
        raise VideoDecodeError("The reviewed Video profile changed or is not qualified.")
    packages = profile.get("packages", [])
    if len(packages) != 1 or packages[0].get("name") != "av" or packages[0].get("version") != binding.version:
        raise VideoDecodeError("The reviewed Video package closure changed.")
    distributions = service._distribution_map(binding.component_site).get("av", [])
    if len(distributions) != 1 or str(distributions[0].version) != binding.version:
        raise VideoDecodeError("Video installed distribution identity changed.")
    # Reuse the admission owner's complete shared-environment read-back, including
    # retained AI profiles. A valid av RECORD alone cannot re-admit this store.
    def dependency_check():
        # Reuse the admission owner's control-flow wrapper so RECORD helpers
        # cannot turn interruption (an OSError subclass) into a damaged-file issue.
        try:
            check()
        except Exception as exc:
            raise owner._DependencyVerificationInterrupted(exc) from exc

    try:
        issues = service._optional_environment_issues(binding.destination_root,
            check_interrupt=dependency_check if check is not None else None)
    except owner._DependencyVerificationInterrupted as exc:
        raise exc.error
    if issues:
        raise VideoDecodeError("Video component read-back failed: " + "; ".join(issues))
    # Record checks can be long; do not launch after a concurrent pin change.
    validate_runtime_payload(payload, check=check)
    return payload


def _launch_worker(runtime_binding=None, *, check=None):
    script = Path(__file__).with_name("video_decoder_worker.py")
    arguments = [sys.executable, "-I", "-u", str(script)]
    if runtime_binding is not None:
        payload = _runtime_payload(runtime_binding, check=check)
        if check is not None:
            check()
        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        if len(encoded) > 16384:
            raise VideoDecodeError("Video runtime binding exceeds its bound.")
        arguments[0] = payload["interpreter_path"]
        arguments.insert(2, "-B")  # Do not mutate the admitted software site.
        arguments += ["--runtime-binding", encoded]
    return subprocess.Popen(arguments,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _worker_messages(spec: dict, *, should_cancel, max_seconds: float,
                     max_frame_bytes: int, runtime_binding=None, decoder_lifetime=None):
    """Read an owned child with a hard parent-side cancellation/deadline."""
    started = monotonic()  # Preflight consumes the same operation budget.
    use = decoder_lifetime.acquire() if decoder_lifetime is not None else None

    def preflight_check():
        if should_cancel is not None and should_cancel():
            raise InterruptedError("Video decoding cancelled safely.")
        if monotonic() - started > max_seconds:
            # Dependency read-back preserves these control-flow exceptions.
            raise TimeoutError("Video decoding exceeded its time limit.")

    try:
        preflight_check()
        child = (_launch_worker() if runtime_binding is None else
                 _launch_worker(runtime_binding, check=preflight_check))
        if use is not None:
            use.owners(child=child)
    except BaseException as exc:
        if use is not None:
            use.finish()
        if isinstance(exc, TimeoutError):
            raise VideoDecodeError("Video decoding exceeded its time limit.") from exc
        if isinstance(exc, OSError) and not isinstance(exc, InterruptedError):
            raise VideoDecodeError("The local video decoder could not start.") from exc
        raise
    stopped = Event()
    messages: Queue = Queue(maxsize=1)
    reader = writer = None
    try:
        assert child.stdin is not None and child.stdout is not None
        payload = json.dumps(spec, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(payload) > MAX_PROTOCOL_HEADER:
            raise VideoDecodeError("Video decoder request exceeds its bound.")

        def write_request():
            try:
                child.stdin.write(payload)
                child.stdin.close()
            except (OSError, ValueError) as exc:
                deliver({"kind": "writer_error", "message": str(exc)[:2000]})

        def deliver(value):
            while not stopped.is_set():
                try:
                    messages.put(value, timeout=.05)
                    return
                except Full:
                    continue

        def read_records():
            try:
                while not stopped.is_set():
                    size = struct.unpack("!I", _read_exact(child.stdout, 4))[0]
                    if not 0 < size <= MAX_PROTOCOL_HEADER:
                        raise VideoDecodeError("Decoder response exceeds its metadata bound.")
                    record = json.loads(_read_exact(child.stdout, size))
                    if not isinstance(record, dict):
                        raise VideoDecodeError("Decoder returned an invalid record.")
                    if record.get("kind") == "frame":
                        length = record.get("rgb_length")
                        if not isinstance(length, int) or not 0 < length <= max_frame_bytes:
                            raise VideoDecodeError("Decoder frame exceeds its memory bound.")
                        record["rgb"] = _read_exact(child.stdout, length)
                    deliver(record)
                    if record.get("kind") in {"end", "error"}:
                        return
            except BaseException as exc:
                deliver({"kind": "reader_error", "message": str(exc)[:2000]})

        writer = Thread(target=write_request, name="CollectiGen video request", daemon=True)
        reader = Thread(target=read_records, name="CollectiGen video IPC", daemon=True)
        if use is not None:
            use.owners(child=child, threads=(reader, writer))
        writer.start()
        reader.start()
        while True:
            if should_cancel is not None and should_cancel():
                raise InterruptedError("Video decoding cancelled safely.")
            if monotonic() - started > max_seconds:
                raise VideoDecodeError("Video decoding exceeded its time limit.")
            try:
                record = messages.get(timeout=.05)
            except Empty:
                continue
            kind = record.get("kind")
            if kind == "error":
                raise VideoDecodeError("Video decoding failed: " + str(record.get("message", "")))
            if kind == "reader_error":
                raise VideoDecodeError("Video decoder stopped: " + str(record.get("message", "")))
            if kind == "writer_error":
                raise VideoDecodeError("Video decoder request failed: " + str(record.get("message", "")))
            if kind == "end":
                return
            yielded_at = monotonic()
            yield record
            # Playback can remain paused indefinitely without spending decode time.
            started += monotonic() - yielded_at
    finally:
        try:
            stopped.set()
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=1)
            if child.stdout is not None:
                child.stdout.close()
            if reader is not None and reader.ident is not None:
                reader.join(timeout=.5)
            if writer is not None and writer.ident is not None:
                writer.join(timeout=.5)
        finally:
            if use is not None:
                use.finish()


def _check(cancel: Callable[[], bool] | None, started: float, limit: float) -> None:
    if cancel is not None and cancel():
        raise InterruptedError("Video decoding cancelled safely.")
    if monotonic() - started > limit:
        raise VideoDecodeError("Video decoding exceeded its time limit.")


def _fraction(value, label: str) -> Fraction:
    try:
        result = Fraction(value)
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise VideoDecodeError(f"The video has no valid {label}.") from exc
    if result <= 0:
        raise VideoDecodeError(f"The video has no valid {label}.")
    return result


def _rational(value: Fraction) -> Rational:
    return Rational(value.numerator, value.denominator)


def _contained_regular(path: str | Path, root: str | Path, max_bytes: int):
    source = Path(path).absolute()
    boundary = Path(root).absolute()
    _assert_unredirected(source)
    _assert_unredirected(boundary)
    try:
        source.resolve(strict=True).relative_to(boundary.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise VideoDecodeError("Video output is outside its exact retained job directory.") from exc
    before = source.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > max_bytes:
        raise VideoDecodeError("Video output is not a bounded regular file.")
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0)) | int(getattr(os, "O_NOFOLLOW", 0))
    descriptor = os.open(source, flags)
    try:
        opened = os.fstat(descriptor)
        _assert_unredirected(source)
        if _identity(opened) == _identity(source.stat(follow_symlinks=False)):
            return source, descriptor, opened
    except BaseException:
        os.close(descriptor)
        raise
    os.close(descriptor)
    raise VideoDecodeError("Video output changed while it was opened.")


def _hash_descriptor(descriptor: int, cancel, started: float, limit: float) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        _check(cancel, started, limit)
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _unchanged(source: Path, descriptor: int, opened, digest: str,
               cancel, started: float, limit: float) -> None:
    _assert_unredirected(source)
    if (_identity(opened) != _identity(os.fstat(descriptor))
            or _identity(opened) != _identity(source.stat(follow_symlinks=False))
            or _hash_descriptor(descriptor, cancel, started, limit) != digest):
        raise VideoDecodeError("Video output changed during decoding.")


def _container(module, descriptor: int):
    os.lseek(descriptor, 0, os.SEEK_SET)
    borrowed = os.fdopen(os.dup(descriptor), "rb")
    try:
        container = module.open(borrowed, mode="r", format="mp4",
            options={"protocol_whitelist": "file"})
    except Exception:
        borrowed.close()
        raise
    return container, borrowed


def _video_stream(container):
    format_name = str(getattr(getattr(container, "format", None), "name", ""))
    if "mp4" not in format_name.split(","):
        raise VideoDecodeError("Only a local MP4 video container is admitted.")
    streams = tuple(container.streams)
    if len(streams) != 1 or getattr(streams[0], "type", None) != "video":
        raise VideoDecodeError("The silent video output must contain exactly one video stream and no audio or other streams.")
    return streams[0], format_name


def _frame_pts(frame, time_base: Fraction) -> int:
    if getattr(frame, "pts", None) is None:
        raise VideoDecodeError("A decoded frame has no presentation timestamp.")
    frame_base = _fraction(getattr(frame, "time_base", None), "frame time base")
    converted = Fraction(int(frame.pts)) * frame_base / time_base
    if converted.denominator != 1:
        raise VideoDecodeError("A frame timestamp cannot be represented in the video stream time base.")
    return int(converted)


def _frame_duration_ticks(frame, stream_base: Fraction) -> int | None:
    duration = getattr(frame, "duration", None)
    if duration is None or int(duration) <= 0:
        return None
    frame_base = _fraction(getattr(frame, "time_base", None), "frame time base")
    converted = Fraction(int(duration)) * frame_base / stream_base
    if converted.denominator != 1:
        raise VideoDecodeError("A frame duration cannot be represented in the video stream time base.")
    return int(converted)


def _frame_geometry(frame, expected_width: int, expected_height: int,
                    max_frame_bytes: int) -> None:
    width, height = int(frame.width), int(frame.height)
    if (width != expected_width or height != expected_height or width <= 0 or height <= 0
            or width * height > MAX_PIXELS or width * height * 4 > max_frame_bytes):
        raise VideoDecodeError("Decoded video frame geometry differs from the reviewed request or memory bound.")


def verify_video_output(
    path: str | Path, expected_request: VideoGenerationRequest, *,
    allowed_root: str | Path, av_module=None,
    should_cancel: Callable[[], bool] | None = None,
    privacy_disposition: str = "local_only",
    max_bytes: int = MAX_VIDEO_BYTES,
    max_frame_bytes: int = MAX_FRAME_BYTES,
    max_seconds: float = MAX_DECODE_SECONDS,
    runtime_binding=None, decoder_lifetime: VideoDecoderLifetime | None = None,
) -> VideoVerification:
    """Full-decode one contained silent result and preserve observed timeline facts.

    This validates media, not job/attempt ownership; the generation receipt must
    independently bind the output to its exact submission and model resources.
    """
    if not isinstance(expected_request, VideoGenerationRequest) or not expected_request.schema_supported:
        raise VideoDecodeError("An exact supported video request is required.")
    if expected_request.audio_policy != "silent":
        raise VideoDecodeError("This video decoder admits silent output only.")
    if runtime_binding is not None and av_module is not None:
        raise VideoDecodeError("Admitted Video runtime requires the isolated decoder worker.")
    if av_module is None:
        spec = {"operation": "verify", "path": str(path), "allowed_root": str(allowed_root),
            "expected_request": expected_request.to_dict(),
            "privacy_disposition": privacy_disposition,
            "max_bytes": max_bytes, "max_frame_bytes": max_frame_bytes,
            "max_seconds": max_seconds}
        result = None
        with closing(_worker_messages(spec, should_cancel=should_cancel,
                max_seconds=max_seconds, max_frame_bytes=max_frame_bytes,
                runtime_binding=runtime_binding, decoder_lifetime=decoder_lifetime)) as records:
            for record in records:
                if record.get("kind") != "result" or result is not None:
                    raise VideoDecodeError("Decoder returned an unexpected verification record.")
                result = VideoVerification(VideoSourceAsset.from_dict(record["asset"]),
                    record["decoder_evidence"])
        if result is None:
            raise VideoDecodeError("Decoder returned no verified video result.")
        return result
    started = monotonic()
    module = _decoder(av_module)
    source, descriptor, opened = _contained_regular(path, allowed_root, max_bytes)
    try:
        first_hash = _hash_descriptor(descriptor, should_cancel, started, max_seconds)
        container, borrowed = _container(module, descriptor)
        try:
            stream, format_name = _video_stream(container)
            base = _fraction(getattr(stream, "time_base", None), "stream time base")
            rate = _fraction(getattr(stream, "average_rate", None), "observed average frame rate")
            nominal = _fraction(getattr(stream, "base_rate", None) or rate, "nominal frame rate")
            codec = str(getattr(getattr(stream, "codec_context", None), "name", ""))
            if codec != "h264":
                raise VideoDecodeError("The first video recipe requires one H.264 stream.")
            pts_values = []
            last_duration = None
            for frame in container.decode(video=0):
                _check(should_cancel, started, max_seconds)
                _frame_geometry(frame, expected_request.width, expected_request.height, max_frame_bytes)
                pts_values.append(_frame_pts(frame, base))
                if len(pts_values) > min(MAX_FRAMES, expected_request.frame_count):
                    raise VideoDecodeError("Video output has more frames than the reviewed request.")
                last_duration = _frame_duration_ticks(frame, base)
            _check(should_cancel, started, max_seconds)
            if len(pts_values) != expected_request.frame_count:
                raise VideoDecodeError("Decoded frame count differs from the reviewed request.")
            start_pts = getattr(stream, "start_time", None)
            start_pts = int(start_pts) if start_pts is not None else pts_values[0]
            stream_duration = getattr(stream, "duration", None)
            stream_duration = int(stream_duration) if stream_duration is not None and int(stream_duration) > 0 else None
            if stream_duration is None and last_duration is not None:
                stream_duration = pts_values[-1] + last_duration - start_pts
            timing = TemporalTiming(_rational(base), start_pts, len(pts_values),
                _rational(nominal), _rational(rate), stream_duration, tuple(pts_values))
            duration = duration_from_frame_pts(pts_values, timing,
                final_duration_ticks=last_duration)
            if rate != expected_request.fps.fraction:
                raise VideoDecodeError("Observed video frame rate differs from the reviewed request.")
            if abs(duration - expected_request.requested_duration) > base:
                raise VideoDecodeError("Decoded video duration differs from the reviewed request.")
            stream_id = str(getattr(stream, "id", getattr(stream, "index", 0)))
            raw_metadata = {
                "container": dict(getattr(container, "metadata", {}) or {}),
                "stream": dict(getattr(stream, "metadata", {}) or {}),
            }
            decoder_evidence = {
                "decoder": "PyAV", "decoder_version": str(getattr(module, "__version__", "unknown")),
                "container": format_name, "codec": codec, "width": expected_request.width,
                "height": expected_request.height, "frames_fully_decoded": len(pts_values),
                "full_decode": True,
            }
            asset = VideoSourceAsset(
                f"video:sha256:{first_hash}:{stream_id}", first_hash, opened.st_size,
                stream_id, timing, raw_metadata, decoder_evidence, privacy_disposition,
            )
        finally:
            container.close()
            borrowed.close()
        _unchanged(source, descriptor, opened, first_hash, should_cancel, started, max_seconds)
        return VideoVerification(asset, decoder_evidence)
    except InterruptedError:
        raise
    except VideoDecodeError:
        raise
    except Exception as exc:
        raise VideoDecodeError(f"The complete video could not be decoded: {exc}") from exc
    finally:
        os.close(descriptor)


def iter_video_preview_frames(
    path: str | Path, asset: VideoSourceAsset, *, allowed_root: str | Path,
    start_pts: int | None = None, av_module=None,
    should_cancel: Callable[[], bool] | None = None,
    max_bytes: int = MAX_VIDEO_BYTES,
    max_frame_bytes: int = MAX_FRAME_BYTES,
    max_seconds: float = MAX_DECODE_SECONDS,
    runtime_binding=None, decoder_lifetime: VideoDecoderLifetime | None = None,
) -> Iterator[VideoPreviewFrame]:
    """Yield at most one RGB frame at a time; seek by exact observed PTS."""
    if not isinstance(asset, VideoSourceAsset):
        raise VideoDecodeError("A verified video asset is required for playback.")
    if runtime_binding is not None and av_module is not None:
        raise VideoDecodeError("Admitted Video runtime requires the isolated decoder worker.")
    if av_module is None:
        spec = {"operation": "preview", "path": str(path), "allowed_root": str(allowed_root),
            "asset": asset.to_dict(), "start_pts": start_pts,
            "max_bytes": max_bytes, "max_frame_bytes": max_frame_bytes,
            "max_seconds": max_seconds}
        with closing(_worker_messages(spec, should_cancel=should_cancel,
                max_seconds=max_seconds, max_frame_bytes=max_frame_bytes,
                runtime_binding=runtime_binding, decoder_lifetime=decoder_lifetime)) as records:
            for record in records:
                if record.get("kind") != "frame":
                    raise VideoDecodeError("Decoder returned an unexpected playback record.")
                yield VideoPreviewFrame(record["pts"], record["width"],
                    record["height"], record["rgb"])
        return
    started = monotonic()
    module = _decoder(av_module)
    source, descriptor, opened = _contained_regular(path, allowed_root, max_bytes)
    try:
        digest = _hash_descriptor(descriptor, should_cancel, started, max_seconds)
        if digest != asset.sha256 or opened.st_size != asset.byte_length:
            raise VideoDecodeError("Video bytes changed after verification.")
        container, borrowed = _container(module, descriptor)
        try:
            stream, _format_name = _video_stream(container)
            base = _fraction(getattr(stream, "time_base", None), "stream time base")
            if base != asset.timing.time_base.fraction or str(getattr(stream, "id", getattr(stream, "index", 0))) != asset.stream_id:
                raise VideoDecodeError("Playback stream differs from verified video evidence.")
            requested = asset.timing.start_pts if start_pts is None else int(start_pts)
            if requested < asset.timing.start_pts or (asset.timing.end_pts is not None and requested >= asset.timing.end_pts):
                raise VideoDecodeError("Requested seek is outside the verified video timeline.")
            index = 0
            for frame in container.decode(video=0):
                _check(should_cancel, started, max_seconds)
                if index >= asset.timing.frame_count:
                    raise VideoDecodeError("Playback has more frames than verified evidence.")
                pts = _frame_pts(frame, base)
                if pts != asset.timing.frame_pts[index]:
                    raise VideoDecodeError("Playback frame timing changed after verification.")
                index += 1
                _frame_geometry(frame, int(asset.probe_provenance["width"]),
                    int(asset.probe_provenance["height"]), max_frame_bytes)
                if pts < requested:
                    continue
                picture = frame.to_image().convert("RGB")
                rgb = picture.tobytes()
                if len(rgb) != frame.width * frame.height * 3:
                    raise VideoDecodeError("Decoded RGB frame has an unexpected byte length.")
                yielded_at = monotonic()
                yield VideoPreviewFrame(pts, int(frame.width), int(frame.height), rgb)
                # A paused player's bounded queue suspends this generator. Only
                # active decode time belongs to the deadline, not user pause.
                started += monotonic() - yielded_at
            if index != asset.timing.frame_count:
                raise VideoDecodeError("Playback ended before the verified final frame.")
        finally:
            container.close()
            borrowed.close()
        _unchanged(source, descriptor, opened, digest, should_cancel, started, max_seconds)
    except InterruptedError:
        raise
    except VideoDecodeError:
        raise
    except Exception as exc:
        raise VideoDecodeError(f"Video playback stopped: {exc}") from exc
    finally:
        os.close(descriptor)
