from __future__ import annotations

from dataclasses import dataclass,replace
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QTimer,Qt
from PySide6.QtWidgets import (
    QCheckBox,QDialog,QDialogButtonBox,QFrame,QGridLayout,QGroupBox,QHBoxLayout,
    QLabel,QProgressBar,QPushButton,QSpinBox,QVBoxLayout,QWidget,
)
from core import DEFAULT_SIMILARITY_PERCENT

from core.duplicate_preflight import (
    DuplicatePreflightLibrarySource,DuplicatePreflightScopePlan,
    PreparedDuplicatePreflightScope,build_duplicate_preflight_scope,
)

from .components import bind_status_footer,compact_button,set_progress_bar
from .duplicate_finder_dialog import DuplicateFinderDialog
from .modal_feedback import FeedbackEnabledDialog
from .workers import CoordinatedCallTask


def _counted(count: int,singular: str,plural: str|None=None) -> str:
    return f"{count:,} {singular if count==1 else plural or singular+'s'}"


def _gallery_duplicate_options(parent=None) -> tuple[bool,float,float]|None:
    dialog=QDialog(parent if isinstance(parent,QWidget) else None)
    dialog.setWindowTitle("Check Gallery Images for Duplicates")
    dialog.resize(620,330); dialog.setMinimumWidth(560)
    root=QVBoxLayout(dialog)
    heading=QLabel("Choose duplicate evidence")
    heading.setObjectName("pageTitle"); root.addWidget(heading)
    exact=QLabel(
        "The default check finds byte-identical files and images with identical "
        "decoded pixels. It changes no image or review decision."
    )
    exact.setWordWrap(True); root.addWidget(exact)
    similar=QCheckBox("Also include visually similar candidates")
    similar.setChecked(False); root.addWidget(similar)
    group=QGroupBox("Inclusive similarity range")
    grid=QGridLayout(group)
    lower=QSpinBox(); lower.setRange(70,100); lower.setValue(DEFAULT_SIMILARITY_PERCENT)
    lower.setSuffix("% minimum"); lower.setAccessibleName("Minimum similarity")
    upper=QSpinBox(); upper.setRange(70,100); upper.setValue(100)
    upper.setSuffix("% maximum"); upper.setAccessibleName("Maximum similarity")
    lower.valueChanged.connect(lambda value:upper.setMinimum(int(value)))
    upper.valueChanged.connect(lambda value:lower.setMaximum(int(value)))
    grid.addWidget(lower,0,0); grid.addWidget(upper,0,1)
    explanation=QLabel(
        "Visual candidates are review suggestions. The endpoints are included, "
        "and a wider range can take longer."
    )
    explanation.setWordWrap(True); grid.addWidget(explanation,1,0,1,2)
    group.setEnabled(False); similar.toggled.connect(group.setEnabled)
    root.addWidget(group)
    buttons=QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel)
    start=buttons.button(QDialogButtonBox.Ok)
    start.setText("Check for Duplicates")
    buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
    root.addWidget(buttons)
    dialog.include_similar_check=similar
    dialog.lower_similarity=lower; dialog.upper_similarity=upper
    try:
        if dialog.exec()!=QDialog.Accepted:
            return None
        return similar.isChecked(),float(lower.value()),float(upper.value())
    finally:
        dialog.deleteLater()


@dataclass(frozen=True)
class DuplicatePreflightContext:
    state: object
    plan: DuplicatePreflightScopePlan
    is_current: Callable[[],bool]
    is_scope_current: Callable[[PreparedDuplicatePreflightScope],bool]
    owner: object=None

def duplicate_preflight_context(page,impact_context) -> DuplicatePreflightContext:
    """Freeze lightweight Ingest/library owners without walking their images."""
    state=page.state; dataset=state.dataset
    ingest_value=str(getattr(page.settings,"ingest_directory","") or "").strip()
    ingest_root=(
        Path(ingest_value).expanduser().resolve()
        if ingest_value else Path.cwd().resolve()
    )
    registry=page.settings.library_registry()
    explicit_ids=tuple(dict.fromkeys(
        str(value) for value in getattr(page.settings,"library_scope_ids",())
        if str(value)
    ))
    included_ids=explicit_ids or tuple(
        profile.stable_id for profile in registry.profiles
    )
    libraries=[]; library_tokens=[]
    for stable_id in included_ids:
        profile=registry.get(stable_id)
        if profile is None:
            continue
        _profile,manager=dataset.profile_manager(stable_id)
        inventory=getattr(manager,"images",()) if manager is not None else ()
        availability=(
            str(dataset.library_availability(stable_id) or "not_loaded")
            if manager is not None else "not_loaded"
        )
        libraries.append(DuplicatePreflightLibrarySource(
            profile.stable_id,profile.name,Path(profile.root).expanduser(),
            availability,inventory,
        ))
        library_tokens.append((
            profile.stable_id,profile.name,Path(profile.root).expanduser(),
            manager,availability,id(inventory),len(inventory),
        ))
    libraries=tuple(libraries)
    library_tokens=tuple(library_tokens)

    def current() -> bool:
        if not impact_context.is_current() or state.dataset is not dataset:
            return False
        current_explicit=tuple(dict.fromkeys(
            str(value) for value in getattr(page.settings,"library_scope_ids",())
            if str(value)
        ))
        if current_explicit!=explicit_ids:
            return False
        current_registry=page.settings.library_registry()
        current_included=current_explicit or tuple(
            profile.stable_id for profile in current_registry.profiles
        )
        if current_included!=included_ids:
            return False
        for stable_id,name,root,manager,availability,inventory_id,count in library_tokens:
            profile,current_manager=dataset.profile_manager(stable_id)
            configured=current_registry.get(stable_id)
            if (
                configured is None or current_manager is not manager
                or configured.name!=name
                or Path(configured.root).expanduser()!=root
                or str(dataset.library_availability(stable_id) or "not_loaded")
                !=availability
            ):
                return False
            inventory=getattr(current_manager,"images",()) if current_manager is not None else ()
            if id(inventory)!=inventory_id or len(inventory)!=count:
                return False
        return True

    def exact_current(prepared: PreparedDuplicatePreflightScope) -> bool:
        if not current():return False
        original_managers={
            stable_id:manager
            for stable_id,_name,_root,manager,_availability,_inventory_id,_count
            in library_tokens
        }
        for stable_id,expected in prepared.library_inventories:
            _profile,manager=dataset.profile_manager(stable_id)
            if manager is not original_managers.get(stable_id):
                return False
            inventory=getattr(manager,"images",()) if manager is not None else ()
            if tuple(Path(value) for value in inventory)!=expected:
                return False
        return True

    plan=DuplicatePreflightScopePlan(
        tuple(Path(value) for value in impact_context.images),ingest_root,libraries,
    )
    return DuplicatePreflightContext(
        state,plan,current,exact_current,page,
    )


def _gallery_duplicate_context(page,paths) -> DuplicatePreflightContext:
    images=tuple(dict.fromkeys(Path(value) for value in paths))
    state=page.state; settings=page.settings
    inventory=getattr(page,"images",())
    inventory_token=(id(inventory),len(inventory))
    ingest_directory=str(getattr(settings,"ingest_directory","") or "")
    page_root=getattr(page,"root",None)
    loaded_folder=getattr(page,"_loaded_folder",None)

    def current() -> bool:
        current_inventory=getattr(page,"images",())
        return (
            not getattr(page,"_shutdown_requested",False)
            and page.state is state and page.settings is settings
            and (id(current_inventory),len(current_inventory))==inventory_token
            and str(getattr(settings,"ingest_directory","") or "")==ingest_directory
            and getattr(page,"root",None)==page_root
            and getattr(page,"_loaded_folder",None)==loaded_folder
        )

    impact=type("GalleryDuplicateScope",(),{})()
    impact.images=images; impact.is_current=current
    context=duplicate_preflight_context(page,impact)
    base_exact=context.is_scope_current

    def exact_current(prepared: PreparedDuplicatePreflightScope) -> bool:
        if not base_exact(prepared):return False
        current_paths={Path(value) for value in getattr(page,"images",())}
        return all(path in current_paths for path in images)

    return DuplicatePreflightContext(
        context.state,context.plan,context.is_current,exact_current,context.owner,
    )


def run_gallery_duplicate_check(page,paths) -> int:
    """Run the shared duplicate workflow for an exact gallery-supplied scope."""
    images=tuple(dict.fromkeys(Path(value) for value in paths))
    if not images or getattr(page,"_shutdown_requested",False):
        return int(QDialog.Rejected)
    options=_gallery_duplicate_options(page)
    if options is None:
        return int(QDialog.Rejected)
    include_similar,lower,upper=options
    context=_gallery_duplicate_context(page,images)
    if not context.is_current():
        return int(QDialog.Rejected)
    dialog=DuplicatePreflightDialog(
        context,include_similar=include_similar,lower_similarity=lower,
        upper_similarity=upper,
        parent=page if isinstance(page,QWidget) else None,purpose="gallery",
    )
    try:
        return int(dialog.exec())
    finally:
        if getattr(page,"_duplicate_preflight_dialog",None) is dialog:
            page._duplicate_preflight_dialog=None
        dialog.deleteLater()
        render=getattr(page,"render_page",None)
        if callable(render):render()


class DuplicatePreflightDialog(FeedbackEnabledDialog):
    """Optional read-only duplicate evidence gate before Ingest preparation."""

    feedback_workflow="Ingest duplicate preflight"

    def __init__(
        self,context: DuplicatePreflightContext,*,include_similar: bool=False,
        lower_similarity: float=DEFAULT_SIMILARITY_PERCENT,
        upper_similarity: float=100,parent=None,
        purpose: str="preparation",
    ):
        super().__init__(parent)
        if purpose not in {"preparation","gallery"}:
            raise ValueError("Duplicate preflight purpose must be preparation or gallery.")
        self.purpose=purpose; self.standalone=purpose=="gallery"
        if self.standalone:self.feedback_workflow="Ingest gallery duplicate check"
        self.context=context; self.state=context.state
        self.include_similar=bool(include_similar)
        self.lower_similarity=float(lower_similarity)
        self.upper_similarity=float(upper_similarity)
        self.prepared=None; self.report=None; self._task=None
        self._task_phase=""; self._closed=False
        self._report_current=False; self._review_dialog=None
        self._coverage_unavailable=False; self._pending_action=""
        if context.owner is not None:
            context.owner._duplicate_preflight_dialog=self
        self.setWindowTitle(
            "Check Gallery Images for Duplicates"
            if self.standalone else "Duplicate Check Before Preparation"
        )
        self.resize(860,540); self.setMinimumSize(720,480)
        root=QVBoxLayout(self); root.setSpacing(10)
        heading=QLabel(
            "Check gallery duplicates"
            if self.standalone else "Check duplicates before preparation"
        )
        heading.setObjectName("sectionTitle"); heading.setWordWrap(True)
        root.addWidget(heading)
        self.scope_label=QLabel(
            f"Scope: {_counted(len(context.plan.incoming_paths),'incoming image')} "
            "compared with "
            f"{_counted(len(context.plan.libraries),'included Dataset library','included Dataset libraries')}. "
            "Only incoming-to-Dataset pairs are checked."
        )
        self.scope_label.setWordWrap(True)
        self.scope_label.setAccessibleName("Exact duplicate preflight scope")
        root.addWidget(self.scope_label)
        self.mode_label=QLabel(
            (
                "Matches: exact bytes, identical decoded pixels, and visually similar "
                f"candidates in the inclusive {self.lower_similarity:g}–"
                f"{self.upper_similarity:g}% range. Visual candidates require review."
                if self.include_similar else
                "Matches: exact bytes and identical decoded pixels. "
                "Visual similarity is not checked."
            )
        )
        self.mode_label.setWordWrap(True); self.mode_label.setObjectName("mutedLabel")
        root.addWidget(self.mode_label)
        authority=QLabel(
            "Review evidence only. This check does not change images or decisions."
            if self.standalone else
            "Review evidence only. This check does not filter the preparation "
            "scope or change images or decisions."
        )
        authority.setToolTip(
            "This check never changes images, Image Knowledge, TXT, Metadata, "
            "organization, or Recycle decisions."
        )
        authority.setAccessibleDescription(authority.toolTip())
        authority.setWordWrap(True); authority.setObjectName("mutedLabel")
        root.addWidget(authority)

        self.summary_panel=QFrame(); self.summary_panel.setObjectName("sectionCard")
        summary_layout=QVBoxLayout(self.summary_panel)
        self.result_summary=QLabel("No duplicate result is available yet.")
        self.result_summary.setWordWrap(True); self.result_summary.setObjectName("sectionTitle")
        summary_layout.addWidget(self.result_summary)
        self.result_detail=QLabel(); self.result_detail.setWordWrap(True)
        self.result_detail.setObjectName("mutedLabel")
        summary_layout.addWidget(self.result_detail)
        self.cache_summary=QLabel(); self.cache_summary.setWordWrap(True)
        self.cache_summary.setObjectName("mutedLabel"); summary_layout.addWidget(self.cache_summary)
        self.dataset_summary=QLabel(); self.dataset_summary.setWordWrap(True)
        self.dataset_summary.setObjectName("mutedLabel")
        summary_layout.addWidget(self.dataset_summary)
        self.exclusion_summary=QLabel(); self.exclusion_summary.setWordWrap(True)
        self.exclusion_summary.setObjectName("warningLabel")
        summary_layout.addWidget(self.exclusion_summary)
        self.summary_panel.hide(); root.addWidget(self.summary_panel)

        self.progress=QProgressBar(); self.progress.hide(); root.addWidget(self.progress)
        self.status=QLabel("Preparing to check duplicates…")
        self.status.setWordWrap(True); self.status.setObjectName("mutedLabel")
        self.status.setAccessibleName("Duplicate preflight status")
        root.addWidget(self.status)
        actions=QHBoxLayout()
        self.cancel_work_button=compact_button(QPushButton("Cancel Check"))
        self.cancel_work_button.clicked.connect(self.cancel_work)
        self.retry_button=compact_button(QPushButton("Resume Check"))
        self.retry_button.clicked.connect(self.start_check); self.retry_button.hide()
        self.review_button=compact_button(QPushButton("Open Duplicate Review…"))
        self.review_button.clicked.connect(self.open_review); self.review_button.setEnabled(False)
        self.continue_button=None
        if not self.standalone:
            self.continue_button=compact_button(
                QPushButton("Start Preparation"),primary=True,
            )
            self.continue_button.clicked.connect(self.continue_preparation)
            self.continue_button.setEnabled(False)
            self.continue_button.setAccessibleDescription(
                "Start the exact preparation submitted behind this check without "
                "filtering or changing its image scope."
            )
        actions.addWidget(self.cancel_work_button); actions.addWidget(self.retry_button)
        actions.addStretch(1); actions.addWidget(self.review_button)
        if self.continue_button is not None:actions.addWidget(self.continue_button)
        root.addStretch(1)
        root.addLayout(actions)
        self.buttons=QDialogButtonBox(
            QDialogButtonBox.Close if self.standalone else QDialogButtonBox.Cancel
        )
        self.buttons.rejected.connect(self.reject); root.addWidget(self.buttons)
        bind_status_footer(
            self,self.status,self.progress,self.cancel_work_button,
            title=(
                "Ingest gallery duplicate check"
                if self.standalone else "Ingest duplicate preflight"
            ),
        )
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False); button.setDefault(False)
        self._freshness_timer=QTimer(self); self._freshness_timer.setInterval(300)
        self._freshness_timer.timeout.connect(self._refresh_currentness)
        self._freshness_timer.start()
        QTimer.singleShot(0,self.start_check)

    def start_check(self) -> None:
        if self._closed or self._task is not None:
            return
        if not self.context.is_current():
            self._stale(
                "The incoming gallery or included Dataset scope changed before the check."
                if self.standalone else
                "The preparation or included Dataset scope changed before the check."
            )
            return
        self.report=None; self._report_current=False
        self._coverage_unavailable=False; self._pending_action=""
        self.summary_panel.hide()
        self.retry_button.hide(); self.cancel_work_button.show()
        if self.continue_button is not None:self.continue_button.setEnabled(False)
        self.review_button.setEnabled(False)
        self.progress.show(); set_progress_bar(self.progress,unknown_text="Preparing exact scope…")
        self.status.setText("Preparing cross-scope image identities in the shared background coordinator…")
        if self.prepared is not None:
            self._start_search(self.prepared)
            return

        context=self.context
        def work(job):
            return build_duplicate_preflight_scope(
                context.plan,progress=job.update,
                should_cancel=lambda:not job.checkpoint(),
            )

        task=CoordinatedCallTask(
            self.context.state.jobs,
            (
                "Preparing Ingest gallery duplicate scope"
                if self.standalone else
                "Preparing Ingest duplicate preflight scope"
            ),
            work,resource="io",key=f"duplicate-preflight-scope:{id(self.context)}",
        )
        self._task=task; self._task_phase="scope"
        task.signals.progress.connect(self._progress)
        task.signals.complete.connect(self._scope_ready)
        task.signals.cancelled.connect(self._cancelled)
        task.signals.failed.connect(self._failed)
        task.start()

    def _scope_ready(self,prepared) -> None:
        if self._closed:return
        self._task=None; self._task_phase=""
        if not self.context.is_current():
            self._stale(
                "The incoming gallery or included Dataset scope changed while identities were prepared."
                if self.standalone else
                "The preparation or included Dataset scope changed while identities were prepared."
            )
            return
        if self.standalone:
            prepared=replace(
                prepared,scope=replace(
                    prepared.scope,
                    label=prepared.scope.label.replace(
                        "Frozen Ingest preflight:","Frozen Ingest gallery check:",1,
                    ),
                ),
            )
        self.prepared=prepared
        if not prepared.dataset_image_count:
            self._coverage_unavailable=True
            self.result_summary.setText(
                "Duplicate evidence is unavailable because no images were "
                "available from the explicitly included Dataset libraries. "
                "This is not a no-match result, and nothing was changed or removed."
            )
            self.cache_summary.setText(
                "Dataset coverage: no images across "
                f"{_counted(prepared.included_library_count,'included library','included libraries')}."
            )
            notes=list(prepared.library_notes)
            if not prepared.included_library_count:
                notes.append("No Dataset libraries are explicitly included.")
            self.exclusion_summary.setText("\n".join(notes[:8]))
            self.exclusion_summary.setObjectName("warningLabel")
            self.summary_panel.show(); self.retry_button.setText("Check Again")
            self._validate_currentness("")
            return
        self._start_search(prepared)

    def _start_search(self,prepared) -> None:
        service=self.context.state.duplicates
        key=service.search_key(
            prepared.scope,self.lower_similarity,self.upper_similarity,
            include_similar=self.include_similar,
            incoming_identities=prepared.incoming_identities,
        )

        def work(job):
            return service.search(
                prepared.scope,lower_similarity=self.lower_similarity,
                upper_similarity=self.upper_similarity,include_similar=self.include_similar,
                incoming_identities=prepared.incoming_identities,
                progress=job.update,should_cancel=lambda:not job.checkpoint(),
            )

        task=CoordinatedCallTask(
            self.context.state.jobs,
            (
                "Checking Ingest gallery images for duplicates"
                if self.standalone else
                "Checking Ingest duplicates before preparation"
            ),
            work,resource="cpu",key=key,
        )
        self._task=task; self._task_phase="search"
        self.status.setText("Checking exact fingerprints in the shared cache…")
        set_progress_bar(self.progress,unknown_text="Checking fingerprints…")
        task.signals.progress.connect(self._progress)
        task.signals.complete.connect(self._ready)
        task.signals.cancelled.connect(self._cancelled)
        task.signals.failed.connect(self._failed)
        task.start()

    def _progress(self,current,total,message) -> None:
        if self._closed:return
        set_progress_bar(self.progress,current,total,detail=str(message or ""))
        if message:self.status.setText(str(message))

    def _finish_task(self) -> None:
        self._task=None; self._task_phase=""; self.progress.hide()
        self.cancel_work_button.hide(); self.retry_button.show()

    def _ready(self,report) -> None:
        if self._closed:return
        self._finish_task(); self.report=report
        prepared=self.prepared
        expected=prepared is not None and (
            report.scope_id==prepared.scope.id
            and bool(report.include_similar)==self.include_similar
            and report.incoming_identities==prepared.incoming_identities
        )
        if not expected:
            self.report=None
            self._failed(
                "Another duplicate job returned a different scope or comparison "
                "mode. Resume this check; no gallery action was taken."
                if self.standalone else
                "Another duplicate job returned a different scope or comparison "
                "mode. Resume this check; preparation remains held."
            )
            return
        relations=[relation for group in report.groups for relation in group.relations]
        byte_count=sum(value.kind=="byte_exact" for value in relations)
        pixel_count=sum(value.kind=="pixel_exact" for value in relations)
        similar_count=sum(value.kind=="similar" for value in relations)
        if report.groups:
            result=_counted(len(report.groups),"potential duplicate set")
            detail=(
                f"{byte_count:,} byte-identical; "
                f"{pixel_count:,} decoded-pixel-identical"
            )
            if self.include_similar:
                detail+=(
                    "; "
                    f"{_counted(similar_count,'visually similar candidate relation')}"
                )
            detail+=". Review these candidates before deciding what to do."
        else:
            incomplete=bool(report.truncated or report.issues or
                            report.stale_identities or prepared.library_notes)
            result=("No matches in checked portion" if incomplete else
                    "No cross-scope matches found")
            detail="In the available checked evidence. Nothing was changed or removed."
        if report.truncated:
            detail+=" The comparison safety bound was reached; this result is incomplete."
        self.result_summary.setText(result)
        self.result_detail.setText(detail)
        self.cache_summary.setText(
            f"Fingerprints: {report.indexed:,} new or changed; "
            f"{report.reused:,} unchanged reused; {report.removed:,} retired."
        )
        self.dataset_summary.setText(
            f"Dataset checked: {_counted(prepared.dataset_image_count,'image')} across "
            f"{_counted(prepared.included_library_count,'included library','included libraries')}."
        )
        issue_lines=list(prepared.library_notes)
        if report.stale_identities:
            issue_lines.append(
                f"{_counted(len(report.stale_identities),'image')} use labelled saved "
                "fingerprints because their current files are unavailable."
            )
        if report.issues:
            issue_lines.append(
                f"{_counted(len(report.issues),'file or comparison issue')} were held; "
                "the check does not treat them as no matches."
            )
        self.exclusion_summary.setText(
            "\n".join(issue_lines[:8]) if issue_lines else
            "Coverage complete: all included library inventories were available."
        )
        self.exclusion_summary.setObjectName("warningLabel" if issue_lines else "mutedLabel")
        self.summary_panel.show()
        self.retry_button.setText("Check Again")
        self._validate_currentness("")

    def _validate_currentness(self,action: str) -> None:
        prepared=self.prepared
        if self._closed or prepared is None or self._task is not None:
            return
        if not self.context.is_current():
            self._stale(
                "The incoming gallery or included Dataset scope changed."
                if self.standalone else
                "The preparation or included Dataset scope changed."
            )
            return
        context=self.context
        def work(job):
            if not job.checkpoint():raise InterruptedError()
            return prepared.scope.id,bool(context.is_scope_current(prepared))
        task=CoordinatedCallTask(
            self.context.state.jobs,
            (
                "Confirming Ingest gallery duplicate scope"
                if self.standalone else "Confirming duplicate preflight scope"
            ),
            work,resource="io",
        )
        self._task=task; self._task_phase="currentness"
        self._pending_action=str(action or "")
        self._report_current=False
        if self.continue_button is not None:self.continue_button.setEnabled(False)
        self.review_button.setEnabled(False)
        self.cancel_work_button.show(); self.retry_button.hide(); self.progress.show()
        set_progress_bar(self.progress,unknown_text="Confirming exact scope…")
        self.status.setText("Confirming the exact included Dataset paths in the background…")
        task.signals.complete.connect(self._currentness_ready)
        task.signals.cancelled.connect(self._cancelled)
        task.signals.failed.connect(self._failed)
        task.start()

    def _currentness_ready(self,result) -> None:
        if self._closed:return
        action=self._pending_action; self._pending_action=""
        self._finish_task()
        prepared=self.prepared
        if (
            prepared is None or not isinstance(result,tuple) or len(result)!=2
            or result[0]!=prepared.scope.id or not bool(result[1])
        ):
            self._stale(
                "The incoming gallery or included Dataset paths changed after the check."
                if self.standalone else
                "The preparation or included Dataset paths changed after the check."
            )
            return
        self._report_current=True
        if self.report is not None:
            self.context.state.duplicates.record_badge_results(self.report)
        if action=="continue":
            self.accept(); return
        if action=="review":
            self._open_review_current(); return
        self._refresh_currentness()

    def _refresh_currentness(self) -> None:
        if self._closed:return
        has_summary=bool(self.report is not None or self._coverage_unavailable)
        current=bool(has_summary and self._report_current and self.context.is_current())
        if has_summary and self._report_current and not current:
            self._stale(
                (
                    "The incoming gallery or included Dataset scope changed after "
                    "this result. Resume the check before using it."
                ) if self.standalone else (
                    "The preparation or included Dataset scope changed after this "
                    "result. Resume the check before using it."
                )
            )
            return
        if self.continue_button is not None:self.continue_button.setEnabled(current)
        self.review_button.setEnabled(
            current and self.report is not None
            and bool(getattr(self.report,"groups",()))
        )
        if current:
            self.status.setText(
                "Check complete. Review matches or close this gallery check."
                if self.standalone else
                "Check complete. Review potential matches or start preparation "
                "with the selected images."
            )

    def _stale(self,message: str) -> None:
        self._report_current=False
        if self.continue_button is not None:self.continue_button.setEnabled(False)
        self.review_button.setEnabled(False)
        self.cancel_work_button.hide(); self.retry_button.show()
        self.retry_button.setText("Resume Check")
        self.status.setText(
            str(message)+(
                " No gallery action was taken."
                if self.standalone else " No preparation has started."
            )
        )

    def _failed(self,message) -> None:
        if self._closed:return
        self._finish_task(); self._report_current=False
        if self.continue_button is not None:self.continue_button.setEnabled(False)
        self.review_button.setEnabled(False)
        self.retry_button.setText("Resume Check")
        self.status.setText(
            f"Duplicate check needs attention: {message} "
            +(
                "No gallery action was taken."
                if self.standalone else "No preparation has started."
            )
        )

    def _cancelled(self) -> None:
        if self._closed:return
        self._finish_task(); self._report_current=False
        if self.continue_button is not None:self.continue_button.setEnabled(False)
        self.review_button.setEnabled(False)
        self.retry_button.setText("Resume Check")
        self.status.setText(
            "Duplicate check paused safely. Completed fingerprints remain in the "
            "shared cache; choose Resume Check to continue. "
            +(
                "No gallery action was taken."
                if self.standalone else "No preparation started."
            )
        )

    def cancel_work(self) -> None:
        task=self._task
        if task is None:return
        task.cancel()
        # CoordinatedCallTask deliberately suppresses delivery after an observer
        # cancels. Finish this dialog state immediately while the orphaned shared
        # job either stops cooperatively or continues for another observer.
        self._task=None; self._task_phase=""; self.progress.hide()
        self._cancelled()

    def continue_preparation(self) -> None:
        if (
            not self._report_current
            or (self.report is None and not self._coverage_unavailable)
            or not self.context.is_current()
        ):
            self._stale("This duplicate result is no longer current.")
            return
        self._validate_currentness("continue")

    def open_review(self) -> None:
        if (
            not self._report_current or self.report is None
            or self.prepared is None or not self.report.groups
            or not self.context.is_current()
        ):
            self._stale("This duplicate result is no longer current.")
            return
        self._validate_currentness("review")

    def _open_review_current(self) -> None:
        if self.report is None or self.prepared is None or not self.report.groups:
            self._stale("This duplicate result is no longer current.")
            return
        prepared=self.prepared
        context=self.context
        review=DuplicateFinderDialog(
            self.context.state,self,frozen_scope=self.prepared.scope,
            frozen_mode="preflight",initial_report=self.report,
            include_similar=self.include_similar,
            incoming_identities=self.prepared.incoming_identities,
            frozen_currentness=self.context.is_current,
            frozen_authority_currentness=(
                lambda:context.is_scope_current(prepared)
            ),
        )
        self._review_dialog=review
        owner=self.context.owner
        if owner is not None:
            review.edit_requested.connect(self._edit_requested)
            review.organize_requested.connect(self._organize_requested)
            review.changed.connect(self._files_changed)
        review.exec()
        self._review_dialog=None
        self._refresh_currentness()

    def _dataset_page(self):
        owner=self.context.owner
        window=owner.window() if owner is not None else None
        return getattr(window,"dataset_page",None)

    def _edit_requested(self,value: str) -> None:
        path=Path(value); profile=self.context.state.settings.library_registry().profile_for_path(path)
        dataset_page=self._dataset_page()
        if profile is not None and dataset_page is not None:
            dataset_page.edit_gallery_image(value)
        elif self.context.owner is not None:
            self.context.owner.edit_gallery_image(value)

    def _organize_requested(self,value: str) -> None:
        dataset_page=self._dataset_page()
        if dataset_page is not None:
            dataset_page._organize_duplicate_image(value)

    def _files_changed(self,values) -> None:
        owner=self.context.owner
        if owner is None:return
        owner.library_paths_changed.emit(tuple(str(Path(value)) for value in values))

    def reject(self) -> None:
        self._closed=True; self._freshness_timer.stop()
        if self._task is not None:
            self._task.cancel(); self._task=None
        review=self._review_dialog
        if review is not None:
            review.reject()
        if self.context.owner is not None:
            self.context.owner._duplicate_preflight_dialog=None
        super().reject()

    def accept(self) -> None:
        self._closed=True; self._freshness_timer.stop()
        if self.context.owner is not None:
            self.context.owner._duplicate_preflight_dialog=None
        super().accept()
