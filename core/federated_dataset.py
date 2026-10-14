from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Callable, Iterable

from .dataset_manager import DatasetManager,DatasetSummary
from .library_profiles import LibraryProfile,LibraryProfileRegistry
from .library_storage import LibraryFileAvailability,LibraryFileState


@dataclass(frozen=True,slots=True)
class LibraryImageIdentity:
    """Stable image identity that does not depend on a machine-local root."""

    library_id: str
    relative_path: str

    @property
    def key(self) -> str:
        return f"{self.library_id}:{self.relative_path}"


class FederatedIndexConnections:
    """One routed connection per local per-library index."""

    def __init__(self,connections: dict[str,object],*,indexes: dict[str,object] | None=None,
                 allowed_library_ids: frozenset[str] | None=None):
        self.connections=connections
        self._indexes=indexes or {}
        self._allowed_library_ids=allowed_library_ids

    def for_library(self,library_id: str):
        library_id=str(library_id)
        if (self._allowed_library_ids is not None and
                library_id not in self._allowed_library_ids):
            raise ValueError("The image is outside the selected library scope.")
        if library_id not in self.connections and library_id in self._indexes:
            self.connections[library_id]=self._indexes[library_id].connect()
        return self.connections.get(library_id)

    def commit(self) -> None:
        for connection in self.connections.values():
            connection.commit()

    def rollback(self) -> None:
        for connection in self.connections.values():
            connection.rollback()

    def close(self) -> None:
        for connection in self.connections.values():
            connection.close()


class FederatedDatasetIndex:
    """Read/write facade that routes each image to its library-local index."""

    def __init__(self,dataset: "FederatedDatasetManager"):
        self.dataset=dataset

    def _target(self,image):
        manager=self.dataset.manager_for_path(image)
        return manager.index if manager is not None else None

    def _connection(self,image,db):
        if not isinstance(db,FederatedIndexConnections):
            return db
        identity=self.dataset.identity_for_path(image)
        return db.for_library(identity.library_id) if identity is not None else None

    def _scoped_indexes(self):
        return tuple(
            (profile.stable_id,manager.index)
            for profile,manager in self.dataset.scoped_profile_managers()
            if manager.index is not None
        )

    def _selected_indexes(self, library_ids: Iterable[str] | None):
        if library_ids is None:
            return self._scoped_indexes()
        requested=tuple(dict.fromkeys(str(value) for value in library_ids))
        if set(requested)-set(self.dataset.scope_library_ids):
            raise ValueError("The selected library scope changed; review it again.")
        selected=set(requested)
        return tuple((library_id,index) for library_id,index in self._scoped_indexes()
                     if library_id in selected)

    def evidence_snapshot_token(self) -> tuple:
        """Identify the complete active evidence snapshot across libraries."""
        return self.evidence_snapshot_token_for_libraries(None)

    def evidence_snapshot_token_for_libraries(
        self, library_ids: Iterable[str] | None,
    ) -> tuple:
        """Identify only the selected libraries' durable evidence snapshots."""
        snapshots=[]
        for library_id,index in self._selected_indexes(library_ids):
            provider=getattr(index,"evidence_snapshot_token",None)
            value=(
                provider()
                if callable(provider) else
                (f"{type(index).__module__}.{type(index).__qualname__}",id(index))
            )
            token=tuple(value) if isinstance(value,(tuple,list)) else (str(value),)
            snapshots.append((library_id,*token))
        return "federated",tuple(snapshots)

    def query_evidence_groups(
        self,groups,match_all: bool,*,should_continue=None,progress=None,
    ):
        """Merge scoped library-local evidence queries without opening images."""
        indexes=self._scoped_indexes()
        paths=[]; coverage={}; total=len(indexes)
        for position,(_library_id,index) in enumerate(indexes,1):
            if should_continue is not None and not should_continue():
                raise InterruptedError("Evidence query cancelled")
            query=getattr(index,"query_evidence_groups",None)
            if not callable(query):
                return None
            result=query(
                groups,match_all,should_continue=should_continue,progress=None,
            )
            if result is None:
                return None
            library_paths,library_coverage=result
            for path in library_paths:
                if path not in coverage:
                    paths.append(path)
                coverage[path]=tuple(library_coverage.get(path,()))
            if progress is not None:
                progress(
                    position,total,
                    f"Searching indexed Image Knowledge ({position}/{total} libraries)",
                )
        return tuple(paths),coverage

    def all_records(self):
        records=[]
        for manager in self.dataset.scoped_managers():
            if manager.index is not None:
                records.extend(manager.index.all_records())
        return records

    def iter_records(self, *, should_cancel: Callable[[], bool] | None=None,
                     batch_size: int=256):
        return self.iter_records_for_libraries(
            None,should_cancel=should_cancel,batch_size=batch_size,
        )

    def iter_records_for_libraries(
        self, library_ids: Iterable[str] | None, *,
        should_cancel: Callable[[], bool] | None=None, batch_size: int=256,
    ):
        """Stream selected local indexes in registry order without full decoding."""
        if batch_size < 1:
            raise ValueError("Choose a positive index batch size.")
        indexes=self._selected_indexes(library_ids)

        def rows():
            for _library_id,index in indexes:
                if should_cancel is not None and should_cancel():
                    raise InterruptedError("Indexed image scan cancelled.")
                yield from index.iter_records(
                    should_cancel=should_cancel,batch_size=batch_size,
                )

        return rows()

    def records_by_path(self):
        records={}
        for manager in self.dataset.scoped_managers():
            if manager.index is not None:
                records.update(manager.index.records_by_path())
        return records

    def get_if_fresh(self,image,db=None):
        index=self._target(image)
        return (
            index.get_if_fresh(image,self._connection(image,db))
            if index is not None else None
        )

    def put(
        self,image,tags,error="",db=None,tag_sources=None,plain_language=None,
    ):
        index=self._target(image)
        if index is None:
            raise ValueError(f"No loaded named library owns {image}")
        return index.put(
            image,tags,error,self._connection(image,db),tag_sources,
            plain_language,
        )

    def connect(self) -> FederatedIndexConnections:
        return FederatedIndexConnections({
            profile.stable_id:manager.index.connect()
            for profile,manager in self.dataset.scoped_profile_managers()
            if manager.index is not None
        })

    def connect_for_libraries(
        self, library_ids: Iterable[str] | None,
    ) -> FederatedIndexConnections:
        """Open each selected local connection only when its first row is checked."""
        indexes=dict(self._selected_indexes(library_ids))
        return FederatedIndexConnections(
            {},indexes=indexes,allowed_library_ids=frozenset(indexes),
        )


class FederatedDatasetManager:
    """Aggregate peer libraries without merging their roots or authority."""

    def __init__(
        self,registry: LibraryProfileRegistry | None=None,*,
        preferred_library_id: str="",scope_library_ids: Iterable[str]=(),
        knowledge=None,
    ):
        self.registry=registry or LibraryProfileRegistry()
        self.preferred_library_id=str(preferred_library_id or "")
        self._scope_library_ids=tuple(str(value) for value in scope_library_ids if str(value))
        self._knowledge=knowledge
        self._managers: dict[str,DatasetManager]={}
        self._resolved_roots: dict[str,tuple[str,Path]]={}
        self._availability: dict[str,str]={}
        self.index=FederatedDatasetIndex(self)
        self.configure(
            self.registry,preferred_library_id=self.preferred_library_id,
            scope_library_ids=self._scope_library_ids,
        )

    @property
    def knowledge(self):
        return self._knowledge

    @knowledge.setter
    def knowledge(self,value) -> None:
        self._knowledge=value
        for manager in self._managers.values():
            manager.knowledge=value

    def configure(
        self,registry: LibraryProfileRegistry,*,preferred_library_id: str="",
        scope_library_ids: Iterable[str] | None=None,
    ) -> None:
        previous_roots=dict(self._resolved_roots)
        retained={}; availability={}
        for profile in registry.profiles:
            if registry.validate(profile,replacing_id=profile.stable_id):
                continue
            manager=self._managers.get(profile.stable_id)
            root_changed=False
            if manager is None:
                manager=DatasetManager(knowledge=self._knowledge)
            else:
                prior=previous_roots.get(profile.stable_id)
                current=os.path.normcase(os.path.abspath(os.fspath(
                    Path(profile.root).expanduser()
                )))
                if prior is not None and prior[0]!=current:
                    manager.clear(); root_changed=True
            retained[profile.stable_id]=manager
            manager.storage_role=profile.role
            availability[profile.stable_id]=self._availability.get(
                profile.stable_id,"not_loaded",
            ) if not root_changed else "not_loaded"
        self.registry=registry
        self._managers=retained
        self._availability=availability
        self._resolved_roots={
            profile.stable_id:(
                os.path.normcase(os.path.abspath(os.fspath(Path(profile.root).expanduser()))),
                Path(os.path.abspath(os.fspath(Path(profile.root).expanduser()))),
            )
            for profile in registry.profiles
            if profile.stable_id in retained
        }
        wanted=str(preferred_library_id or self.preferred_library_id)
        self.preferred_library_id=(
            wanted if wanted in retained else
            next(iter(retained),"")
        )
        if scope_library_ids is not None:
            self._scope_library_ids=tuple(
                dict.fromkeys(str(value) for value in scope_library_ids if str(value) in retained)
            )
        else:
            self._scope_library_ids=tuple(
                value for value in self._scope_library_ids if value in retained
            )

    @property
    def scope_library_ids(self) -> tuple[str,...]:
        if self._scope_library_ids:
            return self._scope_library_ids
        return tuple(
            profile.stable_id for profile in self.registry.profiles
            if profile.stable_id in self._managers
        )

    @property
    def explicit_scope_library_ids(self) -> tuple[str,...]:
        return self._scope_library_ids

    def set_scope(self,library_ids: Iterable[str]) -> tuple[str,...]:
        requested=tuple(dict.fromkeys(str(value) for value in library_ids if str(value)))
        unknown=[value for value in requested if value not in self._managers]
        if unknown:
            raise KeyError(", ".join(unknown))
        self._scope_library_ids=requested
        return self.scope_library_ids

    def profile(self,library_id: str) -> LibraryProfile | None:
        return self.registry.get(str(library_id))

    def manager(self,library_id: str) -> DatasetManager | None:
        return self._managers.get(str(library_id))

    def set_library_availability(self,library_id: str,state: str) -> None:
        library_id=str(library_id)
        if library_id not in self._managers:
            raise KeyError(library_id)
        self._availability[library_id]=str(state or "not_loaded")

    def library_availability(self,library_id: str) -> str:
        return self._availability.get(str(library_id),"not_loaded")

    def availability_for_path(self,path: str | Path) -> str:
        library_id=self._library_id_for_path(path)
        return self.library_availability(library_id) if library_id else "outside_library"

    def storage_availability(self,path: str | Path) -> LibraryFileAvailability:
        library_id=self._library_id_for_path(path)
        manager=self.manager(library_id)
        if manager is None:
            return LibraryFileAvailability(
                LibraryFileState.UNAVAILABLE,
                "No configured image library owns this path.",
                "Choose an image from an included named library.",
                False,False,
            )
        return self._manager_storage_availability(
            manager,self.library_availability(library_id),path,
        )

    def storage_availability_by_path(self) -> dict[Path,LibraryFileAvailability]:
        """Project exact cached availability for the scoped inventory without routing each path."""
        result={}
        available=LibraryFileAvailability()
        for profile,manager in self.scoped_profile_managers():
            state=self.library_availability(profile.stable_id)
            if state=="current":
                # Inventory keys are already Paths; share the immutable default
                # instead of constructing a Path and status for every image.
                result.update(
                    (path,manager.storage_states.get(path,available))
                    for path in manager.images
                )
                continue
            for path in manager.images:
                result[path]=self._manager_storage_availability(manager,state,path)
        return result

    @staticmethod
    def _manager_storage_availability(manager,library_state,path) -> LibraryFileAvailability:
        if library_state in {"validated_checking","validated_cancelled"}:
            paused=library_state=="validated_cancelled"
            current=manager.storage_states.get(Path(path))
            if current is None:
                return LibraryFileAvailability(
                    LibraryFileState.UNAVAILABLE,
                    "The current file has not finished its startup availability check.",
                    "Wait for the library check to finish before opening or changing this image.",
                    False,False,
                )
            if not current.readable:
                return current
            return LibraryFileAvailability(
                current.state,
                ("Available for browsing; library checks are paused." if paused else
                 "Available for browsing while startup processing finishes."),
                ("Resume library checks before changing this image." if paused else
                 "Wait for the current library scan to finish before changing this image."),
                True,False,current.conflict_paths,
            )
        if library_state!="current":
            current=manager.storage_states.get(Path(path))
            detail=(
                current.detail
                if current is not None and current.state is not LibraryFileState.AVAILABLE
                else "The source library is not currently verified."
            )
            recovery=(
                current.recovery
                if current is not None and current.state is not LibraryFileState.AVAILABLE
                else "Restore access and refresh the library before opening or changing this image."
            )
            return LibraryFileAvailability(
                current.state if current is not None and current.state is not LibraryFileState.AVAILABLE else LibraryFileState.UNAVAILABLE,
                detail,recovery,False,False,
                current.conflict_paths if current is not None else (),
            )
        return manager.storage_availability(path)

    def manager_for_path(self,path: str | Path) -> DatasetManager | None:
        library_id=self._library_id_for_path(path)
        return self.manager(library_id) if library_id else None

    def _library_id_for_path(self,path: str | Path) -> str:
        absolute=os.path.normcase(os.path.abspath(os.fspath(Path(path).expanduser())))
        for library_id,(root_key,_root) in self._resolved_roots.items():
            try:
                if os.path.commonpath((absolute,root_key))==root_key:
                    return library_id
            except ValueError:
                continue
        return ""

    def identity_for_path(self,path: str | Path) -> LibraryImageIdentity | None:
        library_id=self._library_id_for_path(path)
        if not library_id:
            return None
        absolute=os.path.abspath(os.fspath(Path(path).expanduser()))
        root=self._resolved_roots[library_id][1]
        relative=Path(os.path.relpath(absolute,root)).as_posix()
        return LibraryImageIdentity(library_id,relative)

    def path_for_identity(self,identity: LibraryImageIdentity) -> Path:
        profile=self.profile(identity.library_id)
        if profile is None:
            raise KeyError(identity.library_id)
        relative=Path(identity.relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Library image identity must contain a safe relative path.")
        root=self._resolved_roots.get(identity.library_id)
        return (root[1] if root is not None else Path(profile.root).expanduser())/relative

    def profile_manager(self,library_id: str):
        profile=self.profile(library_id); manager=self.manager(library_id)
        return (profile,manager) if profile is not None and manager is not None else (None,None)

    def profile_managers(self):
        for profile in self.registry.profiles:
            manager=self._managers.get(profile.stable_id)
            if manager is not None:
                yield profile,manager

    def scoped_profile_managers(self):
        allowed=set(self.scope_library_ids)
        for profile,manager in self.profile_managers():
            if profile.stable_id in allowed:
                yield profile,manager

    def scoped_managers(self):
        return tuple(manager for _profile,manager in self.scoped_profile_managers())

    @property
    def all_images(self) -> list[Path]:
        return [image for _profile,manager in self.profile_managers() for image in manager.images]

    @property
    def readable_images(self) -> list[Path]:
        return [
            image for profile,manager in self.scoped_profile_managers()
            if self.library_availability(profile.stable_id) in {
                "current","validated_checking","validated_cancelled",
            }
            for image in manager.readable_images
            if self.library_availability(profile.stable_id)=="current"
            or self._manager_storage_availability(
                manager,self.library_availability(profile.stable_id),image,
            ).readable
        ]

    @property
    def writable_images(self) -> list[Path]:
        return [
            image for profile,manager in self.scoped_profile_managers()
            if self.library_availability(profile.stable_id)=="current"
            for image in manager.writable_images
        ]

    @property
    def images(self) -> list[Path]:
        return [image for manager in self.scoped_managers() for image in manager.images]

    @property
    def root(self) -> Path | None:
        preferred=self.manager(self.preferred_library_id)
        if (
            self.preferred_library_id in self.scope_library_ids
            and preferred is not None and preferred.root is not None
        ):
            return preferred.root
        return next((manager.root for manager in self.scoped_managers() if manager.root is not None),None)

    @property
    def roots(self) -> tuple[Path,...]:
        return tuple(manager.root for manager in self.scoped_managers() if manager.root is not None)

    @property
    def include_subfolders(self) -> bool:
        preferred=self.manager(self.preferred_library_id)
        return preferred.include_subfolders if preferred is not None else True

    @staticmethod
    def _aggregate_summary(managers) -> DatasetSummary:
        summary=DatasetSummary()
        for manager in managers:
            for field in summary.__dataclass_fields__:
                setattr(summary,field,getattr(summary,field)+getattr(manager.summary,field))
        return summary

    @property
    def summary(self) -> DatasetSummary:
        summary=self._aggregate_summary(self.scoped_managers())
        summary.unique_tags=len(self.tag_counts)
        return summary

    @property
    def all_summary(self) -> DatasetSummary:
        managers=tuple(manager for _profile,manager in self.profile_managers())
        summary=self._aggregate_summary(managers)
        summary.unique_tags=len({
            tag for manager in managers for tag in manager.tag_counts
        })
        return summary

    @property
    def tag_counts(self) -> dict[str,int]:
        combined={}
        for manager in self.scoped_managers():
            for tag,count in manager.tag_counts.items():
                combined[tag]=combined.get(tag,0)+count
        return combined

    @property
    def tag_sources(self) -> dict[str,set[str]]:
        combined={}
        for manager in self.scoped_managers():
            for tag,sources in manager.tag_sources.items():
                combined.setdefault(tag,set()).update(sources)
        return combined

    @property
    def tag_examples(self) -> dict[str,str]:
        combined={}
        for manager in self.scoped_managers():
            for tag,path in manager.tag_examples.items():
                combined.setdefault(tag,path)
        return combined

    @property
    def indexed_images(self) -> set[Path]:
        return set().union(*(manager.indexed_images for manager in self.scoped_managers()))

    @property
    def validated_records(self) -> dict[Path,object]:
        return {
            path:record for manager in self.scoped_managers()
            for path,record in manager.validated_records.items()
        }

    @property
    def changed_images(self) -> set[Path]:
        return set().union(*(manager.changed_images for manager in self.scoped_managers()))

    @property
    def schema_migration_candidates(self) -> list[Path]:
        return [
            path for manager in self.scoped_managers()
            for path in manager.schema_migration_candidates
        ]

    def cache_managed_record(self,image: Path,record,*,db=None):
        manager=self.manager_for_path(image)
        if manager is None:
            raise ValueError(f"No named library owns {image}")
        connection=db
        if isinstance(db,FederatedIndexConnections):
            identity=self.identity_for_path(image)
            connection=db.for_library(identity.library_id) if identity is not None else None
        return manager.cache_managed_record(image,record,db=connection)

    def prioritize(self,images: list[Path]) -> DatasetSummary:
        grouped={}
        for image in images:
            identity=self.identity_for_path(image)
            if identity is not None:
                grouped.setdefault(identity.library_id,[]).append(Path(image))
        for library_id,paths in grouped.items():
            manager=self.manager(library_id)
            if manager is not None:
                manager.prioritize(paths)
        return self.summary

    def clear(self) -> None:
        for library_id,manager in self._managers.items():
            manager.clear()
            self._availability[library_id]="not_loaded"

    def clear_library(self,library_id: str) -> None:
        manager=self.manager(library_id)
        if manager is not None:
            manager.clear()
            self._availability[str(library_id)]="not_loaded"
