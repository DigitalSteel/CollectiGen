"""Shared Pack container checks, factored from CollectiGen's native owner.

Native extraction retains caller limits and accepted output bytes. NUL names,
nonportable aliases, duplicate destinations and excessive recognized-JSON depth
are refused. Syntax parsing remains separate from native IO.
"""
from pathlib import Path
import os
import re

MAX_PACK_FILES = 64
MAX_PACK_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_PACK_RELATIONSHIPS = 40_000
MAX_PACK_RECORDS = 20_000
SUPPORTED_PACK_SCHEMA_VERSION = 9
FOUNDATION_PACK_SCHEMA_VERSION = 10
FOUNDATION_PACK_SCOPE = "default_foundation"
MAX_FOUNDATION_MANIFEST_BYTES = 128 * 1024
MAX_PACK_JSON_DEPTH = 64

# Only members read as JSON by the existing native/preview Pack contracts.
PACK_JSON_PATHS = frozenset({
    "manifest.json",
    *("data/user/" + name for name in (
        "tags.json", "categories.json", "characters.json", "series.json",
        "outfits.json", "patterns.json", "consumer_policies.json", "relationships.json")),
    *("data/" + name for name in (
        "tags.json", "characters.json", "series.json", "outfits.json")),
    *("data/defaults/" + name for name in (
        "foundation_tags.json", "community_tags.json", "promoted_tags.json",
        "categories.json", "characters.json", "series.json", "outfits.json",
        "relationships.json", "tag_aliases.json", "concepts.json", "modifiers.json",
        "patterns.json", "consumer_policies.json")),
})
_JSON_STRUCTURE = re.compile(rb'["{}\[\]]')
_JSON_STRING_END = re.compile(rb'["\\]')


class _JsonDepthGuard:
    """Bound open JSON containers without parsing values or recursing.

    Syntax, Unicode, duplicate keys and numbers remain the existing parser's
    responsibility. Quoted braces/brackets and escaped quotes are not nesting.
    State survives streaming boundaries, including an escaped next byte.
    """

    def __init__(self, maximum):
        self.maximum = maximum
        self.depth = self.max_seen = 0
        self.in_string = self.escape_next = False

    def feed(self, raw):
        if not raw:
            return
        index = 1 if self.escape_next else 0
        self.escape_next = False
        while index < len(raw):
            match = (_JSON_STRING_END if self.in_string else _JSON_STRUCTURE).search(raw, index)
            if match is None:
                break
            token = raw[match.start()]
            index = match.end()
            if self.in_string:
                if token == 92:
                    if index == len(raw):
                        self.escape_next = True
                    else:
                        index += 1
                else:
                    self.in_string = False
            elif token == 34:
                self.in_string = True
            elif token in (123, 91):
                self.depth += 1
                self.max_seen = max(self.max_seen, self.depth)
                if self.depth > self.maximum:
                    raise ValueError(f"Knowledge Pack JSON nesting exceeds {self.maximum} containers.")
            else:
                # Malformed closing syntax is still rejected by json.loads.
                self.depth = max(0, self.depth - 1)


def validate_json_depth(raw):
    """Apply the fixed Pack ingress budget before the existing JSON decoder."""
    guard = _JsonDepthGuard(MAX_PACK_JSON_DEPTH)
    guard.feed(raw)


def checked_archive_entries(archive, *, max_files=MAX_PACK_FILES,
                            max_uncompressed_bytes=MAX_PACK_UNCOMPRESSED_BYTES, max_member_bytes=None):
    """Check the shared reader budget before opening any member payload."""
    entries = archive.infolist()
    if len(entries) > max_files:
        raise ValueError(f"Knowledge Pack contains too many files ({len(entries)}; maximum {max_files}).")
    total = sum(max(0, int(item.file_size)) for item in entries)
    if total > max_uncompressed_bytes:
        raise ValueError("Knowledge Pack is too large after extraction.")
    if max_member_bytes is not None and any(item.file_size > max_member_bytes for item in entries):
        raise ValueError("Knowledge Pack participant exceeds its byte bound.")
    return entries


def extract_pack_safely(archive, target, *, max_files=MAX_PACK_FILES,
                        max_uncompressed_bytes=MAX_PACK_UNCOMPRESSED_BYTES, max_member_bytes=None):
    """The native extraction algorithm; destination is a caller-owned temp root."""
    entries = checked_archive_entries(archive, max_files=max_files,
                                      max_uncompressed_bytes=max_uncompressed_bytes, max_member_bytes=max_member_bytes)
    root = target.resolve()
    seen_destinations = set()
    total_streamed = 0
    for item in entries:
        destination = checked_destination(item, target, root)
        canonical = destination.relative_to(root).as_posix()
        if os.name == "nt":
            canonical = canonical.casefold()
        if canonical in seen_destinations:
            raise ValueError(f"Knowledge Pack contains a duplicate destination: {item.filename}")
        seen_destinations.add(canonical)
        if item.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(item, "r") as source, destination.open("wb") as output:
            guard = _JsonDepthGuard(MAX_PACK_JSON_DEPTH) if canonical in PACK_JSON_PATHS else None
            streamed = 0
            while chunk := source.read(65536):
                streamed += len(chunk)
                total_streamed += len(chunk)
                if total_streamed > max_uncompressed_bytes or (max_member_bytes is not None and streamed > max_member_bytes):
                    raise ValueError("Knowledge Pack stream exceeds its byte bound.")
                if guard is not None:
                    guard.feed(chunk)
                output.write(chunk)


def checked_destination(item, target, root):
    """Native path checks plus deliberate original-name NUL rejection."""
    original = item.orig_filename
    if "\x00" in original:
        raise ValueError("Knowledge Pack contains a NUL in its original entry name.")
    name = item.filename.replace("\\", "/")
    relative = Path(name)
    unix_mode = (item.external_attr >> 16) & 0o170000
    if not name or relative.is_absolute() or ".." in relative.parts or unix_mode == 0o120000:
        raise ValueError(f"Knowledge Pack contains an unsafe path: {item.filename}")
    # Win32 may trim suffix dots/spaces or treat a colon as an alternate stream.
    # Refuse those nonportable spellings before resolution/open instead of
    # silently mapping their bytes onto another archive member's destination.
    if any(part not in {"", ".", ".."} and
           (part.endswith((".", " ")) or ":" in part)
           for part in name.split("/")):
        raise ValueError(f"Knowledge Pack contains a nonportable path alias: {item.filename}")
    destination = (target / relative).resolve()
    if destination != root and root not in destination.parents:
        raise ValueError(f"Knowledge Pack contains an unsafe path: {item.filename}")
    return destination


def validate_manifest(manifest, *, supported_schema=SUPPORTED_PACK_SCHEMA_VERSION, allow_foundation=False):
    """Original native manifest object/schema validation, including coercion."""
    if not isinstance(manifest, dict):
        raise ValueError("The Knowledge Pack manifest must contain a JSON object.")
    if manifest.get("format") != "tagmanager_knowledge_pack":
        raise ValueError("Unsupported knowledge pack format.")
    schema_version = int(manifest.get("schema_version", 1) or 0)
    foundation = manifest.get("scope") == FOUNDATION_PACK_SCOPE or "foundation" in manifest
    if foundation:
        if (not allow_foundation or type(manifest.get("schema_version")) is not int
                or schema_version != FOUNDATION_PACK_SCHEMA_VERSION or manifest.get("scope") != FOUNDATION_PACK_SCOPE):
            raise ValueError("Foundation mode requires the explicit schema-10 native Foundation loader.")
        return manifest
    if schema_version < 1 or schema_version > supported_schema:
        raise ValueError(
            f"Knowledge Pack schema {schema_version} is not supported by this version of CollectiGen."
        )
    return manifest


def is_record_list(values):
    """Native patch-list predicate; no filtering of malformed items."""
    return isinstance(values, list) and all(isinstance(item, dict) for item in values)
