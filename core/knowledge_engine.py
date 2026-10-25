from __future__ import annotations

import difflib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping

from .tag_collection import normalize_tag


@dataclass
class ModifierValue:
    id: str
    display: str
    group: str
    aliases: list[str] = field(default_factory=list)

    def forms(self) -> set[str]:
        return {normalize_tag(self.display), *(normalize_tag(v) for v in self.aliases)}


@dataclass
class ConceptRecord:
    id: str
    display: str
    category: str
    aliases: list[str] = field(default_factory=list)
    allowed_modifiers: list[str] = field(default_factory=list)

    def forms(self) -> set[str]:
        return {normalize_tag(self.display), *(normalize_tag(v) for v in self.aliases)}


@dataclass
class KnowledgeSuggestion:
    tag: str
    category: str = "uncategorized"
    concept_id: str | None = None
    properties: dict[str, str] = field(default_factory=dict)
    confidence: float = 0.0
    reason: str = ""
    pattern_id: str = ""
    similar_tags: list[str] = field(default_factory=list)
    parent_id: str | None = None
    generic_tag: str = ""

    @property
    def group_key(self) -> str:
        if self.pattern_id:
            return self.pattern_id
        if self.concept_id:
            props = ",".join(sorted(self.properties))
            return f"concept:{self.concept_id}:{props}"
        return ""


@dataclass(frozen=True)
class ModifierPolicyResolution:
    """Declared policy source; is_resolved does not assert executable readiness."""

    groups: tuple[str, ...] = ()
    source_kind: str = "none"
    source_tag_id: str = ""
    source_label: str = ""

    @property
    def is_explicit(self) -> bool:
        return self.source_kind == "explicit"

    @property
    def is_resolved(self) -> bool:
        return self.source_kind != "none"


@dataclass(frozen=True)
class FoundationInterpretation:
    """Detached capability evidence; lexical availability never grants execution."""
    kind: str
    reference: str
    lexical_bank_available: bool = False
    native_family_id: str = ""
    native_family_candidates: tuple[str, ...] = ()
    executable: bool = False
    reason: str = ""
    value_id: str = ""
    property_key: str = ""

    @property
    def native_family_available(self):
        return bool(self.native_family_id)


@dataclass(frozen=True)
class ModifierSlotSuggestion:
    """One exact, non-authoritative suggestion for a named modifier slot."""

    group: str
    status: str
    suggested_value_id: str = ""
    candidate_value_ids: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class ModifierPolicyBaseEntry:
    """One canonical base-tag policy shown from a modifier's reverse view."""

    tag_id: str
    display: str
    category_id: str
    groups: tuple[str, ...]
    source_kind: str
    source_label: str
    provenance_source: str = ""
    reviewed_at: str = ""
    policy_fingerprint: str = ""
    authority: str = ""
    relationship_ids: tuple[str, ...] = ()
    evidence_status: str = "reviewed"
    evidence_label: str = "Reviewed exact family authority"
    evidence_reason: str = ""

    @property
    def is_migration(self) -> bool:
        return "migrat" in self.provenance_source.casefold()

    @property
    def needs_source_review(self) -> bool:
        return self.source_kind == "explicit" and not self.provenance_source

    @property
    def needs_evidence_review(self) -> bool:
        return self.evidence_status != "reviewed"


@dataclass(frozen=True)
class LegacyModifierMatch:
    """One exact shipped-literal match retained as migration evidence."""

    value_id: str
    display: str
    group: str
    sources: tuple[str, ...]


@dataclass(frozen=True)
class LegacyModifierCandidate:
    """One stable Tag whose canonical name or alias matches legacy evidence."""

    tag_id: str
    display: str
    category_id: str
    category_label: str
    matches: tuple[LegacyModifierMatch, ...]
    legacy_groups: tuple[str, ...]
    current_family_id: str = ""
    current_family_label: str = ""
    effective_family_id: str = ""
    effective_family_label: str = ""
    suggested_family_ids: tuple[str, ...] = ()
    suggested_family_labels: tuple[str, ...] = ()
    reviewed_decision: str = ""
    decision_family_id: str = ""
    decision_family_label: str = ""
    decision_relationship_id: str = ""
    decision_record_fingerprint: str = ""
    decision_candidate_fingerprint: str = ""
    decision_layer: str = ""
    decision_source: str = ""
    decision_reviewed_at: str = ""
    status: str = "review_required"
    ambiguity_reasons: tuple[str, ...] = ()
    fingerprint: str = ""

    @property
    def requires_review(self) -> bool:
        return self.status not in {
            "category_backed", "reviewed_modifier", "reviewed_ordinary",
        }


@dataclass(frozen=True)
class LegacyModifierInventory:
    """Read-only aggregate for the conservative modifier migration review."""

    total_tags: int
    total_legacy_values: int
    matched_legacy_values: int
    shared_legacy_values: int
    candidates: tuple[LegacyModifierCandidate, ...]
    counts_by_group: tuple[tuple[str, int], ...]

    @property
    def unmatched_legacy_values(self) -> int:
        return self.total_legacy_values - self.matched_legacy_values

    @property
    def category_backed_count(self) -> int:
        return sum(item.status == "category_backed" for item in self.candidates)

    @property
    def review_required_count(self) -> int:
        return sum(item.requires_review for item in self.candidates)

    @property
    def ambiguous_count(self) -> int:
        return sum(item.status == "ambiguous" for item in self.candidates)

    @property
    def family_mismatch_count(self) -> int:
        return sum(item.status == "family_mismatch" for item in self.candidates)

    @property
    def reviewed_modifier_count(self) -> int:
        return sum(item.status == "reviewed_modifier" for item in self.candidates)

    @property
    def reviewed_ordinary_count(self) -> int:
        return sum(item.status == "reviewed_ordinary" for item in self.candidates)

    @property
    def stale_decision_count(self) -> int:
        return sum(item.status == "stale_decision" for item in self.candidates)


class FoundationKnowledge:
    """Foundation concepts and reusable modifier values shipped with CollectiGen."""

    def __init__(self, modifiers_path: str | Path, concepts_path: str | Path) -> None:
        self.modifiers_path = Path(modifiers_path)
        self.concepts_path = Path(concepts_path)
        self.modifiers: dict[str, list[ModifierValue]] = {}
        self.modifier_group_priorities: dict[str, int] = {}
        self.concepts: dict[str, ConceptRecord] = {}
        self._modifier_lookup: dict[str, ModifierValue] = {}
        self._modifier_matches: dict[str, list[ModifierValue]] = {}
        self._concept_lookup: dict[str, ConceptRecord] = {}
        self.load()

    def load(self) -> None:
        self.modifiers.clear(); self.modifier_group_priorities.clear(); self.concepts.clear(); self._modifier_lookup.clear(); self._modifier_matches.clear(); self._concept_lookup.clear()
        sources={path:(path.read_bytes() if path.exists() else None) for path in (self.modifiers_path,self.concepts_path)}
        self._loaded_source_fingerprints={path:(sha256(raw).digest() if raw is not None else ()) for path,raw in sources.items()}
        if sources[self.modifiers_path] is not None:
            payload = json.loads(sources[self.modifiers_path].decode("utf-8"))
            configured=payload.get("group_priorities",{})
            self.modifier_group_priorities={str(key):int(value) for key,value in configured.items()} if isinstance(configured,dict) else {}
            for group, items in payload.get("groups", {}).items():
                values: list[ModifierValue] = []
                for item in items:
                    if isinstance(item, str):
                        record = ModifierValue(normalize_tag(item).replace(" ", "_"), item, group)
                    else:
                        record = ModifierValue(
                            id=str(item.get("id") or normalize_tag(item.get("display", "value")).replace(" ", "_")),
                            display=str(item.get("display") or item.get("id") or "value"),
                            group=group,
                            aliases=[str(v) for v in item.get("aliases", [])],
                        )
                    values.append(record)
                    for form in record.forms():
                        self._modifier_lookup[form] = record
                        matches=self._modifier_matches.setdefault(form,[])
                        if not any(
                            value.id==record.id and value.group==record.group
                            for value in matches
                        ):
                            matches.append(record)
                self.modifiers[group] = values
                self.modifier_group_priorities.setdefault(group,(len(payload.get("groups",{}))-len(self.modifiers)+1)*100)
        if sources[self.concepts_path] is not None:
            payload = json.loads(sources[self.concepts_path].decode("utf-8"))
            for item in payload.get("concepts", []):
                if not isinstance(item, dict): continue
                record = ConceptRecord(
                    id=str(item.get("id")),
                    display=str(item.get("display") or item.get("id")),
                    category=str(item.get("category") or "uncategorized"),
                    aliases=[str(v) for v in item.get("aliases", [])],
                    allowed_modifiers=[str(v) for v in item.get("allowed_modifiers", [])],
                )
                self.concepts[record.id] = record
                for form in record.forms(): self._concept_lookup[form] = record

    def modifier_for(self, value: str) -> ModifierValue | None:
        """Return one shipped literal for parsing or migration evidence.

        The legacy registry is not stable-Tag authority.  Callers that need to
        classify a Tag must use :meth:`modifier_for_tag`, which resolves only a
        reviewed category-backed family.
        """
        return self._modifier_lookup.get(normalize_tag(value))

    def modifiers_for(self, value: str) -> tuple[ModifierValue, ...]:
        """Return every exact shipped match without choosing semantic authority."""
        return tuple(self._modifier_matches.get(normalize_tag(value),()))

    def concept_for(self, value: str) -> ConceptRecord | None:
        return self._concept_lookup.get(normalize_tag(value))

    @staticmethod
    def _modifier_family_name_key(value: str) -> str:
        key=normalize_tag(value).replace(" ","_")
        return key[:-1] if key.endswith("s") else key

    def canonical_modifier_group(self, group: str, categories=None) -> str:
        """Resolve a legacy family name to one reviewed category stable ID.

        Older policies used shipped names such as ``color`` and ``pattern``.
        They remain readable, but once category knowledge is available the
        effective policy points at the category that owns the reviewed values.
        """
        requested=str(group or "").strip()
        if not requested or categories is None:
            return requested
        family_ids=tuple(categories.reusable_modifier_family_ids())
        if requested in family_ids:
            return requested
        wanted=self._modifier_family_name_key(requested)
        role_for_legacy={
            "color":"modifier_color",
            "material":"modifier_material",
            "pattern":"modifier_pattern",
        }.get(wanted,"")
        matches=[]
        for family_id in family_ids:
            record=categories.get(family_id)
            if record is None:
                continue
            role=str(categories.effective_semantic_role(family_id) or "")
            name_key=self._modifier_family_name_key(record.name)
            if role_for_legacy and role==role_for_legacy or name_key==wanted:
                matches.append(family_id)
        return matches[0] if len(matches)==1 else requested

    def ordered_modifier_groups(self, groups: Iterable[str], categories=None) -> list[str]:
        if categories is not None:
            result=[]
            for group in groups:
                stable_id=self.canonical_modifier_group(str(group),categories)
                if stable_id and stable_id not in result:
                    result.append(stable_id)
            return result
        requested={str(group) for group in groups if str(group)}
        known=sorted(
            {group for group in requested if group in self.modifiers},
            key=lambda group:(-self.modifier_group_priorities.get(group,0),group.casefold()),
        )
        return [*known,*sorted(requested-set(known),key=str.casefold)]

    def modifier_group_definitions(
        self,
        tags,
        categories=None,
        modifier_roles: Mapping[str, Any] | None = None,
    ) -> dict[str,list[ModifierValue]]:
        """Return category-backed reviewed values for every modifier family.

        A family is a role on a category, not a requirement that the category
        live beneath a special taxonomy parent.  Descendant categories inherit
        the nearest family role, while each value keeps its SDXL Tag stable ID
        and aliases.
        """
        if categories is None:
            return {group:list(values) for group,values in self.modifiers.items()}
        result={family_id:[] for family_id in categories.reusable_modifier_family_ids()}
        for record in tags.tags.values():
            role=(modifier_roles or {}).get(str(record.id))
            if role is not None and getattr(role,"is_ordinary",False):
                continue
            family_id=(
                str(getattr(role,"family_id","") or "")
                if role is not None else
                categories.modifier_family_source(getattr(record,"category",None))
            )
            if family_id not in result:
                continue
            result[family_id].append(ModifierValue(
                id=str(record.id),display=str(record.display),group=family_id,
                aliases=list(tags.effective_aliases(record)),
            ))
        for values in result.values():
            values.sort(key=lambda value:(value.display.casefold(),value.id))
        return result

    def modifier_group_label(self, group: str, categories=None) -> str:
        stable_id=self.canonical_modifier_group(group,categories)
        if categories is not None and (record:=categories.get(stable_id)) is not None:
            semantic_label={
                "modifier_color":"Color",
                "modifier_material":"Material",
                "modifier_pattern":"Pattern",
            }.get(str(categories.effective_semantic_role(stable_id) or ""))
            if semantic_label:
                return semantic_label
            return str(record.name)
        return str(group or "").replace("_"," ").title()

    @staticmethod
    def _composition_remainders(record, base) -> set[tuple[str, ...]]:
        """Return only exact prefix/suffix differences between tag and base forms."""
        record_forms={
            normalize_tag(value)
            for value in (getattr(record,"display",""),*getattr(record,"aliases",()))
            if normalize_tag(value)
        }
        base_forms={
            normalize_tag(value)
            for value in (getattr(base,"display",""),*getattr(base,"aliases",()))
            if normalize_tag(value)
        }
        remainders=set()
        for record_form in record_forms:
            for base_form in base_forms:
                if record_form.startswith(base_form+" "):
                    remainder=record_form[len(base_form):].strip()
                elif record_form.endswith(" "+base_form):
                    remainder=record_form[:-len(base_form)].strip()
                else:
                    continue
                if remainder:
                    remainders.add(tuple(remainder.split()))
        return remainders

    def suggest_modifier_slots(
        self,record,base,groups: Iterable[str],
        definitions: Mapping[str,Iterable[ModifierValue]],current_values=(),
    ) -> tuple[ModifierSlotSuggestion, ...]:
        """Parse exact tag/base forms into reviewed slots without applying them.

        A result exists only when every remaining token is an exact canonical or
        alias form from an allowed family and no family is used more than once.
        Multiple complete decompositions are returned as review-only ambiguity.
        """
        ordered=tuple(dict.fromkeys(str(group) for group in groups if str(group)))
        if not ordered or record is None or base is None:
            return ()
        group_position={group:index for index,group in enumerate(ordered)}
        lexemes=[]
        value_groups={}
        for group in ordered:
            for value in definitions.get(group,()):
                value_groups.setdefault(str(value.id),set()).add(group)
                for form in value.forms():
                    tokens=tuple(form.split())
                    if tokens:
                        lexemes.append((tokens,group,str(value.id)))
        lexemes.sort(key=lambda item:(-len(item[0]),group_position[item[1]],item[2]))
        solutions=set()
        for words in self._composition_remainders(record,base):
            def walk(position,used,assigned):
                if len(solutions)>=128:
                    return
                if position==len(words):
                    solutions.add(tuple(
                        (group,value_id) for group in ordered
                        if (value_id:=assigned.get(group))
                    ))
                    return
                for tokens,group,value_id in lexemes:
                    if group in used or words[position:position+len(tokens)]!=tokens:
                        continue
                    walk(
                        position+len(tokens),used|{group},
                        {**assigned,group:value_id},
                    )
            walk(0,set(),{})
        if not solutions:
            return ()
        current_by_group={group:[] for group in ordered}
        for raw_value in dict.fromkeys(str(value) for value in current_values if str(value)):
            matched=value_groups.get(raw_value,set())
            if len(matched)==1:
                current_by_group[next(iter(matched))].append(raw_value)
        if len(solutions)==1:
            solution=dict(next(iter(solutions)))
            result=[]
            for group in ordered:
                suggested=solution.get(group,"")
                if not suggested:
                    continue
                current=current_by_group[group]
                status=(
                    "current" if current==[suggested] else
                    "contradictory" if current else
                    "suggested"
                )
                reason=(
                    "The reviewed value already matches the exact tag and Base Tag names."
                    if status=="current" else
                    "The exact tag and Base Tag names differ by this reviewed value."
                    if status=="suggested" else
                    "The exact-name suggestion differs from the current reviewed value."
                )
                result.append(ModifierSlotSuggestion(
                    group=group,status=status,suggested_value_id=suggested,
                    candidate_value_ids=(suggested,),reason=reason,
                ))
            return tuple(result)
        result=[]
        for group in ordered:
            candidates=tuple(sorted({
                value_id for solution in solutions
                for candidate_group,value_id in solution
                if candidate_group==group
            }))
            if not candidates:
                continue
            result.append(ModifierSlotSuggestion(
                group=group,status="ambiguous",
                candidate_value_ids=candidates,
                reason=(
                    "The exact tag and Base Tag names have multiple complete "
                    "reviewed modifier interpretations."
                ),
            ))
        return tuple(result)

    def interpret_value_annotation(self, record) -> FoundationInterpretation:
        reference = str(getattr(record, "concept_id", "") or "")
        if reference in self.concepts:
            return FoundationInterpretation("native_concept_reference", reference)
        bank = self.modifiers.get(reference)
        if not bank:
            return FoundationInterpretation("unsupported_annotation", reference, reason="missing_concept_and_lexical_bank")
        properties = getattr(record, "properties", {})
        names = (getattr(record, "display", ""), *getattr(record, "aliases", ()))
        name_matches = {row.id for row in bank if any(normalize_tag(name) in row.forms() for name in names)}
        if len(name_matches) != 1:
            return FoundationInterpretation("unsupported_annotation", reference, True, reason="ambiguous_or_missing_lexical_value")
        # The declared group plus exact bank value name is structural evidence.
        # An optional property is corroborating evidence and must not contradict it.
        for key in dict.fromkeys((reference, "value")):
            if key not in properties:
                continue
            value = properties[key]
            property_matches = {row.id for row in bank if isinstance(value, str) and normalize_tag(value) in row.forms()}
            if property_matches != name_matches:
                return FoundationInterpretation("unsupported_annotation", reference, True, reason="conflicting_group_value_evidence")
        return FoundationInterpretation("legacy_value_annotation", reference, True,
            value_id=next(iter(name_matches)), property_key=reference)

    def interpret_group_declaration(self, group, categories=None) -> FoundationInterpretation:
        requested = str(group)
        family_ids = tuple(categories.reusable_modifier_family_ids()) if categories is not None else ()
        canonical = self.canonical_modifier_group(requested, categories)
        native = canonical if canonical in family_ids else ""
        wanted = self._modifier_family_name_key(requested)
        role = {"color":"modifier_color", "material":"modifier_material", "pattern":"modifier_pattern"}.get(wanted)
        candidates = tuple(identity for identity in family_ids if
            (role and categories.effective_semantic_role(identity) == role) or
            self._modifier_family_name_key(categories.get(identity).name) == wanted)
        reason = "canonical_policy_validation_required" if native else (
            "ambiguous_native_families" if len(candidates) > 1 else
            "native_family_unavailable" if self.modifiers.get(requested) else "lexical_bank_and_native_family_missing")
        return FoundationInterpretation("legacy_group_declaration", requested,
            bool(self.modifiers.get(requested)), native, candidates, reason=reason, property_key=requested)

    def interpret_validated_policy(self, service, tag_id) -> FoundationInterpretation:
        from .allowed_dimension_policy import AllowedDimensionPolicyService, VALUE_SCOPE_UNREVIEWED
        if type(service) is not AllowedDimensionPolicyService or service.foundation is not self:
            raise TypeError("Exact native policy validator required")
        policy = service.effective_for_tag(tag_id)
        executable = (policy.authority in {"canonical_default", "canonical_user"}
            and policy.is_resolved and not policy.blocked_reason
            and all(scope.mode != VALUE_SCOPE_UNREVIEWED for scope in policy.value_scopes))
        return FoundationInterpretation("resolved_native_policy" if executable else "unsupported_policy",
            str(tag_id), executable=executable,
            reason="" if executable else policy.blocked_reason or "canonical_policy_validation_required")

    def concept_for_tag(self, record) -> ConceptRecord | None:
        """Resolve only one exact reviewed concept identity for a tag record."""
        concept_id=str(getattr(record,"concept_id","") or "")
        if concept_id in self.concepts:
            return self.concepts[concept_id]
        matches={
            concept.id:concept
            for value in (getattr(record,"display",""),*getattr(record,"aliases",()))
            if (concept:=self.concept_for(value)) is not None
        }
        return next(iter(matches.values())) if len(matches)==1 else None

    def modifier_policy_concept_for_tag(self, record) -> ConceptRecord | None:
        """Resolve foundation modifier policy from exact Tag identity only.

        ``concept_id`` also classifies composed and broadly promoted vocabulary.
        That reusable-subject classification is not an exact reviewed decision
        that every classified Tag owns the concept's Allowed Details policy.
        Only an exact canonical name or declared alias may seed that fallback.
        """
        matches={
            concept.id:concept
            for value in (getattr(record,"display",""),*getattr(record,"aliases",()))
            if (concept:=self.concept_for(value)) is not None
        }
        return next(iter(matches.values())) if len(matches)==1 else None

    def modifier_for_tag(
        self,
        record,
        categories=None,
        modifier_roles: Mapping[str, Any] | None = None,
    ) -> ModifierValue | None:
        """Resolve one reviewed explicit or category-backed modifier Tag role.

        Exact canonical names and aliases in the shipped literal registry are
        migration candidates, not runtime role authority.  Failing closed when
        category knowledge is unavailable also prevents old callers from
        silently restoring that lexical classification.
        """
        if record is not None and categories is not None:
            role=(modifier_roles or {}).get(str(record.id))
            if role is not None and getattr(role,"is_ordinary",False):
                return None
            group=(
                str(getattr(role,"family_id","") or "")
                if role is not None else
                categories.modifier_family_source(getattr(record,"category",None))
            )
            if group:
                return ModifierValue(
                    id=str(record.id),display=str(record.display),group=group,
                    aliases=[str(value) for value in getattr(record,"aliases",())],
                )
        return None

    def legacy_modifier_inventory(
        self,
        tags,
        categories=None,
        modifier_roles: Mapping[str, Any] | None = None,
    ) -> LegacyModifierInventory:
        """Inventory exact legacy literal matches without changing any authority.

        Canonical names and declared aliases are evidence only.  The report
        records the current stable category-family role separately, identifies
        ambiguous shared literals and mismatched families, and fingerprints the
        exact source state for a future stale-guarded review transaction.
        """
        records=tuple(sorted(tags.tags.values(),key=lambda item:item.id))
        matches_by_tag: dict[str, dict[tuple[str,str], dict[str,Any]]] = {}
        tag_ids_by_value: dict[tuple[str,str], set[str]] = {}
        for record in records:
            found: dict[tuple[str,str], dict[str,Any]] = {}
            sources=(
                ("Canonical name",str(getattr(record,"display","") or "")),
                *(("Alias",str(value)) for value in getattr(record,"aliases",())),
            )
            for source_kind,text in sources:
                if not normalize_tag(text):
                    continue
                for value in self.modifiers_for(text):
                    key=(value.group,value.id)
                    entry=found.setdefault(key,{"value":value,"sources":set()})
                    entry["sources"].add(f'{source_kind}: {text}')
                    tag_ids_by_value.setdefault(key,set()).add(record.id)
            if found:
                matches_by_tag[record.id]=found

        family_ids=(
            set(categories.reusable_modifier_family_ids())
            if categories is not None else set()
        )
        candidates=[]
        group_counts: Counter[str]=Counter()
        for record in records:
            found=matches_by_tag.get(record.id)
            if not found:
                continue
            groups=tuple(self.ordered_modifier_groups({key[0] for key in found}))
            for group in groups:
                group_counts[group]+=1
            group_order={group:index for index,group in enumerate(groups)}
            matches=tuple(
                LegacyModifierMatch(
                    value_id=entry["value"].id,
                    display=entry["value"].display,
                    group=entry["value"].group,
                    sources=tuple(sorted(entry["sources"],key=str.casefold)),
                )
                for key,entry in sorted(
                    found.items(),
                    key=lambda item:(
                        group_order.get(item[0][0],len(group_order)),
                        item[1]["value"].display.casefold(),item[0][1],
                    ),
                )
            )
            current_family=(
                str(categories.modifier_family_source(getattr(record,"category",None)) or "")
                if categories is not None else ""
            )
            suggested=[]
            for group in groups:
                stable_id=self.canonical_modifier_group(group,categories)
                if stable_id in family_ids and stable_id not in suggested:
                    suggested.append(stable_id)
            ambiguity=[]
            if len(matches)>1:
                ambiguity.append("The Tag name and aliases match multiple legacy values.")
            if len(groups)>1:
                ambiguity.append("The matched legacy values belong to multiple families.")
            if any(len(tag_ids_by_value.get((item.group,item.value_id),()))>1 for item in matches):
                ambiguity.append("At least one legacy value matches multiple stable Tags.")
            category_id=str(getattr(record,"category","") or "")
            category_label=(
                categories.path_label(category_id)
                if categories is not None and categories.get(category_id) is not None
                else category_id or "Uncategorized"
            )
            current_family_label=(
                categories.path_label(current_family)
                if categories is not None and current_family else ""
            )
            suggested_labels=tuple(
                categories.path_label(stable_id) if categories is not None else stable_id
                for stable_id in suggested
            )
            role=(modifier_roles or {}).get(str(record.id))
            fingerprint_payload={
                "tag_id":record.id,"display":record.display,
                "aliases":list(getattr(record,"aliases",())),
                "category_id":category_id,"current_family_id":current_family,
                "suggested_family_ids":suggested,
                "matches":[
                    [item.group,item.value_id,*item.sources] for item in matches
                ],
            }
            fingerprint=sha256(json.dumps(
                fingerprint_payload,ensure_ascii=False,sort_keys=True,
                separators=(",",":"),
            ).encode("utf-8")).hexdigest()
            reviewed_decision=str(getattr(role,"decision","") or "")
            decision_family_id=str(getattr(role,"family_id","") or "")
            decision_family_label=(
                categories.path_label(decision_family_id)
                if categories is not None and decision_family_id else ""
            )
            decision_candidate_fingerprint=str(
                getattr(role,"candidate_fingerprint","") or ""
            )
            if role is not None and decision_candidate_fingerprint!=fingerprint:
                status="stale_decision"
            elif role is not None and getattr(role,"is_ordinary",False):
                status="reviewed_ordinary"
            elif role is not None:
                status="reviewed_modifier"
            elif current_family and current_family in suggested:
                status="category_backed"
            elif current_family:
                status="family_mismatch"
            elif ambiguity:
                status="ambiguous"
            else:
                status="review_required"
            effective_family_id=(
                "" if role is not None and getattr(role,"is_ordinary",False)
                else decision_family_id or current_family
            )
            effective_family_label=(
                categories.path_label(effective_family_id)
                if categories is not None and effective_family_id else ""
            )
            candidates.append(LegacyModifierCandidate(
                tag_id=record.id,display=record.display,
                category_id=category_id,category_label=category_label,
                matches=matches,legacy_groups=groups,
                current_family_id=current_family,
                current_family_label=current_family_label,
                effective_family_id=effective_family_id,
                effective_family_label=effective_family_label,
                suggested_family_ids=tuple(suggested),
                suggested_family_labels=suggested_labels,
                reviewed_decision=reviewed_decision,
                decision_family_id=decision_family_id,
                decision_family_label=decision_family_label,
                decision_relationship_id=str(
                    getattr(role,"relationship_id","") or ""
                ),
                decision_record_fingerprint=str(
                    getattr(role,"record_fingerprint","") or ""
                ),
                decision_candidate_fingerprint=decision_candidate_fingerprint,
                decision_layer=str(getattr(role,"layer","") or ""),
                decision_source=str(getattr(role,"source","") or ""),
                decision_reviewed_at=str(
                    getattr(role,"reviewed_at","") or ""
                ),
                status=status,ambiguity_reasons=tuple(ambiguity),
                fingerprint=fingerprint,
            ))
        all_values={
            (value.group,value.id)
            for values in self.modifiers.values() for value in values
        }
        matched_values=set(tag_ids_by_value)
        return LegacyModifierInventory(
            total_tags=len(records),total_legacy_values=len(all_values),
            matched_legacy_values=len(matched_values),
            shared_legacy_values=sum(
                len(tag_ids)>1 for tag_ids in tag_ids_by_value.values()
            ),
            candidates=tuple(sorted(
                candidates,key=lambda item:(item.display.casefold(),item.tag_id)
            )),
            counts_by_group=tuple(sorted(group_counts.items())),
        )

    def effective_modifier_policy(self, record_or_id, tags, categories=None) -> ModifierPolicyResolution:
        """Resolve explicit, foundation, or inherited reusable dimensions.

        The result is derived from one canonical per-tag policy.  A modifier
        value can inspect compatible bases by querying this resolver; it does
        not need a second directional relationship store.
        """
        record=(tags.tags.get(record_or_id) if isinstance(record_or_id,str) else record_or_id)
        if record is None:
            return ModifierPolicyResolution()
        visited=set()
        current=record
        inherited=False
        while current is not None and current.id not in visited:
            visited.add(current.id)
            explicit=getattr(current,"allowed_modifier_groups",None)
            if explicit is not None:
                kind="inherited" if inherited else "explicit"
                label=(
                    f"Inherited from Base Tag {current.display} (explicit policy)"
                    if inherited else "Explicit on this SDXL Tag"
                )
                return ModifierPolicyResolution(
                    tuple(self.ordered_modifier_groups(explicit,categories)),kind,current.id,label,
                )
            concept=self.modifier_policy_concept_for_tag(current)
            if concept is not None:
                kind="inherited" if inherited else "foundation"
                label=(
                    f"Inherited from Base Tag {current.display}"
                    if inherited else f"Reviewed default for {current.display}"
                )
                return ModifierPolicyResolution(
                    tuple(self.ordered_modifier_groups(concept.allowed_modifiers,categories)),
                    kind,current.id,label,
                )
            base_id,_,_=tags.effective_base_tag(current,categories)
            if not base_id:
                break
            current=tags.tags.get(base_id)
            inherited=True
        return ModifierPolicyResolution()

    def modifier_policy_base_entries(
        self, tags, categories=None, *, group: str="",
    ) -> tuple[ModifierPolicyBaseEntry, ...]:
        """Inventory canonical policy owners with reviewable source evidence.

        This reverse lookup deliberately excludes inherited/composed tags.  Their
        effective policy remains derived from one stable base-tag owner, so the
        modifier editor never presents a second relationship store or counts the
        same policy many times.
        """
        requested_group=self.canonical_modifier_group(group,categories)
        result=[]
        for record in tags.tags.values():
            explicit=getattr(record,"allowed_modifier_groups",None)
            concept=self.modifier_policy_concept_for_tag(record)
            if explicit is None and concept is None:
                continue
            if explicit is not None:
                groups=tuple(self.ordered_modifier_groups(explicit,categories))
                provenance=getattr(record,"relation_extras",{}).get(
                    "allowed_modifier_groups_provenance",{}
                )
                if not isinstance(provenance,dict):
                    provenance={}
                source_kind="explicit"
                source_label="Explicit reviewed policy"
                provenance_source=str(provenance.get("source") or "")
                reviewed_at=str(provenance.get("reviewed_at") or "")
                evidence_status="needs_review"
                evidence_label="Compatibility family needs review"
                evidence_reason=(
                    "This inline compatibility policy is not canonical reviewed "
                    "family authority. Retain or remove the exact family deliberately."
                )
            else:
                groups=tuple(self.ordered_modifier_groups(concept.allowed_modifiers,categories))
                source_kind="foundation"
                source_label=f"Foundation default: {concept.display}"
                provenance_source="shipped_foundation_concept"
                reviewed_at=""
                evidence_status="reviewed"
                evidence_label="Reviewed foundation family"
                evidence_reason=(
                    f"The exact shipped foundation concept {concept.display!r} "
                    "allows this family."
                )
            if requested_group and requested_group not in groups:
                continue
            result.append(ModifierPolicyBaseEntry(
                tag_id=record.id,display=record.display,
                category_id=str(getattr(record,"category","") or ""),
                groups=groups,source_kind=source_kind,
                source_label=source_label,
                provenance_source=provenance_source,reviewed_at=reviewed_at,
                evidence_status=evidence_status,
                evidence_label=evidence_label,
                evidence_reason=evidence_reason,
            ))
        return tuple(sorted(result,key=lambda item:(item.display.casefold(),item.tag_id)))

    def modifier_policy_base_ids(self, tags, categories=None, *, group: str="") -> tuple[str, ...]:
        """Return stable IDs from the canonical policy-owner inventory."""
        return tuple(
            entry.tag_id for entry in self.modifier_policy_base_entries(
                tags,categories,group=group,
            )
        )


class SuggestionEngine:
    """Explainable suggestion engine using patterns, concepts, modifiers, and known tags."""

    def __init__(self, foundation: FoundationKnowledge, pattern_registry, tag_database) -> None:
        self.foundation = foundation
        self.patterns = pattern_registry
        self.tags = tag_database
        self._family_count=-1
        self._family_index={}

    def suggest(self, tag: str, *, include_similar: bool = True) -> KnowledgeSuggestion:
        value = normalize_tag(tag)
        pattern = self.patterns.match(value)
        parsed = self._parse_composable(value)
        if pattern:
            suggestion = parsed or KnowledgeSuggestion(tag=value)
            suggestion.category = pattern.category
            suggestion.confidence = max(suggestion.confidence, float(pattern.confidence))
            suggestion.reason = f"Matched reusable pattern: {pattern.name}"
            suggestion.pattern_id = pattern.id
            if pattern.concept_id and pattern.concept_id in self.foundation.concepts:
                concept=self.foundation.concepts[pattern.concept_id]
                suggestion.concept_id=concept.id
                parent=self.tags.find(concept.display)
                if parent is not None and normalize_tag(parent.display)!=value:
                    suggestion.parent_id=parent.id
                elif normalize_tag(concept.display)!=value:
                    suggestion.generic_tag=concept.display
            if include_similar: suggestion.similar_tags = self._similar(value)
            return suggestion
        if parsed:
            if include_similar: parsed.similar_tags = self._similar(value)
            return parsed
        count = self._subject_count(value)
        if count:
            if include_similar: count.similar_tags = self._similar(value)
            return count
        related = self._related_category(value)
        if related:
            return related
        similar = self._similar(value) if include_similar else []
        return KnowledgeSuggestion(
            tag=value,
            confidence=0.25 if similar else 0.0,
            reason="No trusted reusable pattern matched.",
            similar_tags=similar,
        )

    def family_anchors(self, value: str) -> tuple[str, ...]:
        """Return the exact lexical anchors used by known-family suggestions."""
        words=re.findall(r"[a-z0-9]+",normalize_tag(value))
        if not words:
            return ()
        anchors=[]
        for anchor in (words[-1],words[0]):
            if (
                len(anchor)<3 or anchor in anchors
                or anchor in {
                    "with","without","from","over","under","back","front","side",
                }
                or self.foundation.modifier_for(anchor) is not None
            ):
                continue
            anchors.append(anchor)
        return tuple(anchors)

    def dependency_keys(
        self, tag: str, suggestion: Mapping[str, Any] | None = None,
    ) -> tuple[str, ...]:
        """Return bounded keys whose reviewed changes can affect one suggestion."""
        value=normalize_tag(tag)
        current=suggestion if isinstance(suggestion,Mapping) else {}
        keys={f"name:{value}"} if value else set()
        pattern_id=str(current.get("pattern_id") or "")
        if not pattern_id and value:
            pattern=self.patterns.match(value)
            pattern_id=pattern.id if pattern is not None else ""
        if pattern_id:keys.add(f"pattern:{pattern_id}")
        concept_id=str(current.get("concept_id") or "")
        if concept_id:keys.add(f"concept:{concept_id}")
        parent_id=str(current.get("parent_id") or "")
        if parent_id:keys.add(f"parent:{parent_id}")
        generic=normalize_tag(current.get("generic_tag") or "")
        if generic:keys.add(f"generic:{generic}")
        keys.update(f"token:{anchor}" for anchor in self.family_anchors(value))
        return tuple(sorted(keys))

    def _related_category(self, value: str) -> KnowledgeSuggestion | None:
        """Infer only when a lexical family has a strong known-category consensus."""
        anchors=self.family_anchors(value)
        if not anchors:return None
        self._ensure_family_index()
        best=None
        categories=getattr(self.tags,"_categories",None)
        restricted={"artist","copyright_series","character_identity","rating"}
        for anchor in anchors:
            members=[record for record in self._family_index.get(anchor,()) if normalize_tag(record.display)!=value]
            counts=Counter(record.category for record in members)
            if not counts:
                continue
            category,support=counts.most_common(1)[0]
            total=sum(counts.values()); agreement=support/total
            semantic_role=(
                str(categories.effective_semantic_role(category) or "")
                if categories is not None else ""
            )
            if (
                support<2 or agreement<.80 or category in restricted
                or semantic_role in {
                    "character_source", "character_identity", "character_outfit",
                }
            ):
                continue
            generic=self.tags.find(anchor)
            concept=self.foundation.concept_for(anchor)
            grounded=bool(
                generic is not None and generic.category==category
                or concept is not None and concept.category==category
            )
            if not grounded and (support<4 or agreement<1):
                continue
            confidence=min(.94,.76+min(support,5)*.035+agreement*.07)
            candidate=(confidence,support,agreement,category,members,anchor,generic,concept)
            if best is None or candidate[:3]>best[:3]:
                best=candidate
        if best is None:
            return None
        confidence,support,agreement,category,members,anchor,generic,concept=best
        examples=[record.display for record in members if record.category==category][:6]
        return KnowledgeSuggestion(
            tag=value,category=category,confidence=confidence,
            reason=f"Related known names agree: {support} of {len(members)} use {category.replace('_',' ')}.",
            pattern_id=f"known-family:{anchor}:{category}",
            similar_tags=examples,
            parent_id=generic.id if generic is not None and normalize_tag(generic.display)!=value else None,
            generic_tag=(concept.display if concept is not None else anchor) if generic is None else "",
        )

    def _ensure_family_index(self) -> None:
        if self._family_count==len(self.tags.tags):
            return
        categories=getattr(self.tags,"_categories",None)
        index={}
        for record in self.tags.tags.values():
            if record.category=="uncategorized" or categories is not None and not categories.get(record.category):
                continue
            for token in set(re.findall(r"[a-z0-9]+",normalize_tag(record.display))):
                if len(token)>=3:
                    index.setdefault(token,[]).append(record)
        self._family_index=index
        self._family_count=len(self.tags.tags)

    def _parse_composable(self, value: str) -> KnowledgeSuggestion | None:
        words = value.split()
        # Longest concept suffix wins, allowing aliases such as "t shirt".
        concept: ConceptRecord | None = None
        concept_len = 0
        for size in range(min(3, len(words)), 0, -1):
            candidate = " ".join(words[-size:])
            concept = self.foundation.concept_for(candidate)
            if concept:
                concept_len = size
                break
        if not concept:
            return None
        prefix = words[:-concept_len]
        properties: dict[str, str] = {}
        remaining: list[str] = []
        i = 0
        while i < len(prefix):
            found = None
            found_size = 0
            for size in (3, 2, 1):
                if i + size > len(prefix): continue
                candidate = " ".join(prefix[i:i+size])
                mod = self.foundation.modifier_for(candidate)
                if mod:
                    found, found_size = mod, size
                    break
            if found:
                if not concept.allowed_modifiers or found.group in concept.allowed_modifiers:
                    properties[found.group] = found.display
                else:
                    remaining.extend(prefix[i:i+found_size])
                i += found_size
            else:
                remaining.append(prefix[i]); i += 1
        if remaining:
            return None
        if not properties and prefix:
            return None
        properties={group:properties[group] for group in self.foundation.ordered_modifier_groups(properties)}
        reason_bits = [f"Recognized concept: {concept.display}"]
        if properties:
            reason_bits.append("recognized " + ", ".join(f"{k}={v}" for k, v in properties.items()))
        generic=self.tags.find(concept.display)
        return KnowledgeSuggestion(
            tag=value,
            category=concept.category,
            concept_id=concept.id,
            properties=properties,
            confidence=0.92 if properties else 0.75,
            reason="; ".join(reason_bits),
            pattern_id=f"composable:{'+'.join(sorted(properties))}:{concept.id}" if properties else "",
            parent_id=generic.id if generic is not None and normalize_tag(generic.display)!=value else None,
            generic_tag=concept.display if generic is None and normalize_tag(concept.display)!=value else "",
        )

    @staticmethod
    def _subject_count(value: str) -> KnowledgeSuggestion | None:
        if re.fullmatch(r"\d+(?:girl|girls|boy|boys|other|others)", value) or value in {
            "solo", "multiple girls", "multiple boys", "multiple characters", "group"
        }:
            return KnowledgeSuggestion(
                tag=value,
                category="image_subject_count",
                concept_id="subject_count",
                confidence=0.99,
                reason="Recognized a standard subject-count expression.",
                pattern_id="subject_count",
            )
        return None

    def _similar(self, value: str, limit: int = 6) -> list[str]:
        known = [record.display for record in self.tags.tags.values() if record.display]
        return difflib.get_close_matches(value, known, n=limit, cutoff=0.58)
