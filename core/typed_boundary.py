from __future__ import annotations

from dataclasses import dataclass

from .automation_templates import has_automation_placeholder
from .caption_import import (
    looks_like_clear_prose, looks_like_clear_tag,
    looks_like_metadata_prose_fragment, metadata_sources_only,
)
from .image_fact_decisions import (
    image_tag_identity, unavailable_confirmed_tag_identities,
)
from .tag_collection import normalize_tag


IMAGE_CONTEXT_SOURCE_PREFIX = "Confirmed Image Knowledge / Context: "


@dataclass(frozen=True)
class TypedBoundaryIssue:
    code: str
    kind: str
    value: str
    message: str


def canonical_tag(value: str, knowledge=None) -> str:
    """Return the reviewed System Knowledge display for a tag name or alias."""
    clean = str(value or "").strip()
    if not clean or knowledge is None:
        return clean
    record = knowledge.find(clean)
    return record.display if record is not None else clean


def _is_recognized_title_identity(value: str, annotation, knowledge=None) -> bool:
    """Return whether System Knowledge resolves a character or source title."""
    if knowledge is None:
        return False

    stored_id = annotation.tag_identities.get(normalize_tag(value), "")
    if stored_id:
        resolved_id = (
            knowledge.resolve_tag_id(stored_id)
            if hasattr(knowledge, "resolve_tag_id")
            else stored_id
        )
        record = getattr(knowledge, "tags", {}).get(resolved_id or "")
    else:
        record = knowledge.find(value)
    if record is None:
        return False

    category = str(getattr(record, "category", "") or "")
    role = category
    categories = getattr(knowledge, "_categories", None)
    if categories is not None and hasattr(categories, "effective_semantic_role"):
        role = str(categories.effective_semantic_role(category) or category)
    return category in {"character_identity", "copyright_series"} or role in {
        "character_identity", "character_source",
    }


def typed_tag_sources(record, knowledge=None) -> dict[str, list[str]]:
    """Collect every current typed tag observation without promoting it.

    Confirmed Image Knowledge, imported TXT/Metadata candidates, and valid AI
    tag findings share this read boundary.  Rejected, ambiguous, stale, prose,
    and invalidly typed values are deliberately absent.  System Knowledge
    aliases resolve to their canonical display before downstream consumers see
    them.
    """
    observed: dict[str, tuple[str, list[str]]] = {}
    unavailable = unavailable_confirmed_tag_identities(
        record.annotation, knowledge,
    )

    def add(value: str, sources) -> None:
        if has_automation_placeholder(value):
            return
        if image_tag_identity(value, knowledge) in unavailable:
            return
        display = canonical_tag(value, knowledge)
        key = normalize_tag(display)
        if not key:
            return
        current_display, current_sources = observed.setdefault(key, (display, []))
        for source in sources if isinstance(sources, (list, tuple, set)) else (sources,):
            label = str(source or "").strip()
            if label and label not in current_sources:
                current_sources.append(label)
        observed[key] = (current_display, current_sources)

    for value in record.annotation.all_structured_tags(include_contexts=False):
        add(value, "Confirmed Image Knowledge")
    for context in record.annotation.contexts:
        for value in context.all_tags():
            add(
                value,
                (
                    "Confirmed Image Knowledge",
                    f"{IMAGE_CONTEXT_SOURCE_PREFIX}{context.id}",
                ),
            )

    findings = record.annotation.source.get("candidate_findings", [])
    for item in findings if isinstance(findings, list) else []:
        if not isinstance(item, dict) or item.get("kind") != "tag":
            continue
        value=item.get("value", ""); sources=item.get("sources") or ["Imported tag source"]
        # Earlier Metadata readers could split prose around quotes or commas
        # and cache the pieces as tag candidates. They remain raw evidence, but
        # cannot participate in tag discovery, rules, or recognition.
        if metadata_sources_only(sources) and looks_like_metadata_prose_fragment(value):
            continue
        add(value,sources)

    for evidence in record.annotation.ai_evidence:
        source = f"AI: {evidence.model_id}" if evidence.model_id else "AI findings"
        for prediction in evidence.typed_tags():
            if not isinstance(prediction, dict):
                continue
            value = str(prediction.get("tag") or "").strip()
            decision = evidence.decisions.get(normalize_tag(value), "")
            if decision in {"rejected", "ignored", "dismissed"}:
                continue
            add(value, source)

    return {display: sources for display, sources in observed.values()}


def audit_typed_boundary(record, knowledge=None) -> list[TypedBoundaryIssue]:
    """Report suspicious historical values without changing their confirmed type."""
    issues: list[TypedBoundaryIssue] = []
    known = (lambda value: knowledge.find(value) is not None) if knowledge is not None else None

    for value in record.annotation.all_structured_tags():
        if has_automation_placeholder(value):
            issues.append(TypedBoundaryIssue(
                "confirmed_unresolved_automation_template", "tag", str(value),
                "A confirmed SDXL Tag contains an unresolved Automation template. Review it explicitly; CollectiGen preserved Image Knowledge but excluded the value from Tag consumers and projections.",
            ))
            continue
        if looks_like_clear_prose(value) and not _is_recognized_title_identity(
            value, record.annotation, knowledge,
        ):
            issues.append(TypedBoundaryIssue(
                "confirmed_tag_looks_like_prose", "tag", str(value),
                "A confirmed SDXL Tag looks sentence-like. Review it explicitly; CollectiGen did not reclassify it.",
            ))
    for value in record.annotation.plain_language:
        if looks_like_clear_tag(value, known):
            issues.append(TypedBoundaryIssue(
                "confirmed_description_looks_like_tag", "description", str(value),
                "A confirmed Plain Language Description resembles a known or strongly marked tag. Review it explicitly; CollectiGen did not reclassify it.",
            ))

    findings = record.annotation.source.get("candidate_findings", [])
    for item in findings if isinstance(findings, list) else []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        value = str(item.get("value") or "")
        if kind == "tag" and has_automation_placeholder(value):
            issues.append(TypedBoundaryIssue(
                "candidate_unresolved_automation_template", "ambiguous", value,
                "Source evidence contains an unresolved Automation template. It remains evidence and cannot be confirmed as an SDXL Tag.",
            ))
        elif kind == "ambiguous":
            issues.append(TypedBoundaryIssue(
                "ambiguous_source_value", kind, value,
                "Source evidence does not establish whether this is an SDXL Tag or Plain Language Description.",
            ))
        elif kind not in {"tag", "description"}:
            issues.append(TypedBoundaryIssue(
                "invalid_candidate_kind", kind, value,
                "Historical candidate evidence has an unsupported kind and was preserved without promotion.",
            ))

    for evidence in record.annotation.ai_evidence:
        for prediction in evidence.typed_tags():
            value = str(prediction.get("tag") or "").strip()
            if has_automation_placeholder(value):
                issues.append(TypedBoundaryIssue(
                    "ai_unresolved_automation_template", "ambiguous", value,
                    "AI evidence contains an unresolved Automation template. The exact model evidence was preserved without promotion.",
                ))
        for message in evidence.type_issues():
            issues.append(TypedBoundaryIssue(
                "invalid_ai_evidence_type", evidence.kind, evidence.id, message,
            ))

    saved = record.annotation.source.get("evidence", [])
    for source in saved if isinstance(saved, list) else []:
        if not isinstance(source, dict):
            continue
        for message in source.get("type_issues", []) if isinstance(source.get("type_issues"), list) else []:
            issues.append(TypedBoundaryIssue(
                "metadata_type_conflict", "metadata", str(source.get("name") or "Metadata"), str(message),
            ))
    return issues
