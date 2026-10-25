"""Adapt explicitly supplied local Help guides without inventing review evidence."""
from __future__ import annotations

from datetime import date
from hashlib import sha256

from .learning_topic_search import LearningTopic, LearningTopicIndex


UNRECORDED_DATE = date.min
MAX_HELP_GUIDES = 64
MAX_HELP_BYTES = 4 * 1024 * 1024


def help_guide_topics(guides):
    """Snapshot (stable ID, title, exact Markdown, source) rows, never read files.

    Existing Help has no per-guide provenance envelope. The minimum date is an
    explicit unknown sentinel, not a claimed publication/review date; consumers
    must display it as unrecorded. Content hashes identify bytes, not approval.
    """
    topics = []
    seen = set()
    size = 0
    for identity, title, body, source in guides:
        if len(topics) >= MAX_HELP_GUIDES:
            raise ValueError("Too many local Help guides.")
        if any(type(value) is not str or not value.strip() for value in (identity, title, body, source)):
            raise ValueError("Each guide needs its exact identity, title, content and source.")
        if identity in seen:
            raise ValueError("Duplicate local Help guide identity.")
        seen.add(identity)
        size += sum(len(value.encode("utf-8")) for value in (identity, title, body, source))
        if size > MAX_HELP_BYTES:
            raise ValueError("Local Help content exceeds its search bound.")
        topics.append(LearningTopic(
            topic_id=identity, revision=sha256(body.encode("utf-8")).hexdigest(),
            title=title, body=body, kind="walkthrough", source=source,
            license="Not recorded per guide", author="Not recorded per guide",
            reviewer="Not recorded per guide", as_of=UNRECORDED_DATE,
            tags=(identity,),
        ))
    return tuple(topics)


def search_help_guides(topics, query, *, guide_id=None, should_continue=None):
    """Build, search and close on this one worker; no connection crosses threads.

    The small bundled Help corpus is rebuilt per request. This intentionally
    uses the accepted stock index lifecycle without a retained-thread service.
    """
    index = LearningTopicIndex(topics, should_continue=should_continue)
    try:
        return index.search(query, tag=guide_id, limit=20, should_continue=should_continue)
    finally:
        index.close()
