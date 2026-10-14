"""Pure Organizer receiver admission; no catalog/library discovery or writes.

A trusted native owner supplies an observation of its already-admitted Knowledge
and current effective aliases. An exported Encyclopedia snapshot or a sender's
revision claims cannot supply this authority. This module does not mint evidence.
"""
from dataclasses import dataclass

from .tag_collection import normalize_tag


class HandoffHold(ValueError):
    """The request cannot use the currently admitted Organizer authority."""


def _text(value, *, allow_empty=False):
    if (type(value) is not str or (not allow_empty and not value.strip()) or len(value.encode("utf-8")) > 4096
            or any(ord(char) < 32 for char in value)):
        raise HandoffHold("bounded_nonempty_text_required")
    return value


@dataclass(frozen=True)
class AdmittedTag:
    stable_id: str
    display: str
    aliases: tuple[str, ...] = ()

    def __post_init__(self):
        _text(self.stable_id)
        _text(self.display)
        if type(self.aliases) is not tuple:
            raise HandoffHold("immutable_native_aliases_required")
        for alias in self.aliases:
            _text(alias)


@dataclass(frozen=True)
class AdmittedCatalog:
    """Host observation, bound to the exact borrowed Knowledge object.

    Aliases come from current effective native authority, never the incoming
    descriptor. The caller obtains this afresh on the GUI thread without library
    scans/writes. No historical rename or catalog adoption is implied.
    """
    knowledge: object
    catalog_id: str
    revision: str
    tags: tuple[AdmittedTag, ...]

    def __post_init__(self):
        _text(self.catalog_id)
        _text(self.revision)
        if (self.knowledge is None or type(self.tags) is not tuple
                or any(type(tag) is not AdmittedTag for tag in self.tags)
                or len({tag.stable_id for tag in self.tags}) != len(self.tags)):
            raise HandoffHold("exact_admitted_catalog_observation_required")


@dataclass(frozen=True)
class MatchingImagesRequest:
    request_id: str
    catalog_id: str
    revision: str
    stable_id: str
    schema_version: int = 1
    kind: str = "display_name_search"

    def __post_init__(self):
        for value in (self.request_id, self.catalog_id, self.revision, self.stable_id):
            _text(value)
        if type(self.schema_version) is not int or self.schema_version != 1 or self.kind != "display_name_search":
            raise HandoffHold("supported_search_descriptor_required")


@dataclass(frozen=True)
class CanonicalRenameRequest:
    """Untrusted correction suggestion, not proof a historical rename occurred.

    before_revision is bounded context only; after_revision names the expected
    CURRENT native revision. Admitting the suggestion opens review, never work.
    """
    request_id: str
    catalog_id: str
    before_revision: str
    after_revision: str
    stable_id: str
    previous_display: str
    new_display: str
    schema_version: int = 1
    kind: str = "canonical_rename"

    def __post_init__(self):
        _text(self.request_id)
        _text(self.catalog_id)
        _text(self.before_revision, allow_empty=True)
        for value in (self.after_revision, self.stable_id, self.previous_display, self.new_display):
            _text(value)
        if type(self.schema_version) is not int or self.schema_version != 1 or self.kind != "canonical_rename":
            raise HandoffHold("supported_rename_descriptor_required")

    def correction_key(self):
        # Untrusted before_revision cannot authorize or evade replay admission.
        return (self.after_revision, self.stable_id, normalize_tag(self.previous_display), self.new_display)


def decode_request(value):
    """Strict bounded descriptor decoding; never execute incoming content."""
    if type(value) in (MatchingImagesRequest, CanonicalRenameRequest):
        value.__post_init__()
        return value
    if type(value) is not dict:
        raise HandoffHold("exact_descriptor_required")
    if type(value.get("kind")) is not str:
        raise HandoffHold("supported_descriptor_kind_required")
    cls = {"display_name_search": MatchingImagesRequest,
           "canonical_rename": CanonicalRenameRequest}.get(value.get("kind"))
    if cls is None or set(value) != set(cls.__dataclass_fields__):
        raise HandoffHold("supported_exact_descriptor_fields_required")
    return cls(**value)


def admit_request(request, catalog, knowledge):
    """Return original display-name search or native reviewed rename payload."""
    request = decode_request(request)
    if type(catalog) is not AdmittedCatalog:
        raise HandoffHold("host_admitted_catalog_required")
    catalog.__post_init__()
    if catalog.knowledge is not knowledge or request.catalog_id != catalog.catalog_id:
        raise HandoffHold("foreign_catalog_or_knowledge")
    tag = next((tag for tag in catalog.tags if tag.stable_id == request.stable_id), None)
    if tag is None:
        raise HandoffHold("unresolved_stable_identity")
    if type(request) is MatchingImagesRequest:
        if request.revision != catalog.revision:
            raise HandoffHold("stale_catalog_revision")
        return tag.display
    if request.after_revision != catalog.revision:
        raise HandoffHold("stale_catalog_revision")
    if tag.display != request.new_display:
        raise HandoffHold("stale_canonical_display")
    old_key = normalize_tag(request.previous_display)
    if (old_key == normalize_tag(tag.display)
            or old_key not in {normalize_tag(alias) for alias in tag.aliases}):
        raise HandoffHold("recognized_old_alias_required")
    owners = {row.stable_id for row in catalog.tags
              if old_key in {normalize_tag(name) for name in (row.display, *row.aliases)}}
    if owners != {request.stable_id}:
        raise HandoffHold("uniquely_owned_old_alias_required")
    return {
        "kind": "canonical_rename", "origin": "tag_encyclopedia",
        "stable_ids": (request.stable_id,), "source_names": (request.previous_display,),
        "label": f'Review image Tag names for "{request.new_display}"',
        "selected_kind": "tag", "selected_id": request.stable_id,
    }
