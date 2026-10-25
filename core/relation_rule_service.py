"""Detached relation/rule admission and review. No stores, jobs, Qt or writers.

Rule envelopes are a negotiated interface, not native Knowledge Pack records.
Unsupported rules retain their entire JSON payload and cannot execute.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
from typing import Any, Mapping

RULE_INTERFACE_VERSION = 1
RULE_SCHEMA_VERSION = 1
RULE_FORMAT = "collectigen.relation-rules"
MEANINGS = frozenset({"exact_identity", "subtype", "implication", "contextual_refinement", "retired_consider"})
MAX_RULE_BYTES = 64 * 1024 * 1024
MAX_RULES = 100_000
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 2_000_000


class RuleAdmissionError(ValueError):
    pass


def _json_value(value):
    # Iterative validation also bounds cycles and caller-built payloads, without
    # depending on Python's recursion limit or silently dropping unknown fields.
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > MAX_JSON_DEPTH:
            raise RuleAdmissionError("JSON payload exceeds supported depth")
        if nodes > MAX_JSON_NODES:
            raise RuleAdmissionError("JSON payload exceeds structural node bound")
        if item is None or type(item) in (str, bool, int):
            continue
        if type(item) is float and math.isfinite(item):
            continue
        if type(item) is list:
            children = item
        elif type(item) is dict and all(type(key) is str for key in item):
            children = item.values()
        else:
            raise RuleAdmissionError("Payload must be finite JSON with string keys")
        if nodes + len(children) + len(pending) > MAX_JSON_NODES:
            raise RuleAdmissionError("JSON payload exceeds structural node bound")
        pending.extend((child, depth + 1) for child in children)


def _parse_json(text, **kwargs):
    try:
        return json.loads(text, **kwargs)
    except RecursionError as error:
        raise RuleAdmissionError("JSON payload exceeds supported depth") from error
    except RuleAdmissionError:
        raise
    except (UnicodeError, ValueError) as error:
        raise RuleAdmissionError("Invalid JSON payload") from error


def _text(value, field):
    if type(value) is not str or not value.strip():
        raise RuleAdmissionError(f"{field} must be a nonempty string")
    return value


def _object(value, field):
    if type(value) is not dict:
        raise RuleAdmissionError(f"{field} must be an object")
    return value


def _list(value, field):
    if type(value) is not list:
        raise RuleAdmissionError(f"{field} must be a list")
    return value


def _validate_occurrence(value):
    _object(value, "occurrence")
    for key in ("owner_kind", "group", "source_value", "source_tag_id", "preimage_fingerprint"):
        _text(value.get(key), "occurrence " + key)
    kind = value["owner_kind"]
    if kind in {"object", "character", "context"}:
        _text(value.get("owner_id"), "occurrence owner_id")
    elif kind in {"image", "global", "scene", "custom_group", "visual_style"}:
        if value.get("owner_id") != "":
            raise RuleAdmissionError("Unowned native occurrences require empty owner_id")
    else:
        raise RuleAdmissionError("Unsupported occurrence owner_kind")


def _revision(value, field):
    if type(value) is not int or value < 1:
        raise RuleAdmissionError(f"{field} must be a positive integer")
    return value


@dataclass(frozen=True)
class FrozenPayload:
    """Canonical immutable bytes-in-text; every decoded view is a new object."""
    canonical_json: str

    def __post_init__(self):
        if type(self.canonical_json) is not str:
            raise RuleAdmissionError("FrozenPayload requires canonical JSON text")
        value = _parse_json(self.canonical_json)
        _json_value(value)
        expected = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if expected != self.canonical_json:
            raise RuleAdmissionError("FrozenPayload must use canonical JSON")

    @classmethod
    def capture(cls, value):
        _json_value(value)
        return cls(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))

    def to_value(self):
        return json.loads(self.canonical_json)

    @property
    def fingerprint(self):
        return sha256(self.canonical_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SourceBinding:
    tag_id: str
    catalog_id: str
    catalog_incarnation: str
    tag_incarnation: str
    authority_fingerprint: str
    state: str
    archive_fingerprint: str = ""

    @classmethod
    def from_dict(cls, row):
        _object(row, "source")
        fields = {key: _text(row.get(key), key) for key in
                  ("tag_id", "catalog_id", "catalog_incarnation", "tag_incarnation", "authority_fingerprint", "state")}
        if fields["state"] not in {"live", "retired"}:
            raise RuleAdmissionError("Invalid source lifecycle state")
        archive = row.get("archive_fingerprint", "")
        if type(archive) is not str or (fields["state"] == "retired" and not archive):
            raise RuleAdmissionError("Retired sources require explicit archive authority")
        return cls(**fields, archive_fingerprint=archive)


@dataclass(frozen=True)
class Role:
    name: str
    owner_kind: str


@dataclass(frozen=True)
class Condition:
    kind: str
    payload: FrozenPayload


@dataclass(frozen=True)
class Qualifier:
    dimension: str
    value_id: str
    state: str

    @classmethod
    def from_dict(cls, row):
        _object(row, "qualifier")
        dimension = _text(row.get("dimension"), "qualifier dimension")
        state = row.get("state")
        value = row.get("value_id", "")
        if type(state) is not str or state not in {"known", "required", "forbidden", "unknown"} or type(value) is not str:
            raise RuleAdmissionError("Invalid qualifier")
        if state != "unknown" and not value:
            raise RuleAdmissionError("Positive/negative qualifiers require identity")
        return cls(dimension, value, state)


@dataclass(frozen=True)
class Assertion:
    tag_id: str
    role: str
    qualifiers: tuple[Qualifier, ...]

    @classmethod
    def from_dict(cls, row):
        _object(row, "assertion")
        if type(row.get("qualifiers", [])) is not list:
            raise RuleAdmissionError("Assertion qualifiers must be a list")
        return cls(_text(row.get("tag_id"), "target tag"), _text(row.get("role"), "target role"),
                   tuple(Qualifier.from_dict(q) for q in row.get("qualifiers", [])))


@dataclass(frozen=True)
class Alternative:
    id: str
    targets: tuple[Assertion, ...]
    payload: FrozenPayload


@dataclass(frozen=True)
class Rule:
    id: str
    schema_revision: int
    content_revision: int
    payload: FrozenPayload

    @classmethod
    def from_dict(cls, row):
        if type(row) is not dict:
            raise RuleAdmissionError("Rule must be an object")
        result = cls(_text(row.get("id"), "rule ID"), _revision(row.get("schema_revision"), "schema revision"),
                     _revision(row.get("content_revision"), "content revision"), FrozenPayload.capture(row))
        if result.schema_revision == RULE_SCHEMA_VERSION:
            _text(row.get("meaning"), "rule meaning")
            SourceBinding.from_dict(row.get("source", {}))
            if type(row.get("reviewed")) is not bool or type(row.get("disabled")) is not bool:
                raise RuleAdmissionError("Explicit reviewed/disabled state required")
            for field in ("conditions", "roles", "qualifiers", "alternatives", "evidence"):
                if type(row.get(field)) is not list:
                    raise RuleAdmissionError(f"{field} must be a list")
            roles = result.roles
            if len({r.name for r in roles}) != len(roles):
                raise RuleAdmissionError("Duplicate role names")
            for condition in row["conditions"]:
                if type(condition) is not dict:
                    raise RuleAdmissionError("Conditions must be structured objects")
                _text(condition.get("kind"), "condition kind")
            for q in row["qualifiers"]:
                Qualifier.from_dict(q)
            alternatives = result.alternatives
            if not alternatives or len({a.id for a in alternatives}) != len(alternatives):
                raise RuleAdmissionError("Unique nonempty alternatives required")
            for alternative in alternatives:
                if not alternative.targets or any(t.role not in {r.name for r in roles} for t in alternative.targets):
                    raise RuleAdmissionError("Targets must form a nonempty bundle with declared roles")
                if len(set(alternative.targets)) != len(alternative.targets):
                    raise RuleAdmissionError("Duplicate conjunctive targets")
        return result

    @property
    def source(self):
        return SourceBinding.from_dict(self.payload.to_value()["source"])

    @property
    def roles(self):
        return tuple(Role(_text(_object(r, "role").get("name"), "role name"),
                          _text(r.get("owner_kind"), "owner kind"))
                     for r in self.payload.to_value().get("roles", []))

    @property
    def conditions(self):
        return tuple(Condition(_text(_object(c, "condition").get("kind"), "condition kind"), FrozenPayload.capture(c))
                     for c in self.payload.to_value().get("conditions", []))

    @property
    def qualifiers(self):
        return tuple(Qualifier.from_dict(q) for q in self.payload.to_value().get("qualifiers", []))

    @property
    def alternatives(self):
        return tuple(Alternative(_text(_object(a, "alternative").get("id"), "alternative ID"),
                                 tuple(Assertion.from_dict(t) for t in _list(a.get("targets"), "alternative targets")), FrozenPayload.capture(a))
                     for a in self.payload.to_value().get("alternatives", []))

    def to_dict(self):
        return self.payload.to_value()


@dataclass(frozen=True)
class RuleCollection:
    payload: FrozenPayload
    rules: tuple[Rule, ...]

    @classmethod
    def from_dict(cls, envelope, *, supported_interfaces=(RULE_INTERFACE_VERSION,)):
        _object(envelope, "envelope")
        if (envelope.get("format") != RULE_FORMAT or type(envelope.get("interface_version")) is not int
                or envelope.get("interface_version") != RULE_INTERFACE_VERSION
                or envelope.get("interface_version") not in supported_interfaces):
            raise RuleAdmissionError("Unsupported negotiated rule interface")
        if type(envelope.get("rules")) is not list or len(envelope["rules"]) > MAX_RULES:
            raise RuleAdmissionError(f"Rule collection requires at most {MAX_RULES} rules")
        seen = {}
        for row in envelope["rules"]:
            rule = Rule.from_dict(row)
            previous = seen.get(rule.id)
            if previous is not None and previous.payload != rule.payload:
                raise RuleAdmissionError("Conflicting complete payloads for rule ID " + rule.id)
            seen[rule.id] = rule
        # Exact duplicates stay in roundtrip bytes, but produce one query result.
        return cls(FrozenPayload.capture(envelope), tuple(seen.values()))

    def to_dict(self):
        return self.payload.to_value()


def decode_rules(raw: bytes, *, supported_interfaces=(RULE_INTERFACE_VERSION,)) -> RuleCollection:
    if type(raw) is not bytes or len(raw) > MAX_RULE_BYTES:
        raise RuleAdmissionError("Rule wire must be bounded UTF-8 bytes")
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise RuleAdmissionError("Duplicate JSON field " + key)
            result[key] = value
        return result
    try:
        value = _parse_json(raw.decode("utf-8"), object_pairs_hook=pairs)
        _json_value(value)
    except UnicodeError as error:
        raise RuleAdmissionError("Invalid rule wire") from error
    return RuleCollection.from_dict(value, supported_interfaces=supported_interfaces)


def encode_rules(collection: RuleCollection) -> bytes:
    raw = collection.payload.canonical_json.encode("utf-8")
    if len(raw) > MAX_RULE_BYTES:
        raise RuleAdmissionError("Rule wire exceeds bound")
    return raw


@dataclass(frozen=True)
class DependencySnapshot:
    """Caller-supplied admitted native evidence, detached without granting authority."""
    payload: FrozenPayload

    def __post_init__(self):
        data = _object(self.payload.to_value(), "dependency snapshot")
        for field in ("catalog", "lifecycle", "rules", "policy", "image_draft", "occurrence", "concepts"):
            _object(data.get(field), field)
        for key in ("id", "incarnation", "authority_fingerprint"):
            _text(data["catalog"].get(key), "catalog " + key)
        _validate_occurrence(data["occurrence"])
        RuleCollection.from_dict(data["rules"])
        for value in data["concepts"].values():
            _object(value, "concept")

    @classmethod
    def capture(cls, *, catalog, lifecycle, rules, policy, image_draft, occurrence, concepts):
        _object(catalog, "catalog")
        for key in ("id", "incarnation", "authority_fingerprint"):
            _text(catalog.get(key), "catalog " + key)
        _validate_occurrence(occurrence)
        return cls(FrozenPayload.capture(dict(catalog=catalog, lifecycle=lifecycle, rules=rules.to_dict(),
                        policy=policy, image_draft=image_draft, occurrence=occurrence, concepts=concepts)))

    @property
    def fingerprint(self):
        return self.payload.fingerprint


@dataclass(frozen=True)
class Candidate:
    rule_id: str
    alternative_id: str
    meaning: str
    bundle: tuple[Assertion, ...]
    holds: tuple[str, ...]
    evidence: FrozenPayload
    rule_payload: FrozenPayload


@dataclass(frozen=True)
class CandidateReview:
    dependency_fingerprint: str
    occurrence: FrozenPayload
    candidates: tuple[Candidate, ...]


@dataclass(frozen=True)
class ChangePlan:
    dependency_fingerprint: str
    occurrence: FrozenPayload
    additions: tuple[Assertion, ...]
    removals: tuple[Assertion, ...]
    preserved_assertions: FrozenPayload
    evidence: FrozenPayload
    preconditions: FrozenPayload
    holds: tuple[str, ...]
    decision: str


class RelationRuleService:
    """Versioned review-only service. No caller can obtain mutation authority here."""
    def inspect(self, snapshot: DependencySnapshot) -> CandidateReview:
        data = snapshot.payload.to_value()
        collection = RuleCollection.from_dict(data["rules"])
        occurrence = data["occurrence"]
        candidates = []
        for rule in collection.rules:
            row = rule.to_dict()
            raw_source = row.get("source", {})
            if type(raw_source) is not dict:
                candidates.append(Candidate(rule.id, "", "unknown", (), ("unsupported_rule_schema",), rule.payload, rule.payload))
                continue
            if raw_source.get("tag_id") != occurrence["source_tag_id"]:
                continue
            if rule.schema_revision != RULE_SCHEMA_VERSION:
                candidates.append(Candidate(rule.id, "", str(row.get("meaning", "unknown")), (),
                    ("unsupported_rule_schema",), rule.payload, rule.payload))
                continue
            source = rule.source
            holds = []
            concept = data["concepts"].get(source.tag_id, {})
            catalog = data["catalog"]
            if (source.catalog_id != catalog["id"] or source.catalog_incarnation != catalog["incarnation"]
                    or source.authority_fingerprint != catalog["authority_fingerprint"]
                    or source.tag_incarnation != concept.get("incarnation") or source.state != concept.get("state")):
                holds.append("source_authority_changed")
            if source.state == "retired" and source.archive_fingerprint != concept.get("archive_fingerprint"):
                holds.append("historical_authority_unavailable")
            if not row["reviewed"] or row["disabled"]:
                holds.append("rule_not_reviewed_or_disabled")
            if not row["evidence"]:
                holds.append("rule_evidence_missing")
            if data["policy"].get("permits_review") is not True:
                holds.append("review_policy_unavailable")
            meaning = row.get("meaning", "unknown")
            if meaning not in MEANINGS:
                holds.append("unsupported_rule_meaning")
            if source.state == "retired" and meaning != "retired_consider":
                holds.append("retired_source_requires_separate_identity_authority")
            if source.state != "retired" and meaning == "retired_consider":
                holds.append("source_not_retired")
            for alternative in rule.alternatives:
                target_holds = list(holds)
                for target in alternative.targets:
                    if data["concepts"].get(target.tag_id, {}).get("state") != "live":
                        target_holds.append("target_not_live")
                candidates.append(Candidate(rule.id, alternative.id, meaning, alternative.targets,
                    tuple(dict.fromkeys(target_holds)), FrozenPayload.capture(row["evidence"]), rule.payload))
        return CandidateReview(snapshot.fingerprint, FrozenPayload.capture(occurrence), tuple(candidates))

    def plan_choice(self, snapshot: DependencySnapshot, review: CandidateReview, *, decision="defer",
                    rule_id="", alternative_id="") -> ChangePlan:
        data = snapshot.payload.to_value()
        holds = []
        if review.dependency_fingerprint != snapshot.fingerprint or review != self.inspect(snapshot):
            holds.append("stale_or_foreign_review")
        if decision not in {"keep", "defer", "cancel", "choose"}:
            holds.append("unsupported_decision")
        matches = [c for c in review.candidates if c.rule_id == rule_id and c.alternative_id == alternative_id]
        if decision == "choose":
            if len(matches) != 1:
                holds.append("select_one_alternative")
            else:
                holds.extend(matches[0].holds)
                # Increment 1 does not evaluate constraints or stage image mutations.
                # Keep complete bundles/evidence and explicitly hold every execution.
                holds.append("rule_execution_not_supported")
        return ChangePlan(snapshot.fingerprint, FrozenPayload.capture(data["occurrence"]), (), (),
            FrozenPayload.capture(data["image_draft"]), FrozenPayload.capture([c.evidence.to_value() for c in matches]),
            snapshot.payload, tuple(dict.fromkeys(holds)), decision)


def read_exact_identity(resolution) -> FrozenPayload:
    """Adapt original alias/identity response; never infer a contextual equivalence."""
    return FrozenPayload.capture({key: getattr(resolution, key, "") for key in
        ("value", "normalized", "status", "tag_id")} | {
        key: list(getattr(resolution, key, ())) for key in
        ("owner_ids", "relationship_ids", "retired_ids", "unavailable_ids")})


def read_active_refinement(knowledge, categories, relationships, source_id) -> FrozenPayload:
    """Use the original live projection; detach full native rows alongside it."""
    from .confirmed_tag_identity import active_refinement_inputs
    relationships = tuple(relationships)
    inputs = active_refinement_inputs(knowledge, categories, relationships, source_id)
    return FrozenPayload.capture({"source_id": source_id, "displays": inputs["displays"],
        "candidate_ids": list(inputs["candidate_ids"]), "relationships": [r.to_dict() for r in relationships],
        "records": {tid: knowledge.tags[tid].to_dict() for tid in inputs["displays"]}})


def read_retired_guidance(knowledge, entry, recovery_record) -> FrozenPayload:
    """Legacy guidance remains labelled; recovery does not prove historical authority."""
    from .confirmed_tag_identity import removed_tag_guidance, retired_refinement_hold
    reason, alternatives = removed_tag_guidance(knowledge, entry, recovery_record)
    return FrozenPayload.capture({"kind": "legacy_guidance", "source_id": entry.stable_id,
        "reason": reason, "alternatives": [{"tag_id": tid, "display": label} for tid, label in alternatives],
        "historical_hold": retired_refinement_hold(knowledge, entry.stable_id),
        "record": recovery_record.to_dict() if recovery_record is not None else None})
