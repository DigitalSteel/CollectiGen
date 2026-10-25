from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
import re
import unicodedata
from typing import Iterable


_MAX_ENTRIES = 5_000
_MAX_FIELD_LENGTH = 512
_MAX_QUERY_LENGTH = 256
_MAX_QUERY_WORDS = 12
_MAX_RESULTS = 100
_WORD_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True)
class SearchEntry:
    key: str
    label: str
    section: str = ""
    keywords: tuple[str, ...] = ()
    context: str = ""


def _normalise(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value)[:_MAX_FIELD_LENGTH]).casefold()
    return " ".join(_WORD_PATTERN.findall(value))


def _one_typo_apart(left: str, right: str) -> bool:
    """Accept one substitution, insertion/deletion, or adjacent transposition."""
    if left == right or min(len(left), len(right)) < 4:
        return False
    difference = len(left) - len(right)
    if abs(difference) > 1:
        return False
    if difference == 0:
        mismatches = [index for index, pair in enumerate(zip(left, right))
                      if pair[0] != pair[1]]
        if len(mismatches) == 1:
            return True
        return (
            len(mismatches) == 2
            and mismatches[1] == mismatches[0] + 1
            and left[mismatches[0]] == right[mismatches[1]]
            and left[mismatches[1]] == right[mismatches[0]]
        )

    shorter, longer = (right, left) if difference > 0 else (left, right)
    short_index = long_index = differences = 0
    while short_index < len(shorter) and long_index < len(longer):
        if shorter[short_index] == longer[long_index]:
            short_index += 1
            long_index += 1
            continue
        differences += 1
        if differences > 1:
            return False
        long_index += 1
    return True


def _word_match(query_word: str, candidate_word: str) -> int | None:
    if query_word == candidate_word:
        return 0
    if len(query_word) >= 2 and candidate_word.startswith(query_word):
        return 1
    if _one_typo_apart(query_word, candidate_word):
        return 2
    return None


def _match_score(entry: SearchEntry, query: str, query_words: tuple[str, ...]):
    # Field priority keeps purpose labels ahead of contextual section matches.
    fields = (
        (entry.label, 0),
        *((keyword, 1) for keyword in entry.keywords),
        (entry.section, 2),
    )
    normalised = [(_normalise(value), priority) for value, priority in fields if value]

    exact_priorities = [priority for value, priority in normalised if value == query]
    if exact_priorities:
        priority = min(exact_priorities)
        return (priority * 3, 0, priority)
    prefix_priorities = [
        priority for value, priority in normalised if value.startswith(query)
    ]
    if prefix_priorities:
        priority = min(prefix_priorities)
        return (priority * 3 + 1, 0, priority)

    candidate_words = [
        (word, priority)
        for value, priority in normalised
        for word in value.split()
    ]
    matches: list[tuple[int, int, int]] = []
    for query_word in query_words:
        alternatives = [
            (quality + priority * 3, quality, priority)
            for word, priority in candidate_words
            if (quality := _word_match(query_word, word)) is not None
        ]
        if not alternatives:
            return None
        matches.append(min(alternatives))
    return (2 + max(cost for cost, _quality, _priority in matches),
            sum(quality for _cost, quality, _priority in matches),
            sum(priority for _cost, _quality, priority in matches))


def _context_score(entry_context: str, current_context: str) -> int:
    if not current_context:
        return 1
    entry_context = _normalise(entry_context)
    if entry_context == current_context:
        return 0
    if set(entry_context.split()) & set(current_context.split()):
        return 1
    return 2


def rank_commands(
    entries: Iterable[SearchEntry],
    query: str,
    *,
    context: str = "",
    limit: int = 30,
) -> list[SearchEntry]:
    """Return bounded, deterministic offline matches for command-like entries."""
    if limit <= 0 or len(str(query)) > _MAX_QUERY_LENGTH:
        return []
    normalised_query = _normalise(query)
    query_words = tuple(normalised_query.split())
    if not query_words or len(query_words) > _MAX_QUERY_WORDS:
        return []
    current_context = _normalise(context)
    ranked = []
    for index, entry in enumerate(islice(entries, _MAX_ENTRIES)):
        score = _match_score(entry, normalised_query, query_words)
        if score is None:
            continue
        ranked.append((score, _context_score(entry.context, current_context), index, entry))
    ranked.sort(key=lambda result: result[:3])
    bounded_limit = min(int(limit), _MAX_RESULTS)
    return [entry for _score, _context, _index, entry in ranked[:bounded_limit]]
