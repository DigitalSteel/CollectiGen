"""Detached read-only System Knowledge lookup. No host initialization."""
from .version import SOFTWARE_VERSION, INTERFACE_VERSION
from .tag_lookup import (
    Category, EffectiveTag, Lookup, Resolution, RelationshipEvidence,
    Snapshot, Source, TagLookup, normalize_term,
)
