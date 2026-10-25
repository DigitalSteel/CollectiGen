from __future__ import annotations

from dataclasses import dataclass,replace
from contextlib import nullcontext
from datetime import datetime,timezone
from hashlib import sha256
import json
from typing import Any,Mapping

from .operation_journal import OperationJournal
from .tag_template_organization import TagTemplateOrganizationReport
from .tag_template_organization_apply import (
    TAG_TEMPLATE_ORGANIZATION_APPLY_KIND,TagTemplateOrganizationApplyResult,
    TagTemplateOrganizationApplyService,
)
from .tag_template_store import (
    ReviewedTagTemplate,TagTemplateApprovalResult,TagTemplateStoreService,
)


TAG_TEMPLATE_GENERATION_KIND="tag_template_generation"
TAG_TEMPLATE_GENERATION_UNDO_KIND="tag_template_generation_undo"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fingerprint(payload: Any) -> str:
    return sha256(json.dumps(
        payload,sort_keys=True,separators=(",",":"),ensure_ascii=False,
    ).encode("utf-8")).hexdigest()


def tag_template_policy_scope_fingerprint(record: ReviewedTagTemplate) -> str:
    """Identify reviewed generation/fact scope without its mutable version."""
    return record.policy_scope_fingerprint


@dataclass(frozen=True)
class TagTemplatePolicyPreview:
    template_id: str
    template_version: int
    generation_enabled: bool
    fact_expansion_enabled: bool
    selected_result_count: int
    max_per_run: int
    create_count: int
    move_count: int
    reuse_count: int
    held_count: int
    organization_plan_fingerprint: str
    scope_fingerprint: str

    @property
    def ready_for_generation(self) -> bool:
        return bool(
            self.generation_enabled and self.selected_result_count
            and not self.held_count and self.organization_plan_fingerprint
        )


class TagTemplatePolicyService:
    """Review template-specific automation flags independently of global policy."""

    def __init__(self,repository: Any) -> None:
        self.repository=repository

    @staticmethod
    def preview(
        record: ReviewedTagTemplate,plan: TagTemplateOrganizationReport|None,*,
        generation_enabled: bool,fact_expansion_enabled: bool,
    ) -> TagTemplatePolicyPreview:
        counts=plan.action_counts if plan is not None else {}
        if plan is not None and (
            plan.template_id!=record.id or plan.template_version!=record.version
        ):
            raise RuntimeError(
                "The organization plan belongs to another template version. Review a current plan before enabling automation."
            )
        if len(record.selected_result_ids)>record.max_per_run:
            raise ValueError("The reviewed template selection exceeds its stored per-run limit.")
        return TagTemplatePolicyPreview(
            record.id,record.version,bool(generation_enabled),
            bool(fact_expansion_enabled),len(record.selected_result_ids),
            record.max_per_run,int(counts.get("create_tag",0)),
            int(counts.get("move_existing",0)),int(counts.get("reuse_in_place",0)),
            int(counts.get("hold",0)),plan.plan_fingerprint if plan is not None else "",
            tag_template_policy_scope_fingerprint(record),
        )

    def approve(
        self,record: ReviewedTagTemplate,preview: TagTemplatePolicyPreview,
        *,expected_current_fingerprint: str="",
    ) -> TagTemplateApprovalResult:
        if expected_current_fingerprint:
            current=next((
                item for _origin,item in
                TagTemplateStoreService(self.repository).records_with_origin()
                if item.id==record.id
            ),None)
            if (
                current is None
                or current.semantic_fingerprint!=expected_current_fingerprint
            ):
                raise RuntimeError(
                    "The reviewed template changed before policy approval."
                )
            reconciled=replace(
                current,expansion_fingerprint=record.expansion_fingerprint,
                selection_fingerprint=record.selection_fingerprint,
            )
            if reconciled.semantic_fingerprint!=record.semantic_fingerprint:
                raise RuntimeError(
                    "Policy review may refresh current materialization fingerprints but cannot replace another template field."
                )
        if preview.template_id!=record.id or preview.template_version!=record.version:
            raise RuntimeError("The reviewed template changed after the policy preview opened.")
        current_scope=tag_template_policy_scope_fingerprint(record)
        if preview.scope_fingerprint!=current_scope:
            raise RuntimeError("The reviewed template scope changed before policy approval.")
        if preview.generation_enabled and not preview.ready_for_generation:
            raise ValueError(
                "Automatic generation needs a nonempty all-ready organization plan within the reviewed per-run limit."
            )
        if preview.fact_expansion_enabled and not record.selected_result_ids:
            raise ValueError(
                "Automatic fact expansion needs a reviewed materialized target scope."
            )
        next_version=record.version+1; now=_utc_now()
        review_core={
            "schema_version":1,"template_id":record.id,
            "template_version":next_version,"scope_fingerprint":current_scope,
            "reviewed_at":now,"generation":{
                "enabled":preview.generation_enabled,
                "organization_plan_fingerprint":preview.organization_plan_fingerprint,
                "selected_result_count":preview.selected_result_count,
                "max_per_run":preview.max_per_run,
                "create_count":preview.create_count,"move_count":preview.move_count,
                "reuse_count":preview.reuse_count,"held_count":preview.held_count,
            },"fact_expansion":{
                "enabled":preview.fact_expansion_enabled,
                "review_by_default":True,"owner_scope_required":True,
                "confirmed_absent_guard":True,"rejection_guard":True,
                "max_suggestions_per_run":preview.max_per_run,
            },
        }
        review_core["fingerprint"]=_fingerprint(review_core)
        proposed=replace(
            record,version=next_version,reviewed_at=now,
            generation_enabled=preview.generation_enabled,
            fact_expansion_enabled=preview.fact_expansion_enabled,
            policy_review=review_core,
        )
        proposed.validate()
        return TagTemplateStoreService(self.repository).save_record(
            proposed,expected_current_fingerprint=(
                expected_current_fingerprint or record.semantic_fingerprint
            ),
        )


class TagTemplateGenerationService:
    """Run reviewed generation through organization's exact transaction/Undo."""

    def __init__(
        self,organization: TagTemplateOrganizationApplyService,
        journal_path: Any,
    ) -> None:
        self.organization=organization; self.journal=OperationJournal(journal_path)

    def _admission(self):
        admission=getattr(self.organization,"rule_pair_admission",None)
        return admission.admit() if admission else nullcontext()

    def run(
        self,record: ReviewedTagTemplate,expansion: Any,
        plan: TagTemplateOrganizationReport,**kwargs,
    ) -> TagTemplateOrganizationApplyResult:
        if isinstance(self.organization,TagTemplateOrganizationApplyService):
            self.organization._check_supplied_binding(kwargs.get("tags"))
        with self._admission():
            return self._run_admitted(record,expansion,plan,**kwargs)

    def _run_admitted(
        self,record: ReviewedTagTemplate,expansion: Any,
        plan: TagTemplateOrganizationReport,**kwargs,
    ) -> TagTemplateOrganizationApplyResult:
        if not record.generation_enabled:
            raise ValueError("Automatic generation is off for this reviewed template.")
        review=dict(record.policy_review.get("generation",{}))
        if not bool(review.get("enabled",False)):
            raise ValueError("This template has no separately reviewed generation policy.")
        if tag_template_policy_scope_fingerprint(record)!=str(
            record.policy_review.get("scope_fingerprint") or ""
        ):
            raise RuntimeError("The reviewed generation scope is stale.")
        if plan.template_id!=record.id or plan.template_version!=record.version:
            raise RuntimeError("Review a current organization plan before automatic generation.")
        if plan.held_count or len(plan.items)>record.max_per_run:
            raise ValueError("Automatic generation is held or exceeds its reviewed per-run limit.")
        operation_id=self.journal.begin(
            TAG_TEMPLATE_GENERATION_KIND,expected=1,
            reason="Run separately reviewed reusable Tag generation",
            metadata={
                "template_id":record.id,"template_version":record.version,
                "scope_fingerprint":tag_template_policy_scope_fingerprint(record),
                "plan_fingerprint":plan.plan_fingerprint,
            },
        )
        try:
            result=self.organization.apply(record,expansion,plan,**kwargs)
            self.journal.append_checkpoint(
                operation_id,item=result.operation_id,metadata={
                    "created":len(result.created_tag_ids),"moved":len(result.moved_tag_ids),
                    "reused":len(result.reused_tag_ids),"changed":result.changed,
                },
            )
        except BaseException as exc:
            self.journal.finish(
                operation_id,"cancelled" if isinstance(exc,InterruptedError) else "failed",
                message=str(exc),
            ); raise
        self.journal.finish(operation_id,"complete",result={
            "organization_operation_id":result.operation_id,
            "created":len(result.created_tag_ids),"moved":len(result.moved_tag_ids),
            "reused":len(result.reused_tag_ids),"changed":result.changed,
        })
        self.journal.prune(); return result

    def recover_interrupted(self) -> tuple[str,...]:
        with self._admission():
            return self._recover_interrupted_admitted()

    def _recover_interrupted_admitted(self) -> tuple[str,...]:
        recovered=list(self.organization.recover_interrupted())
        organization_operations=tuple(
            item for item in self.journal.operations()
            if item.kind==TAG_TEMPLATE_ORGANIZATION_APPLY_KIND
        )
        for operation in self.journal.operations():
            if operation.kind!=TAG_TEMPLATE_GENERATION_KIND or operation.status!="applying":continue
            candidates=tuple(
                item for item in organization_operations
                if item.created_at>=operation.created_at
                and str(item.metadata.get("template_id") or "")==str(operation.metadata.get("template_id") or "")
                and int(item.metadata.get("template_version") or 0)==int(operation.metadata.get("template_version") or 0)
            )
            child=candidates[-1] if candidates else None
            if child is not None and child.status=="complete":
                self.journal.finish(operation.id,"complete",message="Recovered completed reviewed generation.",result={
                    "organization_operation_id":child.id,"recovered":True,
                    "created":len(child.result.get("created_tag_ids",())),
                    "moved":len(child.result.get("moved_tag_ids",())),
                    "reused":len(child.result.get("reused_tag_ids",())),
                })
            else:
                self.journal.finish(
                    operation.id,"failed",
                    message="Recovered generation history; no complete linked organization write was found.",
                )
            recovered.append(operation.id)
        if recovered:self.journal.prune()
        return tuple(recovered)

    def undo_last(self) -> str:
        with self._admission():
            return self._undo_last_admitted()

    def _undo_last_admitted(self) -> str:
        generation=self.journal.latest(
            kind=TAG_TEMPLATE_GENERATION_KIND,statuses=("complete",),
        )
        if generation is None:
            raise ValueError("There is no completed automatic reusable Tag generation to restore.")
        child_id=str(generation.result.get("organization_operation_id") or "")
        child=self.organization.undoable_operation()
        if child is None or child.id!=child_id:
            raise RuntimeError(
                "Newer System Knowledge work exists; restore it from its own reviewed history before this generation."
            )
        undo_id=self.journal.begin(
            TAG_TEMPLATE_GENERATION_UNDO_KIND,expected=1,
            reason="Restore reviewed automatic reusable Tag generation",
            metadata={"restores":generation.id,"organization_operation_id":child_id},
        )
        try:
            restored=self.organization.undo_last()
            self.journal.append_checkpoint(undo_id,item=restored)
        except BaseException as exc:
            self.journal.finish(undo_id,"failed",message=str(exc)); raise
        self.journal.finish(undo_id,"complete",result={"restores":generation.id})
        self.journal.finish(generation.id,"undone")
        self.journal.prune(); return generation.id


__all__=[
    "TAG_TEMPLATE_GENERATION_KIND","TAG_TEMPLATE_GENERATION_UNDO_KIND",
    "TagTemplateGenerationService","TagTemplatePolicyPreview",
    "TagTemplatePolicyService","tag_template_policy_scope_fingerprint",
]
