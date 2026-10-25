from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .dataset_scope import DatasetScope
from .dataset_index import CachedImageRecord, DatasetIndex
from .image_record import ImageRecord
from .library_storage import (
    LibraryFileAvailability,LibraryFileState,LibraryStorageInspector,
)
from .typed_boundary import typed_tag_sources
from .tag_collection import normalize_tag


@dataclass
class DatasetSummary:
    images: int = 0
    captions: int = 0
    sidecars: int = 0
    unique_tags: int = 0
    errors: int = 0
    indexed: int = 0
    reused_from_cache: int = 0
    online_only: int = 0
    conflicts: int = 0
    read_only: int = 0
    unavailable: int = 0


@dataclass
class DatasetManager:
    """Shared, lightweight dataset scan and cache for the unified application."""

    root: Path | None = None
    include_subfolders: bool = True
    images: list[Path] = field(default_factory=list)
    tag_counts: dict[str, int] = field(default_factory=dict)
    tag_sources: dict[str, set[str]] = field(default_factory=dict)
    tag_examples: dict[str, str] = field(default_factory=dict)
    summary: DatasetSummary = field(default_factory=DatasetSummary)
    indexed_images: set[Path] = field(default_factory=set)
    schema_migration_candidates: list[Path] = field(default_factory=list)
    validated_records: dict[Path, CachedImageRecord] = field(default_factory=dict)
    changed_images: set[Path] = field(default_factory=set)
    index: DatasetIndex | None = None
    knowledge: object | None = field(default=None, repr=False)
    storage_role: str = "local_only"
    storage_inspector: LibraryStorageInspector = field(
        default_factory=LibraryStorageInspector,repr=False,
    )
    storage_states: dict[Path,LibraryFileAvailability] = field(
        default_factory=dict,
    )

    def clear(self) -> None:
        self.root = None
        self.images.clear()
        self.tag_counts.clear()
        self.tag_sources.clear(); self.tag_examples.clear()
        self.indexed_images.clear()
        self.schema_migration_candidates.clear()
        self.validated_records.clear(); self.changed_images.clear()
        self.storage_states.clear()
        self.summary = DatasetSummary()
        self.index = None

    def storage_availability(self,image: str | Path) -> LibraryFileAvailability:
        return self.storage_states.get(Path(image),LibraryFileAvailability())

    @property
    def readable_images(self) -> list[Path]:
        return [
            image for image in self.images
            if self.storage_availability(image).readable
        ]

    @property
    def writable_images(self) -> list[Path]:
        return [
            image for image in self.images
            if self.storage_availability(image).ready_for_write
        ]

    def inventory(
        self,
        root: str | Path,
        *,
        include_subfolders: bool = True,
        excluded_directories: tuple[Path, ...] = (),
        progress: Callable[[int,int,str],None] | None = None,
        should_cancel: Callable[[],bool] | None = None,
    ) -> DatasetSummary:
        """Find image paths quickly without opening every caption or sidecar."""
        root_path = Path(root).expanduser().resolve()
        if not root_path.is_dir(): raise NotADirectoryError(root_path)
        self.root = root_path; self.include_subfolders = include_subfolders
        self.images = list(DatasetScope(
            root_path,include_subfolders=include_subfolders,
            excluded_directories=excluded_directories,
        ).iter_images(progress=progress,should_cancel=should_cancel))
        self.tag_counts.clear(); self.tag_sources.clear(); self.tag_examples.clear(); self.indexed_images.clear(); self.schema_migration_candidates.clear()
        self.validated_records.clear(); self.changed_images.clear()
        self.index = DatasetIndex(root_path)
        self.summary = DatasetSummary(images=len(self.images))
        self.storage_states=self.storage_inspector.inspect_images(
            self.images,detect_conflicts=self.storage_role=="cloud_synced",
        )
        for availability in self.storage_states.values():
            if availability.state is LibraryFileState.ONLINE_ONLY:
                self.summary.online_only+=1
            elif availability.state is LibraryFileState.CONFLICT:
                self.summary.conflicts+=1
            elif availability.state is LibraryFileState.READ_ONLY:
                self.summary.read_only+=1
            elif availability.state is LibraryFileState.UNAVAILABLE:
                self.summary.unavailable+=1
        cached_by_path=self.index.records_by_path()
        for image in self.images:
            if self.storage_availability(image).readable:
                continue
            cached=cached_by_path.get(image)
            if cached is not None:
                self._apply_cached(cached,reused=True)
                self.indexed_images.add(image)
        self.summary.unique_tags=len(self.tag_counts)
        return self.summary

    def restore_cached(
        self,root: str | Path,*,include_subfolders: bool=True,
        cache_dir: Path | None=None,
    ) -> DatasetSummary | None:
        """Restore the previous searchable snapshot before filesystem validation."""
        index=DatasetIndex(Path(root).expanduser(),cache_dir) if cache_dir is not None else DatasetIndex(Path(root).expanduser())
        root_path=index.root
        cached=index.all_records()
        if not include_subfolders:
            cached=[item for item in cached if Path(item.path).parent==root_path]
        if not cached:
            return None
        self.root=root_path; self.include_subfolders=include_subfolders
        self.index=index
        self.images=[Path(item.path) for item in cached]
        self.storage_states={
            image:LibraryFileAvailability(
                LibraryFileState.UNAVAILABLE,
                "This is saved catalog information; the current file has not been checked.",
                "Restore access to the library and refresh it before opening or changing this image.",
                False,False,
            )
            for image in self.images
        }
        self.tag_counts.clear(); self.tag_sources.clear(); self.tag_examples.clear(); self.indexed_images=set(self.images); self.schema_migration_candidates.clear()
        # Restored rows are useful immediately, but remain unvalidated until a
        # cheap fingerprint check or the normal background inventory confirms
        # them.  Gallery safety therefore does not mistake stale cache data for
        # current evidence.
        self.validated_records.clear(); self.changed_images.clear()
        self.summary=DatasetSummary(images=len(cached))
        for item in cached:
            self._apply_cached(item,reused=True)
        self.summary.unique_tags=len(self.tag_counts)
        return self.summary

    def reuse_unchanged_cached(self,changed_paths,*,previously_validated=None) -> None:
        """Restore unrelated catalog rows after a scoped move inventory.

        This reuses display data, not new validation or initialization authority.
        Only an identical row already validated before this move retains that
        validation; missing rows remain pending for the normal library check.
        """
        if self.index is None:
            return
        changed={Path(value) for value in changed_paths}
        previous=previously_validated or {}
        cached_by_path=self.index.records_by_path()
        for image in self.images:
            if image in changed or image in self.indexed_images:
                continue
            cached=cached_by_path.get(image)
            if cached is None:
                continue
            self._apply_cached(cached,reused=True)
            self.indexed_images.add(image)
            if previous.get(image)==cached:
                self.validated_records[image]=cached
        self.summary.unique_tags=len(self.tag_counts)

    def _apply_cached(self, cached: CachedImageRecord, *, reused: bool) -> None:
        if cached.caption_exists: self.summary.captions += 1
        if cached.sidecar_exists: self.summary.sidecars += 1
        if cached.error: self.summary.errors += 1
        for tag in cached.tags:
            key=normalize_tag(tag)
            if key:
                self.tag_counts[key]=self.tag_counts.get(key,0)+1
                self.tag_examples.setdefault(key,cached.path)
                sources=cached.tag_sources.get(tag,cached.tag_sources.get(key,[]))
                self.tag_sources.setdefault(key,set()).update(str(value) for value in sources if str(value).strip())
        self.summary.indexed += 1
        if reused: self.summary.reused_from_cache += 1

    def replace_cached(
        self,cached: CachedImageRecord,*,previous: CachedImageRecord|None=None,
    ) -> None:
        """Replace one derived row and keep in-memory aggregates exact."""
        self.replace_cached_batch(((cached,previous),))

    def replace_cached_batch(
        self,
        replacements: tuple[
            tuple[CachedImageRecord,CachedImageRecord|None],...
        ] | list[tuple[CachedImageRecord,CachedImageRecord|None]],
    ) -> None:
        """Publish a durable row batch with one aggregate-detail rebuild."""
        affected=set()
        resolved=[]
        for cached,previous in replacements:
            path=Path(cached.path)
            if previous is None:
                previous=self.validated_records.get(path)
            affected.update(
                normalize_tag(tag) for tag in cached.tags if normalize_tag(tag)
            )
            if previous is not None:
                affected.update(
                    normalize_tag(tag) for tag in previous.tags if normalize_tag(tag)
                )
            resolved.append((path,cached,previous))
        for _path,_cached,previous in resolved:
            if previous is not None:
                self._remove_cached(previous)
                self.summary.indexed=max(0,self.summary.indexed-1)
        for path,cached,_previous in resolved:
            self._apply_cached(cached,reused=False)
            self.validated_records[path]=cached
            self.indexed_images.add(path)
            self.changed_images.add(path)
        self._rebuild_cached_tag_details(affected)
        self.summary.unique_tags=len(self.tag_counts)

    def _remove_cached(self, cached: CachedImageRecord) -> None:
        if cached.caption_exists: self.summary.captions = max(0, self.summary.captions - 1)
        if cached.sidecar_exists: self.summary.sidecars = max(0, self.summary.sidecars - 1)
        if cached.error: self.summary.errors = max(0, self.summary.errors - 1)
        for tag in cached.tags:
            key = normalize_tag(tag)
            if not key: continue
            count = self.tag_counts.get(key, 0) - 1
            if count > 0:
                self.tag_counts[key] = count
            else:
                self.tag_counts.pop(key, None)
            self.tag_sources.pop(key, None)
            self.tag_examples.pop(key, None)

    def _rebuild_cached_tag_details(self,keys: set[str]) -> None:
        """Rebuild source/example details only for identities touched by one row."""
        wanted={key for key in keys if key and self.tag_counts.get(key,0)>0}
        examples={}; sources={key:set() for key in wanted}
        for path,record in self.validated_records.items():
            for tag in record.tags:
                key=normalize_tag(tag)
                if key not in wanted:
                    continue
                examples.setdefault(key,str(path))
                values=record.tag_sources.get(
                    tag,record.tag_sources.get(key,[]),
                )
                sources[key].update(
                    str(value) for value in values if str(value).strip()
                )
        for key in wanted:
            if key in examples:
                self.tag_examples[key]=examples[key]
            if sources[key]:
                self.tag_sources[key]=sources[key]

    def validate_cached(
        self,
        images: list[Path] | None = None,
        *,
        progress: Callable[[int,int,str],None] | None = None,
        should_cancel: Callable[[],bool] | None = None,
    ) -> int:
        """Validate saved rows once and publish every exact unchanged result.

        Startup consumers share ``validated_records`` and ``indexed_images``
        after this pass. Managed initialization, Dataset analysis, and content
        safety can therefore reuse the same image/TXT/JSON signature decision
        instead of each probing the complete library again.
        """
        if self.index is None:
            return 0
        queue=[
            Path(image) for image in (images if images is not None else self.images)
            if self.storage_availability(image).readable
            and Path(image) not in self.indexed_images
        ]
        fresh=0; total=len(queue)
        connection=self.index.connect()
        try:
            for position,image in enumerate(queue,1):
                if should_cancel and should_cancel():
                    break
                cached=self.index.get_if_fresh(image,connection)
                if cached is not None:
                    self._apply_cached(cached,reused=True)
                    self.validated_records[image]=cached
                    self.indexed_images.add(image)
                    fresh+=1
                else:
                    # The saved library view may have supplied a workflow
                    # projection for this path before the cheap signature pass.
                    # Retire only that rebuildable projection as soon as the
                    # image family is known to have changed.
                    self.index.invalidate_workflow_image(image,db=connection)
                    self.changed_images.add(image)
                if progress and (position==total or position%25==0):
                    progress(position,total,image.name)
            connection.commit()
        finally:
            connection.close()
        self.summary.unique_tags=len(self.tag_counts)
        return fresh

    def analyze(
        self,
        images: list[Path] | None = None,
        *,
        progress: Callable[[int, int, str], None] | None = None,
        should_cancel: Callable[[],bool] | None = None,
        allow_legacy_txt: bool = False,
    ) -> DatasetSummary:
        """Deep-index requested images; callers may put visible/selected paths first."""
        queue = [
            image for image in (images if images is not None else self.images)
            if self.storage_availability(image).readable
        ]
        pending = [image for image in queue if image not in self.indexed_images]
        total = len(pending)
        connection=self.index.connect() if self.index else None
        try:
            for index,image in enumerate(pending,start=1):
                if should_cancel and should_cancel():
                    break
                cached=self.index.get_if_fresh(image,connection) if self.index else None
                if cached:
                    self._apply_cached(cached,reused=True)
                else:
                    tags=[]; tag_sources={}; plain_language=[]; error=''
                    try:
                        caption=image.with_suffix('.txt'); sidecar=image.with_suffix('.tags.json')
                        if sidecar.exists() or (allow_legacy_txt and caption.exists()):
                            record=ImageRecord(image)
                            tag_sources=typed_tag_sources(record,self.knowledge)
                            tags=list(tag_sources)
                            plain_language=list(record.annotation.plain_language)
                    except Exception as exc: error=str(exc)
                    cached=self.index.put(image,tags,error,connection,tag_sources,plain_language) if self.index else CachedImageRecord(str(image),image.with_suffix('.txt').exists(),image.with_suffix('.tags.json').exists(),tags,error,tag_sources,plain_language)
                    self._apply_cached(cached,reused=False)
                    self.changed_images.add(image)
                    if cached.sidecar_exists: self.schema_migration_candidates.append(image)
                self.validated_records[image]=cached
                self.indexed_images.add(image)
                if progress: progress(index,total,image.name)
            if connection: connection.commit()
        finally:
            if connection: connection.close()
        self.summary.unique_tags=len(self.tag_counts)
        if images is None and self.index: self.index.remove_missing(self.images)
        return self.summary

    def cache_managed_record(self,image: Path,record: ImageRecord,*,db=None) -> CachedImageRecord:
        """Publish one completed authority record into the rebuildable local view."""
        image=Path(image)
        tag_sources=typed_tag_sources(record,self.knowledge)
        previous=self.validated_records.get(image)
        if previous is None and image in self.indexed_images and self.index is not None:
            previous=self.index.cached_record(image)
        cached=(
            self.index.put(
                image,list(tag_sources),db=db,tag_sources=tag_sources,
                plain_language=record.annotation.plain_language,
                preserve_current_workflow=True,
            )
            if self.index else
            CachedImageRecord(
                str(image),image.with_suffix(".txt").exists(),
                image.with_suffix(".tags.json").exists(),list(tag_sources),"",
                tag_sources,list(record.annotation.plain_language),
            )
        )
        if image in self.indexed_images:
            self.replace_cached(cached,previous=previous)
        else:
            self.validated_records[image]=cached
            self.changed_images.add(image)
            self._apply_cached(cached,reused=False)
            self.indexed_images.add(image)
            if cached.sidecar_exists:
                self.schema_migration_candidates.append(image)
            self.summary.unique_tags=len(self.tag_counts)
        return cached

    def prioritize(self, images: list[Path]) -> DatasetSummary:
        """Synchronously index a small active set before background work reaches it."""
        return self.analyze([Path(image) for image in images])

    def load(
        self,
        root: str | Path,
        *,
        include_subfolders: bool = True,
        excluded_directories: tuple[Path, ...] = (),
        progress: Callable[[int, int, str], None] | None = None,
    ) -> DatasetSummary:
        root_path = Path(root).expanduser().resolve()
        if not root_path.is_dir():
            raise NotADirectoryError(root_path)

        self.inventory(root_path,include_subfolders=include_subfolders,excluded_directories=excluded_directories)
        # Retained only as the documented MIG-01 compatibility loader. Managed
        # Qt inventory initializes JSON authority before calling ``analyze``.
        return self.analyze(progress=progress,allow_legacy_txt=True)
