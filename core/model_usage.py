"""Opt-in successful input use, attested by an execution owner, never selection.

Owners retain the FIRST admission token immutably in their durable execution
receipt. A verifier must not rebind an old execution to a new admission. This is
the trust boundary that lets bounded history reject replay without retaining an
unbounded list of execution identities. Verifiers run outside registry locks.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime
import re
from uuid import uuid4

from .model_resource_registry import ModelResourceError
from .model_workspace import _path

MAX_PENDING = 256
MAX_OBSERVATIONS = 256
MAX_INPUTS = 64
_ID = re.compile(r"sha256:[0-9a-f]{64}")
_OPAQUE = re.compile(r"[A-Za-z0-9_.:-]{1,160}")


@dataclass(frozen=True)
class UsageAdmission:
    generation: str
    sequence: int

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        if (not isinstance(value, dict) or set(value) != {"generation", "sequence"}
                or not isinstance(value["generation"], str)
                or not re.fullmatch(r"[0-9a-f]{32}", value["generation"])
                or type(value["sequence"]) is not int or not 1 <= value["sequence"] < 2**63):
            raise ModelResourceError("Usage admission is invalid; preserve the owner receipt.")
        return cls(**value)


@dataclass(frozen=True)
class UsageAdmissionFacts:
    owner_id: str
    owner_version: str
    execution_id: str
    resource_ids: tuple[str, ...]
    required_set_digest: str


@dataclass(frozen=True)
class UsageAttestation(UsageAdmissionFacts):
    completed_at: str
    admission: UsageAdmission


def _facts(value):
    if not isinstance(value, UsageAdmissionFacts):
        raise ModelResourceError("A trusted execution owner must attest model use.")
    for key in ("owner_id", "owner_version", "execution_id"):
        text = getattr(value, key)
        if not isinstance(text, str) or not _OPAQUE.fullmatch(text):
            raise ModelResourceError("Usage accepts bounded opaque owner and execution identities only.")
    if (not isinstance(value.resource_ids, tuple) or not 1 <= len(value.resource_ids) <= MAX_INPUTS
            or any(not isinstance(key, str) or not _ID.fullmatch(key) for key in value.resource_ids)
            or tuple(sorted(set(value.resource_ids))) != value.resource_ids):
        raise ModelResourceError("Usage requires a sorted complete set of exact input identities.")
    if not isinstance(value.required_set_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", value.required_set_digest):
        raise ModelResourceError("Usage requires the exact complete-set digest.")
    return {"owner_id": value.owner_id, "owner_version": value.owner_version,
            "execution_id": value.execution_id, "resource_ids": list(value.resource_ids),
            "required_set_digest": value.required_set_digest}


def _time(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ModelResourceError("Usage completion needs an explicit UTC time.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ModelResourceError("Usage completion needs an explicit UTC time.") from exc
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise ModelResourceError("Usage completion needs an explicit UTC time.")
    return parsed


def _row_facts(row):
    try:
        return _facts(UsageAdmissionFacts(row["owner_id"], row["owner_version"], row["execution_id"],
                                         tuple(row["resource_ids"]), row["required_set_digest"]))
    except (KeyError, TypeError) as exc:
        raise ModelResourceError("Retained successful-use evidence is invalid.") from exc


def usage_state(snapshot):
    """Read-only strict projection; absent history is disabled and unknown."""
    state = snapshot.get("model_usage")
    if state is None and "model_usage" not in snapshot:
        return {"schema_version": 1, "enabled": False, "generation": "", "next_sequence": 1,
                "closed_through": 0, "pending": {}, "observations": [], "compacted": False}
    keys = {"schema_version", "enabled", "generation", "next_sequence", "closed_through", "pending", "observations", "compacted"}
    if (not isinstance(state, dict) or set(state) != keys or type(state["schema_version"]) is not int
            or state["schema_version"] != 1 or type(state["enabled"]) is not bool
            or type(state["compacted"]) is not bool):
        raise ModelResourceError("Successful-use history is unsupported; preserve it for review.")
    UsageAdmission.from_dict({"generation": state["generation"], "sequence": state["next_sequence"]})
    closed = state["closed_through"]
    if type(closed) is not int or not 0 <= closed < state["next_sequence"]:
        raise ModelResourceError("Usage replay watermark is invalid.")
    pending, observations = state["pending"], state["observations"]
    if not isinstance(pending, dict) or len(pending) > MAX_PENDING or not isinstance(observations, list) or len(observations) > MAX_OBSERVATIONS:
        raise ModelResourceError("Successful-use history exceeds its supported bound.")
    seen = set()
    facts_keys = {"owner_id", "owner_version", "execution_id", "resource_ids", "required_set_digest"}
    for key, row in pending.items():
        if (not isinstance(key, str) or not key.isdecimal() or str(int(key)) != key
                or not closed < int(key) < state["next_sequence"] or not isinstance(row, dict)
                or set(row) != facts_keys):
            raise ModelResourceError("Pending usage admission is invalid.")
        _row_facts(row); seen.add(int(key))
    for row in observations:
        if not isinstance(row, dict) or set(row) != facts_keys | {"sequence", "completed_at"}:
            raise ModelResourceError("Retained successful-use evidence is invalid.")
        seq = row["sequence"]
        if type(seq) is not int or not 0 < seq < state["next_sequence"] or seq in seen:
            raise ModelResourceError("Retained usage sequence is invalid.")
        _row_facts(row); _time(row["completed_at"]); seen.add(seq)
    return deepcopy(state)


def successful_use_summary(snapshot):
    state = usage_state(snapshot)
    resources = {}
    for row in state["observations"]:
        for identity in row["resource_ids"]:
            item = resources.setdefault(identity, {"successful_use_count": 0, "last_success_at": ""})
            item["successful_use_count"] += 1
            if not item["last_success_at"] or _time(row["completed_at"]) > _time(item["last_success_at"]):
                item["last_success_at"] = row["completed_at"]
    return {"enabled": state["enabled"], "resources": resources,
            "retained_successes": len(state["observations"]), "history_compacted": state["compacted"],
            "pending_count": len(state["pending"]),
            "message": "Counts cover retained verified successes only; earlier and external use is unknown."}


class ModelUsageService:
    def __init__(self, registry, *, admission_verifier=None, success_verifier=None, protected_roots=()):
        self.registry = registry
        self.admission_verifier, self.success_verifier = admission_verifier, success_verifier
        self.protected_roots = tuple(protected_roots)
        self.last_admission_state = "unavailable"

    def _guard(self):
        _path(self.registry.path, self.protected_roots)

    def summary(self):
        self._guard()
        value, token = self.registry.snapshot()
        return {**successful_use_summary(value), "registry_token": token}

    def set_enabled(self, enabled, *, expected_registry=None):
        if type(enabled) is not bool:
            raise ModelResourceError("Choose whether to remember successful model use.")
        self._guard()
        with self.registry._writer():
            value, token = self.registry.snapshot(); state = usage_state(value)
            if expected_registry is not None and token != expected_registry:
                raise ModelResourceError("Model preferences changed. Review successful-use settings again.")
            if state["enabled"] == enabled:
                return
            state.update(enabled=enabled, generation=uuid4().hex, next_sequence=1, closed_through=0, pending={})
            # Existing observations are independent of the new admission epoch.
            # Renumber them into the new epoch so all state has one sequence space.
            for index, row in enumerate(state["observations"], 1):
                row["sequence"] = index
            state["next_sequence"] = len(state["observations"]) + 1
            state["closed_through"] = state["next_sequence"] - 1
            value["model_usage"] = state; self.registry._save(value, token)

    def reset(self, *, expected_registry=None):
        """Clear active usage only; registry recovery backups follow registry policy."""
        self._guard()
        with self.registry._writer():
            value, token = self.registry.snapshot(); old = usage_state(value)
            if expected_registry is not None and token != expected_registry:
                raise ModelResourceError("Model preferences changed. Review the usage reset again.")
            state = usage_state({})
            state.update(enabled=old["enabled"], generation=uuid4().hex)
            value["model_usage"] = state; self.registry._save(value, token)

    def admit(self, owner_id, owner_version, execution_id, resource_ids, required_set_digest):
        self.last_admission_state = "unavailable"
        self._guard()
        captured = usage_state(self.registry.snapshot()[0])
        if not captured["enabled"]:
            self.last_admission_state = "disabled"; return None
        if self.admission_verifier is None:
            return None
        proposed = _facts(UsageAdmissionFacts(owner_id, owner_version, execution_id, resource_ids, required_set_digest))
        trusted = self.admission_verifier(execution_id)
        if trusted is None:
            return None
        if _facts(trusted) != proposed:
            raise ModelResourceError("The execution owner's admitted inputs changed.")
        with self.registry._writer():
            value, token = self.registry.snapshot(); state = usage_state(value)
            if not state["enabled"]:
                self.last_admission_state = "disabled"; return None
            if state["generation"] != captured["generation"]:
                self.last_admission_state = "stale"; return None
            if any(identity not in value["resources"] for identity in resource_ids):
                raise ModelResourceError("Every successful-use input must be registered by exact identity.")
            for seq, row in state["pending"].items():
                if row == proposed:
                    self.last_admission_state = "admitted"
                    return UsageAdmission(state["generation"], int(seq))
            if len(state["pending"]) >= MAX_PENDING or state["next_sequence"] >= 2**63 - 1:
                self.last_admission_state = "history_full"; return None
            admission = UsageAdmission(state["generation"], state["next_sequence"])
            state["next_sequence"] += 1
            state["pending"][str(admission.sequence)] = proposed
            value["model_usage"] = state; self.registry._save(value, token)
            self.last_admission_state = "admitted"
            return admission

    def record_success(self, admission, proof):
        admission = UsageAdmission.from_dict(admission.to_dict()) if isinstance(admission, UsageAdmission) else UsageAdmission.from_dict(admission)
        self._guard()
        state = usage_state(self.registry.snapshot()[0])
        status = self._status(state, admission)
        if status:
            return {"state": status}
        if self.success_verifier is None:
            return {"state": "unavailable"}
        evidence = self.success_verifier(proof)
        if evidence is None:
            return {"state": "unverified"}
        if type(evidence) is not UsageAttestation or evidence.admission != admission:
            raise ModelResourceError("Successful use needs the owner's immutable original admission.")
        facts = _facts(evidence); _time(evidence.completed_at)
        with self.registry._writer():
            value, token = self.registry.snapshot(); state = usage_state(value)
            status = self._status(state, admission)
            if status:
                return {"state": status}
            if state["pending"][str(admission.sequence)] != facts:
                raise ModelResourceError("Verified successful inputs differ from the admitted complete set.")
            del state["pending"][str(admission.sequence)]
            state["observations"].append({**facts, "sequence": admission.sequence, "completed_at": evidence.completed_at})
            if len(state["observations"]) > MAX_OBSERVATIONS:
                state["observations"] = state["observations"][-MAX_OBSERVATIONS:]
                state["compacted"] = True
            state["closed_through"] = min(map(int, state["pending"]), default=state["next_sequence"]) - 1
            value["model_usage"] = state; self.registry._save(value, token)
        return {"state": "recorded"}

    @staticmethod
    def _status(state, admission):
        if not state["enabled"]:
            return "disabled"
        if admission.generation != state["generation"]:
            return "stale"
        if str(admission.sequence) not in state["pending"]:
            return "already_recorded" if any(row["sequence"] == admission.sequence for row in state["observations"]) else "stale"
        return ""
