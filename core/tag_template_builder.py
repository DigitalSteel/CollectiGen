from __future__ import annotations

from collections import Counter
from dataclasses import dataclass,field
from datetime import datetime,timezone
from hashlib import sha256
from itertools import combinations,product
import json
from typing import Any,Callable,Iterable,Mapping

from .category_registry import exact_category_anchor_ids
from .tag_collection import normalize_tag
from .tag_template_discovery import MAX_TAG_TEMPLATE_DISCOVERY_TAGS
from .tag_template_expansion import (
    MAX_TAG_TEMPLATE_EXPANSION_VALUES,
    TAG_TEMPLATE_ALIAS_EQUIVALENT,
    TAG_TEMPLATE_CONFLICTING,
    TAG_TEMPLATE_EXACT_EXISTING,
    TAG_TEMPLATE_EXCLUDED,
    TAG_TEMPLATE_EXPANSION_LABELS,
    TAG_TEMPLATE_IGNORED,
    TAG_TEMPLATE_INVALID,
    TAG_TEMPLATE_MISSING,
    TAG_TEMPLATE_TOMBSTONED,
)
from .tag_template_store import (
    DEFAULT_TAG_TEMPLATE_MAX_PER_RUN,
    TAG_TEMPLATE_FORMAT_VERSION,
    ReviewedTagTemplate,
    TagTemplateApprovalResult,
    TagTemplateStoreService,
    tag_template_selection_fingerprint,
)


MAX_TAG_TEMPLATE_SLOTS = 6
MAX_TAG_TEMPLATE_ANCHORS = 6
MAX_TAG_TEMPLATE_POTENTIAL = 200_000


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fingerprint(payload: Any) -> str:
    return sha256(json.dumps(
        payload,sort_keys=True,separators=(",",":"),ensure_ascii=False,
    ).encode("utf-8")).hexdigest()


def _ordered(values: Iterable[str]) -> tuple[str,...]:
    result=[]; seen=set()
    for value in values:
        text=str(value or "").strip()
        if text and text not in seen:
            seen.add(text); result.append(text)
    return tuple(result)


def _join_token_parts(parts: Iterable[tuple[str,str]]) -> str:
    rendered=""; previous_kind=""
    for kind,text in parts:
        if not text:continue
        if kind=="separator":
            rendered=rendered.rstrip()+text; previous_kind=kind; continue
        if rendered and previous_kind!="separator":rendered+=" "
        rendered+=text; previous_kind=kind
    return " ".join(rendered.split())


@dataclass(frozen=True)
class TagTemplateAnchor:
    tag_id: str
    label: str
    role: str = "base"

    def validate(self) -> None:
        if not self.tag_id or not self.label:
            raise ValueError("Every literal chip needs one stable SDXL Tag.")
        if self.role not in {"base","component","implication"}:
            raise ValueError("Choose Base, component, or implication for every literal chip.")


@dataclass(frozen=True)
class TagTemplateSlot:
    id: str
    family_id: str
    family_label: str
    placeholder: str
    included_value_ids: tuple[str,...]
    excluded_value_ids: tuple[str,...] = ()
    invalid_value_ids: tuple[str,...] = ()
    held_value_ids: tuple[str,...] = ()
    required: bool = False
    extras: Mapping[str,Any] = field(default_factory=dict,repr=False,compare=False)

    def validate(self) -> None:
        if not self.id or not self.family_id or not self.family_label:
            raise ValueError("Every typed selector needs stable slot and modifier-family identities.")
        if not self.placeholder.strip().strip("{}"):
            raise ValueError("Every typed selector needs a visible placeholder.")
        included=set(self.included_value_ids); excluded=set(self.excluded_value_ids)
        invalid=set(self.invalid_value_ids)
        if not included or included&excluded or included&invalid or excluded&invalid:
            raise ValueError("Each typed selector needs disjoint reviewed include, exclude, and invalid values.")
        if not set(self.held_value_ids).issubset(included|excluded|invalid):
            raise ValueError("Held slot values must belong to the reviewed value scope.")

    def to_dict(self) -> dict[str,Any]:
        payload=dict(self.extras); payload.update({
            "kind":"modifier_slot","id":self.id,"family_id":self.family_id,
            "family_label":self.family_label,
            "placeholder":self.placeholder.strip().strip("{}"),
            "included_value_ids":list(self.included_value_ids),
            "excluded_value_ids":list(self.excluded_value_ids),
            "invalid_value_ids":list(self.invalid_value_ids),
            "held_value_ids":list(self.held_value_ids),"required":self.required,
        }); return payload


@dataclass(frozen=True)
class TagTemplateToken:
    kind: str
    reference_id: str = ""
    text: str = ""
    extras: Mapping[str,Any] = field(default_factory=dict,repr=False,compare=False)

    def to_dict(self) -> dict[str,Any]:
        payload=dict(self.extras); payload["kind"]=self.kind
        if self.kind=="anchor":payload["tag_id"]=self.reference_id
        elif self.kind=="modifier_slot":payload["slot_id"]=self.reference_id
        elif self.kind=="separator":payload["text"]=self.text
        return payload


@dataclass(frozen=True)
class TagTemplateDraft:
    anchors: tuple[TagTemplateAnchor,...]
    slots: tuple[TagTemplateSlot,...]
    tokens: tuple[TagTemplateToken,...]
    template_id: str = ""
    version: int = 0
    constraints: Mapping[str,Any] = field(default_factory=dict)
    destination_policy: Mapping[str,Any] = field(default_factory=dict)
    max_per_run: int = DEFAULT_TAG_TEMPLATE_MAX_PER_RUN

    def validate(self) -> None:
        if not 1<=len(self.anchors)<=MAX_TAG_TEMPLATE_ANCHORS:
            raise ValueError(f"Use between one and {MAX_TAG_TEMPLATE_ANCHORS} literal Tag chips.")
        if not 1<=len(self.slots)<=MAX_TAG_TEMPLATE_SLOTS:
            raise ValueError(f"Use between one and {MAX_TAG_TEMPLATE_SLOTS} typed wildcard selectors.")
        for anchor in self.anchors:anchor.validate()
        if len({item.tag_id for item in self.anchors})!=len(self.anchors):
            raise ValueError("A literal Tag belongs in the chip order once; reuse its stable chip instead of duplicating authority.")
        if sum(item.role=="base" for item in self.anchors)>1:
            raise ValueError("A composite template may have at most one primary Base SDXL Tag.")
        for slot in self.slots:slot.validate()
        if len({item.id for item in self.slots})!=len(self.slots):
            raise ValueError("Every typed selector needs a distinct stable slot identity.")
        anchor_ids={item.tag_id for item in self.anchors}; slot_ids={item.id for item in self.slots}
        seen_anchors=set(); seen_slots=set()
        for token in self.tokens:
            if token.kind=="anchor" and token.reference_id in anchor_ids:
                seen_anchors.add(token.reference_id)
            elif token.kind=="modifier_slot" and token.reference_id in slot_ids:
                seen_slots.add(token.reference_id)
            elif token.kind=="separator" and token.text and not any(
                character.isalnum() for character in token.text
            ):
                continue
            else:
                raise ValueError("The ordered chip plan contains an unresolved Tag, selector, or separator.")
        if seen_anchors!=anchor_ids or seen_slots!=slot_ids:
            raise ValueError("Every literal Tag and typed selector must appear in the ordered chip plan.")
        if not 1<=int(self.max_per_run)<=MAX_TAG_TEMPLATE_EXPANSION_VALUES:
            raise ValueError("The per-run limit is outside the supported bounded range.")

    @property
    def fingerprint(self) -> str:
        return _fingerprint({
            "anchors":[vars(item) for item in self.anchors],
            "slots":[item.to_dict() for item in self.slots],
            "tokens":[item.to_dict() for item in self.tokens],
            "constraints":dict(self.constraints),
            "destination_policy":dict(self.destination_policy),
            "max_per_run":self.max_per_run,
        })

    @property
    def display_template(self) -> str:
        anchors={item.tag_id:item.label for item in self.anchors}
        slots={item.id:"{"+item.placeholder.strip().strip("{}")+"}" for item in self.slots}
        parts=[]
        for token in self.tokens:
            if token.kind=="anchor":parts.append((token.kind,anchors[token.reference_id]))
            elif token.kind=="modifier_slot":parts.append((token.kind,slots[token.reference_id]))
            else:parts.append((token.kind,token.text))
        return _join_token_parts(parts)


@dataclass(frozen=True)
class TagTemplateBuildItem:
    id: str
    active_slot_ids: tuple[str,...]
    slot_values: tuple[tuple[str,str,str,str],...]
    generated_tag: str
    normalized_tag: str
    outcome: str
    identity_basis: str
    existing_tag_ids: tuple[str,...] = ()
    existing_tags: tuple[str,...] = ()
    current_categories: tuple[str,...] = ()
    hold_reasons: tuple[str,...] = ()

    @property
    def held(self) -> bool:
        return bool(self.hold_reasons) or self.outcome in {
            TAG_TEMPLATE_EXCLUDED,TAG_TEMPLATE_IGNORED,TAG_TEMPLATE_TOMBSTONED,
            TAG_TEMPLATE_INVALID,TAG_TEMPLATE_CONFLICTING,
        }

    @property
    def outcome_label(self) -> str:
        return TAG_TEMPLATE_EXPANSION_LABELS.get(self.outcome,self.outcome)

    @property
    def review_boundary(self) -> str:
        if self.held:
            return "Held: "+(self.hold_reasons[0] if self.hold_reasons else self.outcome_label)
        return (
            "Ready for reviewed creation planning"
            if self.outcome==TAG_TEMPLATE_MISSING else
            "Ready for reviewed reuse and organization planning"
        )

    @property
    def modifier_family_ids(self) -> tuple[str,...]:
        return _ordered(item[1] for item in self.slot_values)

    @property
    def modifier_value_ids(self) -> tuple[str,...]:
        return tuple(item[2] for item in self.slot_values)

    @property
    def value_id(self) -> str:
        return self.modifier_value_ids[0] if self.modifier_value_ids else ""

    @property
    def value(self) -> str:
        return " + ".join(item[3] for item in self.slot_values)


@dataclass(frozen=True)
class TagTemplateBuildReport:
    draft: TagTemplateDraft
    items: tuple[TagTemplateBuildItem,...]
    potential_count: int
    bounded_results: int
    policy_fingerprint: str

    @property
    def outcome_counts(self) -> Mapping[str,int]:
        return dict(Counter(item.outcome for item in self.items))

    @property
    def ready_count(self) -> int:
        return sum(not item.held for item in self.items)

    @property
    def held_count(self) -> int:
        return sum(item.held for item in self.items)

    @property
    def approval_fingerprint(self) -> str:
        return _fingerprint({
            "draft":self.draft.fingerprint,"potential_count":self.potential_count,
            "bounded_results":self.bounded_results,
            "policy_fingerprint":self.policy_fingerprint,
            "items":[{
                "id":item.id,"active":item.active_slot_ids,
                "values":item.slot_values,"tag":item.generated_tag,
                "normalized":item.normalized_tag,"outcome":item.outcome,
                "identity_basis":item.identity_basis,
                "existing":item.existing_tag_ids,"holds":item.hold_reasons,
            } for item in self.items],
        })


class TagTemplateBuilderService:
    """Preview structured multi-slot templates without mutating any authority."""

    def __init__(
        self,tags: Any,categories: Any,foundation: Any,*,
        policy_resolver: Any=None,composition_resolver: Any=None,
        modifier_roles: Mapping[str,Any]|None=None,
        ignore_values: Iterable[str]=(),
        max_results: int=MAX_TAG_TEMPLATE_EXPANSION_VALUES,
        max_potential: int=MAX_TAG_TEMPLATE_POTENTIAL,
    ) -> None:
        self.tags=tags; self.categories=categories; self.foundation=foundation
        self.policy_resolver=policy_resolver
        self.composition_resolver=composition_resolver
        self.modifier_roles=dict(modifier_roles or {})
        self.ignore_values={normalize_tag(item) for item in ignore_values if normalize_tag(item)}
        self.max_results=max(1,int(max_results)); self.max_potential=max(1,int(max_potential))
        resolver=getattr(categories,"exact_category_anchor_ids",None)
        self._derived_base_anchors=(
            dict(resolver()) if callable(resolver) else exact_category_anchor_ids(
                getattr(categories,"categories",{}).values(),
                getattr(tags,"tags",{}).values(),
            )
        )

    def _category_label(self,category_id: str) -> str:
        resolver=getattr(self.categories,"path_label",None)
        if callable(resolver):
            label=str(resolver(category_id) or "")
            if label:return label
        record=getattr(self.categories,"get",lambda _value:None)(category_id)
        return str(getattr(record,"name","") or category_id or "Uncategorized")

    def _identity_indexes(self):
        canonical={}; aliases={}
        for record in self.tags.tags.values():
            key=normalize_tag(str(record.display))
            if key:canonical.setdefault(key,set()).add(str(record.id))
            for alias in self.tags.effective_aliases(record):
                key=normalize_tag(str(alias))
                if key:aliases.setdefault(key,set()).add(str(record.id))
        return (
            {key:tuple(sorted(values)) for key,values in canonical.items()},
            {key:tuple(sorted(values)) for key,values in aliases.items()},
        )

    def _removed_index(self):
        names={}; archived=set()
        for archive in getattr(self.tags,"active_removed_archives",lambda:())():
            for stable_id in getattr(archive,"tag_ids",()):
                stable_id=str(stable_id); archived.add(stable_id)
                record=archive.tag_record(stable_id)
                if record is None:continue
                for name in (record.display,*record.aliases):
                    key=normalize_tag(str(name))
                    if key:names.setdefault(key,[]).append(stable_id)
        return ({key:tuple(_ordered(values)) for key,values in names.items()},
                set(getattr(self.tags,"removed_tag_ids",set()))-archived)

    @staticmethod
    def _removed_ids(key,index,legacy):
        matches=list(index.get(key,())); slug="_".join(key.split())
        matches.extend(item for item in sorted(legacy) if item==slug or (
            item.startswith(slug+"_") and item[len(slug)+1:].isdigit()
        ))
        return tuple(_ordered(matches))

    def _policy_for(self,anchor_id: str):
        resolver=getattr(self.policy_resolver,"effective_for_tag",None)
        if callable(resolver):return resolver(anchor_id)
        return self.foundation.effective_modifier_policy(
            self.tags.tags.get(anchor_id),self.tags,self.categories,
        )

    def _allows(self,policy: Any,family_id: str,value: Any) -> bool:
        resolver=getattr(self.policy_resolver,"allows_value",None)
        if callable(resolver):return bool(resolver(policy,family_id,str(value.id)))
        allows=getattr(policy,"allows_value",None)
        path=getattr(self.categories,"path_ids",lambda _value:())(
            str(getattr(self.tags.tags.get(str(value.id)),"category","") or "")
        )
        return bool(allows(family_id,str(value.id),tuple(path))) if callable(allows) else (
            family_id in tuple(getattr(policy,"groups",()))
        )

    def _resolutions(self):
        resolver=getattr(self.composition_resolver,"resolutions",None)
        if callable(resolver):return resolver()
        return self.composition_resolver if isinstance(self.composition_resolver,Mapping) else {}

    @staticmethod
    def _forbidden(selected: Mapping[str,str],constraints: Mapping[str,Any]) -> bool:
        for raw in constraints.get("forbidden_combinations",()):
            if not isinstance(raw,Mapping):continue
            if raw and all(selected.get(str(key))==str(value) for key,value in raw.items()):
                return True
        allowed=tuple(item for item in constraints.get("allowed_combinations",()) if isinstance(item,Mapping))
        if allowed and not any(
            all(str(rule.get(key))==value for key,value in selected.items())
            for rule in allowed
        ):
            return True
        return False

    def _render(self,draft: TagTemplateDraft,selected: Mapping[str,tuple[str,str,str]]):
        anchors={item.tag_id:item.label for item in draft.anchors}; parts=[]
        for token in draft.tokens:
            if token.kind=="anchor":parts.append((token.kind,anchors[token.reference_id]))
            elif token.kind=="modifier_slot":
                value=selected.get(token.reference_id)
                if value is not None:parts.append((token.kind,value[2]))
            else:parts.append((token.kind,token.text))
        return _join_token_parts(parts)

    def _existing_holds(
        self,owner_ids: Iterable[str],draft: TagTemplateDraft,
        values: tuple[tuple[str,str,str,str],...],resolutions: Mapping[str,Mapping[str,Any]],
    ) -> tuple[str,...]:
        reasons=[]
        base=next((item for item in draft.anchors if item.role=="base"),None)
        for owner_id in owner_ids:
            record=self.tags.tags.get(owner_id)
            if record is None:
                reasons.append(f"Existing owner {owner_id} is unavailable."); continue
            if base is not None:
                base_id,_source,kind=self.tags.effective_base_tag(
                    record,self.categories,derived_anchors=self._derived_base_anchors,
                )
                if str(base_id or "")!=base.tag_id:
                    reasons.append(
                        f"{record.display} does not have the selected reviewed Base Tag ({kind.replace('_',' ') or 'none'})."
                    )
            by_family={family_id:value_id for _slot,family_id,value_id,_label in values}
            for family_id,value_id in by_family.items():
                slot=resolutions.get(owner_id,{}).get(family_id)
                if slot is not None and (
                    not bool(getattr(slot,"is_set",False))
                    or str(getattr(slot,"value_id","") or "")!=value_id
                ):
                    reasons.append(
                        f"{record.display} has a conflicting reviewed {family_id} modifier value."
                    )
        return _ordered(reasons)

    def preview(
        self,draft: TagTemplateDraft,
        progress: Callable[[int,int,str],None]|None=None,
        cancelled: Callable[[],bool]|None=None,
    ) -> TagTemplateBuildReport:
        cancelled=cancelled or (lambda:False); draft.validate()
        if len(self.tags.tags)>MAX_TAG_TEMPLATE_DISCOVERY_TAGS:
            raise ValueError(
                f"Template preview stops above {MAX_TAG_TEMPLATE_DISCOVERY_TAGS:,} current Tags so identity deduplication cannot become partial."
            )
        anchor_by_id={item.tag_id:self.tags.tags.get(item.tag_id) for item in draft.anchors}
        if any(record is None for record in anchor_by_id.values()):
            raise ValueError("A literal Tag chip is no longer available in System Knowledge.")
        definitions=self.foundation.modifier_group_definitions(
            self.tags,self.categories,self.modifier_roles,
        )
        base=next((item for item in draft.anchors if item.role=="base"),None)
        policy=self._policy_for(base.tag_id) if base is not None else None
        slot_values={}; authority=[]
        for slot in draft.slots:
            by_id={str(item.id):item for item in definitions.get(slot.family_id,())}
            known=set(slot.included_value_ids)|set(slot.excluded_value_ids)|set(slot.invalid_value_ids)
            missing=known-set(by_id)
            if missing:
                raise ValueError(
                    f"Typed selector {slot.placeholder} contains values that are no longer available: "+", ".join(sorted(missing))
                )
            values=tuple(by_id[item] for item in slot.included_value_ids)
            slot_values[slot.id]=values
            authority.append({
                "slot":slot.id,"family":slot.family_id,
                "included":list(slot.included_value_ids),
                "excluded":list(slot.excluded_value_ids),
                "invalid":list(slot.invalid_value_ids),"held":list(slot.held_value_ids),
                "allowed":[str(item.id) for item in values if policy is None or self._allows(policy,slot.family_id,item)],
            })
        required={item.id for item in draft.slots if item.required}
        slot_ids=[item.id for item in draft.slots]
        subsets=[]
        for size in range(1,len(slot_ids)+1):
            subsets.extend(item for item in combinations(slot_ids,size) if required.issubset(item))
        potential=sum(
            max(1,product_count) for subset in subsets
            for product_count in [
                __import__("math").prod(len(slot_values[slot_id]) for slot_id in subset)
            ]
        )
        if potential>self.max_potential:
            raise ValueError(
                f"This reviewed subset closure has {potential:,} potential results, above the {self.max_potential:,} safe preview limit. Narrow included values or require more slots."
            )
        raw=[]
        for subset in subsets:
            pools=[slot_values[slot_id] for slot_id in subset]
            for chosen in product(*pools):
                if cancelled():raise InterruptedError()
                selected={
                    slot_id:(
                        next(item.family_id for item in draft.slots if item.id==slot_id),
                        str(value.id),str(value.display),
                    ) for slot_id,value in zip(subset,chosen)
                }
                value_ids={slot_id:item[1] for slot_id,item in selected.items()}
                if self._forbidden(value_ids,draft.constraints):continue
                rendered=self._render(draft,selected)
                values=tuple((slot_id,*selected[slot_id]) for slot_id in subset)
                raw.append((tuple(subset),values,rendered,normalize_tag(rendered)))
        potential=len(raw)
        normalized_counts=Counter(item[3] for item in raw if item[3])
        canonical,aliases=self._identity_indexes(); removed,legacy=self._removed_index()
        resolutions=self._resolutions(); multi_valued=set(
            map(str,draft.constraints.get("multi_valued_family_ids",()))
        )
        items=[]; retained=raw[:self.max_results]; total=len(retained)
        for index,(subset,values,rendered,key) in enumerate(retained,1):
            if cancelled():raise InterruptedError()
            reasons=[]; owner_ids=(); outcome=TAG_TEMPLATE_MISSING
            identity_basis="No active exact identity"
            family_values={}
            for _slot,family_id,value_id,_label in values:
                prior=family_values.get(family_id)
                if prior is not None and prior!=value_id and family_id not in multi_valued:
                    reasons.append(f"The {family_id} dimension has conflicting single-valued choices.")
                family_values[family_id]=value_id
            slot_by_id={item.id:item for item in draft.slots}
            if any(value_id in slot_by_id[slot_id].held_value_ids for slot_id,_family,value_id,_label in values):
                reasons.append("At least one selected modifier value is held by the reviewed slot scope.")
            if policy is not None:
                definitions_by_id={
                    str(item.id):item for family in definitions.values() for item in family
                }
                for _slot,family_id,value_id,label in values:
                    value=definitions_by_id[value_id]
                    if not self._allows(policy,family_id,value):
                        reasons.append(f"The reviewed Base policy excludes {label} from {family_id}.")
            if not key or not values:
                outcome=TAG_TEMPLATE_INVALID; reasons.append("The chip plan did not render one distinct modifier result.")
            elif normalized_counts[key]>1:
                outcome=TAG_TEMPLATE_CONFLICTING
                reasons.append("More than one slot/value path renders the same canonical Tag name.")
            else:
                canonical_ids=canonical.get(key,()); alias_ids=aliases.get(key,())
                if len(canonical_ids)>1:
                    outcome=TAG_TEMPLATE_CONFLICTING; owner_ids=canonical_ids
                    identity_basis="Multiple exact canonical owners"
                    reasons.append("The rendered canonical name belongs to more than one active stable Tag.")
                elif len(canonical_ids)==1:
                    outcome=TAG_TEMPLATE_EXACT_EXISTING; owner_ids=canonical_ids
                    identity_basis="One exact canonical owner"
                elif len(alias_ids)>1:
                    outcome=TAG_TEMPLATE_CONFLICTING; owner_ids=alias_ids
                    identity_basis="Multiple declared Other Name owners"
                    reasons.append("The rendered name is declared by more than one active stable Tag.")
                elif len(alias_ids)==1:
                    outcome=TAG_TEMPLATE_ALIAS_EQUIVALENT; owner_ids=alias_ids
                    identity_basis="One declared Other Name owner"
                else:
                    deleted=self._removed_ids(key,removed,legacy)
                    if deleted:
                        outcome=TAG_TEMPLATE_TOMBSTONED; owner_ids=deleted
                        identity_basis="Exact deleted-name history"
                        reasons.append("Deletion history reserves this exact identity.")
                    elif key in self.ignore_values:
                        outcome=TAG_TEMPLATE_IGNORED
                        identity_basis="Active exact Automation ignore rule"
                        reasons.append("An active exact ignore rule reserves this rendered Tag.")
            if outcome in {TAG_TEMPLATE_EXACT_EXISTING,TAG_TEMPLATE_ALIAS_EQUIVALENT}:
                existing_holds=self._existing_holds(
                    owner_ids,draft,values,resolutions,
                )
                if existing_holds:
                    outcome=TAG_TEMPLATE_CONFLICTING; reasons.extend(existing_holds)
            if reasons and outcome==TAG_TEMPLATE_MISSING:
                outcome=TAG_TEMPLATE_CONFLICTING
            digest=_fingerprint({
                "draft":draft.fingerprint,"subset":subset,
                "values":[(item[0],item[2]) for item in values],
            })[:20]
            existing=tuple(self.tags.tags[item] for item in owner_ids if item in self.tags.tags)
            items.append(TagTemplateBuildItem(
                f"tag_template_result.{digest}",subset,values,rendered,key,
                outcome,identity_basis,tuple(owner_ids),
                tuple(item.display for item in existing),
                _ordered(self._category_label(str(item.category or "")) for item in existing),
                _ordered(reasons),
            ))
            if progress and (index==total or index%max(1,total//200)==0):
                progress(index,total,f"Previewing {rendered}")
        return TagTemplateBuildReport(
            draft,tuple(items),potential,max(0,potential-len(retained)),
            _fingerprint(authority),
        )

    @staticmethod
    def record_from_report(
        report: TagTemplateBuildReport,*,selected_result_ids: Iterable[str],
        template_id: str="",version: int=1,reviewed_at: str|None=None,
        extras: Mapping[str,Any]|None=None,
        projection_rule: Mapping[str,Any]|None=None,
        policy_review: Mapping[str,Any]|None=None,
        generation_enabled: bool=False,fact_expansion_enabled: bool=False,
    ) -> ReviewedTagTemplate:
        if report.bounded_results:
            raise ValueError("Approve only a complete bounded multi-slot preview.")
        requested=_ordered(selected_result_ids); by_id={item.id:item for item in report.items}
        if any(item not in by_id for item in requested):
            raise ValueError("The reviewed selection contains a result outside this exact preview.")
        if any(by_id[item].held for item in requested):
            raise ValueError("Held template results cannot enter the reviewed creation scope.")
        requested_set=set(requested)
        selected=tuple(item.id for item in report.items if item.id in requested_set)
        draft=report.draft; first=draft.slots[0]
        stable_id=template_id or "tag_template."+draft.fingerprint[:20]
        selection=tag_template_selection_fingerprint(report.approval_fingerprint,selected)
        record=ReviewedTagTemplate(
            id=stable_id,version=max(1,int(version)),template=draft.display_template,
            anchor_ids=tuple(item.tag_id for item in draft.anchors),
            anchor_labels=tuple(item.label for item in draft.anchors),
            anchor_roles=tuple(item.role for item in draft.anchors),
            modifier_family_id=first.family_id,modifier_family=first.family_label,
            placeholder=first.placeholder.strip().strip("{}"),token_position="structured",
            included_value_ids=first.included_value_ids,
            excluded_value_ids=first.excluded_value_ids,
            invalid_value_ids=first.invalid_value_ids,held_value_ids=first.held_value_ids,
            selected_result_ids=selected,selection_reviewed=True,
            selection_fingerprint=selection,policy_fingerprint=report.policy_fingerprint,
            candidate_fingerprint=draft.fingerprint,
            expansion_fingerprint=report.approval_fingerprint,
            reviewed_at=reviewed_at or _utc_now(),source="manual_template_builder",
            generation_enabled=generation_enabled,
            fact_expansion_enabled=fact_expansion_enabled,
            max_per_run=draft.max_per_run,format_version=TAG_TEMPLATE_FORMAT_VERSION,
            extras=dict(extras or {}),
            structured_slots=tuple(item.to_dict() for item in draft.slots),
            structured_tokens=tuple(item.to_dict() for item in draft.tokens),
            constraints=dict(draft.constraints),
            destination_policy=dict(draft.destination_policy),
            projection_rule=dict(projection_rule or {}),
            policy_review=dict(policy_review or {}),
        )
        record.validate(); return record

    @staticmethod
    def draft_from_record(record: ReviewedTagTemplate) -> TagTemplateDraft:
        roles=record.anchor_roles or tuple(
            "base" if index==0 else "component" for index in range(len(record.anchor_ids))
        )
        anchors=tuple(
            TagTemplateAnchor(stable_id,label,roles[index])
            for index,(stable_id,label) in enumerate(zip(record.anchor_ids,record.anchor_labels))
        )
        slots=[]
        for index,item in enumerate(record.slot_records,1):
            known={
                "kind","id","family_id","family_label","placeholder","required",
                "included_value_ids","excluded_value_ids","invalid_value_ids","held_value_ids",
            }
            slots.append(TagTemplateSlot(
                str(item.get("id") or f"slot_{index}"),str(item.get("family_id") or ""),
                str(item.get("family_label") or item.get("family_id") or ""),
                str(item.get("placeholder") or ""),_ordered(item.get("included_value_ids",())),
                _ordered(item.get("excluded_value_ids",())),_ordered(item.get("invalid_value_ids",())),
                _ordered(item.get("held_value_ids",())),bool(item.get("required",False)),
                {key:value for key,value in item.items() if key not in known},
            ))
        tokens=[]
        for item in record.token_records:
            known={"kind","tag_id","slot_id","text"}; kind=str(item.get("kind") or "")
            reference=str(item.get("tag_id") or item.get("slot_id") or "")
            if kind=="modifier_slot" and not reference:
                family=str(item.get("family_id") or "")
                matches=[slot.id for slot in slots if slot.family_id==family]
                reference=matches[0] if len(matches)==1 else ""
            tokens.append(TagTemplateToken(
                kind,reference,str(item.get("text") or ""),
                {key:value for key,value in item.items() if key not in known},
            ))
        draft=TagTemplateDraft(
            anchors,tuple(slots),tuple(tokens),record.id,record.version,
            dict(record.constraints),dict(record.destination_policy),record.max_per_run,
        )
        draft.validate(); return draft

    def approve(
        self,repository: Any,report: TagTemplateBuildReport,*,
        selected_result_ids: Iterable[str],current: ReviewedTagTemplate|None=None,
    ) -> TagTemplateApprovalResult:
        selected_result_ids=_ordered(selected_result_ids)
        next_selection_fingerprint=tag_template_selection_fingerprint(
            report.approval_fingerprint,selected_result_ids,
        )
        same_structured_scope=bool(
            current is not None
            and current.candidate_fingerprint==report.draft.fingerprint
            and current.selection_fingerprint==next_selection_fingerprint
        )
        record=self.record_from_report(
            report,selected_result_ids=selected_result_ids,
            template_id=current.id if current is not None else "",
            version=(current.version+1 if current is not None else 1),
            extras=current.extras if current is not None else {},
            projection_rule=(
                current.projection_rule if same_structured_scope else {}
            ),
            policy_review={},generation_enabled=False,fact_expansion_enabled=False,
        )
        return TagTemplateStoreService(repository).save_record(
            record,expected_current_fingerprint=(
                current.semantic_fingerprint if current is not None else ""
            ),
        )


__all__=[
    "MAX_TAG_TEMPLATE_ANCHORS","MAX_TAG_TEMPLATE_POTENTIAL","MAX_TAG_TEMPLATE_SLOTS",
    "TagTemplateAnchor","TagTemplateBuildItem","TagTemplateBuildReport",
    "TagTemplateBuilderService","TagTemplateDraft","TagTemplateSlot","TagTemplateToken",
]
