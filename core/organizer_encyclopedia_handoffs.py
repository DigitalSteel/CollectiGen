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


# Additive search-only endpoint contract. Existing hosted sender/receiver and
# canonical rename admission above are unchanged; nothing activates this seam.
from datetime import datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
import json
import re

from suite_core.backend_contracts import StableId
from .app_capabilities import (AppBinding, NegotiatedCapability, TAG_SEARCH_CAPABILITY,
    TransportKind, digest_text, exact_id, require)
from .app_availability import AppEndpointBinding, SelectedPeerProof, aware_time

MAX_SEARCH_LIFETIME = timedelta(minutes=2)
MAX_SCOPE_TTL = timedelta(seconds=30)


@dataclass(frozen=True, slots=True)
class RecipientSearchScope:
    """Already admitted recipient context; never paths or sender-derived choices."""
    profile_id: StableId
    library_ids: tuple[StableId, ...]
    privacy_scope: str
    scope_revision: StableId

    def __post_init__(self):
        exact_id(self.profile_id)
        exact_id(self.scope_revision)
        require(type(self.library_ids) is tuple and 1 <= len(self.library_ids) <= 64,
                "bounded_exact_libraries_required")
        for value in self.library_ids:
            exact_id(value)
        require(tuple(sorted(set(self.library_ids), key=lambda v: v.value)) == self.library_ids,
                "ordered_unique_libraries_required")
        # Local Only is deliberately not an endpoint scope.
        require(self.privacy_scope in {"local_explicit", "explicit_connection"},
                "explicit_shareable_privacy_scope_required")

    def mapping(self):
        return {"profile_id": self.profile_id.value, "library_ids": [v.value for v in self.library_ids],
                "privacy_scope": self.privacy_scope, "scope_revision": self.scope_revision.value}


@dataclass(frozen=True, slots=True)
class PreparedRecipientSearch:
    """Original recipient's review snapshot, not an incoming envelope assertion."""
    target: AppEndpointBinding
    scope: RecipientSearchScope
    prepared_at: datetime
    valid_for: timedelta

    def __post_init__(self):
        require(type(self.target) is AppEndpointBinding and type(self.scope) is RecipientSearchScope,
                "exact_prepared_recipient_required")
        self.target.__post_init__()
        self.scope.__post_init__()
        aware_time(self.prepared_at)
        require(type(self.valid_for) is timedelta and timedelta(0) < self.valid_for <= MAX_SCOPE_TTL,
                "bounded_prepared_scope_ttl_required")


@dataclass(frozen=True, slots=True)
class SearchHandoffEnvelope:
    request_id: str
    source: AppBinding
    source_generation: StableId
    target: AppEndpointBinding
    negotiation: NegotiatedCapability
    scope: RecipientSearchScope
    payload: MatchingImagesRequest
    issued_at: datetime
    expires_at: datetime
    schema_version: int = 1

    def __post_init__(self):
        _text(self.request_id)
        require(len(self.request_id) <= 160, "bounded_endpoint_request_id_required")
        require(type(self.source) is AppBinding and type(self.target) is AppEndpointBinding,
                "exact_handoff_app_bindings_required")
        self.source.__post_init__()
        self.target.__post_init__()
        exact_id(self.source_generation)
        require(type(self.scope) is RecipientSearchScope, "exact_recipient_scope_required")
        self.scope.__post_init__()
        require(type(self.payload) is MatchingImagesRequest, "search_v1_payload_only")
        self.payload.__post_init__()
        require(self.request_id == self.payload.request_id, "envelope_payload_request_id_mismatch")
        require(type(self.negotiation) is NegotiatedCapability, "exact_negotiation_required")
        self.negotiation.__post_init__()
        require(self.negotiation.capability_id == TAG_SEARCH_CAPABILITY
                and self.negotiation.request_version == self.negotiation.response_version == 1
                and self.negotiation.transport is TransportKind.LOCAL
                and self.negotiation.declaration_digest == self.target.declaration_digest,
                "negotiated_local_search_v1_required")
        aware_time(self.issued_at)
        aware_time(self.expires_at)
        require(timedelta(0) < self.expires_at - self.issued_at <= MAX_SEARCH_LIFETIME,
                "bounded_handoff_deadline_required")
        require(type(self.schema_version) is int and self.schema_version == 1,
                "search_envelope_v1_required")

    @property
    def fingerprint(self):
        # Canonical UTC avoids offset spelling differences for equal instants.
        endpoint = self.target
        value = {"schema_version": 1, "request_id": self.request_id, "source": self.source.mapping(),
                 "source_generation": self.source_generation.value, "target": endpoint.app.mapping(),
                 "connection_ref": endpoint.connection_ref.value, "instance_id": endpoint.instance_id.value,
                 "endpoint_ref": endpoint.endpoint_ref.value, "declaration_digest": endpoint.declaration_digest,
                 "capability_id": self.negotiation.capability_id.value, "request_version": 1,
                 "response_version": 1, "transport": "local", "scope": self.scope.mapping(),
                 "payload": {key: getattr(self.payload, key) for key in self.payload.__dataclass_fields__},
                 "issued_at": self.issued_at.astimezone(timezone.utc).isoformat(),
                 "expires_at": self.expires_at.astimezone(timezone.utc).isoformat()}
        return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AdmittedSearchHandoff:
    """Current recipient validation result; an opener must consume exact scope.

    This is not evidence of visible delivery. A future adapter must enforce the
    scope in the native Dataset operation, which currently accepts only text.
    """
    envelope: SearchHandoffEnvelope
    display_name: str
    admitted_at: datetime

    def __post_init__(self):
        require(type(self.envelope) is SearchHandoffEnvelope, "exact_search_envelope_required")
        self.envelope.__post_init__()
        _text(self.display_name)
        aware_time(self.admitted_at)
        require(self.envelope.issued_at <= self.admitted_at < self.envelope.expires_at,
                "admission_outside_request_lifetime")


def admit_search_envelope(envelope, *, prepared, current_scope, current_target,
                          current_source, current_source_generation, peer_proof, catalog, knowledge, now):
    """Fresh recipient validation; no route, transfer, profile selection or write."""
    require(type(envelope) is SearchHandoffEnvelope, "exact_search_envelope_required")
    envelope.__post_init__()
    aware_time(now)
    require(envelope.issued_at <= now < envelope.expires_at, "expired_or_future_search_request")
    require(type(prepared) is PreparedRecipientSearch, "original_prepared_recipient_required")
    prepared.__post_init__()
    require(prepared.prepared_at <= now <= prepared.prepared_at + prepared.valid_for,
            "prepared_recipient_scope_expired")
    require(type(current_scope) is RecipientSearchScope and current_scope == prepared.scope == envelope.scope,
            "recipient_profile_library_privacy_or_revision_changed")
    current_scope.__post_init__()
    require(type(current_target) is AppEndpointBinding and current_target == prepared.target == envelope.target,
            "selected_target_instance_changed")
    require(type(current_source) is AppBinding and current_source == envelope.source,
            "selected_source_binding_changed")
    exact_id(current_source_generation)
    require(current_source_generation == envelope.source_generation, "stale_sender_generation")
    require(type(peer_proof) is SelectedPeerProof and peer_proof.valid_at(current_target, now),
            "selected_peer_proof_required")
    # Preserve all existing catalog/revision/stable-ID/borrowed Knowledge checks.
    display = admit_request(envelope.payload, catalog, knowledge)
    return AdmittedSearchHandoff(envelope, display, now)


class SearchReceiptStage(str, Enum):
    QUEUED = "queued"
    DELIVERED = "delivered"
    HELD = "held"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    UNKNOWN_OUTCOME = "unknown_outcome"


@dataclass(frozen=True, slots=True)
class SearchHandoffReceipt:
    request_id: str
    source: AppBinding
    source_generation: StableId
    target: AppEndpointBinding
    fingerprint: str
    stage: SearchReceiptStage
    reason: str
    observed_at: datetime
    expires_at: datetime

    def __post_init__(self):
        _text(self.request_id)
        require(type(self.source) is AppBinding and type(self.target) is AppEndpointBinding,
                "exact_receipt_bindings_required")
        require(len(self.request_id) <= 160, "bounded_endpoint_request_id_required")
        self.source.__post_init__()
        self.target.__post_init__()
        exact_id(self.source_generation)
        digest_text(self.fingerprint)
        require(type(self.stage) is SearchReceiptStage, "exact_receipt_stage_required")
        _text(self.reason)
        require(re.fullmatch(r"[a-z][a-z0-9_]{0,127}", self.reason), "content_free_receipt_reason_required")
        aware_time(self.observed_at)
        aware_time(self.expires_at)


class SearchSettlementLedger:
    """Bounded, single-owner in-memory receipt lookup; no dispatch or retries.

    Caller serializes access. Delivery is an explicit consumer assertion after
    exact-scope admission AND actual native search opening. Records survive
    connection loss in this object, not process restart. Unknown is terminal;
    an ambiguous request is never automatically submitted again.
    """
    def __init__(self, *, maximum=128, retention=timedelta(minutes=5)):
        require(type(maximum) is int and 1 <= maximum <= 512, "bounded_receipt_capacity_required")
        require(type(retention) is timedelta and timedelta(0) < retention <= timedelta(minutes=5),
                "bounded_receipt_retention_required")
        self.maximum, self.retention = maximum, retention
        self._records = {}
        self._last_now = None

    def _clock(self, now):
        aware_time(now)
        require(self._last_now is None or now >= self._last_now, "receipt_clock_rollback")
        self._last_now = now
        for key, receipt in tuple(self._records.items()):
            if now >= receipt.expires_at + self.retention:
                del self._records[key]
            elif receipt.stage is SearchReceiptStage.QUEUED and now >= receipt.expires_at:
                self._records[key] = self._with(receipt, SearchReceiptStage.EXPIRED, "request_expired", now)

    @staticmethod
    def _key(envelope):
        require(type(envelope) is SearchHandoffEnvelope, "exact_search_envelope_required")
        envelope.__post_init__()
        return (envelope.source, envelope.request_id)

    @staticmethod
    def _with(receipt, stage, reason, now):
        from dataclasses import replace
        return replace(receipt, stage=stage, reason=reason, observed_at=now)

    def _existing(self, envelope, now):
        self._clock(now)
        receipt = self._records.get(self._key(envelope))
        if receipt is not None:
            require(receipt.fingerprint == envelope.fingerprint, "duplicate_request_content_conflict")
        return receipt

    def queue(self, admission, *, now):
        require(type(admission) is AdmittedSearchHandoff, "current_search_admission_required")
        admission.__post_init__()
        envelope = admission.envelope
        existing = self._existing(envelope, now)
        if existing is not None:
            return existing  # Includes cancelled/lost requests; never redispatch.
        require(admission.admitted_at == now and envelope.issued_at <= now < envelope.expires_at,
                "fresh_search_admission_required")
        require(len(self._records) < self.maximum, "receipt_ledger_full")
        receipt = SearchHandoffReceipt(envelope.request_id, envelope.source, envelope.source_generation, envelope.target,
            envelope.fingerprint, SearchReceiptStage.QUEUED, "awaiting_native_search", now, envelope.expires_at)
        self._records[self._key(envelope)] = receipt
        return receipt

    def lookup(self, envelope, *, now):
        return self._existing(envelope, now)

    def record_delivery(self, admission, *, now):
        """Call only AFTER actual exact-scope native opening, with fresh admission."""
        require(type(admission) is AdmittedSearchHandoff, "current_search_admission_required")
        admission.__post_init__()
        require(admission.admitted_at == now, "fresh_search_admission_required")
        receipt = self._existing(admission.envelope, now)
        require(receipt is not None and receipt.stage is SearchReceiptStage.QUEUED,
                "queued_request_required_for_delivery")
        receipt = self._with(receipt, SearchReceiptStage.DELIVERED, "native_search_opened", now)
        self._records[self._key(admission.envelope)] = receipt
        return receipt

    def hold(self, envelope, *, reason, now):
        return self._settle(envelope, SearchReceiptStage.HELD, reason, now)

    def cancel(self, envelope, *, now):
        return self._settle(envelope, SearchReceiptStage.CANCELLED, "explicit_request_cancelled", now)

    def connection_lost(self, envelope, *, now):
        return self._settle(envelope, SearchReceiptStage.UNKNOWN_OUTCOME, "connection_lost_no_resend", now)

    def _settle(self, envelope, stage, reason, now):
        receipt = self._existing(envelope, now)
        require(receipt is not None, "queued_request_required_for_settlement")
        if receipt.stage is not SearchReceiptStage.QUEUED:
            return receipt
        receipt = self._with(receipt, stage, reason, now)
        self._records[self._key(envelope)] = receipt
        return receipt

    def invalidate_source_generation(self, source, generation, *, now):
        """Trusted source-observer notification; incoming requests cannot set it."""
        require(type(source) is AppBinding, "app_binding_required")
        exact_id(generation)
        self._clock(now)
        # Retain no payload/catalog content; only the source epoch identity.
        for key, receipt in tuple(self._records.items()):
            if (receipt.source == source and receipt.source_generation != generation
                    and receipt.stage is SearchReceiptStage.QUEUED):
                self._records[key] = self._with(receipt, SearchReceiptStage.HELD, "sender_generation_invalidated", now)
