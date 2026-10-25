from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
import time
from typing import Callable, Iterable, Generator
from threading import Lock, get_ident
from enum import Enum
from types import SimpleNamespace

from .annotation import ImageAnnotation
from .batch_preparation import BatchPreparationService
from .ai_interrogation import _preparation_record
from .image_record import ImageRecord
from .operation_journal import JournalOperation, OperationJournal
from .history_configuration import HistoryConfigurationError, path_key
from .history_versions import HistoryError
from .image_knowledge_history import HistoryWriteInterrupted
from .preferred_caption_preparation import (
    PreferredCaptionPreparationApplyResult,
    PreferredCaptionPreparationService,
)


CONFIDENCE_PRESETS = {
    "conservative": {
        "label": "Conservative",
        "minimum_tag_votes": 3,
        "external_evidence_vote": True,
        "description": (
            "Requires at least three votes before a known, conflict-free Tag "
            "can be confirmed. Each distinct current AI tagger counts "
            "once; matching typed TXT/Metadata together add one vote."
        ),
    },
    "balanced": {
        "label": "Balanced",
        "minimum_tag_votes": 2,
        "external_evidence_vote": True,
        "description": (
            "Requires at least two votes before a known, conflict-free Tag can "
            "be confirmed. Each distinct current AI tagger counts once; "
            "matching typed TXT/Metadata together add one vote."
        ),
    },
    "permissive": {
        "label": "Permissive — one model",
        "minimum_tag_votes": 1,
        "external_evidence_vote": False,
        "description": (
            "Warning: one current AI model may confirm a known Tag. "
            "TXT/Metadata alone cannot confirm it."
        ),
    },
}


class PreparationStaleError(RuntimeError):
    """A preparation transaction no longer matches current Image Knowledge."""


@dataclass(frozen=True)
class PreparationOptions:
    """The visible, shared choices for one preparation run."""

    read_external_evidence: bool = True
    suggest_sdxl_tags: bool = True
    suggest_plain_language: bool = True
    # ``apply_safe_facts`` remains a compatibility input for callers saved
    # before tag confirmation and preferred-description preparation became
    # independently configurable. New callers should set the two explicit
    # options below.
    apply_safe_facts: bool | None = None
    confirm_safe_tags: bool | None = None
    prepare_preferred_descriptions: bool | None = None
    skip_ai_value: int = 7
    skip_ai_unit: str = "days"
    force_all_ai: bool = False
    allow_memory_pressure: bool = False  # Explicit reviewed task choice; never a saved default.
    confidence_preset: str = "balanced"
    minimum_tag_votes: int | None = None
    count_external_evidence_vote: bool | None = None

    @property
    def maximum_ai_age_seconds(self) -> float:
        multiplier={"hours":3600,"days":86400,"months":30*86400}.get(self.skip_ai_unit,86400)
        return float(max(1,int(self.skip_ai_value))*multiplier)

    @property
    def normalized_confidence_preset(self) -> str:
        value=str(self.confidence_preset or "balanced").strip().casefold()
        configured=(
            self.tag_agreement_requirement(),
            self.uses_external_evidence_vote,
        )
        if value in CONFIDENCE_PRESETS:
            policy=CONFIDENCE_PRESETS[value]
            if configured==(
                int(policy["minimum_tag_votes"]),
                bool(policy["external_evidence_vote"]),
            ):
                return value
        for key,policy in CONFIDENCE_PRESETS.items():
            if configured==(
                int(policy["minimum_tag_votes"]),
                bool(policy["external_evidence_vote"]),
            ):
                return key
        return "custom"

    def tag_agreement_requirement(self, _configured_minimum: int=1) -> int:
        if self.minimum_tag_votes is not None:
            return max(1,min(99,int(self.minimum_tag_votes)))
        value=str(self.confidence_preset or "balanced").strip().casefold()
        policy=CONFIDENCE_PRESETS.get(value,CONFIDENCE_PRESETS["balanced"])
        return int(policy["minimum_tag_votes"])

    @property
    def uses_external_evidence_vote(self) -> bool:
        if self.count_external_evidence_vote is not None:
            return bool(self.count_external_evidence_vote)
        value=str(self.confidence_preset or "balanced").strip().casefold()
        policy=CONFIDENCE_PRESETS.get(value,CONFIDENCE_PRESETS["balanced"])
        return bool(policy["external_evidence_vote"])

    @property
    def allows_single_model_promotion(self) -> bool:
        return self.tag_agreement_requirement()==1

    @property
    def confirms_safe_tags(self) -> bool:
        if self.confirm_safe_tags is not None:
            return bool(self.confirm_safe_tags)
        return True if self.apply_safe_facts is None else bool(self.apply_safe_facts)

    @property
    def prepares_preferred_descriptions(self) -> bool:
        if self.prepare_preferred_descriptions is not None:
            return bool(self.prepare_preferred_descriptions)
        return True if self.apply_safe_facts is None else bool(self.apply_safe_facts)

    @property
    def has_automated_application(self) -> bool:
        return self.confirms_safe_tags or self.prepares_preferred_descriptions


@dataclass
class PreparationImageOutcome:
    image: Path
    expected_ai_passes: int = 0
    tag_model_passes: int = 0
    description_model_passes: int = 0
    ai_passes_skipped: int = 0
    safe_tags_applied: int = 0
    conflicts_held: int = 0
    preferred_description_applied: bool = False
    review_required: bool = False
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False
    external_evidence_refreshed: bool = False

    @property
    def completed_ai_passes(self) -> int:
        return self.tag_model_passes+self.description_model_passes

    @property
    def status(self) -> str:
        if self.conflicts_held:
            return "conflicted"
        satisfied=self.completed_ai_passes+self.ai_passes_skipped
        if self.errors:
            return "partial" if satisfied or self.preferred_description_applied else "failed"
        if self.cancelled and satisfied<self.expected_ai_passes:
            return "partial"
        if self.expected_ai_passes and not self.completed_ai_passes and self.ai_passes_skipped:
            return "skipped"
        return "completed"

    def detail(self) -> str:
        parts=[]
        if self.external_evidence_refreshed:
            parts.append("TXT and Metadata evidence refreshed")
        if self.completed_ai_passes:
            parts.append(f"{self.completed_ai_passes:,} AI pass(es) completed")
        if self.ai_passes_skipped:
            parts.append(f"{self.ai_passes_skipped:,} current pass(es) reused")
        if self.safe_tags_applied:
            parts.append(f"{self.safe_tags_applied:,} agreed Tag(s) confirmed")
        if self.conflicts_held:
            parts.append(f"{self.conflicts_held:,} conflict(s) held for review")
        if self.preferred_description_applied:
            parts.append("preferred Description updated")
        if self.review_required:
            parts.append("review findings available")
        if self.cancelled and (
            self.completed_ai_passes+self.ai_passes_skipped<self.expected_ai_passes
        ):
            parts.append("cancelled before every requested pass finished")
        if self.errors:
            parts.append("; ".join(dict.fromkeys(self.errors)))
        return "; ".join(parts) or "No changes were needed."

    def as_dict(self) -> dict:
        return {
            "image":str(self.image),"status":self.status,"detail":self.detail(),
            "expected_ai_passes":self.expected_ai_passes,
            "tag_model_passes":self.tag_model_passes,
            "description_model_passes":self.description_model_passes,
            "ai_passes_skipped":self.ai_passes_skipped,
            "safe_tags_applied":self.safe_tags_applied,
            "conflicts_held":self.conflicts_held,
            "preferred_description_applied":self.preferred_description_applied,
            "review_required":self.review_required,
            "errors":list(self.errors),"cancelled":self.cancelled,
            "external_evidence_refreshed":self.external_evidence_refreshed,
        }


@dataclass
class PreparationRunResult:
    images: int = 0
    external_updates: int = 0
    tag_model_passes: int = 0
    description_model_passes: int = 0
    ai_passes_skipped: int = 0
    safe_tags_applied: int = 0
    conflicts_held: int = 0
    preferred_descriptions_applied: int = 0
    preferred_descriptions_held: int = 0
    operation_id: str = ""
    changes_recorded: int = 0
    resumed: bool = False
    review_paths: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False
    outcomes: list[PreparationImageOutcome] = field(default_factory=list)
    history_operation_id: str = ""
    history_coverage: list[dict] = field(default_factory=list)
    history_interrupted: bool = False

    def outcome_counts(self) -> dict[str,int]:
        counts={key:0 for key in ("completed","partial","skipped","conflicted","failed")}
        for outcome in self.outcomes:
            counts[outcome.status]=counts.get(outcome.status,0)+1
        return counts

    def summary_lines(self) -> list[str]:
        lines=[
            (
                f"Processed {self.images:,} image(s): {self.external_updates:,} "
                f"inward TXT/Metadata evidence refresh(es), "
                f"{self.tag_model_passes + self.description_model_passes:,} AI "
                f"model pass(es), and {self.ai_passes_skipped:,} fresh AI "
                "pass(es) skipped."
            ),
            (
                f"Applied {self.safe_tags_applied:,} safely agreed Tag(s) "
                f"and {self.preferred_descriptions_applied:,} attributable "
                "preferred description update(s)."
            ),
            (
                f"Held {self.conflicts_held:,} Tag conflict(s) and "
                f"{self.preferred_descriptions_held:,} description decision(s) "
                "for review."
            ),
            "Raw source and AI evidence was preserved; ambiguous findings were not confirmed, and source TXT, Metadata, and image files were not rewritten.",
        ]
        if self.cancelled:
            lines.insert(0,(
                "Preparation stopped; completed image changes were kept. Review saved history before starting another preparation."
                if self.history_operation_id else
                "Paused safely after completed image changes were kept. Choose Prepare again with the same scope and choices to resume."
            ))
        elif self.resumed:
            lines.insert(0,"Resumed the prior matching preparation transaction safely.")
        if self.history_interrupted:
            lines.insert(0,"Saved-history preparation stopped. Completed changes were retained; review the affected image and recover its saved history before continuing.")
        counts=self.outcome_counts()
        lines.insert(1,(
            f"Per-image outcomes: {counts['completed']:,} completed, "
            f"{counts['partial']:,} partial, {counts['skipped']:,} skipped, "
            f"{counts['conflicted']:,} conflicted, and {counts['failed']:,} failed."
        ))
        return lines

    def as_page_result(self) -> dict:
        return {
            "processed":self.images,
            "skipped":self.ai_passes_skipped,
            "errors":list(self.errors),
            "summary_lines":self.summary_lines(),
            "review_paths":list(self.review_paths),
            "preparation_operation_id":self.operation_id,
            "preparation_changes_recorded":self.changes_recorded,
            "preparation_resumed":self.resumed,
            "history_operation_id":self.history_operation_id,
            "history_coverage":list(self.history_coverage),
            "history_interrupted":self.history_interrupted,
            "preferred_descriptions_applied":self.preferred_descriptions_applied,
            "cancelled":self.cancelled,
            "outcomes":[outcome.as_dict() for outcome in self.outcomes],
            "outcome_counts":self.outcome_counts(),
        }


@dataclass
class PreparationUndoResult:
    operation_id: str = ""
    changed: int = 0


def _backend_identity(backend) -> dict:
    if backend is None:
        return {}
    provider=getattr(backend,"evidence_identity",None)
    if callable(provider):
        try:
            identity=provider()
        except Exception:
            identity=None
        if isinstance(identity,dict):
            normalized=copy.deepcopy(identity)
            settings=normalized.get("settings")
            if isinstance(settings,dict):
                settings.pop("execution_provider",None)
            return normalized
    return {
        "model_id":str(getattr(backend,"model_id","") or type(backend).__name__),
        "model_revision":str(getattr(backend,"model_revision","") or ""),
        "kind":str(getattr(backend,"kind","") or ""),
    }


def _canonical_digest(value) -> str:
    payload=json.dumps(
        value,sort_keys=True,separators=(",",":"),ensure_ascii=False,default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class _PreparationCaptureContext:
    """Per-run committed receipts; never mutable state on a shared producer."""
    def __init__(self, context):
        self.context = context
        self.version_ids = []
        self.error = ""
        self.interrupted = False

    def __getattr__(self, name):
        return getattr(self.context, name)

    @contextmanager
    def locked(self, image):
        try:
            with self.context.locked(image):
                yield
        except Exception as exc:
            self.error = str(exc)
            self.interrupted = isinstance(exc, HistoryWriteInterrupted)
            raise

    def write(self, *args, **kwargs):
        before = self.store.current_head(self.entity)
        result = self.context.write(*args, **kwargs)
        # Called inside the canonical writer's original configuration/history
        # locks. Return a reference only after the durable commit succeeded.
        head = self.store.current_head(self.entity)
        if head is not None and head != before and head.operation_id == self.operation_id and head.origin in {"save", "restore"}:
            if head.version_id not in self.version_ids:
                self.version_ids.append(head.version_id)
        return result


@dataclass(frozen=True)
class PreparationStageCheckpoint:
    """Returned canonical stage; progress units are counters, not success receipts."""
    stage: str
    progress_units: int
    total_units: int


@dataclass(frozen=True)
class PreparationContinuationIntent:
    request_id: str
    paths: tuple[Path, ...]
    options: PreparationOptions


class PreparationContinuationState(str, Enum):
    READY = "ready"
    RUNNING = "running"
    CHECKPOINTED = "checkpointed"
    CANCEL_REQUESTED = "cancel_requested"
    FINALIZING = "finalizing"
    FINALIZED = "finalized"
    FAILED = "failed"


class PreparationContinuation:
    """Passive per-request state behind the unchanged serial driver.

    Checkpointed is not resource-free parking: production still drains under its
    whole-request worker and AIWorkLease. Managed backend release occurs in the
    canonical stages. No fresh suspended-resume authority, replay or stage sharing
    is provided. A started checkpoint may be cancelled into canonical finalization
    without executing the next stage; pre-start cancel stays on the queue route.
    """
    def __init__(self, driver, intent: PreparationContinuationIntent):
        self.intent = intent
        self._driver = driver
        self._lock = Lock()
        self._advancing = False
        self._drain_owner = None
        self._state = PreparationContinuationState.READY
        self._generation = 0
        self._cancel_requested = False
        self._failure = None
        self._result = None
        self.last_checkpoint = None

    @property
    def state(self):
        with self._lock:
            return self._state

    @property
    def attempt_generation(self):
        with self._lock:
            return self._generation

    @property
    def cancel_requested(self):
        with self._lock:
            return self._cancel_requested

    @property
    def waiting(self):
        with self._lock:
            return not self._advancing and self._state in {
                PreparationContinuationState.READY,
                PreparationContinuationState.CHECKPOINTED,
            }

    @property
    def done(self):
        with self._lock:
            return self._state == PreparationContinuationState.FINALIZED

    @property
    def result(self) -> PreparationRunResult:
        with self._lock:
            if self._state != PreparationContinuationState.FINALIZED:
                raise RuntimeError("Preparation has not finalized its outcomes.")
            return self._result

    def _drive(self, expected_generation, *, cancel=False):
        with self._lock:
            if self._advancing:
                raise RuntimeError("A preparation continuation is already advancing.")
            if self._drain_owner is not None and self._drain_owner != get_ident():
                raise RuntimeError("A preparation continuation is already draining.")
            if expected_generation is not None and (
                type(expected_generation) is not int or expected_generation != self._generation
            ):
                raise RuntimeError("This preparation continuation attempt is stale.")
            if self._failure is not None:
                raise RuntimeError("A failed continuation cannot be restarted.") from self._failure
            if self._state == PreparationContinuationState.FINALIZED:
                if cancel:
                    raise RuntimeError("Preparation has already finalized its outcomes.")
                return None
            if cancel and self._state != PreparationContinuationState.CHECKPOINTED:
                raise RuntimeError("Cancellation finalization requires an existing stage checkpoint.")
            self._advancing = True
            self._generation += 1
            if cancel:
                self._cancel_requested = True
            self._state = (PreparationContinuationState.CANCEL_REQUESTED if cancel
                           else PreparationContinuationState.RUNNING)
        try:
            if cancel:
                with self._lock:
                    self._state = PreparationContinuationState.FINALIZING
                checkpoint = self._driver.throw(InterruptedError("Preparation cancelled safely"))
                # A canonical cancellation handler returns final outcomes, never
                # another inference stage. Refuse a noncanonical driver here.
                raise RuntimeError("Cancelled preparation yielded another stage instead of finalizing.")
            checkpoint = next(self._driver)
            with self._lock:
                self.last_checkpoint = checkpoint
                self._state = PreparationContinuationState.CHECKPOINTED
            return checkpoint
        except StopIteration as terminal:
            with self._lock:
                self._state = PreparationContinuationState.FINALIZING
                self._result = terminal.value
                self._state = PreparationContinuationState.FINALIZED
            return None
        except BaseException as exc:
            with self._lock:
                self._failure = exc
                self._state = PreparationContinuationState.FAILED
            raise
        finally:
            with self._lock:
                self._advancing = False

    def advance(self, *, expected_generation=None) -> PreparationStageCheckpoint | None:
        return self._drive(expected_generation)

    def cancel_and_finalize(self, *, expected_generation=None) -> PreparationRunResult:
        """Only cancel a reached checkpoint; no admission/worker release implied."""
        self._drive(expected_generation, cancel=True)
        return self.result

    def drain(self, *, expected_generation=None) -> PreparationRunResult:
        with self._lock:
            if self._drain_owner is not None:
                raise RuntimeError("A preparation continuation is already draining.")
            self._drain_owner = get_ident()
        try:
            self.advance(expected_generation=expected_generation)
            while not self.done:
                self.advance()
            return self.result
        finally:
            with self._lock:
                self._drain_owner = None


class UnifiedPreparationService:
    """Run the same evidence, AI, and safe-fact preparation on any page scope."""

    def __init__(
        self,
        knowledge,
        ai_service,
        *,
        preferred_caption_service: PreferredCaptionPreparationService | None = None,
        journal: OperationJournal | None = None,
    ) -> None:
        self.knowledge=knowledge
        self.ai_service=ai_service
        self.preferred_caption_service=preferred_caption_service
        self.journal=journal

    OPERATION_KIND="unified_image_preparation"
    UNDO_KIND="unified_image_preparation_undo"
    POLICY_ID="unified_preparation_confidence"
    POLICY_VERSION=2

    def create_continuation(
        self,
        images: Iterable[str | Path],
        options: PreparationOptions,
        *,
        tag_backends=(),
        description_backend=None,
        minimum_tag_agreement: int = 1,
        progress: Callable[[int,int,str],None] | None = None,
        should_cancel: Callable[[],bool] | None = None,
        history_operation=None,
        request_id: str = "",
    ) -> PreparationContinuation:
        paths = tuple(dict.fromkeys(Path(value) for value in images))
        backends = tuple(tag_backends) if options.suggest_sdxl_tags else ()
        driver = self._run_stages(
            paths, options, tag_backends=backends,
            description_backend=description_backend,
            minimum_tag_agreement=minimum_tag_agreement,
            progress=progress, should_cancel=should_cancel,
            history_operation=history_operation,
        )
        return PreparationContinuation(driver, PreparationContinuationIntent(
            str(request_id), paths, options,
        ))

    def run(
        self,
        images: Iterable[str | Path],
        options: PreparationOptions,
        *,
        tag_backends=(),
        description_backend=None,
        minimum_tag_agreement: int = 1,
        progress: Callable[[int,int,str],None] | None = None,
        should_cancel: Callable[[],bool] | None = None,
        history_operation=None,
    ) -> PreparationRunResult:
        return self.create_continuation(
            images, options, tag_backends=tag_backends,
            description_backend=description_backend,
            minimum_tag_agreement=minimum_tag_agreement,
            progress=progress, should_cancel=should_cancel,
            history_operation=history_operation,
        ).drain()

    def _run_stages(
        self,
        images: Iterable[str | Path],
        options: PreparationOptions,
        *,
        tag_backends=(),
        description_backend=None,
        minimum_tag_agreement: int = 1,
        progress: Callable[[int,int,str],None] | None = None,
        should_cancel: Callable[[],bool] | None = None,
        history_operation=None,
    ) -> Generator[PreparationStageCheckpoint, None, PreparationRunResult]:
        paths=list(dict.fromkeys(Path(value) for value in images))
        should_cancel=should_cancel or (lambda:False)
        tag_backends=list(tag_backends) if options.suggest_sdxl_tags else []
        description_backend=(
            description_backend if options.suggest_plain_language else None
        )
        for backend in [*tag_backends,*([description_backend] if description_backend else [])]:
            setter=getattr(backend,"set_runtime_memory_override",None)
            if callable(setter):setter(bool(options.allow_memory_pressure))
        preferred=(
            self.preferred_caption_service
            if options.prepares_preferred_descriptions else None
        )
        evidence_units=len(paths) if options.read_external_evidence else 0
        tag_units=self.ai_service.work_units(len(paths),tag_backends)
        if tag_backends:
            tag_units+=len(paths)*(1+int(options.confirms_safe_tags))
        description_units=(
            self.ai_service.work_units(len(paths),[description_backend])
            if description_backend is not None else 0
        )
        preferred_units=(2*len(paths)) if preferred is not None else 0
        total=max(1,evidence_units+tag_units+description_units+preferred_units)
        current=0
        from .task_eta import RemainingTaskEstimate
        task_eta=RemainingTaskEstimate()
        task_eta.update('external','TXT/Metadata',evidence_units)
        task_eta.update('tag-review','Tag agreement/review',len(paths) if tag_backends else 0)
        task_eta.update('preferred-check','Description review',len(paths) if preferred else 0)
        task_eta.update('preferred-save','Description saving',len(paths) if preferred else 0)
        task_eta.update('final','Result/history finalization',1)
        backend_keys={id(backend):f'model-{index}' for index,backend in enumerate([*tag_backends,*([description_backend] if description_backend else [])])}
        backend_labels={id(backend):('WD '+str(index+1) if index<len(tag_backends) else 'Caption') for index,backend in enumerate([*tag_backends,*([description_backend] if description_backend else [])])}
        for backend in [*tag_backends,*([description_backend] if description_backend else [])]:
            key=backend_keys[id(backend)];label=backend_labels[id(backend)]
            task_eta.update(key,label+' inference',len(paths))
            task_eta.update(key+'-load',label+' cold-model loading',1)
            task_eta.update(key+'-wait',label+' admission/wait',1)
            task_eta.update(key+'-checks',label+' cache/input checks',len(paths))
            task_eta.update(key+'-save',label+' result saving',len(paths))
            task_eta.update(key+'-release',label+' release',1)
        def observe_inference(backend,remaining,phase):
            key=backend_keys[id(backend)];label=backend_labels[id(backend)]
            if phase=='held':task_eta.state='held'
            elif phase=='loading':task_eta.state='running'
            if phase=='finished':
                task_eta.complete(key,key+'-load',key+'-wait',key+'-checks',key+'-save',key+'-release');return
            if phase=='inference':task_eta.complete(key+'-load',key+'-wait')
            if phase=='loading':
                getter=getattr(backend,'remaining_loading_cost',None)
                try:load_low,load_high,samples=getter() if callable(getter) else (None,None,0)
                except Exception:load_low,load_high,samples=None,None,0
                task_eta.update(key+'-load',label+f' cold-model loading ({samples} successful observations; residency may shorten)',1,load_low,load_high)
            estimator=getattr(backend,'remaining_inference_estimate',None)
            try:low,high=estimator(remaining) if callable(estimator) else (None,None)
            except Exception:low,high=None,None
            task_eta.update(key,label+' inference',remaining,low,high)
            task_eta.update(key+'-checks',label+' cache/input checks',remaining)
            getter=getattr(backend,'remaining_saving_cost',None)
            try:save_low,save_high,samples=getter(remaining) if callable(getter) else (None,None,0)
            except Exception:save_low,save_high,samples=None,None,0
            task_eta.update(key+'-save',label+f' result saving ({samples} successful observations; payload context may vary)',remaining,save_low,save_high)
        def eta_label(label):return label+'; '+task_eta.text()
        result=PreparationRunResult(images=len(paths))
        admitted, attempted = {}, set()
        native_kwargs = {}
        if history_operation is not None:
            result.history_operation_id = history_operation.operation_id
            # Per-run state only; a shared AI/preferred service never stores an
            # active context or retargets a subsequent operation.
            def context_for(image):
                key = path_key(image)
                attempted.add(key)
                context = history_operation.context_for(image)
                if context is None or context.operation_id != result.history_operation_id:
                    raise HistoryError("Preparation requires its exact admitted history context.")
                if key not in admitted:
                    admitted[key] = _PreparationCaptureContext(context)
                return admitted[key]
            native_kwargs = {"history_operation": SimpleNamespace(operation_id=result.history_operation_id, context_for=context_for)}
        expected_ai_passes=len(tag_backends)+int(description_backend is not None)
        outcomes={
            path:PreparationImageOutcome(
                image=path,expected_ai_passes=expected_ai_passes,
            )
            for path in paths
        }
        required_stages = {name for name, enabled in (("external", options.read_external_evidence), ("tags", bool(tag_backends)),
                           ("description", description_backend is not None), ("preferred", preferred is not None)) if enabled}
        completed_stages = {path: set() for path in paths}
        operation_id=""
        if self.journal is not None and history_operation is None:
            signature=self._run_signature(
                paths,options,tag_backends,description_backend,
                minimum_tag_agreement,
            )
            operation=self._resumable_operation(signature)
            self.recover_interrupted(exclude={operation.id} if operation else set())
            if operation is not None:
                operation_id=operation.id
                result.resumed=True
            else:
                operation_id=self.journal.begin(
                    self.OPERATION_KIND,
                    expected=len(paths),
                    reason="Prepare image evidence, AI results, and safe facts",
                    metadata={
                        "signature":signature,
                        "paths":sorted(str(path.resolve()) for path in paths),
                        "options":asdict(options),
                        "confidence_preset":options.normalized_confidence_preset,
                        "tag_agreement":options.tag_agreement_requirement(
                            minimum_tag_agreement
                        ),
                        "external_evidence_vote":(
                            options.uses_external_evidence_vote
                        ),
                        "confirm_safe_tags":options.confirms_safe_tags,
                        "prepare_preferred_descriptions":(
                            options.prepares_preferred_descriptions
                        ),
                        "policy_id":self.POLICY_ID,
                        "policy_version":self.POLICY_VERSION,
                        "writes":"image_knowledge_only",
                    },
                )
            result.operation_id=operation_id

        def record_mutation(
            image: Path,before: dict,after: dict,sidecar_existed: bool,reason: str,
        ) -> None:
            if history_operation is not None or self.journal is None or not operation_id or before==after:
                return
            self.journal.append_change(
                operation_id,item=str(image),before=before,after=after,
                reason=reason,
                metadata={"sidecar_existed":bool(sidecar_existed)},
            )

        def report(offset: int,prefix: str=""):
            def stage(index: int,_total: int,name: str):
                if should_cancel():
                    raise InterruptedError("Preparation cancelled safely")
                label=f"{prefix}: {name}" if prefix and name else prefix or name
                if prefix=='Checking safe descriptions':task_eta.update('preferred-check','Description review',len(paths)-max(0,int(index)))
                elif prefix=='Applying safe descriptions':task_eta.update('preferred-save','Description saving',len(paths)-max(0,int(index)))
                if progress is not None:
                    progress(min(total,offset+max(0,int(index))),total,eta_label(label))
            stage.observe_inference=observe_inference
            return stage

        try:
            if options.read_external_evidence:
                preparation=BatchPreparationService(self.knowledge)
                for index,path in enumerate(paths,1):
                    if should_cancel():
                        raise InterruptedError("Preparation cancelled safely")
                    if history_operation is not None:
                        _preparation_record(path, native_kwargs["history_operation"])
                    item=preparation.assess(path)
                    if item.error:
                        result.errors.append(f"{path.name}: {item.error}")
                        outcomes[path].errors.append(str(item.error))
                    else:
                        try:
                            changed=preparation.apply(
                                item,
                                prepare_from_txt=True,
                                remember_evidence=True,
                                fill_gaps_from_metadata=False,
                                knowledge=self.knowledge,
                                mutation_callback=record_mutation,
                                **native_kwargs,
                            )
                        except Exception as exc:
                            if history_operation is not None and isinstance(exc, (HistoryError, HistoryConfigurationError, InterruptedError)):
                                raise
                            result.errors.append(f"{path.name}: {exc}")
                            outcomes[path].errors.append(str(exc))
                        else:
                            result.external_updates+=int(bool(changed))
                            outcomes[path].external_evidence_refreshed=bool(changed)
                            completed_stages[path].add("external")
                    task_eta.update('external','TXT/Metadata',len(paths)-index)
                    if progress is not None:
                        progress(index,total,eta_label(f"Reading TXT and Metadata: {path.name}"))
                current=evidence_units
                yield PreparationStageCheckpoint("external", current, total)

            maximum_age=(
                0.0 if options.force_all_ai else options.maximum_ai_age_seconds
            )
            if tag_backends:
                tag_result=self.ai_service.analyze_taggers(
                    paths,tag_backends,self.knowledge,report(current,"AI Tags"),
                    force=options.force_all_ai,
                    auto_merge=options.confirms_safe_tags,
                    minimum_agreement=options.tag_agreement_requirement(
                        minimum_tag_agreement
                    ),
                    allow_single_model_promotion=(
                        options.allows_single_model_promotion
                    ),
                    count_external_evidence_vote=(
                        options.uses_external_evidence_vote
                    ),
                    run_mode="forced" if options.force_all_ai else "preparation",
                    maximum_age_seconds=maximum_age,
                    should_cancel=should_cancel,
                    mutation_callback=record_mutation,
                    **native_kwargs,
                )
                result.tag_model_passes=tag_result.processed
                result.ai_passes_skipped+=tag_result.skipped_existing
                result.safe_tags_applied=tag_result.auto_merged
                result.conflicts_held=tag_result.conflicts_held
                result.review_paths.extend(tag_result.review_paths)
                result.errors.extend(tag_result.errors)
                for path,item in tag_result.image_results.items():
                    outcome=outcomes.setdefault(path,PreparationImageOutcome(path))
                    outcome.tag_model_passes+=item.processed
                    outcome.ai_passes_skipped+=item.skipped_existing
                    outcome.safe_tags_applied+=item.auto_merged
                    outcome.conflicts_held+=item.conflicts_held
                    outcome.review_required=outcome.review_required or item.review_required
                    outcome.errors.extend(item.errors)
                    if not item.errors and not tag_result.cancelled and not getattr(tag_result, "history_interrupted", False):
                        completed_stages[path].add("tags")
                result.cancelled=tag_result.cancelled
                result.history_interrupted=history_operation is not None and bool(getattr(tag_result, "history_interrupted", False))
                task_eta.complete('tag-review')
                for backend in tag_backends:
                    observe_inference(backend,0,'finished')
                current+=tag_units
                yield PreparationStageCheckpoint("tags", current, total)

            if description_backend is not None and not result.cancelled and not result.history_interrupted:
                description_result=self.ai_service.analyze_batch(
                    paths,description_backend,self.knowledge,
                    report(current,"AI Descriptions"),
                    force=options.force_all_ai,
                    run_mode="forced" if options.force_all_ai else "preparation",
                    maximum_age_seconds=maximum_age,
                    should_cancel=should_cancel,
                    mutation_callback=record_mutation,
                    return_partial_on_cancel=True,
                    **native_kwargs,
                )
                result.description_model_passes=description_result.processed
                result.ai_passes_skipped+=description_result.skipped_existing
                result.review_paths.extend(description_result.review_paths)
                result.errors.extend(description_result.errors)
                for path,item in description_result.image_results.items():
                    outcome=outcomes.setdefault(path,PreparationImageOutcome(path))
                    outcome.description_model_passes+=item.processed
                    outcome.ai_passes_skipped+=item.skipped_existing
                    outcome.review_required=outcome.review_required or item.review_required
                    outcome.errors.extend(item.errors)
                    if not item.errors and item.processed + item.skipped_existing:
                        completed_stages[path].add("description")
                result.cancelled=description_result.cancelled
                result.history_interrupted=history_operation is not None and bool(getattr(description_result, "history_interrupted", False))
                observe_inference(description_backend,0,'finished')
                current+=description_units
                yield PreparationStageCheckpoint("description", current, total)

            if preferred is not None and not result.cancelled and not result.history_interrupted:
                if history_operation is None:
                    preferred.recover_interrupted()
                preview=preferred.preview(
                    paths,progress=report(current,"Checking safe descriptions"),
                    should_cancel=should_cancel,
                    **native_kwargs,
                )
                if not preview.cancelled:task_eta.complete('preferred-check')
                current+=len(paths)
                if preview.cancelled:
                    result.cancelled=True
                else:
                    applied: PreferredCaptionPreparationApplyResult=preferred.apply(
                        preview,progress=report(current,"Applying safe descriptions"),
                        should_cancel=should_cancel,
                        mutation_callback=record_mutation,
                        **native_kwargs,
                    )
                    result.preferred_descriptions_applied=applied.changed
                    result.preferred_descriptions_held=(
                        applied.held+applied.stale+applied.failed
                    )
                    result.cancelled=applied.cancelled
                    result.history_interrupted=history_operation is not None and bool(getattr(applied, "history_interrupted", False))
                    result.errors.extend(
                        f"{item.image.name}: {item.error}"
                        for item in applied.items
                        if item.status in {"failed", "interrupted"} and item.error
                    )
                    for item in applied.items:
                        outcome=outcomes.setdefault(
                            item.image,PreparationImageOutcome(item.image),
                        )
                        if item.status=="changed":
                            outcome.preferred_description_applied=True
                        elif item.status in {"failed", "interrupted"} and item.error:
                            outcome.errors.append(item.error)
                        if item.status not in {"failed", "interrupted", "not_processed", "stale"}:
                            completed_stages[item.image].add("preferred")
                if not result.cancelled and not result.history_interrupted:task_eta.complete('preferred-save')
                current+=len(paths)
                yield PreparationStageCheckpoint("preferred", current, total)
        except (HistoryError, HistoryConfigurationError) as exc:
            if history_operation is None:
                if self.journal is not None and operation_id:
                    self.journal.finish(operation_id,"failed",message=str(exc))
                raise
            result.history_interrupted = True
            result.errors.append(str(exc))
        except InterruptedError:
            result.cancelled=True
        except Exception as exc:
            if self.journal is not None and operation_id:
                self.journal.finish(operation_id,"failed",message=str(exc))
            raise

        from .task_costs import cost_identity,cpu_profile,storage_device,cost_range,observe_cost
        timing_store=getattr(self.ai_service,'timing_store',None)
        final_identity=None
        if timing_store is not None:
            try:
                volumes={storage_device(path.parent) for path in paths}
                journal_device=storage_device(self.journal.path.parent) if self.journal is not None else 'none'
                hardware=cpu_profile()
                history_devices={storage_device(context.store.scope.history_root) for context in admitted.values()} if history_operation is not None else set()
                if None not in history_devices and None not in volumes and journal_device is not None and hardware is not None:
                    final_identity=cost_identity(timing_store,{'phase':'finalization','hardware':hardware,'volumes':sorted(volumes),
                        'journal_storage':journal_device,'history_storage':sorted(history_devices),'history':history_operation is not None,'journal':self.journal is not None,
                        'scope_bucket':max(1,len(paths)).bit_length(),'model_passes_bucket':max(1,result.tag_model_passes+result.description_model_passes).bit_length(),
                        'skipped_bucket':max(1,result.ai_passes_skipped).bit_length(),'errors':bool(result.errors)})
            except Exception:final_identity=None
        low,high,samples=cost_range(final_identity,1)
        task_eta.update('final',f'Result/history finalization ({samples} successful observations)',1,low,high)
        if progress is not None and not result.cancelled and not result.history_interrupted:
            progress(current,total,eta_label('Finalizing preparation results'))
        cost_clock=getattr(self,'_cost_clock',time.perf_counter);final_started=cost_clock()
        result.review_paths=list(dict.fromkeys(result.review_paths))
        review_set=set(result.review_paths)
        for path,outcome in outcomes.items():
            outcome.review_required=outcome.review_required or path in review_set
            outcome.cancelled=result.cancelled
        result.outcomes=[outcomes[path] for path in paths]
        if history_operation is not None:
            for path in paths:
                key = path_key(path)
                context = admitted.get(key)
                finished = required_stages.issubset(completed_stages[path])
                coverage = {"image": str(path), "operation_id": result.history_operation_id, "status": "held" if key in attempted else ("unprocessed" if result.cancelled or result.history_interrupted else "unchanged"), "version_ids": []}
                if context is not None:
                    # Receipts were frozen while the canonical commit's locks
                    # were still held. Cancellation/revocation must not cause
                    # new history or source reads merely to report the result.
                    coverage.update(entity_id=context.entity.entity_id, version_ids=list(context.version_ids),
                                    status="interrupted" if context.interrupted else (
                                        "held" if context.error or outcomes[path].errors else (
                                            ("complete" if finished else "partial") if context.version_ids else (
                                                "unchanged" if finished else "unprocessed"))))
                    result.history_interrupted = result.history_interrupted or context.interrupted
                    if context.error:
                        coverage["error"] = context.error
                result.history_coverage.append(coverage)
                if coverage["status"] in {"held", "interrupted", "unprocessed", "partial"}:
                    outcomes[path].errors.append(coverage.get("error") or "Preparation did not finish this image's admitted saved-history work.")
        if self.journal is not None and operation_id:
            snapshot=self.journal.store_snapshot([
                outcome.as_dict() for outcome in result.outcomes
            ])
            self.journal.finish(
                operation_id,"cancelled" if result.cancelled else "complete",
                message=(
                    "Paused safely; rerun the same scope and choices to resume."
                    if result.cancelled else "Preparation completed."
                ),
                result={
                    "snapshot":snapshot,
                    "counts":result.outcome_counts(),
                    "review_count":len(result.review_paths),
                    "error_count":len(result.errors),
                    "cancelled":result.cancelled,
                },
            )
            operation=self._operation(operation_id)
            result.changes_recorded=self._undoable_image_count(operation)
            self.journal.prune()
        if not result.cancelled and not result.history_interrupted and not result.errors:
            observe_cost(final_identity,max(0,cost_clock()-final_started))
        task_eta.complete('final')
        if progress is not None and (result.cancelled or result.history_interrupted):
            task_eta.state='cancelled' if result.cancelled else 'held'
            try:progress(current,total,eta_label('Preparation cancelled' if result.cancelled else 'Preparation needs history attention'))
            except InterruptedError:pass
        if progress is not None and not result.cancelled and not result.history_interrupted:
            counts=result.outcome_counts()
            failed=counts.get("failed",0)
            issues=bool(result.errors or failed or counts.get("partial",0) or counts.get("conflicted",0))
            label=("Preparation failed" if failed and failed==len(paths)
                   else "Preparation finished with issues" if issues
                   else "Preparation complete")
            progress(total,total,label)
        return result

    def undo(self, operation_id: str) -> PreparationUndoResult:
        if self.journal is None:
            raise ValueError("Preparation recovery is unavailable.")
        operation=self._operation(operation_id)
        if (
            operation.kind!=self.OPERATION_KIND
            or operation.status not in {"complete","cancelled"}
            or not operation.changes
        ):
            raise ValueError("That preparation batch is not available to undo.")

        prepared=[]
        for image,changes in self._group_changes(operation).items():
            initial=self.journal.load_snapshot(changes[0]["before"])
            states=[initial]
            for change in changes:
                states.append(self.journal.load_snapshot(change["after"]))
            try:
                current=self._current_snapshot(image)
            except Exception as exc:
                raise PreparationStaleError(
                    f"{image.name} is unavailable and cannot be checked before undo."
                ) from exc
            if current==initial:
                continue
            if current not in states[1:]:
                raise PreparationStaleError(
                    f"{image.name} changed after this batch, so it cannot be undone automatically."
                )
            prepared.append((
                image,current,initial,
                bool(changes[0]["metadata"].get("sidecar_existed",True)),
            ))

        undo_id=self.journal.begin(
            self.UNDO_KIND,expected=len(prepared),
            reason=f"Undo unified preparation batch {operation_id}",
            metadata={"restores":operation_id,"writes":"image_knowledge_only"},
        )
        restored=[]
        try:
            for image,current,initial,sidecar_existed in prepared:
                self.journal.append_change(
                    undo_id,item=str(image),before=current,after=initial,
                    reason="Undo unified image preparation",
                    metadata={"sidecar_existed":True},
                )
                self._restore(image,initial,sidecar_existed=sidecar_existed)
                restored.append((image,current))
        except Exception as exc:
            rollback_errors=[]
            for image,current in reversed(restored):
                try:
                    self._restore(image,current,sidecar_existed=True)
                except Exception as rollback_exc:
                    rollback_errors.append(f"{image.name}: {rollback_exc}")
            message=str(exc)
            if rollback_errors:
                message+="; automatic recovery also failed: "+"; ".join(rollback_errors)
            self.journal.finish(undo_id,"failed",message=message)
            raise RuntimeError(message) from exc
        self.journal.finish(undo_id,"complete")
        self.journal.finish(operation_id,"undone")
        self.journal.prune()
        return PreparationUndoResult(undo_id,len(restored))

    def recover_interrupted(self, *, exclude: set[str] | None=None) -> list[str]:
        if self.journal is None:
            return []
        excluded=set(exclude or ())
        recovered=[]
        for operation in self.journal.operations():
            if (
                operation.id in excluded
                or operation.kind!=self.OPERATION_KIND
                or operation.status!="applying"
            ):
                continue
            safe=self._operation_matches_current_state(operation)
            self.journal.finish(
                operation.id,"cancelled" if safe else "failed",
                message=(
                    "Recovered interrupted preparation; completed writes were kept."
                    if safe else
                    "Recovery stopped because current Image Knowledge diverged from the journal."
                ),
            )
            recovered.append(operation.id)
        return recovered

    def _run_signature(
        self,paths,options,tag_backends,description_backend,minimum_tag_agreement,
    ) -> str:
        return _canonical_digest({
            "paths":sorted(str(path.resolve()) for path in paths),
            "options":asdict(options),
            "tag_backends":[_backend_identity(value) for value in tag_backends],
            "description_backend":_backend_identity(description_backend),
            "tag_agreement":options.tag_agreement_requirement(minimum_tag_agreement),
            "policy_id":self.POLICY_ID,
            "policy_version":self.POLICY_VERSION,
        })

    def _resumable_operation(self, signature: str) -> JournalOperation | None:
        if self.journal is None:
            return None
        matches=[
            operation for operation in self.journal.operations()
            if operation.kind==self.OPERATION_KIND
            and operation.status in {"applying","cancelled"}
            and operation.metadata.get("signature")==signature
        ]
        if not matches:
            return None
        operation=matches[-1]
        if self._operation_matches_current_state(operation):
            return operation
        if operation.status=="applying":
            self.journal.finish(
                operation.id,"failed",
                message="Image Knowledge diverged before preparation could resume.",
            )
        return None

    def _operation_matches_current_state(self, operation: JournalOperation) -> bool:
        try:
            for image,changes in self._group_changes(operation).items():
                states=[]
                for change in changes:
                    states.append(self.journal.load_snapshot(change["before"]))
                    states.append(self.journal.load_snapshot(change["after"]))
                if self._current_snapshot(image) not in states:
                    return False
        except Exception:
            return False
        return True

    def _undoable_image_count(self, operation: JournalOperation) -> int:
        changed=0
        try:
            for image,changes in self._group_changes(operation).items():
                initial=self.journal.load_snapshot(changes[0]["before"])
                current=self._current_snapshot(image)
                if current!=initial:
                    changed+=1
        except Exception:
            return 0
        return changed

    @staticmethod
    def _group_changes(operation: JournalOperation) -> dict[Path,list[dict]]:
        grouped={}
        for change in operation.changes:
            grouped.setdefault(Path(change["item"]),[]).append(change)
        return grouped

    def _operation(self, operation_id: str) -> JournalOperation:
        if self.journal is None:
            raise ValueError("Preparation recovery is unavailable.")
        operation=next(
            (item for item in self.journal.operations() if item.id==operation_id),
            None,
        )
        if operation is None:
            raise ValueError("The requested preparation operation is unavailable.")
        return operation

    @staticmethod
    def _current_snapshot(image: Path) -> dict:
        if not image.is_file():
            raise FileNotFoundError(f"{image.name} is unavailable.")
        return ImageRecord(image).annotation.to_dict()

    @staticmethod
    def _restore(image: Path,snapshot: dict,*,sidecar_existed: bool) -> None:
        sidecar=image.with_suffix(".tags.json")
        if not sidecar_existed:
            sidecar.unlink(missing_ok=True)
            return
        record=ImageRecord(image,auto_load=False)
        record.annotation=ImageAnnotation.from_dict(copy.deepcopy(snapshot),image.name)
        record.loaded=True
        record.modified=True
        record.save_sidecar(force=True)
        if ImageRecord(image).annotation.to_dict()!=snapshot:
            raise RuntimeError(
                f"{image.name} could not be restored to its exact prior Image Knowledge."
            )
