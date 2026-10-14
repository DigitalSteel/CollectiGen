from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from typing import Callable,Iterable

from .tag_collection import normalize_tag


@dataclass(frozen=True)
class EvidenceQueryResult:
    """Cached paths plus the normalized query groups each path supports."""

    paths: tuple[str,...]
    coverage: dict[str,tuple[tuple[str,...],...]]


class EvidenceQueryService:
    """Bounded process cache for read-only Tag Encyclopedia evidence queries."""

    def __init__(self,*,max_entries: int=64,max_paths: int=100_000) -> None:
        self.max_entries=max(1,int(max_entries))
        self.max_paths=max(1,int(max_paths))
        self._cache:OrderedDict[tuple,EvidenceQueryResult]=OrderedDict()
        self._cached_paths=0
        self._lock=RLock()

    @staticmethod
    def _groups(groups: Iterable[Iterable[str]]) -> tuple[tuple[str,...],...]:
        result=[]
        for group in groups:
            values=tuple(sorted({
                canonical for value in group
                if (canonical:=normalize_tag(value))
            }))
            if values:
                result.append(values)
        return tuple(sorted(set(result)))

    @staticmethod
    def _source_token(index) -> tuple:
        provider=getattr(index,"evidence_snapshot_token",None)
        if callable(provider):
            value=provider()
            return tuple(value) if isinstance(value,(tuple,list)) else (str(value),)
        return (f"{type(index).__module__}.{type(index).__qualname__}",id(index))

    def _key(self,index,groups,match_all: bool) -> tuple:
        return self._source_token(index),bool(match_all),self._groups(groups)

    def request_key(self,index,groups,match_all: bool) -> str:
        payload=json.dumps(self._key(index,groups,match_all),ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _get(self,key: tuple) -> EvidenceQueryResult|None:
        with self._lock:
            result=self._cache.get(key)
            if result is not None:
                self._cache.move_to_end(key)
            return result

    def _put(self,key: tuple,result: EvidenceQueryResult) -> None:
        if len(result.paths)>self.max_paths:
            return
        with self._lock:
            previous=self._cache.pop(key,None)
            if previous is not None:
                self._cached_paths-=len(previous.paths)
            self._cache[key]=result; self._cached_paths+=len(result.paths)
            while (
                len(self._cache)>self.max_entries
                or self._cached_paths>self.max_paths
            ):
                _,removed=self._cache.popitem(last=False)
                self._cached_paths-=len(removed.paths)

    def query_result(
        self,index,groups,match_all: bool,*,should_continue: Callable[[],bool]|None=None,
        progress: Callable[[int,int,str],None]|None=None,
    ) -> EvidenceQueryResult:
        """Return matching cached paths and per-path group coverage."""
        key=self._key(index,groups,match_all)
        cached=self._get(key)
        if cached is not None:
            return cached
        canonical_groups=key[2]
        if not canonical_groups:
            return EvidenceQueryResult((),{})
        indexed_query=getattr(index,"query_evidence_groups",None)
        if callable(indexed_query):
            indexed=indexed_query(
                canonical_groups,match_all,
                should_continue=should_continue,progress=progress,
            )
            if indexed is not None:
                paths,coverage=indexed
                result=EvidenceQueryResult(tuple(paths),dict(coverage))
                if self._source_token(index)==key[0]:
                    self._put(key,result)
                return result
        group_sets=tuple(frozenset(group) for group in canonical_groups)
        any_values=frozenset().union(*group_sets)
        records=index.all_records(); total=len(records); matches=[]; coverage={}
        for current,image in enumerate(records,1):
            if should_continue is not None and not should_continue():
                raise InterruptedError("Evidence query cancelled")
            image_tags={value for item in image.tags if (value:=normalize_tag(item))}
            matched_groups=tuple(
                group for group,group_set in zip(canonical_groups,group_sets)
                if group_set.intersection(image_tags)
            )
            matched=(
                len(matched_groups)==len(canonical_groups)
                if match_all else bool(any_values.intersection(image_tags))
            )
            if matched:
                path=str(image.path); matches.append(path)
                coverage[path]=matched_groups
            if progress is not None and (current==total or current%256==0):
                progress(current,total,"Searching cached Image Knowledge")
        result=EvidenceQueryResult(tuple(matches),coverage)
        if self._source_token(index)==key[0]:
            self._put(key,result)
        return result

    def query(
        self,index,groups,match_all: bool,*,should_continue: Callable[[],bool]|None=None,
        progress: Callable[[int,int,str],None]|None=None,
    ) -> tuple[str,...]:
        """Return matching cached paths without reading image files or sidecars."""
        return self.query_result(
            index,groups,match_all,
            should_continue=should_continue,progress=progress,
        ).paths

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear(); self._cached_paths=0
