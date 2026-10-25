from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Callable,Iterable,Mapping

from .automation_templates import has_automation_placeholder
from .caption_import import looks_like_metadata_prose_fragment,metadata_sources_only
from .paths import CACHE_DIR
from .tag_collection import normalize_tag

@dataclass(frozen=True)
class CachedImageRecord:
    path: str
    caption_exists: bool
    sidecar_exists: bool
    tags: list[str]
    error: str = ""
    tag_sources: dict[str, list[str]] = None
    plain_language: list[str] = None

    def __post_init__(self) -> None:
        if self.tag_sources is None:
            object.__setattr__(self,"tag_sources",{})
        if self.plain_language is None:
            object.__setattr__(self,"plain_language",[])


def _signature(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat(); return stat.st_size, stat.st_mtime_ns
    except OSError:
        return -1, -1


def _typed_cache_values(
    tags: list[str],sources: dict[str,list[str]],
) -> tuple[list[str],dict[str,list[str]]]:
    """Keep an older disposable cache from reintroducing Metadata prose as tags."""
    normalized_sources={normalize_tag(key):list(values) for key,values in sources.items()}
    visible=[
        tag for tag in tags
        if not has_automation_placeholder(tag) and not (
            metadata_sources_only(normalized_sources.get(normalize_tag(tag),()))
            and looks_like_metadata_prose_fragment(tag)
        )
    ]
    visible_keys={normalize_tag(tag) for tag in visible}
    return visible,{
        key:list(values) for key,values in sources.items()
        if normalize_tag(key) in visible_keys
    }


class DatasetIndex:
    """Rebuildable SQLite cache for parsed per-image dataset information."""

    # This is a disposable search cache, not Image Knowledge schema. Version 4
    # added separately typed confirmed Plain Language Descriptions. Version 5
    # rematerializes typed Tag rows so per-image rejected/confirmed-absent
    # identities cannot survive in contextual evidence results. Version 6
    # rematerializes stable secondary-context scope markers in the existing
    # source map so interactive conflict queries do not reopen every sidecar.
    # Version 7 adds per-image current-rule results beside the completion
    # checkpoint. These rows are disposable and bind exact portable authority
    # content to the System Knowledge / Automation fingerprint that derived
    # actionable candidates and diagnostics from it. Version 8 adds a
    # rebuildable stable-ID dependency index for exact System Knowledge
    # invalidation. A completion sentinel distinguishes a genuinely empty
    # dependency set from a legacy or interrupted cache row.
    # Negative decisions are intentionally not copied into this disposable cache.
    # Version 9 adds persisted, rebuildable Image Workflow State projections.
    # They remain subordinate to portable Image Knowledge and are reused only
    # while both the indexed file signatures and shared context fingerprint
    # still match.
    SCHEMA_VERSION = 9
    EVIDENCE_INDEX_VERSION = 1
    MANAGED_RULE_RESULT_VERSION = 1

    def __init__(self, dataset_root: Path, cache_dir: Path = CACHE_DIR) -> None:
        # Cache lookup must not probe a remembered UNC/network root. Validation
        # happens later in the background inventory pass.
        self.root = Path(os.path.abspath(os.path.normpath(os.fspath(Path(dataset_root).expanduser()))))
        self._records_cache: list[CachedImageRecord] | None = None
        self._records_cache_token: tuple[str,int,int] | None = None
        self._records_revision = 0
        self._records_lock = RLock()
        key = hashlib.sha256(str(self.root).casefold().encode("utf-8")).hexdigest()[:16]
        cache_dir=Path(cache_dir); cache_dir.mkdir(parents=True, exist_ok=True)
        self.path = cache_dir / f"dataset_{key}.sqlite3"
        self._initialize()

    def evidence_snapshot_token(self) -> tuple[str,int,int]:
        """Identify the durable saved-record snapshot across app restarts."""
        try:
            generation=int(self.metadata("evidence_generation","0") or 0)
        except (OSError,sqlite3.DatabaseError,TypeError,ValueError,OverflowError):
            generation=-1
        return str(self.path),self.SCHEMA_VERSION,generation

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=20)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _initialize(self) -> None:
        with closing(self.connect()) as db, db:
            # Serialize additive cache upgrades across independent page indexes.
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            version = db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
            try:
                previous_version=int(version[0]) if version else 0
            except (TypeError,ValueError,OverflowError):
                previous_version=-1
            images_table_existed=bool(db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='images'"
            ).fetchone())
            image_columns=(
                {str(row[1]) for row in db.execute("PRAGMA table_info(images)")}
                if images_table_existed else set()
            )
            evidence_table_existed=bool(db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='image_evidence_tags'"
            ).fetchone())
            evidence_columns=(
                {str(row[1]) for row in db.execute("PRAGMA table_info(image_evidence_tags)")}
                if evidence_table_existed else set()
            )
            rule_results_existed=bool(db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='managed_rule_results'"
            ).fetchone())
            rule_result_columns=(
                {str(row[1]) for row in db.execute("PRAGMA table_info(managed_rule_results)")}
                if rule_results_existed else set()
            )
            dependency_table_existed=bool(db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='managed_dependencies'"
            ).fetchone())
            dependency_columns=(
                {
                    str(row[1])
                    for row in db.execute("PRAGMA table_info(managed_dependencies)")
                }
                if dependency_table_existed else set()
            )
            workflow_table_existed=bool(db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='image_workflow_states'"
            ).fetchone())
            workflow_columns=(
                {
                    str(row[1])
                    for row in db.execute("PRAGMA table_info(image_workflow_states)")
                }
                if workflow_table_existed else set()
            )
            if evidence_table_existed and not {"path","normalized_tag"}.issubset(evidence_columns):
                db.execute("DROP TABLE image_evidence_tags")
                evidence_table_existed=False
            if rule_results_existed and not {
                "path","image_size","image_mtime","sidecar_size","sidecar_mtime",
                "authority_sha256","rules_fingerprint","result_fingerprint",
                "result_json",
            }.issubset(rule_result_columns):
                db.execute("DROP TABLE managed_rule_results")
            if dependency_table_existed and not {
                "path","kind","stable_id",
            }.issubset(dependency_columns):
                db.execute("DROP TABLE managed_dependencies")
            if workflow_table_existed and not {
                "path","image_size","image_mtime","caption_size",
                "caption_mtime","sidecar_size","sidecar_mtime",
                "context_fingerprint","state_json",
            }.issubset(workflow_columns):
                db.execute("DROP TABLE image_workflow_states")
            if (
                previous_version not in {0,2,3,6,7,8,self.SCHEMA_VERSION}
                or (previous_version==0 and images_table_existed)
            ):
                db.execute("DROP TABLE IF EXISTS images")
                db.execute("DROP TABLE IF EXISTS image_evidence_tags")
                db.execute("DROP TABLE IF EXISTS image_workflow_states")
                images_table_existed=False
                image_columns=set()
                evidence_table_existed=False
            db.execute("""CREATE TABLE IF NOT EXISTS images (
                path TEXT PRIMARY KEY,
                image_size INTEGER NOT NULL, image_mtime INTEGER NOT NULL,
                caption_size INTEGER NOT NULL, caption_mtime INTEGER NOT NULL,
                sidecar_size INTEGER NOT NULL, sidecar_mtime INTEGER NOT NULL,
                tags_json TEXT NOT NULL, tag_sources_json TEXT NOT NULL DEFAULT '{}',
                plain_language_json TEXT,
                error TEXT NOT NULL DEFAULT ''
            )""")
            if images_table_existed and "plain_language_json" not in image_columns:
                db.execute("ALTER TABLE images ADD COLUMN plain_language_json TEXT")
            db.execute("""CREATE TABLE IF NOT EXISTS image_evidence_tags (
                path TEXT NOT NULL,
                normalized_tag TEXT NOT NULL,
                PRIMARY KEY(path,normalized_tag)
            )""")
            db.execute("""CREATE INDEX IF NOT EXISTS image_evidence_tags_by_tag
                ON image_evidence_tags(normalized_tag,path)""")
            db.execute("""CREATE TABLE IF NOT EXISTS managed_scan_checkpoints (
                path TEXT PRIMARY KEY,
                image_size INTEGER NOT NULL, image_mtime INTEGER NOT NULL,
                sidecar_size INTEGER NOT NULL, sidecar_mtime INTEGER NOT NULL,
                rules_fingerprint TEXT NOT NULL, job_fingerprint TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS managed_rule_results (
                path TEXT PRIMARY KEY,
                image_size INTEGER NOT NULL, image_mtime INTEGER NOT NULL,
                sidecar_size INTEGER NOT NULL, sidecar_mtime INTEGER NOT NULL,
                authority_sha256 TEXT NOT NULL,
                rules_fingerprint TEXT NOT NULL,
                result_fingerprint TEXT NOT NULL,
                result_json TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS managed_dependencies (
                path TEXT NOT NULL,
                kind TEXT NOT NULL,
                stable_id TEXT NOT NULL,
                PRIMARY KEY(path,kind,stable_id)
            )""")
            db.execute("""CREATE INDEX IF NOT EXISTS managed_dependencies_lookup
                ON managed_dependencies(kind,stable_id,path)""")
            db.execute("""CREATE TABLE IF NOT EXISTS image_schema_migration_checkpoints (
                path TEXT PRIMARY KEY,
                image_size INTEGER NOT NULL, image_mtime INTEGER NOT NULL,
                sidecar_size INTEGER NOT NULL, sidecar_mtime INTEGER NOT NULL,
                target_version INTEGER NOT NULL
            )""")
            # Dataset and Ingest assess different contexts for the same image.
            # Keep one current projection per surface, not one per context
            # revision (which would grow indefinitely as rules change).
            legacy_workflow=bool(db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='image_workflow_states'"
            ).fetchone()) and "cache_scope" not in {
                str(row[1]) for row in db.execute("PRAGMA table_info(image_workflow_states)")
            }
            if legacy_workflow:
                db.execute("ALTER TABLE image_workflow_states RENAME TO legacy_image_workflow_states")
            db.execute("""CREATE TABLE IF NOT EXISTS image_workflow_states (
                path TEXT NOT NULL,
                cache_scope TEXT NOT NULL DEFAULT 'dataset',
                image_size INTEGER NOT NULL, image_mtime INTEGER NOT NULL,
                caption_size INTEGER NOT NULL, caption_mtime INTEGER NOT NULL,
                sidecar_size INTEGER NOT NULL, sidecar_mtime INTEGER NOT NULL,
                context_fingerprint TEXT NOT NULL,
                state_json TEXT NOT NULL,
                PRIMARY KEY(path,cache_scope)
            )""")
            # Separate invalidation tokens reject late status-worker writes.
            # Additive disposable-cache metadata never clears existing results.
            db.execute("""CREATE TABLE IF NOT EXISTS workflow_invalidations (
                path TEXT PRIMARY KEY, revision INTEGER NOT NULL
            )""")
            if legacy_workflow:
                # The old row did not record its owning surface. Retain it in
                # both slots; exact context/signature checks still decide reuse.
                for scope in ("dataset","ingest"):
                    db.execute("""INSERT INTO image_workflow_states
                        SELECT path,?,image_size,image_mtime,caption_size,caption_mtime,
                               sidecar_size,sidecar_mtime,context_fingerprint,state_json
                        FROM legacy_image_workflow_states""",(scope,))
                db.execute("DROP TABLE legacy_image_workflow_states")
            db.execute("""CREATE INDEX IF NOT EXISTS image_workflow_states_context
                ON image_workflow_states(context_fingerprint,path)""")
            evidence_version=db.execute(
                "SELECT value FROM metadata WHERE key='evidence_index_version'"
            ).fetchone()
            if (
                previous_version!=self.SCHEMA_VERSION
                or not evidence_table_existed
                or not evidence_version
                or str(evidence_version[0])!=str(self.EVIDENCE_INDEX_VERSION)
            ):
                self._rebuild_evidence_index(db)
                self._advance_evidence_generation(db)
            db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_version',?)",(str(self.SCHEMA_VERSION),))
            db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('evidence_index_version',?)",(str(self.EVIDENCE_INDEX_VERSION),))
            db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('dataset_root',?)",(str(self.root),))

    @staticmethod
    def _advance_evidence_generation(db) -> None:
        row=db.execute(
            "SELECT value FROM metadata WHERE key='evidence_generation'"
        ).fetchone()
        try:
            generation=int(row[0]) if row else 0
        except (TypeError,ValueError,OverflowError):
            generation=0
        db.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES('evidence_generation',?)",
            (str(generation+1),),
        )

    @staticmethod
    def _replace_evidence_tags(db,path: str,tags: Iterable[str]) -> None:
        db.execute("DELETE FROM image_evidence_tags WHERE path=?",(str(path),))
        values=sorted({value for tag in tags if (value:=normalize_tag(tag))})
        db.executemany(
            "INSERT INTO image_evidence_tags(path,normalized_tag) VALUES(?,?)",
            ((str(path),value) for value in values),
        )

    def _rebuild_evidence_index(self,db) -> None:
        """Rebuild normalized Tag rows only from the disposable saved view."""
        db.execute("DELETE FROM image_evidence_tags")
        rows=db.execute(
            "SELECT path,tags_json,tag_sources_json FROM images"
        )
        for path,tags_json,tag_sources_json in rows:
            try:
                tags=[str(value) for value in json.loads(tags_json)]
            except (json.JSONDecodeError,TypeError):
                tags=[]
            try:
                sources={
                    str(key):[str(value) for value in values if str(value).strip()]
                    for key,values in json.loads(tag_sources_json or "{}").items()
                    if isinstance(values,list)
                }
            except (json.JSONDecodeError,TypeError,AttributeError):
                sources={}
            visible,_sources=_typed_cache_values(tags,sources)
            self._replace_evidence_tags(db,str(path),visible)

    def metadata(self, key: str, default: str = "") -> str:
        with closing(self.connect()) as db:
            row=db.execute("SELECT value FROM metadata WHERE key=?",(key,)).fetchone()
        return str(row[0]) if row else default

    def set_metadata(self, key: str, value: str) -> None:
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)",(key,str(value)))

    @staticmethod
    def fingerprint(image: Path) -> tuple[int, int, int, int, int, int]:
        return (*_signature(image), *_signature(image.with_suffix('.txt')), *_signature(image.with_suffix('.tags.json')))

    def get_if_fresh(self, image: Path, db=None) -> CachedImageRecord | None:
        if db is None:
            with closing(self.connect()) as connection: return self.get_if_fresh(image,connection)
        row = db.execute("SELECT image_size,image_mtime,caption_size,caption_mtime,sidecar_size,sidecar_mtime,tags_json,tag_sources_json,plain_language_json,error FROM images WHERE path=?",(str(image),)).fetchone()
        if not row or tuple(row[:6]) != self.fingerprint(image): return None
        try: tags = [str(value) for value in json.loads(row[6])]
        except (json.JSONDecodeError, TypeError): return None
        try:
            sources = {
                str(key): [str(value) for value in values if str(value).strip()]
                for key, values in json.loads(row[7] or "{}").items()
                if isinstance(values, list)
            }
        except (json.JSONDecodeError, TypeError, AttributeError):
            sources = {}
        if row[8] is None:
            # A pre-v4 cache row has no trustworthy Description snapshot. Make
            # the normal readable-library analysis republish this row once.
            return None
        try:
            plain_language=[str(value) for value in json.loads(row[8])]
        except (json.JSONDecodeError,TypeError):
            return None
        tags,sources=_typed_cache_values(tags,sources)
        return CachedImageRecord(
            str(image),row[2] >= 0,row[4] >= 0,tags,
            str(row[9] or ''),sources,plain_language,
        )

    def cached_workflow_states(
        self,*,context_fingerprint: str,cache_scope: str="dataset",context_validator=None,
    ) -> dict[Path,dict[str,object]]:
        """Load signature-matched readiness projections without probing files."""
        result={}
        self.workflow_cache_summary={}
        try:
            with closing(self.connect()) as db:
                # Counts explain a restart miss without logging paths, values,
                # rule identities or portable image evidence.
                counts=db.execute(
                    """SELECT
                         SUM(CASE WHEN states.path IS NULL THEN 1 ELSE 0 END),
                         SUM(CASE WHEN states.context_fingerprint<>? THEN 1 ELSE 0 END),
                         SUM(CASE WHEN states.context_fingerprint=? AND (
                           images.image_size<>states.image_size OR images.image_mtime<>states.image_mtime OR
                           images.caption_size<>states.caption_size OR images.caption_mtime<>states.caption_mtime OR
                           images.sidecar_size<>states.sidecar_size OR images.sidecar_mtime<>states.sidecar_mtime
                         ) THEN 1 ELSE 0 END)
                       FROM images LEFT JOIN image_workflow_states AS states
                         ON images.path=states.path AND states.cache_scope=?
                       WHERE images.sidecar_size>=0""",
                    (str(context_fingerprint),str(context_fingerprint),str(cache_scope)),
                ).fetchone()
                self.workflow_cache_summary=dict(zip(
                    ("missing","context_changed","file_changed"),
                    (int(value or 0) for value in counts),
                ))
                rows=db.execute(
                    """SELECT states.path,states.state_json,states.context_fingerprint
                       FROM image_workflow_states AS states
                       INNER JOIN images AS images ON images.path=states.path
                         AND images.image_size=states.image_size
                         AND images.image_mtime=states.image_mtime
                         AND images.caption_size=states.caption_size
                         AND images.caption_mtime=states.caption_mtime
                         AND images.sidecar_size=states.sidecar_size
                         AND images.sidecar_mtime=states.sidecar_mtime
                       WHERE (states.context_fingerprint=? OR ?) AND states.cache_scope=?""",
                    (str(context_fingerprint),bool(context_validator),str(cache_scope)),
                )
                rows=list(rows)
            for path,state_json,saved_context in rows:
                try:
                    payload=json.loads(str(state_json))
                except (json.JSONDecodeError,TypeError):
                    continue
                if isinstance(payload,dict) and (saved_context==str(context_fingerprint)
                        or callable(context_validator) and context_validator(payload)):
                    result[Path(path)]=payload
        except (OSError,sqlite3.DatabaseError,TypeError):
            self.workflow_cache_summary={"unavailable":1}
            return {}
        self.workflow_cache_summary["restored"]=len(result)
        return result

    def validated_workflow_states(
        self,images: Iterable[Path],*,context_fingerprint: str,cache_scope: str="dataset",context_validator=None,source_fingerprints=None,should_continue=None,progress=None,validate_sources: bool=True,
    ) -> dict[Path,dict[str,object]]:
        """Load projections whose stored image-family timestamps still match.

        This is the Ingest/open-folder boundary: it performs only cheap size and
        modified-time checks. It never parses an unchanged image, TXT caption,
        or Image Knowledge sidecar.
        """
        wanted={Path(image) for image in images}
        if not wanted:
            return {}
        result={}
        try:
            def selected_rows():
                selected=tuple(str(path) for path in wanted)
                for offset in range(0,len(selected),256):
                    if should_continue is not None and not should_continue():
                        raise InterruptedError("Saved status validation cancelled safely")
                    batch=selected[offset:offset+256]
                    placeholders=",".join("?" for _ in batch)
                    # Load only requested families, and release the read cursor
                    # before semantic validation can yield to foreground work.
                    with closing(self.connect()) as db:
                        rows=list(db.execute(
                            """SELECT path,image_size,image_mtime,caption_size,
                                      caption_mtime,sidecar_size,sidecar_mtime,state_json,context_fingerprint
                               FROM image_workflow_states
                               WHERE (context_fingerprint=? OR ?) AND cache_scope=?
                                 AND path IN ("""+placeholders+")",
                            (str(context_fingerprint),bool(context_validator),str(cache_scope),*batch),
                        ))
                    yield from rows
            checked=0
            if progress:progress(0,len(wanted))
            for row in selected_rows():
                if should_continue is not None and not should_continue():
                    raise InterruptedError("Saved status validation cancelled safely")
                path=Path(row[0])
                if path not in wanted:continue
                checked+=1
                if progress and checked%32==0:progress(checked,len(wanted))
                # Dataset checks the captured six-field receipt immediately
                # before using each row. Avoid probing all cloud-backed families
                # twice before the first classification result is delivered.
                if validate_sources and self.fingerprint(path)!=tuple(row[1:7]):continue
                try:
                    payload=json.loads(str(row[7]))
                except (json.JSONDecodeError,TypeError):
                    continue
                if isinstance(payload,dict) and (row[8]==str(context_fingerprint)
                        or callable(context_validator) and context_validator(payload)):
                    result[path]=payload
                    if source_fingerprints is not None:source_fingerprints[path]=tuple(row[1:7])
            if progress:progress(len(wanted),len(wanted))
        except InterruptedError:
            raise
        except (OSError,sqlite3.DatabaseError,TypeError):
            return {}
        return result

    def current_workflow_proofs(self, images, *, should_cancel=None,progress=None):
        """Return compact proofs of unchanged Dataset status, from a worker.

        Each proof retains its own context; only the consumer can establish
        that its knowledge/character/category context and current value agree.
        No proof authorizes installing an older result into the current view.
        """
        from .workflow_state_cache import workflow_state_digest

        wanted={Path(image) for image in images}
        result={}
        if not wanted:
            return result
        try:
            with closing(self.connect()) as db:
                selected=tuple(str(path) for path in wanted)
                for offset in range(0,len(selected),256):
                    if progress:progress(offset,len(selected))
                    if should_cancel and should_cancel():
                        break
                    batch=selected[offset:offset+256]
                    placeholders=",".join("?" for _ in batch)
                    rows=db.execute(
                        """SELECT path,image_size,image_mtime,caption_size,
                                  caption_mtime,sidecar_size,sidecar_mtime,
                                  context_fingerprint,state_json
                           FROM image_workflow_states
                           WHERE cache_scope='dataset' AND path IN ("""
                        +placeholders+")",batch,
                    )
                    for row in rows:
                        if should_cancel and should_cancel():
                            return result
                        path=Path(row[0])
                        if self.fingerprint(path)!=tuple(row[1:7]):
                            continue
                        try:
                            payload=json.loads(str(row[8]))
                            if isinstance(payload,dict):
                                result[str(path)]=(str(row[7]),workflow_state_digest(payload))
                        except (ValueError,TypeError):
                            continue
        except (OSError,sqlite3.DatabaseError,TypeError):
            return {}
        if progress:progress(len(wanted),len(wanted))
        return result

    def record_workflow_states(
        self,values: Mapping[Path,Mapping],*,context_fingerprint: str,db=None,
        expected_fingerprints: Mapping[Path,tuple[int,...]]|None=None,
        cache_scope: str="dataset",expected_revisions: Mapping[Path,int]|None=None,
    ) -> set[Path]:
        """Persist stable rebuildable readiness projections after a worker scan.

        A source family can change while classification is reading it.  Callers
        that capture the pre-read fingerprint may provide it here; changed rows
        are then skipped instead of publishing an old result under a new file
        signature.
        """
        if db is None:
            with closing(self.connect()) as connection,connection:
                return self.record_workflow_states(
                    values,context_fingerprint=context_fingerprint,db=connection,
                    expected_fingerprints=expected_fingerprints,
                    cache_scope=cache_scope,expected_revisions=expected_revisions,
                )
        expected={
            Path(path):tuple(fingerprint)
            for path,fingerprint in (expected_fingerprints or {}).items()
        }
        if expected_revisions is not None and not db.in_transaction:
            db.execute("BEGIN IMMEDIATE")
        revisions=self.workflow_revisions(values,db=db) if expected_revisions is not None else {}
        rows=[]
        recorded=set()
        for image,payload in values.items():
            image=Path(image)
            if expected_revisions is not None and revisions.get(image,0)!=expected_revisions.get(image,0):
                continue
            fingerprint=self.fingerprint(image)
            if image in expected and expected[image]!=fingerprint:
                continue
            rows.append((
                str(image),str(cache_scope),*fingerprint,str(context_fingerprint),
                json.dumps(
                    dict(payload),sort_keys=True,separators=(",",":"),
                    ensure_ascii=False,
                ),
            ))
            recorded.add(image)
        if rows:
            db.executemany(
                """INSERT OR REPLACE INTO image_workflow_states(
                       path,cache_scope,image_size,image_mtime,caption_size,caption_mtime,
                       sidecar_size,sidecar_mtime,context_fingerprint,state_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
        return recorded

    def sensitivity_workflow_images(self,groups,*,images=None,should_continue=None) -> set[Path]:
        """Select saved workflow rows by confirmed rating inputs, including Ingest.

        Only legacy rows without indexed inputs need a read of saved JSON;
        never inspect embedded Metadata, TXT or AI to narrow a rating change.
        """
        from .image_record import ImageRecord
        wanted={normalize_tag(tag) for group in groups for tag in group}
        scope=None if images is None else set(map(Path,images))
        selected=set()
        with closing(self.connect()) as db:
            rows=list(db.execute("""SELECT path,cache_scope,state_json,image_size,image_mtime,
                caption_size,caption_mtime,sidecar_size,sidecar_mtime FROM image_workflow_states"""))
        legacy_by_scope={}
        for row in rows:
            legacy_by_scope.setdefault(row[1],[]).append(Path(row[0]))
        inputs={key:self.workflow_confirmed_tags(paths,cache_scope=key)
                for key,paths in legacy_by_scope.items()}
        for row in rows:
            if should_continue is not None and not should_continue():
                raise InterruptedError("Rating dependency query cancelled")
            path=Path(row[0])
            if scope is not None and path not in scope:continue
            try:payload=json.loads(row[2])
            except (TypeError,ValueError):selected.add(path);continue
            if not isinstance(payload,dict):selected.add(path);continue
            tags=payload.get("rating_tags",inputs[row[1]].get(path))
            if tags is None:
                before=self.fingerprint(path)
                if before!=tuple(row[3:]):selected.add(path);continue
                try:
                    if not path.with_suffix(".tags.json").is_file():
                        selected.add(path);continue
                    tags=tuple(ImageRecord(path).annotation.all_structured_tags())
                except (OSError,ValueError,TypeError):selected.add(path);continue
                if self.fingerprint(path)!=before:selected.add(path);continue
                # Retain discovered dependency coverage without certifying a
                # newly performed preparation or changing any saved decision.
                payload["rating_tags"]=tags
                with closing(self.connect()) as db,db:
                    db.execute("UPDATE image_workflow_states SET state_json=? WHERE path=? AND cache_scope=? AND state_json=?",
                        (json.dumps(payload,sort_keys=True,separators=(",",":"),ensure_ascii=False),str(path),row[1],row[2]))
            if (not isinstance(tags,(list,tuple)) or any(not isinstance(tag,str) for tag in tags)
                    or any(normalize_tag(tag) in wanted for tag in tags)):
                selected.add(path)
        return selected

    def workflow_confirmed_tags(self,images,*,cache_scope="dataset",recover_legacy=False) -> dict[Path,tuple[str,...]]:
        """Recover legacy rating inputs only from matching indexed authority."""
        wanted=tuple(dict.fromkeys(str(Path(path)) for path in images)); result={}
        with closing(self.connect()) as db:
            for offset in range(0,len(wanted),256):
                batch=wanted[offset:offset+256]; placeholders=",".join("?" for _ in batch)
                rows=db.execute("""SELECT images.path,images.tag_sources_json FROM images
                    JOIN image_workflow_states AS states ON states.path=images.path
                    AND states.image_size=images.image_size AND states.image_mtime=images.image_mtime
                    AND states.caption_size=images.caption_size AND states.caption_mtime=images.caption_mtime
                    AND states.sidecar_size=images.sidecar_size AND states.sidecar_mtime=images.sidecar_mtime
                    WHERE states.cache_scope=? AND images.path IN ("""+placeholders+")",(cache_scope,*batch))
                for path,raw in rows:
                    try:sources=json.loads(raw)
                    except (TypeError,ValueError):continue
                    if isinstance(sources,dict):
                        result[Path(path)]=tuple(tag for tag,labels in sources.items()
                            if isinstance(labels,list) and "Confirmed Image Knowledge" in labels)
        if recover_legacy:
            from .image_record import ImageRecord
            missing=set(map(Path,wanted))-set(result)
            with closing(self.connect()) as db:
                rows=db.execute("""SELECT path,image_size,image_mtime,caption_size,
                    caption_mtime,sidecar_size,sidecar_mtime FROM image_workflow_states
                    WHERE cache_scope=?""",(cache_scope,))
                for row in rows:
                    path=Path(row[0])
                    if path not in missing:continue
                    before=self.fingerprint(path)
                    if before!=tuple(row[1:]):continue
                    try:
                        if not path.with_suffix(".tags.json").is_file():continue
                        tags=tuple(ImageRecord(path).annotation.all_structured_tags())
                    except (OSError,ValueError,TypeError):continue
                    if self.fingerprint(path)==before:result[path]=tags
        return result

    def workflow_revisions(self,images,*,db=None) -> dict[Path,int]:
        """Capture exact invalidation tokens without probing image files."""
        if db is None:
            with closing(self.connect()) as connection:
                return self.workflow_revisions(images,db=connection)
        wanted={str(Path(path)):Path(path) for path in images}
        result={path:0 for path in wanted.values()}
        keys=tuple(wanted)
        for offset in range(0,len(keys),256):
            batch=keys[offset:offset+256]
            placeholders=",".join("?" for _ in batch)
            for path,revision in db.execute(
                "SELECT path,revision FROM workflow_invalidations WHERE path IN ("+placeholders+")",batch,
            ):
                result[wanted[path]]=int(revision)
        return result

    def invalidate_workflow_images(self,images,*,db=None) -> None:
        """Atomically retire exact rows and reject older in-flight checkpoints."""
        if db is None:
            with closing(self.connect()) as connection,connection:
                self.invalidate_workflow_images(images,db=connection)
            return
        rows=[(str(Path(path)),) for path in set(images)]
        db.executemany("""INSERT INTO workflow_invalidations(path,revision) VALUES(?,1)
            ON CONFLICT(path) DO UPDATE SET revision=revision+1""",rows)
        db.executemany("DELETE FROM image_workflow_states WHERE path=?",rows)

    def invalidate_workflow_image(self,image: Path,*,db=None) -> None:
        """Discard one disposable projection without touching authority data."""
        if db is None:
            with closing(self.connect()) as connection,connection:
                self.invalidate_workflow_image(image,db=connection)
            return
        self.invalidate_workflow_images((image,),db=db)

    def trusted_sidecars(self, images: list[Path]) -> set[Path]:
        """Return previously parsed, unchanged JSON records without consulting TXT."""
        wanted={str(Path(image)):Path(image) for image in images}
        trusted=set()
        with closing(self.connect()) as db:
            rows=db.execute(
                "SELECT path,image_size,image_mtime,sidecar_size,sidecar_mtime,error FROM images"
            )
            for path,image_size,image_mtime,sidecar_size,sidecar_mtime,error in rows:
                image=wanted.get(str(path))
                if image is None or error or int(sidecar_size)<0: continue
                if (
                    _signature(image)==(int(image_size),int(image_mtime))
                    and _signature(image.with_suffix('.tags.json'))==(int(sidecar_size),int(sidecar_mtime))
                ):
                    trusted.add(image)
        return trusted

    def record_managed_completion(
        self,image: Path,*,rules_fingerprint: str,job_fingerprint: str,
        derived_result: dict[str,object]|None=None,db=None,
    ) -> None:
        """Durably mark one fully handled managed-scan image as reusable.

        The local checkpoint is rebuildable and never owns image data.  TXT is
        intentionally absent because a normal managed scan trusts current JSON;
        rereading changed external evidence remains a deliberate maintenance
        action.  Image and sidecar signatures still prevent stale authority from
        being trusted after either managed input changes.
        """
        image=Path(image); sidecar=image.with_suffix(".tags.json")
        image_signature=_signature(image); sidecar_signature=_signature(sidecar)
        if image_signature[0]<0 or sidecar_signature[0]<0:
            raise FileNotFoundError(f"Managed completion cannot be checkpointed for {image}.")
        if db is None:
            with closing(self.connect()) as connection, connection:
                self.record_managed_completion(
                    image,rules_fingerprint=rules_fingerprint,
                    job_fingerprint=job_fingerprint,
                    derived_result=derived_result,db=connection,
                )
            return
        payload=None; authority_sha256=""; result_json=""; result_fingerprint=""
        if derived_result is not None:
            payload=dict(derived_result)
            authority_sha256=str(payload.get("authority_sha256") or "")
            if (
                len(authority_sha256)!=64
                or any(value not in "0123456789abcdef" for value in authority_sha256)
            ):
                raise ValueError("A managed rule result requires an exact authority SHA-256.")
            if str(payload.get("rules_fingerprint") or "")!=str(rules_fingerprint):
                raise ValueError("The managed rule result does not match the rule fingerprint.")
            if int(payload.get("version") or 0)!=self.MANAGED_RULE_RESULT_VERSION:
                raise ValueError("The managed rule result version is not supported.")
            result_json=json.dumps(
                payload,sort_keys=True,separators=(",",":"),ensure_ascii=False,
            )
            result_fingerprint=hashlib.sha256(result_json.encode("utf-8")).hexdigest()
        db.execute("SAVEPOINT managed_completion")
        try:
            # Dependency coverage belongs to the exact derived projection.
            # Retire it in the same transaction before replacing that result;
            # a crash before Review projection then lacks the completion
            # sentinel and must fall back to a broad refresh.
            db.execute(
                "DELETE FROM managed_dependencies WHERE path=?",(str(image),),
            )
            if payload is None:
                db.execute(
                    "DELETE FROM managed_rule_results WHERE path=?",(str(image),),
                )
            else:
                db.execute(
                    """INSERT OR REPLACE INTO managed_rule_results(
                        path,image_size,image_mtime,sidecar_size,sidecar_mtime,
                        authority_sha256,rules_fingerprint,result_fingerprint,result_json
                    ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        str(image),*image_signature,*sidecar_signature,
                        authority_sha256,str(rules_fingerprint),
                        result_fingerprint,result_json,
                    ),
                )
            db.execute(
                """INSERT OR REPLACE INTO managed_scan_checkpoints(
                    path,image_size,image_mtime,sidecar_size,sidecar_mtime,
                    rules_fingerprint,job_fingerprint
                ) VALUES(?,?,?,?,?,?,?)""",
                (
                    str(image),*image_signature,*sidecar_signature,
                    str(rules_fingerprint),str(job_fingerprint),
                ),
            )
        except Exception:
            db.execute("ROLLBACK TO managed_completion")
            db.execute("RELEASE managed_completion")
            raise
        db.execute("RELEASE managed_completion")

    @classmethod
    def _decode_managed_rule_result(
        cls,authority_sha256: object,rules_fingerprint: object,
        result_fingerprint: object,result_json: object,*,
        expected_rules_fingerprint: str,
    ) -> dict[str,object]|None:
        try:
            encoded=str(result_json)
            if hashlib.sha256(encoded.encode("utf-8")).hexdigest()!=str(result_fingerprint):
                return None
            payload=json.loads(encoded)
            if not isinstance(payload,dict):
                return None
            if int(payload.get("version") or 0)!=cls.MANAGED_RULE_RESULT_VERSION:
                return None
            if str(rules_fingerprint)!=str(expected_rules_fingerprint):
                return None
            if str(payload.get("rules_fingerprint") or "")!=str(expected_rules_fingerprint):
                return None
            if str(payload.get("authority_sha256") or "")!=str(authority_sha256):
                return None
            authority_sha256=str(authority_sha256)
            if (
                len(authority_sha256)!=64
                or any(value not in "0123456789abcdef" for value in authority_sha256)
            ):
                return None
            for key in (
                "candidate_findings","candidate_automation","automatic_rule_state",
            ):
                if key not in payload:
                    return None
            return payload
        except (json.JSONDecodeError,TypeError,ValueError,OverflowError):
            return None

    def managed_rule_result(
        self,image: Path,*,rules_fingerprint: str,db=None,
    ) -> dict[str,object]|None:
        """Return one exact current disposable rule result, or fail closed."""
        image=Path(image)
        try:
            if db is None:
                with closing(self.connect()) as connection:
                    return self.managed_rule_result(image,rules_fingerprint=rules_fingerprint,db=connection)
            row=db.execute(
                    """SELECT image_size,image_mtime,sidecar_size,sidecar_mtime,
                              authority_sha256,rules_fingerprint,
                              result_fingerprint,result_json
                       FROM managed_rule_results
                       WHERE path=? AND rules_fingerprint=?""",
                    (str(image),str(rules_fingerprint)),
            ).fetchone()
            if not row:
                return None
            if (
                _signature(image)!=(int(row[0]),int(row[1]))
                or _signature(image.with_suffix(".tags.json"))!=(int(row[2]),int(row[3]))
            ):
                return None
            return self._decode_managed_rule_result(
                row[4],row[5],row[6],row[7],
                expected_rules_fingerprint=str(rules_fingerprint),
            )
        except (OSError,sqlite3.DatabaseError,TypeError,ValueError,OverflowError):
            return None

    def invalidate_managed_image(self, image: Path, *, db=None) -> None:
        """Retire disposable rule/checkpoint rows for one exact image."""
        if db is None:
            with closing(self.connect()) as connection, connection:
                self.invalidate_managed_image(image, db=connection)
            return
        db.execute("DELETE FROM managed_rule_results WHERE path=?", (str(Path(image)),))
        db.execute("DELETE FROM managed_scan_checkpoints WHERE path=?", (str(Path(image)),))
        db.execute("DELETE FROM managed_dependencies WHERE path=?", (str(Path(image)),))
        self.invalidate_workflow_images((image,),db=db)

    def record_managed_dependencies(
        self,image: Path,*,tag_ids: Iterable[str]=(),
        relationship_ids: Iterable[str]=(),db=None,
    ) -> bool:
        """Replace one exact rebuildable System Knowledge dependency set."""
        image=Path(image); path=str(image)
        values={
            ("tag",str(value).strip())
            for value in tag_ids
            if value is not None and str(value).strip()
        }
        values.update(
            ("relationship",str(value).strip())
            for value in relationship_ids
            if value is not None and str(value).strip()
        )
        # The sentinel is required even for an image with no current stable-ID
        # dependencies. Its absence makes exact invalidation fail broad.
        values.add(("complete","1"))
        if db is None:
            with closing(self.connect()) as connection,connection:
                return self.record_managed_dependencies(
                    image,tag_ids=tag_ids,relationship_ids=relationship_ids,
                    db=connection,
                )
        existing={
            (str(kind),str(stable_id))
            for kind,stable_id in db.execute(
                "SELECT kind,stable_id FROM managed_dependencies WHERE path=?",
                (path,),
            )
        }
        if existing==values:
            return False
        db.execute("SAVEPOINT managed_dependencies_replace")
        try:
            db.execute("DELETE FROM managed_dependencies WHERE path=?",(path,))
            db.executemany(
                "INSERT INTO managed_dependencies(path,kind,stable_id) VALUES(?,?,?)",
                ((path,kind,stable_id) for kind,stable_id in sorted(values)),
            )
        except Exception:
            db.execute("ROLLBACK TO managed_dependencies_replace")
            db.execute("RELEASE managed_dependencies_replace")
            raise
        db.execute("RELEASE managed_dependencies_replace")
        return True

    def update_managed_dependency_batch(self,images,details=None,*,should_continue=None,progress=None):
        """Commit dependency coverage in bounded transactions, including no-op reuse."""
        images=tuple(map(Path,images));details=details if isinstance(details,dict) else {}
        for offset in range(0,len(images),64):
            if should_continue is not None and not should_continue():
                raise InterruptedError("Dependency finalization cancelled safely")
            batch=images[offset:offset+64]
            with closing(self.connect()) as db,db:
                db.execute("BEGIN")
                for path in batch:
                    value=details.get(str(path))
                    if isinstance(value,dict) and value.get("complete") is True:
                        self.record_managed_dependencies(path,tag_ids=value.get("tag_ids",()),
                            relationship_ids=value.get("relationship_ids",()),db=db)
                    else:self.invalidate_managed_dependencies(path,db=db)
            if progress:progress(min(offset+64,len(images)),len(images))

    def invalidate_managed_dependencies(self,image: Path,*,db=None) -> None:
        """Retire only exact-targeting coverage while preserving rule rows."""
        if db is None:
            with closing(self.connect()) as connection,connection:
                self.invalidate_managed_dependencies(image,db=connection)
            return
        db.execute(
            "DELETE FROM managed_dependencies WHERE path=?",(str(Path(image)),),
        )

    def managed_dependency_images(
        self,images: Iterable[Path],*,tag_ids: Iterable[str]=(),
        relationship_ids: Iterable[str]=(),include_uncovered: bool=False,
    ) -> set[Path]|None:
        """Resolve exact dependencies, optionally including only uncovered rows."""
        wanted={str(Path(image)):Path(image) for image in images}
        if not wanted:
            return set()
        tag_values={
            str(value).strip() for value in tag_ids
            if value is not None and str(value).strip()
        }
        relationship_values={
            str(value).strip() for value in relationship_ids
            if value is not None and str(value).strip()
        }
        complete=set(); selected=set()
        try:
            with closing(self.connect()) as db:
                for (path,) in db.execute(
                    "SELECT path FROM managed_dependencies "
                    "WHERE kind='complete' AND stable_id='1'"
                ):
                    image=wanted.get(str(path))
                    if image is not None:
                        complete.add(image)
                for kind,values in (
                    ("tag",tag_values),("relationship",relationship_values),
                ):
                    for stable_id in values:
                        for (path,) in db.execute(
                            "SELECT path FROM managed_dependencies "
                            "WHERE kind=? AND stable_id=?",
                            (kind,stable_id),
                        ):
                            image=wanted.get(str(path))
                            if image is not None:
                                selected.add(image)
        except sqlite3.DatabaseError:
            return None
        uncovered=set(wanted.values())-complete
        if uncovered and not include_uncovered:
            return None
        return selected | uncovered

    def managed_dependency_refresh_images(self,images,**dependencies):
        """Refresh known matches and repair missing coverage, not known nonmatches."""
        return self.managed_dependency_images(
            images,include_uncovered=True,**dependencies,
        )

    def trusted_managed_images(
        self,images: list[Path],*,rules_fingerprint: str,job_fingerprint: str,
        validated_images: set[Path]|None=None,
        require_rule_results: bool=False,
    ) -> set[Path]:
        """Return exact current per-image completions for this managed job.

        ``validated_images`` identifies rows whose complete image/TXT/JSON
        signature was checked by the current startup pass. For those rows the
        matching saved image/sidecar checkpoint can be joined locally without
        repeating filesystem probes. Other callers retain the exact stat-based
        fallback.
        """
        return self._trusted_managed_images(
            images,rules_fingerprint=str(rules_fingerprint),
            job_fingerprint=str(job_fingerprint),
            validated_images=validated_images,
            require_rule_results=require_rule_results,
        )

    def rebind_managed_rule_results(
        self,images,*,rules_fingerprint,job_fingerprint,validated_images,
        rebind,should_continue=None,
    ) -> set[Path]:
        """Reuse source-validated, semantically equal results with atomic guards.

        Only the startup pass's validated catalog may use this path. Read and
        evaluate receipts outside the write transaction; commit a bounded batch
        only while its result, authority checkpoint and catalog still agree.
        This does not create authority completions or discard dependency coverage.
        """
        wanted={str(Path(path)) for path in images}&{str(Path(path)) for path in validated_images}
        if not wanted:return set()
        retained=set()
        try:
            ordered=sorted(wanted)
            for offset in range(0,len(ordered),64):
                if should_continue is not None and not should_continue():return retained
                batch=ordered[offset:offset+64]
                placeholders=",".join("?" for _ in batch)
                with closing(self.connect()) as db:
                    rows=list(db.execute(f"""SELECT results.path,results.authority_sha256,
                        results.rules_fingerprint,results.result_fingerprint,results.result_json
                        FROM managed_rule_results AS results
                        JOIN managed_scan_checkpoints AS checkpoints ON checkpoints.path=results.path
                        JOIN images ON images.path=results.path
                        WHERE results.path IN ({placeholders}) AND checkpoints.job_fingerprint=?
                          AND checkpoints.rules_fingerprint=results.rules_fingerprint
                          AND results.rules_fingerprint<>?
                          AND checkpoints.image_size=results.image_size AND checkpoints.image_mtime=results.image_mtime
                          AND checkpoints.sidecar_size=results.sidecar_size AND checkpoints.sidecar_mtime=results.sidecar_mtime
                          AND images.image_size=results.image_size AND images.image_mtime=results.image_mtime
                          AND images.sidecar_size=results.sidecar_size AND images.sidecar_mtime=results.sidecar_mtime""",
                        (*batch,str(job_fingerprint),str(rules_fingerprint))))
                prepared=[]
                for path,authority,old_rules,old_hash,old_json in rows:
                    if should_continue is not None and not should_continue():return retained
                    if path not in wanted:continue
                    payload=self._decode_managed_rule_result(authority,old_rules,old_hash,old_json,
                        expected_rules_fingerprint=old_rules)
                    if payload is None:continue
                    current=rebind(payload)
                    if not isinstance(current,dict) or current.get("rules_fingerprint")!=str(rules_fingerprint):continue
                    if current.get("authority_sha256")!=authority:continue
                    encoded=json.dumps(current,sort_keys=True,separators=(",",":"),ensure_ascii=False)
                    digest=hashlib.sha256(encoded.encode("utf-8")).hexdigest()
                    if self._decode_managed_rule_result(authority,rules_fingerprint,digest,encoded,
                            expected_rules_fingerprint=rules_fingerprint) is None:continue
                    prepared.append((path,old_rules,old_hash,digest,encoded))
                if not prepared:continue
                if should_continue is not None and not should_continue():return retained
                committed=[]
                with closing(self.connect()) as db,db:
                    db.execute("BEGIN")
                    for path,old_rules,old_hash,digest,encoded in prepared:
                        updated=db.execute("""UPDATE managed_rule_results AS results
                            SET rules_fingerprint=?,result_fingerprint=?,result_json=?
                            WHERE path=? AND rules_fingerprint=? AND result_fingerprint=?
                              AND EXISTS (SELECT 1 FROM managed_scan_checkpoints AS checkpoints
                                JOIN images ON images.path=checkpoints.path
                                WHERE checkpoints.path=results.path AND checkpoints.job_fingerprint=?
                                  AND checkpoints.rules_fingerprint=results.rules_fingerprint
                                  AND checkpoints.image_size=results.image_size AND checkpoints.image_mtime=results.image_mtime
                                  AND checkpoints.sidecar_size=results.sidecar_size AND checkpoints.sidecar_mtime=results.sidecar_mtime
                                  AND images.image_size=results.image_size AND images.image_mtime=results.image_mtime
                                  AND images.sidecar_size=results.sidecar_size AND images.sidecar_mtime=results.sidecar_mtime)""",
                            (str(rules_fingerprint),digest,encoded,path,old_rules,old_hash,str(job_fingerprint)))
                        if updated.rowcount!=1:continue
                        updated=db.execute("""UPDATE managed_scan_checkpoints SET rules_fingerprint=?
                            WHERE path=? AND rules_fingerprint=? AND job_fingerprint=?""",
                            (str(rules_fingerprint),path,old_rules,str(job_fingerprint)))
                        if updated.rowcount!=1:raise sqlite3.IntegrityError("Managed receipt changed during rebind")
                        committed.append(Path(path))
                retained.update(committed)
        except (OSError,sqlite3.DatabaseError,TypeError,ValueError):
            return retained
        return retained

    def trusted_managed_authority_images(
        self,images: list[Path],*,job_fingerprint: str,
        validated_images: set[Path]|None=None,
    ) -> set[Path]:
        """Return current authority completions independent of derived rules.

        A System Knowledge or Automation change invalidates disposable rule
        results, not the separately fingerprinted portable authority job.  A
        remembered library may therefore finish startup from its already-
        validated local catalog and defer only the stale derived projections to
        the preemptible refresh coordinator.  Unvalidated callers retain the
        exact filesystem-signature fallback.
        """
        return self._trusted_managed_images(
            images,rules_fingerprint=None,
            job_fingerprint=str(job_fingerprint),
            validated_images=validated_images,
            require_rule_results=False,
        )

    def _trusted_managed_images(
        self,images: list[Path],*,rules_fingerprint: str|None,
        job_fingerprint: str,validated_images: set[Path]|None,
        require_rule_results: bool,
    ) -> set[Path]:
        wanted={str(Path(image)):Path(image) for image in images}; trusted=set()
        validated={str(Path(image)) for image in (validated_images or set())}
        if not wanted:
            return trusted
        if require_rule_results and rules_fingerprint is None:
            return trusted
        try:
            with closing(self.connect()) as db:
                parameters=[str(job_fingerprint)]
                if require_rule_results:
                    query="""SELECT checkpoints.path,
                                      checkpoints.image_size,checkpoints.image_mtime,
                                      checkpoints.sidecar_size,checkpoints.sidecar_mtime,
                                      images.image_size,images.image_mtime,
                                      images.sidecar_size,images.sidecar_mtime,
                                      results.image_size,results.image_mtime,
                                      results.sidecar_size,results.sidecar_mtime,
                                      results.authority_sha256,results.rules_fingerprint,
                                      results.result_fingerprint,results.result_json
                               FROM managed_scan_checkpoints AS checkpoints
                               LEFT JOIN images ON images.path=checkpoints.path
                               LEFT JOIN managed_rule_results AS results
                                      ON results.path=checkpoints.path
                               WHERE checkpoints.job_fingerprint=?
                                 AND checkpoints.rules_fingerprint=?"""
                    parameters.append(str(rules_fingerprint))
                else:
                    # Authority reuse does not need or fetch potentially large
                    # disposable result JSON.  Keep the tuple shape shared with
                    # the exact-result validation below.
                    query="""SELECT checkpoints.path,
                                      checkpoints.image_size,checkpoints.image_mtime,
                                      checkpoints.sidecar_size,checkpoints.sidecar_mtime,
                                      images.image_size,images.image_mtime,
                                      images.sidecar_size,images.sidecar_mtime,
                                      NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL
                               FROM managed_scan_checkpoints AS checkpoints
                               LEFT JOIN images ON images.path=checkpoints.path
                               WHERE checkpoints.job_fingerprint=?"""
                    if rules_fingerprint is not None:
                        query+=" AND checkpoints.rules_fingerprint=?"
                        parameters.append(str(rules_fingerprint))
                rows=db.execute(query,tuple(parameters))
                for (
                    path,image_size,image_mtime,sidecar_size,sidecar_mtime,
                    cached_image_size,cached_image_mtime,
                    cached_sidecar_size,cached_sidecar_mtime,
                    result_image_size,result_image_mtime,
                    result_sidecar_size,result_sidecar_mtime,
                    authority_sha256,result_rules_fingerprint,
                    result_fingerprint,result_json,
                ) in rows:
                    image=wanted.get(str(path))
                    if image is None:
                        continue
                    try:
                        image_signature=(int(image_size),int(image_mtime))
                        sidecar_signature=(int(sidecar_size),int(sidecar_mtime))
                    except (TypeError,ValueError,OverflowError):
                        continue
                    if require_rule_results:
                        try:
                            result_signatures_match=(
                                image_signature==(
                                    int(result_image_size),int(result_image_mtime),
                                )
                                and sidecar_signature==(
                                    int(result_sidecar_size),int(result_sidecar_mtime),
                                )
                            )
                        except (TypeError,ValueError,OverflowError):
                            result_signatures_match=False
                        if not result_signatures_match or self._decode_managed_rule_result(
                            authority_sha256,result_rules_fingerprint,
                            result_fingerprint,result_json,
                            expected_rules_fingerprint=str(rules_fingerprint),
                        ) is None:
                            continue
                    cache_matches=False
                    if str(path) in validated:
                        try:
                            cache_matches=(
                                image_signature==(
                                    int(cached_image_size),int(cached_image_mtime),
                                )
                                and sidecar_signature==(
                                    int(cached_sidecar_size),int(cached_sidecar_mtime),
                                )
                            )
                        except (TypeError,ValueError,OverflowError):
                            cache_matches=False
                    if cache_matches:
                        trusted.add(image)
                    elif (
                        _signature(image)==image_signature
                        and _signature(image.with_suffix(".tags.json"))==sidecar_signature
                    ):
                        trusted.add(image)
        except sqlite3.DatabaseError:
            # A disposable damaged cache must fail closed.  The ordinary scan
            # will rebuild valid rows from authoritative sidecars.
            return set()
        return trusted

    def record_schema_migration_completion(
        self,image: Path,*,target_version: int,db=None,
    ) -> None:
        """Checkpoint one verified image-sidecar schema upgrade locally."""
        image=Path(image); sidecar=image.with_suffix(".tags.json")
        image_signature=_signature(image); sidecar_signature=_signature(sidecar)
        if image_signature[0]<0 or sidecar_signature[0]<0:
            raise FileNotFoundError(
                f"Schema migration completion cannot be checkpointed for {image}."
            )
        if db is None:
            with closing(self.connect()) as connection, connection:
                self.record_schema_migration_completion(
                    image,target_version=target_version,db=connection,
                )
            return
        db.execute(
            """INSERT OR REPLACE INTO image_schema_migration_checkpoints(
                path,image_size,image_mtime,sidecar_size,sidecar_mtime,target_version
            ) VALUES(?,?,?,?,?,?)""",
            (
                str(image),*image_signature,*sidecar_signature,int(target_version),
            ),
        )

    def trusted_schema_migration_images(
        self,images: list[Path],*,target_version: int,
    ) -> set[Path]:
        """Return unchanged records whose target-schema write was verified."""
        wanted={str(Path(image)):Path(image) for image in images}; trusted=set()
        if not wanted:
            return trusted
        try:
            with closing(self.connect()) as db:
                rows=db.execute(
                    """SELECT path,image_size,image_mtime,sidecar_size,sidecar_mtime
                       FROM image_schema_migration_checkpoints
                       WHERE target_version=?""",
                    (int(target_version),),
                )
                for path,image_size,image_mtime,sidecar_size,sidecar_mtime in rows:
                    image=wanted.get(str(path))
                    if image is None:
                        continue
                    try:
                        image_signature=(int(image_size),int(image_mtime))
                        sidecar_signature=(int(sidecar_size),int(sidecar_mtime))
                    except (TypeError,ValueError,OverflowError):
                        continue
                    if (
                        _signature(image)==image_signature
                        and _signature(image.with_suffix(".tags.json"))==sidecar_signature
                    ):
                        trusted.add(image)
        except sqlite3.DatabaseError:
            # This is a disposable optimization. Corruption must fail closed
            # and make the migration service inspect portable authority again.
            return set()
        return trusted

    def put(self, image: Path, tags: list[str], error: str = "", db=None,
            tag_sources: dict[str, list[str]] | None = None,
            plain_language: Iterable[str] | None = None, *,
            preserve_current_workflow: bool = False) -> CachedImageRecord:
        if db is None:
            with closing(self.connect()) as connection, connection:
                return self.put(
                    image,tags,error,connection,tag_sources,plain_language,
                    preserve_current_workflow=preserve_current_workflow,
                )
        fingerprint = self.fingerprint(image)
        if preserve_current_workflow:
            # A derived-only index rebuild is not an authority change. Keep
            # signature-matched status; its separate context still gates reuse.
            db.execute(
                """DELETE FROM image_workflow_states WHERE path=? AND (
                     image_size<>? OR image_mtime<>? OR caption_size<>? OR
                     caption_mtime<>? OR sidecar_size<>? OR sidecar_mtime<>?)""",
                (str(image),*fingerprint),
            )
        else:
            self.invalidate_workflow_images((image,),db=db)
        tags,sources=_typed_cache_values(list(tags),tag_sources or {})
        descriptions=[str(value) for value in (plain_language or ())]
        db.execute("INSERT OR REPLACE INTO images(path,image_size,image_mtime,caption_size,caption_mtime,sidecar_size,sidecar_mtime,tags_json,tag_sources_json,plain_language_json,error) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(str(image),*fingerprint,json.dumps(tags,ensure_ascii=False),json.dumps(sources,ensure_ascii=False),json.dumps(descriptions,ensure_ascii=False),error))
        self._replace_evidence_tags(db,str(image),tags)
        self._advance_evidence_generation(db)
        self._invalidate_records_cache()
        return CachedImageRecord(
            str(image),fingerprint[2] >= 0,fingerprint[4] >= 0,list(tags),
            error,{str(key):list(values) for key,values in sources.items()},
            descriptions,
        )

    def remove_missing(self, current_images: list[Path]) -> int:
        current={str(path) for path in current_images}
        with closing(self.connect()) as db, db:
            stored=[row[0] for row in db.execute("SELECT path FROM images")]
            missing=[path for path in stored if path not in current]
            checkpoint_paths=[row[0] for row in db.execute("SELECT path FROM managed_scan_checkpoints")]
            missing_checkpoints=[path for path in checkpoint_paths if path not in current]
            rule_result_paths=[row[0] for row in db.execute("SELECT path FROM managed_rule_results")]
            missing_rule_results=[path for path in rule_result_paths if path not in current]
            migration_paths=[
                row[0] for row in db.execute(
                    "SELECT path FROM image_schema_migration_checkpoints"
                )
            ]
            missing_migrations=[path for path in migration_paths if path not in current]
            workflow_paths=[
                row[0] for row in db.execute(
                    "SELECT path FROM image_workflow_states"
                )
            ]
            missing_workflows=[
                path for path in workflow_paths if path not in current
            ]
            db.executemany("DELETE FROM images WHERE path=?",((path,) for path in missing))
            db.executemany(
                "DELETE FROM image_evidence_tags WHERE path=?",
                ((path,) for path in missing),
            )
            db.executemany(
                "DELETE FROM managed_scan_checkpoints WHERE path=?",
                ((path,) for path in missing_checkpoints),
            )
            db.executemany(
                "DELETE FROM managed_rule_results WHERE path=?",
                ((path,) for path in missing_rule_results),
            )
            db.executemany(
                "DELETE FROM image_schema_migration_checkpoints WHERE path=?",
                ((path,) for path in missing_migrations),
            )
            db.executemany(
                "DELETE FROM image_workflow_states WHERE path=?",
                ((path,) for path in missing_workflows),
            )
            if missing:
                self._advance_evidence_generation(db)
        if missing:
            self._invalidate_records_cache()
        return len(missing)

    def query_evidence_groups(
        self,groups: Iterable[Iterable[str]],match_all: bool,*,
        should_continue: Callable[[],bool]|None=None,
        progress: Callable[[int,int,str],None]|None=None,
    ) -> tuple[tuple[str,...],dict[str,tuple[tuple[str,...],...]]]|None:
        """Query the durable normalized Tag index without library access.

        ``None`` means the disposable index could not be read and lets the
        caller attempt its older saved-record fallback. Authoritative image
        files and sidecars are never consulted here.
        """
        canonical_groups=tuple(tuple(group) for group in groups)
        wanted=sorted({value for group in canonical_groups for value in group})
        if not wanted:
            return (),{}
        if should_continue is not None and not should_continue():
            raise InterruptedError("Evidence query cancelled")
        matched_tags:dict[str,set[str]]={}
        try:
            with closing(self.connect()) as db:
                total=int(db.execute("SELECT COUNT(*) FROM images").fetchone()[0])
                for offset in range(0,len(wanted),400):
                    if should_continue is not None and not should_continue():
                        raise InterruptedError("Evidence query cancelled")
                    chunk=wanted[offset:offset+400]
                    placeholders=",".join("?" for _value in chunk)
                    rows=db.execute(
                        f"SELECT path,normalized_tag FROM image_evidence_tags "
                        f"WHERE normalized_tag IN ({placeholders}) ORDER BY path",
                        chunk,
                    )
                    for row_number,(path,tag) in enumerate(rows,1):
                        if (
                            row_number%1024==0
                            and should_continue is not None
                            and not should_continue()
                        ):
                            raise InterruptedError("Evidence query cancelled")
                        matched_tags.setdefault(str(path),set()).add(str(tag))
        except sqlite3.DatabaseError:
            return None
        if should_continue is not None and not should_continue():
            raise InterruptedError("Evidence query cancelled")
        matches=[]; coverage={}
        group_sets=tuple(frozenset(group) for group in canonical_groups)
        for position,path in enumerate(sorted(matched_tags),1):
            if (
                position%256==0
                and should_continue is not None
                and not should_continue()
            ):
                raise InterruptedError("Evidence query cancelled")
            tags=matched_tags[path]
            matched_groups=tuple(
                group for group,group_set in zip(canonical_groups,group_sets)
                if group_set.intersection(tags)
            )
            matched=(
                len(matched_groups)==len(canonical_groups)
                if match_all else bool(matched_groups)
            )
            if matched:
                matches.append(path); coverage[path]=matched_groups
        if progress is not None and total:
            progress(total,total,"Searching indexed Image Knowledge")
        return tuple(matches),coverage

    def confirmed_evidence_paths(
        self,paths: Iterable[str|Path],groups: Iterable[Iterable[str]],*,
        should_continue: Callable[[],bool]|None=None,
        progress: Callable[[int,int,str],None]|None=None,
    ) -> tuple[str,...]:
        """Keep candidates with an exact confirmed tag from the selected groups.

        The durable evidence index narrows by normalized tag first. This second
        bounded query reads source maps only for those candidates so AI, TXT,
        and Metadata evidence cannot trigger System Knowledge rating refreshes.
        """
        candidates=tuple(dict.fromkeys(str(value) for value in paths))
        wanted={normalize_tag(value) for group in groups for value in group}
        wanted.discard("")
        if not candidates or not wanted:return ()
        confirmed=set(); total=len(candidates)
        with closing(self.connect()) as db:
            for offset in range(0,total,400):
                if should_continue is not None and not should_continue():
                    raise InterruptedError("Confirmed evidence query cancelled")
                chunk=candidates[offset:offset+400]
                placeholders=",".join("?" for _value in chunk)
                rows=db.execute(
                    f"SELECT path,tag_sources_json FROM images "
                    f"WHERE path IN ({placeholders})",
                    chunk,
                )
                for path,sources_json in rows:
                    try:sources=json.loads(sources_json or "{}")
                    except (json.JSONDecodeError,TypeError):continue
                    if not isinstance(sources,dict):continue
                    if any(
                        normalize_tag(tag) in wanted
                        and isinstance(values,list)
                        and "Confirmed Image Knowledge" in values
                        for tag,values in sources.items()
                    ):
                        confirmed.add(str(path))
                if progress is not None:
                    progress(
                        min(offset+len(chunk),total),total,
                        "Checking confirmed Image Knowledge",
                    )
        return tuple(path for path in candidates if path in confirmed)

    def _invalidate_records_cache(self) -> None:
        with self._records_lock:
            self._records_cache = None
            self._records_cache_token = None
            self._records_revision += 1

    def all_records(self) -> list[CachedImageRecord]:
        """Return the last saved snapshot without touching the image library."""
        token=self.evidence_snapshot_token()
        valid_token=token[-1]>=0
        with self._records_lock:
            if (self._records_cache is not None and valid_token
                    and self._records_cache_token==token):
                return self._records_cache
            self._records_cache=None
            self._records_cache_token=None
            revision = self._records_revision
        records=[]
        with closing(self.connect()) as db:
            rows=db.execute(
                "SELECT path,caption_size,sidecar_size,tags_json,tag_sources_json,plain_language_json,error FROM images ORDER BY path"
            )
            records.extend(self._cached_records_from_rows(rows))
        # Local invalidation can precede commit, and another index instance can
        # commit without changing our memory revision. Bind reuse to committed
        # evidence too; a failed token read never authorizes cached rows.
        final_token=self.evidence_snapshot_token()
        with self._records_lock:
            if (revision == self._records_revision and valid_token
                    and final_token==token):
                self._records_cache = records
                self._records_cache_token = token
        return records

    def iter_records(self, *, should_cancel: Callable[[], bool] | None = None,
                     batch_size: int = 256):
        """Stream disposable rows for bounded background scans without caching all rows."""
        if batch_size < 1:
            raise ValueError("Choose a positive index batch size.")
        with closing(self.connect()) as db:
            rows = db.execute(
                "SELECT path,caption_size,sidecar_size,tags_json,tag_sources_json,"
                "plain_language_json,error FROM images ORDER BY path"
            )
            while True:
                if should_cancel is not None and should_cancel():
                    raise InterruptedError("Indexed image scan cancelled.")
                batch = rows.fetchmany(batch_size)
                if not batch:
                    break
                for record in self._cached_records_from_rows(batch):
                    if should_cancel is not None and should_cancel():
                        raise InterruptedError("Indexed image scan cancelled.")
                    yield record

    @staticmethod
    def _cached_records_from_rows(rows) -> list[CachedImageRecord]:
        """Decode disposable index rows without changing full-snapshot caching."""
        records=[]
        for path,caption_size,sidecar_size,tags_json,tag_sources_json,plain_language_json,error in rows:
            try:
                tags=[str(value) for value in json.loads(tags_json)]
            except (json.JSONDecodeError,TypeError):
                tags=[]
            try:
                sources={
                    str(key):[str(value) for value in values if str(value).strip()]
                    for key,values in json.loads(tag_sources_json or "{}").items()
                    if isinstance(values,list)
                }
            except (json.JSONDecodeError,TypeError,AttributeError):
                sources={}
            try:
                plain_language=[
                    str(value) for value in json.loads(plain_language_json)
                ] if plain_language_json is not None else []
            except (json.JSONDecodeError,TypeError):
                plain_language=[]
            tags,sources=_typed_cache_values(tags,sources)
            records.append(CachedImageRecord(
                str(path),int(caption_size)>=0,int(sidecar_size)>=0,tags,
                str(error or ""),sources,plain_language,
            ))
        return records

    def records_for_paths(
        self,paths: Iterable[str|Path],db=None,
    ) -> dict[Path,CachedImageRecord]:
        """Return only requested disposable rows, keyed by their saved paths.

        This is deliberately uncached: callers that already own a SQLite
        transaction can pass it through ``db`` and receive its current rows,
        while the full interactive snapshot remains owned by ``all_records``.
        Missing and repeated requested paths are omitted.  Requests are split
        below SQLite's parameter limit, so a broad caller still has bounded
        query size without materializing unrelated rows.
        """
        requested=tuple(dict.fromkeys(
            str(Path(value)) for value in paths if value
        ))
        if not requested:
            return {}
        if db is None:
            with closing(self.connect()) as connection:
                return self.records_for_paths(requested,connection)
        records={}
        for offset in range(0,len(requested),400):
            chunk=requested[offset:offset+400]
            placeholders=",".join("?" for _value in chunk)
            rows=db.execute(
                "SELECT path,caption_size,sidecar_size,tags_json,tag_sources_json,"
                "plain_language_json,error FROM images "
                f"WHERE path IN ({placeholders})",
                chunk,
            )
            records.update({
                Path(record.path):record
                for record in self._cached_records_from_rows(rows)
            })
        return {
            Path(path):records[Path(path)]
            for path in requested if Path(path) in records
        }

    def cached_record(self,image: Path,db=None) -> CachedImageRecord|None:
        """Return one disposable cache row without materializing the full index."""
        if db is None:
            with closing(self.connect()) as connection:
                return self.cached_record(image,connection)
        row=db.execute(
            "SELECT path,caption_size,sidecar_size,tags_json,tag_sources_json,"
            "plain_language_json,error FROM images WHERE path=?",
            (str(Path(image)),),
        ).fetchone()
        if row is None:
            return None
        return self._cached_records_from_rows((row,))[0]

    def records_by_path(self) -> dict[Path, CachedImageRecord]:
        """Return a reusable path lookup for interactive filtering and review."""
        return {Path(record.path): record for record in self.all_records()}
