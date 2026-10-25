from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .category_base_relationships import (
    category_base_relationship_index,
    direct_category_base_authority,
)
from .category_registry import exact_category_anchor_ids
from .relationship_workspace import RelationshipWorkspaceService
from .tag_collection import normalize_tag
from .tag_semantic_relationships import (
    effective_tag_semantic_links_from_index,
    tag_semantic_relationship_index,
)


MAX_CATEGORY_BASE_DISCOVERY_CATEGORIES = 10_000
MAX_CATEGORY_BASE_DISCOVERY_CANDIDATES = 1_000


@dataclass(frozen=True)
class CategoryBaseCandidate:
    category_id: str
    category: str
    category_path: str
    target_id: str
    target: str
    match_basis: str
    before: str
    after: str
    effective_target_unchanged: bool
    direct_member_count: int
    affected_direct_member_count: int
    descendant_category_count: int
    descendant_member_count: int
    tag_specific_base_override_count: int
    downstream_effects: tuple[str, ...]

    @property
    def independently_safe(self) -> bool:
        return (
            self.effective_target_unchanged
            and self.descendant_category_count == 0
        )

    @property
    def evidence_label(self) -> str:
        return {
            "stable_id":"Stable ID equals Category ID",
            "exact_canonical_name":"Exact canonical Tag name",
            "declared_alias":"Exact declared Other Name",
        }.get(self.match_basis,self.match_basis.replace("_"," ").title())

    def handoff_scope(self) -> dict[str, Any]:
        return {
            "origin":"category_base_discovery",
            "review_view_id":"all",
            "selected_kind":"category",
            "selected_id":self.category_id,
            "category_id":self.category_id,
            "category":self.category,
            "category_path":self.category_path,
            "target_id":self.target_id,
            "target":self.target,
            "match_basis":self.match_basis,
            "evidence_label":self.evidence_label,
            "before":self.before,
            "after":self.after,
            "direct_member_count":self.direct_member_count,
            "affected_direct_member_count":self.affected_direct_member_count,
            "descendant_category_count":self.descendant_category_count,
            "descendant_member_count":self.descendant_member_count,
            "tag_specific_base_override_count":self.tag_specific_base_override_count,
            "independently_safe":self.independently_safe,
            "effective_target_unchanged":self.effective_target_unchanged,
            "downstream_effects":list(self.downstream_effects),
        }


@dataclass(frozen=True)
class CategoryBaseDiscoveryReport:
    candidates: tuple[CategoryBaseCandidate, ...]
    processed_categories: int
    bounded_categories: int
    bounded_candidates: int

    @property
    def independently_safe(self) -> tuple[CategoryBaseCandidate, ...]:
        return tuple(item for item in self.candidates if item.independently_safe)

    @property
    def broader_scope_required(self) -> tuple[CategoryBaseCandidate, ...]:
        return tuple(item for item in self.candidates if not item.independently_safe)


class CategoryBaseDiscoveryService:
    """Report exact Category Base candidates without creating authority."""

    def __init__(
        self,
        tags: Any,
        categories: Any,
        *,
        default_relationship_path: str | Path,
        user_relationship_path: str | Path,
        workspace: RelationshipWorkspaceService | None = None,
        max_categories: int = MAX_CATEGORY_BASE_DISCOVERY_CATEGORIES,
        max_candidates: int = MAX_CATEGORY_BASE_DISCOVERY_CANDIDATES,
    ) -> None:
        self.tags=tags
        self.categories=categories
        self.workspace=workspace or RelationshipWorkspaceService(
            default_relationship_path,user_relationship_path,tags=tags,
        )
        self.max_categories=max(1,int(max_categories))
        self.max_candidates=max(1,int(max_candidates))

    @staticmethod
    def _descendant_ids(
        category_id: str,children: dict[str,list[str]],
    ) -> set[str]:
        result=set(); pending=list(children.get(str(category_id),()))
        while pending:
            value=pending.pop()
            if value in result:continue
            result.add(value); pending.extend(children.get(value,()))
        return result

    @classmethod
    def _descendant_totals(
        cls,
        category_ids: set[str],
        children: dict[str,list[str]],
        parents: dict[str,str],
        own_member_counts: dict[str,int],
        own_override_counts: dict[str,int],
    ) -> dict[str,tuple[int,int,int]]:
        """Aggregate descendant, member, and override counts once per tree."""
        descendant_counts={category_id:0 for category_id in category_ids}
        member_counts={
            category_id:int(own_member_counts.get(category_id,0))
            for category_id in category_ids
        }
        override_counts={
            category_id:int(own_override_counts.get(category_id,0))
            for category_id in category_ids
        }
        remaining_children={
            category_id:sum(child in category_ids for child in children.get(category_id,()))
            for category_id in category_ids
        }
        pending=[
            category_id for category_id,count in remaining_children.items()
            if count==0
        ]
        completed=set()
        while pending:
            category_id=pending.pop()
            if category_id in completed:continue
            completed.add(category_id)
            parent_id=parents.get(category_id,"")
            if not parent_id or parent_id not in category_ids or parent_id==category_id:
                continue
            descendant_counts[parent_id]+=1+descendant_counts[category_id]
            member_counts[parent_id]+=member_counts[category_id]
            override_counts[parent_id]+=override_counts[category_id]
            remaining_children[parent_id]-=1
            if remaining_children[parent_id]==0:pending.append(parent_id)

        # Invalid cyclic input must stay bounded and visible rather than hang.
        for category_id in category_ids-completed:
            descendants=cls._descendant_ids(category_id,children)
            descendants.discard(category_id)
            descendant_counts[category_id]=len(descendants)
            member_counts[category_id]=sum(
                own_member_counts.get(item,0)
                for item in descendants|{category_id}
            )
            override_counts[category_id]=sum(
                own_override_counts.get(item,0)
                for item in descendants|{category_id}
            )
        return {
            category_id:(
                descendant_counts[category_id],
                member_counts[category_id],
                override_counts[category_id],
            )
            for category_id in category_ids
        }

    def discover(self) -> CategoryBaseDiscoveryReport:
        relationships=self.workspace.effective
        base_index=category_base_relationship_index(relationships)
        semantic_index=tag_semantic_relationship_index(relationships)
        derived=exact_category_anchor_ids(
            self.categories.categories.values(),self.tags.tags.values(),
        )
        children: dict[str,list[str]]={}
        parents: dict[str,str]={}
        for record in self.categories.categories.values():
            children.setdefault(str(record.parent_id or ""),[]).append(record.id)
            parents[record.id]=str(record.parent_id or "")
        tags_by_category: dict[str,dict[str,Any]]={}
        override_tag_ids=set()
        for record in self.tags.tags.values():
            tags_by_category.setdefault(str(record.category),{})[record.id]=record
            if effective_tag_semantic_links_from_index(
                record,semantic_index,
            ).parent_id:
                override_tag_ids.add(record.id)

        category_ids=set(self.categories.categories)
        descendant_totals=self._descendant_totals(
            category_ids,children,parents,
            {
                category_id:len(records)
                for category_id,records in tags_by_category.items()
            },
            {
                category_id:sum(record.id in override_tag_ids for record in records.values())
                for category_id,records in tags_by_category.items()
            },
        )

        ordered=sorted(
            self.categories.categories.values(),
            key=lambda item:(item.name.casefold(),item.id),
        )
        scanned=ordered[:self.max_categories]
        bounded_categories=max(0,len(ordered)-len(scanned))
        candidates=[]; candidate_count=0
        for category in scanned:
            direct=direct_category_base_authority(category,base_index)
            if direct.represented or direct.ambiguous or direct.policy=="own":
                continue
            direct_tags=tags_by_category.get(category.id,{})
            matching_ids=set()
            stable_match=self.tags.tags.get(category.id)
            if stable_match is not None and stable_match.category==category.id:
                matching_ids.add(stable_match.id)
            matching_ids.update(
                owner_id
                for owner_id in self.tags.identity_owner_ids(category.name)
                if owner_id in direct_tags
            )
            derived_id=str(derived.get(category.id) or "")
            if derived_id:matching_ids.add(derived_id)
            if len(matching_ids)!=1:continue
            target_id=next(iter(matching_ids))
            target=direct_tags.get(target_id)
            if target is None:continue
            category_key=normalize_tag(category.name)
            if target.id==category.id:
                basis="stable_id"
            elif normalize_tag(target.display)==category_key:
                basis="exact_canonical_name"
            elif category_key in {
                normalize_tag(alias) for alias in self.tags.effective_aliases(target)
            }:
                basis="declared_alias"
            else:
                continue

            descendant_count,subtree_member_count,subtree_override_count=(
                descendant_totals.get(category.id,(0,len(direct_tags),0))
            )
            descendant_member_count=max(0,subtree_member_count-len(direct_tags))
            tag_specific_bases=max(
                0,subtree_override_count-int(target.id in override_tag_ids),
            )
            effective_id,effective_source,_policy=self.categories.effective_base_tag(
                category.id,derived_anchors=derived,relationship_index=base_index,
            )
            unchanged=effective_id==target.id and effective_source==category.id
            candidate_count+=1
            if len(candidates)>=self.max_candidates:continue
            candidates.append(CategoryBaseCandidate(
                category_id=category.id,
                category=category.name,
                category_path=self.categories.path_label(category.id),
                target_id=target.id,
                target=target.display,
                match_basis=basis,
                before=(
                    "Derived exact-identity fallback; no direct canonical Category Base relationship."
                    if unchanged else
                    "No direct or derived Category Base target; the exact identity match is review evidence only."
                ),
                after=(
                    "One reviewed canonical Category Base relationship with the same effective Tag target."
                    if unchanged else
                    "One reviewed canonical Category Base relationship with a new effective Tag target."
                ),
                effective_target_unchanged=unchanged,
                direct_member_count=len(direct_tags),
                affected_direct_member_count=max(0,len(direct_tags)-int(target.id in direct_tags)),
                descendant_category_count=descendant_count,
                descendant_member_count=descendant_member_count,
                tag_specific_base_override_count=tag_specific_bases,
                downstream_effects=(
                    (
                        "Would replace derived name matching with portable, recoverable reviewed Category Base authority."
                        if unchanged else
                        "Would establish a new inherited Category Base target for members without a Tag-specific Broader Tag."
                    ),
                    "Existing Tag-specific Broader Tags would remain authoritative.",
                    "Would not confirm a Tag on an image or rewrite Image Knowledge.",
                ),
            ))

        candidates.sort(key=lambda item:(item.category_path.casefold(),item.category_id))
        bounded_candidates=max(0,candidate_count-len(candidates))
        retained=tuple(candidates)
        return CategoryBaseDiscoveryReport(
            retained,len(scanned),bounded_categories,bounded_candidates,
        )
