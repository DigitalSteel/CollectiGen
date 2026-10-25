from __future__ import annotations

from dataclasses import dataclass,field,replace
from contextlib import nullcontext
from pathlib import Path

from .image_record import ImageRecord,ImageRecordEditSnapshot
from .image_fact_decisions import image_tag_identity
from .image_record_recovery import (
    ImageRecordRecoveryPlan,ImageRecordRecoveryService,
)


@dataclass(frozen=True)
class TrainingProjectionExclusionPlan:
    """One exact per-image projection decision, separate from image truth."""

    image: Path
    value: str
    exclude: bool
    disposition: str
    detail: str
    currently_excluded: bool
    recovery_plan: ImageRecordRecoveryPlan|None=None
    history_operation: object=field(default=None,compare=False,repr=False)

    @property
    def will_write(self) -> bool:
        return self.disposition=="change"


@dataclass(frozen=True)
class TrainingProjectionExclusionResult:
    status: str
    detail: str
    image: Path
    value: str
    excluded: bool
    record: ImageRecord|None=None
    recovery_manifest: Path|None=None


class TrainingProjectionExclusionService:
    """Safely change only ``export_excluded`` for one confirmed image Tag."""

    def __init__(self,*,knowledge=None,recovery_service=None,history_operation_factory=None) -> None:
        self.knowledge=knowledge
        self.recovery_service=(
            recovery_service or ImageRecordRecoveryService()
        )
        from .source_correction import SourceCorrectionHistory
        self._history=SourceCorrectionHistory(self,history_operation_factory)

    def plan(
        self,image: str|Path,value: str,*,exclude: bool=True,
    ) -> TrainingProjectionExclusionPlan:
        try:
            operation=self._history.operation(image)
            context=operation.context_for(image) if operation is not None else None
            with context.locked(Path(image)) if context is not None else nullcontext():
                if context is not None:
                    if context.store.has_pending(context.entity):
                        raise RuntimeError("Resolve interrupted saved history before previewing a training exclusion.")
                return replace(self._plan(image,value,exclude=exclude),history_operation=operation)
        except Exception as exc:
            return TrainingProjectionExclusionPlan(Path(image),str(value),bool(exclude),"held",str(exc),False)

    def _plan(self,image,value,*,exclude=True):
        image_path=Path(image).resolve()
        cleaned=str(value or "").strip()
        if not cleaned:
            return TrainingProjectionExclusionPlan(
                image_path,"",bool(exclude),"unsupported",
                "Choose one exact confirmed SDXL Tag.",False,
            )
        try:
            record=ImageRecord(image_path,known_tags=self.knowledge)
            snapshot=ImageRecordEditSnapshot.capture(record)
        except Exception as exc:
            return TrainingProjectionExclusionPlan(
                image_path,cleaned,bool(exclude),"unsupported",
                f"Image Knowledge is unavailable: {exc}",False,
            )

        wanted=image_tag_identity(cleaned,self.knowledge)
        confirmed=next((
            item for item in record.annotation.all_structured_tags()
            if image_tag_identity(item,self.knowledge)==wanted
        ),None)
        excluded_values=list(record.annotation.export_excluded)
        currently_excluded=any(
            image_tag_identity(item,self.knowledge)==wanted
            for item in excluded_values
        )
        if exclude and confirmed is None:
            return TrainingProjectionExclusionPlan(
                image_path,cleaned,True,"unsupported",
                "This exact value is not a confirmed Image Knowledge fact. "
                "Confirm it first; no training exclusion was staged.",
                currently_excluded,
            )
        if bool(exclude)==currently_excluded:
            detail=(
                "This confirmed fact is already omitted from future training projections."
                if exclude else
                "This fact is already eligible for future training projections."
            )
            return TrainingProjectionExclusionPlan(
                image_path,cleaned,bool(exclude),"current",detail,
                currently_excluded,
            )

        if exclude:
            record.annotation.export_excluded=[*excluded_values,confirmed]
            detail=(
                "Keep the confirmed fact and omit it from future training "
                "projections for this image. Existing TXT and Metadata remain unchanged."
            )
        else:
            record.annotation.export_excluded=[
                item for item in excluded_values
                if image_tag_identity(item,self.knowledge)!=wanted
            ]
            detail=(
                "Keep the confirmed fact and make it eligible for future "
                "training projections again. Existing TXT and Metadata remain unchanged."
            )
        record.modified=True
        try:
            recovery_plan=self.recovery_service.plan(snapshot,record)
        except Exception as exc:
            return TrainingProjectionExclusionPlan(
                image_path,cleaned,bool(exclude),"held",
                f"The projection decision could not be staged safely: {exc}",
                currently_excluded,
            )
        if recovery_plan.conflicts:
            return TrainingProjectionExclusionPlan(
                image_path,cleaned,bool(exclude),"held",
                "Image Knowledge changed while this exact projection decision "
                "was being prepared. Nothing was staged; review the newest image data.",
                currently_excluded,
            )
        return TrainingProjectionExclusionPlan(
            image_path,cleaned,bool(exclude),"change",detail,
            currently_excluded,recovery_plan,
        )

    def apply(
        self,plan: TrainingProjectionExclusionPlan,
    ) -> TrainingProjectionExclusionResult:
        if not plan.will_write or plan.recovery_plan is None:
            return TrainingProjectionExclusionResult(
                plan.disposition,plan.detail,plan.image,plan.value,
                plan.currently_excluded,
            )
        try:
            operation=self._history.admit(plan)
            context=operation.context_for(plan.image) if operation is not None else None
            service=self._history.recovery_service(context) if context is not None else self.recovery_service
            with context.locked(plan.image) if context is not None else nullcontext():
                if context is not None and context.store.has_pending(context.entity):
                    raise RuntimeError("Resolve interrupted saved history before applying a training exclusion.")
                recovered=service.apply(plan.recovery_plan,knowledge=self.knowledge)
            record=recovered.record
            wanted=image_tag_identity(plan.value,self.knowledge)
            excluded=any(
                image_tag_identity(item,self.knowledge)==wanted
                for item in record.annotation.export_excluded
            )
            confirmed=any(
                image_tag_identity(item,self.knowledge)==wanted
                for item in record.annotation.all_structured_tags()
            )
            if excluded!=plan.exclude or (plan.exclude and not confirmed):
                raise RuntimeError(
                    "The saved projection decision failed semantic read-back verification."
                )
        except Exception as exc:
            return TrainingProjectionExclusionResult(
                "held",str(exc),plan.image,plan.value,
                plan.currently_excluded,
            )
        return TrainingProjectionExclusionResult(
            "complete",plan.detail,plan.image,plan.value,excluded,record,
            recovered.recovery_manifest,
        )
