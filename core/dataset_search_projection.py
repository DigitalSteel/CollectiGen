"""Reusable, local Dataset search projections.

The Dataset index remains the disposable source of cached Image Knowledge.  This
module keeps a normalized in-memory view of that cache so typeahead does not
rebuild a Python search string for every cached row on the Qt thread.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from typing import Mapping

from .image_workflow_state import image_preparation_stage


def normalize_dataset_query(value: str) -> str:
    """Normalize the display query without changing typed evidence semantics."""
    return str(value or "").strip().casefold()


@dataclass(frozen=True)
class DatasetSearchSpec:
    query: str = ""
    stage_mode: str = "all"
    state_mode: str = "all"
    projection_mode: str = "all"

    def __post_init__(self) -> None:
        object.__setattr__(self, "query", normalize_dataset_query(self.query))
        for name in ("stage_mode", "state_mode", "projection_mode"):
            object.__setattr__(self, name, str(getattr(self, name) or "all"))


@dataclass(frozen=True)
class DatasetSearchProjection:
    """An immutable result from one cached Dataset snapshot."""

    source_token: tuple[object, ...]
    memory_token: tuple[object, ...]
    paths: tuple[object, ...]
    records: Mapping[object, object]
    stage_counts: Mapping[str, int]
    unknown_count: int
    stale_paths: tuple[object, ...] = ()
    retained_selected: tuple[str, ...] = ()
    paused_count: int = 0


@dataclass(frozen=True)
class _NormalizedRow:
    path: object
    searchable: str


@dataclass(frozen=True)
class _Catalog:
    token: tuple[object, ...]
    memory_token: tuple[object, ...]
    dataset: object
    index: object
    inventory_owners: tuple[object, ...]
    paths: tuple[object, ...]
    records: Mapping[object, object]
    rows: tuple[_NormalizedRow, ...]


@dataclass(frozen=True)
class _ProjectionCacheContext:
    key: tuple[object, ...]
    source_token: tuple[object, ...]
    memory_token: tuple[object, ...]
    readiness_owner: object
    readiness_revision: int
    source_generation: int


@dataclass(frozen=True)
class _ProjectionCacheEntry:
    readiness_owner: object
    projection: DatasetSearchProjection


class DatasetSearchProjectionStore:
    """Bounded per-page cache of normalized, typed Dataset search rows.

    It is intentionally in-memory and rebuildable.  The source token includes
    the Dataset index's own revision/snapshot and inventory identity, so it
    never becomes portable Image Knowledge or a second storage authority.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._catalog: _Catalog | None = None
        self._projection_results: OrderedDict[
            tuple[object, ...], _ProjectionCacheEntry
        ] = OrderedDict()
        self._projection_result_limit = 8

    def invalidate_projections(self) -> None:
        """Drop reusable results after a page-owned source invalidation."""
        with self._lock:
            self._projection_results.clear()

    @staticmethod
    def _index_token(index) -> tuple[object, ...]:
        if index is None:
            return ("no-index",)
        provider = getattr(index, "evidence_snapshot_token", None)
        if callable(provider):
            try:
                value = provider()
            except Exception:
                value = None
            if value is not None:
                values = tuple(value) if isinstance(value, (tuple, list)) else (value,)
                return ("snapshot", *values)
        return (
            "memory", id(index),
            int(getattr(index, "_records_revision", 0) or 0),
        )

    @staticmethod
    def _inventory_token(dataset, images) -> tuple[object, ...]:
        managers = getattr(dataset, "scoped_managers", None)
        if callable(managers):
            try:
                values = tuple(managers())
            except Exception:
                values = ()
            if values:
                return tuple((id(manager.images), len(manager.images)) for manager in values)
        return (id(images), len(images))

    @staticmethod
    def _memory_index_token(dataset, index) -> tuple[object, ...]:
        managers=getattr(dataset,"scoped_managers",None)
        if callable(managers):
            try:
                values=tuple(managers())
            except Exception:
                values=()
            if values:
                return tuple(
                    (id(getattr(manager,"index",None)),int(getattr(
                        getattr(manager,"index",None),"_records_revision",0,
                    ) or 0)) for manager in values
                )
        return (id(index),int(getattr(index,"_records_revision",0) or 0))

    @classmethod
    def memory_token(cls,dataset) -> tuple[object, ...]:
        """Read no disk: enough identity/revision evidence for UI stale refusal."""
        index=getattr(dataset,"index",None)
        managers=getattr(dataset,"scoped_managers",None)
        if callable(managers):
            try:
                values=tuple(managers())
            except Exception:
                values=()
            if values:
                return (
                    id(dataset),cls._memory_index_token(dataset,index),
                    tuple((id(manager.images),len(manager.images)) for manager in values),
                    cls.availability_token(dataset),
                )
        images=getattr(dataset,"images",())
        return (
            id(dataset),cls._memory_index_token(dataset,index),
            cls._inventory_token(dataset,images),
            cls.availability_token(dataset),
        )

    @staticmethod
    def availability_token(dataset) -> tuple[object, ...]:
        profiles=getattr(dataset,"scoped_profile_managers",None)
        if callable(profiles):
            return tuple((profile.stable_id,dataset.library_availability(profile.stable_id),
                          id(getattr(manager,"storage_states",None)),len(getattr(manager,"storage_states",())))
                         for profile,manager in profiles())
        states=getattr(dataset,"storage_states",None)
        return (id(states),len(states) if states is not None else 0)

    def _source_token(self, dataset, index, images) -> tuple[object, ...]:
        return (
            id(dataset), id(index), self._index_token(index),
            self._inventory_token(dataset, images),
        )

    @staticmethod
    def _normalize_row(path, record) -> _NormalizedRow:
        # Tags and descriptions remain independently typed in the source cache.
        # They are concatenated only for this user-facing text match.
        values = [str(path)]
        if record is not None:
            values.extend(str(value) for value in (getattr(record, "tags", ()) or ()))
            values.extend(
                str(value) for value in (getattr(record, "plain_language", ()) or ())
            )
        return _NormalizedRow(path, normalize_dataset_query(" ".join(values)))

    def _catalog_for(
        self,dataset,index,*,should_continue=None,
        source_token=None,memory_token=None,
    ) -> _Catalog:
        inventory=getattr(dataset,"images",())
        managers=getattr(dataset,"scoped_managers",None)
        owners=tuple(managers()) if callable(managers) else (dataset,)
        inventories=tuple(getattr(owner,"images",()) for owner in owners)
        availability=tuple(getattr(owner,"storage_states",None) for owner in owners)
        token=(
            source_token if source_token is not None
            else self._source_token(dataset,index,inventory)
        )
        memory_token=(
            memory_token if memory_token is not None
            else self.memory_token(dataset)
        )
        with self._lock:
            catalog = self._catalog
            if (
                catalog is not None and catalog.token == token
                and catalog.memory_token == memory_token
                and catalog.dataset is dataset and catalog.index is index
            ):
                return catalog
        if should_continue is not None and not should_continue():
            raise InterruptedError("Dataset search cancelled")
        images=tuple(inventory or ())
        records = index.records_by_path() if index is not None else {}
        rows = []
        for position, path in enumerate(images, 1):
            if position % 256 == 0 and should_continue is not None and not should_continue():
                raise InterruptedError("Dataset search cancelled")
            rows.append(self._normalize_row(path, records.get(path)))
        # An index update during the cache read gets a fresh request instead of
        # a mixed projection.  The next request will use the new token.
        final_inventory=getattr(dataset,"images",())
        final_token = self._source_token(dataset, index, final_inventory)
        if final_token != token or self.memory_token(dataset)!=memory_token:
            raise InterruptedError("Dataset search source changed")
        catalog = _Catalog(token, memory_token, dataset, index,
                           (*inventories,*availability),images, records, tuple(rows))
        with self._lock:
            self._catalog = catalog
        return catalog

    def cached_projection(
        self,dataset,spec: DatasetSearchSpec,*,readiness_owner,
        source_generation: int,selected_paths=(),should_continue=None,
    ) -> tuple[DatasetSearchProjection | None,_ProjectionCacheContext | None]:
        """Look up an exact result without copying the readiness snapshot.

        Source proof may read the disposable SQLite index, so callers invoke
        this only inside their cancellable worker. Unrevisioned mappings and
        selected scopes bypass reuse because neither has a stable exact key.
        """
        if selected_paths:
            return None,None
        revision=getattr(readiness_owner,"revision",None)
        if revision is None:
            return None,None
        if should_continue is not None and not should_continue():
            raise InterruptedError("Dataset search cancelled")
        index=getattr(dataset,"index",None)
        source_token=self._source_token(
            dataset,index,getattr(dataset,"images",()),
        )
        memory_token=self.memory_token(dataset)
        if should_continue is not None and not should_continue():
            raise InterruptedError("Dataset search cancelled")
        context=_ProjectionCacheContext(
            (
                source_token,memory_token,id(readiness_owner),int(revision),
                int(source_generation),spec,
            ),
            source_token,memory_token,readiness_owner,int(revision),
            int(source_generation),
        )
        with self._lock:
            entry=self._projection_results.get(context.key)
            if entry is None or entry.readiness_owner is not readiness_owner:
                return None,context
            self._projection_results.move_to_end(context.key)
            return entry.projection,context

    def _remember_projection(
        self,context: _ProjectionCacheContext,projection: DatasetSearchProjection,
    ) -> None:
        entry=_ProjectionCacheEntry(context.readiness_owner,projection)
        with self._lock:
            self._projection_results[context.key]=entry
            self._projection_results.move_to_end(context.key)
            while len(self._projection_results)>self._projection_result_limit:
                self._projection_results.popitem(last=False)

    @staticmethod
    def _workflow_for(readiness_cache, path):
        value = readiness_cache.get(path)
        return value[1] if value is not None else None

    @staticmethod
    def _matches(spec: DatasetSearchSpec, record, workflow) -> bool:
        has_record = bool(record and getattr(record, "sidecar_exists", False))
        if spec.stage_mode != "all" and (
            (has_record and workflow is None)
            or image_preparation_stage(workflow,has_record=has_record) != spec.stage_mode
        ):
            return False
        if spec.projection_mode == "record_missing" and has_record:
            return False
        if spec.projection_mode in {"current", "needs_sync"}:
            if workflow is None:
                return False
            needs_sync = getattr(workflow, "training_export_health_state", "") != "current"
            if spec.projection_mode == "current" and needs_sync:
                return False
            if spec.projection_mode == "needs_sync" and not needs_sync:
                return False
        if spec.state_mode != "all":
            state_matches = {
                "ready": bool(workflow and getattr(workflow, "ready", False)),
                "ai": bool(workflow and getattr(workflow, "key", "") == "ai"),
                "review": bool(workflow and getattr(workflow, "key", "") == "review"),
                "incomplete": bool(
                    not has_record or workflow and getattr(workflow, "key", "") == "incomplete"
                ),
                "problem": bool(workflow and getattr(workflow, "key", "") == "problem"),
            }.get(spec.state_mode, True)
            if not state_matches:
                return False
        return True

    def project(
        self, dataset, spec: DatasetSearchSpec, *, readiness_cache,
        selected_paths=(), should_continue=None,cache_context=None,
        cache_guard=None,
    ) -> DatasetSearchProjection:
        """Build one filter result without opening images or sidecars."""
        index = getattr(dataset, "index", None)
        catalog = self._catalog_for(
            dataset,index,should_continue=should_continue,
            source_token=(cache_context.source_token if cache_context else None),
            memory_token=(cache_context.memory_token if cache_context else None),
        )
        needs_derived = (
            spec.stage_mode in {"unprepared", "partial", "complete"}
            or spec.state_mode != "all"
            or spec.projection_mode in {"current", "needs_sync"}
        )
        readable_paths = None
        if needs_derived:
            snapshot = getattr(dataset, "storage_availability_by_path", None)
            if callable(snapshot):
                readable_paths = {
                    path for path, status in snapshot().items()
                    if bool(getattr(status, "readable", True))
                }
            else:
                availability=getattr(dataset,"storage_availability",None)
                if callable(availability):
                    readable_paths={path for path in catalog.paths
                                    if bool(getattr(availability(path),"readable",True))}
        stale = []
        paused_count=0
        library_availability=getattr(dataset,"availability_for_path",lambda _path:"current")
        counts = {"unprepared": 0, "partial": 0, "complete": 0}
        unknown = 0
        matches = []
        for position, row in enumerate(catalog.rows, 1):
            if position % 256 == 0 and should_continue is not None and not should_continue():
                raise InterruptedError("Dataset search cancelled")
            path = row.path
            record = catalog.records.get(path)
            workflow = self._workflow_for(readiness_cache, path)
            has_record = bool(record and getattr(record, "sidecar_exists", False))
            if not has_record:
                counts["unprepared"] += 1
            elif workflow is None:
                unknown += 1
            else:
                counts[image_preparation_stage(workflow,has_record=True)] += 1
            readable = (
                path in readable_paths if readable_paths is not None else True
            )
            if needs_derived and has_record and readable and workflow is None:
                if library_availability(path)=="validated_cancelled":
                    paused_count+=1
                else:
                    stale.append(path)
                continue
            if needs_derived and not readable:
                continue
            if not self._matches(spec, record, workflow):
                continue
            if spec.query and spec.query not in row.searchable:
                continue
            matches.append(path)
        retained_selected = tuple(
            str(path) for path in matches if str(path) in selected_paths
        )
        # Bind the result to the source used to construct it, never to a newer
        # revision observed only after matching the old normalized rows.
        if (self.memory_token(dataset)!=catalog.memory_token
                or self._source_token(dataset,index,getattr(dataset,"images",()))!=catalog.token):
            raise InterruptedError("Dataset search source changed")
        if should_continue is not None and not should_continue():
            raise InterruptedError("Dataset search cancelled")
        projection = DatasetSearchProjection(
            catalog.token, catalog.memory_token, tuple(matches), catalog.records, counts, unknown,
            tuple(stale), retained_selected,paused_count,
        )
        if cache_context is not None:
            cache_current=(
                not selected_paths
                and cache_context.source_token==catalog.token
                and cache_context.memory_token==catalog.memory_token
                and getattr(cache_context.readiness_owner,"revision",None)
                ==cache_context.readiness_revision
                and (cache_guard is None or cache_guard())
            )
            if cache_current:
                self._remember_projection(cache_context,projection)
        return projection
