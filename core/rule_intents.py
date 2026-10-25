from __future__ import annotations

from typing import Final


TRAINING_PROJECTION: Final = "training_projection"
REJECT_INCOMING_EVIDENCE: Final = "reject_incoming_evidence"
IMAGE_CORRECTION_REVIEW: Final = "image_correction_review"

RULE_INTENT_LABELS: Final = {
    TRAINING_PROJECTION: "Build training captions",
    REJECT_INCOMING_EVIDENCE: "Reject incoming evidence",
    IMAGE_CORRECTION_REVIEW: "Review image corrections",
}


def allowed_rule_intents(kind: str) -> tuple[str, ...]:
    """Return the explicit meanings supported by one legacy rule family."""
    if kind == "ignore_rules":
        return (
            REJECT_INCOMING_EVIDENCE,
            TRAINING_PROJECTION,
            IMAGE_CORRECTION_REVIEW,
        )
    return (TRAINING_PROJECTION, IMAGE_CORRECTION_REVIEW)


def suggested_rule_intent(kind: str) -> str:
    """Return the safest recommendation without silently applying it."""
    return REJECT_INCOMING_EVIDENCE if kind == "ignore_rules" else TRAINING_PROJECTION


def explicit_rule_intent(kind: str, record: dict) -> str:
    """Return a valid stored intent, or an empty value for legacy ambiguity."""
    value = str(record.get("intent") or "")
    return value if value in allowed_rule_intents(kind) else ""
