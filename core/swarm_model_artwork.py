"""Explicit selected-model Swarm inline artwork, at one pinned source contract.

DescribeModel is the only request. References are never followed; the existing
metadata-only importer continues discarding artwork and retaining its own limits.
"""
from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import re
import stat
from urllib.parse import urlparse
from uuid import uuid4

from .model_artwork import ArtworkReview, ModelArtworkService, _decode, _review_fingerprint, _sha
from .model_resource_registry import ModelResourceError, ResourceInspection, _bytes, _cancel, _current, inspect_model_resource
from .model_cache_reference import CacheReferenceReview
from .model_storage_policy import retained_fast_roots
from .model_workspace import _path, _pinned_model_file
from .swarm_model_import import (
    MAX_METADATA_BYTES, PINNED_SWARM_REVISION, SwarmModelImport,
    SwarmModelImportError, _json_bytes, parse_describe_model,
)

MAX_INLINE_CHARS = 8 * 1024 * 1024
MAX_ARTWORK_RESPONSE_BYTES = MAX_INLINE_CHARS + MAX_METADATA_BYTES + 4096
_INLINE = re.compile(r"data:image/(png|jpeg|jpg|webp);base64,([A-Za-z0-9+/]*={0,2})\Z", re.ASCII)
_MIME_FORMAT = {"png": "PNG", "jpeg": "JPEG", "jpg": "JPEG", "webp": "WEBP"}
_PLACEHOLDERS = frozenset({"imgs/model_placeholder.jpg", "imgs/legacy_ckpt.jpg"})


class SwarmArtworkUnavailable(SwarmModelImportError):
    def __init__(self, status, reason):
        self.status, self.reason = status, reason
        super().__init__(reason)


@dataclass(frozen=True)
class SwarmInlineImage:
    raw_bytes: bytes
    data_uri_sha256: str
    width: int
    height: int
    format: str


@dataclass(frozen=True)
class SwarmArtworkReview:
    artwork: ArtworkReview
    resource_id: str
    selected_path: Path
    subtype: str
    model_name: str
    inspection: ResourceInspection
    registry_token: str
    inventory_fingerprint: str
    source_binding: dict
    source_digest: str
    review_id: str


@dataclass(frozen=True)
class _SourceRead:
    image: SwarmInlineImage
    selected_path: Path
    inspection: ResourceInspection
    registry_token: str
    inventory_fingerprint: str
    source_binding: dict
    source: dict
    cache_review: CacheReferenceReview | None = None

    @property
    def digest(self):
        return _sha(_bytes(self.source))


def parse_inline_artwork(payload, *, subtype, model_name) -> SwarmInlineImage:
    """Parse one bounded response without fetching a referenced image."""
    if not isinstance(payload, dict) or set(payload) != {"model"} or not isinstance(payload["model"], dict):
        raise SwarmModelImportError("SwarmUI did not return one selected model description.")
    if len(_json_bytes(payload)) > MAX_ARTWORK_RESPONSE_BYTES:
        raise SwarmModelImportError("The SwarmUI artwork response exceeds the separate response limit.")
    model = payload["model"]
    metadata = {key: value for key, value in model.items() if key != "preview_image"}
    if len(_json_bytes({"model": metadata})) > MAX_METADATA_BYTES:
        raise SwarmModelImportError("The non-artwork model description exceeds its metadata limit.")
    # Reuse selected-name/local and metadata schema checks without broadening the
    # old parser's 1 MiB contract or ever feeding it the inline artwork payload.
    parse_describe_model({"model": metadata}, subtype=subtype, model_name=model_name)
    value = model.get("preview_image")
    if value is None or isinstance(value, str) and (not value.strip() or value in _PLACEHOLDERS):
        raise SwarmArtworkUnavailable("unavailable", "The selected SwarmUI model has no preview artwork.")
    if not isinstance(value, str) or len(value) > MAX_INLINE_CHARS:
        raise SwarmArtworkUnavailable("unsupported", "The selected artwork is not a supported bounded inline image.")
    match = _INLINE.fullmatch(value)
    if match is None:
        raise SwarmArtworkUnavailable("unsupported", "Only inline PNG, JPEG or WebP artwork is supported; image references are not followed.")
    encoded = match.group(2)
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise SwarmArtworkUnavailable("unsupported", "The selected artwork has invalid base64 data.") from exc
    if not raw or base64.b64encode(raw).decode("ascii") != encoded:
        raise SwarmArtworkUnavailable("unsupported", "The selected artwork needs canonical, complete base64 data.")
    try:
        _, width, height, fmt = _decode(raw)
    except ModelResourceError as exc:
        raise SwarmArtworkUnavailable("unsupported", str(exc)) from exc
    if fmt != _MIME_FORMAT[match.group(1)]:
        raise SwarmArtworkUnavailable("unsupported", "The inline artwork MIME type does not match its decoded image format.")
    return SwarmInlineImage(raw, _sha(value.encode("ascii")), width, height, fmt)


def _loopback_binding(binding):
    if (not isinstance(binding, dict) or binding.get("source_revision") != PINNED_SWARM_REVISION
            or not isinstance(binding.get("endpoint"), str) or not isinstance(binding.get("server_id"), str)
            or not binding["server_id"]):
        raise SwarmModelImportError("The selected SwarmUI source has no qualified artwork enrollment.")
    endpoint = urlparse(binding["endpoint"])
    if (endpoint.scheme != "http" or endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}
            or endpoint.username is not None or endpoint.password is not None or endpoint.query or endpoint.fragment):
        raise SwarmModelImportError("Select a qualified enrolled loopback SwarmUI source.")


def _fingerprint(review):
    return _sha(_bytes({"artwork": _review_fingerprint(review.artwork), "resource_id": review.resource_id,
        "selected_path": str(review.selected_path), "subtype": review.subtype, "model_name": review.model_name,
        "inspection": {"path": str(review.inspection.path), "identity": review.inspection.stored_identity(),
                       "file_state": review.inspection.file_state}, "registry_token": review.registry_token,
        "inventory_fingerprint": review.inventory_fingerprint, "source_binding": review.source_binding,
        "source_digest": review.source_digest, "review_id": review.review_id}))


class SwarmModelArtwork:
    def __init__(self, artwork_service: ModelArtworkService, metadata_importer: SwarmModelImport):
        if artwork_service.registry.path != metadata_importer.workspace.registry.path:
            raise ModelResourceError("Artwork and source review must use the same model registry.")
        self.artwork = artwork_service
        self.importer = metadata_importer
        self._issued = {}

    def _selected_boundary(self, resource_id, selected_path, expected_registry, cache_selection=None):
        value, _, _ = self.artwork._observe(resource_id, expected_registry)
        if cache_selection is None:
            selected = _path(selected_path, (*self.artwork._boundaries(), *self.importer.workspace.protected_roots,
                                            *retained_fast_roots(value)))
        else:
            _, _, selected = self.importer._selected(resource_id, selected_path, expected_registry, cache_selection)
            selected = _path(selected, self.artwork._boundaries())
        state = selected.stat(follow_symlinks=False)
        if (not stat.S_ISREG(state.st_mode)
                or int(getattr(state, "st_file_attributes", 0)) & (0x1000 | 0x40000 | 0x400000)):
            raise SwarmModelImportError("The selected registered model must be fully local before artwork import.")
        return selected

    def candidates(self, resource_id, selected_path, *, expected_registry, should_cancel=None, cache_selection=None):
        _cancel(should_cancel)
        selected = self._selected_boundary(resource_id, selected_path, expected_registry, cache_selection)
        current = self.importer.correspondence(resource_id, selected, expected_registry=expected_registry,
                                               should_cancel=should_cancel, cache_selection=cache_selection)
        _loopback_binding(current.source_binding)
        return (current.choice,)

    def _read_source(self, resource_id, selected_path, *, subtype, model_name, expected_registry,
                     should_cancel=None, progress=None, cache_selection=None, guarded_inspection=None, cache_review=None):
        _cancel(should_cancel)
        selected = self._selected_boundary(resource_id, selected_path, expected_registry, cache_selection)
        before = self.importer.correspondence(resource_id, selected, expected_registry=expected_registry,
                                              should_cancel=should_cancel, cache_selection=cache_selection)
        _loopback_binding(before.source_binding)
        if (subtype, model_name) != (before.choice.subtype, before.choice.model_name):
            raise SwarmModelImportError("The selected model's SwarmUI correspondence changed; review it again.")
        paths = self.importer.paths
        source_record = paths._record()
        if paths._binding(source_record) != before.source_binding:
            raise SwarmModelImportError("The SwarmUI enrollment changed before the artwork read.")
        try:
            transport, session_id = paths.manager._server_session(source_record)
            payload = transport.post("DescribeModel", {"session_id": session_id, "subtype": subtype,
                                     "modelName": model_name}, max_bytes=MAX_ARTWORK_RESPONSE_BYTES)
        except InterruptedError:
            raise
        except Exception as exc:
            raise SwarmArtworkUnavailable("unavailable", "The selected SwarmUI artwork description is unavailable.") from exc
        _cancel(should_cancel)
        image = parse_inline_artwork(payload, subtype=subtype, model_name=model_name)
        self._selected_boundary(resource_id, selected, expected_registry, cache_selection)
        if cache_selection is None:
            inspection = inspect_model_resource(selected, should_cancel=should_cancel, progress=progress)
        elif guarded_inspection is None:
            verified = self.importer._review_cache(resource_id, selected, cache_selection, expected_registry,
                should_cancel=should_cancel, progress=progress)
            inspection, cache_review = verified.inspection, verified.review
        else:
            inspection = guarded_inspection
        if inspection.resource_id != resource_id or inspection.stored_identity() != before.record["identity"]:
            raise SwarmModelImportError("The selected bytes do not match the registered full-file model identity.")
        _current(inspection)
        after = self.importer.correspondence(resource_id, selected, expected_registry=expected_registry,
                                             should_cancel=should_cancel, cache_selection=cache_selection)
        if before != after:
            raise SwarmModelImportError("The enrolled model or source folders changed during the artwork read.")
        source = {"kind": "swarm_inline", "endpoint": before.source_binding["endpoint"],
            "server_id": before.source_binding["server_id"], "source_revision": PINNED_SWARM_REVISION,
            "folder_fingerprint": before.folder_fingerprint, "subtype": subtype, "model_name": model_name,
            "data_uri_sha256": image.data_uri_sha256, "sha256": _sha(image.raw_bytes), "size": len(image.raw_bytes),
            "width": image.width, "height": image.height, "format": image.format}
        return _SourceRead(image, selected, inspection, expected_registry, before.inventory.fingerprint,
                           deepcopy(before.source_binding), source, cache_review)

    def preview(self, resource_id, selected_path, *, subtype, model_name, expected_registry,
                should_cancel=None, progress=None, cache_selection=None):
        cache_selection = deepcopy(cache_selection)
        source = self._read_source(resource_id, selected_path, subtype=subtype, model_name=model_name,
            expected_registry=expected_registry, should_cancel=should_cancel, progress=progress, cache_selection=cache_selection)
        frozen = deepcopy(source)

        def recheck(*, should_cancel=None, progress=None, guarded_inspection=None):
            fresh = self._read_source(resource_id, frozen.selected_path, subtype=subtype, model_name=model_name,
                expected_registry=frozen.registry_token, should_cancel=should_cancel, progress=progress,
                cache_selection=cache_selection, guarded_inspection=guarded_inspection, cache_review=frozen.cache_review)
            if (fresh.digest != frozen.digest or fresh.inspection != frozen.inspection
                    or fresh.source_binding != frozen.source_binding or fresh.inventory_fingerprint != frozen.inventory_fingerprint):
                raise SwarmModelImportError("The selected SwarmUI artwork, model or source changed after review.")

        @contextmanager
        def guard(*, should_cancel=None, progress=None):
            _cancel(should_cancel)
            self._selected_boundary(resource_id, frozen.selected_path, frozen.registry_token, cache_selection)
            if cache_selection is not None:
                with self.importer.cache_references.pinned(frozen.cache_review, should_cancel=should_cancel, progress=progress) as verified:
                    recheck(should_cancel=should_cancel, progress=progress, guarded_inspection=verified.inspection)
                    yield
            else:
                with _pinned_model_file(frozen.selected_path, (*self.artwork._boundaries(), *self.importer.workspace.protected_roots)):
                    recheck(should_cancel=should_cancel, progress=progress)
                    yield

        reviewed = self.artwork._preview_imported(resource_id, source.image.raw_bytes, source.source,
            expected_registry=expected_registry, source_guard=guard, should_cancel=should_cancel, progress=progress)
        review = SwarmArtworkReview(reviewed, resource_id, source.selected_path, subtype, model_name, source.inspection,
            expected_registry, source.inventory_fingerprint, deepcopy(source.source_binding), source.digest, uuid4().hex)
        self._issued[review.review_id] = _fingerprint(review)
        while len(self._issued) > 32: self._issued.pop(next(iter(self._issued)))
        return review

    def apply(self, review, *, confirmed=False, should_cancel=None, progress=None):
        if not confirmed:
            raise ModelResourceError("Confirm the selected SwarmUI artwork review before importing.")
        if not isinstance(review, SwarmArtworkReview) or self._issued.get(review.review_id) != _fingerprint(review):
            raise SwarmModelImportError("The SwarmUI artwork review changed or expired; read the selected source again.")
        return self.artwork.apply(deepcopy(review.artwork), confirmed=True, should_cancel=should_cancel, progress=progress)
