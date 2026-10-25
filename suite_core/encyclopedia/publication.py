"""Strict versioned public DTO codec and guarded atomic single-writer publication.

The caller owns the native mutation/publication guard. Content hashes detect
corruption, not publisher authenticity. No native files or directories are opened.
"""
from contextlib import AbstractContextManager
from dataclasses import dataclass, fields, is_dataclass
from hashlib import sha256
import json
import math
import os
import re
from pathlib import Path
import tempfile

from suite_core.knowledge import (
    Category, EffectiveTag, Lookup, RelationshipEvidence, Snapshot, Source, TagLookup,
)
from .version import (SNAPSHOT_INTERFACE_VERSION, LEGACY_SNAPSHOT_INTERFACE_VERSION,
                      DIAGNOSTIC_SNAPSHOT_INTERFACE_VERSION)

MAX_BYTES = 32 * 1024 * 1024
MAX_ROWS = 100_000
MAX_TOTAL_ROWS = 300_000
MAX_ASSOCIATIONS = 1_000_000
MAX_STRING_LENGTH = 262_144
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 8_000_000
MAX_JSON_ENTRIES = 4_000_000
MAX_INTEGER = 10 ** 256 - 1
_STRING_SPECIAL = re.compile(rb'["\\]')
_TOKEN = re.compile(rb'[^\s{}\[\],:"]+')


def _admit_json(raw, *, depth_limit, node_limit, collection_limit, string_limit,
                entry_limit):
    """Bound lexical work before standard JSON parsing; not a syntax parser.

    Standard json.loads remains responsible for syntax and duplicate keys.
    Every key/value/container counts; private and unknown fields count too.
    """
    stack = []
    nodes = entries = index = 0
    while index < len(raw):
        character = raw[index]
        if character in b' \t\r\n':
            index += 1
            continue
        if character in (93, 125):
            if not stack:
                raise PublicationError("Invalid JSON nesting")
            stack.pop()
            index += 1
            continue
        if character == 44:
            if stack and stack[-1][0] == 91:
                stack[-1][2] = True
            index += 1
            continue
        if character == 58:
            if stack and stack[-1][0] == 123:
                stack[-1][1] += 1
                entries += 1
            index += 1
        else:
            nodes += 1
            if stack and stack[-1][0] == 91 and stack[-1][2]:
                stack[-1][1] += 1
                stack[-1][2] = False
                entries += 1
            if stack and stack[-1][1] > collection_limit:
                raise PublicationError("JSON collection exceeds accepted bound")
            if character in (91, 123):
                stack.append([character, 0, True])
                if len(stack) > depth_limit:
                    raise PublicationError("JSON nesting exceeds accepted bound")
                index += 1
            elif character == 34:
                start = index
                index += 1
                while True:
                    match = _STRING_SPECIAL.search(raw, index)
                    if match is None or match.start() - start > 6 * string_limit + 1:
                        raise PublicationError("JSON string exceeds accepted bound")
                    index = match.start()
                    if raw[index] == 34:
                        index += 1
                        break
                    index += 2
                if len(json.loads(raw[start:index])) > string_limit:
                    raise PublicationError("JSON string exceeds accepted bound")
            else:
                match = _TOKEN.match(raw, index)
                if match is None or match.end() - index > 256:
                    raise PublicationError("Invalid or oversized JSON scalar")
                index = match.end()
        if nodes > node_limit or entries > entry_limit:
            raise PublicationError("JSON structure exceeds accepted bound")
        if stack and stack[-1][1] > collection_limit:
            raise PublicationError("JSON collection exceeds accepted bound")
    if stack:
        raise PublicationError("Invalid JSON nesting")
    return nodes, entries


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise PublicationError("Nonfinite JSON number")
    return result


def _reject_constant(value):
    raise PublicationError("Nonfinite JSON number")


class PublicationError(ValueError):
    pass


class StalePublication(PublicationError):
    pass


@dataclass(frozen=True)
class Publication:
    catalog_id: str
    generation: int
    snapshot: Snapshot

    def __post_init__(self):
        if not isinstance(self.catalog_id, str) or not self.catalog_id.strip():
            raise PublicationError("Catalog identity is required")
        _string(self.catalog_id)
        if type(self.generation) is not int or not 1 <= self.generation <= MAX_INTEGER:
            raise PublicationError("Generation must be a positive integer")
        _validate_snapshot(self.snapshot)
        TagLookup(self.snapshot)
        category_ids = {row.id for row in self.snapshot.categories}
        if any(identity not in category_ids for identity in self.snapshot.source.scope):
            raise PublicationError("Scope requires published Categories")
        if any(row.category_id not in category_ids for row in self.snapshot.tags):
            raise PublicationError("Tags require published Categories")

    @property
    def token(self):
        return sha256(encode(self)).hexdigest()


def _dataclass_json(value):
    if not is_dataclass(value):
        raise TypeError("Unsupported DTO value")
    return {field.name: getattr(value, field.name) for field in fields(value)
            if not (type(value) is Lookup and field.name in {"retired_ids","unavailable_ids"}
                    and not getattr(value,field.name))}


def _chunks(value):
    encoder = json.JSONEncoder(sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False, default=_dataclass_json)
    total = 0
    for chunk in encoder.iterencode(value):
        try:
            raw = chunk.encode("utf-8")
        except UnicodeEncodeError as error:
            raise PublicationError("Invalid DTO Unicode") from error
        total += len(raw)
        if total > MAX_BYTES:
            raise PublicationError("Publication exceeds size limit")
        yield raw


def _json(value):
    result = bytearray()
    for chunk in _chunks(value):
        result.extend(chunk)
    return bytes(result)


def _hash(value):
    digest = sha256()
    for chunk in _chunks(value):
        digest.update(chunk)
    return digest.hexdigest()


def _validate_snapshot(snapshot):
    if type(snapshot) is not Snapshot or type(snapshot.source) is not Source:
        raise PublicationError("Invalid DTO type")
    associations = total = 0
    def validate_row(cls, row):
        nonlocal associations
        if type(row) is not cls:
            raise PublicationError("Invalid DTO row type")
        for field in fields(cls):
            key, value = field.name, getattr(row, field.name)
            if key in {"scope", "aliases", "owner_ids", "relationship_ids", "retired_ids", "unavailable_ids"}:
                if not isinstance(value, (tuple, list)) or len(value) > MAX_ROWS:
                    raise PublicationError("Invalid DTO list")
                associations += len(value)
                for item in value:
                    _string(item)
            elif key in {"reviewed", "disabled"}:
                if type(value) is not bool:
                    raise PublicationError("Invalid DTO boolean")
            elif key == "version":
                if type(value) is not int or not 0 <= value <= MAX_INTEGER:
                    raise PublicationError("Invalid evidence version")
            else:
                _string(value)
    validate_row(Source, snapshot.source)
    for key, cls in (("tags", EffectiveTag), ("categories", Category),
                     ("lookups", Lookup), ("relationships", RelationshipEvidence)):
        rows = getattr(snapshot, key)
        if not isinstance(rows, (tuple, list)) or len(rows) > MAX_ROWS:
            raise PublicationError("Invalid DTO rows")
        total += len(rows)
        for row in rows:
            validate_row(cls, row)
    for name in ("redirects", "removed_ids"):
        rows = getattr(snapshot, name)
        if len(rows) > MAX_ROWS:
            raise PublicationError("Invalid DTO rows")
        total += len(rows)
    for pair in snapshot.redirects:
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            raise PublicationError("Invalid redirect pair")
        associations += 2
        for item in pair:
            _string(item)
    for item in snapshot.removed_ids:
        _string(item)
    if total > MAX_TOTAL_ROWS or associations > MAX_ASSOCIATIONS:
        raise PublicationError("Aggregate DTO work exceeds accepted bound")


def encode(publication):
    _string(publication.catalog_id)
    _validate_snapshot(publication.snapshot)
    payload = dict(catalog_id=publication.catalog_id, generation=publication.generation,
                   snapshot=publication.snapshot)
    interface = DIAGNOSTIC_SNAPSHOT_INTERFACE_VERSION if any(row.retired_ids or row.unavailable_ids or row.status in {"retired","unavailable"}
                         for row in publication.snapshot.lookups) else LEGACY_SNAPSHOT_INTERFACE_VERSION
    return _json(dict(interface_version=interface, payload=payload,
                      payload_sha256=_hash(payload)))


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PublicationError("Duplicate JSON field")
        result[key] = value
    return result


def _object(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise PublicationError("Unsupported or incomplete DTO fields")


def _string(value):
    if not isinstance(value, str) or len(value) > MAX_STRING_LENGTH:
        raise PublicationError("DTO text must be a string")
    return value


def _strings(value):
    if not isinstance(value, list) or len(value) > MAX_ROWS:
        raise PublicationError("Invalid DTO list")
    return tuple(_string(item) for item in value)


def _row(cls, value, interface=LEGACY_SNAPSHOT_INTERFACE_VERSION):
    fields = cls.__dataclass_fields__
    if cls is Lookup:
        base = set(fields) - {"retired_ids","unavailable_ids"}
        if not isinstance(value,dict) or not base <= set(value) or not set(value) <= set(fields):
            raise PublicationError("Unsupported or incomplete lookup fields")
        if interface==LEGACY_SNAPSHOT_INTERFACE_VERSION and (set(value)!=base or value["status"] in {"retired","unavailable"}):
            raise PublicationError("Retirement diagnostics require snapshot interface 2")
    else:
        _object(value, fields)
    for key, item in value.items():
        if key in {"scope", "aliases", "owner_ids", "relationship_ids", "retired_ids", "unavailable_ids"}:
            _strings(item)
        elif key in {"reviewed", "disabled"}:
            if type(item) is not bool:
                raise PublicationError("Invalid DTO boolean")
        elif key == "version":
            if type(item) is not int or not 0 <= item <= MAX_INTEGER:
                raise PublicationError("Invalid evidence version")
        else:
            _string(item)
    return cls(**value)


def decode(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise PublicationError("Invalid publication size")
    try:
        _admit_json(raw, depth_limit=MAX_JSON_DEPTH, node_limit=MAX_JSON_NODES,
                    collection_limit=MAX_ROWS, string_limit=MAX_STRING_LENGTH,
                    entry_limit=MAX_JSON_ENTRIES)
        envelope = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_reject_constant, parse_float=_finite_float)
        _object(envelope, ("interface_version", "payload", "payload_sha256"))
        if (type(envelope["interface_version"]) is not int or envelope["interface_version"] not in {LEGACY_SNAPSHOT_INTERFACE_VERSION,DIAGNOSTIC_SNAPSHOT_INTERFACE_VERSION}
                or envelope["interface_version"]>SNAPSHOT_INTERFACE_VERSION):
            raise PublicationError("Unsupported snapshot interface")
        payload = envelope["payload"]
        _object(payload, ("catalog_id", "generation", "snapshot"))
        if _hash(payload) != envelope["payload_sha256"]:
            raise PublicationError("Publication checksum mismatch")
        value = payload["snapshot"]
        _object(value, Snapshot.__dataclass_fields__)
        rows = {}
        for key, cls in (("tags", EffectiveTag), ("categories", Category),
                         ("lookups", Lookup), ("relationships", RelationshipEvidence)):
            if not isinstance(value[key], list) or len(value[key]) > MAX_ROWS:
                raise PublicationError("Invalid DTO rows")
            rows[key] = tuple(_row(cls, item, envelope["interface_version"]) for item in value[key])
        removed = _strings(value["removed_ids"])
        redirects = value["redirects"]
        if not isinstance(redirects, list) or len(redirects) > MAX_ROWS:
            raise PublicationError("Invalid redirects")
        for pair in redirects:
            if len(_strings(pair)) != 2:
                raise PublicationError("Invalid redirect pair")
        snapshot = Snapshot(_row(Source, value["source"]), **rows,
                            redirects=tuple(tuple(pair) for pair in redirects), removed_ids=removed)
        return Publication(payload["catalog_id"], payload["generation"], snapshot)
    except PublicationError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
        raise PublicationError("Invalid snapshot publication") from error


def read(path):
    with Path(path).open("rb") as stream:
        return decode(stream.read(MAX_BYTES + 1))


def require_successor(previous, candidate):
    if previous.catalog_id != candidate.catalog_id:
        raise StalePublication("Catalog identity changed; explicitly open the new catalog")
    if candidate.generation < previous.generation:
        raise StalePublication("Older snapshot generation")
    if candidate.generation == previous.generation and candidate != previous:
        raise StalePublication("Generation reused for different content")
    if candidate.generation > previous.generation and candidate.snapshot.source.revision == previous.snapshot.source.revision:
        raise StalePublication("New generation requires a new authority revision")


def publish(publication, path, *, expected_token, revision, guard: AbstractContextManager):
    """Replace one approved publication under the host's exclusive guard.

    expected_token=None permits first publication only. Otherwise bind the exact
    previous receipt. Guard must cover every native mutation and this publisher;
    it is not a cross-process locking implementation. Parent directory must exist.
    """
    destination = Path(path)
    raw = encode(publication)
    decode(raw)
    with guard:
        if revision() != publication.snapshot.source.revision:
            raise StalePublication("Native revision changed before publication")
        previous = read(destination) if destination.exists() else None
        if (None if previous is None else previous.token) != expected_token:
            raise StalePublication("Publication changed since approval")
        if previous is not None:
            require_successor(previous, publication)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".encyclopedia-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if temporary.read_bytes() != raw:
                raise PublicationError("Staged publication failed read-back")
            if revision() != publication.snapshot.source.revision:
                raise StalePublication("Native revision changed during publication")
            os.replace(temporary, destination)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return publication.token

