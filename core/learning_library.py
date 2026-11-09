"""Versioned instructional content and separate transactional reading state.

No network, user-library discovery, Knowledge writes or downloaded execution.
Checksums establish pack integrity, never publisher authenticity or review.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
import sqlite3
import os
from uuid import uuid4
import zipfile
import zlib

from .learning_topic_search import LearningTopic, LearningTopicIndex, MAX_CONTENT_BYTES
from suite_core.knowledge.pack_container import checked_archive_entries, validate_json_depth

MAX_PACK_BYTES = MAX_CONTENT_BYTES + 1024 * 1024
MAX_STORED_REVISIONS = 256
MAX_STORED_BYTES = 4 * MAX_CONTENT_BYTES
REVIEW_STATES = frozenset({'reviewed-source', 'review-needed', 'deprecated', 'unsupported'})
_ID = re.compile(r'[a-z0-9][a-z0-9:_-]{0,127}\Z')


def _text(value, label, maximum=2048):
    if type(value) is not str or not value.strip() or len(value) > maximum or '\x00' in value:
        raise ValueError(f'Invalid {label}.')
    return value


def _id(value):
    if type(value) is not str or not _ID.fullmatch(value):
        raise ValueError('Invalid instructional identity.')
    return value


def _date(value):
    if type(value) is not str:
        raise ValueError('An exact ISO content date is required.')
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError('Use an exact ISO content date.')
    return parsed


def _strings(value, label, maximum=64):
    if type(value) is not list or len(value) > maximum:
        raise ValueError(f'Use bounded {label}.')
    result = tuple(_id(item) for item in value)
    if len(set(result)) != len(result):
        raise ValueError(f'Duplicate {label}.')
    return result


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('Duplicate JSON key.')
        result[key] = value
    return result


def _json(raw, maximum=MAX_CONTENT_BYTES):
    if type(raw) is not bytes or not raw or len(raw) > maximum:
        raise ValueError('Instructional payload exceeds its byte bound.')
    validate_json_depth(raw)
    def reject_constant(value):
        raise ValueError('Nonfinite JSON numbers are not admitted.')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=reject_constant)
    if type(value) is not dict:
        raise ValueError('An instructional envelope is required.')
    return value


@dataclass(frozen=True)
class LearningArticle:
    topic: LearningTopic
    links: tuple[str, ...]
    contexts: tuple[str, ...]
    review_state: str
    origin: str = 'bundled'


@dataclass(frozen=True)
class LearningCohort:
    cohort_id: str
    revision: str
    as_of: date
    license: str
    articles: tuple[LearningArticle, ...]
    raw: bytes


def bundled_cohort() -> LearningCohort:
    """Read one shipped source asset, never discover a user library or profile."""
    path = Path(__file__).resolve().parents[1] / 'resources' / 'learning' / 'bundled_topics.json'
    with path.open('rb') as source:
        raw = source.read(MAX_CONTENT_BYTES + 1)
    return parse_cohort(raw)


def open_bundled_library(state_dir=None):
    if state_dir is None:
        from .paths import USER_DATA_DIR
        state_dir = USER_DATA_DIR / 'learning'
    return LearningLibrary(Path(state_dir), bundled_cohort())


def parse_cohort(raw: bytes, *, origin='bundled') -> LearningCohort:
    """Preserve the exact envelope, including unknown future optional fields."""
    envelope = _json(raw)
    if type(envelope.get('schema_version')) is not int or envelope['schema_version'] != 1:
        raise ValueError('Unsupported Learning cohort schema.')
    identity = _id(envelope.get('cohort_id'))
    revision = _text(envelope.get('revision'), 'cohort revision', 128)
    as_of = _date(envelope.get('as_of'))
    license_name = _text(envelope.get('license'), 'cohort license', 256)
    rows = envelope.get('topics')
    if type(rows) is not list or not rows or len(rows) > 40000:
        raise ValueError('A bounded nonempty topic cohort is required.')
    articles = []
    for row in rows:
        if type(row) is not dict:
            raise ValueError('An instructional topic object is required.')
        _id(row.get('topic_id'))
        values = {key: row.get(key) for key in ('topic_id', 'revision', 'title', 'body',
            'kind', 'source', 'license', 'author', 'reviewer')}
        if type(values['kind']) is not str:
            raise ValueError('An instructional kind is required.')
        values['as_of'] = _date(row.get('as_of'))
        for name in ('synonyms', 'tags'):
            supplied = row.get(name, [])
            if type(supplied) is not list:
                raise ValueError('Use immutable bounded topic text lists.')
            values[name] = tuple(supplied)
        applicability = row.get('applicability', [])
        if type(applicability) is not list or any(type(pair) is not list for pair in applicability):
            raise ValueError('Use exact tool/version pairs.')
        values['applicability'] = tuple(tuple(pair) for pair in applicability)
        state = row.get('review_state', 'review-needed')
        if type(state) is not str or state not in REVIEW_STATES:
            raise ValueError('Unknown instructional review state.')
        articles.append(LearningArticle(LearningTopic(**values),
            _strings(row.get('links', []), 'related topics'),
            _strings(row.get('contexts', []), 'Help contexts'), state, origin))
    # Use the canonical topic validation and count/content bounds, not a second
    # implementation of search/index constraints. This disposable index is closed.
    index = LearningTopicIndex(article.topic for article in articles)
    index.close()
    return LearningCohort(identity, revision, as_of, license_name, tuple(articles), raw)


def export_pack(cohort: LearningCohort) -> bytes:
    """File transfer includes content only, never a user's notes or reading state."""
    checked = parse_cohort(cohort.raw)
    manifest = {'schema_version': 1, 'cohort_id': checked.cohort_id,
        'revision': checked.revision, 'payload_bytes': len(checked.raw),
        'sha256': sha256(checked.raw).hexdigest()}
    output = BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json', json.dumps(manifest, sort_keys=True).encode())
        archive.writestr('topics.json', checked.raw)
    return output.getvalue()


def inspect_pack(raw: bytes) -> LearningCohort:
    """Validate a bounded exact two-member ZIP in memory; never extract it."""
    if type(raw) is not bytes or len(raw) > MAX_PACK_BYTES:
        raise ValueError('Learning Pack exceeds its archive bound.')
    try:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            entries = checked_archive_entries(archive, max_files=2,
                max_uncompressed_bytes=MAX_PACK_BYTES, max_member_bytes=MAX_CONTENT_BYTES)
            if {entry.filename for entry in entries} != {'manifest.json', 'topics.json'} or len(entries) != 2:
                raise ValueError('Learning Pack requires exactly manifest.json and topics.json.')
            for entry in entries:
                mode = (entry.external_attr >> 16) & 0o170000
                if ('\x00' in entry.orig_filename or entry.flag_bits & 1 or entry.is_dir()
                        or mode not in (0, 0o100000)
                        or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    raise ValueError('Unsafe Learning Pack member.')
            manifest = _json(archive.read('manifest.json'), 16384)
            payload = archive.read('topics.json')
            if (type(manifest.get('schema_version')) is not int or manifest['schema_version'] != 1
                    or type(manifest.get('payload_bytes')) is not int
                    or manifest['payload_bytes'] != len(payload)
                    or manifest.get('sha256') != sha256(payload).hexdigest()):
                raise ValueError('Learning Pack integrity/schema mismatch.')
            cohort = parse_cohort(payload, origin='imported; review claims unverified')
            if (manifest.get('cohort_id'), manifest.get('revision')) != (cohort.cohort_id, cohort.revision):
                raise ValueError('Learning Pack identity mismatch.')
            return cohort
    except (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, NotImplementedError) as exc:
        raise ValueError('Unreadable Learning Pack.') from exc


def read_pack_file(path: Path) -> bytes:
    """Read only the user-selected file, with an actual streamed byte bound."""
    with Path(path).open('rb') as source:
        raw = source.read(MAX_PACK_BYTES + 1)
    if len(raw) > MAX_PACK_BYTES:
        raise ValueError('Learning Pack exceeds its archive bound.')
    return raw


def write_pack_file(path: Path, raw: bytes, *, expected_digest: str | None):
    """Atomic selected-file export with stale guard and retained prior bytes.

    The caller obtains confirmation for this exact path/expected prior hash.
    No notes are accepted by this API. A prior file is retained beside the target
    as a unique recovery artifact; unrelated files/backups are never overwritten.
    """
    inspect_pack(raw)
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Choose a regular Learning Pack destination.')
    old = read_pack_file(path) if path.exists() else None
    if (sha256(old).hexdigest() if old is not None else None) != expected_digest:
        raise ValueError('Export destination changed; review the destination again.')
    temporary = path.with_name(f'.{path.name}.{uuid4().hex}.tmp')
    backup = None
    try:
        with temporary.open('xb') as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        if temporary.read_bytes() != raw:
            raise OSError('Learning Pack export failed staged read-back.')
        if old is not None:
            backup = path.with_name(f'{path.name}.{uuid4().hex}.previous')
            with backup.open('xb') as recovery:
                recovery.write(old)
            if backup.read_bytes() != old:
                raise OSError('Learning Pack recovery artifact failed read-back.')
        # Recheck immediately before atomic publication. External replacement in
        # the final OS rename interval is not a filesystem CAS guarantee.
        current = read_pack_file(path) if path.exists() else None
        if current != old or path.is_symlink():
            raise ValueError('Export destination changed; retained prior bytes for review.')
        os.replace(temporary, path)
        if read_pack_file(path) != raw:
            raise OSError('Learning Pack export failed final read-back; retain recovery artifact.')
        return backup
    except OSError as exc:
        if backup is not None:
            raise OSError(f'{exc} Prior bytes retained at {backup}.') from exc
        raise
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class LearningImportPreview:
    cohort: LearningCohort
    generation: int
    digest: str
    already_active: bool = False


@dataclass(frozen=True)
class LearningRollbackPreview:
    cohort_id: str
    current_revision: str
    previous_revision: str | None
    generation: int


class LearningLibrary:
    """Canonical content plus SQLite state; each call owns its connection/thread.

    Imported revisions are immutable. Activation rollback touches only content
    pointers, so later notes, bookmarks and reading history survive recovery.
    """
    def __init__(self, state_dir: Path, bundled: LearningCohort):
        self.bundled = parse_cohort(bundled.raw)
        self._protected_ids = {a.topic.topic_id for a in self.bundled.articles}
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.database = self.state_dir / 'learning.sqlite3'
        with self._connection(write=True) as db:
            for statement in (
                'CREATE TABLE IF NOT EXISTS library_meta (id INTEGER PRIMARY KEY CHECK(id=1), generation INTEGER NOT NULL)',
                'CREATE TABLE IF NOT EXISTS cohorts (id TEXT, revision TEXT, digest TEXT NOT NULL, raw BLOB NOT NULL, PRIMARY KEY(id,revision))',
                'CREATE TABLE IF NOT EXISTS active_cohorts (id TEXT PRIMARY KEY, revision TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS imports (sequence INTEGER PRIMARY KEY, id TEXT NOT NULL, old_revision TEXT, new_revision TEXT NOT NULL, restored INTEGER NOT NULL DEFAULT 0)',
                'CREATE TABLE IF NOT EXISTS bookmarks (topic_id TEXT, revision TEXT, PRIMARY KEY(topic_id,revision))',
                'CREATE TABLE IF NOT EXISTS reading_history (sequence INTEGER PRIMARY KEY, topic_id TEXT NOT NULL, revision TEXT NOT NULL, opened TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS notes (topic_id TEXT PRIMARY KEY, body TEXT NOT NULL)'):
                db.execute(statement)
            db.execute('INSERT OR IGNORE INTO library_meta VALUES (1,0)')

    @contextmanager
    def _connection(self, *, write=False):
        db = sqlite3.connect(self.database, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            else:
                db.execute('BEGIN')
            yield db
            if write:
                db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @property
    def generation(self):
        with self._connection() as db:
            return db.execute('SELECT generation FROM library_meta WHERE id=1').fetchone()[0]

    def cohorts(self):
        with self._connection() as db:
            rows = db.execute('SELECT c.raw,c.digest FROM active_cohorts a LEFT JOIN cohorts c ON c.id=a.id AND c.revision=a.revision ORDER BY a.id').fetchall()
        return (self.bundled, *(self._stored_cohort(row) for row in rows))

    @staticmethod
    def _stored_cohort(row):
        if row is None or row['raw'] is None:
            raise ValueError('Missing accepted Learning Pack revision; retain state for recovery.')
        if type(row['raw']) is not bytes:
            raise ValueError('Invalid stored Learning Pack payload; retain state for recovery.')
        raw = row['raw']
        if sha256(raw).hexdigest() != row['digest']:
            raise ValueError('Stored Learning Pack changed; retain state for recovery.')
        return parse_cohort(raw, origin='imported; review claims unverified')

    def _admit_active(self, db, replaced_id, replacement):
        rows = db.execute('SELECT c.raw,c.digest FROM active_cohorts a LEFT JOIN cohorts c ON c.id=a.id AND c.revision=a.revision WHERE a.id<>?', (replaced_id,)).fetchall()
        if len(rows) >= 64:
            raise ValueError('Too many active Learning cohorts.')
        cohorts = [self.bundled]
        if replacement is not None:
            cohorts.append(replacement)
        raw_size = sum(len(c.raw) for c in cohorts)
        for row in rows:
            if row['raw'] is None:
                raise ValueError('Missing accepted Learning Pack revision.')
            raw_size += len(row['raw'])
            if raw_size > MAX_CONTENT_BYTES:
                raise ValueError('Combined Learning Library exceeds its content bound.')
            cohorts.append(self._stored_cohort(row))
        if raw_size > MAX_CONTENT_BYTES:
            raise ValueError('Combined Learning Library exceeds its content bound.')
        owners = {}
        topics = []
        for cohort in cohorts:
            for article in cohort.articles:
                identity = article.topic.topic_id
                if identity in owners and owners[identity] != cohort.cohort_id:
                    raise ValueError('Topic identities conflict with another installed cohort.')
                owners[identity] = cohort.cohort_id
                topics.append(article.topic)
        index = LearningTopicIndex(topics)
        index.close()

    def articles(self):
        return tuple(article for cohort in self.cohorts() for article in cohort.articles)

    def find(self, topic_id, revision):
        return next((a for a in self.articles() if (a.topic.topic_id, a.topic.revision) == (topic_id, revision)), None)

    def preview_import(self, raw: bytes):
        cohort = inspect_pack(raw)
        digest = sha256(cohort.raw).hexdigest()
        shipped_match = cohort.cohort_id == self.bundled.cohort_id and cohort.raw == self.bundled.raw
        if not shipped_match and (cohort.cohort_id == self.bundled.cohort_id or any(a.topic.topic_id in self._protected_ids for a in cohort.articles)):
            raise ValueError('Learning Packs cannot replace trusted shipped topics.')
        with self._connection() as db:
            generation = db.execute('SELECT generation FROM library_meta WHERE id=1').fetchone()[0]
            active = db.execute('SELECT c.digest FROM active_cohorts a JOIN cohorts c ON c.id=a.id AND c.revision=a.revision WHERE a.id=?', (cohort.cohort_id,)).fetchone()
        return LearningImportPreview(cohort, generation, digest, shipped_match or active is not None and active[0] == digest)

    def import_reviewed(self, preview: LearningImportPreview):
        if type(preview) is not LearningImportPreview:
            raise ValueError('Review a Learning Pack before importing it.')
        cohort = parse_cohort(preview.cohort.raw, origin='imported; review claims unverified')
        digest = sha256(cohort.raw).hexdigest()
        shipped_match = cohort.cohort_id == self.bundled.cohort_id and cohort.raw == self.bundled.raw
        if (digest != preview.digest or not shipped_match and (cohort.cohort_id == self.bundled.cohort_id
                or any(a.topic.topic_id in self._protected_ids for a in cohort.articles))):
            raise ValueError('Changed or protected Learning Pack.')
        with self._connection(write=True) as db:
            generation = db.execute('SELECT generation FROM library_meta WHERE id=1').fetchone()[0]
            if generation != preview.generation:
                raise ValueError('Learning Library changed; review the pack again.')
            if shipped_match:
                return False
            previous = db.execute('SELECT revision FROM active_cohorts WHERE id=?', (cohort.cohort_id,)).fetchone()
            existing = db.execute('SELECT digest FROM cohorts WHERE id=? AND revision=?', (cohort.cohort_id, cohort.revision)).fetchone()
            if existing is not None and existing[0] != digest:
                raise ValueError('Conflicting bytes for the same cohort revision.')
            if previous is not None and previous[0] == cohort.revision:
                return False
            self._admit_active(db, cohort.cohort_id, cohort)
            if existing is None:
                size, count = db.execute('SELECT COALESCE(SUM(length(raw)),0),COUNT(*) FROM cohorts').fetchone()
                if size + len(cohort.raw) > MAX_STORED_BYTES or count >= MAX_STORED_REVISIONS:
                    raise ValueError('Retained Learning Pack storage is full; preserve/export history before another import.')
            db.execute('INSERT OR IGNORE INTO cohorts VALUES (?,?,?,?)', (cohort.cohort_id, cohort.revision, digest, cohort.raw))
            db.execute('INSERT OR REPLACE INTO active_cohorts VALUES (?,?)', (cohort.cohort_id, cohort.revision))
            db.execute('INSERT INTO imports(id,old_revision,new_revision) VALUES (?,?,?)', (cohort.cohort_id, previous[0] if previous else None, cohort.revision))
            db.execute('UPDATE library_meta SET generation=generation+1 WHERE id=1')
        return True

    def preview_rollback(self, cohort_id):
        with self._connection() as db:
            generation = db.execute('SELECT generation FROM library_meta WHERE id=1').fetchone()[0]
            row = db.execute('SELECT * FROM imports WHERE id=? AND restored=0 ORDER BY sequence DESC LIMIT 1', (_id(cohort_id),)).fetchone()
            active = db.execute('SELECT revision FROM active_cohorts WHERE id=?', (cohort_id,)).fetchone()
            if row is None or active is None or active[0] != row['new_revision']:
                raise ValueError('No matching active Learning Pack update to restore.')
            return LearningRollbackPreview(cohort_id, active[0], row['old_revision'], generation)

    def rollback_import(self, cohort_id, *, expected_generation):
        with self._connection(write=True) as db:
            if db.execute('SELECT generation FROM library_meta WHERE id=1').fetchone()[0] != expected_generation:
                raise ValueError('Learning Library changed; review recovery again.')
            row = db.execute('SELECT * FROM imports WHERE id=? AND restored=0 ORDER BY sequence DESC LIMIT 1', (_id(cohort_id),)).fetchone()
            active = db.execute('SELECT revision FROM active_cohorts WHERE id=?', (cohort_id,)).fetchone()
            if row is None or active is None or active[0] != row['new_revision']:
                raise ValueError('No matching active Learning Pack update to restore.')
            restored = None if row['old_revision'] is None else self._stored_cohort(
                db.execute('SELECT raw,digest FROM cohorts WHERE id=? AND revision=?', (cohort_id, row['old_revision'])).fetchone())
            self._admit_active(db, cohort_id, restored)
            if row['old_revision'] is None:
                db.execute('DELETE FROM active_cohorts WHERE id=?', (cohort_id,))
            else:
                db.execute('UPDATE active_cohorts SET revision=? WHERE id=?', (row['old_revision'], cohort_id))
            db.execute('UPDATE imports SET restored=1 WHERE sequence=?', (row['sequence'],))
            db.execute('UPDATE library_meta SET generation=generation+1 WHERE id=1')

    def bookmark(self, topic_id, revision, *, enabled=True):
        _id(topic_id); _text(revision, 'topic revision', 128)
        with self._connection(write=True) as db:
            if enabled:
                db.execute('INSERT OR IGNORE INTO bookmarks VALUES (?,?)', (topic_id, revision))
            else:
                db.execute('DELETE FROM bookmarks WHERE topic_id=? AND revision=?', (topic_id, revision))

    def bookmarks(self):
        with self._connection() as db:
            return tuple(tuple(row) for row in db.execute('SELECT topic_id,revision FROM bookmarks ORDER BY topic_id,revision'))

    def record_read(self, topic_id, revision):
        _id(topic_id); _text(revision, 'topic revision', 128)
        with self._connection(write=True) as db:
            db.execute('INSERT INTO reading_history(topic_id,revision,opened) VALUES (?,?,?)', (topic_id, revision, datetime.now(timezone.utc).isoformat()))
            db.execute('DELETE FROM reading_history WHERE sequence NOT IN (SELECT sequence FROM reading_history ORDER BY sequence DESC LIMIT 1000)')

    def history(self, limit=50):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Use a bounded reading history limit.')
        with self._connection() as db:
            return tuple(tuple(row) for row in db.execute('SELECT topic_id,revision,opened FROM reading_history ORDER BY sequence DESC LIMIT ?', (limit,)))

    def save_note(self, topic_id, body, *, expected=''):
        _id(topic_id)
        if type(body) is not str or len(body) > 65536 or '\x00' in body:
            raise ValueError('Use a bounded plain-text note.')
        if type(expected) is not str:
            raise ValueError('The note draft needs its exact prior text.')
        with self._connection(write=True) as db:
            row = db.execute('SELECT body FROM notes WHERE topic_id=?', (topic_id,)).fetchone()
            if (row[0] if row else '') != expected:
                raise ValueError('Note changed; keep the draft and reload before saving.')
            db.execute('INSERT OR REPLACE INTO notes VALUES (?,?)', (topic_id, body))

    def note(self, topic_id):
        _id(topic_id)
        with self._connection() as db:
            row = db.execute('SELECT body FROM notes WHERE topic_id=?', (topic_id,)).fetchone()
            return row[0] if row else ''
