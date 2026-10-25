"""Disposable offline LRN-01 index over explicitly supplied instructional topics.

Uses standard-library SQLite FTS5. No file discovery, content publication,
network, notes store, Knowledge authority, semantic answers or UI integration.
The caller owns content provenance/review and the original job/thread lifecycle.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import date
import re
import sqlite3


MAX_TOPICS = 40_000
MAX_CONTENT_BYTES = 64 * 1024 * 1024
KINDS = frozenset({"concept", "walkthrough", "decision-guide", "worked-example",
                   "glossary", "troubleshooting"})


@dataclass(frozen=True)
class LearningTopic:
    topic_id: str
    revision: str
    title: str
    body: str
    kind: str
    source: str
    license: str
    author: str
    reviewer: str
    as_of: date
    synonyms: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    # Exact tool/version labels only; this index does not interpret version ranges.
    applicability: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class TopicMatch:
    topic: LearningTopic
    excerpt: str
    qualifications: tuple[str, ...]


def _text(value, limit, field):
    if type(value) is not str or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError(f"A bounded nonempty {field} is required.")


def _validated(topic):
    if type(topic) is not LearningTopic:
        raise TypeError("Supply exact LearningTopic records.")
    for field, limit in (("topic_id", 128), ("revision", 128), ("title", 256),
                         ("body", 65536), ("source", 2048), ("license", 256),
                         ("author", 256), ("reviewer", 256)):
        _text(getattr(topic, field), limit, field)
    if topic.kind not in KINDS or type(topic.as_of) is not date:
        raise ValueError("An instructional kind and exact as-of date are required.")
    for field in ("synonyms", "tags"):
        values = getattr(topic, field)
        if type(values) is not tuple or len(values) > 32:
            raise ValueError("Use bounded immutable synonyms and tags.")
        for value in values:
            _text(value, 128, field)
        if len({value.casefold() for value in values}) != len(values):
            raise ValueError("Duplicate synonyms or tags are not admitted.")
    if type(topic.applicability) is not tuple or len(topic.applicability) > 32:
        raise ValueError("Use bounded immutable tool/version applicability.")
    for item in topic.applicability:
        if type(item) is not tuple or len(item) != 2:
            raise ValueError("Each applicability item needs an exact tool and version.")
        _text(item[0], 128, "tool")
        _text(item[1], 128, "version")
    return replace(topic)


def _checkpoint(should_continue):
    if should_continue is not None and not should_continue():
        raise InterruptedError("Learning topic operation cancelled.")


class LearningTopicIndex:
    """One-thread in-memory search owner; rebuild swaps only after full success."""

    def __init__(self, topics=(), *, should_continue=None):
        self._db = None
        self._topics = {}
        self._revisions = Counter()
        self.rebuild(topics, should_continue=should_continue)

    def close(self):
        if self._db is not None:
            self._db.close()
            self._db = None
            self._topics = {}
            self._revisions = Counter()

    def rebuild(self, topics, *, should_continue=None):
        """Build from supplied canonical content; never replace it with index data."""
        _checkpoint(should_continue)
        candidate = sqlite3.connect(":memory:")
        prepared, seen, total = {}, set(), 0
        try:
            candidate.execute("CREATE VIRTUAL TABLE topic_fts USING fts5(title, body, synonyms)")
            candidate.execute("CREATE TABLE topic_tags(topic_row INTEGER, tag TEXT)")
            candidate.execute("CREATE INDEX tag_lookup ON topic_tags(tag, topic_row)")
            with candidate:
                for number, raw in enumerate(topics, 1):
                    _checkpoint(should_continue)
                    if number > MAX_TOPICS:
                        raise ValueError("Learning topic count exceeds the bounded index limit.")
                    topic = _validated(raw)
                    key = (topic.topic_id, topic.revision)
                    if key in seen:
                        raise ValueError("Duplicate topic identity/revision; prior index retained.")
                    seen.add(key)
                    values = (topic.title, topic.body, " ".join(topic.synonyms))
                    content = (topic.topic_id, topic.revision, topic.title, topic.body,
                               topic.kind, topic.source, topic.license, topic.author,
                               topic.reviewer, *topic.synonyms, *topic.tags,
                               *(part for pair in topic.applicability for part in pair))
                    total += sum(len(value.encode("utf-8")) for value in content)
                    if total > MAX_CONTENT_BYTES:
                        raise ValueError("Learning content exceeds the bounded index byte limit.")
                    candidate.execute("INSERT INTO topic_fts(rowid,title,body,synonyms) VALUES (?,?,?,?)",
                                      (number, *values))
                    candidate.executemany("INSERT INTO topic_tags VALUES (?,?)",
                                          ((number, tag.casefold()) for tag in topic.tags))
                    prepared[number] = topic
            candidate.execute("INSERT INTO topic_fts(topic_fts) VALUES ('integrity-check')")
            _checkpoint(should_continue)
        except BaseException:
            candidate.close()
            raise
        old = self._db
        self._db, self._topics = candidate, prepared
        self._revisions = Counter(topic.topic_id for topic in prepared.values())
        if old is not None:
            old.close()

    def search(self, query, *, kind=None, tag=None, limit=20,
               dated_before=None, should_continue=None):
        """Literal AND search; exact ties use topic ID/revision, never insertion order.

        Source, license, review identity, date and applicability travel with every
        match. Qualifications expose multiple revisions and caller-selected age;
        neither indicates verified compatibility with the user's current runtime.
        """
        if self._db is None:
            raise RuntimeError("Learning topic index is closed.")
        if type(query) is not str or len(query) > 512:
            raise ValueError("Use a search query of at most 512 characters.")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Use a result limit between 1 and 100.")
        if kind is not None and kind not in KINDS:
            raise ValueError("Unknown instructional kind.")
        if tag is not None:
            _text(tag, 128, "tag filter")
        if dated_before is not None and type(dated_before) is not date:
            raise ValueError("Use an exact date cutoff.")
        words = re.findall(r"\w+", query, flags=re.UNICODE)
        if len(words) > 32:
            raise ValueError("Use at most 32 literal search terms.")
        _checkpoint(should_continue)
        if not words:
            return ()
        expression = " AND ".join('"' + word + '"' for word in words)
        sql = ("SELECT rowid, bm25(topic_fts, 6.0, 1.0, 3.0), "
               "snippet(topic_fts, 1, '', '', ' … ', 24) "
               "FROM topic_fts WHERE topic_fts MATCH ?")
        parameters = [expression]
        if tag is not None:
            sql += " AND rowid IN (SELECT topic_row FROM topic_tags WHERE tag=?)"
            parameters.append(tag.casefold())
        # Corpus is bounded. Keep kind filtering before the final result limit.
        interrupted = False
        def progress():
            nonlocal interrupted
            interrupted = should_continue is not None and not should_continue()
            return int(interrupted)
        self._db.set_progress_handler(progress, 1000)
        try:
            rows = self._db.execute(sql, parameters).fetchall()
        except sqlite3.OperationalError:
            if interrupted:
                raise InterruptedError("Learning topic search cancelled.") from None
            raise
        finally:
            self._db.set_progress_handler(None, 0)
        _checkpoint(should_continue)
        rows.sort(key=lambda row: (row[1], self._topics[row[0]].topic_id, self._topics[row[0]].revision))
        matches = []
        for rowid, score, excerpt in rows:
            topic = self._topics[rowid]
            if kind is not None and topic.kind != kind:
                continue
            notes = []
            if self._revisions[topic.topic_id] > 1:
                notes.append("multiple-topic-revisions")
            if dated_before is not None and topic.as_of < dated_before:
                notes.append("dated-before-requested-cutoff")
            matches.append(TopicMatch(topic, excerpt, tuple(notes)))
            if len(matches) == limit:
                break
        _checkpoint(should_continue)
        return tuple(matches)
