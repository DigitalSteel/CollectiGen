"""Read-only explanations of an already frozen training caption.

No source reads, tokenizer estimates, quality scores or authority decisions.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Mapping

from .training_configuration import package_caption


TEXT_PAGE_SIZE = 4_096
DECISION_PAGE_SIZE = 50
_GOAL_PROMPTS = {
    "character": "Check that identity stays clear across outfits, poses and backgrounds. For multiple subjects, inspect each fact's owner.",
    "style": "Check that the intended style is represented across different subjects and settings, rather than only one repeated scene.",
    "product": "Check the product's identity and distinguishing details across views. Inspect whether background or branding is intentional.",
    "outfit": "Check the outfit's distinguishing details and the wearer of each fact across poses and characters.",
    "action": "Check who performs the action and how its important relationships are described across examples.",
    "concept": "Check what should stay consistent and what should vary across examples of this concept.",
}


@dataclass(frozen=True)
class TextPage:
    text: str
    page: int
    pages: int
    start: int
    end: int
    total: int


def text_page(text: str, page: int = 0) -> TextPage:
    """Slice by Unicode code point; joining every page reproduces exact text."""
    total = len(text)
    pages = max(1, (total + TEXT_PAGE_SIZE - 1) // TEXT_PAGE_SIZE)
    page = max(0, min(int(page), pages - 1))
    start = page * TEXT_PAGE_SIZE
    end = min(total, start + TEXT_PAGE_SIZE)
    return TextPage(text[start:end], page, pages, start, end, total)


@dataclass(frozen=True)
class CaptionReview:
    stable_id: str
    goal: str
    question: str
    profile: str
    target: str
    caption: str
    token_status: str
    context: str


def review_caption(plan, candidate) -> CaptionReview:
    """Consume only the caller's current candidate and profile snapshot."""
    configuration = plan.configuration
    profile = plan.profile
    family = configuration.target_model_family if configuration else plan.goal.target_model_family
    version = configuration.target_model_version if configuration else plan.goal.target_model_version
    base = configuration.base_model_id if configuration else plan.goal.base_model_id
    caption = package_caption(candidate.caption, configuration) if configuration else candidate.caption
    budget = configuration.max_caption_tokens if configuration else profile.token_budget
    actual_hash = hashlib.sha256(candidate.caption.encode("utf-8")).hexdigest()
    stored_hash = candidate.caption_sha256
    hash_status = "matches the displayed canonical text" if stored_hash == actual_hash else (
        "unavailable in this snapshot" if not stored_hash else "MISMATCH: differs from the displayed canonical text"
    )
    token_status = (
        f"Requested budget: {budget:,} model tokens. " if budget else "No model-token budget is declared. "
    ) + "Exact tokenizer measurement is unavailable; character counts do not measure tokens or quality."
    context = (
        f"Profile ID: {profile.profile_id}\nProfile fingerprint: {profile.fingerprint()}\n"
        f"Profile target: {profile.target_model_family or 'Unspecified'} "
        f"{profile.target_model_version or '(version unspecified)'}\n"
        f"Caption strategy: {profile.caption_strategy}\n"
        f"Base model: {base or 'Unspecified; compatibility has not been verified'}\n"
        f"Candidate: {candidate.stable_id}\n"
        f"Frozen source scope: {plan.source_label}\nScope fingerprint: {plan.scope_fingerprint}\n"
        f"Inspection: {'cancelled; only processed candidates are shown' if plan.cancelled else 'frozen candidate snapshot'}\n"
        f"Image Knowledge fingerprint: {candidate.knowledge_fingerprint or 'Unavailable'}\n"
        f"Stored canonical caption hash: {hash_status}\n"
        f"Current canonical caption SHA-256: {actual_hash}\n"
        f"Displayed package-caption SHA-256: {hashlib.sha256(caption.encode('utf-8')).hexdigest()}\n"
        f"Current role: {candidate.role}; decision: {candidate.decision}\n"
        "Goal prompts are manual review questions, not findings about this image. "
        "The manual adapter has no verified tokenizer, trained-model evaluation or caption-rotation support. "
        "Review Dataset Evidence for existing scope-wide observations."
    )
    return CaptionReview(
        candidate.stable_id, plan.goal.text,
        _GOAL_PROMPTS.get(plan.goal.kind, _GOAL_PROMPTS["concept"]),
        f"{profile.name} · v{profile.version}",
        f"{family or 'Unspecified family'} · {version or 'Version unspecified'} (declared)",
        caption, token_status, context,
    )


def decision_page(candidate, page: int = 0):
    """No scan of the full fact set; only one bounded slice reaches Qt."""
    decisions = candidate.projection_decisions
    total = len(decisions)
    pages = max(1, (total + DECISION_PAGE_SIZE - 1) // DECISION_PAGE_SIZE)
    page = max(0, min(int(page), pages - 1))
    start = page * DECISION_PAGE_SIZE
    return decisions[start:start + DECISION_PAGE_SIZE], page, pages, total


def decision_text(decision: Mapping[str, object]) -> str:
    """Keep owner and relationship evidence separately meaningful and exact."""
    fields = (
        ("Reason", "reason"),
        ("Recorded source value", "source_value"), ("Source", "source"),
        ("Owner scope", "owner_scope"), ("Fact type", "fact_type"),
        ("Projection treatment", "action"), ("Caption output", "output"),
        ("Profile policy", "profile_policy"),
    )
    lines = [f"{label}: {decision.get(key) or '(not recorded)'}" for label, key in fields]
    path = decision.get("relationship_path") or ()
    lines.append("Supporting relationship path: " + (" → ".join(map(str, path)) or "(not recorded)"))
    lines.append("Image Knowledge: this read-only review leaves every recorded fact unchanged. An omission or combination applies only to this projection.")
    return "\n\n".join(lines)
