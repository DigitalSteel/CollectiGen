from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from typing import Any,Callable,Iterable,Mapping

from .category_registry import category_is_protected
from .tag_collection import normalize_tag
from .tag_template_expansion import (
    TAG_TEMPLATE_ALIAS_EQUIVALENT,TAG_TEMPLATE_EXACT_EXISTING,
    TAG_TEMPLATE_MISSING,TagTemplateExpansionReport,
)
from .tag_template_store import ReviewedTagTemplate


MAX_TAG_TEMPLATE_ORGANIZATION_CATEGORIES = 10_000

TAG_TEMPLATE_ORGANIZE_CREATE = "create_tag"
TAG_TEMPLATE_ORGANIZE_MOVE = "move_existing"
TAG_TEMPLATE_ORGANIZE_REUSE = "reuse_in_place"
TAG_TEMPLATE_ORGANIZE_HOLD = "hold"

TAG_TEMPLATE_ORGANIZATION_ACTION_LABELS = {
    TAG_TEMPLATE_ORGANIZE_CREATE:"Create Tag and place",
    TAG_TEMPLATE_ORGANIZE_MOVE:"Move existing Tag",
    TAG_TEMPLATE_ORGANIZE_REUSE:"Reuse in place",
    TAG_TEMPLATE_ORGANIZE_HOLD:"Held for review",
}


class TagTemplateOrganizationStaleError(RuntimeError):
    """Current authority no longer matches the approved organization scope."""


def _ordered_unique(values: Iterable[str]) -> tuple[str,...]:
    result=[]; seen=set()
    for value in values:
        text=str(value or "").strip()
        if text and text not in seen:
            seen.add(text); result.append(text)
    return tuple(result)


def derived_tag_template_category_id(
    parent_category_id: str,modifier_family_ids: Iterable[str],
) -> str:
    """Return identity from the stable parent and normalized dimension set."""

    parent=str(parent_category_id or "").strip()
    dimensions=tuple(sorted(set(map(str,modifier_family_ids))))
    if not parent or not dimensions or any(not value for value in dimensions):
        raise ValueError("A derived template destination needs one parent and typed dimension.")
    digest=sha256((parent+"\x1f"+"\x1f".join(dimensions)).encode("utf-8")).hexdigest()[:20]
    return f"tag_template_category.{digest}"


@dataclass(frozen=True)
class TagTemplateDestinationPlan:
    parent_category_id: str
    parent_category: str
    parent_base_status: str
    proposed_base_relationship: bool
    category_id: str
    category: str
    category_action: str
    collision_category_ids: tuple[str,...] = ()
    hold_reasons: tuple[str,...] = ()
    base_match_basis: str = ""
    modifier_family_ids: tuple[str,...] = ()
    reviewed_base_approval: bool = False

    @property
    def ready(self) -> bool:
        return bool(self.category_id and not self.hold_reasons)


@dataclass(frozen=True)
class TagTemplateOrganizationItem:
    result_id: str
    value_id: str
    value: str
    tag: str
    expansion_outcome: str
    existing_tag_ids: tuple[str,...]
    current_categories: tuple[str,...]
    destination_category_id: str
    destination_category: str
    action: str
    hold_reasons: tuple[str,...] = ()
    modifier_family_ids: tuple[str,...] = ()
    modifier_value_ids: tuple[str,...] = ()

    @property
    def action_label(self) -> str:
        return TAG_TEMPLATE_ORGANIZATION_ACTION_LABELS.get(self.action,self.action)

    @property
    def held(self) -> bool:
        return self.action==TAG_TEMPLATE_ORGANIZE_HOLD or bool(self.hold_reasons)

    @property
    def review_boundary(self) -> str:
        return (
            "Held: "+self.hold_reasons[0]
            if self.hold_reasons else
            "Ready in this read-only plan"
        )


@dataclass(frozen=True)
class TagTemplateOrganizationReport:
    template_id: str
    template_version: int
    template: str
    template_fingerprint: str
    expansion_fingerprint: str
    destination: TagTemplateDestinationPlan
    items: tuple[TagTemplateOrganizationItem,...]
    selected_result_ids: tuple[str,...]
    unselected_ready_count: int

    @property
    def action_counts(self) -> Mapping[str,int]:
        return dict(Counter(item.action for item in self.items))

    @property
    def ready_count(self) -> int:
        return sum(not item.held for item in self.items)

    @property
    def held_count(self) -> int:
        return sum(item.held for item in self.items)

    @property
    def plan_fingerprint(self) -> str:
        payload=(
            self.template_id,self.template_version,self.template_fingerprint,
            self.expansion_fingerprint,self.destination,
            tuple(self.items),self.selected_result_ids,self.unselected_ready_count,
        )
        return sha256(repr(payload).encode("utf-8")).hexdigest()


class TagTemplateOrganizationPlanningService:
    """Plan one selected template scope without changing System Knowledge."""

    def __init__(
        self,tags: Any,categories: Any,*,
        max_categories: int=MAX_TAG_TEMPLATE_ORGANIZATION_CATEGORIES,
    ) -> None:
        self.tags=tags
        self.categories=categories
        self.max_categories=max(1,int(max_categories))

    def _category_label(self,category_id: str) -> str:
        resolver=getattr(self.categories,"path_label",None)
        if callable(resolver):
            value=str(resolver(category_id) or "")
            if value:return value
        record=getattr(self.categories,"get",lambda _value:None)(category_id)
        return str(getattr(record,"name","") or category_id or "Unavailable")

    def _anchor_names(self,anchor_id: str) -> tuple[str,...]:
        resolver=getattr(self.tags,"identity_names",None)
        record=getattr(self.tags,"tags",{}).get(anchor_id)
        values=(
            resolver(record) if callable(resolver) and record is not None else
            (getattr(record,"display",anchor_id),*getattr(record,"aliases",()))
        )
        return _ordered_unique(normalize_tag(str(value)) for value in values)

    def _base_destination(
        self,record: ReviewedTagTemplate,
        progress: Callable[[int,int,str],None] | None,
        cancelled: Callable[[],bool],
    ) -> tuple[str,str,bool,str,tuple[str,...]]:
        roles=record.anchor_roles or tuple(
            "base" if index==0 else "component"
            for index in range(len(record.anchor_ids))
        )
        bases=tuple(
            stable_id for stable_id,role in zip(record.anchor_ids,roles)
            if role=="base"
        )
        if len(bases)!=1:
            return "","Primary Base Tag needs review",False,"",(
                "Choose exactly one literal anchor as the primary Base SDXL Tag before organizing this composite template.",
            )
        anchor_id=bases[0]
        reviewed=[]; compatibility=[]
        direct_resolver=getattr(self.categories,"direct_category_base",None)
        category_values=tuple(getattr(self.categories,"categories",{}).values())
        total=len(category_values)
        stride=max(1,total//200) if total else 1
        for index,category in enumerate(category_values,1):
            if cancelled():raise InterruptedError()
            if progress and (index==total or index%stride==0):
                progress(index,total,f"Resolving Category {category.name}")
            direct=direct_resolver(category.id) if callable(direct_resolver) else None
            if (
                direct is None or direct.ambiguous or direct.policy!="own"
                or direct.base_tag_id!=anchor_id
            ):
                continue
            if direct.represented and direct.scope_reviewed:
                reviewed.append(category)
            else:
                compatibility.append(category)
        matches=(*reviewed,*compatibility)
        if len(matches)>1:
            labels=", ".join(self._category_label(item.id) for item in matches)
            return "","Multiple Category Base destinations",False,"",(
                f"More than one Category declares the selected anchor as its Base Tag: {labels}.",
            )
        if reviewed:
            category=reviewed[0]
            return category.id,"Reviewed Category Base",False,"",()
        if compatibility:
            category=compatibility[0]
            return category.id,"Category Base needs current review",False,"",(
                f"{self._category_label(category.id)} identifies the anchor only through compatibility or unreviewed-scope authority.",
            )

        anchor_names=set(self._anchor_names(anchor_id))
        exact=tuple(
            category for category in category_values
            if normalize_tag(str(category.name)) in anchor_names
        )
        if len(exact)>1:
            labels=", ".join(self._category_label(item.id) for item in exact)
            return "","Ambiguous exact Category match",False,"",(
                f"More than one Category exactly matches the anchor name or declared Other Name: {labels}.",
            )
        if not exact:
            return "","No reviewed Category Base",False,"",(
                "No Category declares this stable anchor as its reviewed Base Tag, and no unique exact Category-name match is available.",
            )
        category=exact[0]
        direct=direct_resolver(category.id) if callable(direct_resolver) else None
        if direct is not None and direct.represented:
            return category.id,"Protected Category Base decision",False,"",(
                f"{self._category_label(category.id)} already has an explicit reviewed Category Base policy that does not select this anchor.",
            )
        if str(getattr(self.tags.tags.get(anchor_id),"category","") or "")!=category.id:
            return category.id,"Exact Category Base membership needs review",True,"",(
                f"The exact anchor is not a direct member of {self._category_label(category.id)}; use the Category Workspace before applying this proposal.",
            )
        basis=(
            "stable_id" if anchor_id==category.id else
            "exact_canonical_name"
            if normalize_tag(str(self.tags.tags[anchor_id].display))==normalize_tag(str(category.name))
            else "declared_alias"
        )
        reviewed=dict(record.destination_policy.get("approved_category_base",{}))
        approved=(
            str(reviewed.get("category_id") or "")==category.id
            and str(reviewed.get("target_id") or "")==anchor_id
            and str(reviewed.get("match_basis") or "")==basis
        )
        return category.id,"Reviewed exact Category Base proposal" if approved else "Proposed exact Category Base",True,basis,(
            () if approved else (
                f"Review the proposed Category Base relationship for {self._category_label(category.id)} before creating or moving Tags.",
            )
        )

    def _destination(
        self,record: ReviewedTagTemplate,report: TagTemplateExpansionReport,
        progress: Callable[[int,int,str],None] | None,
        cancelled: Callable[[],bool],
    ) -> TagTemplateDestinationPlan:
        parent_id,base_status,proposed_base,match_basis,holds=self._base_destination(
            record,progress,cancelled,
        )
        if not parent_id:
            return TagTemplateDestinationPlan(
                "","Unavailable",base_status,False,"","Unavailable","Held",(),holds,
                match_basis,(),False,
            )
        parent=self.categories.get(parent_id)
        parent_label=self._category_label(parent_id)
        dimensions=record.modifier_family_ids
        category_id=derived_tag_template_category_id(parent_id,dimensions)
        slot_labels=_ordered_unique(
            str(item.get("family_label") or item.get("family_id") or "")
            for item in record.slot_records
        )
        configured_name=str(record.destination_policy.get("label") or "").strip()
        dimension_label=(
            slot_labels[0] if len(slot_labels)==1 else
            " + ".join(slot_labels)+" Combinations"
        )
        proposed_name=configured_name or f"{parent.name} — {dimension_label}"
        existing=self.categories.get(category_id)
        collisions=tuple(
            item.id for item in self.categories.children_of(parent_id)
            if normalize_tag(str(item.name))==normalize_tag(proposed_name)
            and item.id!=category_id
        )
        reasons=list(holds)
        if existing is not None and existing.parent_id!=parent_id:
            reasons.append(
                f"Stable destination {category_id} exists under an incompatible parent Category."
            )
        if collisions:
            reasons.append(
                "The proposed destination label already belongs to a different child identity: "
                +", ".join(self._category_label(item) for item in collisions)+"."
            )
        category_label=(
            self._category_label(category_id) if existing is not None else
            f"{parent_label} → {proposed_name}"
        )
        action=(
            "Held" if reasons else
            "Reuse derived Category" if existing is not None else
            "Create derived Category"
        )
        return TagTemplateDestinationPlan(
            parent_id,parent_label,base_status,proposed_base,
            category_id,category_label,action,collisions,_ordered_unique(reasons),
            match_basis,dimensions,
            proposed_base and not any("Review the proposed Category Base" in item for item in reasons),
        )

    def preview(
        self,record: ReviewedTagTemplate,report: TagTemplateExpansionReport,
        progress: Callable[[int,int,str],None] | None=None,
        cancelled: Callable[[],bool] | None=None,
    ) -> TagTemplateOrganizationReport:
        cancelled=cancelled or (lambda:False)
        if not record.supported or not record.anchor_ids:
            raise TagTemplateOrganizationStaleError(
                "Only a supported reviewed structured template can be planned."
            )
        if len(getattr(self.categories,"categories",{}))>self.max_categories:
            raise TagTemplateOrganizationStaleError(
                f"Organization preview stops above {self.max_categories:,} Categories so destination resolution cannot become partial."
            )
        if getattr(report,"bounded_values",getattr(report,"bounded_results",0)):
            raise TagTemplateOrganizationStaleError(
                "Organization preview requires the complete reviewed-value expansion."
            )
        if report.approval_fingerprint!=record.expansion_fingerprint:
            raise TagTemplateOrganizationStaleError(
                "This approved template expansion changed. Reopen discovery and approve the current plan before organization preview."
            )
        current_candidate_fingerprint=(
            report.candidate.review_fingerprint
            if hasattr(report,"candidate") else report.draft.fingerprint
        )
        if current_candidate_fingerprint!=record.candidate_fingerprint:
            raise TagTemplateOrganizationStaleError(
                "This approved template family changed. Reopen discovery and approve current authority before organization preview."
            )
        by_id={item.id:item for item in report.items}
        missing=tuple(
            stable_id for stable_id in record.selected_result_ids
            if stable_id not in by_id
        )
        held=tuple(
            stable_id for stable_id in record.selected_result_ids
            if stable_id in by_id and by_id[stable_id].held
        )
        if missing or held:
            raise TagTemplateOrganizationStaleError(
                "The reviewed result selection contains missing or newly held results. Reopen and reapprove the template selection."
            )
        destination=self._destination(record,report,progress,cancelled)
        selected_set=set(record.selected_result_ids)
        selected=tuple(item for item in report.items if item.id in selected_set)
        items=[]; total=len(selected)
        for index,item in enumerate(selected,1):
            if cancelled():raise InterruptedError()
            reasons=list(destination.hold_reasons)
            existing=tuple(
                self.tags.tags[stable_id] for stable_id in item.existing_tag_ids
                if stable_id in self.tags.tags
            )
            current_categories=_ordered_unique(
                self._category_label(str(tag.category or "")) for tag in existing
            )
            if len(existing)>1:
                reasons.append("More than one existing stable Tag owns this selected result.")
            for tag in existing:
                current=self.categories.get(str(tag.category or ""))
                if (
                    current is not None and category_is_protected(current)
                    and current.id!=destination.category_id
                ):
                    reasons.append(
                        f"{tag.display} is assigned to protected Category {self._category_label(current.id)}."
                    )
            if reasons:
                action=TAG_TEMPLATE_ORGANIZE_HOLD
            elif item.outcome==TAG_TEMPLATE_MISSING:
                action=TAG_TEMPLATE_ORGANIZE_CREATE
            elif item.outcome in {
                TAG_TEMPLATE_EXACT_EXISTING,TAG_TEMPLATE_ALIAS_EQUIVALENT,
            } and len(existing)==1:
                action=(
                    TAG_TEMPLATE_ORGANIZE_REUSE
                    if str(existing[0].category or "")==destination.category_id else
                    TAG_TEMPLATE_ORGANIZE_MOVE
                )
            else:
                action=TAG_TEMPLATE_ORGANIZE_HOLD
                reasons.append(
                    "This selected expansion outcome is not eligible for deterministic organization."
                )
            items.append(TagTemplateOrganizationItem(
                item.id,item.value_id,item.value,item.generated_tag,item.outcome,
                item.existing_tag_ids,current_categories,destination.category_id,
                destination.category,action,_ordered_unique(reasons),
                tuple(getattr(item,"modifier_family_ids",(record.modifier_family_id,))),
                tuple(getattr(item,"modifier_value_ids",(item.value_id,))),
            ))
            if progress:
                progress(index,total,f"Planning {item.generated_tag}")
        return TagTemplateOrganizationReport(
            record.id,record.version,record.template,record.semantic_fingerprint,
            report.approval_fingerprint,destination,tuple(items),
            record.selected_result_ids,
            sum(not item.held and item.id not in selected_set for item in report.items),
        )
