from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

from .annotation import GLOBAL_TAG_GROUPS, SCENE_TAG_GROUPS, ImageAnnotation
from .constraint_resolver import EffectiveConstraintResolver
from .tag_collection import TagCollection
from .category_registry import exact_category_anchor_ids
from .tag_database import TagDatabase, TagRecord
from .paths import DEFAULTS_DIR, RELATIONSHIPS_PATH, TAGS_PATH
from .relationship_workspace import RelationshipWorkspaceService
from .tag_semantic_relationships import TagSemanticRelationshipService
from .consumer_policies import (
    EXACT_TAG,IMAGE_IDENTIFICATION,OPT_IN_FACT_EXPANSION,SUGGEST_FOR_REVIEW,
    ConsumerPolicyDecision,ConsumerPolicyTraceStep,
    ConsumerPolicyResolver,
)
from .knowledge_relationships import (
    ADVISORY_ASSOCIATION,
    ASSOCIATED_CHARACTER,
    CATEGORY_BASE,
    CHARACTER_SOURCE,
    ENTAILMENT,
    ENTITY_TAG,
    OTHER_APPEARANCE,
    MODIFIER_VALUE,
    SPECIES_TRAIT,
    SPECIFICITY,
)


RELATION_LABELS = {
    "base": "Broader Tag",
    "category_base": "Inherited Category Base",
    "implies": "Also Means",
    "template_modifier": "Reviewed Template Modifier",
    "character_identity": "Reviewed Character Identity",
    "character_source": "Reviewed Character Source",
    "character_outfit": "Reviewed Character Outfit",
    "other_appearance": "Reviewed Other Appearance",
    "species_trait": "Reviewed Species / Trait",
    "usually_related": "Usually Related — Review Only",
}

POLICY_KIND_BY_RELATION_ALIAS = {
    "base": SPECIFICITY,
    "category_base": CATEGORY_BASE,
    "implies": ENTAILMENT,
    "usually_related": ADVISORY_ASSOCIATION,
    "character_identity": ENTITY_TAG,
    "character_source": CHARACTER_SOURCE,
    # An already assigned outfit's representative Tag is an entity-tag fact;
    # the broader Associated Character relation alone remains manual context.
    "character_outfit": ENTITY_TAG,
    "other_appearance": OTHER_APPEARANCE,
    "species_trait": SPECIES_TRAIT,
}

CHARACTER_GROUP_LABELS = {
    "appearance": "Appearance",
    "clothing": "Clothing",
    "pose": "Pose",
    "expression": "Expression",
    "actions": "Actions",
    "other": "Other",
}


@dataclass(frozen=True)
class RelatedTagSuggestion:
    """One bounded, explainable missing-association suggestion."""

    tag_id: str
    display: str
    supporting_tag_id: str
    supporting_display: str
    path_ids: tuple[str, ...]
    path_displays: tuple[str, ...]
    relation_kinds: tuple[str, ...]
    scope: str = "Image-wide"
    source: str = "Reviewed System Knowledge"
    blocked_reason: str = ""
    conflict_tag_ids: tuple[str, ...] = ()
    owner_id: str = ""
    owner_group: str = ""
    owner_kind: str = "image"
    relationship_ids: tuple[str, ...] = ()
    canonical_relationship_kinds: tuple[str, ...] = ()
    policy_decisions: tuple[ConsumerPolicyDecision, ...] = ()

    @property
    def direct(self) -> bool:
        return len(self.relation_kinds) == 1

    @property
    def eligible(self) -> bool:
        return not self.blocked_reason

    @property
    def relationship_label(self) -> str:
        return " \u2192 ".join(
            RELATION_LABELS.get(kind, kind) for kind in self.relation_kinds
        )

    @property
    def path_label(self) -> str:
        return " \u2192 ".join(self.path_displays)

    @property
    def support_label(self) -> str:
        return "Direct" if self.direct else f"Transitive ({len(self.relation_kinds)} steps)"

    @property
    def policy_permits_review(self) -> bool:
        return bool(self.policy_decisions) and all(
            decision.permits_related_review for decision in self.policy_decisions
        )

    @property
    def policy_permits_fact_expansion(self) -> bool:
        return bool(self.policy_decisions) and all(
            decision.permits_fact_expansion for decision in self.policy_decisions
        )

    @property
    def applicability_label(self) -> str:
        modes = tuple(dict.fromkeys(decision.mode for decision in self.policy_decisions))
        if not modes:
            return "Policy unavailable — excluded"
        if modes == ("suggest_for_review",):
            return "Review only"
        if all(mode == "opt_in_fact_expansion" for mode in modes):
            return "Eligible for reviewed opt-in expansion"
        return " → ".join(mode.replace("_", " ").title() for mode in modes)

    @property
    def applicability_explanation(self) -> str:
        return "\n".join(
            line
            for decision in self.policy_decisions
            for line in (*decision.explanation_path, f"Decision: {decision.explanation}")
        )


@dataclass(frozen=True)
class _TraversalEdge:
    target_id: str
    relationship_id: str
    relationship_kind: str
    relation_alias: str
    scope: str
    decision: ConsumerPolicyDecision


def related_tag_destination_container(
    annotation: ImageAnnotation,
    *,
    owner_kind: str,
    owner_id: str = "",
    owner_group: str = "",
) -> list[str] | None:
    """Resolve one reviewed suggestion to its exact Image Knowledge container."""

    kind = owner_kind or "image"
    group = owner_group
    if kind == "image":
        return annotation.sdxl_tags
    if kind == "global":
        return annotation.global_tags.setdefault(group or "other", [])
    if kind == "scene":
        return annotation.scene.setdefault(group or "other", [])
    if kind == "visual_style":
        return annotation.visual_style.style_tags
    if kind == "custom_group":
        if not group:
            return None
        return annotation.custom_groups.setdefault(group, [])
    if kind == "character":
        owner = next(
            (
                assignment
                for assignment in annotation.characters
                if assignment.id == owner_id
            ),
            None,
        )
        if owner is None:
            return None
        return owner.tags.setdefault(group or "other", [])
    if kind == "object":
        owner = annotation.get_object(owner_id)
        if owner is None:
            return None
        return owner.tags
    if kind == "context":
        owner = annotation.get_context(owner_id)
        if owner is None:
            return None
        return owner.tags
    return None


def add_related_tag_to_destination(
    annotation: ImageAnnotation,
    *,
    display: str,
    owner_kind: str,
    owner_id: str = "",
    owner_group: str = "",
) -> bool:
    """Add one reviewed tag without flattening its typed destination."""

    container = related_tag_destination_container(
        annotation,
        owner_kind=owner_kind,
        owner_id=owner_id,
        owner_group=owner_group,
    )
    if container is None:
        return False
    values = TagCollection(container)
    changed = values.add(display)
    if not changed:
        return False
    container[:] = values.to_list()
    absent = TagCollection(annotation.confirmed_absent)
    absent.remove(display)
    annotation.confirmed_absent = absent.to_list()
    return True


def add_related_tag_to_annotation(
    annotation: ImageAnnotation,
    suggestion: RelatedTagSuggestion,
) -> bool:
    """Add a reviewed suggestion to its exact Image Knowledge destination."""

    return add_related_tag_to_destination(
        annotation,
        display=suggestion.display,
        owner_kind=suggestion.owner_kind,
        owner_id=suggestion.owner_id,
        owner_group=suggestion.owner_group,
    )


class RelatedTagSuggestionService:
    """Traverse only reviewed, stable-ID relationships within strict bounds."""

    DEFAULT_MAX_DEPTH = 3
    DEFAULT_MAX_RESULTS = 60

    def __init__(
        self,
        knowledge: TagDatabase,
        categories=None,
        characters=None,
        *,
        constraint_resolver: EffectiveConstraintResolver | None = None,
        semantic_relationships: TagSemanticRelationshipService | None = None,
        consumer_policy_resolver: ConsumerPolicyResolver | None = None,
        tag_template_service=None,
    ):
        self.knowledge = knowledge
        self.categories = categories
        self.characters = characters
        self.tag_template_service=tag_template_service
        if (
            self.tag_template_service is None
            and Path(knowledge.path).resolve()==TAGS_PATH.resolve()
        ):
            try:
                from .tag_template_projection import active_tag_template_projection_service
                self.tag_template_service=active_tag_template_projection_service(knowledge)
            except (OSError,RuntimeError,ValueError):
                self.tag_template_service=None
        self.constraints = constraint_resolver or EffectiveConstraintResolver.load(
            knowledge
        )
        user_path = (
            RELATIONSHIPS_PATH
            if Path(knowledge.path).resolve() == TAGS_PATH.resolve()
            else Path(knowledge.path).with_name("relationships.json")
        )
        workspace = None
        workspace_error = ""
        if semantic_relationships is None:
            try:
                workspace = RelationshipWorkspaceService(
                    DEFAULTS_DIR / "relationships.json", user_path,
                    tags=knowledge,
                )
            except (OSError, RuntimeError, ValueError) as error:
                workspace_error = str(error)
        self.semantic_relationships = semantic_relationships
        self.semantic_relationship_error = ""
        if self.semantic_relationships is None:
            if workspace is not None:
                try:
                    self.semantic_relationships = TagSemanticRelationshipService(
                        knowledge,
                        default_path=DEFAULTS_DIR / "relationships.json",
                        user_path=user_path,
                        workspace=workspace,
                    )
                except (OSError, RuntimeError, ValueError) as error:
                    self.semantic_relationship_error = str(error)
            else:
                self.semantic_relationship_error = workspace_error
        self.consumer_policy_error = ""
        self.consumer_policies = consumer_policy_resolver
        if self.consumer_policies is None:
            try:
                self.consumer_policies = ConsumerPolicyResolver.from_paths(
                    DEFAULTS_DIR / "consumer_policies.json",
                    user_path.with_name("consumer_policies.json"),
                )
            except (OSError, RuntimeError, ValueError) as error:
                self.consumer_policy_error = str(error)
                self.consumer_policies = ConsumerPolicyResolver(())
        category_relationships=getattr(
            self.semantic_relationships,"effective_relationships",None,
        )
        if category_relationships is None:
            category_relationships = (
                workspace.effective
                if workspace is not None
                else getattr(
                    getattr(self.semantic_relationships, "workspace", None),
                    "effective",
                    (),
                )
            )
        self._effective_relationships = tuple(category_relationships)
        self._active_relationships_by_source: dict[str, list] = {}
        self._category_base_relationships: dict[tuple[str, str], object] = {}
        for relationship in self._effective_relationships:
            if relationship.disabled or not relationship.reviewed:
                continue
            source = relationship.source_endpoint
            if relationship.kind == ADVISORY_ASSOCIATION and not relationship.directed:
                for endpoint in relationship.endpoints:
                    if endpoint.kind == "tag":
                        self._active_relationships_by_source.setdefault(
                            endpoint.id, []
                        ).append(relationship)
                continue
            if source is None:
                continue
            if source.kind == "tag":
                self._active_relationships_by_source.setdefault(source.id, []).append(
                    relationship
                )
            elif relationship.kind == CATEGORY_BASE and source.kind == "category":
                for endpoint in relationship.target_endpoints:
                    if endpoint.kind == "tag":
                        self._category_base_relationships[(source.id, endpoint.id)] = (
                            relationship
                        )
        if (
            self.categories is not None
            and hasattr(self.categories, "configure_category_base_authority")
        ):
            self.categories.configure_category_base_authority(
                category_relationships,
            )
        self._derived_category_anchors: dict[str,str] | None = None

    def dependency_tag_ids(
        self,
        confirmed_tags: Iterable[str],
        *,
        confirmed_absent: Iterable[str] = (),
        rejected_tags: Iterable[str] = (),
        annotation=None,
    ) -> tuple[str, ...]:
        """Return every stable Tag ID that can affect one suggestion pass.

        This deliberately includes reviewed character-derived roots even when
        they do not currently emit an eligible suggestion.  A later exact
        relationship or policy change must still find the affected image.
        """
        tag_ids = set(self._identity_ids(confirmed_tags))
        tag_ids.update(self._identity_ids(confirmed_absent))
        tag_ids.update(self._identity_ids(rejected_tags))
        if annotation is None:
            return tuple(sorted(tag_ids))

        tag_ids.update(
            self._identity_ids(
                [
                    *getattr(annotation, "sdxl_tags", ()),
                    *getattr(annotation, "uncategorized", ()),
                    *self._structured_image_tags(annotation),
                ]
            )
        )
        identities = getattr(annotation, "tag_identities", {})
        if isinstance(identities, dict):
            tag_ids.update(
                str(stable_id)
                for stable_id in identities.values()
                if str(stable_id) in self.knowledge.tags
            )
        for assignment in getattr(annotation, "objects", ()):
            stable_id = str(getattr(assignment, "identity_tag_id", "") or "")
            if stable_id in self.knowledge.tags:
                tag_ids.add(stable_id)

        if self.characters is None:
            return tuple(sorted(tag_ids))
        for assignment in getattr(annotation, "characters", ()):
            character_id = str(getattr(assignment, "character_id", "") or "")
            character = self.characters.characters.get(character_id)
            character_tag = self._reviewed_character_tag(character, "character")
            if character_tag is not None:
                tag_ids.add(character_tag.id)

            source_id = (
                self.characters.character_source_id("character", character_id)
                if character is not None else ""
            )
            source = self.characters.series.get(source_id) if source_id else None
            source_tag = self._reviewed_character_tag(source, "series")
            if source_tag is not None:
                tag_ids.add(source_tag.id)

            trait_ids = list(
                self.characters.species_trait_target_ids(
                    "character", character_id,
                )
                if character_id else ()
            )
            if character_tag is not None:
                trait_ids.extend(
                    self.characters.species_trait_target_ids(
                        "tag", character_tag.id,
                    )
                )
            tag_ids.update(
                stable_id for stable_id in trait_ids
                if stable_id in self.knowledge.tags
            )
            for appearance_source_id in (
                self.characters.other_appearance_ids("character", character_id)
                if character_id else ()
            ):
                appearance_tag = self._reviewed_character_tag(
                    self.characters.series.get(appearance_source_id), "series",
                )
                if appearance_tag is not None:
                    tag_ids.add(appearance_tag.id)

            for raw_outfit_id in getattr(assignment, "outfit_ids", ()):
                outfit_id = str(raw_outfit_id)
                outfit = self.characters.outfits.get(outfit_id)
                outfit_tag = self._reviewed_character_tag(outfit, "outfit")
                if outfit_tag is not None:
                    tag_ids.add(outfit_tag.id)
                outfit_source_id = (
                    self.characters.character_source_id("outfit", outfit_id)
                    if outfit is not None else ""
                )
                outfit_source_tag = self._reviewed_character_tag(
                    self.characters.series.get(outfit_source_id), "series",
                )
                if outfit_source_tag is not None:
                    tag_ids.add(outfit_source_tag.id)
                for appearance_source_id in (
                    self.characters.other_appearance_ids("outfit", outfit_id)
                    if outfit is not None else ()
                ):
                    appearance_tag = self._reviewed_character_tag(
                        self.characters.series.get(appearance_source_id), "series",
                    )
                    if appearance_tag is not None:
                        tag_ids.add(appearance_tag.id)
        return tuple(sorted(tag_ids))

    def _category_anchor_ids(self):
        if self.categories is None or not hasattr(self.categories,"categories"):
            return None
        categories=tuple(self.categories.categories.values())
        # The published identity index includes every ambiguous canonical owner.
        # Use it only to narrow candidates; the shared exact-name/category rule
        # still rejects aliases, wrong-category matches and duplicate anchors.
        candidates={}
        for category in categories:
            for tag_id in self.knowledge.identity_owner_ids(category.name):
                record=self.knowledge.tags.get(tag_id)
                if record is not None:
                    candidates[tag_id]=record
        return exact_category_anchor_ids(categories,candidates.values())

    def suggest(
        self,
        confirmed_tags: Iterable[str],
        *,
        relationship_roots: Iterable[str] | None = None,
        confirmed_absent: Iterable[str] = (),
        rejected_tags: Iterable[str] = (),
        annotation=None,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> tuple[RelatedTagSuggestion, ...]:
        max_depth = max(0, int(max_depth))
        max_results = max(0, int(max_results))
        if not max_depth or not max_results:
            return ()

        self._derived_category_anchors=self._category_anchor_ids()

        confirmed_records = self._records(confirmed_tags)
        image_wide_confirmed_ids = {record.id for record in confirmed_records}
        if annotation is not None:
            image_wide_confirmed_ids.update(
                self._identity_ids(
                    [
                        *annotation.sdxl_tags,
                        *annotation.uncategorized,
                        *self._structured_image_tags(annotation),
                    ]
                )
            )
        if relationship_roots is None:
            root_records = confirmed_records
            relationship_root_ids = None
        else:
            relationship_root_ids = {
                record.id for record in self._records(relationship_roots)
            }
            root_records = tuple(
                record
                for record in confirmed_records
                if record.id in relationship_root_ids
            )
        absent_ids = self._identity_ids(confirmed_absent)
        rejected_ids = self._identity_ids(rejected_tags)
        best: dict[tuple[str, str], RelatedTagSuggestion] = {}

        for support in root_records:
            queue = deque(
                [(support.id, (support.id,), (), (), (), frozenset({support.id}))]
            )
            while queue:
                (
                    current_id, path_ids, relation_kinds, relationship_ids,
                    policy_decisions, seen,
                ) = queue.popleft()
                if len(relation_kinds) >= max_depth:
                    continue
                current = self.knowledge.tags.get(current_id)
                if current is None:
                    continue
                for edge in self._outgoing(current):
                    if not edge.decision.permits_related_review:
                        continue
                    target_id = edge.target_id
                    if target_id in seen or target_id not in self.knowledge.tags:
                        continue
                    target = self.knowledge.tags[target_id]
                    next_path = (*path_ids, target_id)
                    next_relations = (*relation_kinds, edge.relation_alias)
                    next_relationship_ids = (*relationship_ids, edge.relationship_id)
                    next_decisions = (*policy_decisions, edge.decision)
                    if (
                        target_id not in image_wide_confirmed_ids
                        and target_id != support.id
                    ):
                        suggestion = self._suggestion(
                            target,
                            support,
                            next_path,
                            next_relations,
                            confirmed_ids=image_wide_confirmed_ids,
                            absent_ids=absent_ids,
                            rejected_ids=rejected_ids,
                            relationship_ids=next_relationship_ids,
                            canonical_relationship_kinds=tuple(
                                decision.selector_id
                                if decision.selector_type == "relationship_kind"
                                else POLICY_KIND_BY_RELATION_ALIAS.get(alias, alias)
                                for alias, decision in zip(
                                    next_relations, next_decisions
                                )
                            ),
                            policy_decisions=next_decisions,
                        )
                        key = (target_id, "")
                        prior = best.get(key)
                        if prior is None or self._rank(suggestion) < self._rank(prior):
                            best[key] = suggestion
                    if edge.decision.permits_fact_expansion:
                        queue.append(
                            (
                                target_id, next_path, next_relations,
                                next_relationship_ids, next_decisions,
                                seen | {target_id},
                            )
                        )

        if annotation is not None:
            for suggestion in self._image_scope_suggestions(
                annotation,
                image_wide_confirmed_ids=image_wide_confirmed_ids,
                relationship_root_ids=relationship_root_ids,
                absent_ids=absent_ids,
                rejected_ids=rejected_ids,
                max_depth=max_depth,
            ):
                key = (suggestion.tag_id, "")
                prior = best.get(key)
                if prior is None or self._rank(suggestion) < self._rank(prior):
                    best[key] = suggestion

        if annotation is not None and self.characters is not None:
            for suggestion in self._character_tag_suggestions(
                annotation,
                image_wide_confirmed_ids=image_wide_confirmed_ids,
                relationship_root_ids=relationship_root_ids,
                absent_ids=absent_ids,
                rejected_ids=rejected_ids,
                max_depth=max_depth,
            ):
                key = (suggestion.tag_id, suggestion.owner_id)
                prior = best.get(key)
                if prior is None or self._rank(suggestion) < self._rank(prior):
                    best[key] = suggestion
            for suggestion in self._character_suggestions(
                annotation,
                image_wide_confirmed_ids=image_wide_confirmed_ids,
                absent_ids=absent_ids,
                rejected_ids=rejected_ids,
            ):
                key = (suggestion.tag_id, suggestion.owner_id)
                prior = best.get(key)
                if prior is None or self._rank(suggestion) < self._rank(prior):
                    best[key] = suggestion

        if annotation is not None:
            for suggestion in self._object_tag_suggestions(
                annotation,
                image_wide_confirmed_ids=image_wide_confirmed_ids,
                relationship_root_ids=relationship_root_ids,
                absent_ids=absent_ids,
                rejected_ids=rejected_ids,
                max_depth=max_depth,
            ):
                key = (suggestion.tag_id, suggestion.owner_id)
                prior = best.get(key)
                if prior is None or self._rank(suggestion) < self._rank(prior):
                    best[key] = suggestion

        if self.tag_template_service is not None:
            for suggestion in self._template_suggestions(
                confirmed_records,annotation=annotation,
                confirmed_ids=image_wide_confirmed_ids,
                absent_ids=absent_ids,rejected_ids=rejected_ids,
                max_results=max_results,
            ):
                key=(suggestion.tag_id,suggestion.owner_id)
                prior=best.get(key)
                if prior is None or self._rank(suggestion)<self._rank(prior):
                    best[key]=suggestion

        eligible = (item for item in best.values() if item.policy_permits_review)
        return tuple(sorted(eligible, key=self._sort_key)[:max_results])

    def _template_suggestions(
        self,confirmed_records,*,annotation,confirmed_ids,absent_ids,rejected_ids,
        max_results: int,
    ) -> tuple[RelatedTagSuggestion,...]:
        """Adapt bounded materialized-template paths into the shared review model."""

        from .tag_template_projection import TagTemplateProjectionFact

        scopes=[(
            "image","image","","Image-wide",tuple(confirmed_records),
        )]
        if annotation is not None:
            scopes.extend(
                (owner_kind,owner_id,group,scope,self._records(values))
                for owner_kind,owner_id,group,scope,values
                in self._structured_image_scopes(annotation)
            )
            for assignment in getattr(annotation,"characters",()):
                owner_id=str(getattr(assignment,"id","") or getattr(assignment,"slot","") or "")
                values=[]
                for group_values in getattr(assignment,"tags",{}).values():
                    values.extend(group_values)
                scopes.append((
                    "character",owner_id,"other",
                    f"Character {getattr(assignment,'slot','')}",self._records(values),
                ))
            for assignment in getattr(annotation,"objects",()):
                owner_id=str(getattr(assignment,"id","") or getattr(assignment,"slot","") or "")
                values=[str(getattr(assignment,"identity_tag","") or "")]
                values.extend(getattr(assignment,"tags",()))
                scopes.append((
                    "object",owner_id,"other",
                    f"Object {getattr(assignment,'slot','')}",self._records(values),
                ))
        result=[]; seen=set()
        for owner_kind,owner_id,owner_group,scope,records in scopes:
            scoped_confirmed={item.id for item in records}
            template_owner_id=str(owner_id or owner_group or owner_kind or "image")
            for support in records:
                fact=TagTemplateProjectionFact(
                    support.id,support.display,owner_kind,template_owner_id,
                    source=scope,
                )
                associations=self.tag_template_service.association_suggestions(
                    fact,confirmed_tag_ids=(scoped_confirmed|set(confirmed_ids)),
                    confirmed_absent_ids=absent_ids,
                    rejected_tag_ids=rejected_ids,
                    max_suggestions=max_results,
                )
                for association in associations:
                    key=(association.tag_id,owner_kind,owner_id)
                    if key in seen:continue
                    seen.add(key)
                    mode=(
                        OPT_IN_FACT_EXPANSION
                        if association.automatic_eligible else SUGGEST_FOR_REVIEW
                    )
                    explanation=(
                        "This exact reviewed template policy permits opt-in fact expansion."
                        if association.automatic_eligible else
                        "This materialized template association requires review."
                    )
                    trace=(ConsumerPolicyTraceStep(
                        "reviewed template",EXACT_TAG,association.template_id,
                        "selected",explanation,association.template_id,mode,
                    ),)
                    decision=ConsumerPolicyDecision(
                        IMAGE_IDENTIFICATION,mode,association.template_id,
                        EXACT_TAG,association.template_id,"user",explanation,trace,
                    )
                    target=self.knowledge.tags.get(association.tag_id)
                    if target is None:continue
                    result.append(RelatedTagSuggestion(
                        tag_id=target.id,display=target.display,
                        supporting_tag_id=support.id,
                        supporting_display=support.display,
                        path_ids=(support.id,target.id),
                        path_displays=(support.display,target.display),
                        relation_kinds=("template_modifier",),scope=scope,
                        source=f"Reviewed reusable template {association.template_id}",
                        blocked_reason=association.blocked_reason,
                        owner_id=owner_id,owner_group=owner_group,
                        owner_kind=owner_kind,
                        relationship_ids=association.relationship_path,
                        canonical_relationship_kinds=(
                            association.relationship_kinds or tuple(
                                MODIFIER_VALUE for _item in association.relationship_path
                            )
                        ),policy_decisions=(decision,),
                    ))
                    if len(result)>=max_results:return tuple(result)
        return tuple(result)

    def _image_scope_suggestions(
        self,
        annotation,
        *,
        image_wide_confirmed_ids: set[str],
        relationship_root_ids: set[str] | None,
        absent_ids: set[str],
        rejected_ids: set[str],
        max_depth: int,
    ) -> tuple[RelatedTagSuggestion, ...]:
        """Traverse reviewed relationships without losing image group placement."""

        suggestions = []
        for owner_kind, owner_id, group, scope, values in self._structured_image_scopes(
            annotation
        ):
            group_records = self._records(values)
            support_records = (
                group_records
                if relationship_root_ids is None
                else tuple(
                    record
                    for record in group_records
                    if record.id in relationship_root_ids
                )
            )
            for support in support_records:
                queue = deque(
                    [(support.id, (support.id,), (), (), (), frozenset({support.id}))]
                )
                while queue:
                    (
                        current_id, path_ids, relation_kinds, relationship_ids,
                        policy_decisions, seen,
                    ) = queue.popleft()
                    if len(relation_kinds) >= max_depth:
                        continue
                    current = self.knowledge.tags.get(current_id)
                    if current is None:
                        continue
                    for edge in self._outgoing(current):
                        if not edge.decision.permits_related_review:
                            continue
                        target_id = edge.target_id
                        if target_id in seen or target_id not in self.knowledge.tags:
                            continue
                        target = self.knowledge.tags[target_id]
                        next_path = (*path_ids, target_id)
                        next_relations = (*relation_kinds, edge.relation_alias)
                        next_relationship_ids = (*relationship_ids, edge.relationship_id)
                        next_decisions = (*policy_decisions, edge.decision)
                        if (
                            target_id not in image_wide_confirmed_ids
                            and target_id != support.id
                        ):
                            suggestions.append(
                                self._scoped_suggestion(
                                    target,
                                    supporting_tag_id=support.id,
                                    supporting_display=support.display,
                                    path_ids=next_path,
                                    path_displays=tuple(
                                        self.knowledge.tags[tag_id].display
                                        for tag_id in next_path
                                    ),
                                    relation_kinds=next_relations,
                                    scope=scope,
                                    owner_id=owner_id,
                                    owner_group=group,
                                    owner_kind=owner_kind,
                                    confirmed_ids=(
                                        self._identity_ids(values)
                                        if owner_kind == "context" else
                                        image_wide_confirmed_ids
                                    ),
                                    absent_ids=absent_ids,
                                    rejected_ids=rejected_ids,
                                    source="Reviewed System Knowledge",
                                    relationship_ids=next_relationship_ids,
                                    canonical_relationship_kinds=tuple(
                                        POLICY_KIND_BY_RELATION_ALIAS.get(kind, kind)
                                        for kind in next_relations
                                    ),
                                    policy_decisions=next_decisions,
                                )
                            )
                        if edge.decision.permits_fact_expansion:
                            queue.append(
                                (
                                    target_id, next_path, next_relations,
                                    next_relationship_ids, next_decisions,
                                    seen | {target_id},
                                )
                            )
        return tuple(suggestions)

    @staticmethod
    def _structured_image_scopes(annotation):
        def ordered_groups(values, preferred):
            if not isinstance(values, dict):
                return ()
            keys = [
                *(group for group in preferred if group in values),
                *sorted(
                    (
                        str(group)
                        for group in values
                        if str(group) not in preferred
                    ),
                    key=str.casefold,
                ),
            ]
            return tuple((key, values.get(key, ())) for key in keys)

        scopes = []
        for group, values in ordered_groups(
            getattr(annotation, "global_tags", {}), GLOBAL_TAG_GROUPS
        ):
            scopes.append(
                (
                    "global",
                    "",
                    group,
                    f"Image \u2014 Global \u2014 {RelatedTagSuggestionService._group_label(group)}",
                    values,
                )
            )
        for group, values in ordered_groups(
            getattr(annotation, "scene", {}), SCENE_TAG_GROUPS
        ):
            scopes.append(
                (
                    "scene",
                    "",
                    group,
                    f"Image \u2014 Scene \u2014 {RelatedTagSuggestionService._group_label(group)}",
                    values,
                )
            )
        style = getattr(getattr(annotation, "visual_style", None), "style_tags", ())
        scopes.append(("visual_style", "", "style_tags", "Image \u2014 Visual Style", style))
        for group, values in ordered_groups(
            getattr(annotation, "custom_groups", {}), ()
        ):
            scopes.append(
                (
                    "custom_group",
                    "",
                    group,
                    f"Image \u2014 Custom \u2014 {RelatedTagSuggestionService._group_label(group)}",
                    values,
                )
            )
        for context in getattr(annotation, "contexts", ()):
            scopes.append(
                (
                    "context",
                    context.id,
                    "tags",
                    f"Image — {context.display_label}",
                    context.tags,
                )
            )
        return tuple(scopes)

    @classmethod
    def _structured_image_tags(cls, annotation):
        values = TagCollection()
        for _kind, _owner_id, _group, _scope, group_values in cls._structured_image_scopes(
            annotation
        ):
            values.extend(group_values)
        return values.to_list()

    @staticmethod
    def _group_label(group: str) -> str:
        return str(group).replace("_", " ").strip().title() or "Other"

    def _character_tag_suggestions(
        self,
        annotation,
        *,
        image_wide_confirmed_ids: set[str],
        relationship_root_ids: set[str] | None,
        absent_ids: set[str],
        rejected_ids: set[str],
        max_depth: int,
    ) -> tuple[RelatedTagSuggestion, ...]:
        """Traverse reviewed relationships without losing character ownership."""

        suggestions = []
        for assignment in sorted(
            getattr(annotation, "characters", ()),
            key=lambda item: (getattr(item, "slot", 0), getattr(item, "id", "")),
        ):
            owner_id = str(getattr(assignment, "id", "") or "")
            if not owner_id:
                continue
            character_id = str(getattr(assignment, "character_id", "") or "")
            character = self.characters.characters.get(character_id)
            owner_label = self._owner_label(assignment, character)
            scoped_ids = self._identity_ids(assignment.all_tags())
            confirmed_ids = image_wide_confirmed_ids | scoped_ids
            groups = getattr(assignment, "tags", {})
            if not isinstance(groups, dict):
                continue
            character_tag = self._reviewed_character_tag(character, "character")

            ordered_groups = [
                *(
                    group
                    for group in CHARACTER_GROUP_LABELS
                    if group in groups
                ),
                *sorted(
                    (
                        str(group)
                        for group in groups
                        if str(group) not in CHARACTER_GROUP_LABELS
                    ),
                    key=str.casefold,
                ),
            ]
            for group in ordered_groups:
                group_records = list(self._records(groups.get(group, ())))
                # A reviewed Character -> Tag link is a stable relationship
                # root supplied by the confirmed character assignment.  Keep
                # its semantic descendants scoped to that character instead
                # of requiring the representative Tag to be copied into Image
                # Knowledge first.
                if (
                    group == "other"
                    and character_tag is not None
                    and character_tag.id not in {item.id for item in group_records}
                ):
                    group_records.append(character_tag)
                support_records = (
                    tuple(group_records)
                    if relationship_root_ids is None
                    else tuple(
                        record
                        for record in group_records
                        if record.id in relationship_root_ids
                    )
                )
                if not support_records:
                    continue
                group_label = CHARACTER_GROUP_LABELS.get(
                    group,
                    str(group).replace("_", " ").strip().title() or "Other",
                )
                scope = (
                    f"Character {assignment.slot} \u2014 {owner_label}"
                    f" \u2014 {group_label}"
                )
                for support in support_records:
                    queue = deque(
                        [(support.id, (support.id,), (), (), (), frozenset({support.id}))]
                    )
                    while queue:
                        (
                            current_id, path_ids, relation_kinds, relationship_ids,
                            policy_decisions, seen,
                        ) = queue.popleft()
                        if len(relation_kinds) >= max_depth:
                            continue
                        current = self.knowledge.tags.get(current_id)
                        if current is None:
                            continue
                        for edge in self._outgoing(current):
                            if not edge.decision.permits_related_review:
                                continue
                            target_id = edge.target_id
                            if (
                                target_id in seen
                                or target_id not in self.knowledge.tags
                            ):
                                continue
                            target = self.knowledge.tags[target_id]
                            next_path = (*path_ids, target_id)
                            next_relations = (*relation_kinds, edge.relation_alias)
                            next_relationship_ids = (*relationship_ids, edge.relationship_id)
                            next_decisions = (*policy_decisions, edge.decision)
                            if target_id not in confirmed_ids and target_id != support.id:
                                suggestions.append(
                                    self._scoped_suggestion(
                                        target,
                                        supporting_tag_id=support.id,
                                        supporting_display=support.display,
                                        path_ids=next_path,
                                        path_displays=tuple(
                                            self.knowledge.tags[tag_id].display
                                            for tag_id in next_path
                                        ),
                                        relation_kinds=next_relations,
                                        scope=scope,
                                        owner_id=owner_id,
                                        owner_group=group,
                                        owner_kind="character",
                                        confirmed_ids=confirmed_ids,
                                        absent_ids=absent_ids,
                                        rejected_ids=rejected_ids,
                                        source="Reviewed System Knowledge",
                                        relationship_ids=next_relationship_ids,
                                        canonical_relationship_kinds=tuple(
                                            POLICY_KIND_BY_RELATION_ALIAS.get(kind, kind)
                                            for kind in next_relations
                                        ),
                                        policy_decisions=next_decisions,
                                    )
                                )
                            if edge.decision.permits_fact_expansion:
                                queue.append(
                                    (
                                        target_id, next_path, next_relations,
                                        next_relationship_ids, next_decisions,
                                        seen | {target_id},
                                    )
                                )
        return tuple(suggestions)

    def _object_tag_suggestions(
        self,
        annotation,
        *,
        image_wide_confirmed_ids: set[str],
        relationship_root_ids: set[str] | None,
        absent_ids: set[str],
        rejected_ids: set[str],
        max_depth: int,
    ) -> tuple[RelatedTagSuggestion, ...]:
        """Traverse reviewed relationships without losing exact object ownership."""

        suggestions = []
        for assignment in sorted(
            getattr(annotation, "objects", ()),
            key=lambda item: (getattr(item, "slot", 0), getattr(item, "id", "")),
        ):
            owner_id = str(getattr(assignment, "id", "") or "")
            identity_tag_id = str(
                getattr(assignment, "identity_tag_id", "") or ""
            )
            identity_tag = str(getattr(assignment, "identity_tag", "") or "")
            if not owner_id or not identity_tag_id or not identity_tag:
                continue
            identity_record = self.knowledge.tags.get(identity_tag_id)
            if identity_record is None:
                continue
            owner_label = identity_record.display
            values = TagCollection([owner_label])
            values.extend(getattr(assignment, "tags", ()))
            scoped_ids = self._identity_ids(values)
            confirmed_ids = image_wide_confirmed_ids | scoped_ids
            support_records = self._records(values)
            if relationship_root_ids is not None:
                support_records = tuple(
                    record
                    for record in support_records
                    if record.id in relationship_root_ids
                )
            scope = f"Object {assignment.slot} \u2014 {owner_label}"

            for support in support_records:
                queue = deque(
                    [(support.id, (support.id,), (), (), (), frozenset({support.id}))]
                )
                while queue:
                    (
                        current_id, path_ids, relation_kinds, relationship_ids,
                        policy_decisions, seen,
                    ) = queue.popleft()
                    if len(relation_kinds) >= max_depth:
                        continue
                    current = self.knowledge.tags.get(current_id)
                    if current is None:
                        continue
                    for edge in self._outgoing(current):
                        if not edge.decision.permits_related_review:
                            continue
                        target_id = edge.target_id
                        if target_id in seen or target_id not in self.knowledge.tags:
                            continue
                        target = self.knowledge.tags[target_id]
                        next_path = (*path_ids, target_id)
                        next_relations = (*relation_kinds, edge.relation_alias)
                        next_relationship_ids = (*relationship_ids, edge.relationship_id)
                        next_decisions = (*policy_decisions, edge.decision)
                        if target_id not in confirmed_ids and target_id != support.id:
                            suggestions.append(
                                self._scoped_suggestion(
                                    target,
                                    supporting_tag_id=support.id,
                                    supporting_display=support.display,
                                    path_ids=next_path,
                                    path_displays=tuple(
                                        self.knowledge.tags[tag_id].display
                                        for tag_id in next_path
                                    ),
                                    relation_kinds=next_relations,
                                    scope=scope,
                                    owner_id=owner_id,
                                    owner_group="tags",
                                    owner_kind="object",
                                    confirmed_ids=confirmed_ids,
                                    absent_ids=absent_ids,
                                    rejected_ids=rejected_ids,
                                    source="Reviewed System Knowledge",
                                    relationship_ids=next_relationship_ids,
                                    canonical_relationship_kinds=tuple(
                                        POLICY_KIND_BY_RELATION_ALIAS.get(kind, kind)
                                        for kind in next_relations
                                    ),
                                    policy_decisions=next_decisions,
                                )
                            )
                        if edge.decision.permits_fact_expansion:
                            queue.append(
                                (
                                    target_id, next_path, next_relations,
                                    next_relationship_ids, next_decisions,
                                    seen | {target_id},
                                )
                            )
        return tuple(suggestions)

    def _character_suggestions(
        self,
        annotation,
        *,
        image_wide_confirmed_ids: set[str],
        absent_ids: set[str],
        rejected_ids: set[str],
    ) -> tuple[RelatedTagSuggestion, ...]:
        suggestions = []
        for assignment in sorted(
            getattr(annotation, "characters", ()),
            key=lambda item: (getattr(item, "slot", 0), getattr(item, "id", "")),
        ):
            owner_id = str(getattr(assignment, "id", "") or "")
            if not owner_id:
                continue
            character_id = str(getattr(assignment, "character_id", "") or "")
            character = self.characters.characters.get(character_id)
            character_tag = self._reviewed_character_tag(character, "character")
            owner_label = self._owner_label(assignment, character)
            scope = f"Character {assignment.slot} \u2014 {owner_label}"
            scoped_ids = self._identity_ids(assignment.all_tags())
            conflict_ids = image_wide_confirmed_ids | scoped_ids

            if (
                character_tag is not None
                and character_tag.id not in scoped_ids
                and character_tag.id not in image_wide_confirmed_ids
            ):
                suggestions.append(
                    self._scoped_suggestion(
                        character_tag,
                        supporting_tag_id=f"character-assignment:{owner_id}",
                        supporting_display=f"Character {assignment.slot}: {owner_label}",
                        path_ids=(character_tag.id,),
                        path_displays=(
                            f"Character {assignment.slot}: {owner_label}",
                            character_tag.display,
                        ),
                        relation_kinds=("character_identity",),
                        scope=scope,
                        owner_id=owner_id,
                        owner_group="other",
                        owner_kind="character",
                        confirmed_ids=conflict_ids,
                        absent_ids=absent_ids,
                        rejected_ids=rejected_ids,
                    )
                )

            source_id = (
                self.characters.character_source_id("character", character.id)
                if character is not None else ""
            )
            source = self.characters.series.get(source_id) if source_id else None
            source_tag = self._reviewed_character_tag(source, "series")
            if (
                source_tag is not None
                and source_tag.id not in scoped_ids
                and source_tag.id not in image_wide_confirmed_ids
            ):
                supporting_id = (
                    character_tag.id
                    if character_tag is not None
                    else f"character-assignment:{owner_id}"
                )
                supporting_display = (
                    character_tag.display
                    if character_tag is not None
                    else f"Character {assignment.slot}: {owner_label}"
                )
                path_ids = (
                    (character_tag.id, source_tag.id)
                    if character_tag is not None
                    else (source_tag.id,)
                )
                path_displays = (
                    (character_tag.display, source_tag.display)
                    if character_tag is not None
                    else (
                        f"Character {assignment.slot}: {owner_label}",
                        source_tag.display,
                    )
                )
                suggestions.append(
                    self._scoped_suggestion(
                        source_tag,
                        supporting_tag_id=supporting_id,
                        supporting_display=supporting_display,
                        path_ids=path_ids,
                        path_displays=path_displays,
                        relation_kinds=("character_source",),
                        scope=scope,
                        owner_id=owner_id,
                        owner_group="other",
                        owner_kind="character",
                        confirmed_ids=conflict_ids,
                        absent_ids=absent_ids,
                        rejected_ids=rejected_ids,
                    )
                )

            species_trait_ids = list(
                self.characters.species_trait_target_ids("character", character_id)
                if character_id else ()
            )
            if character_tag is not None:
                species_trait_ids.extend(
                    self.characters.species_trait_target_ids("tag", character_tag.id)
                )
            for trait_id in dict.fromkeys(species_trait_ids):
                trait = self.knowledge.tags.get(trait_id)
                if (
                    trait is None
                    or trait.id in scoped_ids
                    or trait.id in image_wide_confirmed_ids
                ):
                    continue
                supporting_id = (
                    character_tag.id
                    if character_tag is not None
                    else f"character-assignment:{owner_id}"
                )
                supporting_display = (
                    character_tag.display
                    if character_tag is not None
                    else f"Character {assignment.slot}: {owner_label}"
                )
                suggestions.append(self._scoped_suggestion(
                    trait,
                    supporting_tag_id=supporting_id,
                    supporting_display=supporting_display,
                    path_ids=(
                        (character_tag.id,trait.id)
                        if character_tag is not None else (trait.id,)
                    ),
                    path_displays=(supporting_display, trait.display),
                    relation_kinds=("species_trait",),
                    scope=scope,
                    owner_id=owner_id,
                    owner_group="appearance",
                    owner_kind="character",
                    confirmed_ids=conflict_ids,
                    absent_ids=absent_ids,
                    rejected_ids=rejected_ids,
                ))

            for appearance_source_id in (
                self.characters.other_appearance_ids("character", character_id)
                if character_id else ()
            ):
                appearance_source = self.characters.series.get(appearance_source_id)
                appearance_tag = self._reviewed_character_tag(
                    appearance_source, "series",
                )
                if (
                    appearance_tag is None
                    or appearance_tag.id in scoped_ids
                    or appearance_tag.id in image_wide_confirmed_ids
                ):
                    continue
                supporting_id = (
                    character_tag.id
                    if character_tag is not None
                    else f"character-assignment:{owner_id}"
                )
                supporting_display = (
                    character_tag.display
                    if character_tag is not None
                    else f"Character {assignment.slot}: {owner_label}"
                )
                suggestions.append(self._scoped_suggestion(
                    appearance_tag,
                    supporting_tag_id=supporting_id,
                    supporting_display=supporting_display,
                    path_ids=(
                        (character_tag.id,appearance_tag.id)
                        if character_tag is not None else (appearance_tag.id,)
                    ),
                    path_displays=(supporting_display, appearance_tag.display),
                    relation_kinds=("other_appearance",),
                    scope=scope,
                    owner_id=owner_id,
                    owner_group="other",
                    owner_kind="character",
                    confirmed_ids=conflict_ids,
                    absent_ids=absent_ids,
                    rejected_ids=rejected_ids,
                ))

            for outfit_id in tuple(getattr(assignment, "outfit_ids", ())):
                outfit = self.characters.outfits.get(str(outfit_id))
                outfit_tag = self._reviewed_character_tag(outfit, "outfit")
                if outfit is None or outfit_tag is None:
                    continue
                incompatible = bool(
                    character_id
                    and self.characters.associated_character_ids(outfit.id)
                    and character_id not in self.characters.associated_character_ids(outfit.id)
                )
                supporting_id = (
                    character_tag.id
                    if character_tag is not None
                    else f"outfit-assignment:{owner_id}:{outfit.id}"
                )
                supporting_display = (
                    character_tag.display
                    if character_tag is not None
                    else f"Assigned outfit: {outfit.name}"
                )
                path_ids = (
                    (character_tag.id, outfit_tag.id)
                    if character_tag is not None
                    else (outfit_tag.id,)
                )
                path_displays = (
                    (character_tag.display, outfit_tag.display)
                    if character_tag is not None
                    else (f"Assigned outfit: {outfit.name}", outfit_tag.display)
                )
                if (
                    outfit_tag.id not in scoped_ids
                    and outfit_tag.id not in image_wide_confirmed_ids
                ):
                    suggestion = self._scoped_suggestion(
                        outfit_tag,
                        supporting_tag_id=supporting_id,
                        supporting_display=supporting_display,
                        path_ids=path_ids,
                        path_displays=path_displays,
                        relation_kinds=("character_outfit",),
                        scope=scope,
                        owner_id=owner_id,
                        owner_group="clothing",
                        owner_kind="character",
                        confirmed_ids=conflict_ids,
                        absent_ids=absent_ids,
                        rejected_ids=rejected_ids,
                    )
                    if incompatible:
                        suggestion = replace(
                            suggestion,
                            blocked_reason=(
                                "Assigned outfit is not reviewed for this character"
                            ),
                        )
                    suggestions.append(suggestion)
                outfit_source_id = self.characters.character_source_id(
                    "outfit", outfit.id,
                )
                outfit_source = (
                    self.characters.series.get(outfit_source_id)
                    if outfit_source_id else None
                )
                outfit_source_tag = self._reviewed_character_tag(outfit_source, "series")
                if (
                    outfit_source_tag is not None
                    and outfit_source_tag.id not in scoped_ids
                    and outfit_source_tag.id not in image_wide_confirmed_ids
                ):
                    source_suggestion = self._scoped_suggestion(
                        outfit_source_tag,
                        supporting_tag_id=outfit_tag.id,
                        supporting_display=outfit_tag.display,
                        path_ids=(outfit_tag.id, outfit_source_tag.id),
                        path_displays=(
                            outfit_tag.display,
                            outfit_source_tag.display,
                        ),
                        relation_kinds=("character_source",),
                        scope=scope,
                        owner_id=owner_id,
                        owner_group="other",
                        owner_kind="character",
                        confirmed_ids=conflict_ids,
                        absent_ids=absent_ids,
                        rejected_ids=rejected_ids,
                    )
                    if incompatible:
                        source_suggestion = replace(
                            source_suggestion,
                            blocked_reason=(
                                "Assigned outfit is not reviewed for this character"
                            ),
                        )
                    suggestions.append(source_suggestion)
                for appearance_source_id in self.characters.other_appearance_ids(
                    "outfit",outfit.id,
                ):
                    if appearance_source_id==outfit_source_id:
                        continue
                    appearance_source=self.characters.series.get(appearance_source_id)
                    appearance_tag=self._reviewed_character_tag(
                        appearance_source,"series",
                    )
                    if (
                        appearance_tag is None
                        or appearance_tag.id in scoped_ids
                        or appearance_tag.id in image_wide_confirmed_ids
                    ):
                        continue
                    appearance_suggestion=self._scoped_suggestion(
                        appearance_tag,
                        supporting_tag_id=outfit_tag.id,
                        supporting_display=outfit_tag.display,
                        path_ids=(outfit_tag.id,appearance_tag.id),
                        path_displays=(outfit_tag.display,appearance_tag.display),
                        relation_kinds=("other_appearance",),
                        scope=scope,
                        owner_id=owner_id,
                        owner_group="other",
                        owner_kind="character",
                        confirmed_ids=conflict_ids,
                        absent_ids=absent_ids,
                        rejected_ids=rejected_ids,
                    )
                    if incompatible:
                        appearance_suggestion=replace(
                            appearance_suggestion,
                            blocked_reason=(
                                "Assigned outfit is not reviewed for this character"
                            ),
                        )
                    suggestions.append(appearance_suggestion)
        return tuple(suggestions)

    def _reviewed_character_tag(self, record, kind: str) -> TagRecord | None:
        if record is None:
            return None
        link = self.characters.effective_knowledge_tag(kind, record.id)
        if not isinstance(link, dict) or link.get("status") != "reviewed":
            return None
        return self.knowledge.tags.get(str(link.get("tag_id") or ""))

    def _owner_label(self, assignment, character) -> str:
        if character is not None:
            return self.characters.character_label(character.id) or character.name
        role = str(getattr(assignment, "role", "") or "").replace("_", " ").strip()
        return role.title() if role else "Unidentified character"

    def _scoped_suggestion(
        self,
        target: TagRecord,
        *,
        supporting_tag_id: str,
        supporting_display: str,
        path_ids: tuple[str, ...],
        path_displays: tuple[str, ...],
        relation_kinds: tuple[str, ...],
        scope: str,
        owner_id: str,
        owner_group: str,
        owner_kind: str,
        confirmed_ids: set[str],
        absent_ids: set[str],
        rejected_ids: set[str],
        source: str = "Reviewed Character Knowledge",
        relationship_ids: tuple[str, ...] = (),
        canonical_relationship_kinds: tuple[str, ...] = (),
        policy_decisions: tuple[ConsumerPolicyDecision, ...] = (),
    ) -> RelatedTagSuggestion:
        constraint_scopes = ["image"]
        if owner_kind == "character":
            constraint_scopes.append("same_character")
        elif owner_kind == "object":
            constraint_scopes.append("same_object")
        blocked, conflicting = self._blocked_state(
            target.id,
            confirmed_ids=confirmed_ids,
            absent_ids=absent_ids,
            rejected_ids=rejected_ids,
            constraint_scopes=constraint_scopes,
        )
        if not policy_decisions:
            canonical_relationship_kinds = tuple(
                POLICY_KIND_BY_RELATION_ALIAS.get(kind, kind)
                for kind in relation_kinds
            )
            policy_decisions = tuple(
                self._policy_decision(
                    target_id=target.id,
                    relationship_kind=kind,
                    scope=scope,
                    owner_kind=owner_kind,
                )
                for kind in canonical_relationship_kinds
            )
        return RelatedTagSuggestion(
            tag_id=target.id,
            display=target.display,
            supporting_tag_id=supporting_tag_id,
            supporting_display=supporting_display,
            path_ids=path_ids,
            path_displays=path_displays,
            relation_kinds=relation_kinds,
            scope=scope,
            source=source,
            blocked_reason=blocked,
            conflict_tag_ids=conflicting,
            owner_id=owner_id,
            owner_group=owner_group,
            owner_kind=owner_kind,
            relationship_ids=relationship_ids,
            canonical_relationship_kinds=canonical_relationship_kinds,
            policy_decisions=policy_decisions,
        )

    def _records(self, values: Iterable[str]) -> tuple[TagRecord, ...]:
        by_id = {}
        for value in values:
            record = self.knowledge.find(str(value))
            if record is not None:
                by_id.setdefault(record.id, record)
        return tuple(
            sorted(by_id.values(), key=lambda item: (item.display.casefold(), item.id))
        )

    def _identity_ids(self, values: Iterable[str]) -> set[str]:
        result = set()
        for value in values:
            raw = str(value)
            record = self.knowledge.find(raw)
            if record is not None:
                result.add(record.id)
            elif raw in self.knowledge.tags:
                result.add(raw)
        return result

    def _policy_decision(
        self,
        *,
        target_id: str,
        relationship_kind: str,
        relationship_id: str = "",
        scope: str = "any",
        owner_kind: str = "image",
    ) -> ConsumerPolicyDecision:
        return self.consumer_policies.resolve(
            IMAGE_IDENTIFICATION,
            relationship_id=relationship_id,
            tag_id=target_id,
            relationship_kind=relationship_kind,
            context={"scope": scope, "owner_kind": owner_kind},
        )

    def _outgoing(self, record: TagRecord) -> tuple[_TraversalEdge, ...]:
        """Return reviewed links that establish independent image facts.

        Modifier-value composition is deliberately absent.  A composition such
        as ``white flower = flower + white`` explains and reconstructs the
        compound Tag, but it does not establish context-free ``white`` as a
        separately meaningful fact about the image or the same scoped owner.
        Base-preserving compound suggestions require their own reviewed Tag
        relationship rather than leaking one modifier slot into Related Tags.
        """
        links: list[tuple[str, str, str, str, str]] = []
        resolved = (
            self.semantic_relationships.effective_for_tag(record.id)
            if self.semantic_relationships is not None else None
        )
        base_id = resolved.parent_id if resolved is not None else ""
        base_kind = "explicit" if base_id else "none"
        if (
            not base_id
            and self.categories is not None
            and hasattr(self.categories, "effective_base_tag")
        ):
            if self._derived_category_anchors is None:
                base_id, _source, _policy = self.categories.effective_base_tag(
                    record.category,
                )
            else:
                base_id, _source, _policy = self.categories.effective_base_tag(
                    record.category,
                    derived_anchors=self._derived_category_anchors,
                )
            base_id = str(base_id or "")
            if base_id == record.id:
                base_id = ""
            base_kind = "inherited" if base_id else "none"
        active = tuple(self._active_relationships_by_source.get(record.id, ()))
        if base_id:
            if base_kind == "explicit":
                relationship = next(
                    (
                        item
                        for item in active
                        if item.kind == SPECIFICITY
                        and any(
                            endpoint.kind == "tag" and endpoint.id == base_id
                            for endpoint in item.target_endpoints
                        )
                    ),
                    None,
                )
                links.append(
                    (
                        base_id,
                        "base",
                        SPECIFICITY,
                        relationship.id if relationship is not None else "",
                        relationship.scope if relationship is not None else "any",
                    )
                )
            else:
                relationship = self._category_base_relationships.get(
                    (str(record.category or ""), base_id)
                )
                links.append(
                    (
                        base_id,
                        "category_base",
                        CATEGORY_BASE,
                        relationship.id if relationship is not None else "",
                        relationship.scope if relationship is not None else "organization",
                    )
                )
        for tag_id in (resolved.implies if resolved is not None else ()):
            relationship = next(
                (
                    item
                    for item in active
                    if item.kind == ENTAILMENT
                    and any(
                        endpoint.kind == "tag" and endpoint.id == tag_id
                        for endpoint in item.target_endpoints
                    )
                ),
                None,
            )
            links.append(
                (
                    tag_id,
                    "implies",
                    ENTAILMENT,
                    relationship.id if relationship is not None else "",
                    relationship.scope if relationship is not None else "any",
                )
            )
        for relationship in sorted(active, key=lambda item: item.id):
            if relationship.kind != ADVISORY_ASSOCIATION:
                continue
            for endpoint in relationship.target_endpoints:
                if endpoint.kind == "tag":
                    links.append(
                        (
                            endpoint.id,
                            "usually_related",
                            ADVISORY_ASSOCIATION,
                            relationship.id,
                            relationship.scope,
                        )
                    )
        deduplicated: dict[str, _TraversalEdge] = {}
        for tag_id, alias, kind, relationship_id, scope in links:
            target_id = str(tag_id)
            deduplicated.setdefault(
                target_id,
                _TraversalEdge(
                    target_id=target_id,
                    relationship_id=relationship_id,
                    relationship_kind=kind,
                    relation_alias=alias,
                    scope=scope,
                    decision=self._policy_decision(
                        target_id=target_id,
                        relationship_kind=kind,
                        relationship_id=relationship_id,
                        scope=scope,
                    ),
                ),
            )
        return tuple(deduplicated.values())

    def _suggestion(
        self,
        target: TagRecord,
        support: TagRecord,
        path_ids: tuple[str, ...],
        relation_kinds: tuple[str, ...],
        *,
        confirmed_ids: set[str],
        absent_ids: set[str],
        rejected_ids: set[str],
        relationship_ids: tuple[str, ...],
        canonical_relationship_kinds: tuple[str, ...],
        policy_decisions: tuple[ConsumerPolicyDecision, ...],
    ) -> RelatedTagSuggestion:
        blocked, conflicting = self._blocked_state(
            target.id,
            confirmed_ids=confirmed_ids,
            absent_ids=absent_ids,
            rejected_ids=rejected_ids,
        )
        return RelatedTagSuggestion(
            tag_id=target.id,
            display=target.display,
            supporting_tag_id=support.id,
            supporting_display=support.display,
            path_ids=path_ids,
            path_displays=tuple(
                self.knowledge.tags[tag_id].display for tag_id in path_ids
            ),
            relation_kinds=relation_kinds,
            blocked_reason=blocked,
            conflict_tag_ids=conflicting,
            relationship_ids=relationship_ids,
            canonical_relationship_kinds=canonical_relationship_kinds,
            policy_decisions=policy_decisions,
        )

    def _blocked_state(
        self,
        target_id: str,
        *,
        confirmed_ids: set[str],
        absent_ids: set[str],
        rejected_ids: set[str],
        constraint_scopes: Iterable[str] = ("image",),
    ) -> tuple[str, tuple[str, ...]]:
        conflicting_ids = set()
        for scope in dict.fromkeys(str(value) for value in constraint_scopes):
            conflicting_ids.update(
                self.constraints.conflicting_tag_ids(
                    target_id,
                    confirmed_ids,
                    scope=scope,
                )
            )
        conflicting = tuple(
            sorted(
                conflicting_ids,
                key=lambda record_id: self.knowledge.tags[
                    record_id
                ].display.casefold(),
            )
        )
        if target_id in absent_ids:
            return "Confirmed absent on this image", conflicting
        if target_id in rejected_ids:
            return "Previously rejected for this image", conflicting
        if conflicting:
            names = ", ".join(
                self.knowledge.tags[tag_id].display for tag_id in conflicting
            )
            return f"Conflicts with confirmed tag: {names}", conflicting
        return "", conflicting

    @staticmethod
    def _rank(suggestion: RelatedTagSuggestion):
        return (
            len(suggestion.relation_kinds),
            suggestion.scope.casefold(),
            suggestion.supporting_display.casefold(),
            suggestion.path_displays,
        )

    @staticmethod
    def _sort_key(suggestion: RelatedTagSuggestion):
        return (
            not suggestion.eligible,
            suggestion.scope.casefold(),
            suggestion.supporting_display.casefold(),
            len(suggestion.relation_kinds),
            suggestion.display.casefold(),
            suggestion.tag_id,
        )
