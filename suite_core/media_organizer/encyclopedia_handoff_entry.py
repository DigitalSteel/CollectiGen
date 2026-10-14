"""Receiver-only GUI-thread seam; no Qt import, scan, save or transport.

The integration owner explicitly supplies fresh canonical catalog admission.
Source components do not connect a browser or qualify visible/native acceptance.
"""
from core.organizer_encyclopedia_handoffs import (
    AdmittedCatalog, CanonicalRenameRequest, HandoffHold, MatchingImagesRequest,
    admit_request, decode_request,
)

from .contracts import BoundaryError


class OrganizerEncyclopediaHandoffReceiver:
    """One admitted host's in-memory, at-most-once dispatch receiver.

    `read_admitted_catalog(state)` must read existing native admission, without
    initializing catalogs or using incoming descriptors as alias authority. Unknown
    admission holds the request. Request IDs/correction suggestions are retained for
    this receiver lifetime only; there is no durable cross-process replay claim.
    """
    def __init__(self, host, *, read_admitted_catalog):
        if not callable(read_admitted_catalog):
            raise HandoffHold("native_admission_reader_required")
        self.host = host
        self.read_admitted_catalog = read_admitted_catalog
        self.last_hold_reason = ""
        self._requests = set()
        self._renames = set()
        self._pending_actions = {}
        self._check_host()
        self._knowledge = host.state.knowledge

    def _check_host(self):
        organizer = getattr(self.host, "organizer", None)
        if organizer is None or organizer.host is not self.host:
            raise HandoffHold("original_admitted_organizer_required")
        organizer._check_context()
        if any(not callable(getattr(self.host, name, None)) for name in
               ("open_dataset_tag", "open_dataset_function", "_queue_dataset_page_action")):
            raise HandoffHold("original_dataset_routes_required")
        if hasattr(self, "_knowledge") and self.host.state.knowledge is not self._knowledge:
            raise HandoffHold("knowledge_authority_changed")

    def _admit(self, request):
        self._check_host()
        catalog = self.read_admitted_catalog(self.host.state)
        if type(catalog) is not AdmittedCatalog:
            raise HandoffHold("native_catalog_admission_unavailable")
        payload = admit_request(request, catalog, self.host.state.knowledge)
        # The callback must not replace borrowed authority during observation.
        self._check_host()
        return payload

    def receive(self, value):
        """Open native search/review only. True can mean deferred dispatch.

        Declined navigation returns False without consuming the request. Once
        queued, replay is held even if later admission fails; use a new explicit
        request after reviewing fresh authority. Deferred delivery revalidates.
        """
        request = decode_request(value)
        rename = request.correction_key() if type(request) is CanonicalRenameRequest else None
        key = (request.catalog_id, rename) if rename is not None else None
        if request.request_id in self._requests or key in self._renames:
            raise HandoffHold("duplicate_handoff_request")
        if getattr(self.host, "_dataset_preparation_shutdown", False):
            self.last_hold_reason = "dataset_shutdown"
            raise HandoffHold("dataset_shutdown")
        self._admit(request)
        self.last_hold_reason = ""
        self._requests.add(request.request_id)
        if key is not None:
            self._renames.add(key)
        invoked = False
        settled = False

        def release():
            self._requests.discard(request.request_id)
            if key is not None:
                self._renames.discard(key)

        def held(reason):
            nonlocal settled
            settled = True
            self._pending_actions.pop(dispatch, None)
            if key is not None:
                self._renames.discard(key)
            self.last_hold_reason = reason
            return False

        def dispatch():
            nonlocal invoked, settled
            if settled:
                return False
            invoked = True
            self._pending_actions.pop(dispatch, None)
            try:
                payload = self._admit(request)
            except (HandoffHold, BoundaryError):
                # Queued GUI delivery must hold safely, not raise through the
                # native event loop. Keep content-free reason for owner display.
                return held("catalog_admission_held")
            if self.host.stack.currentIndex() != 2:
                return held("dataset_navigation_held")
            if type(request) is MatchingImagesRequest:
                # Original native method queues show_tag, i.e. display-name
                # search, not an all-stable-ID evidence query/result snapshot.
                result = self.host.open_dataset_tag(payload)
            else:
                correction = getattr(self.host.dataset_page, "image_identity_correction", None)
                busy = getattr(correction, "has_active_background_work", None)
                if not callable(busy):
                    return held("correction_review_unavailable")
                if busy():
                    return held("correction_review_busy")
                result = self.host.open_dataset_function(
                    "dataset.image_facts.update_matching", payload)
                # Native opener True only means the dialog was raised. Require
                # actual scope acceptance (or the stock deferred pending scope).
                child = getattr(correction, "_child", None)
                accepted = (getattr(child or correction, "_matching_scope", None) == payload
                            or getattr(correction, "_pending_scope", None) == payload)
                if result is False or not accepted:
                    return held("correction_scope_not_accepted")
            if result is False:
                return held("dataset_dispatch_held")
            settled = True
            return True  # Native show_tag returns None on success.

        try:
            self.host.navigation.setCurrentRow(2)
            self._check_host()
            if self.host.stack.currentIndex() != 2:
                release()
                return False
            # Deferred dispatch performs the second fresh admission; a third
            # synchronous capture here would add no mutation authority.
            self._pending_actions[dispatch] = held
            result = self.host._queue_dataset_page_action(dispatch)
            if settled and not invoked:
                return False  # Synchronous native preparation failure settled it.
            if result is False and not invoked:
                self._pending_actions.pop(dispatch, None)
                release()
                self.last_hold_reason = "dataset_dispatch_held"
            return result is not False
        except Exception:
            if not invoked and not settled:
                self._pending_actions.pop(dispatch, None)
                release()
            raise


    def settle_dropped_actions(self, actions, *, reason="dataset_preparation_failed"):
        """Settle only this receiver's exact dropped callbacks, not other owners.

        Original request IDs remain consumed. Semantic correction keys are freed
        for a new explicit request; a later stale callback cannot dispatch again.
        """
        if reason not in {"dataset_preparation_failed", "dataset_shutdown"}:
            raise HandoffHold("bounded_dataset_failure_reason_required")
        for action in tuple(actions):
            settle = self._pending_actions.get(action)
            if settle is not None:
                settle(reason)


def with_encyclopedia_handoff_settlement(window_type):
    """Compose before native worker callbacks are connected; no monkey patch.

    Integration owner installs this base wrapper in its composition. Keep native
    task identity checks, error presentation and callback cleanup unchanged.
    """
    class HandoffSettlementWindow(window_type):
        def closeEvent(self, event):
            receiver = getattr(self, "_encyclopedia_handoff_receiver", None)
            pending = tuple(getattr(self, "_pending_dataset_page_actions", ()))
            try:
                return super().closeEvent(event)
            finally:
                if receiver is not None and getattr(self, "_dataset_preparation_shutdown", False):
                    remaining = tuple(getattr(self, "_pending_dataset_page_actions", ()))
                    dropped = tuple(action for action in pending
                                    if not any(action is other for other in remaining))
                    receiver.settle_dropped_actions(dropped, reason="dataset_shutdown")

        def _dataset_page_preparation_failed(self, task, message):
            if task is None or task is getattr(self, "_dataset_page_task", None):
                receiver = getattr(self, "_encyclopedia_handoff_receiver", None)
                if receiver is not None:
                    receiver.settle_dropped_actions(
                        tuple(getattr(self, "_pending_dataset_page_actions", ())),
                        reason=("dataset_shutdown" if getattr(self, "_dataset_preparation_shutdown", False)
                                else "dataset_preparation_failed"))
            return super()._dataset_page_preparation_failed(task, message)

    return HandoffSettlementWindow


def install_encyclopedia_handoff_receiver(host, *, read_admitted_catalog):
    """Explicit owner integration; installing this starts no workflow."""
    existing = getattr(host, "_encyclopedia_handoff_receiver", None)
    if existing is not None:
        if (type(existing) is not OrganizerEncyclopediaHandoffReceiver
                or existing.host is not host
                or existing.read_admitted_catalog is not read_admitted_catalog):
            raise HandoffHold("original_receiver_enrollment_required")
        existing._check_host()
        return existing
    receiver = OrganizerEncyclopediaHandoffReceiver(
        host, read_admitted_catalog=read_admitted_catalog)
    host._encyclopedia_handoff_receiver = receiver
    return receiver
