"""Bounded, read-only media classification; temporal records stay separate."""
from __future__ import annotations

import os
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from .dataset_scope import DatasetScope, IMG_EXTENSIONS
from .verified_transfer import VerifiedTransferEngine
from .video_asset import VideoAssetRecord
from .library_storage import LibraryStorageInspector

VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"})
OTHER_IMAGES = frozenset({".gif", ".bmp", ".tif", ".tiff", ".avif", ".heic", ".heif"})
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_IMAGE_PIXELS = 32_000_000
_FORMATS = {"PNG": {".png"}, "JPEG": {".jpg", ".jpeg"}, "WEBP": {".webp"}}


@dataclass(frozen=True)
class MediaEntry:
    path: Path
    kind: str
    status: str
    detail: str
    image_ready: bool = False
    video_ready: bool = False


def inspect_media(path: Path, *, verify_pixels: bool = False, file_attributes=None) -> MediaEntry:
    """Inspect headers for inventory; decode one bounded still for intake.

    A video container signature is identification, never decoder qualification.
    Playback continues through the existing hash-bound temporal player.
    """
    path = Path(path)
    def result(kind, status, detail, **kwargs):
        return MediaEntry(path, kind, status, detail, **kwargs)
    try:
        storage = LibraryStorageInspector()
        if storage.requires_remote_content(path, enumerated_attributes=({path: file_attributes} if file_attributes is not None else None)):
            return result("Unknown", "Unavailable", "Media is online-only. Make it available locally before inspection; no content was downloaded.")
        if VerifiedTransferEngine._redirected(path) or not path.is_file():
            return result("Unknown", "Unavailable", "File is missing or redirected; restore its original location.")
        before = path.stat()
        with path.open("rb") as handle:
            header = handle.read(32)
        if header[4:8] == b"ftyp" and header[8:12] in {b"avif", b"avis", b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
            return result("Image", "Unsupported image", "AVIF/HEIF media is not admitted to still-image workflows. Preserve the original and export a supported still image explicitly.")
        video = (header[4:8] == b"ftyp" or header.startswith(b"\x1aE\xdf\xa3")
                 or (header.startswith(b"RIFF") and header[8:12] == b"AVI "))
        if video:
            if path.suffix.lower() not in VIDEO_EXTENSIONS:
                return result("Video", "Extension mismatch", "Video content has an image or unknown extension. Keep the original; correct the filename outside image intake.")
            if path.suffix.lower() != ".mp4":
                return result("Video", "Unsupported container", "This container is visible here but has no supported video workflow.")
            companion = path.with_name(path.name + ".video.json")
            if not companion.exists():
                return result("Video", "Unregistered", "No temporal companion. For a generated clip, return through Generate → Copy Verified Video to Ingest. External clip registration is not yet supported.")
            if storage.requires_remote_content(companion):
                return result("Video", "Unavailable", "The temporal companion is online-only. Make the complete video family available locally first.")
            record = VideoAssetRecord.read(path)
            if not record.supported:
                return result("Video", "Newer record", "Keep the original companion; this version cannot open its temporal record.")
            if record.asset.byte_length != before.st_size:
                return result("Video", "Changed file", "Video length differs from its temporal record. Reconnect the original verified output.")
            return result("Video", "Registered", "Open timing, lineage and protected playback. Playback rechecks the exact video; this is not training readiness.", video_ready=True)
        if before.st_size > MAX_IMAGE_BYTES:
            return result("Unknown", "Inspection limit", "File exceeds the 64 MiB image inspection limit. No image action is admitted.")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                fmt = image.format
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    return result("Image", "Inspection limit", "Image exceeds the 32 megapixel inspection limit.")
                if getattr(image, "n_frames", 1) != 1:
                    return result("Animation", "Select a still frame", "Animated media cannot enter still-image intake. Export an explicit still frame first; no first-frame conversion is automatic.")
                if fmt not in _FORMATS or path.suffix.lower() not in _FORMATS[fmt]:
                    return result("Image", "Extension mismatch" if fmt in _FORMATS else "Unsupported image", "Image format and supported extension must agree (PNG, JPEG or WebP). Preserve the original when correcting or converting it.")
                if verify_pixels:
                    image.load()
            if verify_pixels:
                with Image.open(path) as image:
                    image.verify()
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            return result("Unknown", "Changed file", "File changed during inspection. Refresh before continuing.")
        return result("Image", "Still image", "Supported still image. Intake additionally verifies decoding; ordinary image review owns knowledge and training readiness.", image_ready=True)
    except (OSError, ValueError, SyntaxError, KeyError, TypeError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        return result("Video" if path.suffix.lower() in VIDEO_EXTENSIONS else "Unknown", "Unreadable", f"Cannot safely inspect media: {exc}")


@dataclass(frozen=True)
class MediaInventory:
    rows: tuple[MediaEntry, ...]
    complete: bool
    issues: tuple[str, ...] = ()


def scan_media(root: Path, *, should_cancel=lambda: False, limit: int = 10000) -> MediaInventory:
    """Scan one explicitly chosen library, excluding recovery and redirect paths.

    Bound both directory entries and retained media rows; no decode/metadata writes.
    """
    if not 1 <= limit <= 10000:
        raise ValueError("Media inspection limit must be between 1 and 10000.")
    if VerifiedTransferEngine._redirected(Path(root)):
        raise ValueError("Choose an original, nonredirected library folder.")
    scope = DatasetScope(root)
    rows, issues = [], []
    visited = 0
    pending = [scope.root]
    ignored = {name.casefold() for name in scope.ignored_directory_names}
    def completed(complete):
        return MediaInventory(tuple(sorted(rows, key=lambda row: str(row.path).casefold())), complete, tuple(issues))
    while pending:
        if should_cancel():
            raise InterruptedError("Media inspection cancelled safely.")
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if should_cancel():
                        raise InterruptedError("Media inspection cancelled safely.")
                    visited += 1
                    if visited > limit * 10 or len(rows) >= limit:
                        issues.append("Inspection limit reached. Choose a narrower subfolder to inspect remaining files.")
                        return completed(False)
                    path = Path(entry.path)
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.casefold() not in ignored and not VerifiedTransferEngine._redirected(path):
                            pending.append(path)
                    elif path.suffix.lower() in IMG_EXTENSIONS | VIDEO_EXTENSIONS | OTHER_IMAGES:
                        rows.append(inspect_media(path, file_attributes=getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)))
                    elif entry.name.lower().endswith(".mp4.video.json"):
                        media = path.with_name(entry.name[:-len(".video.json")])
                        if not media.exists():
                            rows.append(MediaEntry(path, "Video", "Missing video", "Temporal companion retained. Restore its exact video; do not treat it as an image sidecar."))
        except InterruptedError:
            raise
        except OSError as exc:
            if len(issues) < 100:
                issues.append(str(exc))
    return completed(not issues)
