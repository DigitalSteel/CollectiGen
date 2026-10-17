from __future__ import annotations

import json
import hashlib
import re
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from .automation_templates import concrete_sdxl_tags
from .conflict_policy import (
    ALLOW_ALL,
    CONFIRMED_OWNER_SEPARATION,
    REVIEWED_EXCEPTION_TAGS,
    SUBJECT_COUNT,
    ConflictAllowPolicy,
    ConflictSubjectPolicy,
)
from .image_record import ImageRecord
from .knowledge_relationships import SCOPED_CONSTRAINT
from .tag_collection import normalize_tag


@dataclass
class ConflictRule:
    id: str
    tags: list[str]
    scope: str = "image"
    label: str = ""
    relation: str = "mutually_exclusive"
    severity: str = "warning"
    notes: str = ""
    relationship_id: str = ""
    allow_when: ConflictAllowPolicy = field(default_factory=ConflictAllowPolicy)
    legacy_ids: tuple[str, ...] = ()
    subject_matching: ConflictSubjectPolicy = field(default_factory=ConflictSubjectPolicy)

    @classmethod
    def from_dict(cls, value: dict) -> "ConflictRule":
        return cls(
            id=str(value.get("id") or "conflict"),
            tags=[str(item) for item in value.get("tags", []) if str(item).strip()],
            scope=str(value.get("scope") or "image"),
            label=str(value.get("label") or ""),
            relation=str(value.get("relation") or "mutually_exclusive"),
            severity=str(value.get("severity") or "warning"),
            notes=str(value.get("notes") or ""),
            relationship_id=str(value.get("relationship_id") or ""),
            allow_when=ConflictAllowPolicy.from_payload(value.get("allow_when")),
            subject_matching=ConflictSubjectPolicy.from_payload(
                value.get("subject_matching")
            ),
            legacy_ids=tuple(
                dict.fromkeys(
                    str(item) for item in value.get("legacy_ids", ()) if str(item)
                )
            ),
        )

    @property
    def accepted_ids(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                value
                for value in (self.id, self.relationship_id, *self.legacy_ids)
                if value
            )
        )


@dataclass
class ConflictMatch:
    rule: ConflictRule
    found_tags: list[str]
    character_slot: int | None = None
    object_slot: int | None = None
    ambiguous: bool = False
    context_id: str = ""
    context_label: str = ""
    owner_kind: str = ""
    owner_id: str = ""
    owner_label: str = ""

    @property
    def message(self) -> str:
        label=self.rule.label or "Conflicting tags"
        values=", ".join(self.found_tags)
        if self.character_slot is not None:
            return f"{label} on character {self.character_slot}: {values}. Choose the correct tag or edit subject ownership."
        if self.object_slot is not None:
            return f"{label} on object {self.object_slot}: {values}. Choose the correct tag or edit object ownership."
        if self.ambiguous:
            return (
                f"Subject ownership unclear: {values}. These Tags can describe different "
                "subjects and are not a proven conflict. Keep both when they apply; "
                "assign their owners if needed."
            )
        if self.context_id:
            owner = (
                f" for {self.owner_label or self.owner_kind.replace('_', ' ')}"
                if self.owner_id else ""
            )
            return (
                f"{label} in {self.context_label or 'secondary context'}{owner}: {values}. "
                "Choose the correct tag or mark this image as a valid exception."
            )
        if self.owner_id:
            return (
                f"{label} for {self.owner_label or self.owner_kind.replace('_', ' ')}: "
                f"{values}. Choose the correct tag or mark this image as a valid exception."
            )
        return f"{label}: {values}. Choose the correct tag or mark this image as a valid exception."


class ConflictRuleEngine:
    """Evaluates image-wide and subject-aware tag relationships."""

    def __init__(
        self,
        rules: list[ConflictRule] | None = None,
        relationship_evaluator=None,
        *,
        relationship_error: str = "",
    ) -> None:
        self.rules = list(rules or [])
        self.relationship_evaluator = relationship_evaluator
        self.relationship_error = str(relationship_error)
        self._typed_relationships={
            item.id:item
            for item in (
                relationship_evaluator.registry.relationships
                if relationship_evaluator is not None else ()
            )
            if item.reviewed and not item.disabled
        }

    @classmethod
    def load(cls, path: str | Path, *, tags=None) -> "ConflictRuleEngine":
        source=Path(path)
        if not source.exists():
            return cls()
        payload=json.loads(source.read_text(encoding="utf-8"))
        raw=payload.get("rules",payload.get("conflicts",[])) if isinstance(payload,dict) else payload
        rules=[]
        for index,item in enumerate(raw):
            if isinstance(item,list):
                rules.append(ConflictRule(f"legacy_{index}",[str(value) for value in item]))
            elif isinstance(item,dict):
                rules.append(ConflictRule.from_dict(item))
        evaluator = None
        relationship_error = ""
        from .relationship_evaluator import active_relationship_evaluator

        try:
            evaluator = (
                active_relationship_evaluator()
                if tags is None
                else active_relationship_evaluator(tags)
            )
        except Exception as exc:
            # Keep the application available without reinterpreting a typed
            # rule as its superseded flat pairwise fallback. Training export
            # remains fail-closed and reports the store error separately.
            relationship_error = str(exc)
        if evaluator is not None:
            canonical = {
                relationship.id: relationship
                for relationship in evaluator.registry.relationships
                if relationship.kind == SCOPED_CONSTRAINT
            }
            enriched = []
            represented = set()
            for rule in rules:
                relationship = canonical.get(rule.relationship_id)
                if relationship is None:
                    enriched.append(rule)
                    continue
                represented.add(relationship.id)
                if relationship.disabled or not relationship.reviewed:
                    continue
                details = dict(relationship.details)
                enriched.append(
                    replace(
                        rule,
                        label=relationship.label or rule.label,
                        scope=relationship.scope or rule.scope,
                        notes=details.get("reason", "") or rule.notes,
                        allow_when=ConflictAllowPolicy.from_payload(
                            relationship.extras.get("allow_when")
                        ),
                        subject_matching=ConflictSubjectPolicy.from_payload(
                            relationship.extras.get("subject_matching")
                        ),
                        legacy_ids=tuple(
                            dict.fromkeys((*rule.legacy_ids, rule.id))
                        ),
                    )
                )
            for relationship in sorted(
                canonical.values(), key=lambda item: item.id
            ):
                if (
                    relationship.id in represented
                    or relationship.disabled
                    or not relationship.reviewed
                ):
                    continue
                details = dict(relationship.details)
                enriched.append(
                    ConflictRule(
                        relationship.id,
                        [],
                        scope=relationship.scope,
                        label=relationship.label or "Contrary Values",
                        relation="mutually_exclusive",
                        severity=details.get("severity", "warning"),
                        notes=details.get("reason", ""),
                        relationship_id=relationship.id,
                        allow_when=ConflictAllowPolicy.from_payload(
                            relationship.extras.get("allow_when")
                        ),
                        subject_matching=ConflictSubjectPolicy.from_payload(
                            relationship.extras.get("subject_matching")
                        ),
                    )
                )
            rules = enriched
        return cls(rules, evaluator, relationship_error=relationship_error)

    def _typed_relationship(self, rule: ConflictRule):
        if not rule.relationship_id or self.relationship_evaluator is None:
            return None
        relationship=self._typed_relationships.get(rule.relationship_id)
        return (
            relationship
            if relationship is not None
            and relationship.scope in {"any",rule.scope}
            else None
        )

    def effective_tags(self, rule: ConflictRule) -> list[str]:
        """Return the effective compatibility labels without reviving stale typed data."""
        if not rule.relationship_id:
            return list(rule.tags)
        relationship = self._typed_relationship(rule)
        if relationship is None:
            return []
        return [
            self.relationship_evaluator.registry.tag_display(endpoint.id)
            for endpoint in relationship.endpoints
            if endpoint.kind == "tag"
        ]

    def found_tags(
        self, tags: list[str], rule: ConflictRule, *,
        _resolved_ids: dict[str, str | None] | None = None,
    ) -> list[str]:
        """Find tags participating in one effective flat or canonical rule."""
        tags=concrete_sdxl_tags(tags)
        evaluator = self.relationship_evaluator
        if not rule.relationship_id:
            return self._found(tags, rule.tags)
        relationship = self._typed_relationship(rule)
        if relationship is None:
            return []
        # Only identity lookup is shared within one evaluate() call. Conflict
        # matches, scope, ownership and allowances are still evaluated per rule.
        resolved_ids = {} if _resolved_ids is None else _resolved_ids
        values_by_id: dict[str, list[str]] = {}
        for value in tags:
            if value not in resolved_ids:
                resolved_ids[value] = evaluator.registry.resolve_tag_id(value)
            tag_id = resolved_ids[value]
            if tag_id:
                values_by_id.setdefault(tag_id, []).append(value)
        conflicts = evaluator.conflicts(
            values_by_id,
            scope=rule.scope,
            relationship_ids=(rule.relationship_id,),
        )
        involved = {
            tag_id
            for conflict in conflicts
            if conflict.relationship_id == rule.relationship_id
            for _, matches in conflict.branch_matches
            for tag_id in matches
        }
        return [
            value
            for value in tags
            if resolved_ids[value] in involved
        ]

    @staticmethod
    def _found(tags:list[str], candidates:list[str]) -> list[str]:
        available={normalize_tag(value) for value in tags}
        return [value for value in candidates if normalize_tag(value) in available]

    @staticmethod
    def _single_subject(
        tags: list[str], policy: ConflictSubjectPolicy | None = None,
    ) -> bool:
        policy = policy or ConflictSubjectPolicy()
        values={normalize_tag(value) for value in tags}
        if values.intersection(policy.multiple_tags):
            return False
        exact_single_count=sum(value in values for value in policy.count_tags)
        if exact_single_count>1:
            return False
        return bool(values.intersection(policy.single_tags)) or exact_single_count == 1

    @staticmethod
    def _unowned_tags(record: ImageRecord) -> list[str]:
        annotation=record.annotation; values=[]
        for groups in (annotation.global_tags,annotation.scene,annotation.custom_groups):
            for tags in groups.values(): values.extend(tags)
        values.extend(annotation.sdxl_tags); values.extend(annotation.uncategorized)
        values.extend(annotation.visual_style.style_tags)
        return concrete_sdxl_tags(values)

    @staticmethod
    def _exception_ids(record: ImageRecord) -> set[str]:
        result = set()
        for value in record.annotation.source.get("accepted_conflict_rules", []):
            if isinstance(value, dict):
                rule_id = str(value.get("rule_id") or value.get("id") or "")
            else:
                rule_id = str(value)
            if rule_id:
                result.add(rule_id)
        return result

    def _resolved_ids(self, tags: list[str]) -> set[str]:
        evaluator = self.relationship_evaluator
        if evaluator is None:
            return set()
        return {
            tag_id
            for value in concrete_sdxl_tags(tags)
            if (tag_id := evaluator.registry.resolve_tag_id(value))
        }

    @staticmethod
    def _subject_count_range(record: ImageRecord) -> tuple[int, int | None] | None:
        values = {
            normalize_tag(value).replace(" ", "")
            for value in record.annotation.all_structured_tags(include_contexts=False)
        }
        exact_by_group: dict[str, set[int]] = {
            "girl": set(), "boy": set(), "other": set(),
        }
        lower_by_group: dict[str, int] = {}
        for value in values:
            match = re.fullmatch(r"(\d+)(girls?|boys?|others?)", value)
            if match:
                count = int(match.group(1))
                group = match.group(2).rstrip("s")
                exact_by_group[group].add(count)
                continue
            match = re.fullmatch(r"(\d+)\+(girls?|boys?|others?)", value)
            if match:
                group = match.group(2).rstrip("s")
                lower_by_group[group] = max(
                    lower_by_group.get(group, 0), int(match.group(1))
                )
        if any(len(counts) > 1 for counts in exact_by_group.values()):
            return None
        exact_values = {
            group: next(iter(counts))
            for group, counts in exact_by_group.items()
            if counts
        }
        if exact_values:
            if any(group in lower_by_group for group in exact_values):
                return None
            exact_total = sum(exact_values.values())
            if lower_by_group:
                return exact_total + sum(lower_by_group.values()), None
            return exact_total, exact_total
        if lower_by_group:
            return sum(lower_by_group.values()), None
        if "solo" in values:
            return 1, 1
        if values.intersection(
            {"multiplecharacters", "multiplegirls", "multipleboys", "multipleothers", "group"}
        ):
            return 2, None
        return None

    @staticmethod
    def _owner_evidence(record: ImageRecord, found: list[str]):
        keys = {normalize_tag(value) for value in found}
        owners: dict[str, set[tuple[str, str]]] = {key: set() for key in keys}
        unowned = {
            normalize_tag(value)
            for value in ConflictRuleEngine._unowned_tags(record)
            if normalize_tag(value) in keys
        }
        for character in record.annotation.characters:
            token = ("character", str(character.id or character.slot))
            for value in character.all_tags():
                if (key := normalize_tag(value)) in owners:
                    owners[key].add(token)
        for item in record.annotation.objects:
            token = ("object", str(item.id or item.slot))
            for value in item.all_tags():
                if (key := normalize_tag(value)) in owners:
                    owners[key].add(token)
        for context in record.annotation.contexts:
            token = (
                str(context.owner_kind or "context"),
                str(context.owner_id or context.id),
            )
            for value in context.all_tags():
                if (key := normalize_tag(value)) in owners:
                    owners[key].add(token)
        return owners, unowned

    @classmethod
    def _confirmed_owner_separation(
        cls, record: ImageRecord, found: list[str]
    ) -> bool:
        owners, unowned = cls._owner_evidence(record, found)
        if unowned or any(not values for values in owners.values()):
            return False
        owner_sets = list(owners.values())
        return all(
            first.isdisjoint(second)
            for index, first in enumerate(owner_sets)
            for second in owner_sets[index + 1 :]
        )

    def _allowance_decision(
        self,
        rule: ConflictRule,
        record: ImageRecord,
        scope_tags: list[str],
        found: list[str],
        *,
        ownership_unknown: bool = False,
    ) -> tuple[bool, bool]:
        policy = rule.allow_when
        if not policy.conditions:
            return False, False
        present_ids = self._resolved_ids(scope_tags)
        truths: dict[str, bool] = {}
        for condition in policy.conditions:
            if condition.kind == REVIEWED_EXCEPTION_TAGS:
                matches = [tag_id in present_ids for tag_id in condition.tag_ids]
                truths[condition.kind] = (
                    all(matches) if condition.tag_logic == ALLOW_ALL else any(matches)
                )
            elif condition.kind == CONFIRMED_OWNER_SEPARATION:
                truths[condition.kind] = self._confirmed_owner_separation(record, found)
            elif condition.kind == SUBJECT_COUNT:
                count_range = self._subject_count_range(record)
                if count_range is None:
                    truths[condition.kind] = False
                    continue
                low, high = count_range
                minimum = condition.minimum_subjects
                maximum = condition.maximum_subjects
                truths[condition.kind] = (
                    (minimum is None or low >= minimum)
                    and (
                        maximum is None
                        or (high is not None and high <= maximum)
                    )
                )
        results = [truths.get(condition.kind, False) for condition in policy.conditions]
        allowed = all(results) if policy.logic == ALLOW_ALL else any(results)
        if not allowed:
            return False, False
        # A multi-subject count does not prove that unowned incompatible facts
        # belong to different subjects or objects. Keep that state reviewable
        # unless an exception Tag or confirmed owner separation also applies.
        if ownership_unknown and rule.scope in {"same_character", "same_object"}:
            decisive = truths.get(REVIEWED_EXCEPTION_TAGS, False) or truths.get(
                CONFIRMED_OWNER_SEPARATION, False
            )
            if not decisive and truths.get(SUBJECT_COUNT, False):
                return False, True
        return True, False

    def ownership_acknowledgement(self, record: ImageRecord, match: ConflictMatch) -> dict:
        """Review evidence for one ambiguous pair; never a rule-wide exception."""
        if not match.ambiguous or len(match.found_tags)!=2:
            raise ValueError("Only an unresolved ownership check for exactly two Tags can be acknowledged.")
        values=sorted(normalize_tag(value) for value in match.found_tags)
        owners,unowned=self._owner_evidence(record,match.found_tags)
        evaluator=self.relationship_evaluator
        identities=[(value,record.annotation.tag_identities.get(value) or (
            evaluator.registry.resolve_tag_id(value) if evaluator is not None else "")) for value in values]
        try:
            stat=record.image_path.stat();image_version=(stat.st_size,stat.st_mtime_ns)
        except OSError as error:
            raise ValueError("Image file version is unavailable; review cannot be verified.") from error
        payload={"image_version":image_version,"rule":asdict(match.rule),"tags":identities,
                 "owners":{value:sorted(owners[value]) for value in values},
                 "unowned":sorted(unowned),"subject_count":self._subject_count_range(record)}
        signature=hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        return {"version":1,"rule_id":match.rule.id,"pair":values,"signature":signature,
                "decision":"different_subjects"}

    def revalidated_ownership_acknowledgements(self,record: ImageRecord):
        """Keep review provenance; stale known decisions cannot revive after a save."""
        raw=record.annotation.source.get("ownership_acknowledgements",[])
        if not isinstance(raw,list):return deepcopy(raw)
        if not raw:return []
        current=[]
        for match in self.evaluate(record,include_accepted=True):
            if match.ambiguous and len(match.found_tags)==2:
                try:current.append(self.ownership_acknowledgement(record,match))
                except ValueError:pass
        result=[]
        for item in record.annotation.source.get("ownership_acknowledgements",[]):
            if isinstance(item,dict) and item.get("version")==1 and item.get("decision")=="different_subjects" and item not in current:
                item={**item,"status":"needs_review"}
            result.append(item)
        return result

    def ownership_acknowledged(self,record: ImageRecord,match: ConflictMatch) -> bool:
        if not match.ambiguous or len(match.found_tags)!=2:return False
        saved=record.annotation.source.get("ownership_acknowledgements",[])
        if not isinstance(saved,list) or not saved:return False
        pair=sorted(normalize_tag(value) for value in match.found_tags)
        candidates=[item for item in saved if isinstance(item,dict) and item.get("version")==1
                    and item.get("rule_id")==match.rule.id and item.get("pair")==pair
                    and item.get("decision")=="different_subjects" and not item.get("status")]
        if not candidates:return False
        try:expected=self.ownership_acknowledgement(record,match)
        except ValueError:return False
        return any(item==expected for item in candidates)

    def evaluate(
        self,
        image: str | Path | ImageRecord,
        *,
        include_accepted: bool = False,
        rule_ids: Iterable[str] | None = None,
    ) -> list[ConflictMatch]:
        record=image if isinstance(image,ImageRecord) else ImageRecord(image)
        all_tags=concrete_sdxl_tags(
            record.annotation.all_structured_tags(include_contexts=False)
        ); matches=[]
        exceptions=self._exception_ids(record)
        requested=(
            {str(value) for value in rule_ids if str(value)}
            if rule_ids is not None else None
        )
        rules=(
            self.rules
            if requested is None else
            [rule for rule in self.rules if requested.intersection(rule.accepted_ids)]
        )

        resolved_ids: dict[str, str | None] = {}

        def found_tags(tags, rule):
            return self.found_tags(tags, rule, _resolved_ids=resolved_ids)

        def consider(rule, scope_tags, found, *, ownership_unknown=False, **fields):
            if len(found) <= 1:
                return False
            allowed, policy_ambiguous = self._allowance_decision(
                rule, record, scope_tags, found,
                ownership_unknown=ownership_unknown,
            )
            if allowed:
                return False
            fields["ambiguous"] = bool(fields.get("ambiguous") or policy_ambiguous)
            match=ConflictMatch(rule, found, **fields)
            if not include_accepted and self.ownership_acknowledged(record,match):return False
            matches.append(match)
            return True

        for rule in rules:
            if not include_accepted and exceptions.intersection(rule.accepted_ids):
                continue
            if rule.scope=="same_character":
                slot_matches=[]; owners={}
                for character in record.annotation.characters:
                    for tag in character.all_tags(): owners.setdefault(normalize_tag(tag),set()).add(character.slot)
                    found=found_tags(character.all_tags(),rule)
                    if len(found)>1:
                        slot_matches.append(character.slot)
                        consider(
                            rule,character.all_tags(),found,
                            character_slot=character.slot,
                            owner_kind="character",owner_id=character.id,
                            owner_label=f"character {character.slot}",
                        )
                if not slot_matches:
                    found=found_tags(all_tags,rule)
                    found_keys = {normalize_tag(value) for value in found}
                    unowned = [
                        value
                        for value in self._unowned_tags(record)
                        if normalize_tag(value) in found_keys
                    ]
                    unresolved=[tag for tag in unowned if not owners.get(normalize_tag(tag))]
                    if unresolved and len(found)>1:
                        ambiguous=not self._single_subject(
                            all_tags, rule.subject_matching
                        )
                        consider(
                            rule,all_tags,found,
                            ownership_unknown=ambiguous,
                            ambiguous=ambiguous,
                        )
                continue
            if rule.scope=="same_object":
                object_matches=[]; owners={}
                for item in record.annotation.objects:
                    for tag in item.all_tags():
                        owners.setdefault(normalize_tag(tag),set()).add(item.id)
                    found=found_tags(item.all_tags(),rule)
                    if len(found)>1:
                        object_matches.append(item.id)
                        consider(
                            rule,item.all_tags(),found,
                            object_slot=item.slot,
                            owner_kind="object",owner_id=item.id,
                            owner_label=item.identity_tag or f"object {item.slot}",
                        )
                grouped_contexts={}
                for context in record.annotation.contexts:
                    if context.owner_kind not in {"object","assignment","outfit"}:
                        continue
                    key=(context.owner_kind,context.owner_id or context.id)
                    grouped_contexts.setdefault(key,[]).extend(context.all_tags())
                for (owner_kind,owner_id),values in grouped_contexts.items():
                    found=found_tags(values,rule)
                    if len(found)>1:
                        object_matches.append(owner_id)
                        consider(
                            rule,values,found,owner_kind=owner_kind,
                            owner_id=owner_id,owner_label=owner_kind.replace("_"," "),
                        )
                if not object_matches:
                    found=found_tags(all_tags,rule)
                    found_keys={normalize_tag(value) for value in found}
                    unresolved=[
                        value for value in self._unowned_tags(record)
                        if normalize_tag(value) in found_keys
                        and not owners.get(normalize_tag(value))
                    ]
                    if unresolved and len(found)>1:
                        consider(
                            rule,all_tags,found,
                            ownership_unknown=True,ambiguous=True,
                        )
                continue
            if rule.scope=="same_context":
                for context in record.annotation.contexts:
                    found=found_tags(context.all_tags(),rule)
                    consider(
                        rule,context.all_tags(),found,
                        context_id=context.id,context_label=context.display_label,
                        owner_kind=context.owner_kind,owner_id=context.owner_id,
                        owner_label=context.owner_label,
                    )
                continue
            found=found_tags(all_tags,rule)
            consider(rule,all_tags,found)
            for context in record.annotation.contexts:
                found=found_tags(context.all_tags(),rule)
                consider(
                    rule,context.all_tags(),found,context_id=context.id,
                    context_label=context.display_label,
                    owner_kind=context.owner_kind,owner_id=context.owner_id,
                    owner_label=context.owner_label,
                )
        return matches
