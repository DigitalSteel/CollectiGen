"""Explicit local artwork for exact model records; never a model or image catalog.

Only a user-chosen still image is read. Browsing projects metadata; resolving
artwork reads an owned, hash-bound normalized thumbnail, never its source.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
from io import BytesIO
import os
from pathlib import Path
import re
import stat
from uuid import uuid4
from urllib.parse import urlparse
import warnings

from PIL import Image, ImageOps

from .model_resource_registry import (
    ModelResourceError, _bounded_read, _bytes, _cancel, _json,
    atomic_verified_bytes,
)
from .model_workspace import _path, _pinned_model_file, _pinned_parent

MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_PIXELS = 16_000_000
MAX_SIDE = 16_384
THUMBNAIL_SIDE = 512
MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024
MAX_RECEIPT_BYTES = 256 * 1024
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_OP = re.compile(r"[0-9a-f]{32}\Z")
_FORMATS = {"PNG": {".png"}, "JPEG": {".jpg", ".jpeg"}, "WEBP": {".webp"}}


class ArtworkRecoveryRequired(ModelResourceError):
    def __init__(self, operation_id, message):
        self.operation_id = operation_id
        super().__init__(f"{message} Reopen artwork recovery {operation_id}.")


@dataclass(frozen=True)
class ArtworkReview:
    resource_id: str
    registry_token: str
    source_path: Path | None
    source_sha256: str
    source_size: int
    width: int
    height: int
    format: str
    thumbnail_bytes: bytes
    descriptor: dict
    before: dict | None
    review_id: str


@dataclass(frozen=True)
class ArtworkImage:
    png_bytes: bytes
    width: int
    height: int
    descriptor: dict


@dataclass(frozen=True)
class ArtworkResult:
    status: str
    resource_id: str
    registry_token: str
    descriptor: dict | None
    operation_id: str = ""


@dataclass(frozen=True)
class ArtworkRecovery:
    operation_id: str
    resource_id: str
    status: str
    message: str
    registry_token: str
    descriptor: dict
    before: dict | None


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _integer(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def _local_regular(path, limit):
    state = path.stat(follow_symlinks=False)
    if (not stat.S_ISREG(state.st_mode) or not 0 < state.st_size <= limit
            or int(getattr(state, "st_file_attributes", 0)) & (0x1000 | 0x40000 | 0x400000)):
        raise ModelResourceError("Artwork files must be bounded, fully local regular data.")
    return state


def validate_descriptor(value, *, resource_id=None):
    """Validate supported metadata without following any recorded location."""
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] not in (1, 2):
        raise ModelResourceError("This artwork record is unsupported; retain it unchanged.")
    if (not isinstance(value.get("resource_id"), str)
            or (resource_id is not None and value["resource_id"] != resource_id)
            or not isinstance(value.get("operation_id"), str) or not _OP.fullmatch(value["operation_id"])
            or value.get("content_rating") != "unknown"):
        raise ModelResourceError("The artwork identity or content-rating record is invalid.")
    source, thumbnail = value.get("source"), value.get("thumbnail")
    if not isinstance(source, dict) or not isinstance(thumbnail, dict):
        raise ModelResourceError("The artwork source or thumbnail record is invalid.")
    if value["schema_version"] == 1:
        if (source.get("kind") != "local_file" or not isinstance(source.get("path"), str)
                or not Path(source["path"]).is_absolute()):
            raise ModelResourceError("The artwork has no supported explicit local source.")
    else:
        from .swarm_model_import import PINNED_SWARM_REVISION, _model_name, _SUBTYPES
        if (source.get("kind") != "swarm_inline" or "path" in source
                or source.get("source_revision") != PINNED_SWARM_REVISION
                or source.get("subtype") not in _SUBTYPES):
            raise ModelResourceError("The artwork has no supported pinned Swarm inline source.")
        _model_name(source.get("model_name"))
        for key in ("endpoint", "server_id"):
            if not isinstance(source.get(key), str) or not 0 < len(source[key]) <= 4096:
                raise ModelResourceError("The artwork source enrollment is invalid.")
        endpoint = urlparse(source["endpoint"])
        if (endpoint.scheme != "http" or endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}
                or endpoint.username is not None or endpoint.password is not None or endpoint.query or endpoint.fragment):
            raise ModelResourceError("The artwork source is not a supported enrolled loopback endpoint.")
        for key in ("folder_fingerprint", "data_uri_sha256"):
            if not isinstance(source.get(key), str) or not _HEX.fullmatch(source[key]):
                raise ModelResourceError("The artwork source fingerprint is invalid.")
    if not isinstance(source.get("format"), str) or source["format"] not in _FORMATS:
        raise ModelResourceError("The artwork source image format is unsupported.")
    for row, limit, side in ((source, MAX_SOURCE_BYTES, MAX_SIDE), (thumbnail, MAX_THUMBNAIL_BYTES, THUMBNAIL_SIDE)):
        if (not isinstance(row.get("sha256"), str) or not _HEX.fullmatch(row["sha256"])
                or not _integer(row.get("size"), 1, limit)
                or not _integer(row.get("width"), 1, side) or not _integer(row.get("height"), 1, side)
                or row["width"] * row["height"] > MAX_PIXELS):
            raise ModelResourceError("The artwork identity or dimensions exceed supported bounds.")
    if thumbnail.get("format") != "PNG":
        raise ModelResourceError("Cached artwork must be a normalized PNG.")
    if len(_bytes(value)) > MAX_RECEIPT_BYTES // 4:
        raise ModelResourceError("The artwork metadata exceeds its supported bound.")
    return deepcopy(value)


def project_artwork(record, resource_id):
    """Return detached metadata and honest status, with no image/file probes."""
    if "artwork" not in record:
        return None, "absent"
    try:
        return validate_descriptor(record["artwork"], resource_id=resource_id), "recorded"
    except (ModelResourceError, ValueError, TypeError, RecursionError):
        return None, "unsupported"


def _decode(raw, *, filename=None, thumbnail=False):
    limit = MAX_THUMBNAIL_BYTES if thumbnail else MAX_SOURCE_BYTES
    if not isinstance(raw, bytes) or not 0 < len(raw) <= limit:
        raise ModelResourceError("The artwork exceeds the supported byte limit.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as image:
                fmt, width, height = image.format, image.width, image.height
                side = THUMBNAIL_SIDE if thumbnail else MAX_SIDE
                if (fmt not in ({"PNG"} if thumbnail else _FORMATS)
                        or not 0 < width <= side or not 0 < height <= side or width * height > MAX_PIXELS
                        or getattr(image, "n_frames", 1) != 1):
                    raise ModelResourceError("Choose one supported, bounded still image (PNG, JPEG or WebP).")
                if filename is not None and Path(filename).suffix.lower() not in _FORMATS[fmt]:
                    raise ModelResourceError("The artwork format does not match its filename extension.")
                image.verify()
            with Image.open(BytesIO(raw)) as image:
                image.load()
                if thumbnail:
                    if image.info or image.mode != "RGBA":
                        raise ModelResourceError("The cached thumbnail is not a metadata-free RGBA PNG.")
                    return raw, width, height, fmt
                oriented = ImageOps.exif_transpose(image)
                oriented.thumbnail((THUMBNAIL_SIDE, THUMBNAIL_SIDE), Image.Resampling.LANCZOS)
                clean = Image.new("RGBA", oriented.size)
                clean.paste(oriented.convert("RGBA"))
                output = BytesIO()
                clean.save(output, format="PNG")
                normalized = output.getvalue()
                if len(normalized) > MAX_THUMBNAIL_BYTES:
                    raise ModelResourceError("The normalized thumbnail exceeds the supported byte limit.")
                return normalized, width, height, fmt
    except (OSError, ValueError, SyntaxError, KeyError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ModelResourceError("The chosen artwork could not be safely decoded.") from exc


def _review_fingerprint(review):
    value = asdict(review)
    value["source_path"] = str(review.source_path)
    value["thumbnail_bytes"] = _sha(review.thumbnail_bytes)
    return _sha(_bytes(value))


class ModelArtworkService:
    def __init__(self, registry, *, protected_roots=(), managed_roots=(), boundary_provider=None):
        self.registry = registry
        self.root = registry.path.parent / "model-artwork"
        self.protected_roots = tuple(protected_roots)
        self.managed_roots = tuple(managed_roots)
        self.boundary_provider = boundary_provider
        self._issued = {}
        self._source_guards = {}
        self._recoveries = {}

    def _boundaries(self):
        if self.boundary_provider is not None:
            protected, managed = self.boundary_provider()
            if tuple(protected) != self.protected_roots or tuple(managed) != self.managed_roots:
                raise ModelResourceError("Protected or managed locations changed; reopen the artwork review.")
        return (*self.protected_roots, *self.managed_roots)

    def _guard(self, path):
        return _path(path, self._boundaries())

    def _observe(self, resource_id, expected=None):
        self._guard(self.registry.path)
        self._guard(self.registry.pending)
        snapshot, token = self.registry.snapshot()
        if expected is not None and token != expected:
            raise ModelResourceError("The model registry changed; review the artwork again.")
        record = snapshot["resources"].get(resource_id)
        if record is None:
            raise ModelResourceError("The selected model is no longer registered.")
        return snapshot, token, record

    @contextmanager
    def _writer(self):
        for path in (self.registry.path, self.registry.pending,
                     self.registry.path.with_name(self.registry.path.name + ".lock"),
                     self.registry.path.parent / "model-resource-recovery"):
            self._guard(path)
        with self.registry._writer():
            yield

    def read(self, resource_id):
        _, token, record = self._observe(resource_id)
        if "artwork" not in record:
            return None, token
        return validate_descriptor(record["artwork"], resource_id=resource_id), token

    def _source(self, path, should_cancel=None):
        _cancel(should_cancel)
        path = self._guard(path)
        # Cache files are not implicit alternative sources, even when selected.
        if path == self.root or self.root in path.parents:
            raise ModelResourceError("Choose an original local image outside the artwork cache.")
        _local_regular(path, MAX_SOURCE_BYTES)
        raw = _bounded_read(path, MAX_SOURCE_BYTES)
        if raw is None:
            raise ModelResourceError("The selected artwork is missing.")
        _cancel(should_cancel)
        self._guard(path)
        return path, raw

    def preview_local(self, resource_id, path, *, expected_registry, should_cancel=None, progress=None):
        _cancel(should_cancel)
        self._observe(resource_id, expected_registry)
        path, raw = self._source(path, should_cancel)
        return self._prepare_review(resource_id, raw, {"kind": "local_file", "path": str(path)},
            source_path=path, expected_registry=expected_registry, should_cancel=should_cancel, progress=progress)

    def _preview_imported(self, resource_id, raw, source, *, expected_registry, source_guard,
                          should_cancel=None, progress=None):
        """Internal source-owner admission; guards are never restored from data.

        The qualified source owner supplies an in-memory context factory that
        rechecks its frozen evidence and pins its selected model through Apply.
        A descriptor or caller-edited review cannot supply this capability.
        """
        if not callable(source_guard) or not isinstance(source, dict) or source.get("kind") != "swarm_inline":
            raise ModelResourceError("A qualified inline source review is required.")
        return self._prepare_review(resource_id, raw, source, source_path=None,
            expected_registry=expected_registry, source_guard=source_guard,
            should_cancel=should_cancel, progress=progress)

    def _prepare_review(self, resource_id, raw, source, *, source_path, expected_registry,
                        source_guard=None, should_cancel=None, progress=None):
        _cancel(should_cancel)
        _, token, record = self._observe(resource_id, expected_registry)
        before = deepcopy(record.get("artwork"))
        if "artwork" in record:
            validate_descriptor(before, resource_id=resource_id)
        if progress: progress(0, 1, "Validating chosen artwork")
        png, width, height, fmt = _decode(raw, filename=source_path)
        _, thumb_width, thumb_height, _ = _decode(png, thumbnail=True)
        descriptor = {"schema_version": 1 if source_path is not None else 2, "resource_id": resource_id, "operation_id": uuid4().hex,
            "source": {**deepcopy(source), "sha256": _sha(raw), "size": len(raw),
                       "width": width, "height": height, "format": fmt},
            "thumbnail": {"sha256": _sha(png), "size": len(png), "width": thumb_width, "height": thumb_height, "format": "PNG"},
            "content_rating": "unknown"}
        validate_descriptor(descriptor, resource_id=resource_id)
        if before is not None and all(before.get(key) == descriptor[key] for key in ("resource_id", "source", "thumbnail", "content_rating")):
            try:
                self.resolve(before, should_cancel=should_cancel)
            except (OSError, ModelResourceError):
                # Explicitly choosing the original again repairs an unavailable
                # cache through a new operation, never by replacing old bytes.
                pass
            else:
                descriptor = deepcopy(before)
        self._observe(resource_id, token)
        _cancel(should_cancel)
        review = ArtworkReview(resource_id, token, source_path, _sha(raw), len(raw), width, height, fmt, png,
                               descriptor, before, uuid4().hex)
        self._issued[review.review_id] = _review_fingerprint(review)
        if source_guard is not None: self._source_guards[review.review_id] = source_guard
        while len(self._issued) > 32:
            expired = next(iter(self._issued))
            self._issued.pop(expired)
            self._source_guards.pop(expired, None)
        if progress: progress(1, 1, "Artwork review ready; no files changed")
        return review

    @contextmanager
    def _source_context(self, review, *, should_cancel=None, progress=None):
        if review.descriptor["schema_version"] == 2:
            guard = self._source_guards.get(review.review_id)
            if guard is None or review.source_path is not None:
                raise ModelResourceError("The imported artwork source review expired; read the selected source again.")
            with guard(should_cancel=should_cancel, progress=progress):
                yield
            return
        self._guard(review.source_path)
        _local_regular(review.source_path, MAX_SOURCE_BYTES)  # before a native read pin could hydrate it
        with _pinned_model_file(review.source_path, self._boundaries()):
            _, raw = self._source(review.source_path, should_cancel)
            if _sha(raw) != review.source_sha256 or len(raw) != review.source_size:
                raise ModelResourceError("The selected source image changed; preview it again.")
            yield

    def _folder(self, operation_id):
        if not isinstance(operation_id, str) or not _OP.fullmatch(operation_id):
            raise ModelResourceError("The artwork recovery identity is invalid.")
        self._guard(self.root)
        return self._guard(self.root / operation_id)

    def _cached(self, path, descriptor, should_cancel=None):
        _cancel(should_cancel)
        self._guard(path)
        _local_regular(path, MAX_THUMBNAIL_BYTES)
        raw = _bounded_read(path, MAX_THUMBNAIL_BYTES)
        thumb = descriptor["thumbnail"]
        if raw is None or len(raw) != thumb["size"] or _sha(raw) != thumb["sha256"]:
            raise ModelResourceError("Cached artwork is missing or changed; reselect the original image.")
        _, width, height, _ = _decode(raw, thumbnail=True)
        if (width, height) != (thumb["width"], thumb["height"]):
            raise ModelResourceError("Cached artwork dimensions differ from their record.")
        self._guard(path)
        _cancel(should_cancel)
        return ArtworkImage(raw, width, height, deepcopy(descriptor))

    def resolve(self, descriptor, *, should_cancel=None):
        value = validate_descriptor(descriptor)
        receipt, _ = self._receipt(value["operation_id"])
        if receipt["descriptor"] != value:
            raise ModelResourceError("The cached artwork does not match its exact association receipt.")
        return self._cached(self._folder(value["operation_id"]) / "thumbnail.png", value, should_cancel)

    def _publish(self, folder, descriptor, should_cancel=None):
        staged, target = folder / "staged.png", folder / "thumbnail.png"
        self._guard(target)
        if target.exists():
            return self._cached(target, descriptor, should_cancel)
        self._cached(staged, descriptor, should_cancel)
        with _pinned_model_file(staged, self._boundaries()):
            _cancel(should_cancel)
            self._guard(target)
            os.link(staged, target)  # exclusive publication: never replace an existing file
            return self._cached(target, descriptor, should_cancel)

    def apply(self, review, *, confirmed=False, should_cancel=None, progress=None):
        if not confirmed:
            raise ModelResourceError("Confirm the displayed artwork association before applying.")
        if not isinstance(review, ArtworkReview) or self._issued.get(review.review_id) != _review_fingerprint(review):
            raise ModelResourceError("The artwork review changed or expired; preview it again.")
        review = deepcopy(review)
        descriptor = validate_descriptor(review.descriptor, resource_id=review.resource_id)
        _decode(review.thumbnail_bytes, thumbnail=True)
        if (_sha(review.thumbnail_bytes) != descriptor["thumbnail"]["sha256"]
                or len(review.thumbnail_bytes) != descriptor["thumbnail"]["size"]):
            raise ModelResourceError("The reviewed thumbnail changed.")
        _cancel(should_cancel)
        self._observe(review.resource_id, review.registry_token)
        with self._source_context(review, should_cancel=should_cancel, progress=progress):
            with self._writer():
                value, token, record = self._observe(review.resource_id, review.registry_token)
                if record.get("artwork") != review.before:
                    raise ModelResourceError("The model's artwork choice changed; review it again.")
                if descriptor == review.before:
                    self.resolve(descriptor, should_cancel=should_cancel)
                    return ArtworkResult("unchanged", review.resource_id, token, deepcopy(descriptor), descriptor["operation_id"])
                folder = self._folder(descriptor["operation_id"])
                self.registry.path.parent.mkdir(parents=True, exist_ok=True)
                with _pinned_parent(self.registry.path.parent, ()):
                    self._guard(self.root)
                    self.root.mkdir(exist_ok=True)
                    with _pinned_parent(self.root, self._boundaries()):
                        _cancel(should_cancel)
                        folder.mkdir(exist_ok=False)
                        try:
                            with _pinned_parent(folder, self._boundaries()):
                                receipt = {"schema_version": 1, "operation_id": descriptor["operation_id"],
                                    "resource_id": review.resource_id, "registry_token": token,
                                    "before": review.before, "descriptor": descriptor}
                                atomic_verified_bytes(self._guard(folder / "receipt.json"), _bytes(receipt), label="Artwork recovery")
                                _cancel(should_cancel)
                                atomic_verified_bytes(self._guard(folder / "staged.png"), review.thumbnail_bytes, label="Artwork thumbnail")
                                self._publish(folder, descriptor, should_cancel)
                                with _pinned_model_file(folder / "thumbnail.png", self._boundaries()):
                                    self.resolve(descriptor, should_cancel=should_cancel)
                                    self._observe(review.resource_id, token)
                                    _cancel(should_cancel)
                                    record["artwork"] = deepcopy(descriptor)
                                    self.registry._save(value, token)
                                    _, token, saved = self._observe(review.resource_id)
                                    if saved.get("artwork") != descriptor:
                                        raise ModelResourceError("Artwork association readback failed.")
                        except Exception as exc:
                            raise ArtworkRecoveryRequired(descriptor["operation_id"], str(exc)) from exc
        if progress: progress(1, 1, "Artwork association saved; original source unchanged")
        return ArtworkResult("applied", review.resource_id, token, deepcopy(descriptor), descriptor["operation_id"])

    def remove(self, resource_id, *, expected_registry, expected_descriptor, confirmed=False):
        if not confirmed:
            raise ModelResourceError("Confirm removal of this artwork association.")
        expected_descriptor = validate_descriptor(expected_descriptor, resource_id=resource_id)
        with self._writer():
            value, token, record = self._observe(resource_id, expected_registry)
            if record.get("artwork") != expected_descriptor:
                raise ModelResourceError("The model's artwork choice changed; review removal again.")
            del record["artwork"]
            self.registry._save(value, token)
            _, token, saved = self._observe(resource_id)
            if "artwork" in saved:
                raise ModelResourceError("Artwork removal readback failed; review registry recovery.")
        return ArtworkResult("removed", resource_id, token, None)

    def _receipt(self, operation_id):
        path = self._guard(self._folder(operation_id) / "receipt.json")
        _local_regular(path, MAX_RECEIPT_BYTES)
        raw = _bounded_read(path, MAX_RECEIPT_BYTES)
        if raw is None:
            raise ModelResourceError("The exact artwork recovery receipt is missing.")
        receipt = _json(raw)
        if (not isinstance(receipt, dict) or receipt.get("schema_version") != 1
                or receipt.get("operation_id") != operation_id
                or not isinstance(receipt.get("registry_token"), str) or not _HEX.fullmatch(receipt["registry_token"])):
            raise ModelResourceError("The artwork recovery receipt is invalid.")
        validate_descriptor(receipt.get("descriptor"), resource_id=receipt.get("resource_id"))
        if receipt["descriptor"]["operation_id"] != operation_id:
            raise ModelResourceError("Artwork recovery belongs to a different operation.")
        if receipt.get("before") is not None:
            validate_descriptor(receipt["before"], resource_id=receipt["resource_id"])
        return receipt, _sha(raw)

    def preview_recovery(self, operation_id, *, should_cancel=None):
        _cancel(should_cancel)
        receipt, fingerprint = self._receipt(operation_id)
        descriptor = receipt["descriptor"]
        _, token, record = self._observe(receipt["resource_id"])
        current = record.get("artwork")
        status, message = "held", "The artwork or registry changed; preserve this receipt and review a fresh choice."
        if current == descriptor or (current == receipt["before"] and token == receipt["registry_token"]):
            try:
                folder = self._folder(operation_id)
                target = self._guard(folder / "thumbnail.png")
                self._cached(target if target.exists() else folder / "staged.png", descriptor, should_cancel)
                if current == descriptor and target.exists():
                    status, message = "completed", "The exact saved association and cached thumbnail are verified."
                elif current != descriptor:
                    status, message = "ready", "Publish this verified cached thumbnail and the originally reviewed association."
            except (OSError, ModelResourceError) as exc:
                message = f"Cached recovery bytes are unavailable: {exc} Choose the original again; no source is reopened here."
        self._observe(receipt["resource_id"], token)
        review = ArtworkRecovery(operation_id, receipt["resource_id"], status, message, token,
                                 deepcopy(descriptor), deepcopy(receipt["before"]))
        self._recoveries[operation_id] = (_sha(_bytes(asdict(review))), fingerprint)
        while len(self._recoveries) > 32: self._recoveries.pop(next(iter(self._recoveries)))
        return review

    def recover(self, review, *, confirmed=False, should_cancel=None):
        if not confirmed:
            raise ModelResourceError("Confirm the exact artwork recovery before applying.")
        if not isinstance(review, ArtworkRecovery):
            raise ModelResourceError("Review the exact artwork recovery first.")
        issued = self._recoveries.get(review.operation_id)
        receipt, fingerprint = self._receipt(review.operation_id)
        if issued != (_sha(_bytes(asdict(review))), fingerprint):
            raise ModelResourceError("The artwork recovery review changed or expired.")
        review = deepcopy(review)
        if review.status not in {"ready", "completed"}:
            raise ModelResourceError(review.message)
        with self._writer():
            value, token, record = self._observe(review.resource_id, review.registry_token)
            if review.status == "completed":
                if record.get("artwork") != review.descriptor:
                    raise ModelResourceError("The saved artwork association changed.")
                self.resolve(review.descriptor, should_cancel=should_cancel)
                return ArtworkResult("unchanged", review.resource_id, token, deepcopy(review.descriptor), review.operation_id)
            if record.get("artwork") != review.before or token != receipt["registry_token"]:
                raise ModelResourceError("The original artwork choice or registry changed; do not replay this operation.")
            folder = self._folder(review.operation_id)
            try:
                with _pinned_parent(folder, self._boundaries()):
                    self._publish(folder, review.descriptor, should_cancel)
                    with _pinned_model_file(folder / "thumbnail.png", self._boundaries()):
                        self.resolve(review.descriptor, should_cancel=should_cancel)
                        self._observe(review.resource_id, token)
                        _cancel(should_cancel)
                        record["artwork"] = deepcopy(review.descriptor)
                        self.registry._save(value, token)
                        _, token, saved = self._observe(review.resource_id)
                        if saved.get("artwork") != review.descriptor:
                            raise ModelResourceError("Recovered artwork association readback failed.")
            except Exception as exc:
                raise ArtworkRecoveryRequired(review.operation_id, str(exc)) from exc
        return ArtworkResult("applied", review.resource_id, token, deepcopy(review.descriptor), review.operation_id)
