from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass,replace
from datetime import datetime,timezone
from hashlib import sha256
import json
from typing import Any,Iterable,Mapping

from .knowledge_relationships import MODIFIER_VALUE,SPECIFICITY
from .tag_template_store import (
    ReviewedTagTemplate,TagTemplateApprovalResult,TagTemplateStoreService,
)


MAX_TAG_TEMPLATE_PROJECTION_FACTS = 20_000
MAX_TAG_TEMPLATE_ASSOCIATIONS = 1_000


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


@dataclass(frozen=True)
class TagTemplateProjectionFact:
    tag_id: str
    tag: str
    owner_kind: str
    owner_id: str
    source: str = ""
    protected: bool = False


@dataclass(frozen=True)
class TagTemplateProjectionScope:
    template_id: str
    template_version: int
    owner_kind: str
    owner_id: str
    source_tag_ids: tuple[str,...]
    target_tag_id: str
    target: str
    modifier_values: tuple[tuple[str,str],...]
    omitted_tag_ids: tuple[str,...]
    retained_tag_ids: tuple[str,...]
    relationship_paths: tuple[tuple[str,...],...]
    hold_reasons: tuple[str,...] = ()

    @property
    def held(self) -> bool:
        return bool(self.hold_reasons)

    @property
    def coverage(self) -> int:
        return len(self.modifier_values)


@dataclass(frozen=True)
class TagTemplateProjectionReport:
    scopes: tuple[TagTemplateProjectionScope,...]
    processed_facts: int
    bounded_facts: int

    @property
    def ready(self) -> tuple[TagTemplateProjectionScope,...]:
        return tuple(item for item in self.scopes if not item.held and item.target_tag_id)

    @property
    def held(self) -> tuple[TagTemplateProjectionScope,...]:
        return tuple(item for item in self.scopes if item.held)


@dataclass(frozen=True)
class TagTemplateAssociationSuggestion:
    template_id: str
    source_tag_id: str
    tag_id: str
    tag: str
    owner_kind: str
    owner_id: str
    relationship_path: tuple[str,...]
    reason: str
    eligible: bool
    blocked_reason: str = ""
    automatic_eligible: bool = False
    relationship_kinds: tuple[str,...] = ()


class TagTemplateProjectionService:
    """Resolve reviewed template combinations without changing confirmed facts."""

    def __init__(
        self,tags: Any,categories: Any,templates: Iterable[ReviewedTagTemplate],*,
        compositions: Mapping[str,Mapping[str,Any]],
        relationships: Iterable[Any]=(),
        max_facts: int=MAX_TAG_TEMPLATE_PROJECTION_FACTS,
    ) -> None:
        self.tags=tags; self.categories=categories
        self.templates=tuple(item for item in templates if item.supported)
        self.compositions=compositions
        self.relationships=tuple(relationships)
        self._relationship_kinds={
            str(getattr(item,"id","") or ""):str(getattr(item,"kind","") or "")
            for item in self.relationships if str(getattr(item,"id","") or "")
        }
        for family_values in self.compositions.values():
            for resolution in family_values.values():
                relationship_id=str(getattr(resolution,"relationship_id","") or "")
                if relationship_id:self._relationship_kinds.setdefault(
                    relationship_id,MODIFIER_VALUE,
                )
        self.max_facts=max(1,int(max_facts))
        self._specificity_paths={}
        for relationship in self.relationships:
            if (
                getattr(relationship,"kind","")==SPECIFICITY
                and getattr(relationship,"directed",False)
                and len(getattr(relationship,"endpoints",()))==2
            ):
                source,target=relationship.endpoints
                self._specificity_paths[(str(source.id),str(target.id))]=str(relationship.id)

    @staticmethod
    def _roles(record: ReviewedTagTemplate) -> tuple[str,...]:
        return record.anchor_roles or tuple(
            "base" if index==0 else "component"
            for index in range(len(record.anchor_ids))
        )

    def _base_id(self,record: ReviewedTagTemplate) -> str:
        bases=tuple(
            stable_id for stable_id,role in zip(record.anchor_ids,self._roles(record))
            if role=="base"
        )
        return bases[0] if len(bases)==1 else ""

    def _record_base(self,tag: Any) -> str:
        resolver=getattr(self.tags,"effective_base_tag",None)
        if not callable(resolver):return str(getattr(tag,"parent_id","") or "")
        value=resolver(tag,self.categories)
        return str(value[0] or "") if isinstance(value,tuple) else str(value or "")

    def _modifier_map(self,tag_id: str,allowed: set[str]) -> dict[str,str]:
        result={}
        for family_id,resolution in self.compositions.get(tag_id,{}).items():
            if family_id not in allowed or not bool(getattr(resolution,"is_set",False)):
                continue
            result[str(family_id)]=str(getattr(resolution,"value_id","") or "")
        return {key:value for key,value in result.items() if value}

    def _targets(self,template: ReviewedTagTemplate):
        base_id=self._base_id(template); families=set(template.modifier_family_ids)
        result=[]
        for tag in self.tags.tags.values():
            if str(tag.id)==base_id:
                modifiers={}
            elif self._record_base(tag)!=base_id:
                continue
            else:
                modifiers=self._modifier_map(str(tag.id),families)
            provenance=any(
                isinstance(item,Mapping)
                and item.get("source")=="reviewed_tag_template"
                and str(item.get("template_id") or "")==template.id
                for item in getattr(tag,"provenance",())
            )
            if str(tag.id)==base_id or provenance:
                result.append((tag,modifiers))
        return tuple(result)

    def preview(self,facts: Iterable[TagTemplateProjectionFact]) -> TagTemplateProjectionReport:
        values=tuple(facts)
        if len(values)>self.max_facts:
            return TagTemplateProjectionReport((),self.max_facts,len(values)-self.max_facts)
        scopes=[]
        for template in self.templates:
            if not bool(template.projection_rule.get("enabled",False)):continue
            base_id=self._base_id(template); families=set(template.modifier_family_ids)
            if not base_id or len(families)<1:continue
            candidates=[]
            for fact in values:
                tag=self.tags.tags.get(fact.tag_id)
                if tag is None:continue
                if fact.tag_id==base_id:
                    candidates.append((fact,{})); continue
                if self._record_base(tag)!=base_id:continue
                modifiers=self._modifier_map(fact.tag_id,families)
                if modifiers:candidates.append((fact,modifiers))
            grouped=defaultdict(list); unknown=[]
            for fact,modifiers in candidates:
                if not fact.owner_kind or not fact.owner_id:unknown.append((fact,modifiers))
                else:grouped[(fact.owner_kind,fact.owner_id)].append((fact,modifiers))
            for fact,modifiers in unknown:
                scopes.append(TagTemplateProjectionScope(
                    template.id,template.version,fact.owner_kind,fact.owner_id,
                    (fact.tag_id,),"","",tuple(sorted(modifiers.items())),(),
                    (fact.tag_id,),(),("Owner/object scope is unknown; automatic combination is held.",),
                ))
            targets=self._targets(template)
            for (owner_kind,owner_id),owned in sorted(grouped.items()):
                if len(owned)<2:continue
                holds=[]; union={}; paths=[]
                if any(fact.protected for fact,_modifiers in owned):
                    holds.append("A protected per-image projection decision is present in this owner scope.")
                for fact,modifiers in owned:
                    for family_id,value_id in modifiers.items():
                        prior=union.get(family_id)
                        if prior is not None and prior!=value_id:
                            holds.append(
                                f"The {family_id} dimension has conflicting values {prior} and {value_id}."
                            )
                        union[family_id]=value_id
                        resolution=self.compositions.get(fact.tag_id,{}).get(family_id)
                        relationship_id=str(getattr(resolution,"relationship_id","") or "")
                        if relationship_id:paths.append((relationship_id,))
                source_ids=_ordered(fact.tag_id for fact,_modifiers in owned)
                exact=tuple(
                    tag for tag,modifiers in targets if modifiers==union
                )
                if len(exact)>1:
                    holds.append("More than one canonical target has the same maximal compatible modifier coverage.")
                elif not exact:
                    holds.append("The canonical target for the maximal compatible modifier set is missing.")
                target=exact[0] if len(exact)==1 else None
                target_id=str(getattr(target,"id","") or "")
                omitted=tuple(item for item in source_ids if item!=target_id)
                retained=tuple(item for item in source_ids if item==target_id)
                if target is not None:
                    base_path=self._specificity_paths.get((target_id,base_id),"")
                    if base_path:paths.append((base_path,))
                scopes.append(TagTemplateProjectionScope(
                    template.id,template.version,owner_kind,owner_id,source_ids,
                    target_id,str(getattr(target,"display","") or ""),
                    tuple(sorted(union.items())),omitted,retained,
                    tuple(dict.fromkeys(paths)),_ordered(holds),
                ))
        return TagTemplateProjectionReport(tuple(scopes),len(values),0)

    def association_suggestions(
        self,source_fact: TagTemplateProjectionFact,*,
        confirmed_tag_ids: Iterable[str]=(),confirmed_absent_ids: Iterable[str]=(),
        rejected_tag_ids: Iterable[str]=(),max_suggestions: int=MAX_TAG_TEMPLATE_ASSOCIATIONS,
    ) -> tuple[TagTemplateAssociationSuggestion,...]:
        if not source_fact.owner_kind or not source_fact.owner_id:return ()
        confirmed=set(confirmed_tag_ids); absent=set(confirmed_absent_ids); rejected=set(rejected_tag_ids)
        suggestions=[]; seen=set(); limit=max(1,int(max_suggestions))
        for template in self.templates:
            base_id=self._base_id(template); families=set(template.modifier_family_ids)
            source=self.tags.tags.get(source_fact.tag_id)
            if source is None or self._record_base(source)!=base_id:continue
            modifiers=self._modifier_map(source_fact.tag_id,families)
            if not modifiers:continue
            source_paths=tuple(
                str(getattr(self.compositions.get(source_fact.tag_id,{}).get(family_id),"relationship_id","") or "")
                for family_id in modifiers
            )
            source_paths=tuple(item for item in source_paths if item)
            for target,target_modifiers in self._targets(template):
                target_id=str(target.id)
                if target_id==source_fact.tag_id or target_id in seen:continue
                if not set(target_modifiers.items()).issubset(set(modifiers.items())):continue
                if len(target_modifiers)>=len(modifiers):continue
                seen.add(target_id)
                base_path=self._specificity_paths.get((source_fact.tag_id,base_id),"")
                target_paths=tuple(
                    str(getattr(self.compositions.get(target_id,{}).get(family_id),"relationship_id","") or "")
                    for family_id in target_modifiers
                )
                target_base_path=self._specificity_paths.get((target_id,base_id),"")
                path=_ordered((
                    base_path,target_base_path,*source_paths,*target_paths,
                ))
                blocked=(
                    "Already confirmed" if target_id in confirmed else
                    "Confirmed absent" if target_id in absent else
                    "Previously rejected" if target_id in rejected else ""
                )
                suggestions.append(TagTemplateAssociationSuggestion(
                    template.id,source_fact.tag_id,target_id,str(target.display),
                    source_fact.owner_kind,source_fact.owner_id,path,
                    "Existing reviewed base/modifier subset from the same materialized template graph.",
                    not blocked,blocked,
                    bool(template.fact_expansion_enabled and not blocked),
                    tuple(self._relationship_kinds.get(item,"") for item in path),
                ))
                if len(suggestions)>=limit:return tuple(suggestions)
        return tuple(suggestions)

    @staticmethod
    def publish_rule(
        repository: Any,record: ReviewedTagTemplate,
    ) -> TagTemplateApprovalResult:
        roles=TagTemplateProjectionService._roles(record)
        bases=tuple(
            stable_id for stable_id,role in zip(record.anchor_ids,roles) if role=="base"
        )
        if len(bases)!=1 or len(record.modifier_family_ids)<1:
            raise ValueError("A projection rule needs one primary Base Tag and typed modifier selectors.")
        next_version=record.version+1
        core={
            "schema_version":1,"enabled":True,"template_id":record.id,
            "template_version":next_version,"base_tag_id":bases[0],
            "modifier_family_ids":list(record.modifier_family_ids),
            "selection_fingerprint":record.selection_fingerprint,
            "maximal_compatible":True,"owner_scope_required":True,
        }
        core["fingerprint"]=_fingerprint(core); core["reviewed_at"]=_utc_now()
        proposed=replace(
            record,version=next_version,reviewed_at=_utc_now(),projection_rule=core,
            generation_enabled=False,fact_expansion_enabled=False,policy_review={},
        )
        proposed.validate()
        return TagTemplateStoreService(repository).save_record(
            proposed,expected_current_fingerprint=record.semantic_fingerprint,
        )


def active_tag_template_projection_service(
    knowledge: Any,*,require_projection: bool=False,
):
    """Build the current typed projection index from canonical repositories."""

    from .automation_repository import AutomationRuleRepository
    from .category_registry import CategoryRegistry
    from .knowledge_engine import FoundationKnowledge
    from .modifier_composition import modifier_composition_resolutions
    from .paths import (
        CATEGORIES_PATH,DEFAULTS_DIR,RELATIONSHIPS_PATH,USER_DATA_DIR,
    )
    from .relationship_store import RelationshipStore

    repository=AutomationRuleRepository(
        DEFAULTS_DIR/"automation_rules.json",
        USER_DATA_DIR/"automation_rules.json",
    )
    templates=tuple(
        record for _origin,record in TagTemplateStoreService(repository).records_with_origin()
    )
    if require_projection:
        templates=tuple(
            item for item in templates if bool(item.projection_rule.get("enabled",False))
        )
    if not templates:return None
    categories=CategoryRegistry(DEFAULTS_DIR/"categories.json",CATEGORIES_PATH)
    default_relationships=RelationshipStore(DEFAULTS_DIR/"relationships.json")
    user_relationships=RelationshipStore(RELATIONSHIPS_PATH)
    relationships=dict(default_relationships.relationships)
    relationships.update(user_relationships.relationships)
    categories.configure_category_base_authority(relationships.values())
    foundation=FoundationKnowledge(
        DEFAULTS_DIR/"modifiers.json",DEFAULTS_DIR/"concepts.json",
    )
    compositions=modifier_composition_resolutions(
        relationships.values(),tags=knowledge,categories=categories,
        foundation=foundation,
    )
    return TagTemplateProjectionService(
        knowledge,categories,templates,compositions=compositions,
        relationships=relationships.values(),
    )


__all__=[
    "MAX_TAG_TEMPLATE_ASSOCIATIONS","MAX_TAG_TEMPLATE_PROJECTION_FACTS",
    "TagTemplateAssociationSuggestion","TagTemplateProjectionFact",
    "TagTemplateProjectionReport","TagTemplateProjectionScope",
    "TagTemplateProjectionService","active_tag_template_projection_service",
]
