from __future__ import annotations

from PySide6.QtCore import QObject
from PySide6.QtGui import QImage

from core import ImageEvidenceInspector,ImageRecord,TrainingCandidateSelectionService
from .image_data_editor import prepare_image_data_editor_context
from .training_candidate_plan_dialog import CandidateFixAction,CandidateFixRequest
from .workers import CancelableProgressCallTask,start_qt_delivery_task


def training_duplicate_provider(state):
    """Read only explicitly mapped named-library cache rows during planning."""
    duplicates=getattr(state,"duplicates",None)
    if duplicates is None:return None

    def provide(candidates,*,progress=None,should_cancel=None):
        from core.duplicate_finder import duplicate_scope_item
        from core.training_duplicate_evidence import build_training_duplicate_evidence
        registry=state.settings.library_registry(); identities={}
        for candidate in candidates:
            if should_cancel and should_cancel():raise InterruptedError()
            source=candidate.source_identity
            if source.get("kind")!="library_image":continue
            profile=registry.profile_for_path(candidate.image)
            if profile is None or profile.stable_id!=source.get("library_id"):continue
            item=duplicate_scope_item(
                candidate.image,source_id=profile.stable_id,source_root=profile.root,
                source_label=profile.name,source_profile_id=profile.stable_id,
            )
            if item.relative_path!=source.get("relative_image_path"):continue
            identities[candidate.stable_id]=item.identity
        return build_training_duplicate_evidence(
            candidates,index_path=duplicates.index.path,identity_by_stable_id=identities,
            decision_keys=(duplicates.decisions.snapshot_keys() if not duplicates.decisions.issues else None),
            progress=progress,should_cancel=should_cancel,
        )
    return provide


class TrainingCandidateReviewSession(QObject):
    """Return canonical edits to one live, frozen training review."""

    def __init__(self,page,dialog):
        super().__init__(dialog)
        self.page=page
        self.dialog=dialog
        self.dataset=page.dataset
        self.dataset_generation=getattr(page,"_dataset_scope_generation",0)
        self.task=None
        self.pending_ids=set()
        self._operation_owned=False
        dialog.fix_requested.connect(self.request)
        dialog.cancel_review_requested.connect(self.cancel)
        dialog.duplicate_review_requested.connect(self.review_duplicates)

    def _current(self,plan) -> bool:
        return bool(
            self.page.dataset is self.dataset
            and getattr(self.page,"_dataset_scope_generation",0)==self.dataset_generation
            and not self.page._shutdown_requested
            and (self.dialog.isVisible() or getattr(
                getattr(self.dialog,"_training_workspace_owner",None),"_review",None
            ) is self.dialog)
            and self.dialog.plan is plan
        )

    def _candidate(self,stable_id):
        return self.dialog._candidate_by_id.get(stable_id)

    def _begin(self,message):
        started,active=self.page.state.begin_long_operation("dataset",message)
        if not started:
            self.dialog.refresh_status.setText(
                "Another Dataset operation is active. Retry when it finishes."
            )
            return False
        self._operation_owned=True
        self.dialog.set_review_busy(True,message)
        return True

    def _finish(self):
        if self._operation_owned:
            self.page.state.finish_long_operation("dataset")
            self._operation_owned=False
        if self.page._training_candidate_task is self.task:
            self.page._training_candidate_task=None
        self.task=None
        self.page.cancel_projection_button.hide()
        self.dialog.set_review_busy(False)
        self.page.update_selection_controls()

    def _start(self,callable_,complete,*,on_cancelled=None):
        self.task=CancelableProgressCallTask(callable_)
        self.page._training_candidate_task=self.task
        self.task.signals.progress.connect(self._progress)
        self.task.signals.complete.connect(complete)
        self.task.signals.failed.connect(self._failed)
        self.task.signals.cancelled.connect(on_cancelled or self._cancelled)
        self.page.update_selection_controls()
        start_qt_delivery_task(self.page.pool,self.task)

    def _progress(self,index,total,name):
        self.page.state.update_long_operation("dataset",index,total,name)
        self.dialog.refresh_status.setText(
            f"Refreshing evidence: {index:,} of {total:,} • {name}"
        )

    def cancel(self):
        if self.task is not None:
            self.task.cancel()
            self.dialog.cancel_refresh_button.setEnabled(False)
            self.dialog.refresh_status.setText("Cancelling safely; keeping the previous plan…")

    def _failed(self,message):
        self._finish()
        self.dialog.require_evidence_refresh(
            f"Evidence could not be refreshed. The previous plan is retained. {message} "
            "Choose Refresh Evidence to retry."
        )

    def _cancelled(self):
        self._finish()
        self.dialog.require_evidence_refresh(
            "Refresh cancelled. Saved image corrections remain saved; the previous plan "
            "is retained. Choose Refresh Evidence before continuing."
        )

    def request(self,request):
        if not isinstance(request,CandidateFixRequest) or self.task is not None:
            return
        candidate=self._candidate(request.stable_id)
        plan=self.dialog.plan
        if candidate is None and request.action==CandidateFixAction.REFRESH and self.pending_ids:
            candidate=next((item for item in plan.candidates if item.stable_id in self.pending_ids),None)
        if candidate is None or not self._current(plan):
            return
        if request.action==CandidateFixAction.REFRESH:
            self.pending_ids.add(candidate.stable_id)
            self.refresh(candidate.stable_id)
            return
        tabs={CandidateFixAction.KNOWLEDGE:0,CandidateFixAction.DESCRIPTIONS:1,
              CandidateFixAction.CHARACTERS:2,CandidateFixAction.METADATA:4}
        if request.action not in tabs:
            return
        if not self.page._storage_scope_ready(
            (candidate.image,),write=True,action="edit Image Knowledge",
            feedback_parent=self.dialog,
        ):
            return
        if not self._begin(f"Opening Image Knowledge for {candidate.image.name}…"):
            return

        def load(_progress,cancelled):
            if cancelled():raise InterruptedError()
            record=ImageRecord(candidate.image)
            report=ImageEvidenceInspector(knowledge=self.page.state.knowledge).inspect(candidate.image)
            preview=QImage(str(candidate.image))
            context=prepare_image_data_editor_context(self.page.state.knowledge,self.page.characters)
            if cancelled():raise InterruptedError()
            return record,report,preview,context

        def loaded(result):
            cancelled=self.task is not None and self.task.is_cancelled()
            self._finish()
            if cancelled:
                return
            if not self._current(plan):
                self._failed("The library or training plan changed while the editor opened.")
                return
            if not self.page._storage_scope_ready(
                (candidate.image,),write=True,action="edit Image Knowledge",
                feedback_parent=self.dialog,
            ):
                return
            self.pending_ids.add(request.stable_id)
            self.dialog.require_evidence_refresh("Return from the image editor to refresh this plan's evidence.")
            self.dialog.set_review_busy(True,"Editing this image; the frozen plan and draft remain open.")
            self.dialog.cancel_refresh_button.hide()
            try:
                self.page._run_data_editor_dialog(
                    *result,review_parent=self.dialog,initial_tab_index=tabs[request.action],
                )
            except Exception as exc:
                self._failed(str(exc))
                return
            self.dialog.set_review_busy(False)
            if self._current(plan):
                self.refresh(request.stable_id)
            else:
                self._failed("The library changed during image review. Reopen the saved draft in its original library.")

        self._start(load,loaded)

    def refresh(self,return_id):
        plan=self.dialog.plan
        self.dialog.require_evidence_refresh("Refreshing changed image evidence before package preparation…")
        if not self._begin("Refreshing training evidence in the background…"):
            return
        targets=tuple(candidate.stable_id for candidate in plan.candidates
                      if candidate.stable_id in self.pending_ids)
        service=TrainingCandidateSelectionService(
            self.page.state.knowledge,self.page.characters,self.page.categories,
            duplicate_evidence_provider=training_duplicate_provider(self.page.state),
        )

        def refresh_plan(progress,cancelled):
            if cancelled():raise InterruptedError()
            # A shared knowledge change widens only the reassessment work, never
            # the user's frozen selection. Inspect storage authority before reads.
            # Validate the complete cached authority scope: dependencies may
            # change between this check and the service's own context snapshot.
            # This does not read or decode unchanged source files.
            for candidate in plan.candidates:
                if cancelled():raise InterruptedError()
                if not self.page._storage_readable(candidate.image):
                    raise ValueError("A selected source is not locally readable. Restore its availability and retry.")
                if candidate.source_identity.get("kind")=="library_image":
                    identity=self.dataset.identity_for_path(candidate.image)
                    if identity is None or identity.key!=candidate.stable_id:
                        raise ValueError("A selected image's library identity changed. Reopen the exact saved draft.")
            return service.refresh_candidates(
                plan,targets,expected_plan_fingerprint=plan.to_manifest_dict()["plan_fingerprint"],
                progress=progress,should_cancel=cancelled,
            )

        def refreshed(result):
            cancelled=self.task is not None and self.task.is_cancelled()
            self._finish()
            if cancelled:
                self._cancelled()
                return
            if not self._current(plan):
                self._failed("The library or training plan changed during refresh.")
                return
            if result.assessment_context_fingerprint!=service.assessment_context_fingerprint():
                self._failed("Shared knowledge changed after refresh. Retry to reassess the frozen selection.")
                return
            self.pending_ids.difference_update(targets)
            self.dialog.adopt_refreshed_plan(result,return_id)
            self.page._training_candidate_plan=result

        self._start(refresh_plan,refreshed)

    def review_duplicates(self):
        from core.duplicate_finder import DuplicateScope,duplicate_scope_item
        from .duplicate_finder_dialog import DuplicateFinderDialog
        plan=self.dialog.plan
        if self.task is not None or self.dialog._refresh_required or not self._current(plan) or len(plan.candidates)<2:
            return
        if not self._begin("Preparing the exact training comparison scope…"):return
        return_id=(self.dialog._selected_candidate().stable_id
                   if self.dialog._selected_candidate() is not None else plan.candidates[0].stable_id)

        def scope_value(progress,cancelled):
            registry=self.page.settings.library_registry(); items=[]
            for candidate in plan.candidates:
                if cancelled():raise InterruptedError()
                if not self.page._storage_readable(candidate.image):
                    raise ValueError("A frozen candidate is unavailable. Restore it before comparing this plan.")
                identity=self.dataset.identity_for_path(candidate.image)
                if identity is None or identity.key!=candidate.stable_id:
                    raise ValueError("A frozen image identity changed. Reopen the exact training draft.")
                profile=registry.profile_for_path(candidate.image)
                if profile is None or profile.stable_id!=candidate.source_identity.get("library_id"):
                    raise ValueError("The training image's owning library changed.")
                item=duplicate_scope_item(candidate.image,source_id=profile.stable_id,
                                          source_root=profile.root,source_label=profile.name,
                                          source_profile_id=profile.stable_id)
                items.append(item)
            if len({item.identity for item in items})!=len(items):
                raise ValueError("Comparison identities do not match the exact training scope.")
            return DuplicateScope("training:"+plan.scope_fingerprint,
                                  f"Frozen training plan: {len(items):,} images",tuple(items))

        def ready(scope):
            cancelled=self.task is not None and self.task.is_cancelled()
            self._finish()
            if cancelled:return
            if not self._current(plan):
                self._failed("The training plan or library changed before comparison."); return
            self.dialog.set_review_busy(True,"Comparing this frozen training scope; return here to continue.")
            self.dialog.cancel_refresh_button.hide()
            comparison=DuplicateFinderDialog(self.page.state,self.dialog,frozen_scope=scope)
            comparison.exec()
            self.dialog.set_review_busy(False)
            if not self._current(plan):
                self._failed("The training plan or library changed during comparison."); return
            if comparison.report is None:
                self.dialog.refresh_status.setText("Comparison closed; the current training plan is unchanged.")
                return
            self.pending_ids.update(candidate.stable_id for candidate in plan.candidates)
            self.refresh(return_id)

        def cancelled_scope():
            self._finish()
            self.dialog.refresh_status.setText("Comparison setup cancelled; the current training plan is unchanged.")

        self._start(scope_value,ready,on_cancelled=cancelled_scope)
