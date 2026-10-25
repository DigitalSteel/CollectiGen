"""Inert application declarations; configuration and support never grant authority.

Version 1 is an additive contract, not a change to installation/module manifests.
No registry, persistence, transport, settings, endpoint or native imports/actions.
"""
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json
import re

from release_contract import SemVer
from suite_core.backend_contracts import CapabilityId, StableId
from suite_core.installation import ComponentId, InstallationId

APP_CAPABILITY_SCHEMA_VERSION = 1
TAG_SEARCH_CAPABILITY = CapabilityId("collectigen.organizer.tag-search.open")


class CapabilityHold(ValueError):
    pass


def require(value, reason):
    if not value:
        raise CapabilityHold(reason)


def exact_id(value, kind=StableId):
    require(type(value) is kind, "exact_identity_required")
    value.__post_init__()


def digest_text(value):
    require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value), "exact_digest_required")


def schema_versions(values):
    require(type(values) is tuple and 1 <= len(values) <= 16
            and all(type(v) is int and 1 <= v <= 65535 for v in values)
            and tuple(sorted(set(values))) == values, "bounded_ordered_schema_versions_required")


class TransportKind(str, Enum):
    HOSTED = "hosted"
    LOCAL = "local"
    EXPLICIT_REMOTE = "explicit_remote"


class CapabilityEffect(str, Enum):
    OPEN_SEARCH = "open_search"
    OPEN_REVIEW = "open_review"
    SETTINGS_READ = "settings_read"
    SETTINGS_REVIEW = "settings_review"
    SETTINGS_WRITE = "settings_write"


@dataclass(frozen=True, slots=True)
class AppBinding:
    component_id: ComponentId
    installation_id: InstallationId
    binding_revision: StableId

    def __post_init__(self):
        exact_id(self.component_id, ComponentId)
        exact_id(self.installation_id, InstallationId)
        exact_id(self.binding_revision)

    def mapping(self):
        return {key: getattr(self, key).value for key in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class CapabilityContract:
    capability_id: CapabilityId
    request_versions: tuple[int, ...]
    response_versions: tuple[int, ...]
    effect: CapabilityEffect
    transports: frozenset[TransportKind]
    required_context: frozenset[str] = frozenset()

    def __post_init__(self):
        exact_id(self.capability_id, CapabilityId)
        schema_versions(self.request_versions)
        schema_versions(self.response_versions)
        require(type(self.effect) is CapabilityEffect, "capability_effect_required")
        require(type(self.transports) is frozenset and 1 <= len(self.transports) <= 3
                and all(type(v) is TransportKind for v in self.transports), "capability_transport_required")
        require(type(self.required_context) is frozenset and all(type(v) is str for v in self.required_context)
                and self.required_context <= {"profile", "library", "privacy", "scope_revision"},
                "supported_context_requirements_required")
        if self.capability_id == TAG_SEARCH_CAPABILITY:
            require(self.effect is CapabilityEffect.OPEN_SEARCH
                    and self.required_context == frozenset({"profile", "library", "privacy", "scope_revision"}),
                    "search_effect_and_exact_recipient_context_required")

    def mapping(self):
        return {"capability_id": self.capability_id.value,
                "request_versions": list(self.request_versions), "response_versions": list(self.response_versions),
                "effect": self.effect.value, "transports": sorted(v.value for v in self.transports),
                "required_context": sorted(self.required_context)}


@dataclass(frozen=True, slots=True)
class AppCapabilityDeclaration:
    binding: AppBinding
    software_version: str
    declaration_revision: StableId
    source_ref: StableId
    capabilities: tuple[CapabilityContract, ...]
    schema_version: int = APP_CAPABILITY_SCHEMA_VERSION

    def __post_init__(self):
        require(type(self.binding) is AppBinding, "app_binding_required")
        self.binding.__post_init__()
        require(type(self.software_version) is str and 1 <= len(self.software_version) <= 96
                and self.software_version.strip() == self.software_version, "exact_software_version_required")
        SemVer.parse(self.software_version)  # Ordering is never wire compatibility.
        exact_id(self.declaration_revision)
        exact_id(self.source_ref)
        require(type(self.schema_version) is int and self.schema_version == 1, "unsupported_declaration_schema")
        require(type(self.capabilities) is tuple and len(self.capabilities) <= 64
                and all(type(v) is CapabilityContract for v in self.capabilities)
                and len({v.capability_id for v in self.capabilities}) == len(self.capabilities),
                "bounded_unique_capabilities_required")
        for value in self.capabilities:
            value.__post_init__()

    def mapping(self):
        return {"schema_version": self.schema_version, "binding": self.binding.mapping(),
                "software_version": self.software_version, "declaration_revision": self.declaration_revision.value,
                "source_ref": self.source_ref.value,
                "capabilities": [v.mapping() for v in sorted(self.capabilities, key=lambda v: v.capability_id.value)]}

    @property
    def digest(self):
        """Integrity identity only; this is not signature or publisher validation."""
        return sha256(json.dumps(self.mapping(), sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class NegotiatedCapability:
    capability_id: CapabilityId
    declaration_digest: str
    request_version: int
    response_version: int
    transport: TransportKind

    def __post_init__(self):
        exact_id(self.capability_id, CapabilityId)
        digest_text(self.declaration_digest)
        schema_versions((self.request_version,))
        schema_versions((self.response_version,))
        require(type(self.transport) is TransportKind, "capability_transport_required")


def negotiate(declaration, capability_id, *, request_versions, response_versions, transport):
    """Pure declared intersection; no endpoint health or permission is inferred."""
    require(type(declaration) is AppCapabilityDeclaration, "app_declaration_required")
    declaration.__post_init__()
    exact_id(capability_id, CapabilityId)
    schema_versions(request_versions)
    schema_versions(response_versions)
    require(type(transport) is TransportKind, "capability_transport_required")
    contract = next((v for v in declaration.capabilities if v.capability_id == capability_id), None)
    require(contract is not None, "capability_not_declared")
    require(transport in contract.transports, "transport_not_declared")
    requests = set(request_versions).intersection(contract.request_versions)
    responses = set(response_versions).intersection(contract.response_versions)
    require(requests and responses, "capability_schema_incompatible")
    return NegotiatedCapability(capability_id, declaration.digest, max(requests), max(responses), transport)
