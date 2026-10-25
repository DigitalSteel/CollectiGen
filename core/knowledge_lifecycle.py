"""Pure, versioned local Knowledge identity rules; no storage or enrollment I/O.

An envelope belongs inside a future qualified canonical document. This module
does not reserve canonical document versions, infer identity from content,
mint tokens, migrate readers/writers, or enable history. Callers supply exact
effective IDs (including default-backed Categories), prepared UUIDs and native
archive proof, and must verify those inputs at the canonical write boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import re
from typing import Mapping
from uuid import UUID


SCHEMA_VERSION = 1
MAX_ENVELOPE_BYTES = 16 * 1024 * 1024
MAX_INCARNATIONS = 100_000
MAX_TRANSITIONS = 100_000
_ENVELOPE_FIELDS = {"schema_version", "namespace_id", "enrollment_id", "entity_kind", "incarnations"}
_ENTRY_FIELDS = {"record_id", "state", "origin", "transition_operation_id", "retirements", "successor"}
_PROOF_FIELDS = {"operation_id", "archive_id", "archive_sha256"}
_REFERENCE_FIELDS = {"namespace_id", "entity_kind", "record_id", "incarnation_id"}
_STATES = {"active", "retired", "redirected"}
_ORIGINS = {"enrolled_current", "created", "restored_legacy"}


class KnowledgeLifecycleError(ValueError):
    """Identity remains unproven; no canonical mutation is authorized."""


def _uuid(value):
    try:
        valid = isinstance(value, str) and str(UUID(value)) == value and UUID(value).int != 0
    except (ValueError, AttributeError):
        valid = False
    if not valid:
        raise KnowledgeLifecycleError("A prepared canonical nonzero UUID is required.")


def _text(value, *, limit=512):
    if (not isinstance(value, str) or not value or value.strip() != value
            or any(ord(char) < 32 for char in value)):
        raise KnowledgeLifecycleError("A bounded exact record/archive identity is required.")
    try:
        if len(value.encode("utf-8")) > limit:
            raise KnowledgeLifecycleError("Record/archive identity exceeds its byte bound.")
    except UnicodeError as exc:
        raise KnowledgeLifecycleError("Identity contains invalid Unicode.") from exc


def _kind(value):
    if value not in ("tag", "category"):
        raise KnowledgeLifecycleError("Unsupported lifecycle entity kind.")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise KnowledgeLifecycleError("Duplicate lifecycle JSON fields are ambiguous.")
        result[key] = value
    return result


def _decode(raw):
    try:
        return json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw, object_pairs_hook=_pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise KnowledgeLifecycleError("Invalid or duplicate lifecycle JSON.") from exc


def _bytes(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise KnowledgeLifecycleError("Lifecycle extensions must be finite JSON values.") from exc
    if len(raw) > MAX_ENVELOPE_BYTES:
        raise KnowledgeLifecycleError("Lifecycle envelope exceeds its byte bound; nothing was pruned.")
    return raw


def _extras(value, known):
    return _bytes({key: item for key, item in value.items() if key not in known}).decode("utf-8")


def _extension_payload(raw, known):
    if not isinstance(raw, str):
        raise KnowledgeLifecycleError("Lifecycle extensions exceed their byte bound.")
    try:
        if len(raw.encode("utf-8")) > MAX_ENVELOPE_BYTES:
            raise KnowledgeLifecycleError("Lifecycle extensions exceed their byte bound.")
    except UnicodeError as exc:
        raise KnowledgeLifecycleError("Invalid lifecycle extension Unicode.") from exc
    value = _decode(raw)
    if not isinstance(value, dict) or set(value).intersection(known):
        raise KnowledgeLifecycleError("Lifecycle extensions cannot override reserved fields.")
    return value


@dataclass(frozen=True)
class KnowledgeIdentityRef:
    namespace_id: str
    entity_kind: str
    record_id: str
    incarnation_id: str

    def __post_init__(self):
        _uuid(self.namespace_id)
        _kind(self.entity_kind)
        _text(self.record_id)
        _uuid(self.incarnation_id)

    def payload(self):
        return {name: getattr(self, name) for name in sorted(_REFERENCE_FIELDS)}


def _reference(value):
    if not isinstance(value, dict) or set(value) != _REFERENCE_FIELDS:
        raise KnowledgeLifecycleError("An exact namespace/kind/record/incarnation reference is required.")
    return KnowledgeIdentityRef(**value)


@dataclass(frozen=True)
class RetirementProof:
    operation_id: str
    archive_id: str | None = None
    archive_sha256: str | None = None
    extensions_json: str = "{}"

    def __post_init__(self):
        _uuid(self.operation_id)
        if self.archive_id is not None:
            _text(self.archive_id)
            if not isinstance(self.archive_sha256, str) or not re.fullmatch("[0-9a-f]{64}", self.archive_sha256):
                raise KnowledgeLifecycleError("An exact native archive digest is required.")
        elif self.archive_sha256 is not None:
            raise KnowledgeLifecycleError("An archive digest needs its archive identity.")
        _extension_payload(self.extensions_json, _PROOF_FIELDS)

    def payload(self):
        return {**_extension_payload(self.extensions_json, _PROOF_FIELDS),
                "operation_id": self.operation_id, "archive_id": self.archive_id,
                "archive_sha256": self.archive_sha256}


@dataclass(frozen=True)
class LifecycleEntry:
    reference: KnowledgeIdentityRef
    state: str
    origin: str
    transition_operation_id: str
    retirements: tuple[RetirementProof, ...] = ()
    successor: KnowledgeIdentityRef | None = None
    extensions_json: str = "{}"

    def __post_init__(self):
        if not isinstance(self.reference, KnowledgeIdentityRef):
            raise KnowledgeLifecycleError("A typed incarnation reference is required.")
        if (not isinstance(self.state, str) or self.state not in _STATES
                or not isinstance(self.origin, str) or self.origin not in _ORIGINS):
            raise KnowledgeLifecycleError("Unsupported lifecycle state or origin.")
        _uuid(self.transition_operation_id)
        if (type(self.retirements) is not tuple or len(self.retirements) > MAX_TRANSITIONS
                or any(not isinstance(proof, RetirementProof) for proof in self.retirements)
                or len({proof.operation_id for proof in self.retirements}) != len(self.retirements)):
            raise KnowledgeLifecycleError("Retirement evidence is invalid, repeated, or oversized.")
        if self.state in {"retired", "redirected"} and (
                not self.retirements or self.retirements[-1].operation_id != self.transition_operation_id):
            raise KnowledgeLifecycleError("An inactive incarnation needs its exact last retirement operation.")
        if self.state == "redirected":
            if (not isinstance(self.successor, KnowledgeIdentityRef)
                    or self.successor.namespace_id != self.reference.namespace_id
                    or self.successor.entity_kind != self.reference.entity_kind
                    or self.successor.record_id == self.reference.record_id):
                raise KnowledgeLifecycleError("A redirect needs a distinct exact local survivor.")
        elif self.successor is not None:
            raise KnowledgeLifecycleError("Only a redirected incarnation may declare a successor.")
        _extension_payload(self.extensions_json, _ENTRY_FIELDS)

    def payload(self):
        value = {**_extension_payload(self.extensions_json, _ENTRY_FIELDS),
                 "record_id": self.reference.record_id, "state": self.state,
                 "origin": self.origin, "transition_operation_id": self.transition_operation_id,
                 "retirements": [proof.payload() for proof in self.retirements]}
        if self.successor is not None:
            value["successor"] = self.successor.payload()
        return value


@dataclass(frozen=True)
class LifecycleEnvelope:
    namespace_id: str
    enrollment_id: str
    entity_kind: str
    entries: tuple[LifecycleEntry, ...]
    extensions_json: str = "{}"

    def __post_init__(self):
        _uuid(self.namespace_id)
        _uuid(self.enrollment_id)
        _kind(self.entity_kind)
        if type(self.entries) is not tuple or len(self.entries) > MAX_INCARNATIONS:
            raise KnowledgeLifecycleError("Lifecycle incarnation count exceeds its bound; nothing was pruned.")
        by_token = {}
        active = set()
        for entry in self.entries:
            if not isinstance(entry, LifecycleEntry):
                raise KnowledgeLifecycleError("Typed lifecycle entries are required.")
            ref = entry.reference
            if ref.namespace_id != self.namespace_id or ref.entity_kind != self.entity_kind:
                raise KnowledgeLifecycleError("Incarnation belongs to a foreign namespace or kind.")
            if ref.incarnation_id in by_token:
                raise KnowledgeLifecycleError("Duplicate incarnation identity.")
            by_token[ref.incarnation_id] = entry
            if entry.state == "active":
                if ref.record_id in active:
                    raise KnowledgeLifecycleError("Duplicate active record identity.")
                active.add(ref.record_id)
        completed = set()
        for token in by_token:
            seen = set()
            current = token
            while current not in completed:
                if current in seen:
                    raise KnowledgeLifecycleError("Lifecycle redirect cycle is invalid.")
                seen.add(current)
                entry = by_token[current]
                if entry.successor is None:
                    break
                target = by_token.get(entry.successor.incarnation_id)
                if target is None or target.reference != entry.successor:
                    raise KnowledgeLifecycleError("Lifecycle redirect survivor is missing or reused.")
                current = entry.successor.incarnation_id
            completed.update(seen)
        object.__setattr__(self, "entries", tuple(sorted(self.entries, key=lambda entry: entry.reference.incarnation_id)))
        # Bound unknown extensions and the complete result, not only inputs.
        self.to_bytes()

    def to_bytes(self):
        return _bytes({**_extension_payload(self.extensions_json, _ENVELOPE_FIELDS),
                       "schema_version": SCHEMA_VERSION, "namespace_id": self.namespace_id,
                       "enrollment_id": self.enrollment_id, "entity_kind": self.entity_kind,
                       "incarnations": {entry.reference.incarnation_id: entry.payload()
                                        for entry in self.entries}})

    @property
    def fingerprint(self):
        return hashlib.sha256(self.to_bytes()).hexdigest()

    def active_reference(self, record_id):
        _text(record_id)
        for entry in self.entries:
            if entry.state == "active" and entry.reference.record_id == record_id:
                return entry.reference
        raise KnowledgeLifecycleError("The requested live identity is unproven or inactive.")


def parse_envelope(raw: bytes) -> LifecycleEnvelope:
    """Strict lifecycle-only decoding; missing/legacy data is never auto-enrolled."""
    if not isinstance(raw, bytes) or len(raw) > MAX_ENVELOPE_BYTES:
        raise KnowledgeLifecycleError("A bounded serialized lifecycle envelope is required.")
    value = _decode(raw)
    if (not isinstance(value, dict) or not _ENVELOPE_FIELDS.issubset(value)
            or type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION):
        raise KnowledgeLifecycleError("Lifecycle schema is missing or unsupported; identity remains unproven.")
    namespace, kind = value["namespace_id"], value["entity_kind"]
    incarnations = value["incarnations"]
    if not isinstance(incarnations, dict) or len(incarnations) > MAX_INCARNATIONS:
        raise KnowledgeLifecycleError("Lifecycle incarnation inventory is invalid or oversized.")
    entries = []
    for token, item in incarnations.items():
        if (not isinstance(item, dict)
                or not {"record_id", "state", "origin", "transition_operation_id"}.issubset(item)):
            raise KnowledgeLifecycleError("Incomplete incarnation evidence.")
        proofs = item.get("retirements", [])
        if not isinstance(proofs, list) or len(proofs) > MAX_TRANSITIONS:
            raise KnowledgeLifecycleError("Retirement evidence must be a bounded list.")
        retired = []
        for proof in proofs:
            if not isinstance(proof, dict) or not _PROOF_FIELDS.issubset(proof):
                raise KnowledgeLifecycleError("Incomplete retirement evidence.")
            retired.append(RetirementProof(proof["operation_id"], proof["archive_id"],
                                           proof["archive_sha256"], _extras(proof, _PROOF_FIELDS)))
        entries.append(LifecycleEntry(
            KnowledgeIdentityRef(namespace, kind, item["record_id"], token), item["state"], item["origin"],
            item["transition_operation_id"], tuple(retired),
            _reference(item["successor"]) if "successor" in item else None,
            _extras(item, _ENTRY_FIELDS),
        ))
    return LifecycleEnvelope(namespace, value["enrollment_id"], kind,
                             tuple(sorted(entries, key=lambda entry: entry.reference.incarnation_id)),
                             _extras(value, _ENVELOPE_FIELDS))


def _effective_ids(values):
    if not isinstance(values, (tuple, list, set, frozenset)) or len(values) > MAX_INCARNATIONS:
        raise KnowledgeLifecycleError("An explicit bounded effective identity set is required.")
    for value in values:
        _text(value)
    if len(set(values)) != len(values):
        raise KnowledgeLifecycleError("Duplicate effective record identity.")
    return frozenset(values)


def _check_effective(envelope, values):
    expected = _effective_ids(values)
    actual = frozenset(entry.reference.record_id for entry in envelope.entries if entry.state == "active")
    if actual != expected:
        raise KnowledgeLifecycleError("Effective records do not match qualified active incarnations.")


def enroll_current(*, namespace_id, enrollment_id, entity_kind, effective_ids,
                   prepared_tokens: Mapping[str, str]) -> LifecycleEnvelope:
    """Construct a current baseline from explicit prepared tokens, without UUID generation.

    This does not accept existing envelopes or claim any pre-enrollment past.
    The caller must prove initial absence under shared writer admission.
    Default-only Categories are simply effective IDs; no local override needed.
    """
    values = _effective_ids(effective_ids)
    if not isinstance(prepared_tokens, Mapping) or set(prepared_tokens) != values:
        raise KnowledgeLifecycleError("Prepared tokens must cover the exact current effective identities.")
    entries = tuple(LifecycleEntry(
        KnowledgeIdentityRef(namespace_id, entity_kind, key, prepared_tokens[key]),
        "active", "enrolled_current", enrollment_id,
    ) for key in sorted(values))
    return LifecycleEnvelope(namespace_id, enrollment_id, entity_kind, entries)


def validate_namespace_pair(tags: LifecycleEnvelope, categories: LifecycleEnvelope):
    if (not isinstance(tags, LifecycleEnvelope) or not isinstance(categories, LifecycleEnvelope)
            or tags.entity_kind != "tag" or categories.entity_kind != "category"
            or tags.namespace_id != categories.namespace_id or tags.enrollment_id != categories.enrollment_id):
        raise KnowledgeLifecycleError("Tags and Categories need the same qualified namespace enrollment.")
    tag_tokens = {entry.reference.incarnation_id for entry in tags.entries}
    if tag_tokens.intersection(entry.reference.incarnation_id for entry in categories.entries):
        raise KnowledgeLifecycleError("Incarnation tokens cannot be reused across entity kinds.")


@dataclass(frozen=True)
class LifecycleTransition:
    action: str
    reference: KnowledgeIdentityRef
    retirement: RetirementProof | None = None
    successor: KnowledgeIdentityRef | None = None
    origin: str = "created"

    def __post_init__(self):
        if self.action not in ("preserve", "create", "retire", "restore", "redirect"):
            raise KnowledgeLifecycleError("Unknown lifecycle transition.")
        if not isinstance(self.reference, KnowledgeIdentityRef):
            raise KnowledgeLifecycleError("A typed transition reference is required.")
        if self.retirement is not None and not isinstance(self.retirement, RetirementProof):
            raise KnowledgeLifecycleError("A typed retirement proof is required.")
        if self.successor is not None and not isinstance(self.successor, KnowledgeIdentityRef):
            raise KnowledgeLifecycleError("A typed successor reference is required.")
        if (self.origin not in ("created", "restored_legacy")
                or (self.action != "create" and self.origin != "created")):
            raise KnowledgeLifecycleError("Only explicit creation can establish a new lifecycle origin.")


def apply_transitions(envelope: LifecycleEnvelope, transitions: tuple[LifecycleTransition, ...], *,
                      operation_id: str, expected_fingerprint: str,
                      effective_before, effective_after) -> LifecycleEnvelope:
    """Validate a complete atomic identity delta, preserving every retained entry.

    Untouched identities are preserved implicitly. Tokens cannot be edited,
    borrowed from a foreign namespace, or dropped. Exact restore uses the most
    recent retirement's archive proof; a legacy/ID-only restore needs an
    explicit new-incarnation creation with origin ``restored_legacy`` instead.
    """
    if not isinstance(envelope, LifecycleEnvelope) or envelope.fingerprint != expected_fingerprint:
        raise KnowledgeLifecycleError("Lifecycle changed since review; the exact pre-state is required.")
    _uuid(operation_id)
    if (type(transitions) is not tuple or len(transitions) > MAX_TRANSITIONS
            or any(not isinstance(item, LifecycleTransition) for item in transitions)):
        raise KnowledgeLifecycleError("A bounded typed lifecycle transition tuple is required.")
    _check_effective(envelope, effective_before)
    entries = {entry.reference.incarnation_id: entry for entry in envelope.entries}
    active = {entry.reference.record_id: entry for entry in envelope.entries if entry.state == "active"}
    redirected_ids = {entry.reference.record_id for entry in envelope.entries if entry.state == "redirected"}
    touched = set()
    new_redirects = []
    for change in transitions:
        ref = change.reference
        if (not isinstance(ref, KnowledgeIdentityRef) or ref.namespace_id != envelope.namespace_id
                or ref.entity_kind != envelope.entity_kind):
            raise KnowledgeLifecycleError("Lifecycle transition has a foreign identity.")
        if ref.incarnation_id in touched:
            raise KnowledgeLifecycleError("An incarnation has repeated transitions in one operation.")
        touched.add(ref.incarnation_id)
        entry = entries.get(ref.incarnation_id)
        if change.action == "create":
            if (entry is not None or ref.record_id in active or change.retirement is not None
                    or change.successor is not None or change.origin not in {"created", "restored_legacy"}
                    or ref.record_id in redirected_ids):
                raise KnowledgeLifecycleError("Creation cannot reuse active, retained, or redirected incarnation authority.")
            entries[ref.incarnation_id] = LifecycleEntry(ref, "active", change.origin, operation_id)
            active[ref.record_id] = entries[ref.incarnation_id]
            continue
        if entry is None or entry.reference != ref:
            raise KnowledgeLifecycleError("Exact historical incarnation is missing or reused.")
        if change.action == "preserve":
            if change.retirement is not None or change.successor is not None:
                raise KnowledgeLifecycleError("Preservation cannot change lifecycle evidence.")
            continue
        if change.action in {"retire", "redirect"}:
            proof = change.retirement
            if (entry.state != "active" or not isinstance(proof, RetirementProof)
                    or proof.operation_id != operation_id):
                raise KnowledgeLifecycleError("Retirement needs the exact active identity and prepared operation proof.")
            successor = change.successor
            if change.action == "redirect":
                target = active.get(successor.record_id) if isinstance(successor, KnowledgeIdentityRef) else None
                if target is None or target.reference != successor or successor.record_id == ref.record_id:
                    raise KnowledgeLifecycleError("Redirect requires the exact active survivor incarnation.")
                new_redirects.append(successor)
                redirected_ids.add(ref.record_id)
            elif successor is not None:
                raise KnowledgeLifecycleError("Retirement does not transfer identity to a survivor.")
            entries[ref.incarnation_id] = replace(entry,
                state="redirected" if successor is not None else "retired",
                transition_operation_id=operation_id, retirements=entry.retirements + (proof,), successor=successor)
            active.pop(ref.record_id)
        elif change.action == "restore":
            if (entry.state != "retired" or ref.record_id in active or change.successor is not None
                    or not isinstance(change.retirement, RetirementProof)
                    or change.retirement != entry.retirements[-1] or change.retirement.archive_id is None):
                raise KnowledgeLifecycleError("Restore requires the exact latest local archived retirement and current absence.")
            entries[ref.incarnation_id] = replace(entry, state="active", transition_operation_id=operation_id)
            active[ref.record_id] = entries[ref.incarnation_id]
        else:
            raise KnowledgeLifecycleError("Unknown lifecycle transition.")
    if any(ref.record_id not in active or active[ref.record_id].reference != ref for ref in new_redirects):
        raise KnowledgeLifecycleError("A merge survivor must remain that exact active incarnation after the operation.")
    result = LifecycleEnvelope(envelope.namespace_id, envelope.enrollment_id, envelope.entity_kind,
                               tuple(sorted(entries.values(), key=lambda item: item.reference.incarnation_id)),
                               envelope.extensions_json)
    _check_effective(result, effective_after)
    return result
