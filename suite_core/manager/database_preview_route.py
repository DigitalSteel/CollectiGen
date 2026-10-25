"""Host-owned serialized preview admission; no Qt, writer, discovery or executor creation.

The host transfers preview/cancel/release-observation mutation ownership here.
It supplies an executor whose submit is nonblocking and executes away from the UI.
Direct external set_releases/preview/cancel while owned violates that contract.
Selected-session changes are also independently checked through public binding.
"""
from concurrent.futures import Future
from dataclasses import dataclass
from threading import Event, get_ident
from uuid import uuid4

from suite_core.database_actions import DatabaseAction, DatabaseActionsController, DatabaseReview, DatabasePreviewStatus
from suite_core.knowledge.catalog_migration_preview import Assignment, RefinementOffer
from suite_core.selected_catalog import CatalogBinding


@dataclass(frozen=True)
class DatabasePreviewInputs:
    controller: DatabaseActionsController
    assignments: tuple = ()
    offers: tuple = ()

    def __post_init__(self):
        if (type(self.controller) is not DatabaseActionsController
                or type(self.assignments) is not tuple or type(self.offers) is not tuple
                or any(type(row) is not Assignment or any(type(value) is not str for value in
                    (row.occurrence_id, row.tag_id, row.source_value, row.evidence_ref)) for row in self.assignments)
                or any(type(row) is not RefinementOffer or type(row.source_id) is not str
                    or type(row.relationship_ref) is not str for row in self.offers)):
            raise ValueError("Explicit immutable approved preview inputs required")


@dataclass(frozen=True)
class PreviewTicket:
    route_id: str
    sequence: int
    generation: int
    binding: CatalogBinding
    action: DatabaseAction


@dataclass(frozen=True)
class PreviewCompletion:
    ticket: PreviewTicket
    review: DatabaseReview | None
    error: str | None = None


def _compute_preview(inputs, ticket, cancelled, ui_owner):
    # A host accidentally passing an inline executor must never calculate on Qt.
    if get_ident() == ui_owner:
        return PreviewCompletion(ticket, None, "Executor did not use a background owner; preview unavailable.")
    if cancelled.is_set() or inputs.controller.session.binding != ticket.binding:
        return PreviewCompletion(ticket, None)
    try:
        review = inputs.controller.preview(ticket.action, assignments=inputs.assignments,
            offers=inputs.offers, should_continue=lambda: not cancelled.is_set())
        return PreviewCompletion(ticket, review)
    except Exception:
        return PreviewCompletion(ticket, None, "Preview inputs could not be reviewed; no catalog or image changed.")


class DatabasePreviewRoute:
    """UI-owner facade with one outstanding Future; host retains executor lifetime.

    Cancellation revokes result admission immediately, but no retry is enabled
    until the outstanding Future acknowledges completion. No future callbacks,
    second event loop, UI join, executor shutdown or forced worker termination.
    Closing is irreversible; Cancel Close may retain an inert route visibly.
    """
    def __init__(self, *, inputs=(), executor=None):
        if (type(inputs) is not tuple or len(inputs) > 32
                or any(type(item) is not DatabasePreviewInputs for item in inputs)
                or len({id(item.controller) for item in inputs}) != len(inputs)
                or executor is not None and not callable(getattr(executor, "submit", None))):
            raise ValueError("Explicit unique approved inputs and host executor required")
        self.inputs = inputs
        self.controller = inputs[0].controller if inputs else None
        self.route_id = uuid4().hex
        self._executor = executor
        self._ui_owner = get_ident()
        self._sequence = 0
        self._generation = 0
        self._future = None
        self._ticket = None
        self._cancelled = None
        self._request_controller = None
        self.review = None
        self.closed = False
        self.faulted = False
        self._unconfirmed = False
        self.reason = "No preview requested. Catalog and original image assignments are unchanged."

    def _owner(self):
        if get_ident() != self._ui_owner:
            raise RuntimeError("Preview route admission/delivery belongs to its UI owner")

    @property
    def pending(self):
        return self._future is not None

    @property
    def quiescent(self):
        return not self.close_held

    @property
    def can_request(self):
        return self._executor is not None and self.controller is not None and not self.pending and not self.closed and not self.faulted

    @property
    def availability_reason(self):
        if self._unconfirmed: return "Executor supplied no completion proof. Preview requests and Manager close remain held for host supervision."
        if self.closed: return "Preview route stopped for close. Reopen Manager with the host route to request another preview."
        if self.faulted: return "Preview executor contract failed; reopen with a working host-owned background executor."
        if self.controller is None: return "No approved selected catalog supplied."
        if self._executor is None: return "Host-owned background executor not supplied; preview requests unavailable."
        if self.pending: return "Preview is finishing. Cancel discards the result; retry is available after completion."
        return "Preview only. Install/merge actions calculate a detached review; no changes are applied."

    def request(self, action):
        self._owner()
        if not self.can_request: return False
        action = DatabaseAction(action)
        inputs = next(item for item in self.inputs if item.controller is self.controller)
        self._sequence += 1
        ticket = PreviewTicket(self.route_id, self._sequence, self._generation, self.controller.session.binding, action)
        cancelled = Event()
        self.review = None
        self._ticket = ticket
        self._cancelled = cancelled
        self._request_controller = self.controller
        try:
            future = self._executor.submit(_compute_preview, inputs, ticket, cancelled, self._ui_owner)
        except Exception:
            # submit may have started work before raising: no retry/close proof.
            cancelled.set()
            self.faulted = True
            self._unconfirmed = True
            self.reason = "Executor submission failed; work completion is unknown. Ownership remains held."
            return False
        if not isinstance(future, Future):
            cancelled.set()
            self.faulted = True
            self._unconfirmed = True
            self.reason = "Executor returned no completion handle; work completion is unknown. Ownership remains held."
            return False
        self._future = future
        self.reason = "Preview pending. Catalog and original image assignments remain unchanged."
        return True

    def invalidate(self, controller=None):
        """Host calls before changing observations/selection; admission revoked now."""
        self._owner()
        controller = self.controller if controller is None else controller
        if controller is not None and not any(item.controller is controller for item in self.inputs):
            raise ValueError("Unknown controller")
        self._generation += 1
        if self._cancelled is not None: self._cancelled.set()
        # Cancel both the prior job scope and the current selected scope.
        for candidate in (self._request_controller, controller):
            if candidate is not None: candidate.cancel()
        self.review = None
        self.reason = "Preview discarded. Original catalog and image assignments are unchanged."

    def select(self, controller):
        self._owner()
        if not any(item.controller is controller for item in self.inputs): raise ValueError("Unknown controller")
        self.invalidate()
        self.controller = controller

    def replace_observations(self, controller, *, base_release, available_release, installed=True):
        """Only approved supplied observations; actual controller owns validation."""
        self.invalidate(controller)
        controller.set_releases(base_release=base_release, available_release=available_release, installed=installed)

    def poll(self):
        """Existing UI tick only: never block, compute, or dispatch automatically."""
        self._owner()
        if self.review is not None and not self.controller.accepts(self.review):
            self.review = None
            self.reason = "Review is out of date. Request a fresh scoped preview."
        if not self.pending or not self._future.done(): return False
        future, ticket, cancelled, controller = self._future, self._ticket, self._cancelled, self._request_controller
        self._future = None
        try:
            result = future.result()  # done already; never a UI wait
        except Exception:
            self.reason = "Preview failed or was cancelled. No catalog or image assignment was changed."
            return False
        if (self.closed or cancelled.is_set() or ticket.generation != self._generation
                or controller is not self.controller or controller.session.binding != ticket.binding):
            self.reason = "Late or discarded preview ignored. Request a fresh preview after completion."
            return False
        if type(result) is not PreviewCompletion or result.ticket != ticket:
            self.reason = "Completion belongs to another request and was ignored."
            return False
        if result.error is not None:
            self.reason = result.error
            self.faulted = True
            return False
        review = result.review
        if (type(review) is not DatabaseReview or review.action != ticket.action or review.binding != ticket.binding
                or review.status in (DatabasePreviewStatus.STALE, DatabasePreviewStatus.CANCELLED)
                or not controller.accepts(review)):
            self.reason = "Preview is stale or cancelled. Request a fresh scoped preview."
            return False
        self.review = review
        self.reason = "Supplied background preview ready for read-only review. Apply remains unavailable."
        return True

    def begin_close(self):
        self._owner()
        if not self.closed:
            self.invalidate()
            self.closed = True

    @property
    def close_held(self):
        # A submission exception/invalid Future supplies no completion proof.
        return self.pending or self._unconfirmed
