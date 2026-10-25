"""Immutable lookup of host-authoritative effective values; no overlay or IO code."""
from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
from types import MappingProxyType


def normalize_term(value: str) -> str:
    """Exactly CollectiGen normalize_tag; IDs themselves are never normalized."""
    return " ".join(value.strip().lower().replace("_", " ").split())


def _id(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("IDs must be nonblank strings")


def _freeze_collection(value, name):
    """Require explicit built-in list/tuple shape before losing raw admission.

    Content/identity semantics and wire capacity limits retain their existing
    owners. Do not consume scalars, mappings, sets or lazy/custom iterables.
    """
    if type(value) not in (list, tuple):
        raise ValueError(name + " requires an explicit list or tuple")
    return tuple(value)


@dataclass(frozen=True)
class Source:
    reference: str
    revision: str
    authority_fingerprint: str
    scope: tuple[str, ...]
    completeness: str = "complete"
    kind: str = "effective_native"

    def __post_init__(self):
        for value in (self.reference, self.revision, self.authority_fingerprint):
            _id(value)
        object.__setattr__(self, "scope", _freeze_collection(self.scope, "Source.scope"))
        if len(set(self.scope)) != len(self.scope):
            raise ValueError("Duplicate scope IDs")
        for value in self.scope:
            _id(value)
        if self.completeness not in {"complete", "partial"}:
            raise ValueError("Unknown completeness")
        if self.kind not in {"effective_native", "reviewed_portable"}:
            raise ValueError("Unsupported snapshot source")


@dataclass(frozen=True)
class Category:
    id: str
    label: str
    path: str


@dataclass(frozen=True)
class EffectiveTag:
    id: str
    display: str
    category_id: str
    aliases: tuple[str, ...] = ()
    minimum_rating: str = ""
    classification_status: str = "unclassified"

    def __post_init__(self):
        object.__setattr__(self, "aliases", _freeze_collection(self.aliases, "EffectiveTag.aliases"))


@dataclass(frozen=True)
class RelationshipEvidence:
    id: str
    source: str
    version: int
    layer: str
    reviewed: bool
    disabled: bool


@dataclass(frozen=True)
class Lookup:
    key: str
    status: str
    tag_id: str = ""
    owner_ids: tuple[str, ...] = ()
    relationship_ids: tuple[str, ...] = ()
    retired_ids: tuple[str, ...] = ()
    unavailable_ids: tuple[str, ...] = ()

    def __post_init__(self):
        for name in ("owner_ids", "relationship_ids", "retired_ids", "unavailable_ids"):
            object.__setattr__(self, name, _freeze_collection(getattr(self, name), "Lookup." + name))


@dataclass(frozen=True)
class Snapshot:
    source: Source
    tags: tuple[EffectiveTag, ...]
    categories: tuple[Category, ...]
    lookups: tuple[Lookup, ...]
    redirects: tuple[tuple[str, str], ...] = ()
    removed_ids: tuple[str, ...] = ()
    relationships: tuple[RelationshipEvidence, ...] = ()

    def __post_init__(self):
        for name in ("tags", "categories", "lookups", "removed_ids", "relationships"):
            object.__setattr__(self, name, _freeze_collection(getattr(self, name), "Snapshot." + name))
        pairs = _freeze_collection(self.redirects, "Snapshot.redirects")
        detached = []
        for pair in pairs:
            pair = _freeze_collection(pair, "Snapshot.redirects pair")
            if len(pair) != 2:
                raise ValueError("Snapshot.redirects pair requires two endpoints")
            detached.append(pair)
        object.__setattr__(self, "redirects", tuple(detached))

    @property
    def fingerprint(self) -> str:
        value = asdict(self)
        for row in value["lookups"]:
            for key in ("retired_ids", "unavailable_ids"):
                if not row[key]:
                    del row[key]
        return sha256(json.dumps(value, sort_keys=True,
                                 ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Resolution:
    value: str
    status: str
    tag: EffectiveTag | None = None
    owner_ids: tuple[str, ...] = ()
    relationship_ids: tuple[str, ...] = ()
    retired_ids: tuple[str, ...] = ()
    unavailable_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class TagLookup:
    snapshot: Snapshot
    _tags: object = field(init=False, repr=False, compare=False)
    _categories: object = field(init=False, repr=False, compare=False)
    _lookups: object = field(init=False, repr=False, compare=False)
    _redirects: object = field(init=False, repr=False, compare=False)
    _removed: frozenset = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        def index(rows, key):
            result = {}
            for row in rows:
                identity = key(row)
                _id(identity)
                if identity in result:
                    raise ValueError("Duplicate snapshot identity: " + identity)
                result[identity] = row
            return MappingProxyType(result)
        tags = index(self.snapshot.tags, lambda row: row.id)
        categories = index(self.snapshot.categories, lambda row: row.id)
        lookups = index(self.snapshot.lookups, lambda row: row.key)
        evidence = index(self.snapshot.relationships, lambda row: row.id)
        redirects = {}
        for donor, target in self.snapshot.redirects:
            _id(donor); _id(target)
            if donor in redirects or donor in tags:
                raise ValueError("Conflicting redirect ID")
            redirects[donor] = target
        for identity in self.snapshot.removed_ids:
            _id(identity)
            if identity in tags:
                raise ValueError("Removed ID is active")
        removed=frozenset(self.snapshot.removed_ids)
        dispositions={}
        def disposition(identity):
            # Memoize each traversed node once. Shared tails and cycles cannot
            # multiply diagnostic admission work by the number of lookup rows.
            current,path,seen=identity,[],set()
            while current not in dispositions:
                if current in tags:
                    result="active"
                    break
                if current in seen:
                    result="unavailable"
                    break
                seen.add(current)
                path.append(current)
                if current in redirects:
                    current=redirects[current]
                else:
                    result="retired" if current in removed else "unavailable"
                    break
            else:
                result=dispositions[current]
            for node in path:
                dispositions[node]=result
            return result
        allowed = {"canonical_name", "canonical_alias", "compatibility_alias", "ambiguous", "disabled", "retired", "unavailable"}
        for key, row in lookups.items():
            if key != normalize_term(key) or row.status not in allowed:
                raise ValueError("Invalid lookup disposition")
            if row.status in {"ambiguous", "disabled", "retired", "unavailable"}:
                if row.tag_id:
                    raise ValueError("Blocked lookup has a target")
            elif row.tag_id not in tags or row.owner_ids != (row.tag_id,):
                raise ValueError("Resolved lookup requires one existing owner")
            for identity in (*row.retired_ids, *row.unavailable_ids):
                _id(identity)
            if len(set(row.retired_ids)) != len(row.retired_ids) or len(set(row.unavailable_ids)) != len(row.unavailable_ids):
                raise ValueError("Duplicate diagnostic identity")
            if any(identity not in removed or identity in tags or identity in redirects
                   for identity in row.retired_ids):
                raise ValueError("Retired alias requires verified tombstones")
            if any(disposition(identity)!="unavailable"
                   for identity in row.unavailable_ids):
                raise ValueError("Unavailable alias has an available or retired identity")
            diagnostics = bool(row.retired_ids or row.unavailable_ids)
            if diagnostics and (row.tag_id or not row.relationship_ids or row.status not in {"ambiguous","retired","unavailable"}):
                raise ValueError("Diagnostic alias must be blocked with retained evidence")
            if row.status=="retired" and (not row.retired_ids or row.unavailable_ids or row.owner_ids):
                raise ValueError("Retired alias requires sole retired disposition")
            if row.status=="unavailable" and (not row.unavailable_ids or row.owner_ids):
                raise ValueError("Unavailable alias requires unresolved evidence")
            if any(owner not in tags for owner in row.owner_ids):
                raise ValueError("Unknown lookup owner")
            if any(identity not in evidence for identity in row.relationship_ids):
                raise ValueError("Missing relationship evidence")
        for tag in tags.values():
            if not normalize_term(tag.display):
                raise ValueError("Blank preferred display")
        for name, value in (("_tags", tags), ("_categories", categories), ("_lookups", lookups),
                            ("_redirects", MappingProxyType(redirects)),
                            ("_removed", removed)):
            object.__setattr__(self, name, value)

    def _present(self, value, identity, status, owners=(), relationships=(), category_ids=None):
        tag = self._tags[identity]
        if tag.category_id not in self._categories:
            return Resolution(value, "unavailable_category", owner_ids=owners, relationship_ids=relationships)
        scope = frozenset(self.snapshot.source.scope)
        requested = scope if category_ids is None else scope.intersection(category_ids)
        if tag.category_id not in requested:
            return Resolution(value, "out_of_scope", owner_ids=owners, relationship_ids=relationships)
        return Resolution(value, status, tag, owners, relationships)

    def get(self, identity: str, *, category_ids=None) -> Resolution:
        """Exact stable ID, reviewed redirect chain, retired or unavailable result."""
        current, seen = identity, set()
        while current not in self._tags and current in self._redirects:
            if current in seen:
                return Resolution(identity, "unavailable")
            seen.add(current)
            current = self._redirects[current]
        if current in self._tags:
            return self._present(identity, current, "redirected" if seen else "stable_id",
                                 (current,), category_ids=category_ids)
        if seen:
            return Resolution(identity, "retired" if current in self._removed else "unavailable")
        return Resolution(identity, "retired" if identity in self._removed else
                          "missing" if self.snapshot.source.completeness == "complete" else "unavailable")

    def resolve(self, value: str, *, category_ids=None) -> Resolution:
        """IDs take precedence. Exact normalized lookup never performs fuzzy merging."""
        if value in self._tags or value in self._redirects or value in self._removed:
            return self.get(value, category_ids=category_ids)
        row = self._lookups.get(normalize_term(value))
        if row is None:
            return Resolution(value, "missing" if self.snapshot.source.completeness == "complete" else "unavailable")
        if not row.tag_id:
            return Resolution(value, row.status, owner_ids=row.owner_ids,
                              relationship_ids=row.relationship_ids,
                              retired_ids=row.retired_ids, unavailable_ids=row.unavailable_ids)
        return self._present(value, row.tag_id, row.status, row.owner_ids, row.relationship_ids, category_ids)

    def suggest(self, prefix: str, *, category_ids, limit: int = 20) -> tuple[EffectiveTag, ...]:
        """Bounded deterministic prefix suggestions; caller must supply allowed categories."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Suggestion limit must be 1..100")
        selected = {}
        category_ids = frozenset(category_ids)
        key = normalize_term(prefix)
        for term, row in self._lookups.items():
            if term.startswith(key) and row.tag_id:
                match = self._present(prefix, row.tag_id, row.status, category_ids=category_ids)
                if match.tag is not None:
                    selected[match.tag.id] = match.tag
        return tuple(sorted(selected.values(), key=lambda tag: (normalize_term(tag.display), tag.id))[:limit])
