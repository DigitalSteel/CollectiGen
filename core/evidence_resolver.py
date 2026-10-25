from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .folder_audit import folder_context
from .image_record import ImageRecord
from .source_inspection import ImageEvidenceInspector
from .tag_collection import normalize_tag


@dataclass(frozen=True)
class EvidenceCandidate:
    field: str
    value: str
    source: str
    confidence: float
    confirmed: bool = False
    explanation: str = ""


@dataclass
class ResolvedAssertion:
    field: str
    value: str | None
    confidence: float
    evidence: list[EvidenceCandidate] = field(default_factory=list)
    alternatives: dict[str, float] = field(default_factory=dict)
    conflict: bool = False

    @property
    def recommendation(self) -> str:
        if self.conflict:
            return "review_conflict"
        if self.value is None:
            return "ask"
        if any(item.confirmed for item in self.evidence if item.value == self.value):
            return "trusted"
        if self.confidence >= 0.92:
            return "confirm_group"
        if self.confidence >= 0.70:
            return "review"
        return "ask"


class EvidenceResolver:
    """Combine independent observations without mistaking absence for evidence."""

    RATING_VALUES = {"general", "sensitive", "questionable", "explicit"}
    STYLE_VALUES = {"photorealistic", "realistic", "semi-realistic"}
    SOURCE_WEIGHTS = {
        "Structured JSON": 1.0,
        "TXT caption": 0.82,
        "Image metadata": 0.78,
        "Folder placement": 0.55,
        "AI model": 0.65,
    }

    def collect(
        self,
        image: Path,
        dataset_root: Path | None = None,
        *,
        include_metadata: bool = True,
    ) -> list[EvidenceCandidate]:
        record = ImageRecord(image)
        candidates: list[EvidenceCandidate] = []

        for tag in record.annotation.sdxl_tags + record.annotation.uncategorized:
            self._tag_candidates(candidates, tag, "Structured JSON", 1.0, True)
        for style in record.annotation.visual_style.style_tags:
            value = normalize_tag(style)
            if value in self.STYLE_VALUES:
                candidates.append(EvidenceCandidate("visual_style", value, "Structured JSON", 1.0, True, "Confirmed structured style tag."))

        report = ImageEvidenceInspector().inspect(image, include_metadata=include_metadata)
        for source in report.sources:
            if source.name == "JSON sidecar":
                continue
            weight = self.SOURCE_WEIGHTS.get(source.name, 0.60)
            for tag in source.tags:
                self._tag_candidates(candidates, tag, source.name, weight, False)

        if dataset_root:
            context = folder_context(image, dataset_root)
            if context.rating:
                candidates.append(EvidenceCandidate("rating", context.rating, "Folder placement", 0.55, False, "Current folder implies this rating."))
            if context.style:
                candidates.append(EvidenceCandidate("visual_style", context.style, "Folder placement", 0.55, False, "Current folder implies this visual style."))

        for evidence in record.annotation.ai_evidence:
            for item in evidence.typed_tags():
                tag = str(item.get("tag") or "")
                confidence = float(item.get("confidence", 0.0) or 0.0) * self.SOURCE_WEIGHTS["AI model"]
                self._tag_candidates(candidates, tag, f"AI model: {evidence.model_id}", confidence, False)
        return candidates

    def resolve(self, candidates: list[EvidenceCandidate], field_name: str) -> ResolvedAssertion:
        relevant = [item for item in candidates if item.field == field_name and item.value]
        if not relevant:
            return ResolvedAssertion(field_name, None, 0.0)
        scores: dict[str, float] = {}
        for item in relevant:
            # Independent agreement increases trust while remaining bounded.
            previous = scores.get(item.value, 0.0)
            scores[item.value] = 1.0 - ((1.0 - previous) * (1.0 - max(0.0, min(1.0, item.confidence))))
        ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        value, confidence = ordered[0]
        second = ordered[1][1] if len(ordered) > 1 else 0.0
        confirmed_values = {item.value for item in relevant if item.confirmed}
        conflict = len(confirmed_values) > 1 or (second >= 0.70 and confidence - second < 0.18)
        return ResolvedAssertion(field_name, value, confidence, relevant, dict(ordered[1:]), conflict)

    def analyze(
        self,
        image: Path,
        dataset_root: Path | None = None,
        *,
        include_metadata: bool = True,
    ) -> dict[str, ResolvedAssertion]:
        candidates = self.collect(image, dataset_root, include_metadata=include_metadata)
        return {
            "rating": self.resolve(candidates, "rating"),
            "visual_style": self.resolve(candidates, "visual_style"),
        }

    def _tag_candidates(self, output: list[EvidenceCandidate], tag: str, source: str, confidence: float, confirmed: bool) -> None:
        value = normalize_tag(tag)
        if value in self.RATING_VALUES:
            output.append(EvidenceCandidate("rating", value, source, confidence, confirmed, f'{source} contains "{tag}".'))
        if value in self.STYLE_VALUES:
            output.append(EvidenceCandidate("visual_style", value, source, confidence, confirmed, f'{source} contains "{tag}".'))
