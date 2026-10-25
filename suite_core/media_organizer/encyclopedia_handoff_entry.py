"""Receiver-only GUI-thread seam; no Qt import, scan, save or transport.

The integration owner explicitly supplies fresh canonical catalog admission.
Source components do not connect a browser or qualify visible/native acceptance.
"""
from dataclasses import dataclass
import math
import re
import time

from core.organizer_encyclopedia_handoffs import (
    AdmittedCatalog, CanonicalRenameRequest, HandoffHold, MatchingImagesRequest,
    SearchReceiptStage, admit_request, decode_request,
    _text,
)

from .contracts import BoundaryError


@dataclass(frozen=True)
class HostedHandoffReceipt:
    request_id: str
    source_generation: int
    stage: SearchReceiptStage
    reason: str

    def __post_init__(self):
        _text(self.request_id)
        if (type(self.source_generation) is not int or self.source_generation < 0
                or type(self.stage) is not SearchReceiptStage or type(self.reason) is not str
                or not re.fullmatch(r"[a-z][a-z0-9_]{0,127}",self.reason)):
            raise HandoffHold("exact_hosted_settlement_receipt_required")


@dataclass
class _TrackedSearch:
    request: MatchingImagesRequest | CanonicalRenameRequest
    scope: object
    current_generation: object
    observer: object
    deadline: float
    receipt: HostedHandoffReceipt


class OrganizerEncyclopediaHandoffReceiver:
    """One admitted host's in-memory, at-most-once dispatch receiver.

    `read_admitted_catalog(state)` must read existing native admission, without
    initializing catalogs or using incoming descriptors as alias authority. Unknown
    admission holds the request. Request IDs/correction suggestions are retained for
    this receiver lifetime only; there is no durable cross-process replay claim.
    """
    def __init__(self, host, *, read_admitted_catalog, read_recipient_scope=None, clock=time.monotonic):
        if not callable(read_admitted_catalog):
            raise HandoffHold("native_admission_reader_required")
        self.host = host
        self.read_admitted_catalog = read_admitted_catalog
        self.last_hold_reason = ""
        self._requests = set()
        self._renames = set()
        self._pending_actions = {}
        self._pending_corrections = {}
        if read_recipient_scope is not None and not callable(read_recipient_scope):
            raise HandoffHold("original_scope_reader_required")
        if not callable(clock):
            raise HandoffHold("original_hosted_clock_required")
        self._read_recipient_scope = read_recipient_scope
        self._clock = clock
        self._last_tracking_time = None
        self._tracked_searches = {}
        self._tracked_settlers = {}
        self.settlement_callback_failures = 0
        self._check_host()
        self._knowledge = host.state.knowledge

    def _tracking_time(self):
        value = self._clock()
        if (type(value) not in (int, float) or not math.isfinite(value) or value < 0
                or self._last_tracking_time is not None and value < self._last_tracking_time):
            raise HandoffHold("hosted_receipt_clock_held")
        self._last_tracking_time = value
        return value

    @staticmethod
    def _source_generation(tracked):
        value = tracked.current_generation()
        if type(value) is not int:
            raise HandoffHold("exact_sender_generation_required")
        return value

    def prepare_recipient_scope(self):
        """GUI thread, before sender preparation; no profile/library selection."""
        self._check_host()
        if self._read_recipient_scope is None:
            from core.organizer_handoff_scope import OrganizerHostedScopeReader
            self._read_recipient_scope = OrganizerHostedScopeReader(self.host)
        return self._read_recipient_scope()

    def prepare_scan_observations(self,*,catalog_id,catalog_revision):
        from core.organizer_scan_observation_join import prepare_retained_scan_join
        self._require_scan_observation_lifetime()
        self.prepare_recipient_scope()
        result=prepare_retained_scan_join(self,hosted_reader=self._read_recipient_scope,
            expected_catalog_id=catalog_id,expected_catalog_revision=catalog_revision)
        self._require_scan_observation_lifetime()
        return result

    def use_scan_observations(self,prepared):
        from core.organizer_scan_observation_join import revalidate_scan_join
        self._require_scan_observation_lifetime()
        self.prepare_recipient_scope()
        result=revalidate_scan_join(self,prepared,hosted_reader=self._read_recipient_scope)
        self._require_scan_observation_lifetime()
        return result

    def _require_scan_observation_lifetime(self):
        if (getattr(self.host,"_dataset_preparation_shutdown",False)
                or getattr(self.host,"_close_after_scan",False)
                or getattr(self.host,"startup_cancelled",False)):
            raise HandoffHold("dataset_shutdown")

    def receive_tracked_search(self, value, *, prepared_scope, source_generation,
                               current_generation, on_settled, lifetime=30.0):
        request = decode_request(value)
        if type(request) is not MatchingImagesRequest:
            raise HandoffHold("tracked_hosted_search_v1_only")
        return self.receive_tracked_handoff(request, prepared_scope=prepared_scope,
            source_generation=source_generation, current_generation=current_generation,
            on_settled=on_settled, lifetime=lifetime)

    def receive_tracked_handoff(self, value, *, prepared_scope, source_generation,
                                current_generation, on_settled, lifetime=30.0):
        """Track the existing in-process search/review routes; no endpoint.

        Repeated identical request is receipt lookup, never a native redispatch.
        Receipts/tombstones are bounded for this receiver lifetime. Pending context
        and observers are released on settlement; no persistent replay claim.
        """
        from core.organizer_handoff_scope import require_hosted_scope
        request = decode_request(value)
        if (type(source_generation) is not int or source_generation < 0
                or not callable(current_generation) or not callable(on_settled)
                or type(lifetime) not in (int, float) or not math.isfinite(lifetime)
                or not 0 < lifetime <= 60):
            raise HandoffHold("bounded_hosted_request_tracking_required")
        existing = self._tracked_searches.get(request.request_id)
        if existing is not None:
            if (existing.request != request or existing.receipt.source_generation != source_generation):
                raise HandoffHold("duplicate_request_content_conflict")
            self.poll_settlements()
            return existing.receipt
        if len(self._tracked_searches) >= 512:
            raise HandoffHold("hosted_receipt_capacity_held")
        require_hosted_scope(prepared_scope, self.prepare_recipient_scope())
        current = current_generation()
        if type(current) is not int or current != source_generation:
            raise HandoffHold("stale_sender_generation")
        receipt = HostedHandoffReceipt(request.request_id, source_generation, SearchReceiptStage.QUEUED,
            "awaiting_native_search" if type(request) is MatchingImagesRequest else "awaiting_native_review")
        tracked = _TrackedSearch(request, prepared_scope, current_generation, on_settled,
                                 self._tracking_time() + lifetime, receipt)
        self._tracked_searches[request.request_id] = tracked
        try:
            accepted = self.receive(request)
            if not accepted and tracked.receipt.stage is SearchReceiptStage.QUEUED:
                self._settle_tracked(tracked, SearchReceiptStage.HELD, "dataset_navigation_held")
        except Exception:
            if tracked.receipt.stage is SearchReceiptStage.QUEUED:
                self._settle_tracked(tracked, SearchReceiptStage.HELD, "catalog_admission_held")
            raise
        return tracked.receipt

    def _settle_tracked(self, tracked, stage, reason):
        if tracked.receipt.stage is not SearchReceiptStage.QUEUED:
            return tracked.receipt
        tracked.receipt = HostedHandoffReceipt(tracked.request.request_id,
            tracked.receipt.source_generation, stage, reason)
        self._tracked_settlers.pop(tracked.request.request_id, None)
        observer = tracked.observer
        tracked.scope = tracked.current_generation = tracked.observer = None
        if observer is not None:
            try:
                observer(tracked.receipt)
            except Exception:
                self.settlement_callback_failures += 1  # No raw failure content.
        return tracked.receipt

    def poll_settlements(self):
        """Future sender may call from its original GUI timer; no retries."""
        now = self._tracking_time()
        for tracked in tuple(self._tracked_searches.values()):
            if tracked.receipt.stage is SearchReceiptStage.QUEUED:
                if now >= tracked.deadline:
                    self._cancel_tracked(tracked, SearchReceiptStage.EXPIRED, "request_expired")
                else:
                    try:
                        current = self._source_generation(tracked)
                    except Exception:
                        self._cancel_tracked(tracked, SearchReceiptStage.UNKNOWN_OUTCOME, "sender_unavailable_no_resend")
                    else:
                        if current != tracked.receipt.source_generation:
                            self._cancel_tracked(tracked, SearchReceiptStage.HELD, "stale_sender_generation")

    def receipt(self, request_id, source_generation):
        if type(source_generation) is not int:
            raise HandoffHold("exact_sender_generation_required")
        self.poll_settlements()
        tracked = self._tracked_searches.get(request_id)
        if tracked is None or tracked.receipt.source_generation != source_generation:
            return None
        return tracked.receipt

    def _cancel_tracked(self, tracked, stage, reason):
        settle = self._tracked_settlers.get(tracked.request.request_id)
        if settle is not None:
            settle(reason, stage=stage)
        return self._settle_tracked(tracked, stage, reason)

    def cancel_tracked(self, request_id, source_generation):
        if type(source_generation) is not int:
            raise HandoffHold("exact_sender_generation_required")
        tracked = self._tracked_searches.get(request_id)
        if tracked is None or tracked.receipt.source_generation != source_generation:
            return None
        return self._cancel_tracked(tracked, SearchReceiptStage.CANCELLED, "explicit_request_cancelled")

    def sender_unavailable(self, request_id, source_generation):
        if type(source_generation) is not int:
            raise HandoffHold("exact_sender_generation_required")
        tracked = self._tracked_searches.get(request_id)
        if tracked is None or tracked.receipt.source_generation != source_generation:
            return None
        return self._cancel_tracked(tracked, SearchReceiptStage.UNKNOWN_OUTCOME, "sender_unavailable_no_resend")

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
        tracked = self._tracked_searches.get(request.request_id)
        if tracked is not None and tracked.request != request:
            raise HandoffHold("duplicate_request_content_conflict")
        if tracked is not None and tracked.receipt.stage is not SearchReceiptStage.QUEUED:
            return False
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
        consuming = False
        settled = False
        delivered = False
        correction_wait = None
        expected_correction = None

        def clear_correction_wait():
            nonlocal correction_wait
            self._pending_corrections.pop(dispatch, None)
            if correction_wait is not None:
                signal, callback = correction_wait
                correction_wait = None
                try:
                    signal.disconnect(callback)
                except (RuntimeError, TypeError):
                    pass  # The original Qt owner may already be destroyed.

        def release():
            self._requests.discard(request.request_id)
            if key is not None:
                self._renames.discard(key)

        def held(reason, *, stage=SearchReceiptStage.HELD):
            nonlocal settled
            if settled:
                return False
            if consuming:
                # Once the native opener has begun, any interruption can race
                # visible consumption. Expiry/generation drift do not prove
                # that nothing opened, just as reentrant Cancel cannot.
                stage, reason = SearchReceiptStage.UNKNOWN_OUTCOME, "native_dispatch_outcome_unknown"
            settled = True
            self._pending_actions.pop(dispatch, None)
            clear_correction_wait()
            if key is not None:
                self._renames.discard(key)
            self.last_hold_reason = reason
            if tracked is not None:
                self._settle_tracked(tracked, stage, reason)
            return False

        def tracked_ready():
            if tracked is None:
                return True
            if settled or tracked.receipt.stage is not SearchReceiptStage.QUEUED:
                return False
            try:
                if self._tracking_time() >= tracked.deadline:
                    return held("request_expired", stage=SearchReceiptStage.EXPIRED)
                if self._source_generation(tracked) != tracked.receipt.source_generation:
                    return held("stale_sender_generation")
            except Exception:
                return held("sender_unavailable_no_resend", stage=SearchReceiptStage.UNKNOWN_OUTCOME)
            from core.organizer_handoff_scope import require_hosted_scope
            try:
                require_hosted_scope(tracked.scope, self.prepare_recipient_scope())
            except (HandoffHold, BoundaryError):
                return held("recipient_scope_changed")
            except Exception:
                return held("recipient_scope_unavailable")
            return not settled and tracked.receipt.stage is SearchReceiptStage.QUEUED

        def dispatch():
            nonlocal invoked, consuming, settled, delivered, correction_wait, expected_correction
            if settled:
                return False
            invoked = True
            self._pending_actions.pop(dispatch, None)
            if not tracked_ready():
                return False
            try:
                payload = self._admit(request)
            except (HandoffHold, BoundaryError):
                # Queued GUI delivery must hold safely, not raise through the
                # native event loop. Keep content-free reason for owner display.
                return held("catalog_admission_held")
            if getattr(self.host, "_dataset_preparation_shutdown", False):
                return held("dataset_shutdown")
            if self.host.stack.currentIndex() != 2:
                return held("dataset_navigation_held")
            if type(request) is MatchingImagesRequest:
                # Original native method queues show_tag, i.e. display-name
                # search, not an all-stable-ID evidence query/result snapshot.
                if tracked is None:
                    result = self.host.open_dataset_tag(payload)
                else:
                    # We are consuming the original Dataset queue on its GUI
                    # thread. Do not call the text-only opener, whose navigation
                    # and second queued lambda could escape this exact scope.
                    # Reuse its stock show_tag operation after the final guard.
                    if not tracked_ready():
                        return False
                    try:
                        consuming = True
                        result = self.host.dataset_page.show_tag(payload)
                    except Exception:
                        return held("native_dispatch_outcome_unknown", stage=SearchReceiptStage.UNKNOWN_OUTCOME)
                    finally:
                        consuming = False
            else:
                correction = getattr(self.host.dataset_page, "image_identity_correction", None)
                if expected_correction is not None and correction is not expected_correction:
                    return held("correction_review_owner_changed")
                busy = getattr(correction, "has_active_background_work", None)
                if not callable(busy):
                    return held("correction_review_unavailable")
                if busy():
                    return held("correction_review_busy")
                if getattr(correction, "prepared", True) is False:
                    # Prepare the stock lazy widget without giving it a payload
                    # that could survive failed preparation and a later retry.
                    if getattr(correction, "_pending_scope", None) is not None:
                        return held("correction_review_busy")
                    signal = getattr(correction, "background_work_finished", None)
                    if not all(callable(getattr(signal, name, None)) for name in ("connect", "disconnect")):
                        return held("correction_review_unavailable")
                    expected_correction = correction

                    def prepared():
                        if settled or not tracked_ready():
                            return False
                        if getattr(correction, "prepared", False) is not True:
                            return held("correction_preparation_failed")
                        clear_correction_wait()
                        # Re-enter admission immediately before the actual child
                        # consumes the scope, including owner/navigation checks.
                        try:
                            return dispatch()
                        except Exception:
                            return held("correction_review_unavailable")

                    correction_wait = (signal, prepared)
                    self._pending_corrections[dispatch] = held
                    try:
                        signal.connect(prepared)
                        if not tracked_ready():
                            return False
                        result = self.host.dataset_page.open_function(
                            "dataset.image_facts.update_matching", None)
                    except Exception:
                        return held("correction_preparation_failed")
                    if not settled and (result is False or
                            not busy() and getattr(correction, "prepared", False) is not True):
                        return held("correction_preparation_failed")
                    return not settled or delivered
                if tracked is None:
                    result = self.host.open_dataset_function("dataset.image_facts.update_matching", payload)
                else:
                    if not tracked_ready():
                        return False
                    from copy import deepcopy
                    try:
                        consuming = True
                        result = self.host.dataset_page.open_function(
                            "dataset.image_facts.update_matching", deepcopy(payload))
                    except Exception:
                        return held("native_dispatch_outcome_unknown", stage=SearchReceiptStage.UNKNOWN_OUTCOME)
                    finally:
                        consuming = False
                # Native opener True only means the dialog was raised. Require
                # actual child scope acceptance, never a deferred pending scope.
                child = getattr(correction, "_child", None)
                accepted = getattr(child or correction, "_matching_scope", None) == payload
                if result is False or not accepted:
                    return held("correction_scope_not_accepted")
            if result is False:
                return held("dataset_dispatch_held")
            if settled:
                return False
            delivered = True
            settled = True
            clear_correction_wait()
            if tracked is not None:
                self._settle_tracked(tracked, SearchReceiptStage.DELIVERED,
                    "native_search_opened" if type(request) is MatchingImagesRequest else "native_review_opened")
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
            if tracked is not None:
                self._tracked_settlers[request.request_id] = held
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

    def settle_pending_corrections(self):
        """Original host shutdown also retires already-open lazy preparation."""
        for settle in tuple(self._pending_corrections.values()):
            settle("dataset_shutdown")


def with_encyclopedia_handoff_settlement(window_type):
    """Compose before native worker callbacks are connected; no monkey patch.

    Integration owner installs this base wrapper in its composition. Keep native
    task identity checks, error presentation and callback cleanup unchanged.
    """
    class HandoffSettlementWindow(window_type):
        def prepare_encyclopedia_scan_observations(self,*,catalog_id,catalog_revision):
            receiver=getattr(self,"_encyclopedia_handoff_receiver",None)
            if receiver is None:
                raise HandoffHold("original_handoff_receiver_unavailable")
            return receiver.prepare_scan_observations(catalog_id=catalog_id,
                catalog_revision=catalog_revision)

        def use_encyclopedia_scan_observations(self,prepared):
            receiver=getattr(self,"_encyclopedia_handoff_receiver",None)
            if receiver is None:
                raise HandoffHold("original_handoff_receiver_unavailable")
            return receiver.use_scan_observations(prepared)

        def closeEvent(self, event):
            receiver = getattr(self, "_encyclopedia_handoff_receiver", None)
            pending = tuple(getattr(self, "_pending_dataset_page_actions", ()))
            try:
                return super().closeEvent(event)
            finally:
                if receiver is not None and getattr(self, "_dataset_preparation_shutdown", False):
                    receiver.settle_pending_corrections()
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
