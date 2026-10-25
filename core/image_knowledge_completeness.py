from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .ai_interrogation import split_plain_language
from .automation_repository import active_automation_engine,_active_automation_evaluation
from .confirmed_tag_identity import ConfirmedTagIdentityService
from .image_record import ImageRecord
from .image_fact_decisions import (
    ImageFindingDecisionService,image_tag_identity,
    unavailable_confirmed_tag_identities,
)
from .settings import normalize_plain_language_exclusion
from .tag_collection import TagCollection, normalize_tag
from .typed_boundary import audit_typed_boundary


@dataclass(frozen=True)
class CompletenessBaseline:
    case_type: str
    useful_tag_target: int
    useful_sentence_target: int
    rationale: str


REPRESENTATIVE_BASELINES = {
    "simple": CompletenessBaseline(
        "simple", 8, 2, "A single uncomplicated subject should not need filler to look complete."
    ),
    "product": CompletenessBaseline(
        "product", 10, 2, "Object identity, material, view, setting, and condition usually carry the useful signal."
    ),
    "landscape": CompletenessBaseline(
        "landscape", 12, 3, "Scene, weather, time, lighting, composition, and style need moderate coverage."
    ),
    "character": CompletenessBaseline(
        "character", 15, 4, "Identity, traits, clothing, pose, action, scene, and style justify the proposed baseline."
    ),
    "multi_subject": CompletenessBaseline(
        "multi_subject", 20, 4, "Multiple subjects need ownership and interaction detail beyond the general baseline."
    ),
    "complex": CompletenessBaseline(
        "complex", 20, 5, "Dense scenes need more relationships and description units, not repeated synonyms."
    ),
    "general": CompletenessBaseline(
        "general", 15, 4, "The proposed 15/4 values remain a useful general reference, not a universal pass/fail gate."
    ),
}

_DIVERSITY_GUIDANCE = {
    "simple": "Review the most relevant subject, action, setting, composition, or other visibly supported groups.",
    "product": "Review object identity, material, view, setting, condition, and composition where they are visible.",
    "landscape": "Review setting, weather, time, lighting, depth, and composition where they are visible.",
    "character": "Review identity, traits, clothing, action, scene, and composition where they are visible.",
    "multi_subject": "Review identities, traits, ownership, interaction, scene, and composition where they are visible.",
    "complex": "Review subjects, relationships, action, scene, lighting, and composition where they are visible.",
    "general": "Review enough relevant identity, trait, action, scene, composition, or optional style groups for this image.",
}


@dataclass(frozen=True)
class CompletenessIssue:
    code: str
    label: str
    severity: str
    deduction: float
    suggested_action: str
    component: str


@dataclass
class CompletenessComponent:
    name: str
    score: float
    maximum: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class ImageKnowledgeCompleteness:
    image: Path
    score: int
    case_type: str
    useful_tags: int
    useful_sentences: int
    tag_target: int
    sentence_target: int
    components: list[CompletenessComponent] = field(default_factory=list)
    issues: list[CompletenessIssue] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.useful_tags > 0 and self.useful_sentences > 0 and self.score >= 70 and not any(
            issue.severity == "high" for issue in self.issues
        )

    @property
    def quickest_action(self) -> str:
        if not self.issues:
            return "No completeness repair is needed."
        return sorted(
            self.issues, key=lambda item: (-item.deduction, item.code)
        )[0].suggested_action


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_WORDS = re.compile(r"[a-z0-9']+", re.IGNORECASE)
_LOW_INFORMATION = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "in", "is", "it", "of", "on", "or", "that", "the", "this", "to", "with",
    "image", "picture", "photo", "shown", "depicts", "detailed",
}


def _useful_sentences(values: Iterable[str]) -> list[str]:
    useful: list[str] = []
    seen: set[str] = set()
    for value in values:
        for sentence in _SENTENCE_SPLIT.split(str(value).strip()):
            sentence = sentence.strip()
            words = [word.casefold() for word in _WORDS.findall(sentence)]
            information = {word for word in words if word not in _LOW_INFORMATION}
            key = " ".join(words)
            if len(words) < 4 or len(information) < 3 or key in seen:
                continue
            seen.add(key)
            useful.append(sentence)
    return useful


class ImageKnowledgeCompletenessService:
    """Score authoritative Image Knowledge, never optional output health."""

    def __init__(
        self, *, metadata=None, knowledge=None, ignored_plain_language=(),
    ) -> None:
        # ``metadata`` remains accepted for compatibility with callers written
        # before IMGK-05 split optional output health from completeness.
        self.knowledge = knowledge
        self.ignored_plain_language = {
            normalize_plain_language_exclusion(value)
            for value in ignored_plain_language
            if normalize_plain_language_exclusion(value)
        }

    @staticmethod
    def baseline_for(case_type: str) -> CompletenessBaseline:
        return REPRESENTATIVE_BASELINES.get(case_type, REPRESENTATIVE_BASELINES["general"])

    def assess(
        self,
        image: str | Path,
        *,
        case_type: str | None = None,
        include_metadata: bool = True,
    ) -> ImageKnowledgeCompleteness:
        return self.assess_record(
            ImageRecord(Path(image)),
            case_type=case_type,
            include_metadata=include_metadata,
        )

    def assess_record(
        self,
        record: ImageRecord,
        *,
        case_type: str | None = None,
        include_metadata: bool = True,
        candidate_findings: list | None = None,
    ) -> ImageKnowledgeCompleteness:
        """Assess an already loaded record without reopening its sidecar."""
        # ``include_metadata`` is intentionally ignored. Missing or unavailable
        # optional outputs must not make the authoritative record incomplete.
        path = record.image_path
        present = TagCollection(record.annotation.all_structured_tags()).to_list()
        absent = TagCollection(record.annotation.confirmed_absent).to_list()
        absent_keys = TagCollection(absent).normalized_set()
        contradictions = [tag for tag in present if normalize_tag(tag) in absent_keys]
        useful_tag_keys: set[str] = set()
        for tag in present:
            known = self.knowledge.find(tag) if self.knowledge is not None else None
            useful_tag_keys.add(str(known.id) if known is not None else normalize_tag(tag))
        useful_tag_keys.discard("")
        sentences = _useful_sentences(record.annotation.plain_language)
        selected_case = case_type or self._detect_case(record, present)
        baseline = self.baseline_for(selected_case)
        issues: list[CompletenessIssue] = []

        def issue(code: str, label: str, severity: str, deduction: float, action: str, component: str) -> None:
            issues.append(CompletenessIssue(code, label, severity, deduction, action, component))

        # Raw ambiguous or malformed observations remain inspectable evidence,
        # but they are deliberately not actionable Image Knowledge. Only a
        # suspicious value already confirmed into Image Knowledge can lower
        # completeness or be corrected by the editor.
        boundary_issues = [
            item for item in audit_typed_boundary(record, self.knowledge)
            if item.code.startswith("confirmed_")
        ]
        for item in boundary_issues:
            kind = "Tag" if item.kind == "tag" else "Description"
            destination = "Image Knowledge" if item.kind == "tag" else "Descriptions"
            issue(
                f"typed_{item.kind}_boundary_review",
                f"Confirmed {kind} needs review: {item.value}. {item.message}",
                "medium", 0,
                f"Open Edit Image Data and choose Review Value for the flagged {kind} in {destination}. No value was changed automatically.",
                "review_resolution",
            )

        authority = 0.0
        authority_reasons: list[str] = []
        if record.sidecar_path.exists():
            authority += 12
        else:
            issue("missing_record", "No authoritative .tags.json record", "high", 12,
                  "Prepare and review the JSON record before projecting it.", "authoritative_record")
        tag_score = 16 * min(1.0, len(useful_tag_keys) / max(1, baseline.useful_tag_target))
        authority += tag_score
        if not useful_tag_keys:
            issue("missing_tags", "No confirmed-present SDXL tags", "high", 16,
                  "Add or accept tags that are visibly supported by the image.", "authoritative_record")
        elif len(useful_tag_keys) < baseline.useful_tag_target:
            issue("thin_tag_coverage", f"{len(useful_tag_keys)} useful tags; contextual target {baseline.useful_tag_target}",
                  "low", 16 - tag_score, "Add missing concepts; do not pad with aliases or duplicates.",
                  "authoritative_record")
        diversity = self._semantic_diversity(record)
        diversity_score = 6 * min(1.0, diversity / 5)
        authority += diversity_score
        if diversity < 5 and useful_tag_keys:
            issue("low_tag_diversity", "Confirmed tags do not yet cover five semantic groups", "low",
                  6 - diversity_score, _DIVERSITY_GUIDANCE.get(selected_case,_DIVERSITY_GUIDANCE["general"]),
                  "authoritative_record")
        sentence_score = 6 * min(1.0, len(sentences) / max(1, baseline.useful_sentence_target))
        authority += sentence_score
        if not sentences:
            issue("missing_description", "No useful plain-language sentence", "medium", 6,
                  "Write a concrete description of visible subjects, actions, and relationships.",
                  "authoritative_record")
        elif len(sentences) < baseline.useful_sentence_target:
            issue("thin_description", f"{len(sentences)} useful sentences; contextual target {baseline.useful_sentence_target}",
                  "low", 6 - sentence_score, "Add distinct facts or relationships; do not split or repeat filler prose.",
                  "authoritative_record")
        authority_reasons.extend([
            f"{len(useful_tag_keys)} canonical, nonduplicate confirmed tags",
            f"{diversity} represented semantic groups",
            f"{len(sentences)} useful, nonduplicate sentences",
        ])

        if contradictions:
            issue("tag_contradiction", "Tags are both confirmed present and confirmed absent", "high", 8,
                  "Resolve each contradictory assertion in the JSON record.", "authoritative_record")
            authority = max(0.0, authority - 8)

        review = 0.0
        review_reasons: list[str] = []
        # Human review is an assurance/provenance claim, not a completeness
        # requirement for routine local organization. Readiness below is still
        # blocked by unresolved evidence, structural gaps, and diagnostic
        # conflicts. External sharing keeps its stricter exact ``reviewed``
        # gate and therefore cannot mistake automation-ready for human-reviewed.
        review += 5
        if record.annotation.review_status == "reviewed":
            review_reasons.append("Human review recorded")
        else:
            review_reasons.append(
                "Human review not recorded; local readiness uses current "
                "finding, structure, and conflict checks"
            )
        unresolved_ai, unresolved_sources = self.unresolved_findings(
            record, candidate_findings=candidate_findings,
        )
        unresolved = unresolved_ai + unresolved_sources
        if unresolved:
            issue("unresolved_evidence", f"{unresolved} source or AI observations still need decisions", "low", 4,
                  "Accept, reject, dismiss, or defer each observation without treating its confidence as completeness.",
                  "review_resolution")
        else:
            review += 4
        structure_score = 6.0
        normalized_present = {normalize_tag(tag) for tag in present}
        if not normalized_present.intersection({"general", "sensitive", "questionable", "explicit"}):
            structure_score -= 2
            issue("missing_rating", "No explicit image rating assertion", "low", 2,
                  "Confirm the image rating independently from semantic categories.", "review_resolution")
        style=record.annotation.visual_style
        if (style.relationship in {"modified","cross_source","mixed"}
                and not style.source_series_ids and not style.style_tags and not style.notes.strip()):
            structure_score -= 2
            issue("incomplete_style_relationship","The asserted style relationship lacks its supporting source or style context","medium",2,
                  "Identify the relevant source/style context, add a short note, or return the relationship to unknown if it is not applicable.","review_resolution")
        # Anonymous subjects still own valid appearance/clothing facts. A
        # public identity or legacy role is optional, including after a user
        # deliberately clears a false character identification.
        review += max(0.0, structure_score)
        review_reasons.extend([
            f"{unresolved} unresolved source or AI observations (evidence confidence is scored elsewhere)",
            f"{len(absent)} explicit confirmed-absent assertions",
        ])

        authority_scale = 70 / 40
        review_scale = 30 / 15
        components = [
            CompletenessComponent(
                "authoritative_record", authority * authority_scale, 70, authority_reasons
            ),
            CompletenessComponent(
                "review_resolution", review * review_scale, 30, review_reasons
            ),
        ]
        issues = [
            CompletenessIssue(
                item.code,
                item.label,
                item.severity,
                item.deduction * (
                    authority_scale
                    if item.component == "authoritative_record"
                    else review_scale
                ),
                item.suggested_action,
                item.component,
            )
            for item in issues
        ]
        total = round(sum(component.score for component in components))
        return ImageKnowledgeCompleteness(
            path, max(0, min(100, total)), baseline.case_type,
            len(useful_tag_keys), len(sentences), baseline.useful_tag_target,
            baseline.useful_sentence_target, components, issues,
        )

    def unresolved_findings(
        self, record: ImageRecord, *, candidate_findings: list | None = None,
        by_kind: dict[str, int] | None = None,
    ) -> tuple[int, int]:
        """Shared AI/source counts for completeness and workflow review state."""
        local_counts={} if by_kind is not None else None
        with _active_automation_evaluation(self.knowledge) as automation:
            decisions = ImageFindingDecisionService(record.annotation, knowledge=self.knowledge)
            confirmed_tags = self._confirmed_tag_identities(record)
            counts=(
                self._unresolved_ai_findings(record, decisions, confirmed_tags=confirmed_tags,
                    by_kind=local_counts,automation=automation),
                self._unresolved_source_findings(record,candidate_findings=candidate_findings,
                    confirmed_tags=confirmed_tags,by_kind=local_counts,automation=automation),
            )
        if by_kind is not None:
            for kind,count in local_counts.items():
                by_kind[kind]=by_kind.get(kind,0)+count
        return counts

    def _confirmed_tag_identities(self, record: ImageRecord) -> set[str]:
        values = record.annotation.all_structured_tags()
        if self.knowledge is None:
            return {image_tag_identity(value) for value in values} - {""}
        plan = ConfirmedTagIdentityService.shared(self.knowledge).plan(
            values, stored_ids=record.annotation.tag_identities, confirmation=False,
        )
        # Stored identity takes precedence if an old display name has since
        # been reused by a different Tag; it must not confirm both identities.
        return {
            f"id:{entry.stable_id}" if entry.stable_id and entry.canonical_display
            else f"name:{entry.normalized_value}"
            for entry in plan.entries if entry.normalized_value
        }

    def _unresolved_source_findings(
        self, record: ImageRecord, *, candidate_findings: list | None = None,
        confirmed_tags: set[str] | None = None,
        by_kind: dict[str, int] | None = None,
        automation=None,
    ) -> int:
        """Count actionable cached source findings without changing evidence."""
        if confirmed_tags is None:
            confirmed_tags = self._confirmed_tag_identities(record)
        rejected_tags = unavailable_confirmed_tag_identities(
            record.annotation,self.knowledge,
        )
        confirmed_prose = {
            normalize_plain_language_exclusion(sentence)
            for value in record.annotation.plain_language
            for sentence in split_plain_language(value)
            if normalize_plain_language_exclusion(sentence)
        }
        decisions=ImageFindingDecisionService(
            record.annotation,knowledge=self.knowledge,
        )
        rejected_prose=(
            decisions.ignored_description_keys()
            | self.ignored_plain_language
        )
        if automation is None:
            automation = active_automation_engine(self.knowledge)

        count = 0
        findings = (candidate_findings if isinstance(candidate_findings, list)
                    else record.annotation.source.get("candidate_findings", []))
        for item in findings if isinstance(findings, list) else ():
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "")
            value = str(item.get("value") or "")
            raw_sources = (
                item.get("sources", [])
                if isinstance(item.get("sources"), list)
                else ()
            )
            sources = {str(source).strip() for source in raw_sources if str(source).strip()} or {"Unknown"}
            fingerprints = item.get("source_fingerprints", {})
            fingerprints = fingerprints if isinstance(fingerprints, dict) else {}
            if kind == "tag":
                key = image_tag_identity(value,self.knowledge)
                if (
                    key
                    and key not in confirmed_tags | rejected_tags
                    and not automation.is_evidence_rejected(value)
                    and any(not decisions.source_is_deferred(kind, value, source) for source in sources)
                ):
                    count += 1
                    if by_kind is not None:
                        by_kind["tag"] = by_kind.get("tag", 0) + 1
            elif kind == "description":
                for sentence in split_plain_language(value):
                    key = normalize_plain_language_exclusion(sentence)
                    if (
                        key
                        and key not in confirmed_prose | rejected_prose
                        and any(
                            not decisions.source_is_deferred(kind, sentence, source)
                            and not decisions.source_was_used_as_edited(
                                kind, sentence, source,
                                source_fingerprint=str(fingerprints.get(source) or ""),
                            )
                            for source in sources
                        )
                    ):
                        count += 1
                        if by_kind is not None:
                            by_kind["description"] = by_kind.get("description", 0) + 1
            # Ambiguous and unsupported historical values remain raw evidence.
            # The editor cannot decide them as Tags or Descriptions, so they
            # must not become an actionable completeness count.
        return count

    def _unresolved_ai_findings(
        self,record: ImageRecord,decisions: ImageFindingDecisionService,
        *, confirmed_tags: set[str] | None = None,
        by_kind: dict[str, int] | None = None,
        automation=None,
    ) -> int:
        if confirmed_tags is None:
            confirmed_tags = self._confirmed_tag_identities(record)
        unavailable_tags = unavailable_confirmed_tag_identities(
            record.annotation,self.knowledge,
        )
        confirmed_prose = {
            normalize_plain_language_exclusion(sentence)
            for value in record.annotation.plain_language
            for sentence in split_plain_language(value)
            if normalize_plain_language_exclusion(sentence)
        }
        unavailable_prose=(
            decisions.ignored_description_keys()
            | self.ignored_plain_language
        )
        if automation is None:
            automation=active_automation_engine(self.knowledge)
        count=0
        for evidence in record.annotation.ai_evidence:
            source=evidence.model_id or "Unknown model"
            if evidence.kind=="sdxl_tags":
                for item in evidence.typed_tags():
                    value=str(item.get("tag") or "")
                    identity=image_tag_identity(value,self.knowledge)
                    decision=evidence.decisions.get(normalize_tag(value),"")
                    if (
                        identity
                        and identity not in confirmed_tags|unavailable_tags
                        and decision not in {
                            "accepted","rejected",*decisions.DEFERRED_VALUES,
                        }
                        and not automation.is_evidence_rejected(value)
                        and not decisions.source_is_deferred(
                            "tag",value,source,evidence_id=evidence.id,
                        )
                    ):
                        count+=1
                        if by_kind is not None:
                            by_kind["tag"] = by_kind.get("tag", 0) + 1
            elif evidence.kind=="plain_language" and evidence.decisions.get(
                "plain_language","",
            ) not in {"accepted","rejected",*decisions.DEFERRED_VALUES}:
                for sentence in split_plain_language(
                    evidence.typed_plain_language()
                ):
                    key=normalize_plain_language_exclusion(sentence)
                    if (
                        key
                        and key not in confirmed_prose|unavailable_prose
                        and not decisions.source_is_deferred(
                            "description",sentence,source,
                            evidence_id=evidence.id,
                        )
                        and not decisions.source_was_used_as_edited(
                            "description",sentence,source,
                            evidence_id=evidence.id,
                        )
                    ):
                        count+=1
                        if by_kind is not None:
                            by_kind["description"] = by_kind.get("description", 0) + 1
        return count

    @staticmethod
    def _detect_case(record: ImageRecord, present: list[str]) -> str:
        normalized = {normalize_tag(tag) for tag in present}
        if len(record.annotation.characters) >= 2 or normalized.intersection(
            {"2girls", "2boys", "multiple girls", "multiple boys", "multiple subjects"}
        ):
            return "multi_subject"
        if record.annotation.characters:
            return "character"
        if normalized.intersection({"product", "product photography", "object focus", "still life"}):
            return "product"
        if normalized.intersection({"landscape", "scenery", "cityscape", "seascape", "mountain"}):
            return "landscape"
        if normalized.intersection({
            "complex background", "busy background", "complex scene",
            "crowd", "many objects",
        }):
            return "complex"
        if normalized.intersection({
            "simple background", "plain background", "solid background",
            "white background", "minimalism", "minimalist",
        }):
            return "simple"
        return "general"

    def _semantic_diversity(self,record: ImageRecord) -> int:
        groups = set()
        for prefix, values in (("global", record.annotation.global_tags), ("scene", record.annotation.scene)):
            groups.update(f"{prefix}:{name}" for name, tags in values.items() if tags)
        if any(character.character_id or character.role for character in record.annotation.characters):
            groups.add("identity")
        if any(character.outfit_ids for character in record.annotation.characters):
            groups.add("outfit")
        if any(character.all_tags() for character in record.annotation.characters):
            groups.add("character_traits")
        if record.annotation.visual_style.style_tags:
            groups.add("style")
        groups.update(
            f"custom:{name}" for name,tags in record.annotation.custom_groups.items()
            if tags
        )
        flat_values=[
            *record.annotation.sdxl_tags,*record.annotation.uncategorized,
        ]
        categorized=False
        for value in flat_values:
            known=self.knowledge.find(value) if self.knowledge is not None else None
            category=normalize_tag(getattr(known,"category","") or "")
            if category:
                groups.add(f"knowledge:{category}")
                categorized=True
        if flat_values and not categorized:
            groups.add("flat_confirmed")
        return len(groups)
