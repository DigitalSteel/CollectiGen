from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields, replace
from typing import Any

from .annotation import (
    CHARACTER_TAG_GROUPS,
    GLOBAL_TAG_GROUPS,
    SCENE_TAG_GROUPS,
    ImageAnnotation,
)
from .automation_templates import has_automation_placeholder
from .character_database import CharacterDatabase
from .content_sensitivity import RATING_ORDER, effective_rating
from .image_contexts import context_relation_description
from .tag_database import TagDatabase
from .tag_collection import normalize_tag
from .tag_ordering import TagOrderCandidate, TagOrderingService


HIERARCHY_KEEP_MOST_SPECIFIC = "keep_most_specific"
HIERARCHY_INCLUDE_BROADER = "include_broader"
HIERARCHY_AS_CONFIRMED = "as_confirmed"
HIERARCHY_POLICIES = (
    HIERARCHY_KEEP_MOST_SPECIFIC,
    HIERARCHY_INCLUDE_BROADER,
    HIERARCHY_AS_CONFIRMED,
)


def _ordered_groups(
    values: dict[str, list[str]],
    preferred: tuple[str, ...],
    profile_version: int,
):
    """Keep legacy ordering while v3+ is stable across save/reload."""
    if profile_version < 3:
        yield from values.items()
        return
    for group in preferred:
        if group in values:
            yield group, values[group]
    for group in sorted(set(values) - set(preferred), key=str.casefold):
        yield group, values[group]


@dataclass(frozen=True)
class ProjectionDecision:
    """One confirmed fact's exact treatment by a training projection."""

    fact_type: str
    source_value: str
    source: str
    action: str
    output: str = ""
    reason: str = ""
    relationship_path: tuple[str, ...] = ()
    owner_scope: str = ""
    profile_policy: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_type": self.fact_type,
            "source_value": self.source_value,
            "source": self.source,
            "action": self.action,
            "output": self.output,
            "reason": self.reason,
            "relationship_path": list(self.relationship_path),
            "owner_scope": self.owner_scope,
            "profile_policy": self.profile_policy,
        }


@dataclass(frozen=True)
class CaptionProjection:
    sdxl_tags: tuple[str, ...] = ()
    plain_language: tuple[str, ...] = ()
    decisions: tuple[ProjectionDecision, ...] = ()
    relationship_fingerprint: str = ""

    def render(self, separator: str = ", ") -> str:
        return separator.join([*self.sdxl_tags, *self.plain_language])

    def decision_counts(self) -> dict[str, int]:
        counts = {
            action: sum(decision.action == action for decision in self.decisions)
            for action in ("emitted", "combined", "omitted")
        }
        held = sum(decision.action == "held" for decision in self.decisions)
        if held:
            counts["held"] = held
        return counts

    def explanation_lines(self) -> tuple[str, ...]:
        counts = self.decision_counts()
        lines = (
            f"Emitted {counts['emitted']} • Combined {counts['combined']} • "
            f"Omitted {counts['omitted']}",
        )
        details = []
        for decision in self.decisions:
            result = f" → {decision.output}" if decision.output else ""
            details.append(
                f"{decision.action.upper()} • {decision.source} • "
                f"{decision.source_value}{result} — {decision.reason}"
            )
        return (*lines, *details)


@dataclass
class ExportProfile:
    name: str = "SDXL tags and descriptions"
    include_character_names: bool = True
    include_character_tags: bool = True
    include_outfit_names: bool = False
    include_outfit_tags: bool = True
    include_outfit_prose: bool = False
    include_object_identities: bool = True
    include_object_tags: bool = True
    include_global: bool = True
    include_scene: bool = True
    include_contexts: bool = True
    include_sdxl: bool = True
    include_uncategorized: bool = True
    include_custom_groups: bool = True
    include_plain_language: bool = True
    character_groups: set[str] | None = None
    global_groups: set[str] | None = None
    scene_groups: set[str] | None = None
    custom_group_names: set[str] | None = None
    separator: str = ", "
    maximum_content_rating: str = "explicit"
    apply_system_ordering: bool = True
    canonicalize_aliases: bool = True
    apply_automation_rules: bool = True
    hierarchy_policy: str = HIERARCHY_KEEP_MOST_SPECIFIC
    profile_id: str = "collectigen.sdxl.tags-and-descriptions"
    version: int = 6
    format_version: int | None = None
    description: str = (
        "Export ordered SDXL Tags followed by confirmed Plain Language Descriptions."
    )
    target_model_family: str = "SDXL"
    target_model_version: str = ""
    training_objective: str = "general fine-tuning"
    caption_strategy: str = "sdxl_tags_then_plain_language"
    vocabulary_assumptions: tuple[str, ...] = ("CollectiGen reviewed SDXL Tag vocabulary",)
    adapter_assumptions: tuple[str, ...] = ()
    token_budget: int | None = None
    trigger_policy: str = "No automatic trigger insertion"

    @property
    def effective_format_version(self) -> int:
        """Projection behavior version, separate from a user profile revision."""
        return self.format_version or self.version

    def with_maximum_content_rating(self, rating: str) -> ExportProfile:
        """Return an operation-specific copy without changing profile identity."""
        return replace(self, maximum_content_rating=rating)

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic, forward-compatible profile snapshot."""
        payload: dict[str, Any] = {}
        for definition in fields(self):
            value = getattr(self, definition.name)
            if definition.name == "format_version" and value is None:
                # Existing built-in and historical snapshots predate the
                # separate format/revision boundary. Omitting the default keeps
                # their exact fingerprints reproducible.
                continue
            if (
                definition.name == "hierarchy_policy"
                and self.effective_format_version < 4
                and value == HIERARCHY_AS_CONFIRMED
            ):
                # Historical snapshots did not contain a hierarchy policy.
                # Omitting the compatibility default preserves their bytes and
                # fingerprints after a round trip.
                continue
            if (
                self.effective_format_version < 2
                and definition.name
                in {"include_object_identities", "include_object_tags"}
                and value is False
            ):
                # These controls were introduced in profile version 2. Omitting
                # disabled values preserves exact legacy snapshots/fingerprints.
                continue
            if (
                self.effective_format_version < 5
                and definition.name == "include_contexts"
            ):
                # Secondary contexts did not participate in historical
                # projection formats or fingerprints.
                continue
            if isinstance(value, set):
                payload[definition.name] = sorted(value)
            elif isinstance(value, tuple):
                payload[definition.name] = list(value)
            else:
                payload[definition.name] = value
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ExportProfile:
        """Restore current or legacy snapshots while ignoring future fields."""
        known = {definition.name for definition in fields(cls)}
        values = {key: value for key, value in payload.items() if key in known}
        profile_version = payload.get("version", 1)
        try:
            profile_version = max(1, int(profile_version))
        except (TypeError, ValueError):
            profile_version = 1
        values["version"] = profile_version
        format_version = payload.get("format_version")
        if format_version is not None:
            try:
                values["format_version"] = max(1, int(format_version))
            except (TypeError, ValueError):
                values["format_version"] = profile_version
        effective_format_version = values.get("format_version") or profile_version
        if effective_format_version < 2:
            values.setdefault("include_object_identities", False)
            values.setdefault("include_object_tags", False)
        if effective_format_version < 4 and "hierarchy_policy" not in payload:
            values["hierarchy_policy"] = HIERARCHY_AS_CONFIRMED
        for key in ("character_groups", "global_groups", "scene_groups", "custom_group_names"):
            if values.get(key) is not None:
                raw_values = values[key]
                values[key] = (
                    set(raw_values)
                    if isinstance(raw_values, (list, tuple, set))
                    else raw_values
                )
        for key in ("vocabulary_assumptions", "adapter_assumptions"):
            if key in values:
                raw_values = values[key]
                values[key] = (
                    ()
                    if raw_values is None
                    else (
                        tuple(str(value) for value in raw_values)
                        if isinstance(raw_values, (list, tuple))
                        else raw_values
                    )
                )
        return cls(**values)

    def fingerprint(self) -> str:
        """Identify the exact projection contract, including its display metadata."""
        encoded = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def provenance(self) -> dict[str, Any]:
        """Small stable identity block suitable for TXT state and export manifests."""
        return {
            "profile_id": self.profile_id,
            "profile_version": self.version,
            "profile_name": self.name,
            "profile_fingerprint": self.fingerprint(),
            "target_model_family": self.target_model_family,
            "target_model_version": self.target_model_version,
            "training_objective": self.training_objective,
            "caption_strategy": self.caption_strategy,
            "token_budget": self.token_budget,
            "trigger_policy": self.trigger_policy,
        }

    def render(
        self,
        annotation: ImageAnnotation,
        character_db: CharacterDatabase | None = None,
        knowledge: TagDatabase | None = None,
        relationship_evaluator=None,
    ) -> str:
        return self.project(
            annotation, character_db, knowledge, relationship_evaluator
        ).render(self.separator)

    def project(
        self,
        annotation: ImageAnnotation,
        character_db: CharacterDatabase | None = None,
        knowledge: TagDatabase | None = None,
        relationship_evaluator=None,
    ) -> CaptionProjection:
        """Return typed projection segments before the lossy TXT join."""
        candidates: list[TagOrderCandidate] = []
        decisions: list[ProjectionDecision] = []
        prose: list[str] = []
        sequence = 0
        rating_omission = (
            knowledge is not None and not self.includes(annotation, knowledge)
        )
        rating_reason = (
            "The image exceeds this profile's maximum content rating."
            if rating_omission
            else ""
        )

        def add(
            value: str,
            group: str = "other",
            slot: int | None = None,
            identity_kind: str = "",
            *,
            source: str,
            included: bool = True,
            omitted_reason: str = "",
            owner_kind: str = "",
            owner_id: str = "",
        ) -> None:
            nonlocal sequence
            value = value.strip()
            if not value:
                return
            if has_automation_placeholder(value):
                decisions.append(ProjectionDecision(
                    "sdxl_tag",
                    value,
                    source,
                    "omitted",
                    reason="Unresolved Automation templates remain in Image Knowledge for explicit review and cannot enter a training projection.",
                ))
                return
            if rating_omission or not included:
                decisions.append(
                    ProjectionDecision(
                        "sdxl_tag",
                        value,
                        source,
                        "omitted",
                        reason=rating_reason or omitted_reason,
                    )
                )
                return
            candidates.append(
                TagOrderCandidate(
                    value,group,slot,identity_kind,sequence,source,
                    owner_kind or ("character" if slot is not None else "image"),
                    owner_id or (str(slot) if slot is not None else "image"),
                )
            )
            sequence += 1

        def extend(
            values,
            group: str = "other",
            slot: int | None = None,
            *,
            source: str,
            included: bool = True,
            omitted_reason: str = "",
            owner_kind: str = "",
            owner_id: str = "",
        ) -> None:
            for value in values:
                add(
                    value,
                    group,
                    slot,
                    source=source,
                    included=included,
                    omitted_reason=omitted_reason,
                    owner_kind=owner_kind,
                    owner_id=owner_id,
                )

        global_hints = {
            "rating": "rating",
            "style": "image_style",
            "subject_count": "subject_count",
        }
        for group, values in _ordered_groups(
            annotation.global_tags, GLOBAL_TAG_GROUPS, self.effective_format_version
        ):
            group_included = self.include_global and (
                self.global_groups is None or group in self.global_groups
            )
            extend(
                values,
                global_hints.get(group, "other"),
                source=f"Global / {group.replace('_', ' ').title()}",
                included=group_included,
                omitted_reason="This global group is disabled in the selected profile.",
            )
        extend(
            annotation.visual_style.style_tags,
            "image_style",
            source="Visual style",
            included=self.include_global,
            omitted_reason="Visual-style tags are disabled with global facts in this profile.",
        )

        for character in sorted(annotation.characters, key=lambda item: item.slot):
            if character.character_id and character_db:
                record = character_db.characters.get(character.character_id)
                if record:
                    add(
                        record.name,
                        "identity",
                        character.slot,
                        "character",
                        source=f"Character {character.slot} / Identity",
                        included=self.include_character_names,
                        omitted_reason="Character names are disabled in the selected profile.",
                    )
                    source_id = character_db.character_source_id("character", record.id)
                    series = character_db.series.get(source_id) if source_id else None
                    if series:
                        add(
                            series.name,
                            "identity",
                            character.slot,
                            "series",
                            source=f"Character {character.slot} / Source",
                            included=self.include_character_names,
                            omitted_reason="Character-source names are disabled in the selected profile.",
                        )
            character_hints = {
                "appearance": "subject_traits",
                "expression": "subject_traits",
                "other": "subject_traits",
                "clothing": "outfit",
                "pose": "action",
                "actions": "action",
            }
            for group, values in _ordered_groups(
                character.tags, CHARACTER_TAG_GROUPS, self.effective_format_version
            ):
                group_included = self.include_character_tags and (
                    self.character_groups is None or group in self.character_groups
                )
                extend(
                    values,
                    character_hints.get(group, "other"),
                    character.slot,
                    source=(
                        f"Character {character.slot} / "
                        f"{group.replace('_', ' ').title()}"
                    ),
                    included=group_included,
                    omitted_reason="This character tag group is disabled in the selected profile.",
                )
            if character_db:
                for outfit_id in character.outfit_ids:
                    outfit = character_db.outfits.get(outfit_id)
                    if not outfit:
                        continue
                    add(
                        outfit.name,
                        "outfit",
                        character.slot,
                        "outfit",
                        source=f"Character {character.slot} / Outfit name",
                        included=self.include_outfit_names,
                        omitted_reason="Outfit names are disabled in the selected profile.",
                    )
                    if self.effective_format_version >= 6:
                        representative_tag_id = character_db.entity_tag_id(
                            "outfit", outfit.id
                        )
                        representative = (
                            knowledge.tags.get(representative_tag_id)
                            if knowledge is not None and representative_tag_id
                            else None
                        )
                        representative_source = (
                            f"Character {character.slot} / Outfit SDXL Tag"
                        )
                        if representative is not None:
                            add(
                                representative.display,
                                "outfit",
                                character.slot,
                                "outfit",
                                source=representative_source,
                                included=self.include_outfit_tags,
                                omitted_reason=(
                                    "Outfit SDXL Tags are disabled in the selected "
                                    "profile."
                                ),
                            )
                        elif self.include_outfit_tags:
                            decisions.append(
                                ProjectionDecision(
                                    "sdxl_tag",
                                    outfit.name,
                                    representative_source,
                                    "held",
                                    reason=(
                                        "This assigned outfit has no single reviewed "
                                        "representative SDXL Tag, so the projection "
                                        "did not invent one from its display name."
                                    ),
                                    owner_scope=f"Character {character.slot}",
                                    profile_policy="reviewed_outfit_representative",
                                )
                            )
                    extend(
                        outfit.sdxl_tags,
                        "outfit",
                        character.slot,
                        source=f"Character {character.slot} / Outfit component Tags",
                        included=self.include_outfit_tags,
                        omitted_reason="Outfit tags are disabled in the selected profile.",
                    )
                    for value in outfit.plain_language:
                        self._add_prose_decision(
                            value,
                            source=f"Character {character.slot} / Outfit description",
                            included=self.include_outfit_prose and self.include_plain_language,
                            omitted_reason=(
                                "Outfit descriptions are disabled in the selected profile."
                                if not self.include_outfit_prose
                                else "Plain Language Descriptions are disabled in the selected profile."
                            ),
                            rating_reason=rating_reason,
                            prose=prose,
                            decisions=decisions,
                        )

        for item in sorted(annotation.objects, key=lambda value: value.slot):
            if item.identity_tag:
                add(
                    item.identity_tag,
                    source=f"Object {item.slot} / Identity",
                    included=self.include_object_identities,
                    omitted_reason=(
                        "Object identities are disabled in the selected profile."
                    ),
                    owner_kind="object",owner_id=str(item.slot),
                )
            extend(
                item.tags,
                source=f"Object {item.slot} / Traits",
                included=self.include_object_tags,
                omitted_reason=(
                    "Object traits are disabled in the selected profile."
                ),
                owner_kind="object",owner_id=str(item.slot),
            )

        for group, values in _ordered_groups(
            annotation.scene, SCENE_TAG_GROUPS, self.effective_format_version
        ):
            group_included = self.include_scene and (
                self.scene_groups is None or group in self.scene_groups
            )
            extend(
                values,
                "scene",
                source=f"Scene / {group.replace('_', ' ').title()}",
                included=group_included,
                omitted_reason="This scene group is disabled in the selected profile.",
            )

        if self.effective_format_version >= 5:
            for context in annotation.contexts:
                source = f"Context / {context.display_label}"
                extend(
                    context.tags,
                    "scene",
                    source=source,
                    included=self.include_contexts,
                    omitted_reason=(
                        "Secondary image contexts are disabled in the selected profile."
                    ),
                    owner_kind="context",owner_id=str(context.id),
                )
                relation = context_relation_description(context)
                if relation:
                    self._add_prose_decision(
                        relation,
                        source=source,
                        included=self.include_contexts and self.include_plain_language,
                        omitted_reason=(
                            "Secondary image contexts are disabled in the selected profile."
                            if not self.include_contexts else
                            "Plain Language Descriptions are disabled in the selected profile."
                        ),
                        rating_reason=rating_reason,
                        prose=prose,
                        decisions=decisions,
                    )

        custom_groups = (
            sorted(annotation.custom_groups.items(), key=lambda item: item[0].casefold())
            if self.effective_format_version >= 3
            else annotation.custom_groups.items()
        )
        for group, values in custom_groups:
            group_included = self.include_custom_groups and (
                self.custom_group_names is None or group in self.custom_group_names
            )
            extend(
                values,
                source=f"Custom / {group}",
                included=group_included,
                omitted_reason="This custom group is disabled in the selected profile.",
            )

        extend(
            annotation.sdxl_tags,
            source="General SDXL Tags",
            included=self.include_sdxl,
            omitted_reason="General SDXL Tags are disabled in the selected profile.",
        )
        extend(
            annotation.uncategorized,
            source="Uncategorized SDXL Tags",
            included=self.include_uncategorized,
            omitted_reason="Uncategorized tags are disabled in the selected profile.",
        )

        template_held_facts: dict[
            tuple[str,str],tuple[str,tuple[str,...],str]
        ] = {}
        template_projection_fingerprint=""
        if self.apply_automation_rules and knowledge is not None:
            from .tag_template_projection import (
                TagTemplateProjectionFact,active_tag_template_projection_service,
            )
            try:
                template_service=active_tag_template_projection_service(
                    knowledge,require_projection=True,
                )
            except (OSError,RuntimeError,ValueError):
                # Malformed optional template authority must not make an
                # otherwise valid training export unusable. Automation exposes
                # the held storage record for repair; projection stays off.
                template_service=None
            if template_service is not None:
                protected_keys={
                    normalize_tag(value) for value in (
                        *annotation.confirmed_absent,*annotation.export_excluded,
                    ) if str(value).strip()
                }
                facts=[]
                for candidate in candidates:
                    record=knowledge.find(candidate.value)
                    if record is None:continue
                    facts.append(TagTemplateProjectionFact(
                        str(record.id),str(record.display),candidate.owner_kind,
                        candidate.owner_id,candidate.source,
                        normalize_tag(candidate.value) in protected_keys,
                    ))
                template_report=template_service.preview(facts)
                template_projection_fingerprint=hashlib.sha256(json.dumps([
                    item.projection_rule.get("fingerprint","")
                    for item in template_service.templates
                ],sort_keys=True,separators=(",",":")).encode("utf-8")).hexdigest()
                next_sequence=max((item.sequence for item in candidates),default=-1)+1
                used=set(); remove_indexes=set(); additions=[]
                for scope in sorted(
                    template_report.scopes,key=lambda item:(
                        item.held,-item.coverage,item.template_id,item.owner_kind,item.owner_id,
                    ),
                ):
                    owner_scope=f"{scope.owner_kind.title()} {scope.owner_id}".strip()
                    scope_hold_reasons=tuple(scope.hold_reasons)
                    if normalize_tag(scope.target) in protected_keys:
                        scope_hold_reasons=(*scope_hold_reasons,
                            "The maximal compatible target has a protected confirmed-absent or export-excluded decision."
                        )
                    paths=tuple(dict.fromkeys(
                        relationship_id for path in scope.relationship_paths
                        for relationship_id in path
                    ))
                    matching=[]
                    for index,candidate in enumerate(candidates):
                        if (
                            candidate.owner_kind!=scope.owner_kind
                            or candidate.owner_id!=scope.owner_id
                        ):continue
                        record=knowledge.find(candidate.value)
                        if record is not None and str(record.id) in scope.source_tag_ids:
                            matching.append((index,candidate,str(record.id)))
                    if scope_hold_reasons:
                        reason=" ".join(scope_hold_reasons)
                        for _index,candidate,_tag_id in matching:
                            template_held_facts[(candidate.value,candidate.source)]=(
                                reason,paths,owner_scope,
                            )
                        continue
                    if any((scope.owner_kind,scope.owner_id,tag_id) in used for tag_id in scope.omitted_tag_ids):
                        continue
                    target_occurrence=next((
                        item for item in matching if item[2]==scope.target_tag_id
                    ),None)
                    base_candidate=(
                        target_occurrence[1] if target_occurrence is not None else
                        matching[0][1] if matching else None
                    )
                    if base_candidate is None:continue
                    for index,candidate,tag_id in matching:
                        if tag_id not in scope.omitted_tag_ids:continue
                        remove_indexes.add(index); used.add((scope.owner_kind,scope.owner_id,tag_id))
                        decisions.append(ProjectionDecision(
                            "sdxl_tag",candidate.value,candidate.source,"combined",
                            scope.target,
                            f"Used reviewed template {scope.template_id} v{scope.template_version} to emit the maximal compatible typed modifier set.",
                            paths,owner_scope,
                            f"Template {scope.template_id} v{scope.template_version}",
                        ))
                    if target_occurrence is None:
                        additions.append(replace(
                            base_candidate,value=scope.target,
                            source=f"{owner_scope} / Reusable template",
                            sequence=next_sequence,
                        )); next_sequence+=1
                if remove_indexes or additions:
                    candidates=[
                        item for index,item in enumerate(candidates)
                        if index not in remove_indexes
                    ]+additions

        relationship_fingerprint = ""
        held_facts: dict[tuple[str, str], tuple[str, tuple[str, ...], str]] = {}
        generated_facts: dict[tuple[str, str], tuple[tuple[str, ...], str]] = {}
        if self.hierarchy_policy != HIERARCHY_AS_CONFIRMED and knowledge is not None:
            if relationship_evaluator is None:
                from .relationship_evaluator import active_relationship_evaluator

                relationship_evaluator = active_relationship_evaluator(knowledge)
            relationship_fingerprint = relationship_evaluator.registry.fingerprint()
            (
                candidates,
                hierarchy_decisions,
                held_facts,
                generated_facts,
            ) = self._apply_hierarchy_policy(
                candidates,
                knowledge,
                relationship_evaluator,
            )
            decisions.extend(hierarchy_decisions)
        held_facts.update(template_held_facts)
        if template_projection_fingerprint:
            relationship_fingerprint=hashlib.sha256(
                (relationship_fingerprint+template_projection_fingerprint).encode("utf-8")
            ).hexdigest()

        ordering = TagOrderingService(knowledge, character_db)
        ordered = ordering.order_with_trace(
            candidates,
            excluded=[*annotation.confirmed_absent, *annotation.export_excluded],
            canonicalize=self.canonicalize_aliases and knowledge is not None,
            deterministic=self.apply_system_ordering and knowledge is not None,
        )
        absent_keys = {
            ordering.canonical_key(value)
            for value in annotation.confirmed_absent
            if value.strip()
        }
        excluded_keys = {
            ordering.canonical_key(value)
            for value in annotation.export_excluded
            if value.strip()
        }
        for decision in ordered.decisions:
            reason = decision.reason
            if decision.canonical_key in absent_keys:
                reason = "Omitted because this tag is confirmed absent from the image."
            elif decision.canonical_key in excluded_keys:
                reason = "Omitted by this image's explicit training-export exclusion."
            projected = ProjectionDecision(
                    "sdxl_tag",
                    decision.source_value,
                    decision.source or "SDXL Tags",
                    decision.action,
                    decision.output,
                    reason,
                )
            held = held_facts.get((decision.source_value, decision.source))
            generated = generated_facts.get((decision.source_value, decision.source))
            if held is not None and decision.action != "omitted":
                held_reason, path, owner_scope = held
                projected = replace(
                    projected,
                    action="held",
                    reason=held_reason,
                    relationship_path=path,
                    owner_scope=owner_scope,
                    profile_policy=self.hierarchy_policy,
                )
            elif generated is not None and decision.action != "omitted":
                path, owner_scope = generated
                projected = replace(
                    projected,
                    reason="Included this reviewed broader Tag for the selected hierarchy policy.",
                    relationship_path=path,
                    owner_scope=owner_scope,
                    profile_policy=self.hierarchy_policy,
                )
            decisions.append(projected)
        output = list(ordered.values)

        if self.apply_automation_rules:
            from .automation_repository import active_automation_engine
            output, steps = active_automation_engine(knowledge).project_tags_with_steps(
                output, automatic_only=True
            )
            decisions = self._apply_rule_steps(decisions, steps)

        for value in annotation.plain_language:
            self._add_prose_decision(
                value,
                source="Plain Language Descriptions",
                included=self.include_plain_language,
                omitted_reason="Plain Language Descriptions are disabled in the selected profile.",
                rating_reason=rating_reason,
                prose=prose,
                decisions=decisions,
            )

        if rating_omission:
            output = []
            prose = []

        return CaptionProjection(
            tuple(output),
            tuple(prose),
            tuple(decisions),
            relationship_fingerprint,
        )

    def _apply_hierarchy_policy(self, candidates, knowledge, evaluator):
        """Apply reviewed relations only where Image Knowledge identifies an owner."""
        retained = list(candidates)
        removed_indexes: set[int] = set()
        decisions: list[ProjectionDecision] = []
        held: dict[tuple[str, str], tuple[str, tuple[str, ...], str]] = {}
        generated: dict[tuple[str, str], tuple[tuple[str, ...], str]] = {}
        next_sequence = max((item.sequence for item in retained), default=-1) + 1
        slots = sorted(
            {item.subject_slot for item in retained if item.subject_slot is not None}
        )
        for slot in slots:
            owner_scope = f"Character {slot}"
            resolved: dict[str, list[tuple[int, TagOrderCandidate]]] = {}
            for index, candidate in enumerate(retained):
                if candidate.subject_slot != slot:
                    continue
                record = knowledge.find(candidate.value)
                if record is not None:
                    resolved.setdefault(record.id, []).append((index, candidate))
            if not resolved:
                continue

            projection = evaluator.keep_most_specific(
                resolved,
                scope="same_character",
            )
            by_tag = {item.tag_id: item for item in projection.decisions}
            for tag_id, occurrences in resolved.items():
                hierarchy_decision = by_tag.get(tag_id)
                if hierarchy_decision is None:
                    continue
                if hierarchy_decision.action == "held":
                    reason = (
                        f"Held contrary values for review for {owner_scope}; "
                        "the hierarchy did not choose one automatically."
                    )
                    for _, candidate in occurrences:
                        held[(candidate.value, candidate.source)] = (
                            reason,
                            hierarchy_decision.relationship_path,
                            owner_scope,
                        )
                elif (
                    self.hierarchy_policy == HIERARCHY_KEEP_MOST_SPECIFIC
                    and hierarchy_decision.action == "omitted"
                ):
                    target = knowledge.tags.get(hierarchy_decision.output_tag_id)
                    output = target.display if target is not None else hierarchy_decision.output_tag_id
                    for index, candidate in occurrences:
                        removed_indexes.add(index)
                        decisions.append(
                            ProjectionDecision(
                                "sdxl_tag",
                                candidate.value,
                                candidate.source,
                                "omitted",
                                output,
                                f"Kept the more specific compatible value {output!r} for {owner_scope}.",
                                hierarchy_decision.relationship_path,
                                owner_scope,
                                self.hierarchy_policy,
                            )
                        )

            if (
                self.hierarchy_policy != HIERARCHY_INCLUDE_BROADER
                or projection.conflicts
            ):
                continue
            present_ids = set(resolved)
            for tag_id, occurrences in tuple(resolved.items()):
                source_candidate = occurrences[0][1]
                for broader_id, path in evaluator.broader_paths(tag_id).items():
                    if broader_id in present_ids:
                        continue
                    broader = knowledge.tags.get(broader_id)
                    if broader is None:
                        continue
                    source = f"{owner_scope} / Hierarchy"
                    added = replace(
                        source_candidate,
                        value=broader.display,
                        source=source,
                        sequence=next_sequence,
                    )
                    next_sequence += 1
                    retained.append(added)
                    generated[(added.value, added.source)] = (path, owner_scope)
                    present_ids.add(broader_id)

        return (
            [
                candidate
                for index, candidate in enumerate(retained)
                if index not in removed_indexes
            ],
            decisions,
            held,
            generated,
        )

    @staticmethod
    def _add_prose_decision(
        value: str,
        *,
        source: str,
        included: bool,
        omitted_reason: str,
        rating_reason: str,
        prose: list[str],
        decisions: list[ProjectionDecision],
    ) -> None:
        value = value.strip()
        if not value:
            return
        if rating_reason or not included:
            decisions.append(
                ProjectionDecision(
                    "plain_language",
                    value,
                    source,
                    "omitted",
                    reason=rating_reason or omitted_reason,
                )
            )
            return
        prose.append(value)
        decisions.append(
            ProjectionDecision(
                "plain_language",
                value,
                source,
                "emitted",
                output=value,
                reason="Included by the selected training profile.",
            )
        )

    @staticmethod
    def _apply_rule_steps(decisions, steps):
        updated = list(decisions)
        for step in steps:
            sources = {normalize_tag(value) for value in step.sources}
            if not sources:
                continue
            for index, decision in enumerate(updated):
                if (
                    decision.fact_type != "sdxl_tag"
                    or decision.action in {"omitted", "held"}
                    or normalize_tag(decision.output) not in sources
                ):
                    continue
                if step.kind == "omit":
                    action = "omitted"
                    output = ""
                    reason = f"Training automation omitted this fact: {step.label}."
                else:
                    action = "combined"
                    output = step.target
                    reason = f"Training automation combined this fact: {step.label}."
                updated[index] = replace(
                    decision,
                    action=action,
                    output=output,
                    reason=reason,
                )
        return updated

    def includes(self, annotation: ImageAnnotation, knowledge: TagDatabase, categories=None) -> bool:
        """Whether an image belongs in an export using this rating profile."""
        maximum = self.maximum_content_rating
        if maximum not in RATING_ORDER:
            maximum = "explicit"
        return RATING_ORDER[effective_rating(annotation, knowledge, categories)] <= RATING_ORDER[maximum]

def builtin_export_profiles() -> tuple[ExportProfile, ...]:
    """Return fresh built-in profiles so callers cannot mutate shared state."""
    default = ExportProfile()
    return (
        default,
        replace(
            default,
            profile_id="collectigen.sdxl.tags-only",
            name="SDXL tags only",
            description="Export ordered SDXL Tags without Plain Language Descriptions.",
            caption_strategy="sdxl_tags_only",
            include_plain_language=False,
        ),
        replace(
            default,
            profile_id="collectigen.plain-language.descriptions-only",
            version=4,
            format_version=None,
            name="Plain language descriptions only",
            description="Export confirmed Plain Language Descriptions without SDXL Tags.",
            target_model_family="Plain language",
            caption_strategy="plain_language_only",
            vocabulary_assumptions=(),
            include_character_names=False,
            include_character_tags=False,
            include_outfit_names=False,
            include_outfit_tags=False,
            include_outfit_prose=True,
            include_object_identities=False,
            include_object_tags=False,
            include_global=False,
            include_scene=False,
            include_contexts=False,
            include_sdxl=False,
            include_uncategorized=False,
            include_custom_groups=False,
        ),
        replace(
            default,
            profile_id="collectigen.sdxl.context-aware",
            version=6,
            format_version=6,
            name="Context-aware SDXL tags and descriptions",
            description=(
                "Export ordered SDXL Tags and descriptions with explicit "
                "secondary image-context relationships."
            ),
            caption_strategy="context_aware_sdxl_tags_then_plain_language",
            include_contexts=True,
        ),
    )


def export_profile_by_id(profile_id: str | None) -> ExportProfile:
    """Resolve a built-in profile and safely fall back to the compatible default."""
    for profile in builtin_export_profiles():
        if profile.profile_id == profile_id:
            return profile
    return ExportProfile()


DEFAULT_EXPORT_PROFILE = ExportProfile()
