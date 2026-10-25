"""Explicit saved-data capture admission for one selected workflow operation.

No global current-library state or implicit enrollment is used. Producers opt
in by requesting a context for an exact member before their canonical save.
"""
from __future__ import annotations

from contextlib import contextmanager
from itertools import islice
from pathlib import Path
import re
from threading import RLock
from types import MappingProxyType
from uuid import uuid4

from .history_configuration import HistoryConfigurationError, path_key


MAX_CAPTURE_IMAGES = 1000
_OPERATION_ID = re.compile(r"[0-9a-f]{32}\Z")


def configured_capture(configuration, images, *, operation_kind, reason="", producer="collectigen", cancelled=lambda: False):
    """Choose explicit opt-in capture without silently dropping part of a scope.

    Unconfigured or explicitly disabled sources keep their existing save path.
    A stale binding or a mixture with participating sources holds the operation
    before any source admission. Callers display that reason and keep the draft.
    """
    selected = tuple(islice(iter(images), MAX_CAPTURE_IMAGES + 1))
    if not selected or len(selected) > MAX_CAPTURE_IMAGES:
        raise HistoryConfigurationError("Select between one and 1000 images for one saved-data operation.")
    paths = tuple(path_key(image) for image in selected)
    if len(set(paths)) != len(paths):
        raise HistoryConfigurationError("Select each exact image once for saved-data capture.")
    descriptors = tuple(configuration.resolve(path=image) for image in paths)
    held = next((item for item in descriptors if item.status not in {"configured", "not_configured", "disabled"}), None)
    if held is not None:
        raise HistoryConfigurationError(held.reason)
    configured = sum(item.status == "configured" for item in descriptors)
    if not configured:
        return None
    if configured != len(paths):
        raise HistoryConfigurationError("This selection mixes sources with and without saved history. Select one group or review History locations in Setup before saving.")
    return HistoryCaptureOperation(configuration, paths, operation_kind=operation_kind,
        reason=reason, producer=producer, cancelled=cancelled)


class HistoryCaptureCancelled(InterruptedError):
    """Capture was cancelled before the next image transaction began."""


class HistoryCaptureOperation:
    """Freeze selected configuration without reading images or creating stores.

    ``context_for`` is deliberate admission and can create the configured
    journal/index. Its returned context rechecks configuration and source
    identity on every write. A batch is a group of per-image transactions,
    never a globally atomic snapshot.
    """

    def __init__(self, configuration, images, *, operation_kind, reason="",
                 operation_id=None, producer="collectigen", cancelled=lambda: False):
        selected = tuple(islice(iter(images), MAX_CAPTURE_IMAGES + 1))
        if not selected or len(selected) > MAX_CAPTURE_IMAGES:
            raise HistoryConfigurationError("Select between one and 1000 images for one saved-history operation.")
        self.operation_id = operation_id if operation_id is not None else uuid4().hex
        if type(self.operation_id) is not str or not _OPERATION_ID.fullmatch(self.operation_id):
            raise HistoryConfigurationError("Saved-history grouping needs an exact operation identity.")
        for value, maximum in ((operation_kind, 128), (producer, 128), (reason, 2048)):
            try:
                valid = type(value) is str and len(value.encode("utf-8")) <= maximum
            except UnicodeError:
                valid = False
            if not valid:
                raise HistoryConfigurationError("Saved-history operation metadata exceeds its supported bound.")
        if not operation_kind or not producer:
            raise HistoryConfigurationError("Identify the saved operation and its producer.")
        self.operation_kind, self.reason, self.producer = operation_kind, reason, producer
        self.configuration = configuration
        self.cancelled = cancelled
        members = {}
        for image in selected:
            self._check_cancel()
            key = path_key(image)
            if key in members:
                raise HistoryConfigurationError("Select each exact image once for saved-history capture.")
            descriptor = configuration.resolve(path=key)
            if descriptor.status != "configured":
                raise HistoryConfigurationError(descriptor.reason)
            members[key] = descriptor
        self.members = MappingProxyType(members)
        self._contexts = {}
        self._stores = {}
        self._lock = RLock()

    def _check_cancel(self):
        if self.cancelled():
            raise HistoryCaptureCancelled("Saved-history operation cancelled before the next image.")

    def context_for(self, image: str | Path):
        """Admit an exact member; no omitted/disabled context may bypass capture."""
        self._check_cancel()
        key = path_key(image)
        descriptor = self.members.get(key)
        if descriptor is None:
            raise HistoryConfigurationError("This image is outside the selected saved-history operation.")
        with self._lock:
            if key not in self._contexts:
                # Preserve one factory object per context: the canonical writer
                # uses its identity to reuse an already held admission guard.
                @contextmanager
                def guard():
                    self._check_cancel()
                    with self.configuration.write_guard(descriptor, path=key):
                        yield

                with guard():
                    from .history_versions import HistoryStore
                    from .image_knowledge_history import HistoryWriteContext
                    scope = descriptor.scope()
                    store = self._stores.get(scope.key)
                    if store is None:
                        store = HistoryStore(scope)
                        self._stores[scope.key] = store
                    from .history_projection import recover_projection_before_bind
                    recover_projection_before_bind(store,key)
                    entity = store.bind(key)
                    context = HistoryWriteContext(store, entity, self.operation_id,
                        operation_kind=self.operation_kind, reason=self.reason,
                        producer=self.producer, authority_guard=guard)
                self._contexts[key] = context
            else:
                # Returning a previously bound context must still refuse a
                # changed/disabled configuration before the producer proceeds.
                with self._contexts[key].authority_guard():
                    pass
            return self._contexts[key]

    def replace_context(self,image,expected_context,renewed_context):
        """Adopt only an explicitly receipt-qualified same-image renewal."""
        key=path_key(image)
        with self._lock:
            if self._contexts.get(key) is not expected_context or renewed_context.operation_id!=self.operation_id:
                raise HistoryConfigurationError("The cached projection context changed; reopen the exact operation.")
            from .history_projection import validate_context_renewal
            validate_context_renewal(expected_context,renewed_context,key)
            self._contexts[key]=renewed_context
