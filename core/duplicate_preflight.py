from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable,Iterable,Literal

from .duplicate_finder import (
    DuplicateFinderService,DuplicateScanIssue,DuplicateScope,DuplicateScopeItem,
    duplicate_scope_item,
)


def _counted(count: int,singular: str,plural: str|None=None) -> str:
    return f"{count:,} {singular if count==1 else plural or singular+'s'}"


@dataclass(frozen=True)
class DuplicatePreflightLibrarySource:
    stable_id: str
    name: str
    root: Path
    availability: str
    images: object


@dataclass(frozen=True)
class DuplicatePreflightScopePlan:
    incoming_paths: tuple[Path,...]
    ingest_root: Path
    libraries: tuple[DuplicatePreflightLibrarySource,...]


@dataclass(frozen=True)
class PreparedDuplicatePreflightScope:
    scope: DuplicateScope
    incoming_identities: frozenset[str]
    included_library_count: int
    dataset_image_count: int
    library_notes: tuple[str,...]
    library_inventories: tuple[tuple[str,tuple[Path,...]],...]


@dataclass(frozen=True)
class DuplicateFingerprintWarmupResult:
    total: int
    completed: int
    indexed: int|None
    reused: int|None
    issues: tuple[DuplicateScanIssue,...]|None
    cancelled: bool
    status: Literal["ready","ready_with_issues","cancelled"]

    @property
    def issue_count(self) -> int|None:
        return None if self.issues is None else len(self.issues)


def _incoming_scope_items(
    paths: Iterable[str|Path],ingest_root: str|Path,*,
    progress: Callable[[int,int,str],None]|None=None,
    should_cancel: Callable[[],bool]|None=None,
) -> tuple[DuplicateScopeItem,...]:
    values=tuple(Path(value) for value in paths)
    items={}
    interval=max(1,(len(values)+199)//200)
    for position,path in enumerate(values,1):
        if should_cancel and should_cancel():
            raise InterruptedError(
                "Incoming duplicate fingerprint preparation cancelled safely."
            )
        item=duplicate_scope_item(
            path,source_id="ingest",source_root=ingest_root,
            source_label="Incoming Images",
        )
        items.setdefault(item.identity,item)
        if progress and (position==len(values) or position%interval==0):
            progress(
                position,len(values),
                f"Preparing incoming image identities {position:,} of {len(values):,}",
            )
    return tuple(items.values())


def warm_duplicate_fingerprints(
    duplicate_service: DuplicateFinderService,
    paths: Iterable[str|Path],ingest_root: str|Path,*,
    progress: Callable[[int,int,str],None]|None=None,
    should_cancel: Callable[[],bool]|None=None,
) -> DuplicateFingerprintWarmupResult:
    """Populate reusable incoming fingerprints without grouping or authority writes."""
    values=tuple(Path(value) for value in paths)
    try:
        items=_incoming_scope_items(
            values,ingest_root,progress=progress,should_cancel=should_cancel,
        )
    except InterruptedError:
        return DuplicateFingerprintWarmupResult(
            len(values),0,None,None,None,True,"cancelled",
        )
    return _warm_duplicate_items(
        duplicate_service,items,prefix="ingest-fingerprint-warmup",
        label="Incoming fingerprint warmup",progress=progress,should_cancel=should_cancel,
    )


def warm_dataset_duplicate_fingerprints(duplicate_service,paths,registry,*,progress=None,should_cancel=None):
    """Index only the requested Dataset paths with their canonical library IDs."""
    values=tuple(dict.fromkeys(Path(value) for value in paths))
    items=[]; held=[]
    for position,path in enumerate(values,1):
        if should_cancel and should_cancel():
            return DuplicateFingerprintWarmupResult(len(values),0,None,None,None,True,"cancelled")
        profile=registry.profile_for_path(path)
        if profile is None:
            held.append(DuplicateScanIssue(path,"library_unavailable","No registered library identity; fingerprint creation was held."))
            continue
        items.append(duplicate_scope_item(path,source_id=profile.stable_id,
            source_root=profile.root,source_label=profile.name,source_profile_id=profile.stable_id))
        if progress and (position==len(values) or position%max(1,len(values)//200)==0):
            progress(position,len(values),"Preparing Dataset fingerprint identities")
    result=_warm_duplicate_items(duplicate_service,tuple(items),prefix="dataset-fingerprint-warmup",
        label="Dataset fingerprint warmup",progress=progress,should_cancel=should_cancel)
    if result.cancelled:return result
    issues=tuple(held)+tuple(result.issues or ())
    return DuplicateFingerprintWarmupResult(len(values),len(values),result.indexed,result.reused,
        issues,False,"ready_with_issues" if issues else "ready")


def _warm_duplicate_items(duplicate_service,items,*,prefix,label,progress=None,should_cancel=None):
    if not items:
        return DuplicateFingerprintWarmupResult(0,0,0,0,(),False,"ready")
    digest=hashlib.sha256()
    digest.update(b"ingest-duplicate-fingerprint-warmup-v1"
                  if prefix=="ingest-fingerprint-warmup" else prefix.encode("utf-8"))
    for item in sorted(items,key=lambda value:value.identity):
        digest.update(f"\0{item.identity}".encode("utf-8"))
    scope=DuplicateScope(
        prefix+":"+digest.hexdigest(),
        f"{label}: {_counted(len(items),'image')}",
        items,
    )
    completed=0

    def scan_progress(current: int,total: int,message: str) -> None:
        nonlocal completed
        completed=max(completed,int(current))
        if progress:progress(current,total,message)

    try:
        _records,issues,indexed,reused,_removed,_stale=(
            duplicate_service.index.scan(
                scope,force=False,progress=scan_progress,
                should_cancel=should_cancel,
            )
        )
    except InterruptedError:
        return DuplicateFingerprintWarmupResult(
            len(items),completed,None,None,None,True,"cancelled",
        )
    status="ready_with_issues" if issues else "ready"
    return DuplicateFingerprintWarmupResult(
        len(items),len(items),indexed,reused,issues,False,status,
    )


def build_duplicate_preflight_scope(
    plan: DuplicatePreflightScopePlan,*,progress=None,should_cancel=None,
) -> PreparedDuplicatePreflightScope:
    """Build exact cross-workspace identities without reading image content."""
    incoming=[]; dataset_items=[]; notes=[]
    library_images=[]
    for library in plan.libraries:
        images=tuple(Path(value) for value in library.images)
        library_images.append((library,images))
    total=len(plan.incoming_paths)+sum(
        len(images) for _library,images in library_images
    )
    current=0; interval=max(1,total//200) if total else 1

    def checkpoint(message: str) -> None:
        nonlocal current
        current+=1
        if should_cancel and should_cancel():
            raise InterruptedError(
                "Duplicate preflight scope preparation cancelled safely."
            )
        if progress and (current==total or current%interval==0):
            progress(current,total,message)

    for path in plan.incoming_paths:
        item=duplicate_scope_item(
            path,source_id="ingest",source_root=plan.ingest_root,
            source_label="Incoming Images",
        )
        incoming.append(item)
        checkpoint(
            f"Preparing incoming image identities {current+1:,} of {total:,}"
        )
    for library,images in library_images:
        for path in images:
            dataset_items.append(duplicate_scope_item(
                path,source_id=library.stable_id,source_root=library.root,
                source_label=library.name,
                source_profile_id=library.stable_id,
            ))
            checkpoint(
                f"Preparing included Dataset identities {current+1:,} of {total:,}"
            )
        if library.availability not in {"current","validated_checking"}:
            notes.append(
                f"{library.name}: {library.availability.replace('_',' ')}; "
                f"{_counted(len(images),'remembered image')} may be unavailable or use "
                "clearly labelled saved fingerprints."
            )
        elif not images:
            notes.append(
                f"{library.name}: no images are present in its current inventory."
            )
    incoming_by_id={item.identity:item for item in incoming}
    dataset_by_id={item.identity:item for item in dataset_items}
    items=tuple((*incoming_by_id.values(),*dataset_by_id.values()))
    digest=hashlib.sha256()
    digest.update(b"ingest-duplicate-preflight-v1")
    for item in items:
        digest.update(f"\0{item.identity}".encode("utf-8"))
    scope=DuplicateScope(
        "ingest-preflight:"+digest.hexdigest(),
        (
            f"Frozen Ingest preflight: {_counted(len(incoming_by_id),'incoming image')} "
            f"against {_counted(len(dataset_by_id),'image')} in "
            f"{_counted(len(plan.libraries),'included Dataset library','included Dataset libraries')}"
        ),
        items,
    )
    return PreparedDuplicatePreflightScope(
        scope,frozenset(incoming_by_id),len(plan.libraries),
        len(dataset_by_id),tuple(notes),tuple(
            (library.stable_id,images) for library,images in library_images
        ),
    )
