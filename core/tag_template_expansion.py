from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from typing import Any,Callable,Iterable,Mapping

from .category_registry import exact_category_anchor_ids
from .tag_collection import normalize_tag
from .tag_template_discovery import (
    MAX_TAG_TEMPLATE_DISCOVERY_TAGS,TagTemplateCandidate,
    TagTemplateDiscoveryService,
)


MAX_TAG_TEMPLATE_EXPANSION_VALUES = 20_000

TAG_TEMPLATE_EXACT_EXISTING = "exact_existing"
TAG_TEMPLATE_ALIAS_EQUIVALENT = "alias_equivalent"
TAG_TEMPLATE_MISSING = "missing"
TAG_TEMPLATE_EXCLUDED = "excluded"
TAG_TEMPLATE_IGNORED = "ignored"
TAG_TEMPLATE_TOMBSTONED = "tombstoned"
TAG_TEMPLATE_INVALID = "invalid"
TAG_TEMPLATE_CONFLICTING = "conflicting"

TAG_TEMPLATE_EXPANSION_LABELS = {
    TAG_TEMPLATE_EXACT_EXISTING: "Exact existing",
    TAG_TEMPLATE_ALIAS_EQUIVALENT: "Alias-equivalent",
    TAG_TEMPLATE_MISSING: "New (missing)",
    TAG_TEMPLATE_EXCLUDED: "Excluded by reviewed scope",
    TAG_TEMPLATE_IGNORED: "Ignored by active rule",
    TAG_TEMPLATE_TOMBSTONED: "Deleted identity held",
    TAG_TEMPLATE_INVALID: "Invalid",
    TAG_TEMPLATE_CONFLICTING: "Conflicting authority",
}


class TagTemplateExpansionStaleError(RuntimeError):
    """The selected discovery row no longer matches current authority."""


@dataclass(frozen=True)
class TagTemplateExpansionItem:
    """One reviewed modifier value rendered against one stable anchor."""

    id: str
    value_id: str
    value: str
    generated_tag: str
    normalized_tag: str
    outcome: str
    identity_basis: str
    existing_tag_ids: tuple[str,...] = ()
    existing_tags: tuple[str,...] = ()
    current_categories: tuple[str,...] = ()
    hold_reasons: tuple[str,...] = ()

    @property
    def outcome_label(self) -> str:
        return TAG_TEMPLATE_EXPANSION_LABELS.get(self.outcome,self.outcome)

    @property
    def held(self) -> bool:
        return bool(self.hold_reasons) or self.outcome in {
            TAG_TEMPLATE_EXCLUDED,TAG_TEMPLATE_IGNORED,TAG_TEMPLATE_TOMBSTONED,
            TAG_TEMPLATE_INVALID,TAG_TEMPLATE_CONFLICTING,
        }

    @property
    def review_boundary(self) -> str:
        if self.held:
            return "Held: " + (self.hold_reasons[0] if self.hold_reasons else self.outcome_label)
        if self.outcome==TAG_TEMPLATE_MISSING:
            return "Ready for later creation planning"
        return "Ready for later reuse and organization planning"


@dataclass(frozen=True)
class TagTemplateExpansionReport:
    """A bounded, preview-only expansion with no persistent decisions."""

    candidate: TagTemplateCandidate
    items: tuple[TagTemplateExpansionItem,...]
    processed_values: int
    bounded_values: int

    @property
    def potential_count(self) -> int:
        return self.processed_values+self.bounded_values

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
    def included_value_ids(self) -> tuple[str,...]:
        return tuple(
            item.value_id for item in self.items
            if item.outcome not in {TAG_TEMPLATE_EXCLUDED,TAG_TEMPLATE_INVALID}
        )

    @property
    def excluded_value_ids(self) -> tuple[str,...]:
        return tuple(
            item.value_id for item in self.items
            if item.outcome==TAG_TEMPLATE_EXCLUDED
        )

    @property
    def invalid_value_ids(self) -> tuple[str,...]:
        return tuple(
            item.value_id for item in self.items
            if item.outcome==TAG_TEMPLATE_INVALID
        )

    @property
    def held_value_ids(self) -> tuple[str,...]:
        return tuple(item.value_id for item in self.items if item.held)

    @property
    def approval_fingerprint(self) -> str:
        payload=(
            self.candidate.review_fingerprint,self.processed_values,
            self.bounded_values,
            tuple((
                item.id,item.value_id,item.value,item.generated_tag,
                item.normalized_tag,item.outcome,item.identity_basis,
                item.existing_tag_ids,item.current_categories,item.hold_reasons,
            ) for item in self.items),
        )
        return sha256(repr(payload).encode("utf-8")).hexdigest()


def _ordered_unique(values: Iterable[str]) -> tuple[str,...]:
    result=[]; seen=set()
    for value in values:
        text=str(value or "").strip()
        if text and text not in seen:
            seen.add(text); result.append(text)
    return tuple(result)


class TagTemplateExpansionService:
    """Revalidate and preview one exact single-modifier family.

    This service does not approve a template or mutate Tags, rules, categories,
    relationships, Image Knowledge, Metadata, TXT, pixels, or locations.
    """

    def __init__(
        self,
        tags: Any,
        categories: Any,
        foundation: Any,
        *,
        policy_resolver: Any = None,
        composition_resolver: Any = None,
        modifier_roles: Mapping[str,Any] | None = None,
        ignore_values: Iterable[str] = (),
        max_values: int = MAX_TAG_TEMPLATE_EXPANSION_VALUES,
    ) -> None:
        self.tags=tags
        self.categories=categories
        self.foundation=foundation
        self.policy_resolver=policy_resolver
        self.composition_resolver=composition_resolver
        self.modifier_roles=dict(modifier_roles or {})
        self.ignore_values={normalize_tag(value) for value in ignore_values if normalize_tag(value)}
        self.max_values=max(1,int(max_values))
        anchor_resolver=getattr(categories,"exact_category_anchor_ids",None)
        self._derived_base_anchors=(
            dict(anchor_resolver()) if callable(anchor_resolver) else
            exact_category_anchor_ids(
                getattr(categories,"categories",{}).values(),
                getattr(tags,"tags",{}).values(),
            )
        )

    def _category_path_ids(self,category_id: str) -> tuple[str,...]:
        resolver=getattr(self.categories,"path_ids",None)
        return tuple(resolver(category_id)) if callable(resolver) else ()

    def _category_label(self,category_id: str) -> str:
        resolver=getattr(self.categories,"path_label",None)
        if callable(resolver):
            label=str(resolver(category_id) or "")
            if label:return label
        record=getattr(self.categories,"get",lambda _value:None)(category_id)
        return str(getattr(record,"name","") or category_id or "Uncategorized")

    def _policy_for(self,anchor_id: str) -> Any:
        resolver=getattr(self.policy_resolver,"effective_for_tag",None)
        if callable(resolver):return resolver(anchor_id)
        anchor=self.tags.tags.get(anchor_id)
        return self.foundation.effective_modifier_policy(
            anchor,self.tags,self.categories,
        )

    def _allows_value(self,policy: Any,family_id: str,value: Any) -> bool:
        resolver=getattr(self.policy_resolver,"allows_value",None)
        if callable(resolver):return bool(resolver(policy,family_id,str(value.id)))
        allows=getattr(policy,"allows_value",None)
        return bool(allows(
            family_id,str(value.id),self._category_path_ids(
                str(getattr(self.tags.tags.get(str(value.id)),"category","") or "")
            ),
        )) if callable(allows) else family_id in tuple(getattr(policy,"groups",()))

    def _identity_indexes(self) -> tuple[dict[str,tuple[str,...]],dict[str,tuple[str,...]]]:
        canonical: dict[str,set[str]]={}; aliases: dict[str,set[str]]={}
        for record in self.tags.tags.values():
            key=normalize_tag(str(record.display))
            if key:canonical.setdefault(key,set()).add(str(record.id))
            for alias in self.tags.effective_aliases(record):
                alias_key=normalize_tag(str(alias))
                if alias_key:aliases.setdefault(alias_key,set()).add(str(record.id))
        return (
            {key:tuple(sorted(values)) for key,values in canonical.items()},
            {key:tuple(sorted(values)) for key,values in aliases.items()},
        )

    def _composition_resolutions(self) -> Mapping[str,Mapping[str,Any]]:
        resolver=getattr(self.composition_resolver,"resolutions",None)
        if callable(resolver):return resolver()
        return self.composition_resolver if isinstance(self.composition_resolver,Mapping) else {}

    def _existing_holds(
        self,owner_ids: Iterable[str],*,anchor_id: str,family_id: str,value_id: str,
        resolutions: Mapping[str,Mapping[str,Any]],family_values: set[str],
    ) -> tuple[str,...]:
        reasons=[]
        for owner_id in owner_ids:
            record=self.tags.tags.get(owner_id)
            if record is None:
                reasons.append(f"Existing owner {owner_id} is no longer available.")
                continue
            base_id,_source,kind=self.tags.effective_base_tag(
                record,self.categories,derived_anchors=self._derived_base_anchors,
            )
            if str(base_id or "")!=anchor_id:
                reasons.append(
                    f"{record.display} does not have the selected reviewed Base Tag "
                    f"({kind.replace('_',' ') or 'none'})."
                )
            slot=resolutions.get(owner_id,{}).get(family_id)
            if slot is not None and bool(getattr(slot,"is_set",False)):
                if str(getattr(slot,"value_id","") or "")!=value_id:
                    reasons.append(
                        f"{record.display} has a conflicting reviewed modifier value."
                    )
            elif slot is not None:
                reasons.append(
                    f"{record.display}'s reviewed modifier slot is disabled or incomplete."
                )
            else:
                current={str(item) for item in getattr(record,"attributes",()) if str(item) in family_values}
                if current and value_id not in current:
                    reasons.append(
                        f"{record.display} has a conflicting retained modifier value."
                    )
        return _ordered_unique(reasons)

    def _removed_identity_index(self) -> tuple[dict[str,tuple[str,...]],set[str]]:
        """Index current archived names once; retain conservative legacy IDs."""
        matches: dict[str,list[str]]={}; archived_ids=set()
        active=getattr(self.tags,"active_removed_archives",lambda:())()
        for archive in active:
            for stable_id in getattr(archive,"tag_ids",()):
                stable_id=str(stable_id)
                archived_ids.add(stable_id)
                if stable_id not in getattr(self.tags,"removed_tag_ids",set()):continue
                record=archive.tag_record(stable_id)
                if record is None:continue
                for name in (record.display,*record.aliases):
                    key=normalize_tag(str(name))
                    if key and stable_id not in matches.setdefault(key,[]):
                        matches[key].append(stable_id)
        return (
            {key:tuple(values) for key,values in matches.items()},
            set(getattr(self.tags,"removed_tag_ids",set()))-archived_ids,
        )

    @staticmethod
    def _removed_ids(
        key: str,index: Mapping[str,tuple[str,...]],legacy_ids: set[str],
    ) -> tuple[str,...]:
        matches=list(index.get(key,()))
        slug="_".join(key.split())
        matches.extend(
            candidate for candidate in sorted(legacy_ids)
            if candidate==slug or (
                candidate.startswith(slug+"_")
                and candidate[len(slug)+1:].isdigit()
            )
        )
        return tuple(dict.fromkeys(matches))

    def _revalidate(
        self,candidate: TagTemplateCandidate,
        progress: Callable[[int,int,str],None] | None,
        cancelled: Callable[[],bool],
    ) -> TagTemplateCandidate:
        report=TagTemplateDiscoveryService(
            self.tags,self.categories,self.foundation,
            policy_resolver=self.policy_resolver,
            composition_resolver=self.composition_resolver,
            modifier_roles=self.modifier_roles,
        ).discover(progress,cancelled)
        current=next((item for item in report.candidates if item.id==candidate.id),None)
        if current is None or not current.ready_for_review:
            raise TagTemplateExpansionStaleError(
                "This reusable Tag family is no longer ready. Run discovery again and review current authority."
            )
        if current.review_fingerprint!=candidate.review_fingerprint:
            raise TagTemplateExpansionStaleError(
                "This reusable Tag family changed after discovery. Run discovery again before previewing its expansion."
            )
        return current

    def preview(
        self,
        candidate: TagTemplateCandidate,
        progress: Callable[[int,int,str],None] | None = None,
        cancelled: Callable[[],bool] | None = None,
    ) -> TagTemplateExpansionReport:
        cancelled=cancelled or (lambda:False)
        if len(self.tags.tags)>MAX_TAG_TEMPLATE_DISCOVERY_TAGS:
            raise TagTemplateExpansionStaleError(
                f"Expansion preview stops above {MAX_TAG_TEMPLATE_DISCOVERY_TAGS:,} current Tags so identity deduplication cannot become partial."
            )
        current=self._revalidate(candidate,progress,cancelled)
        if cancelled():raise InterruptedError()
        if len(current.anchor_ids)!=1:
            raise TagTemplateExpansionStaleError("The selected family no longer has one stable anchor.")
        anchor_id=current.anchor_ids[0]
        anchor=self.tags.tags.get(anchor_id)
        if anchor is None:
            raise TagTemplateExpansionStaleError("The selected stable anchor is no longer available.")

        definitions=self.foundation.modifier_group_definitions(
            self.tags,self.categories,self.modifier_roles,
        )
        values=tuple(definitions.get(current.modifier_family_id,()))
        retained=values[:self.max_values]
        policy=self._policy_for(anchor_id)
        canonical,aliases=self._identity_indexes()
        resolutions=self._composition_resolutions()
        removed_index,legacy_removed_ids=self._removed_identity_index()
        family_values={str(value.id) for value in values}
        rendered=[]
        for value in retained:
            target=(
                f"{value.display} {anchor.display}"
                if current.token_position=="prefix" else
                f"{anchor.display} {value.display}"
            )
            rendered.append((value," ".join(target.split()),normalize_tag(target)))
        target_values: dict[str,set[str]]={}
        for value,_target,key in rendered:
            if key:target_values.setdefault(key,set()).add(str(value.id))

        items=[]; total=len(retained); stride=max(1,total//200) if total else 1
        for index,(value,target,key) in enumerate(rendered,1):
            if cancelled():raise InterruptedError()
            value_id=str(value.id); reasons=[]; identity_basis="No active exact identity"
            owner_ids=(); outcome=TAG_TEMPLATE_MISSING
            if not key or key==normalize_tag(str(anchor.display)):
                outcome=TAG_TEMPLATE_INVALID
                reasons.append("The rendered value does not produce a distinct valid Tag name.")
            elif len(target_values.get(key,()))>1:
                outcome=TAG_TEMPLATE_CONFLICTING
                reasons.append("More than one reviewed modifier value renders the same canonical Tag name.")
            elif not self._allows_value(policy,current.modifier_family_id,value):
                outcome=TAG_TEMPLATE_EXCLUDED
                reasons.append(
                    f"The selected anchor's reviewed {current.modifier_family} scope excludes {value.display}."
                )
            else:
                canonical_ids=canonical.get(key,())
                alias_ids=aliases.get(key,())
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
                    removed=self._removed_ids(key,removed_index,legacy_removed_ids)
                    if removed:
                        outcome=TAG_TEMPLATE_TOMBSTONED; owner_ids=removed
                        identity_basis="Exact deleted-name history"
                        reasons.append("Deletion history reserves this exact identity; it will not be silently recreated.")
                    elif key in self.ignore_values:
                        outcome=TAG_TEMPLATE_IGNORED
                        identity_basis="Active exact Automation ignore rule"
                        reasons.append("An active exact ignore rule reserves this rendered Tag for review.")
                if outcome in {TAG_TEMPLATE_EXACT_EXISTING,TAG_TEMPLATE_ALIAS_EQUIVALENT}:
                    existing_reasons=self._existing_holds(
                        owner_ids,anchor_id=anchor_id,
                        family_id=current.modifier_family_id,value_id=value_id,
                        resolutions=resolutions,family_values=family_values,
                    )
                    if existing_reasons:
                        outcome=TAG_TEMPLATE_CONFLICTING; reasons.extend(existing_reasons)

            existing_records=tuple(
                self.tags.tags[item] for item in owner_ids if item in self.tags.tags
            )
            digest=sha256(f"{current.id}\x1f{value_id}".encode("utf-8")).hexdigest()[:20]
            items.append(TagTemplateExpansionItem(
                id=f"tag_template_expansion.{digest}",value_id=value_id,
                value=str(value.display),generated_tag=target,normalized_tag=key,
                outcome=outcome,identity_basis=identity_basis,
                existing_tag_ids=tuple(owner_ids),
                existing_tags=tuple(record.display for record in existing_records),
                current_categories=_ordered_unique(
                    self._category_label(str(record.category or ""))
                    for record in existing_records
                ),
                hold_reasons=_ordered_unique(reasons),
            ))
            if progress and (index==total or index%stride==0):
                progress(index,total,f"Expanding {value.display}")
        return TagTemplateExpansionReport(
            current,tuple(items),len(retained),max(0,len(values)-len(retained)),
        )
