"""Inert Manager-owned diagnostics service; work starts only from Help actions."""
from concurrent.futures import Future
from dataclasses import dataclass, replace
from pathlib import Path
from threading import get_ident

from suite_core.diagnostics import Diagnostics, Component, export_preview
from suite_core.diagnostic_review_candidate import DiagnosticsReviewController, ReviewState, ReviewStatus, ReviewIssue
from .diagnostic_help_candidate import DiagnosticsHelpProvider, HelpRequest, HelpAction, safe_review
from .resource_executor import ResourceExecutorOwner


@dataclass(frozen=True)
class _FrozenOwner:
    value: object

    def preview(self):
        return self.value


class ManagerDiagnosticsService:
    """One exact request/Future; public worker lifetime governs close permission.

    No startup reader or writer. Two immutable issued previews suffice to retain
    the currently reviewed anchor while a cancelled refresh is discarded.
    No raw log fallback, catalog discovery, recorder activation or telemetry.
    """
    def __init__(self, root, build, *, executor=None, writer=export_preview, external_root=False):
        if executor is not None and type(executor) is not ResourceExecutorOwner:
            raise ValueError('manager_owned_diagnostics_executor_required')
        if not callable(writer):
            raise ValueError('verified_diagnostics_writer_required')
        if type(external_root) is not bool:
            raise ValueError("explicit_external_root_flag_required")
        self._external_root = external_root
        self.root = Path(root)
        self.build = build
        self.executor = executor if executor is not None else ResourceExecutorOwner()
        self.writer = writer
        self._owner = get_ident()
        self._request = None
        self._future = None
        self._sequence = 0
        self._issued = ()
        self._uncertain = False
        self.closed = False

    def _ui_owner(self):
        if get_ident() != self._owner:
            raise RuntimeError('diagnostics_delivery_ui_owner_required')

    @property
    def close_held(self):
        return self._uncertain or self._future is not None or self.executor.close_held

    def submit(self, request):
        self._ui_owner()
        if self.closed or self.close_held:
            return False
        if (type(request) is not HelpRequest or type(request.sequence) is not int
            or not self._sequence < request.sequence < 2**63
            or not isinstance(request.action, HelpAction) or not safe_review(request.review)):
            return False
        if request.action is HelpAction.PREVIEW:
            if request.destination is not None:
                return False
        elif (not isinstance(request.destination, Path) or not request.review.selection_confirmed
              or not request.review.selected or not any(request.review.preview is item for item in self._issued)):
            return False
        self._request = request
        self._sequence = request.sequence
        try:
            future = self.executor.submit(self._work, request)
            if type(future) is not Future:
                raise ValueError('missing_request_future')
            self._future = future
        except Exception:
            self._uncertain = True
            raise RuntimeError('diagnostics_submission_unconfirmed') from None
        return True

    def _failed(self, request):
        if request.action is HelpAction.EXPORT:
            return replace(request.review, status=ReviewStatus.FAILED, issue=ReviewIssue.EXPORT_FAILED, hashes=())
        return ReviewState(issue=ReviewIssue.PREVIEW_FAILED)

    def _work(self, request):
        # A mistaken inline executor cannot read logs or export on Qt.
        if get_ident() == self._owner:
            return self._failed(request)
        try:
            if request.action is HelpAction.PREVIEW:
                root = self.root
                if self._external_root:
                    from suite_core.installation import _path
                    root = _path(root)  # Revalidate in the Preview worker, before reader construction.
                reader = Diagnostics(root, component=Component.MANAGER, build=self.build, recording=False)
                try:
                    review = DiagnosticsReviewController(reader).refresh()
                finally:
                    reader.close()
                if safe_review(review) and review.preview is not None:
                    anchor = request.review.preview if any(request.review.preview is item for item in self._issued) else None
                    self._issued = ((anchor,) if anchor is not None else ()) + (review.preview,)
                return review
            if not any(request.review.preview is item for item in self._issued):
                return self._failed(request)
            # Refresh from these exact immutable bytes, never reread changed logs.
            review = DiagnosticsReviewController(_FrozenOwner(request.review.preview))
            review.refresh()
            review.select(request.review.selected)
            return review.export(request.destination, writer=self.writer)
        except Exception:
            return self._failed(request)

    def poll(self):
        self._ui_owner()
        if self._future is None or not self._future.done():
            return None
        request, future = self._request, self._future
        try:
            state = future.result()
        except BaseException:
            state = self._failed(request)
        self._request = None
        self._future = None
        return request, state

    def begin_close(self):
        self._ui_owner()
        self.closed = True
        self.executor.begin_close()  # queued cancellation only; running export remains owned

    def provider(self):
        return DiagnosticsHelpProvider(self.submit, self.poll, lambda: self.close_held, self.begin_close)


def create_diagnostics_help_provider(root, build, *, executor=None, writer=export_preview, external_root=False):
    """Construct an inert service; no filesystem/dependency inventory is read."""
    return ManagerDiagnosticsService(root, build, executor=executor, writer=writer, external_root=external_root).provider()
