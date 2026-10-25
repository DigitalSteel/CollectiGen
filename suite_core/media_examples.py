"""Disconnected read-only media examples; no core, Qt or publication imports.

The host injects its existing EvidenceQueryService and already-admitted dataset.
Returned references are private host data, never Knowledge publication fields.
"""
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from pathlib import PurePosixPath, PureWindowsPath
from typing import Callable, Protocol


@dataclass(frozen=True)
class MediaReference:
    library_id: str
    relative_path: str

    def __post_init__(self):
        path = self.relative_path
        if (not isinstance(self.library_id, str) or not self.library_id.strip()
                or not isinstance(path, str) or not path or any(ord(c) < 32 for c in path)
                or "\\" in path or PurePosixPath(path).is_absolute()
                or PureWindowsPath(path).drive
                or any(part in ("", ".", "..") for part in path.split("/"))):
            raise ValueError("Media references require a library and safe relative path")


@dataclass(frozen=True)
class ExampleIdentity:
    """Host-approved identity_names, not aliases inferred from a label."""
    tag_id: str
    names: tuple[str, ...]
    category_id: str = ""

    def __post_init__(self):
        if (not isinstance(self.tag_id, str) or not self.tag_id
                or not isinstance(self.category_id, str)
                or not isinstance(self.names, tuple) or not self.names
                or any(not isinstance(name, str) or not name.strip() for name in self.names)):
            raise ValueError("An example identity requires immutable approved names")


@dataclass(frozen=True)
class FrozenEvidenceRecord:
    """Private host-local cached input, not a publication or result field."""
    path: str
    tags: tuple[str, ...]

    def __post_init__(self):
        if (not isinstance(self.path, str) or not self.path
                or not isinstance(self.tags, tuple)
                or any(not isinstance(tag, str) for tag in self.tags)):
            raise ValueError("Cached evidence requires an immutable path/tag record")


@dataclass(frozen=True)
class FrozenEvidenceSnapshot:
    """Host-admitted cached rows and a coherent monotonic revision token."""
    revision: str
    records: tuple[FrozenEvidenceRecord, ...]
    content_token: str = field(init=False, repr=False)

    def __post_init__(self):
        if (not isinstance(self.revision, str) or not self.revision
                or not isinstance(self.records, tuple)
                or any(not isinstance(row, FrozenEvidenceRecord) for row in self.records)):
            raise ValueError("Cached evidence requires an immutable versioned snapshot")
        digest = sha256()
        for row in self.records:
            for text in (row.path, *row.tags):
                encoded = text.encode("utf-8")
                digest.update(str(len(encoded)).encode("ascii") + b":" + encoded)
            digest.update(b";")
        object.__setattr__(self, "content_token", digest.hexdigest())


class _MemoryIndex:
    """EvidenceQueryService-compatible view; its fallback reads memory only."""
    def __init__(self, library_id, binding, snapshot):
        self.snapshot = snapshot
        self.token = (library_id, binding, snapshot.revision, snapshot.content_token)

    def evidence_snapshot_token(self):
        return self.token

    def all_records(self):
        return self.snapshot.records


@dataclass(frozen=True)
class ExampleRequest:
    selection_token: str
    knowledge_revision: str
    library_ids: tuple[str, ...]
    identities: tuple[ExampleIdentity, ...]
    match_all: bool = True

    def __post_init__(self):
        if (not isinstance(self.selection_token, str) or not self.selection_token
                or not isinstance(self.knowledge_revision, str) or not self.knowledge_revision
                or not isinstance(self.library_ids, tuple)
                or any(not isinstance(value, str) or not value for value in self.library_ids)
                or len(set(self.library_ids)) != len(self.library_ids)
                or not isinstance(self.identities, tuple)
                or any(not isinstance(value, ExampleIdentity) for value in self.identities)
                or len({value.tag_id for value in self.identities}) != len(self.identities)
                or not isinstance(self.match_all, bool)):
            raise ValueError("An example request requires a frozen exact scope")

    @classmethod
    def for_category(cls, selection_token, knowledge_revision, library_ids,
                     category_ids, identities):
        """OR across explicit category membership, including supplied descendants.

        The native host supplies authoritative descendant IDs. Published category
        path labels cannot establish hierarchy or membership.
        """
        allowed = frozenset(category_ids)
        return cls(selection_token, knowledge_revision, library_ids,
                   tuple(value for value in identities if value.category_id in allowed), False)


class ExampleStatus(str, Enum):
    SUCCESS = "success"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"
    ERROR = "error"
    CANCELLED = "cancelled"
    STALE = "stale"


@dataclass(frozen=True)
class ExampleResult:
    request: ExampleRequest
    status: ExampleStatus
    references: tuple[MediaReference, ...] = ()
    catalog_token: str = ""


class MediaExampleProvider(Protocol):
    def query(self, request: ExampleRequest, *,
              should_continue: Callable[[], bool] | None = None) -> ExampleResult: ...


class _Unavailable(Exception):
    pass


class _Stale(Exception):
    pass


class CatalogMediaExampleProvider:
    """Adapt already-loaded native federation and cached evidence, without startup.

    No file probing, thumbnail loading, writes, or network work is authorized by
    this class. The host owns selection/revision tokens and execution scheduling.
    Query failures return only a status, never potentially private exception text.
    """
    def __init__(self, dataset, evidence_queries, *, read_snapshot, current_selection,
                 current_knowledge_revision, max_references=100_000):
        if not isinstance(max_references, int) or max_references < 1:
            raise ValueError("Choose a positive reference bound")
        self.dataset = dataset
        self.evidence_queries = evidence_queries
        self.read_snapshot = read_snapshot
        self.current_selection = current_selection
        self.current_knowledge_revision = current_knowledge_revision
        self.max_references = max_references

    def _sources(self, request):
        scope = tuple(self.dataset.scope_library_ids)
        if not request.library_ids:
            raise _Unavailable()
        if not set(request.library_ids).issubset(scope):
            raise _Stale()
        sources = {}
        for profile, manager in self.dataset.scoped_profile_managers():
            if profile.stable_id not in request.library_ids:
                continue
            if profile.stable_id in sources:
                raise _Unavailable()
            index = manager.index
            if index is None:
                raise _Unavailable()
            snapshot = self.read_snapshot(profile.stable_id, index)
            if snapshot is None:
                raise _Unavailable()
            if not isinstance(snapshot, FrozenEvidenceSnapshot):
                raise TypeError("The host reader must return a frozen cached snapshot")
            sources[profile.stable_id] = _MemoryIndex(profile.stable_id, id(index), snapshot)
        if set(sources) != set(request.library_ids):
            raise _Unavailable()
        # Include binding identity: replacing an index with a reused revision
        # cannot make an old result acceptable.
        token = (scope, tuple((key, sources[key].evidence_snapshot_token())
                            for key in request.library_ids))
        return sources, token

    def query(self, request, *, should_continue=None):
        def result(status, references=(), catalog_token=""):
            return ExampleResult(request, status, references, catalog_token)

        def checkpoint():
            if (self.current_selection() != request.selection_token
                    or self.current_knowledge_revision() != request.knowledge_revision):
                raise _Stale()
            if should_continue is not None and not should_continue():
                raise InterruptedError()
            return True

        try:
            checkpoint()
            sources, before = self._sources(request)
            references = []
            seen = set()
            # Normalize each identity independently using the injected native
            # service's exact comparison semantics. Its general-purpose _groups
            # drops empty groups; doing that across an AND request would silently
            # remove a required Tag. A wholly empty required group means no match,
            # while empty alternatives beside a valid name remain supported.
            normalized = tuple(self.evidence_queries._groups((identity.names,))
                               for identity in request.identities)
            groups = (() if request.match_all and any(not group for group in normalized)
                      else tuple(group[0] for group in normalized if group))
            if groups:
                for library_id in request.library_ids:
                    checkpoint()
                    paths = self.evidence_queries.query(
                        sources[library_id], groups, request.match_all,
                        should_continue=checkpoint,
                    )
                    for path in paths:
                        checkpoint()
                        identity = self.dataset.identity_for_path(path)
                        if identity is None or identity.library_id != library_id:
                            raise _Stale()
                        reference = MediaReference(identity.library_id, identity.relative_path)
                        if reference not in seen:
                            seen.add(reference)
                            references.append(reference)
                            if len(references) > self.max_references:
                                raise _Unavailable()
            checkpoint()
            _, after = self._sources(request)
            if before != after:
                raise _Stale()
            checkpoint()
            return result(ExampleStatus.SUCCESS if references else ExampleStatus.EMPTY,
                          tuple(references), self._token_digest(after))
        except _Stale:
            return result(ExampleStatus.STALE)
        except InterruptedError:
            return result(ExampleStatus.CANCELLED)
        except _Unavailable:
            return result(ExampleStatus.UNAVAILABLE)
        except Exception:
            return result(ExampleStatus.ERROR)

    def accepts(self, value: ExampleResult) -> bool:
        """GUI delivery must separately reject selection changes after query return.

        Checks source binding, scope and cached revision again without querying
        media. It confers no image opening or edit authority. The host must call
        this on delivery even if a worker returned before the selection changed.
        """
        try:
            if (self.current_selection() != value.request.selection_token
                    or self.current_knowledge_revision() != value.request.knowledge_revision):
                return False
            if not set(value.request.library_ids).issubset(self.dataset.scope_library_ids):
                return False
            if value.status not in (ExampleStatus.SUCCESS, ExampleStatus.EMPTY):
                return True
            _, token = self._sources(value.request)
            return (self._token_digest(token) == value.catalog_token
                    and self.current_selection() == value.request.selection_token
                    and self.current_knowledge_revision() == value.request.knowledge_revision)
        except Exception:
            return False

    @staticmethod
    def _token_digest(token):
        # Existing native source tokens contain machine-local cache paths.
        # Return a freshness digest, never those path-bearing tokens.
        return sha256(repr(token).encode("utf-8")).hexdigest()
