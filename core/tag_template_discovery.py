from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Callable, Iterable, Mapping

from .category_registry import exact_category_anchor_ids
from .tag_collection import normalize_tag


MAX_TAG_TEMPLATE_DISCOVERY_TAGS = 20_000
MAX_TAG_TEMPLATE_DISCOVERY_CANDIDATES = 1_000


@dataclass(frozen=True)
class TagTemplateMatch:
    """One exact existing Tag decomposition retained by a candidate family."""

    tag_id: str
    tag: str
    matched_name: str
    name_basis: str
    value_id: str
    value: str
    category_id: str
    category: str
    base_resolution: str
    modifier_resolution: str
    hold_reasons: tuple[str, ...] = ()

    @property
    def held(self) -> bool:
        return bool(self.hold_reasons)


@dataclass(frozen=True)
class TagTemplateCandidate:
    """One stable, preview-only reusable Tag family proposal."""

    id: str
    template: str
    anchor_ids: tuple[str, ...]
    anchor: str
    anchor_forms: tuple[str, ...]
    anchor_basis: str
    modifier_family_id: str
    modifier_family: str
    placeholder: str
    token_position: str
    policy_source: str
    policy_fingerprint: str
    matches: tuple[TagTemplateMatch, ...]
    family_hold_reasons: tuple[str, ...] = ()
    hold_reasons: tuple[str, ...] = ()

    @property
    def ready_for_review(self) -> bool:
        return (
            len(self.anchor_ids) == 1
            and self.ready_match_count > 0
            and not self.family_hold_reasons
        )

    @property
    def ready_match_count(self) -> int:
        return sum(not item.held for item in self.matches)

    @property
    def held_match_count(self) -> int:
        return sum(item.held for item in self.matches)

    @property
    def matching_record_count(self) -> int:
        return len({item.tag_id for item in self.matches})

    @property
    def category_boundary(self) -> str:
        categories={item.category_id for item in self.matches if item.category_id}
        return "One current Category" if len(categories) <= 1 else "Multiple current Categories"

    @property
    def review_fingerprint(self) -> str:
        """Identify the exact report state a later preview was reviewed from."""
        payload=(
            self.id,self.template,self.anchor_ids,self.anchor,self.anchor_forms,
            self.anchor_basis,self.modifier_family_id,self.modifier_family,
            self.placeholder,self.token_position,self.policy_source,
            self.policy_fingerprint,
            self.family_hold_reasons,self.hold_reasons,
            tuple((
                item.tag_id,item.tag,item.matched_name,item.name_basis,
                item.value_id,item.value,
                item.category_id,item.base_resolution,item.modifier_resolution,
                item.hold_reasons,
            ) for item in self.matches),
        )
        return sha256(repr(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TagTemplateDiscoveryReport:
    """Bounded discovery result that never establishes template authority."""

    candidates: tuple[TagTemplateCandidate, ...]
    processed_tags: int
    bounded_tags: int
    bounded_candidates: int

    @property
    def ready_for_review(self) -> tuple[TagTemplateCandidate, ...]:
        return tuple(item for item in self.candidates if item.ready_for_review)

    @property
    def held(self) -> tuple[TagTemplateCandidate, ...]:
        return tuple(item for item in self.candidates if not item.ready_for_review)


def _ordered_unique(values: Iterable[str]) -> tuple[str, ...]:
    result=[]; seen=set()
    for value in values:
        text=str(value or "").strip()
        if text and text not in seen:
            seen.add(text); result.append(text)
    return tuple(result)


class TagTemplateDiscoveryService:
    """Discover exact single-modifier families without creating knowledge.

    The service intentionally stops at a report. Exact names and declared Other
    Names can expose a candidate, but only reviewed Base Tag, Allowed Details,
    and typed modifier authority can make it ready for a later review step.
    """

    def __init__(
        self,
        tags: Any,
        categories: Any,
        foundation: Any,
        *,
        policy_resolver: Any = None,
        composition_resolver: Any = None,
        modifier_roles: Mapping[str, Any] | None = None,
        max_tags: int = MAX_TAG_TEMPLATE_DISCOVERY_TAGS,
        max_candidates: int = MAX_TAG_TEMPLATE_DISCOVERY_CANDIDATES,
    ) -> None:
        self.tags=tags
        self.categories=categories
        self.foundation=foundation
        self.policy_resolver=policy_resolver
        self.composition_resolver=composition_resolver
        self.modifier_roles=dict(modifier_roles or {})
        self.max_tags=max(1,int(max_tags))
        self.max_candidates=max(1,int(max_candidates))
        self._policy_cache={}
        anchor_resolver=getattr(categories,"exact_category_anchor_ids",None)
        self._derived_base_anchors=(
            dict(anchor_resolver()) if callable(anchor_resolver) else
            exact_category_anchor_ids(
                getattr(categories,"categories",{}).values(),
                getattr(tags,"tags",{}).values(),
            )
        )

    def _identity_names(self,record: Any) -> tuple[str, ...]:
        resolver=getattr(self.tags,"identity_names",None)
        values=(
            resolver(record) if callable(resolver) else
            (getattr(record,"display",""),*getattr(record,"aliases",()))
        )
        result=[]; seen=set()
        for value in values:
            key=normalize_tag(str(value))
            if key and key not in seen:
                seen.add(key); result.append(str(value))
        return tuple(result)

    def _category_label(self,category_id: str) -> str:
        path_label=getattr(self.categories,"path_label",None)
        if callable(path_label):
            label=str(path_label(category_id) or "")
            if label:return label
        record=getattr(self.categories,"get",lambda _value:None)(category_id)
        return str(getattr(record,"name","") or category_id or "Uncategorized")

    def _policy_for(self,anchor: Any) -> Any:
        stable_id=str(anchor.id)
        if stable_id in self._policy_cache:return self._policy_cache[stable_id]
        resolver=getattr(self.policy_resolver,"effective_for_tag",None)
        policy=(
            resolver(stable_id) if callable(resolver) else
            self.foundation.effective_modifier_policy(anchor,self.tags,self.categories)
        )
        self._policy_cache[stable_id]=policy
        return policy

    def _category_path_ids(self,category_id: str) -> tuple[str, ...]:
        resolver=getattr(self.categories,"path_ids",None)
        return tuple(resolver(category_id)) if callable(resolver) else ()

    def _reviewed_base_status(self,record: Any,anchor_id: str) -> tuple[str,str]:
        base_id,_source,kind=self.tags.effective_base_tag(
            record,self.categories,derived_anchors=self._derived_base_anchors,
        )
        if str(base_id or "") != anchor_id:
            if base_id:
                base=getattr(self.tags,"tags",{}).get(str(base_id))
                return (
                    f"Conflicts with reviewed Base Tag {getattr(base,'display',base_id)}.",
                    f"Different reviewed Base Tag ({kind.replace('_',' ')})",
                )
            if kind in {"ambiguous_explicit","ambiguous_category_base"}:
                return (
                    "The existing Tag has ambiguous reviewed Base Tag authority.",
                    "Ambiguous Base Tag authority",
                )
            if kind in {"missing_explicit","missing_inherited","cycle"}:
                return (
                    f"The existing Tag's Base Tag authority is invalid ({kind.replace('_',' ')}).",
                    f"Invalid Base Tag authority ({kind.replace('_',' ')})",
                )
            return (
                "The exact name match has no reviewed Base Tag relationship.",
                "Exact name only; no reviewed Base Tag",
            )
        if kind=="explicit":
            return "","Reviewed Tag-specific Broader Tag"
        if kind=="inherited":
            category_base=getattr(self.categories,"effective_base_tag",None)
            direct_id=""; policy="none"
            if callable(category_base):
                direct_id,_owner,policy=category_base(str(getattr(record,"category","") or ""))
            if str(direct_id or "")==anchor_id and policy!="ambiguous":
                return "","Reviewed Category Base Tag"
            return (
                "The Category supplies only an exact derived anchor, not reviewed Category Base authority.",
                "Exact derived Category anchor",
            )
        return (
            "The exact name match does not have reviewed Base Tag authority.",
            f"Unreviewed Base Tag resolution ({kind.replace('_',' ')})",
        )

    def _match_review(
        self,
        *,
        record: Any,
        record_name: str,
        record_basis: str,
        anchor_ids: tuple[str, ...],
        group: str,
        value: Any,
        ambiguous_modifier: bool,
        resolutions: Mapping[str,Mapping[str,Any]],
    ) -> tuple[TagTemplateMatch,tuple[str,...],str,str]:
        reasons=[]; base_resolution="Ambiguous exact anchor"
        policy_source="No unique anchor authority"
        policy_fingerprint=""
        modifier_resolution="Exact canonical or declared Other Name decomposition"
        if len(anchor_ids)!=1:
            reasons.append(
                "The exact anchor name belongs to more than one stable Tag. Choose the intended anchor before review."
            )
        else:
            anchor=self.tags.tags.get(anchor_ids[0])
            if anchor is None:
                reasons.append("The exact anchor Tag is no longer available.")
            else:
                base_reason,base_resolution=self._reviewed_base_status(record,anchor.id)
                if base_reason:reasons.append(base_reason)
                policy=self._policy_for(anchor)
                groups=tuple(str(item) for item in getattr(policy,"groups",()) if str(item))
                policy_source=str(getattr(policy,"source_label","") or "No reviewed Allowed Details policy")
                policy_fingerprint=str(getattr(policy,"record_fingerprint","") or "")
                blocked=str(getattr(policy,"blocked_reason","") or "")
                if blocked:
                    reasons.append(f"Allowed Details authority is blocked: {blocked}")
                elif group not in groups:
                    reasons.append(
                        f"The anchor does not currently allow reviewed {self.foundation.modifier_group_label(group,self.categories)} values."
                    )
                else:
                    allows_value=getattr(policy,"allows_value",None)
                    if callable(allows_value) and not allows_value(
                        group,str(value.id),self._category_path_ids(
                            str(getattr(self.tags.tags.get(str(value.id)),"category","") or "")
                        ),
                    ):
                        reasons.append(
                            f"The anchor's reviewed {self.foundation.modifier_group_label(group,self.categories)} scope excludes {value.display}."
                        )
                if "compatibility" in str(getattr(policy,"authority","")).casefold():
                    reasons.append("The Allowed Details policy is compatibility evidence and still needs review.")

                slot=resolutions.get(str(record.id),{}).get(group)
                if slot is not None and bool(getattr(slot,"is_set",False)):
                    if str(getattr(slot,"value_id","") or "")!=str(value.id):
                        reasons.append("The exact-name value conflicts with the existing reviewed modifier value.")
                        modifier_resolution="Conflicts with existing modifier value"
                    else:
                        modifier_resolution="Matches existing reviewed modifier value"
                elif slot is not None:
                    reasons.append("The existing reviewed modifier slot is disabled or has no selected value.")
                    modifier_resolution="Existing modifier slot needs review"
                elif any(
                    str(current)==str(value.id)
                    for current in getattr(record,"attributes",())
                ):
                    modifier_resolution="Matches retained compatibility modifier value"
                elif any(
                    str(current) and str(current)!=str(value.id)
                    for current in getattr(record,"attributes",())
                ):
                    reasons.append("The exact-name value conflicts with the existing reviewed modifier value.")
                    modifier_resolution="Conflicts with existing modifier value"
        if ambiguous_modifier:
            reasons.append(
                "The exact modifier text belongs to more than one reviewed value or dimension."
            )
            modifier_resolution="Ambiguous reviewed modifier text"
        match=TagTemplateMatch(
            tag_id=str(record.id),tag=str(record.display),matched_name=record_name,
            name_basis=record_basis,value_id=str(value.id),value=str(value.display),
            category_id=str(getattr(record,"category","") or ""),
            category=self._category_label(str(getattr(record,"category","") or "")),
            base_resolution=base_resolution,
            modifier_resolution=modifier_resolution,
            hold_reasons=_ordered_unique(reasons),
        )
        return match,match.hold_reasons,policy_source,policy_fingerprint

    def discover(
        self,
        progress: Callable[[int,int,str],None] | None = None,
        cancelled: Callable[[],bool] | None = None,
    ) -> TagTemplateDiscoveryReport:
        cancelled=cancelled or (lambda:False)
        records=tuple(sorted(
            self.tags.tags.values(),key=lambda item:(str(item.display).casefold(),str(item.id)),
        ))
        scanned=records[:self.max_tags]
        total=len(scanned)
        if progress:progress(0,total,"Preparing exact canonical and Other Name indexes")

        definitions=self.foundation.modifier_group_definitions(
            self.tags,self.categories,self.modifier_roles,
        )
        identity_index: dict[str,dict[str,str]]={}
        for record in scanned:
            if cancelled():raise InterruptedError()
            display_key=normalize_tag(str(record.display))
            for name in self._identity_names(record):
                key=normalize_tag(name)
                basis="Exact canonical name" if key==display_key else "Declared Other Name"
                existing=identity_index.setdefault(key,{}).get(str(record.id))
                if existing!="Exact canonical name":
                    identity_index[key][str(record.id)]=basis

        prefix_lexemes: dict[str,list[tuple[tuple[str,...],str,Any,str,int]]]={}
        suffix_lexemes: dict[str,list[tuple[tuple[str,...],str,Any,str,int]]]={}
        raw_lexemes=[]; owners_by_form={}
        for group,values in definitions.items():
            for value in values:
                forms={normalize_tag(item) for item in (value.display,*value.aliases)}
                for form in sorted(item for item in forms if item):
                    tokens=tuple(form.split())
                    raw_lexemes.append((tokens,str(group),value,form))
                    owners_by_form.setdefault(form,set()).add((str(group),str(value.id)))
        for tokens,group,value,form in raw_lexemes:
            ambiguity=len(owners_by_form[form])
            item=(tokens,str(group),value,form,ambiguity)
            prefix_lexemes.setdefault(tokens[0],[]).append(item)
            suffix_lexemes.setdefault(tokens[-1],[]).append(item)
        for index in (prefix_lexemes,suffix_lexemes):
            for items in index.values():
                items.sort(key=lambda item:(-len(item[0]),item[1],str(item[2].id)))

        composition_resolutions={}
        resolver=getattr(self.composition_resolver,"resolutions",None)
        if callable(resolver):composition_resolutions=resolver()
        elif isinstance(self.composition_resolver,Mapping):composition_resolutions=self.composition_resolver

        builders: dict[tuple[tuple[str,...],str,str],dict[str,Any]]={}
        seen_candidate_keys=set(); bounded_candidates=0
        stride=max(1,total//200) if total else 1
        for index,record in enumerate(scanned,1):
            if cancelled():raise InterruptedError()
            record_display_key=normalize_tag(str(record.display))
            for record_name in self._identity_names(record):
                record_key=normalize_tag(record_name); words=tuple(record_key.split())
                if len(words)<2:continue
                record_basis=(
                    "Exact canonical name" if record_key==record_display_key
                    else "Declared Other Name"
                )
                possibilities=[]
                for lexeme in prefix_lexemes.get(words[0],()):
                    tokens,group,value,_form,ambiguity=lexeme
                    if len(tokens)<len(words) and words[:len(tokens)]==tokens:
                        possibilities.append(("prefix",group,value,ambiguity," ".join(words[len(tokens):])))
                for lexeme in suffix_lexemes.get(words[-1],()):
                    tokens,group,value,_form,ambiguity=lexeme
                    if len(tokens)<len(words) and words[-len(tokens):]==tokens:
                        possibilities.append(("suffix",group,value,ambiguity," ".join(words[:-len(tokens)])))
                for position,group,value,ambiguity,anchor_form in possibilities:
                    owners=identity_index.get(anchor_form,{})
                    anchor_ids=tuple(sorted(owner_id for owner_id in owners if owner_id!=str(record.id)))
                    if not anchor_ids:continue
                    anchor_basis=(
                        owners[anchor_ids[0]] if len(anchor_ids)==1
                        else "Ambiguous exact canonical or Other Name"
                    )
                    anchor_record=self.tags.tags.get(anchor_ids[0]) if len(anchor_ids)==1 else None
                    if anchor_record is not None and self.foundation.modifier_for_tag(
                        anchor_record,self.categories,self.modifier_roles,
                    ) is not None:
                        continue
                    key=(anchor_ids,group,position)
                    if key not in seen_candidate_keys:
                        seen_candidate_keys.add(key)
                        if len(builders)>=self.max_candidates:
                            bounded_candidates+=1; continue
                        anchor_label=(
                            str(anchor_record.display) if anchor_record is not None else
                            " / ".join(
                                str(self.tags.tags[item].display) for item in anchor_ids
                                if item in self.tags.tags
                            ) or anchor_form
                        )
                        family_label=self.foundation.modifier_group_label(group,self.categories)
                        placeholder=normalize_tag(family_label).replace(" ","_")
                        template=(
                            f"{{{placeholder}}} {anchor_label}" if position=="prefix"
                            else f"{anchor_label} {{{placeholder}}}"
                        )
                        digest=sha256(
                            ("\x1f".join((*anchor_ids,group,position))).encode("utf-8")
                        ).hexdigest()[:20]
                        builders[key]={
                            "id":f"tag_template_candidate.{digest}","template":template,
                            "anchor_ids":anchor_ids,"anchor":anchor_label,
                            "anchor_forms":set(),"anchor_bases":set(),
                            "group":group,"family":family_label,"placeholder":placeholder,
                            "position":position,"matches":[],"match_keys":set(),
                            "reasons":[],"policy_source":"","policy_fingerprint":"",
                        }
                    builder=builders.get(key)
                    if builder is None:continue
                    builder["anchor_forms"].add(anchor_form)
                    builder["anchor_bases"].add(anchor_basis)
                    match_key=(str(record.id),str(value.id),record_key)
                    if match_key in builder["match_keys"]:continue
                    builder["match_keys"].add(match_key)
                    match,reasons,policy_source,policy_fingerprint=self._match_review(
                        record=record,record_name=str(record_name),record_basis=record_basis,
                        anchor_ids=anchor_ids,group=group,value=value,
                        ambiguous_modifier=ambiguity>1,
                        resolutions=composition_resolutions,
                    )
                    builder["matches"].append(match)
                    builder["reasons"].extend(reasons)
                    builder["policy_source"]=policy_source
                    builder["policy_fingerprint"]=policy_fingerprint
            if progress and (index==total or index%stride==0):
                progress(index,total,str(record.display))

        candidates=[]
        for builder in builders.values():
            values_by_record={}
            for match in builder["matches"]:
                values_by_record.setdefault(match.tag_id,set()).add(match.value_id)
            reasons=list(builder["reasons"]); family_reasons=[]
            if any(len(values)>1 for values in values_by_record.values()):
                family_reasons.append(
                    "At least one existing Tag has multiple exact value decompositions in this family."
                )
            reasons.extend(family_reasons)
            candidates.append(TagTemplateCandidate(
                id=builder["id"],template=builder["template"],
                anchor_ids=builder["anchor_ids"],anchor=builder["anchor"],
                anchor_forms=tuple(sorted(builder["anchor_forms"])),
                anchor_basis=" / ".join(sorted(builder["anchor_bases"])),
                modifier_family_id=builder["group"],modifier_family=builder["family"],
                placeholder=builder["placeholder"],token_position=builder["position"],
                policy_source=builder["policy_source"],
                policy_fingerprint=builder["policy_fingerprint"],
                matches=tuple(sorted(
                    builder["matches"],
                    key=lambda item:(item.tag.casefold(),item.tag_id,item.value.casefold(),item.matched_name.casefold()),
                )),
                family_hold_reasons=_ordered_unique(family_reasons),
                hold_reasons=_ordered_unique(reasons),
            ))
        candidates.sort(key=lambda item:(not item.ready_for_review,item.template.casefold(),item.id))
        return TagTemplateDiscoveryReport(
            tuple(candidates),total,max(0,len(records)-total),bounded_candidates,
        )
