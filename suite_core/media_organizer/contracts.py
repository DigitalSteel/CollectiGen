"""Borrowed Media Organizer contracts; no runtime loading or data discovery.

Native hosts retain admission, persistence, cancellation and recovery authority.
Import this module to exchange contracts without loading entry/composition code.
"""
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Protocol


class BoundaryError(ValueError):
    """The supplied application host does not retain the canonical authority."""


class Workspace(Enum):
    LIBRARY = "library"
    INCOMING = "incoming"
    SETTINGS = "settings"


# Canonical PageNavigationList takes page indices, not presentation row indices.
PAGE_INDEX = MappingProxyType({
    Workspace.LIBRARY: 2, Workspace.INCOMING: 1, Workspace.SETTINGS: 6,
})
SERVICE_NAMES = (
    "dataset", "knowledge", "events", "jobs", "moves", "edits", "recycle",
    "duplicates", "history_configuration", "preparation_requests",
    "ai", "ai_runtimes", "ai_work_lease", "derived_refresh", "setup_queue",
)


class Shell(Protocol):
    settings: object
    state: object
    startup_cancelled: bool
    navigation: object

    def choose_library(self): ...
    def open_completed_dataset_image(self, path: str): ...
    def edit_image_data(self, path: str): ...
    def close(self): ...


@dataclass(frozen=True)
class ExistingContext:
    """Borrowed settings and fully admitted native AppState; never a new store."""
    settings: object
    state: object

    def validate(self):
        if self.settings is None or self.state is None:
            raise BoundaryError("Explicit admitted settings and state are required.")
        if getattr(self.state, "settings", None) is not self.settings:
            raise BoundaryError("State must own the supplied settings instance.")
        missing = [name for name in SERVICE_NAMES
                   if getattr(self.state, name, None) is None]
        if missing:
            raise BoundaryError("Required native services missing: " + ", ".join(missing))


class Availability(Enum):
    UNKNOWN = "unknown"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class OptionalBackendStatus:
    availability: Availability
    explanation: str

    def __post_init__(self):
        if not isinstance(self.availability, Availability) or not self.explanation.strip():
            raise BoundaryError("Backend status requires typed availability and an explanation.")


class OptionalBackendUnavailable(RuntimeError):
    """Provider adapter reports expected unavailability without blocking local work."""


class TagLookupService(Protocol):
    """Existing suite_core.knowledge.TagLookup interface 1, borrowed by session."""
    def get(self, identity: str, *, category_ids=None): ...


@dataclass(frozen=True)
class EncyclopediaSession:
    """No store: lookup borrowed from the existing approved Knowledge service.

    Example images require a separately approved, exact-scope native Dataset
    service/session adapter. This candidate does not enumerate or snapshot images.
    """
    lookup: TagLookupService

    def lookup_tag(self, identity: str, *, category_ids=None):
        return self.lookup.get(identity, category_ids=category_ids)
