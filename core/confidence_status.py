from __future__ import annotations


CONFIDENCE_STATUSES = ("red", "orange", "yellow", "green", "neutral")


def status_from_confidence(confidence: float | None) -> str:
    if confidence is None:
        return "neutral"
    value = min(1.0, max(0.0, float(confidence)))
    if value >= 0.90:
        return "green"
    if value >= 0.75:
        return "yellow"
    if value >= 0.50:
        return "orange"
    return "red"


def status_from_attention(score: int | float) -> str:
    value = max(0.0, float(score))
    if value >= 75:
        return "red"
    if value >= 40:
        return "orange"
    if value > 0:
        return "yellow"
    return "green"


def status_from_severity(severity: str) -> str:
    return {
        "error": "red", "critical": "red", "high": "red",
        "problem": "orange", "review": "orange", "medium": "orange",
        "warning": "yellow", "low": "yellow", "uncertain": "yellow",
        "ready": "green", "confirmed": "green",
    }.get(str(severity or "").casefold(), "neutral")
