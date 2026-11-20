"""Project discovery into public facts for a reviewed contextual request."""

from functools import lru_cache
import re
import sqlite3

from core.backend_connection_settings import BackendConnectionSettings
from core.learning_help_catalog import search_help_guides
from core.learning_library import bundled_cohort
from core.training_setup_selection import (
    TrainingSetupSelectionError, TrainingSetupSelectionRecoveryRequired, TrainingSetupSelectionService,
)


COMPONENTS = {
    "collectigen.media-organizer": "Media Organizer",
    "collectigen.tag-encyclopedia": "Tag Encyclopedia",
    "collectigen.generate": "Generate",
    "collectigen.train": "Train",
}
PACKAGE_STATES = {
    "package_verified": "Package identity verified. Runtime readiness requires a separate check.",
    "not_configured": "No saved connection. Review setup in Suite Manager.",
    "missing": "Saved package is missing. Review setup in Suite Manager.",
    "unknown": "Package status is unknown.",
}
TRAINER_STATES = {
    "not_configured": "Trainer Setup is required.",
    "configured": "Trainer Setup is saved. Check the trainer, model and machine in Train.",
    "recovery_required": "Recover Trainer Setup in Suite Manager.",
    "unknown": "Trainer Setup status is unknown.",
}
SOFTWARE_STATES = {
    "missing": "The saved managed trainer software is missing.",
    "verified": "Managed trainer software files were verified.",
    "unverified": "Managed trainer software could not be verified.",
    "unknown": "Managed trainer software status is unknown.",
}
_QUERY_TERMS = re.compile(r"\b(?:training collections?|remix)\b", re.IGNORECASE)


def suite_context_revision(settings_path):
    """Read the settings revisions that can change an open help preview."""
    try:
        settings = BackendConnectionSettings(settings_path)
        manager = settings.observe().digest
    except (OSError, ValueError):
        manager = None
    if manager is None:
        return None, None
    try:
        _, trainer = TrainingSetupSelectionService(settings.path.parent / "training-setup.json").snapshot()
    except TrainingSetupSelectionRecoveryRequired:
        trainer = "recovery_required"
    except (OSError, TrainingSetupSelectionError):
        trainer = None
    return manager, trainer


@lru_cache(maxsize=1)
def _public_topics():
    return tuple(article.topic for article in bundled_cohort().articles
                 if article.origin == "bundled" and article.review_state == "reviewed-source")


def help_for_question(base, question, *, budget=8192):
    """Search just the shipped public corpus and bound the exact preview context."""
    match = _QUERY_TERMS.search(question)
    query = match.group().casefold().removesuffix("s") if match else question[:512]
    if not query.strip():
        return base
    try:
        replies = search_help_guides(_public_topics(), query)
    except (OSError, ValueError, sqlite3.Error):
        return base
    context = base
    for reply in replies[:3]:
        topic = reply.topic
        excerpt = (
            f"\n\nBundled Learning topic {topic.topic_id} revision {topic.revision}.\n"
            f"{topic.title}\n{reply.excerpt}"
        )
        if len((context + excerpt).encode("utf-8")) > budget:
            break
        context += excerpt
    return context


def public_suite_help(discovery):
    """Use known state codes only. Omit private fields and free-form diagnostics."""
    lines = [
        "Answer questions about CollectiGen using these public workflow facts.",
        "Do not assume an unverified workflow is ready or claim that you performed an action.",
        "Organizer can create, save and export training collections without Train.",
        "Opening training work in Train is separate from starting a run.",
        "Remix starts from an eligible image in Organizer and uses Generate.",
        "When a module needs setup, the user can review it in Suite Manager and return to the same selection.",
    ]
    components = discovery.get("components", {})
    for component, label in COMPONENTS.items():
        row = components.get(component, {})
        state = PACKAGE_STATES.get(row.get("state"), PACKAGE_STATES["unknown"])
        lines.append(f"{label}. {state}")
        if component == "collectigen.train":
            lines.append(TRAINER_STATES.get(row.get("trainer_setup"), TRAINER_STATES["unknown"]))
            lines.append(SOFTWARE_STATES.get(row.get("trainer_software"), SOFTWARE_STATES["unknown"]))
    return "\n".join(lines)


def preview_suite_help(controller, question, discovery):
    """Send this preview to the user before any provider receives the messages."""
    base = public_suite_help(discovery)
    return controller.preview_context(question, help_for_question(
        base, question, budget=controller.max_context_bytes - len(question.encode("utf-8")),
    ))
