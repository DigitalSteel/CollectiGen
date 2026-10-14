"""Shared persistence for global Plain Language Description exclusions."""

from __future__ import annotations

from collections.abc import Iterable

from .settings import normalize_plain_language_exclusion


def _values(value: object) -> list[object]:
    """Return a safe snapshot of the configured values without treating text as rows."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        return []
    return list(value)


def add_description_exclusions(settings, values) -> bool:
    """Add exact description exclusions and save them as sorted original spellings.

    Matching uses the existing plain-language exclusion normalizer.  A failed
    settings write restores the in-memory list so callers can show the failure
    without accidentally leaving an unsaved policy active.
    """
    previous = _values(getattr(settings, "ignored_plain_language_sentences", ()))
    by_key: dict[str, object] = {}
    for value in previous:
        key = normalize_plain_language_exclusion(value)
        if key and key not in by_key:
            by_key[key] = value
    for value in _values(values):
        key = normalize_plain_language_exclusion(value)
        if key and key not in by_key:
            by_key[key] = value
    updated = sorted(by_key.values(), key=lambda value: str(value).casefold())
    if updated == previous:
        return False
    settings.ignored_plain_language_sentences = updated
    try:
        settings.save()
    except Exception:
        settings.ignored_plain_language_sentences = previous
        raise
    return True


def restore_description_exclusions(settings, values) -> bool:
    """Restore only exact normalized exclusions selected by the person.

    The current settings are read at restore time, preserving unrelated values
    added after a dialog opened.  As with additions, an unsuccessful save rolls
    back the in-memory setting.
    """
    previous = _values(getattr(settings, "ignored_plain_language_sentences", ()))
    restoring = {
        normalize_plain_language_exclusion(value)
        for value in _values(values)
        if normalize_plain_language_exclusion(value)
    }
    if not restoring:
        return False
    updated = [
        value
        for value in previous
        if normalize_plain_language_exclusion(value) not in restoring
    ]
    if updated == previous:
        return False
    settings.ignored_plain_language_sentences = updated
    try:
        settings.save()
    except Exception:
        settings.ignored_plain_language_sentences = previous
        raise
    return True
