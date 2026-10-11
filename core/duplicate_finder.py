from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from array import array
from concurrent.futures import FIRST_COMPLETED,ThreadPoolExecutor,wait
from contextlib import closing
from dataclasses import dataclass,field,replace
from datetime import datetime,timezone
from pathlib import Path
from threading import RLock
from typing import Callable,Iterable,Mapping
from uuid import uuid4

from PIL import Image,ImageOps

from .dataset_scope import IMG_EXTENSIONS
from .image_knowledge_completeness import ImageKnowledgeCompletenessService
from .image_record import ImageRecord


DUPLICATE_INDEX_SCHEMA_VERSION=1
DUPLICATE_ALGORITHM_VERSION=2
MIN_SIMILARITY_PERCENT=70
DEFAULT_SIMILARITY_PERCENT=95
MAX_RELATIONS=100_000
MAX_CANDIDATE_EVALUATIONS=4_000_000
MAX_VISUAL_INDEX_PROBES=20_000_000
MAX_FINGERPRINT_WORKERS=4
FINGERPRINT_COMMIT_BATCH=128
MAX_BADGE_EVIDENCE=100_000
MAX_SESSION_REPORTS=8
_FEATURE_RATIOS=(1.0,.90,.75,.60)
_FEATURE_BITS=256


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class DuplicateScopeItem:
    identity: str
    path: Path
    source_id: str
    relative_path: str
    source_label: str
    source_root: str
    source_profile_id: str=""


@dataclass(frozen=True)
class DuplicateScope:
    id: str
    label: str
    items: tuple[DuplicateScopeItem,...]


@dataclass(frozen=True)
class DuplicateFingerprint:
    identity: str
    scope_id: str
    path: Path
    source_id: str
    relative_path: str
    source_label: str
    source_root: str
    source_profile_id: str
    size: int
    mtime_ns: int
    ctime_ns: int
    sample_sha256: str
    byte_sha256: str
    pixel_sha256: str
    perceptual_hashes: tuple[int,...]
    feature_labels: tuple[str,...]
    width: int
    height: int
    format: str
    mode: str
    updated_at: str
    error: str=""
    algorithm_version: int=DUPLICATE_ALGORITHM_VERSION

    @property
    def pixel_count(self) -> int:
        return max(0,self.width)*max(0,self.height)

    @property
    def usable(self) -> bool:
        return bool(
            not self.error and self.byte_sha256 and self.pixel_sha256
            and self.perceptual_hashes
        )


@dataclass(frozen=True)
class DuplicateRelation:
    first_identity: str
    second_identity: str
    kind: str
    similarity: float
    evidence: str

    @property
    def label(self) -> str:
        return {
            "byte_exact":"Byte-identical",
            "pixel_exact":"Decoded pixels identical",
            "similar":"Visually similar candidate",
        }.get(self.kind,"Similarity candidate")


@dataclass(frozen=True)
class DuplicateGroup:
    id: str
    member_identities: tuple[str,...]
    relations: tuple[DuplicateRelation,...]

    @property
    def exact_relation_count(self) -> int:
        return sum(
            relation.kind in {"byte_exact","pixel_exact"}
            for relation in self.relations
        )

    @property
    def similarity_relation_count(self) -> int:
        return sum(relation.kind=="similar" for relation in self.relations)


@dataclass(frozen=True)
class DuplicateScanIssue:
    path: Path
    code: str
    detail: str


@dataclass(frozen=True)
class DuplicateSearchReport:
    scope_id: str
    scope_label: str
    lower_similarity: float
    upper_similarity: float
    records: Mapping[str,DuplicateFingerprint]
    groups: tuple[DuplicateGroup,...]
    issues: tuple[DuplicateScanIssue,...]=()
    indexed: int=0
    reused: int=0
    removed: int=0
    truncated: bool=False
    stale_identities: tuple[str,...]=()
    candidate_evaluations: int=0
    candidate_budget: int=MAX_CANDIDATE_EVALUATIONS
    visual_index_probes: int=0
    visual_index_probe_budget: int=MAX_VISUAL_INDEX_PROBES
    truncation_reason: str=""
    include_similar: bool=True
    incoming_identities: frozenset[str]|None=None

    @property
    def matched_images(self) -> int:
        return len({
            identity for group in self.groups
            for identity in group.member_identities
        })


@dataclass(frozen=True)
class DuplicateBadgeEvidence:
    """Small session-only projection of one completed duplicate comparison."""

    kind: str
    match_count: int
    exact_relation_count: int
    visual_relation_count: int
    scope_label: str
    checked_at: str
    truncated: bool=False


@dataclass(frozen=True)
class DuplicateDistinctDecision:
    key: str
    created_at: str
    reason: str
    first_identity: str
    first_path: Path
    second_identity: str
    second_path: Path


@dataclass(frozen=True)
class DuplicateMemberDetails:
    identity: str
    completeness_score: int|None
    completeness_label: str
    metadata_summary: str
    provenance_summary: str
    derivative_summary: str
    error: str=""
    has_tags: bool|None=None
    has_descriptions: bool|None=None
    visual_status: str="neutral"


@dataclass(frozen=True)
class DuplicateReferenceRecommendation:
    identity: str=""
    rationale: str=(
        "Both images remain set to Keep until the user explicitly chooses one "
        "for the recoverable Recycle Bin preview."
    )


def _safe_identity_part(value: object) -> str:
    return str(value or "").replace("\\","/").strip().casefold()


def duplicate_scope_item(
    path: str|Path,*,source_id: str,source_root: str|Path,
    source_label: str="",source_profile_id: str="",
) -> DuplicateScopeItem:
    image=Path(path).expanduser().resolve()
    root=Path(source_root).expanduser().resolve()
    try:
        relative=image.relative_to(root).as_posix()
    except ValueError:
        relative=image.name
    identity=f"{_safe_identity_part(source_id)}:{_safe_identity_part(relative)}"
    return DuplicateScopeItem(
        identity,image,str(source_id),relative,str(source_label or source_id),
        str(root),str(source_profile_id or ""),
    )


class DuplicateIndex:
    """Rebuildable machine-local byte, pixel, and perceptual fingerprint cache."""

    def __init__(
        self,path: str|Path,*,max_workers: int|None=None,
        write_batch_size: int=FINGERPRINT_COMMIT_BATCH,
    ):
        self.path=Path(path)
        available=max(1,int(os.cpu_count() or 1)-1)
        self.max_workers=max(1,min(
            MAX_FINGERPRINT_WORKERS,available,
            int(max_workers) if max_workers is not None else available,
        ))
        self.write_batch_size=max(1,int(write_batch_size))
        self.path.parent.mkdir(parents=True,exist_ok=True)
        try:self._initialize()
        except sqlite3.DatabaseError as exc:
            corrupt_codes={
                getattr(sqlite3,"SQLITE_CORRUPT",11),
                getattr(sqlite3,"SQLITE_NOTADB",26),
            }
            if getattr(exc,"sqlite_errorcode",None) not in corrupt_codes:
                raise
            for value in (
                self.path,
                self.path.with_name(self.path.name+"-wal"),
                self.path.with_name(self.path.name+"-shm"),
            ):
                value.unlink(missing_ok=True)
            self._initialize()

    def connect(self):
        db=sqlite3.connect(self.path,timeout=30)
        db.row_factory=sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _initialize(self) -> None:
        with closing(self.connect()) as db:
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            current=db.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            if current is not None and int(current[0])!=DUPLICATE_INDEX_SCHEMA_VERSION:
                db.execute("DROP TABLE IF EXISTS fingerprints")
            db.execute("""CREATE TABLE IF NOT EXISTS fingerprints (
                identity TEXT PRIMARY KEY,
                scope_id TEXT NOT NULL,
                path TEXT NOT NULL,
                source_id TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                source_label TEXT NOT NULL,
                source_root TEXT NOT NULL,
                source_profile_id TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                ctime_ns INTEGER NOT NULL,
                sample_sha256 TEXT NOT NULL,
                byte_sha256 TEXT NOT NULL,
                pixel_sha256 TEXT NOT NULL,
                perceptual_json TEXT NOT NULL,
                feature_labels_json TEXT NOT NULL,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                format TEXT NOT NULL,
                mode TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error TEXT NOT NULL,
                algorithm_version INTEGER NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS fingerprint_scope ON fingerprints(scope_id)")
            db.execute("CREATE INDEX IF NOT EXISTS fingerprint_bytes ON fingerprints(byte_sha256)")
            db.execute("CREATE INDEX IF NOT EXISTS fingerprint_pixels ON fingerprints(pixel_sha256)")
            db.execute(
                "INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_version',?)",
                (str(DUPLICATE_INDEX_SCHEMA_VERSION),),
            )
            db.commit()

    @staticmethod
    def _record(row) -> DuplicateFingerprint:
        return DuplicateFingerprint(
            str(row["identity"]),str(row["scope_id"]),Path(row["path"]),
            str(row["source_id"]),str(row["relative_path"]),
            str(row["source_label"]),str(row["source_root"]),
            str(row["source_profile_id"]),int(row["size"]),
            int(row["mtime_ns"]),int(row["ctime_ns"]),
            str(row["sample_sha256"]),str(row["byte_sha256"]),
            str(row["pixel_sha256"]),tuple(
                int(value,16) for value in json.loads(row["perceptual_json"])
            ),tuple(str(value) for value in json.loads(row["feature_labels_json"])),
            int(row["width"]),int(row["height"]),str(row["format"]),
            str(row["mode"]),str(row["updated_at"]),str(row["error"]),
            int(row["algorithm_version"]),
        )

    def get(self,identity: str,db=None) -> DuplicateFingerprint|None:
        owned=db is None
        connection=db or self.connect()
        try:
            row=connection.execute(
                "SELECT * FROM fingerprints WHERE identity=?",(str(identity),)
            ).fetchone()
            return self._record(row) if row is not None else None
        finally:
            if owned:connection.close()

    def get_many(
        self,identities: Iterable[str],db=None,
    ) -> dict[str,DuplicateFingerprint]:
        """Load current-scope cache rows in bounded SQLite queries."""
        values=tuple(dict.fromkeys(str(value) for value in identities))
        owned=db is None
        connection=db or self.connect()
        records={}
        try:
            for offset in range(0,len(values),500):
                chunk=values[offset:offset+500]
                placeholders=",".join("?" for _value in chunk)
                rows=connection.execute(
                    f"SELECT * FROM fingerprints WHERE identity IN ({placeholders})",
                    chunk,
                ).fetchall()
                records.update(
                    (str(row["identity"]),self._record(row)) for row in rows
                )
            return records
        finally:
            if owned:connection.close()

    def records_for_scope(self,scope_id: str) -> tuple[DuplicateFingerprint,...]:
        with closing(self.connect()) as db:
            rows=db.execute(
                "SELECT * FROM fingerprints WHERE scope_id=? ORDER BY identity",
                (str(scope_id),),
            ).fetchall()
        return tuple(self._record(row) for row in rows)

    def put(
        self,record: DuplicateFingerprint,db=None,*,commit: bool=True,
    ) -> None:
        values=(
            record.identity,record.scope_id,str(record.path),record.source_id,
            record.relative_path,record.source_label,record.source_root,
            record.source_profile_id,record.size,record.mtime_ns,record.ctime_ns,
            record.sample_sha256,record.byte_sha256,record.pixel_sha256,
            json.dumps([f"{value:064x}" for value in record.perceptual_hashes]),
            json.dumps(record.feature_labels),record.width,record.height,
            record.format,record.mode,record.updated_at,record.error,
            record.algorithm_version,
        )
        owned=db is None
        connection=db or self.connect()
        try:
            connection.execute("""INSERT OR REPLACE INTO fingerprints(
                identity,scope_id,path,source_id,relative_path,source_label,
                source_root,source_profile_id,size,mtime_ns,ctime_ns,sample_sha256,
                byte_sha256,pixel_sha256,perceptual_json,feature_labels_json,
                width,height,format,mode,updated_at,error,algorithm_version
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",values)
            if commit:connection.commit()
        finally:
            if owned:connection.close()

    def remove_missing(self,scope_id: str,identities: Iterable[str]) -> int:
        keep=set(str(value) for value in identities)
        with closing(self.connect()) as db:
            current={
                str(row[0]) for row in db.execute(
                    "SELECT identity FROM fingerprints WHERE scope_id=?",
                    (str(scope_id),),
                )
            }
            missing=tuple((value,) for value in current-keep)
            if missing:
                db.executemany("DELETE FROM fingerprints WHERE identity=?",missing)
                db.commit()
            return len(missing)

    def clear(self) -> None:
        with closing(self.connect()) as db:
            db.execute("DELETE FROM fingerprints")
            db.commit()

    @staticmethod
    def _sample_signature(path: Path) -> tuple[int,int,int,str]:
        before=path.stat()
        digest=hashlib.sha256()
        digest.update(str(before.st_size).encode("ascii"))
        with path.open("rb") as stream:
            offsets=(0,max(0,before.st_size//2-32_768),max(0,before.st_size-65_536))
            for offset in dict.fromkeys(offsets):
                stream.seek(offset)
                payload=stream.read(65_536)
                digest.update(str(offset).encode("ascii")); digest.update(payload)
        after=path.stat()
        if (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(
            after.st_size,after.st_mtime_ns,after.st_ctime_ns
        ):
            raise RuntimeError(f"{path.name} changed while its fingerprint was checked.")
        return after.st_size,after.st_mtime_ns,after.st_ctime_ns,digest.hexdigest()

    @staticmethod
    def is_current(record: DuplicateFingerprint) -> bool:
        try:
            signature=DuplicateIndex._sample_signature(record.path)
        except (OSError,RuntimeError):
            return False
        return signature==(
            record.size,record.mtime_ns,record.ctime_ns,record.sample_sha256
        )

    @staticmethod
    def _hash_bytes(
        path: Path,should_cancel: Callable[[],bool]|None=None,
    ) -> str:
        before=path.stat(); digest=hashlib.sha256(); length=0
        with path.open("rb") as stream:
            while chunk:=stream.read(1024*1024):
                if should_cancel and should_cancel():
                    raise InterruptedError("Duplicate indexing cancelled safely; completed fingerprints were retained.")
                digest.update(chunk); length+=len(chunk)
        after=path.stat()
        if (
            (before.st_size,before.st_mtime_ns,before.st_ctime_ns)
            !=(after.st_size,after.st_mtime_ns,after.st_ctime_ns)
            or length!=after.st_size
        ):
            raise RuntimeError(f"{path.name} changed while it was indexed.")
        return digest.hexdigest()

    @staticmethod
    def _dhash_grid(scaled: Image.Image) -> int:
        pixels=scaled.tobytes(); value=0
        for row in range(16):
            offset=row*17
            for column in range(16):
                value=(value<<1)|int(pixels[offset+column]>pixels[offset+column+1])
        return value

    @classmethod
    def _dhash(cls,image: Image.Image) -> int:
        return cls._dhash_grid(
            image.convert("L").resize((17,17),Image.Resampling.LANCZOS)
        )

    @classmethod
    def _canonical_feature(cls,image: Image.Image,ratio: float) -> int:
        candidate=image
        if ratio<1:
            width,height=image.size
            crop_width=max(2,round(width*ratio)); crop_height=max(2,round(height*ratio))
            left=max(0,(width-crop_width)//2); top=max(0,(height-crop_height)//2)
            candidate=image.crop((left,top,left+crop_width,top+crop_height))
        # Resize once per crop. Rotating the small square grid is equivalent for
        # this orientation-canonical heuristic and avoids four costly Lanczos
        # resizes for every feature.
        scaled=candidate.convert("L").resize((17,17),Image.Resampling.LANCZOS)
        rotations=(
            scaled,
            scaled.transpose(Image.Transpose.ROTATE_90),
            scaled.transpose(Image.Transpose.ROTATE_180),
            scaled.transpose(Image.Transpose.ROTATE_270),
        )
        return min(cls._dhash_grid(value) for value in rotations)

    @classmethod
    def _decode(cls,path: Path) -> tuple[str,tuple[int,...],tuple[str,...],int,int,str,str]:
        with Image.open(path) as opened:
            if bool(getattr(opened,"is_animated",False)) or int(getattr(opened,"n_frames",1))>1:
                raise ValueError("Animated images are excluded from duplicate comparison.")
            image_format=str(opened.format or path.suffix.lstrip(".")).upper()
            image=ImageOps.exif_transpose(opened); image.load()
            width,height=image.size; original_mode=str(image.mode or "")
            rgba=image.convert("RGBA")
            digest=hashlib.sha256()
            digest.update(f"{width}x{height}:RGBA:".encode("ascii")); digest.update(rgba.tobytes())
            features=[]; labels=[]
            for ratio in _FEATURE_RATIOS:
                value=cls._canonical_feature(rgba,ratio)
                if value in features:
                    continue
                features.append(value)
                labels.append("full frame" if ratio==1 else f"center {round(ratio*100)}% crop")
            return digest.hexdigest(),tuple(features),tuple(labels),width,height,image_format,original_mode

    @classmethod
    def fingerprint(
        cls,item: DuplicateScopeItem,scope_id: str,signature=None,
        should_cancel: Callable[[],bool]|None=None,
    ) -> DuplicateFingerprint:
        path=Path(item.path)
        signature=signature or cls._sample_signature(path)
        size,mtime_ns,ctime_ns,sample=signature
        byte_hash=""; pixel_hash=""; features=(); labels=(); width=height=0
        image_format=path.suffix.lstrip(".").upper(); mode=""; error=""
        try:
            if path.suffix.casefold() not in IMG_EXTENSIONS:
                raise ValueError("Unsupported or video file type excluded from duplicate comparison.")
            byte_hash=cls._hash_bytes(path,should_cancel)
            if should_cancel and should_cancel():
                raise InterruptedError("Duplicate indexing cancelled safely; completed fingerprints were retained.")
            pixel_hash,features,labels,width,height,image_format,mode=cls._decode(path)
        except InterruptedError:
            raise
        except Exception as exc:
            error=str(exc)
        return DuplicateFingerprint(
            item.identity,str(scope_id),path,item.source_id,item.relative_path,
            item.source_label,item.source_root,item.source_profile_id,size,
            mtime_ns,ctime_ns,sample,byte_hash,pixel_hash,features,labels,
            width,height,image_format,mode,_utc_now(),error,
        )

    def _evaluate_item(
        self,item: DuplicateScopeItem,scope_id: str,
        cached: DuplicateFingerprint|None,force: bool,
        should_cancel: Callable[[],bool]|None=None,
    ) -> tuple[str,DuplicateFingerprint]:
        signature=self._sample_signature(item.path)
        if (
            not force and cached is not None
            and cached.algorithm_version==DUPLICATE_ALGORITHM_VERSION
            and signature==(cached.size,cached.mtime_ns,cached.ctime_ns,cached.sample_sha256)
        ):
            return "reused",replace(
                cached,path=Path(item.path),scope_id=scope_id,
                source_id=item.source_id,relative_path=item.relative_path,
                source_label=item.source_label,source_root=item.source_root,
                source_profile_id=item.source_profile_id,
            )
        return "indexed",self.fingerprint(
            item,scope_id,signature,should_cancel=should_cancel,
        )

    def scan(
        self,scope: DuplicateScope,*,force: bool=False,
        progress: Callable[[int,int,str],None]|None=None,
        should_cancel: Callable[[],bool]|None=None,
    ) -> tuple[dict[str,DuplicateFingerprint],tuple[DuplicateScanIssue,...],int,int,int,tuple[str,...]]:
        records={}; issues=[]; stale=set(); reused=indexed=0; total=len(scope.items)
        usable_reused=usable_indexed=0
        completed=0; iterator=iter(scope.items); exhausted=False; pending={}
        writes_since_commit=0
        executor=ThreadPoolExecutor(
            max_workers=self.max_workers,thread_name_prefix="duplicate-fingerprint",
        )
        try:
            with closing(self.connect()) as db:
                cached_records=self.get_many(
                    (item.identity for item in scope.items),db,
                )
                try:
                    while not exhausted or pending:
                        if should_cancel and should_cancel():
                            raise InterruptedError("Duplicate indexing cancelled safely; completed fingerprints were retained.")
                        while not exhausted and len(pending)<self.max_workers*2:
                            try:item=next(iterator)
                            except StopIteration:
                                exhausted=True; break
                            cached=cached_records.get(item.identity)
                            future=executor.submit(
                                self._evaluate_item,item,scope.id,cached,bool(force),
                                should_cancel,
                            )
                            pending[future]=(item,cached)
                        if not pending:continue
                        done,_remaining=wait(tuple(pending),return_when=FIRST_COMPLETED)
                        for future in done:
                            item,cached=pending.pop(future)
                            try:
                                outcome,record=future.result()
                                if record!=cached:
                                    self.put(record,db,commit=False)
                                    writes_since_commit+=1
                                if outcome=="reused":reused+=1
                                else:indexed+=1
                                if not record.error:
                                    if outcome=="reused":usable_reused+=1
                                    else:usable_indexed+=1
                                records[item.identity]=record
                                if record.error:
                                    issues.append(DuplicateScanIssue(record.path,"decode_error",record.error))
                            except InterruptedError:
                                raise
                            except Exception as exc:
                                if cached is not None:
                                    records[item.identity]=replace(
                                        cached,path=Path(item.path),scope_id=scope.id,
                                        source_id=item.source_id,relative_path=item.relative_path,
                                        source_label=item.source_label,source_root=item.source_root,
                                        source_profile_id=item.source_profile_id,
                                    )
                                    stale.add(item.identity)
                                    issues.append(DuplicateScanIssue(
                                        Path(item.path),"stale_cached",
                                        f"Current pixels are unavailable; retained the saved local fingerprint for browsing only. {exc}",
                                    ))
                                else:
                                    issues.append(DuplicateScanIssue(Path(item.path),"unavailable",str(exc)))
                            completed+=1
                            if progress:
                                progress(completed,total,f"Checking duplicate fingerprints: {usable_reused:,} reused, {usable_indexed:,} new/refreshed, {len(issues):,} held • {self.max_workers} worker(s)")
                            if writes_since_commit>=self.write_batch_size:
                                db.commit(); writes_since_commit=0
                    db.commit(); writes_since_commit=0
                except InterruptedError:
                    db.commit(); writes_since_commit=0
                    raise
        finally:
            executor.shutdown(wait=True,cancel_futures=True)
        removed=self.remove_missing(scope.id,(item.identity for item in scope.items))
        return records,tuple(issues),indexed,reused,removed,tuple(sorted(stale))


class DuplicateDecisionStore:
    """Durable reviewed pair decisions; unlike fingerprints, these are not cache."""

    SCHEMA_VERSION=1

    def __init__(self,path: str|Path):
        self.path=Path(path); self.backup=self.path.with_suffix(self.path.suffix+".bak")
        self._lock=RLock(); self._decision_keys=frozenset()
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.extra_fields={}; self.decisions={}; self.issues=[]; self._load()

    @staticmethod
    def pair_key(first: DuplicateFingerprint,second: DuplicateFingerprint) -> str:
        identities=sorted((
            f"{first.identity}|{first.byte_sha256}|{first.pixel_sha256}",
            f"{second.identity}|{second.byte_sha256}|{second.pixel_sha256}",
        ))
        return hashlib.sha256("\n".join(identities).encode("utf-8")).hexdigest()

    def _load_payload(self,path: Path):
        value=json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value,dict):raise ValueError("decision store root is not an object")
        return value

    def _load(self) -> None:
        payload={}
        if self.path.exists():
            try:payload=self._load_payload(self.path)
            except Exception as exc:
                self.issues.append(f"Current duplicate decisions were damaged: {exc}")
                if self.backup.exists():
                    try:
                        payload=self._load_payload(self.backup)
                        self.issues.append("Loaded the verified previous duplicate decisions.")
                    except Exception as backup_exc:
                        self.issues.append(f"Previous duplicate decisions were unavailable: {backup_exc}")
        self.extra_fields={
            str(key):value for key,value in payload.items()
            if key not in {"schema_version","decisions"}
        }
        values=payload.get("decisions",{})
        self.decisions=dict(values) if isinstance(values,dict) else {}
        self._decision_keys=frozenset(self.decisions)

    def is_distinct(self,first: DuplicateFingerprint,second: DuplicateFingerprint) -> bool:
        return self.pair_key(first,second) in self._decision_keys

    def snapshot_keys(self) -> frozenset[str]:
        """One immutable current-content decision view for read-only consumers."""
        return self._decision_keys

    def history(self) -> tuple[DuplicateDistinctDecision,...]:
        with self._lock:
            rows=[]
            for key,value in self.decisions.items():
                if not isinstance(value,dict) or value.get("decision")!="distinct":
                    continue
                first=value.get("first",{}); second=value.get("second",{})
                if not isinstance(first,dict) or not isinstance(second,dict):
                    continue
                rows.append(DuplicateDistinctDecision(
                    str(key),str(value.get("created_at") or ""),
                    str(value.get("reason") or "Reviewed as intentionally distinct"),
                    str(first.get("identity") or ""),Path(str(first.get("path") or "")),
                    str(second.get("identity") or ""),Path(str(second.get("path") or "")),
                ))
            return tuple(sorted(rows,key=lambda row:(row.created_at,row.key),reverse=True))

    def remove_key(self,key: str) -> bool:
        with self._lock:
            key=str(key)
            if key not in self.decisions:return False
            previous=self.decisions.pop(key)
            try:self.save()
            except Exception:
                self.decisions[key]=previous; raise
            return True

    def mark_distinct(
        self,first: DuplicateFingerprint,second: DuplicateFingerprint,
        *,reason: str="Reviewed as intentionally distinct",
    ) -> str:
        if first.identity==second.identity:raise ValueError("Choose two different images.")
        with self._lock:
            key=self.pair_key(first,second)
            missing=object(); previous=self.decisions.get(key,missing)
            self.decisions[key]={
                "decision":"distinct","created_at":_utc_now(),"reason":str(reason),
                "first":{"identity":first.identity,"path":str(first.path),"byte_sha256":first.byte_sha256,"pixel_sha256":first.pixel_sha256},
                "second":{"identity":second.identity,"path":str(second.path),"byte_sha256":second.byte_sha256,"pixel_sha256":second.pixel_sha256},
            }
            try:self.save()
            except Exception:
                if previous is missing:self.decisions.pop(key,None)
                else:self.decisions[key]=previous
                raise
            return key

    def unmark_distinct(
        self,first: DuplicateFingerprint,second: DuplicateFingerprint,
    ) -> bool:
        return self.remove_key(self.pair_key(first,second))

    def save(self) -> None:
        with self._lock:
            payload={**self.extra_fields,"schema_version":self.SCHEMA_VERSION,"decisions":self.decisions}
            encoded=json.dumps(payload,ensure_ascii=False,indent=2,sort_keys=True)+"\n"
            temporary=self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
            try:
                with temporary.open("x",encoding="utf-8",newline="\n") as stream:
                    stream.write(encoded); stream.flush(); os.fsync(stream.fileno())
                if self.path.exists():
                    previous=self.path.read_bytes()
                    backup_tmp=self.backup.with_name(f".{self.backup.name}.{uuid4().hex}.tmp")
                    try:
                        with backup_tmp.open("xb") as stream:
                            stream.write(previous); stream.flush(); os.fsync(stream.fileno())
                        os.replace(backup_tmp,self.backup)
                    finally:
                        if backup_tmp.exists():backup_tmp.unlink()
                os.replace(temporary,self.path)
                verified=self._load_payload(self.path)
                if verified.get("decisions")!=self.decisions:
                    raise RuntimeError("Duplicate decisions did not verify after writing.")
                self._decision_keys=frozenset(self.decisions)
            finally:
                if temporary.exists():temporary.unlink()


class _PerceptualBandIndex:
    """Deterministic locality index for bounded perceptual candidate lookup."""

    _MASK=(1<<_FEATURE_BITS)-1
    _SHIFTS=(0,3,5,8,11,23)
    _MIN_BAND_MATCHES=2

    def __init__(self,lower_similarity: float):
        # Broad ranges use narrower bands to retain candidate recall. The
        # displayed Hamming score remains the final decision; a band is only a
        # cheap candidate route and never exact evidence.
        # Six rotated 14-bit partitions keep the broader profiled 88% boundary highly
        # recallable. Requiring two corroborating bands rejects the abundant
        # one-band collisions that made four-feature libraries near-quadratic.
        self.width=14 if float(lower_similarity)>=85 else 10
        self.shifts=self._SHIFTS
        self.band_mask=(1<<self.width)-1
        self.band_count=len(range(0,_FEATURE_BITS-self.width+1,self.width))
        self.prefix_count=len(self.shifts)*self.band_count
        # A packed adjacency list avoids millions of Python dict/list/int
        # objects at library scale. Each head/node is a signed 32-bit offset;
        # records are unsigned 32-bit positions in the ordered scan.
        self.heads=array("i",[-1])*(self.prefix_count*(1<<self.width))
        self.records=array("I")
        self.next=array("i")
        self.match_counts=bytearray()
        self.match_generations=array("I")
        self.query_generation=0

    def _keys(self,value: int):
        for shift_index,shift in enumerate(self.shifts):
            rotated=(value<<shift)&self._MASK
            if shift:rotated|=value>>(_FEATURE_BITS-shift)
            for band_index,offset in enumerate(range(0,_FEATURE_BITS-self.width+1,self.width)):
                prefix=shift_index*self.band_count+band_index
                yield (prefix<<self.width)|((rotated>>offset)&self.band_mask)

    def add(self,value: int,record_index: int) -> None:
        if record_index>=len(self.match_counts):
            missing=record_index+1-len(self.match_counts)
            self.match_counts.extend(b"\0"*missing)
            self.match_generations.extend(array("I",[0])*missing)
        for key in self._keys(value):
            head=self.heads[key]
            if head>=0 and self.records[head]==record_index:
                continue
            self.records.append(record_index)
            self.next.append(head)
            self.heads[key]=len(self.records)-1

    def query(
        self,value: int,*,consume_probe: Callable[[],bool]|None=None,
    ) -> tuple[set[int],bool]:
        self.query_generation+=1
        generation=self.query_generation
        found=set()
        for key in self._keys(value):
            node=self.heads[key]
            while node>=0:
                if consume_probe is not None and not consume_probe():
                    return found,True
                record_index=self.records[node]
                if record_index not in found:
                    if self.match_generations[record_index]!=generation:
                        self.match_generations[record_index]=generation
                        count=1
                    else:
                        count=self.match_counts[record_index]+1
                    if count>=self._MIN_BAND_MATCHES:
                        found.add(record_index)
                    else:
                        self.match_counts[record_index]=count
                node=self.next[node]
        return found,False


def perceptual_similarity(
    first: DuplicateFingerprint,second: DuplicateFingerprint,
) -> tuple[float,str]:
    best_distance=_FEATURE_BITS+1; best=(0,0)
    for first_index,first_hash in enumerate(first.perceptual_hashes):
        for second_index,second_hash in enumerate(second.perceptual_hashes):
            distance=(first_hash^second_hash).bit_count()
            if distance<best_distance:
                best_distance=distance; best=(first_index,second_index)
    if best_distance>_FEATURE_BITS:return 0.0,"No perceptual fingerprint"
    score=max(0.0,100.0*(1-best_distance/_FEATURE_BITS))
    first_label=first.feature_labels[best[0]] if best[0]<len(first.feature_labels) else "frame"
    second_label=second.feature_labels[best[1]] if best[1]<len(second.feature_labels) else "frame"
    return score,f"{best_distance}/{_FEATURE_BITS} perceptual bits differ ({first_label} ↔ {second_label})"


def relation_between(
    first: DuplicateFingerprint,second: DuplicateFingerprint,
) -> DuplicateRelation:
    identities=(first.identity,second.identity)
    if first.byte_sha256 and first.byte_sha256==second.byte_sha256:
        return DuplicateRelation(*identities,"byte_exact",100.0,"The complete file bytes have the same SHA-256 digest.")
    if first.pixel_sha256 and first.pixel_sha256==second.pixel_sha256:
        return DuplicateRelation(*identities,"pixel_exact",100.0,"Decoded, orientation-normalized RGBA pixels are identical; file bytes or Metadata differ.")
    score,evidence=perceptual_similarity(first,second)
    return DuplicateRelation(*identities,"similar",score,evidence)


class _UnionFind:
    def __init__(self,values):self.parent={value:value for value in values}
    def find(self,value):
        while self.parent[value]!=value:
            self.parent[value]=self.parent[self.parent[value]]; value=self.parent[value]
        return value
    def union(self,first,second):
        first=self.find(first); second=self.find(second)
        if first!=second:self.parent[max(first,second)]=min(first,second)


def find_duplicate_groups(
    records: Mapping[str,DuplicateFingerprint],*,
    lower_similarity: float=DEFAULT_SIMILARITY_PERCENT,
    upper_similarity: float=100,decisions: DuplicateDecisionStore|None=None,
    include_similar: bool=True,
    incoming_identities: frozenset[str]|None=None,
    max_relations: int=MAX_RELATIONS,
    max_candidate_evaluations: int=MAX_CANDIDATE_EVALUATIONS,
    max_visual_index_probes: int=MAX_VISUAL_INDEX_PROBES,
    progress: Callable[[int,int,str],None]|None=None,
    should_cancel: Callable[[],bool]|None=None,
    metrics: dict[str,int|str]|None=None,
) -> tuple[tuple[DuplicateGroup,...],bool]:
    lower=float(lower_similarity); upper=float(upper_similarity)
    if lower<MIN_SIMILARITY_PERCENT or upper>100 or lower>upper:
        raise ValueError(f"Similarity range must be between {MIN_SIMILARITY_PERCENT} and 100 percent.")
    if should_cancel and should_cancel():
        raise InterruptedError(
            "Duplicate comparison cancelled safely; completed fingerprints were retained."
        )
    usable={key:value for key,value in records.items() if value.usable}
    incoming=(
        None if incoming_identities is None
        else frozenset(str(identity) for identity in incoming_identities)
    )
    union=_UnionFind(usable); relations=[]; seen=set(); truncated=False
    truncation_reason=""; candidate_evaluations=0; visual_index_probes=0
    candidate_budget=max(1,int(max_candidate_evaluations))
    index_probe_budget=max(1,int(max_visual_index_probes))

    def cancel_checkpoint(counter: int) -> None:
        if counter%256==0 and should_cancel and should_cancel():
            raise InterruptedError(
                "Duplicate comparison cancelled safely; completed fingerprints were retained."
            )

    def consume_candidate() -> bool:
        nonlocal candidate_evaluations,truncated,truncation_reason
        if candidate_evaluations>=candidate_budget:
            truncated=True
            truncation_reason=truncation_reason or "candidate_evaluation_bound"
            return False
        candidate_evaluations+=1
        cancel_checkpoint(candidate_evaluations)
        return True

    def consume_index_probe() -> bool:
        nonlocal visual_index_probes,truncated,truncation_reason
        if visual_index_probes>=index_probe_budget:
            truncated=True
            truncation_reason=truncation_reason or "visual_index_probe_bound"
            return False
        visual_index_probes+=1
        cancel_checkpoint(visual_index_probes)
        return True

    def add(first: DuplicateFingerprint,second: DuplicateFingerprint,relation=None) -> bool:
        nonlocal truncated,truncation_reason
        pair=tuple(sorted((first.identity,second.identity)))
        if incoming is not None and (
            (first.identity in incoming)==(second.identity in incoming)
        ):
            return False
        if pair in seen or first.identity==second.identity:return False
        if decisions is not None and decisions.is_distinct(first,second):return False
        if len(relations)>=max(1,int(max_relations)):
            truncated=True
            truncation_reason=truncation_reason or "relation_bound"
            return False
        seen.add(pair)
        relation=relation or relation_between(first,second)
        relations.append(relation); union.union(first.identity,second.identity)
        return True

    for field_name in ("byte_sha256","pixel_sha256"):
        grouped={}
        for record_position,record in enumerate(usable.values(),1):
            cancel_checkpoint(record_position)
            value=getattr(record,field_name)
            if value:grouped.setdefault(value,[]).append(record)
        for members in grouped.values():
            members=sorted(members,key=lambda value:value.identity)
            if len(members)<2:continue
            if incoming is None:
                # Prefer a compact spanning tree, but do not let one reviewed-distinct
                # pair hide another valid exact relation in the same hash bucket.
                for index,member in enumerate(members[1:],start=1):
                    for previous in reversed(members[:index]):
                        if not consume_candidate():break
                        if add(previous,member):break
                    if truncated:break
            else:
                incoming_members=[
                    member for member in members if member.identity in incoming
                ]
                dataset_members=[
                    member for member in members if member.identity not in incoming
                ]
                if not incoming_members or not dataset_members:
                    continue
                component_count=len({union.find(member.identity) for member in members})
                if component_count==1:
                    continue
                attempted=set()

                def attempt(first,second) -> bool:
                    nonlocal component_count
                    if not consume_candidate():return False
                    pair=tuple(sorted((first.identity,second.identity)))
                    if (
                        pair in attempted
                        or union.find(first.identity)==union.find(second.identity)
                    ):
                        return False
                    attempted.add(pair)
                    connected=add(first,second)
                    if connected:component_count-=1
                    return connected

                # The common case stays linear: one incoming anchor connects every
                # Dataset member, then one Dataset anchor connects the remaining
                # incoming members. This avoids losing cross-scope exact relations
                # when identities from one side sort next to each other.
                for member in dataset_members:
                    if component_count==1:break
                    for candidate in incoming_members:
                        if attempt(candidate,member):break
                    if truncated:break
                if not truncated:
                    for member in incoming_members[1:]:
                        if component_count==1:break
                        for candidate in dataset_members:
                            if attempt(candidate,member):break
                        if truncated:break

                # Reviewed-distinct anchor edges can leave a connected exact bucket
                # split. Search only then, and retain the global candidate bound.
                if not truncated and component_count>1:
                    for first in incoming_members:
                        if component_count==1:break
                        for second in dataset_members:
                            if component_count==1:break
                            attempt(first,second)
                            if truncated:break
                        if truncated:break
            if truncated:break
        if truncated:break

    visual_index=_PerceptualBandIndex(lower)
    ordered_records=(
        [] if truncated or not include_similar else
        sorted(usable.values(),key=lambda value:value.identity)
    )
    for position,record in enumerate(ordered_records,1):
        if should_cancel and should_cancel():
            raise InterruptedError("Duplicate comparison cancelled safely; completed fingerprints were retained.")
        candidates=set()
        for feature in record.perceptual_hashes:
            found,probe_truncated=visual_index.query(
                feature,consume_probe=consume_index_probe,
            )
            candidates.update(found)
            if probe_truncated:break
        if truncated:break
        for candidate_index in sorted(candidates):
            candidate=ordered_records[candidate_index]
            if incoming is not None and (
                (candidate.identity in incoming)==(record.identity in incoming)
            ):
                continue
            if candidate.byte_sha256==record.byte_sha256 or candidate.pixel_sha256==record.pixel_sha256:
                continue
            if not consume_candidate():break
            relation=relation_between(candidate,record)
            if lower-1e-9<=relation.similarity<=upper+1e-9:
                add(candidate,record,relation)
            if truncated:break
        for feature in record.perceptual_hashes:visual_index.add(feature,position-1)
        if progress and (position==len(ordered_records) or position%max(1,len(ordered_records)//200)==0):
            progress(position,len(ordered_records),f"Comparing visual fingerprints {position:,} of {len(ordered_records):,}")
        if truncated:break

    if metrics is not None:
        metrics.update({
            "candidate_evaluations":candidate_evaluations,
            "candidate_budget":candidate_budget,
            "visual_index_probes":visual_index_probes,
            "visual_index_probe_budget":index_probe_budget,
            "truncation_reason":truncation_reason,
        })

    components={}; relations_by_root={}
    for relation in relations:
        root=union.find(relation.first_identity)
        components.setdefault(root,set()).update((relation.first_identity,relation.second_identity))
        relations_by_root.setdefault(root,[]).append(relation)
    groups=[]
    for root,members in components.items():
        ordered=tuple(sorted(members))
        group_relations=tuple(relations_by_root.get(root,()))
        identifier=hashlib.sha256("\n".join(ordered).encode("utf-8")).hexdigest()[:20]
        groups.append(DuplicateGroup(identifier,ordered,group_relations))
    groups.sort(key=lambda group:(-len(group.member_identities),group.id))
    return tuple(groups),truncated


class DuplicateFinderService:
    def __init__(self,index: DuplicateIndex,decisions: DuplicateDecisionStore):
        self.index=index; self.decisions=decisions
        self._badge_lock=RLock()
        self._badge_evidence: dict[str,DuplicateBadgeEvidence]={}
        self._report_lock=RLock()
        self._session_reports: dict[str,DuplicateSearchReport]={}

    @staticmethod
    def _session_report_key(report: DuplicateSearchReport) -> str:
        incoming=(
            "all-pairs" if report.incoming_identities is None else
            "cross-scope:"+hashlib.sha256(
                "\n".join(sorted(report.incoming_identities)).encode("utf-8")
            ).hexdigest()
        )
        return (
            f"{report.scope_id}|{int(report.include_similar)}|"
            f"{report.lower_similarity:.4f}|{report.upper_similarity:.4f}|{incoming}"
        )

    def record_session_report(self,report: DuplicateSearchReport) -> None:
        """Keep a bounded completed report for reopening this app-session review."""
        key=self._session_report_key(report)
        with self._report_lock:
            self._session_reports.pop(key,None)
            self._session_reports[key]=report
            while len(self._session_reports)>MAX_SESSION_REPORTS:
                self._session_reports.pop(next(iter(self._session_reports)))

    def latest_session_report(
        self,scope: DuplicateScope,*,include_similar: bool=True,
        incoming_identities: frozenset[str]|None=None,
    ) -> DuplicateSearchReport|None:
        """Return the newest completed report for the same exact logical scope."""
        incoming=(
            None if incoming_identities is None else frozenset(incoming_identities)
        )
        with self._report_lock:
            for report in reversed(tuple(self._session_reports.values())):
                if (
                    report.scope_id==scope.id
                    and bool(report.include_similar)==bool(include_similar)
                    and report.incoming_identities==incoming
                ):
                    return report
        return None

    def clear_session_reports(self) -> None:
        with self._report_lock:self._session_reports.clear()

    def has_session_reports(self) -> bool:
        with self._report_lock:return bool(self._session_reports)

    @staticmethod
    def _badge_path_key(path: str|Path) -> str:
        return os.path.normcase(os.path.abspath(os.fspath(Path(path))))

    def record_badge_results(self,report: DuplicateSearchReport) -> None:
        """Publish a compact current result without retaining the full report.

        Callers publish only after their scope-currentness gate passes. Gallery
        reads are then constant-time and never fingerprint or compare images.
        """
        stale=set(report.stale_identities)
        relations_by_identity: dict[str,list[DuplicateRelation]]={}
        matches_by_identity: dict[str,set[str]]={}
        for group in report.groups:
            for relation in group.relations:
                for identity,other in (
                    (relation.first_identity,relation.second_identity),
                    (relation.second_identity,relation.first_identity),
                ):
                    if identity in stale or other in stale:
                        continue
                    relations_by_identity.setdefault(identity,[]).append(relation)
                    matches_by_identity.setdefault(identity,set()).add(other)
        checked_at=_utc_now()
        replacements={}
        for identity,record in report.records.items():
            if identity in stale:
                continue
            relations=relations_by_identity.get(identity,())
            if not relations:
                continue
            exact=sum(
                relation.kind in {"byte_exact","pixel_exact"}
                for relation in relations
            )
            visual=sum(relation.kind=="similar" for relation in relations)
            replacements[self._badge_path_key(record.path)]=DuplicateBadgeEvidence(
                "exact" if exact else "similar",
                len(matches_by_identity.get(identity,())),exact,visual,
                report.scope_label,checked_at,bool(report.truncated),
            )
        checked_paths={
            self._badge_path_key(record.path) for record in report.records.values()
        }
        with self._badge_lock:
            for key in checked_paths:
                self._badge_evidence.pop(key,None)
            self._badge_evidence.update(replacements)
            overflow=len(self._badge_evidence)-MAX_BADGE_EVIDENCE
            if overflow>0:
                for key in tuple(self._badge_evidence)[:overflow]:
                    self._badge_evidence.pop(key,None)

    def badge_for_path(self,path: str|Path) -> DuplicateBadgeEvidence|None:
        with self._badge_lock:
            return self._badge_evidence.get(self._badge_path_key(path))

    def invalidate_badges(self,paths: Iterable[str|Path]=()) -> None:
        keys=tuple(self._badge_path_key(path) for path in paths)
        with self._badge_lock:
            if not keys:
                self._badge_evidence.clear()
                return
            for key in keys:
                self._badge_evidence.pop(key,None)

    @staticmethod
    def search_key(
        scope: DuplicateScope,lower: float,upper: float,force: bool=False,*,
        include_similar: bool=True,
        incoming_identities: frozenset[str]|None=None,
    ) -> str:
        digest=hashlib.sha256()
        digest.update(
            f"{scope.id}|{lower:.4f}|{upper:.4f}|{int(force)}|"
            f"{int(include_similar)}".encode("utf-8")
        )
        for item in scope.items:digest.update(f"\0{item.identity}".encode("utf-8"))
        if incoming_identities is None:
            digest.update(b"\0all-pairs")
        else:
            digest.update(b"\0cross-scope")
            for identity in sorted(incoming_identities):
                digest.update(f"\0{identity}".encode("utf-8"))
        return "duplicates:"+digest.hexdigest()

    def search(
        self,scope: DuplicateScope,*,
        lower_similarity: float=DEFAULT_SIMILARITY_PERCENT,
        upper_similarity: float=100,force: bool=False,
        include_similar: bool=True,
        incoming_identities: frozenset[str]|None=None,
        progress: Callable[[int,int,str],None]|None=None,
        should_cancel: Callable[[],bool]|None=None,
    ) -> DuplicateSearchReport:
        records,issues,indexed,reused,removed,stale=self.index.scan(
            scope,force=force,progress=progress,should_cancel=should_cancel,
        )
        if progress:
            comparison=(
                "Comparing exact and visually similar fingerprints"
                if include_similar else "Comparing exact fingerprints"
            )
            progress(0,len(records),comparison)
        metrics={}
        groups,truncated=find_duplicate_groups(
            records,lower_similarity=lower_similarity,
            upper_similarity=upper_similarity,decisions=self.decisions,
            include_similar=include_similar,
            incoming_identities=incoming_identities,
            progress=progress,should_cancel=should_cancel,
            metrics=metrics,
        )
        if truncated:
            reason=str(metrics.get("truncation_reason") or "comparison_bound")
            details={
                "relation_bound":(
                    f"The {MAX_RELATIONS:,}-relation safety bound was reached; "
                    "narrow the scope or increase the lower similarity threshold."
                ),
                "candidate_evaluation_bound":(
                    f"The {int(metrics.get('candidate_budget',0)):,}-candidate "
                    "comparison budget was reached; the candidate space is too dense."
                ),
                "visual_index_probe_bound":(
                    f"The {int(metrics.get('visual_index_probe_budget',0)):,}-probe "
                    "visual routing budget was reached; the candidate space is too dense."
                ),
            }
            issues=(*issues,DuplicateScanIssue(
                Path(),reason,details.get(
                    reason,"A duplicate comparison safety bound was reached."
                ),
            ))
        report=DuplicateSearchReport(
            scope.id,scope.label,float(lower_similarity),float(upper_similarity),
            records,groups,tuple(issues),indexed,reused,removed,truncated,
            stale,int(metrics.get("candidate_evaluations",0)),
            int(metrics.get("candidate_budget",MAX_CANDIDATE_EVALUATIONS)),
            int(metrics.get("visual_index_probes",0)),
            int(metrics.get("visual_index_probe_budget",MAX_VISUAL_INDEX_PROBES)),
            str(metrics.get("truncation_reason", "")),
            bool(include_similar),
            None if incoming_identities is None else frozenset(incoming_identities),
        )
        self.record_session_report(report)
        return report

    def regroup(
        self,report: DuplicateSearchReport,*,
        progress: Callable[[int,int,str],None]|None=None,
        should_cancel: Callable[[],bool]|None=None,
    ) -> DuplicateSearchReport:
        metrics={}
        groups,truncated=find_duplicate_groups(
            report.records,lower_similarity=report.lower_similarity,
            upper_similarity=report.upper_similarity,decisions=self.decisions,
            include_similar=report.include_similar,
            incoming_identities=report.incoming_identities,
            progress=progress,should_cancel=should_cancel,metrics=metrics,
        )
        return replace(
            report,groups=groups,truncated=truncated,
            candidate_evaluations=int(metrics.get("candidate_evaluations",0)),
            candidate_budget=int(
                metrics.get("candidate_budget",MAX_CANDIDATE_EVALUATIONS)
            ),
            visual_index_probes=int(metrics.get("visual_index_probes",0)),
            visual_index_probe_budget=int(
                metrics.get("visual_index_probe_budget",MAX_VISUAL_INDEX_PROBES)
            ),
            truncation_reason=str(metrics.get("truncation_reason","")),
        )

    def mark_distinct(
        self,report: DuplicateSearchReport,first: DuplicateFingerprint,
        second: DuplicateFingerprint,*,
        progress: Callable[[int,int,str],None]|None=None,
        should_cancel: Callable[[],bool]|None=None,
    ) -> DuplicateSearchReport:
        """Validate, persist, and regroup a reviewed pair away from the UI thread."""
        if first.identity==second.identity:
            raise ValueError("Choose two different images.")
        if (
            report.records.get(first.identity)!=first
            or report.records.get(second.identity)!=second
        ):
            raise ValueError("The comparison result changed; re-run the duplicate search.")
        if should_cancel and should_cancel():
            raise InterruptedError("Mark Distinct stopped before any decision was saved.")
        if progress:progress(0,2,"Checking both current fingerprints")
        executor=ThreadPoolExecutor(
            max_workers=2,thread_name_prefix="duplicate-distinct-check",
        )
        pending={
            executor.submit(self.index.is_current,record):record
            for record in (first,second)
        }
        current={}; completed=0
        try:
            while pending:
                if should_cancel and should_cancel():
                    raise InterruptedError("Mark Distinct stopped before any decision was saved.")
                done,_remaining=wait(tuple(pending),return_when=FIRST_COMPLETED)
                for future in done:
                    record=pending.pop(future)
                    current[record.identity]=bool(future.result()); completed+=1
                    if progress:
                        progress(
                            completed,2,
                            f"Checked current fingerprint {completed:,} of 2",
                        )
        finally:
            executor.shutdown(wait=True,cancel_futures=True)
        if not all(current.get(record.identity,False) for record in (first,second)):
            raise ValueError(
                "One comparison image changed or became unavailable. Re-run the "
                "duplicate search before recording a decision."
            )
        if should_cancel and should_cancel():
            raise InterruptedError("Mark Distinct stopped before any decision was saved.")
        if progress:progress(0,0,"Saving the reviewed-distinct decision")
        self.decisions.mark_distinct(first,second)
        if progress:progress(0,0,"Refreshing duplicate result sets")
        updated=self.regroup(report,progress=progress)
        self.record_badge_results(updated)
        if progress:progress(1,1,"Reviewed-distinct decision saved")
        return updated

    def unmark_distinct(
        self,report: DuplicateSearchReport,first: DuplicateFingerprint,
        second: DuplicateFingerprint,*,
        progress: Callable[[int,int,str],None]|None=None,
    ) -> tuple[bool,DuplicateSearchReport]:
        """Undo one durable decision and refresh its result sets off the UI thread."""
        if progress:progress(0,0,"Restoring the reviewed pair")
        removed=self.decisions.unmark_distinct(first,second)
        if not removed:return False,report
        if progress:progress(0,0,"Refreshing duplicate result sets")
        updated=self.regroup(report,progress=progress)
        self.record_badge_results(updated)
        if progress:progress(1,1,"Reviewed-distinct decision undone")
        return True,updated

    def member_details(
        self,records: Iterable[DuplicateFingerprint],*,knowledge=None,
    ) -> tuple[DuplicateMemberDetails,...]:
        service=ImageKnowledgeCompletenessService(knowledge=knowledge)
        result=[]
        for fingerprint in records:
            try:
                record=ImageRecord(fingerprint.path)
                completeness=service.assess_record(record)
                generation=record.annotation.generation_records
                source=record.annotation.source if isinstance(record.annotation.source,dict) else {}
                metadata_bits=[]
                if record.sidecar_path.exists():metadata_bits.append("Image Knowledge sidecar")
                if record.caption_path.exists():metadata_bits.append("adjacent TXT")
                metadata_bits.append(f"{len(source):,} provenance field(s)")
                generators=[]
                for item in generation[:3]:
                    label=" ".join(value for value in (
                        item.generator_name,item.generator_version,item.source,
                    ) if value)
                    if label and label not in generators:generators.append(label)
                provenance=(
                    f"{len(generation):,} exact generator record(s): "+", ".join(generators)
                    if generation else "No exact generator record is stored."
                )
                extras=getattr(record.annotation,"extra_fields",{})
                derivative=next((
                    extras.get(key) for key in (
                        "derivative_relationship","derivative_of","parent_image","lineage"
                    ) if extras.get(key)
                ),None) if isinstance(extras,dict) else None
                result.append(DuplicateMemberDetails(
                    fingerprint.identity,completeness.score,
                    f"{completeness.score}/100 • "+(
                        "Image Knowledge complete" if completeness.ready else "Needs fact review"
                    )," • ".join(metadata_bits),provenance,
                    str(derivative) if derivative is not None else "No derivative relationship is recorded.",
                    "",bool(record.annotation.all_structured_tags()),
                    bool(record.annotation.plain_language),
                    "green" if completeness.ready else "orange",
                ))
            except Exception as exc:
                result.append(DuplicateMemberDetails(
                    fingerprint.identity,None,"Image data unavailable",
                    "Metadata and Image Knowledge could not be read.",
                    "Provenance unavailable.","Derivative relationship unavailable.",
                    str(exc),None,None,"red",
                ))
        return tuple(result)

    @staticmethod
    def recommend_reference(
        records: Iterable[DuplicateFingerprint],
        details: Mapping[str,DuplicateMemberDetails]|None=None,
    ) -> DuplicateReferenceRecommendation:
        values=tuple(records); details=details or {}
        if len(values)<2:return DuplicateReferenceRecommendation()
        ranked=[]
        for record in values:
            detail=details.get(record.identity)
            completeness=detail.completeness_score if detail and detail.completeness_score is not None else -1
            ranked.append((completeness,record.pixel_count,record.identity,record))
        ranked.sort(reverse=True); best=ranked[0]; second=ranked[1]
        if best[:2]==second[:2]:return DuplicateReferenceRecommendation()
        reasons=[]
        if best[0]>second[0] and best[0]>=0:reasons.append(f"higher Image Knowledge completeness ({best[0]}/100)")
        if best[1]>second[1]:reasons.append(f"higher decoded resolution ({best[3].width} × {best[3].height})")
        if not reasons:return DuplicateReferenceRecommendation()
        return DuplicateReferenceRecommendation(
            best[3].identity,
            "Suggested only because it has "+" and ".join(reasons)+". Review provenance and lineage before choosing; no file is selected for removal.",
        )
