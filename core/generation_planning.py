"""Versioned, generator-neutral planning and readiness for image generation.

This module deliberately does not submit generation work.  It preserves an
exact source reference, records reviewed changes in a detached neutral request,
and provides the fail-closed preflight consumed by generator adapters.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any, Callable, Mapping
from uuid import uuid4

from .annotation import GenerationRecord, GenerationResource
from .external_write_transaction import atomic_verified_bytes, write_verified_json
from .generation_metadata import GenerationTranslation, translate_generation_record
from .training_source_io import _assert_unredirected, _identity


PLAN_SCHEMA_VERSION = 1
MAX_PLAN_BYTES = 16 * 1024 * 1024
MAX_TEXT_BYTES = 4 * 1024 * 1024
MAX_COLLECTION_ITEMS = 20_000
MAX_JSON_DEPTH = 64
MAX_RECOVERY_RECEIPT_BYTES = 64 * 1024

_INTENTS = {"reproduce", "variation", "new"}
_INTENT_ALIASES = {
    "reproduce as closely as possible": "reproduce",
    "create a reviewed variation": "variation",
    "create something new": "new",
}
_PROTECTED_REQUEST_PATHS = {"raw", "source", "source_field"}
_KNOWLEDGE_GROUPS = {
    "character_wearer",
    "character_wearer_outfit",
    "character_associated",
    "character_associated_outfit",
    "character_other",
    "generic_outfit",
    "composition",
    "style",
    "model_trigger",
    "compatible_setting",
    "seed",
    "prompt_fragment",
    "other",
}
_RESOURCE_ROLE_ALIASES = {"controlnet": "control_model"}
_TARGET_ALIASES = {
    "a1111": "automatic1111",
    "automatic1111": "automatic1111",
    "forge": "forge",
    "sdnext": "sdnext",
    "swarm": "swarmui",
    "swarmui": "swarmui",
    "comfy": "comfyui",
    "comfyui": "comfyui",
}
_SCALAR_SETTINGS = (
    "positive_prompt",
    "negative_prompt",
    "sampler",
    "scheduler",
    "seed",
    "steps",
    "cfg_scale",
    "width",
    "height",
    "denoising_strength",
)
_PARAMETER_ALIASES = {
    "positive_prompt": "prompt",
    "negative_prompt": "negativeprompt",
    "cfg_scale": "cfgscale",
    "denoising_strength": "initimagecreativity",
}


class GenerationPlanError(RuntimeError):
    """Base error for invalid or unsafe generation-plan operations."""


class GenerationPlanValidationError(GenerationPlanError):
    pass


class GenerationPlanStaleError(GenerationPlanError):
    pass


class GenerationPlanRecoveryRequired(GenerationPlanError):
    pass


def _reject_constant(value: str) -> None:
    raise ValueError(f"Unsupported JSON number: {value}")


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GenerationPlanValidationError("Duplicate JSON fields are not supported.")
        result[key] = value
    return result


def _check_bounds(value: Any, *, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise GenerationPlanValidationError("The generation plan exceeds the nesting limit.")
    if value is None or isinstance(value, (bool, int, float)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise GenerationPlanValidationError("Generation plans require finite JSON numbers.")
        return
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_TEXT_BYTES:
            raise GenerationPlanValidationError("A generation-plan text value exceeds the supported limit.")
        return
    if isinstance(value, list):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise GenerationPlanValidationError("A generation-plan list exceeds the supported item limit.")
        for item in value:
            _check_bounds(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise GenerationPlanValidationError("A generation-plan object exceeds the supported field limit.")
        for key, item in value.items():
            if not isinstance(key, str):
                raise GenerationPlanValidationError("Generation-plan object keys must be strings.")
            _check_bounds(key, depth=depth + 1)
            _check_bounds(item, depth=depth + 1)
        return
    raise GenerationPlanValidationError(f"Generation plans cannot store {type(value).__name__} values.")


def _json_value(value: Any) -> Any:
    def prepare(item: Any, depth: int = 0) -> Any:
        if depth > MAX_JSON_DEPTH:
            raise GenerationPlanValidationError("The generation plan exceeds the nesting limit.")
        if hasattr(item, "to_dict") and callable(item.to_dict):
            return prepare(item.to_dict(), depth + 1)
        if isinstance(item, Mapping):
            return {key: prepare(nested, depth + 1) for key, nested in item.items()}
        if isinstance(item, (list, tuple)):
            return [prepare(nested, depth + 1) for nested in item]
        return item

    value = prepare(value)
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise GenerationPlanValidationError("The generation plan contains an invalid JSON value.") from exc
    if len(raw) > MAX_PLAN_BYTES:
        raise GenerationPlanValidationError("The generation plan exceeds the supported size limit.")
    try:
        copied = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise GenerationPlanValidationError("The generation plan contains invalid JSON.") from exc
    _check_bounds(copied)
    return copied


def _canonical_bytes(value: Any) -> bytes:
    copied = _json_value(value)
    return json.dumps(
        copied,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _document_bytes(value: Any) -> bytes:
    copied = _json_value(value)
    raw = (json.dumps(copied, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(raw) > MAX_PLAN_BYTES:
        raise GenerationPlanValidationError("The generation plan exceeds the supported size limit.")
    return raw


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _byte_digest(raw: bytes | None) -> str:
    return hashlib.sha256(raw if raw is not None else b"<absent>").hexdigest()


def _decode(raw: bytes) -> Any:
    if len(raw) > MAX_PLAN_BYTES:
        raise GenerationPlanValidationError("The saved generation plan exceeds the supported size limit.")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_pairs,
            parse_constant=_reject_constant,
        )
    except GenerationPlanValidationError:
        raise
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise GenerationPlanValidationError("The saved generation plan is invalid JSON.") from exc
    _check_bounds(value)
    return value


def _normal_intent(value: Any) -> str:
    intent = str(value or "new").strip().casefold()
    intent = _INTENT_ALIASES.get(intent, intent)
    if intent not in _INTENTS:
        raise GenerationPlanValidationError("Choose reproduce, variation, or new as the planning intent.")
    return intent


def _default_safety_policy(source_identity: Mapping[str, Any] | None) -> dict[str, Any]:
    supplied = source_identity.get("content_safety") if isinstance(source_identity, Mapping) else None
    if isinstance(supplied, Mapping):
        policy = _json_value(dict(supplied))
    else:
        policy = {
            "policy_version": 1,
            "state": "review_required",
            "reviewed": False,
            "source": "default",
            "consequence": "Execution remains blocked until content safety is reviewed.",
        }
    return _validate_safety_policy(policy)


def _validate_safety_policy(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise GenerationPlanValidationError("Content safety must be an explicit policy object.")
    policy = _json_value(dict(value))
    if type(policy.get("policy_version")) is not int or policy["policy_version"] < 1:
        raise GenerationPlanValidationError("Content safety requires a positive policy_version.")
    state = str(policy.get("state") or "").strip().casefold()
    if state not in {"review_required", "allowed", "restricted", "blocked"}:
        raise GenerationPlanValidationError("Content safety has an unsupported state.")
    if not isinstance(policy.get("reviewed"), bool):
        raise GenerationPlanValidationError("Content safety requires an explicit reviewed value.")
    policy["state"] = state
    return policy


def _validate_fragment(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise GenerationPlanValidationError("Each knowledge fragment must be an object.")
    fragment = _json_value(dict(value))
    fragment_id = str(fragment.get("id") or "").strip()
    text = str(fragment.get("text") or "")
    group = str(fragment.get("group") or "other").strip().casefold()
    if not fragment_id or len(fragment_id.encode("utf-8")) > 512:
        raise GenerationPlanValidationError("Each knowledge fragment requires a bounded stable ID.")
    if group not in _KNOWLEDGE_GROUPS:
        raise GenerationPlanValidationError(f"Knowledge fragment {fragment_id!r} has an unsupported group.")
    if not isinstance(fragment.get("reviewed"), bool) or not isinstance(fragment.get("included"), bool):
        raise GenerationPlanValidationError("Knowledge fragments require explicit reviewed and included values.")
    if fragment["included"] and not fragment["reviewed"]:
        raise GenerationPlanValidationError("Unreviewed knowledge cannot be included in a generation request.")
    fragment.update({"id": fragment_id, "text": text, "group": group})
    fragment.setdefault("scope", "image")
    fragment.setdefault("source", "reviewed_local")
    fragment.setdefault("provenance", {})
    fragment.setdefault("consequence", "")
    return _json_value(fragment)


def _validate_fragments(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise GenerationPlanValidationError("Knowledge fragments must be a list.")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        fragment = _validate_fragment(item)
        if fragment["id"] in seen:
            raise GenerationPlanValidationError("Knowledge fragment IDs must be unique.")
        seen.add(fragment["id"])
        result.append(fragment)
    return result


_PATH_PART = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def _path_parts(path: str) -> list[str | int]:
    if not isinstance(path, str) or not path or len(path) > 4096:
        raise GenerationPlanValidationError("A request change has an invalid path.")
    parts: list[str | int] = []
    position = 0
    for match in _PATH_PART.finditer(path):
        if match.start() != position and path[position:match.start()] != ".":
            raise GenerationPlanValidationError(f"Request path {path!r} is invalid.")
        parts.append(int(match.group(2)) if match.group(2) is not None else match.group(1))
        position = match.end()
    if position != len(path) or not parts:
        raise GenerationPlanValidationError(f"Request path {path!r} is invalid.")
    return parts


def _value_at(root: Any, parts: list[str | int]) -> Any:
    current = root
    for part in parts:
        try:
            current = current[part]
        except (KeyError, IndexError, TypeError) as exc:
            raise GenerationPlanValidationError("A request change points to a missing value.") from exc
    return deepcopy(current)


def _set_at(root: dict[str, Any], path: str, value: Any) -> tuple[Any, Any]:
    parts = _path_parts(path)
    if not isinstance(parts[0], str) or parts[0] in _PROTECTED_REQUEST_PATHS:
        raise GenerationPlanValidationError(f"Request field {parts[0]!r} is an immutable source reference.")
    current: Any = root
    for part in parts[:-1]:
        try:
            current = current[part]
        except (KeyError, IndexError, TypeError) as exc:
            raise GenerationPlanValidationError(f"Request path {path!r} does not exist.") from exc
    final = parts[-1]
    before = _value_at(root, parts) if (
        isinstance(current, list) and isinstance(final, int) and 0 <= final < len(current)
    ) or (isinstance(current, dict) and isinstance(final, str) and final in current) else None
    try:
        if isinstance(current, list):
            if not isinstance(final, int) or final < 0 or final >= len(current):
                raise GenerationPlanValidationError(f"Request path {path!r} has an invalid list index.")
            current[final] = _json_value(value)
        elif isinstance(current, dict) and isinstance(final, str):
            current[final] = _json_value(value)
        else:
            raise GenerationPlanValidationError(f"Request path {path!r} cannot be changed.")
    except IndexError as exc:
        raise GenerationPlanValidationError(f"Request path {path!r} has an invalid list index.") from exc
    return before, deepcopy(current[final])


@dataclass(frozen=True)
class ReadinessIssue:
    code: str
    path: str
    message: str
    consequence: str = "Execution is blocked until this issue is resolved."
    recovery: str = "Review the request and refresh readiness."
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "path": self.path,
            "message": self.message,
            "consequence": self.consequence,
            "recovery": self.recovery,
            "details": _json_value(self.details),
        }


@dataclass(frozen=True)
class GenerationReadiness:
    ready: bool
    exact_replay: bool
    issues: tuple[ReadinessIssue, ...]
    translation: GenerationTranslation | None
    export_available: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "exact_replay": self.exact_replay,
            "export_available": self.export_available,
            "issues": [item.to_dict() for item in self.issues],
            "translation": self.translation.to_dict() if self.translation is not None else None,
        }


@dataclass(frozen=True, slots=True)
class GenerationPlan:
    """Immutable snapshots for a source, neutral request, and reviewed changes."""

    _payload: bytes = field(repr=False)
    _storage_digest: str = field(default="", repr=False, compare=False)

    @classmethod
    def create(
        cls,
        record: GenerationRecord | None,
        intent: str = "new",
        goal: str = "",
        source_identity: dict[str, Any] | None = None,
    ) -> "GenerationPlan":
        chosen = _normal_intent(intent)
        if record is None and chosen == "reproduce":
            raise GenerationPlanValidationError("A reproduce plan requires an existing generation record.")
        if record is not None and not isinstance(record, GenerationRecord):
            raise GenerationPlanValidationError("The source must be a GenerationRecord or None.")
        identity = _json_value(source_identity or {})
        if not isinstance(identity, dict):
            raise GenerationPlanValidationError("Source identity must be an object.")
        plan_id = f"generation-plan.{uuid4().hex}"
        source = _json_value(record.to_dict()) if record is not None else None
        request = deepcopy(source) if source is not None else GenerationRecord(id=f"request_{plan_id.rsplit('.', 1)[-1][:12]}").to_dict()
        payload = {
            "schema_version": PLAN_SCHEMA_VERSION,
            "plan_id": plan_id,
            "revision": 0,
            "intent": chosen,
            "goal": str(goal or ""),
            "source_record": source,
            "source_identity": identity,
            "source_fingerprint": _sha(source) if source is not None else "",
            "source_identity_fingerprint": _sha(identity) if identity else "",
            "request": request,
            "request_fingerprint": _sha(request),
            "knowledge_fragments": [],
            "safety_policy": _default_safety_policy(identity),
            "target_context": {},
            "changes": [],
        }
        return cls._from_payload(payload, require_digest=False)

    @classmethod
    def from_dict(cls, value: Any) -> "GenerationPlan":
        if not isinstance(value, dict):
            raise GenerationPlanValidationError("A generation plan must be a JSON object.")
        return cls._from_payload(value, require_digest=True)

    @classmethod
    def _from_payload(cls, value: Any, *, require_digest: bool) -> "GenerationPlan":
        payload = _json_value(value)
        if not isinstance(payload, dict):
            raise GenerationPlanValidationError("A generation plan must be a JSON object.")
        digest = payload.pop("digest", None)
        version = payload.get("schema_version")
        if type(version) is not int or version < 1:
            raise GenerationPlanValidationError("The generation plan has an invalid schema version.")
        plan_id = payload.get("plan_id")
        if not isinstance(plan_id, str) or not plan_id or len(plan_id) > 512:
            raise GenerationPlanValidationError("The generation plan has an invalid ID.")
        if type(payload.get("revision")) is not int or payload["revision"] < 0:
            raise GenerationPlanValidationError("The generation plan has an invalid revision.")
        payload["intent"] = _normal_intent(payload.get("intent"))
        payload["goal"] = str(payload.get("goal") or "")
        source = payload.get("source_record")
        if source is not None and not isinstance(source, dict):
            raise GenerationPlanValidationError("The source generation record is invalid.")
        source_record = GenerationRecord.from_dict(source) if source is not None else None
        request_value = payload.get("request")
        if not isinstance(request_value, dict):
            raise GenerationPlanValidationError("The neutral request is invalid.")
        request = GenerationRecord.from_dict(request_value)
        normalized_source = source_record.to_dict() if source_record is not None else None
        normalized_request = request.to_dict()
        if source_record is not None and normalized_request.get("raw") != normalized_source.get("raw"):
            raise GenerationPlanValidationError("The request changed the immutable raw source payload.")
        if payload.get("source_fingerprint", "") != (_sha(normalized_source) if normalized_source is not None else ""):
            raise GenerationPlanValidationError("The source generation fingerprint does not match.")
        identity = payload.get("source_identity", {})
        if not isinstance(identity, dict):
            raise GenerationPlanValidationError("The source identity is invalid.")
        if payload.get("source_identity_fingerprint", "") != (_sha(identity) if identity else ""):
            raise GenerationPlanValidationError("The source identity fingerprint does not match.")
        if payload.get("request_fingerprint") != _sha(normalized_request):
            raise GenerationPlanValidationError("The neutral request fingerprint does not match.")
        payload["source_record"] = normalized_source
        payload["request"] = normalized_request
        payload["knowledge_fragments"] = _validate_fragments(payload.get("knowledge_fragments", []))
        payload["safety_policy"] = _validate_safety_policy(payload.get("safety_policy"))
        if not isinstance(payload.get("target_context", {}), dict):
            raise GenerationPlanValidationError("The saved target context is invalid.")
        payload.setdefault("target_context", {})
        changes = payload.get("changes", [])
        if not isinstance(changes, list) or any(not isinstance(item, dict) for item in changes):
            raise GenerationPlanValidationError("The saved generation-plan changes are invalid.")
        payload["changes"] = _json_value(changes)
        wanted = _sha(payload)
        if require_digest and digest != wanted:
            raise GenerationPlanValidationError("The saved generation-plan digest does not match.")
        payload["digest"] = wanted
        encoded = _canonical_bytes(payload)
        if len(encoded) > MAX_PLAN_BYTES:
            raise GenerationPlanValidationError("The generation plan exceeds the supported size limit.")
        return cls(encoded)

    def _data(self) -> dict[str, Any]:
        return json.loads(self._payload.decode("utf-8"))

    @property
    def schema_version(self) -> int:
        return int(self._data()["schema_version"])

    @property
    def plan_id(self) -> str:
        return str(self._data()["plan_id"])

    @property
    def revision(self) -> int:
        return int(self._data()["revision"])

    @property
    def intent(self) -> str:
        return str(self._data()["intent"])

    @property
    def goal(self) -> str:
        return str(self._data()["goal"])

    @property
    def digest(self) -> str:
        return str(self._data()["digest"])

    @property
    def storage_digest(self) -> str:
        """Exact saved-file token populated by :func:`load_plan`."""
        return self._storage_digest

    @property
    def source(self) -> GenerationRecord | None:
        value = self._data().get("source_record")
        return GenerationRecord.from_dict(value) if value is not None else None

    @property
    def source_identity(self) -> dict[str, Any]:
        return deepcopy(self._data()["source_identity"])

    @property
    def source_fingerprint(self) -> str:
        return str(self._data()["source_fingerprint"])

    @property
    def request(self) -> GenerationRecord:
        return GenerationRecord.from_dict(self._data()["request"])

    @property
    def request_fingerprint(self) -> str:
        return str(self._data()["request_fingerprint"])

    @property
    def knowledge_fragments(self) -> tuple[dict[str, Any], ...]:
        return tuple(deepcopy(self._data()["knowledge_fragments"]))

    @property
    def safety_policy(self) -> dict[str, Any]:
        return deepcopy(self._data()["safety_policy"])

    @property
    def target_context(self) -> dict[str, Any]:
        return deepcopy(self._data()["target_context"])

    def to_dict(self) -> dict[str, Any]:
        return self._data()

    def export_json(self) -> bytes:
        return export_plan(self)

    def diff(self) -> list[dict[str, Any]]:
        return deepcopy(self._data()["changes"])

    def fork(self, *, seed: int | None = None) -> "GenerationPlan":
        """Create a separately reviewed iteration without changing source evidence."""
        if self.schema_version > PLAN_SCHEMA_VERSION:
            raise GenerationPlanValidationError("A newer plan cannot be forked by this version.")
        if seed is not None and (type(seed) is not int or not 0 <= seed < 2**63):
            raise GenerationPlanValidationError("An iteration seed must be a nonnegative 63-bit integer.")
        data = self._data()
        parent_digest = data.pop("digest", None)
        parent_id = data["plan_id"]
        identity = uuid4().hex
        data["plan_id"] = f"generation-plan.{identity}"
        data["request"]["id"] = f"request_{identity[:12]}"
        if seed is not None:
            previous = data["request"].get("seed")
            for stage in data["request"].get("stages", []):
                if previous is not None and stage.get("seeds") == [previous]:
                    stage["seeds"] = [seed]
            data["request"]["seed"] = seed
        data["request_fingerprint"] = _sha(data["request"])
        data["revision"] = 0
        data["changes"] = []
        data["safety_policy"]["reviewed"] = False
        context = data["target_context"]
        for key in ("adapter_id", "adapter_version", "fingerprint"):
            context.pop(key, None)
        context["iteration"] = {"parent_plan_id": parent_id, "parent_plan_digest": parent_digest,
                                "seed": data["request"].get("seed")}
        context["seed_choice"] = {"mode": "specific", "value": data["request"].get("seed")}
        return self._from_payload(data, require_digest=False)

    def revise(
        self,
        changes: dict[str, Any],
        source: str = "user",
        consequence: str = "",
    ) -> "GenerationPlan":
        if self.schema_version > PLAN_SCHEMA_VERSION:
            raise GenerationPlanValidationError("A plan from a newer schema can be exported but not revised.")
        if not isinstance(changes, dict) or not changes:
            raise GenerationPlanValidationError("A revision requires at least one explicit change.")
        provenance = str(source or "").strip()
        if not provenance or len(provenance.encode("utf-8")) > 4096:
            raise GenerationPlanValidationError("A revision requires a bounded provenance source.")
        consequence_text = str(consequence or "")
        data = self._data()
        data.pop("digest", None)
        next_revision = data["revision"] + 1
        events: list[dict[str, Any]] = []

        def record(path: str, before: Any, after: Any) -> None:
            if before == after:
                return
            events.append({
                "revision": next_revision,
                "path": path,
                "before": _json_value(before),
                "after": _json_value(after),
                "source": provenance,
                "consequence": consequence_text,
            })

        controls = {
            "request", "goal", "intent", "knowledge_fragments", "knowledge",
            "knowledge_selection", "safety_policy", "content_safety", "target_context",
        }
        request_changes: dict[str, Any] = {}
        nested = changes.get("request", {})
        if nested is not None:
            if not isinstance(nested, dict):
                raise GenerationPlanValidationError("The request revision must be an object.")
            request_changes.update(nested)
        for key, value in changes.items():
            if key not in controls:
                request_changes[key] = value
        request = deepcopy(data["request"])
        for path, value in request_changes.items():
            before, after = _set_at(request, str(path), value)
            record(f"request.{path}", before, after)
        rebuilt = GenerationRecord.from_dict(request).to_dict()
        if data["source_record"] is not None and rebuilt.get("raw") != data["source_record"].get("raw"):
            raise GenerationPlanValidationError("The request changed the immutable raw source payload.")
        data["request"] = rebuilt

        if "goal" in changes:
            before, after = data["goal"], str(changes["goal"] or "")
            data["goal"] = after
            record("goal", before, after)
        if "intent" in changes:
            before, after = data["intent"], _normal_intent(changes["intent"])
            if after == "reproduce" and data["source_record"] is None:
                raise GenerationPlanValidationError("A reproduce plan requires an existing generation record.")
            data["intent"] = after
            record("intent", before, after)
        if "knowledge_fragments" in changes or "knowledge" in changes:
            supplied = changes.get("knowledge_fragments", changes.get("knowledge"))
            after = _validate_fragments(_json_value(supplied))
            before = data["knowledge_fragments"]
            data["knowledge_fragments"] = after
            record("knowledge_fragments", before, after)
        if "knowledge_selection" in changes:
            selection = changes["knowledge_selection"]
            if not isinstance(selection, Mapping) or any(not isinstance(value, bool) for value in selection.values()):
                raise GenerationPlanValidationError("Knowledge selection maps fragment IDs to explicit booleans.")
            selection = {str(key): value for key, value in selection.items()}
            before = deepcopy(data["knowledge_fragments"])
            known = {fragment["id"] for fragment in before}
            missing = sorted(str(key) for key in selection if str(key) not in known)
            if missing:
                raise GenerationPlanValidationError(f"Unknown knowledge fragment IDs: {', '.join(missing)}")
            for fragment in data["knowledge_fragments"]:
                if fragment["id"] in selection:
                    fragment["included"] = selection[fragment["id"]]
                    _validate_fragment(fragment)
            record("knowledge_fragments", before, data["knowledge_fragments"])
        if "safety_policy" in changes or "content_safety" in changes:
            before = data["safety_policy"]
            after = _validate_safety_policy(changes.get("safety_policy", changes.get("content_safety")))
            source_safety = data["source_identity"].get("content_safety", {})
            source_state = (
                str(source_safety.get("state") or "").casefold()
                if isinstance(source_safety, dict) else ""
            )
            if after["state"] == "allowed" and (
                data["source_identity"].get("protected") is True
                or source_state in {"blocked", "restricted"}
            ):
                raise GenerationPlanValidationError(
                    "A general acknowledgement cannot weaken protected source content safety."
                )
            data["safety_policy"] = after
            record("safety_policy", before, after)
        if "target_context" in changes:
            if not isinstance(changes["target_context"], Mapping):
                raise GenerationPlanValidationError("Target context must be an object.")
            before, after = data["target_context"], _json_value(dict(changes["target_context"]))
            data["target_context"] = after
            record("target_context", before, after)
        if not events:
            return self
        data["request_fingerprint"] = _sha(data["request"])
        data["revision"] = next_revision
        data["changes"].extend(events)
        return type(self)._from_payload(data, require_digest=False)

    @property
    def seed_choice(self) -> str:
        """Return the saved presentation choice only while its exact seed agrees."""
        seed = self.request.seed
        source_seed = self.source.seed if self.source is not None else None
        source_known = type(source_seed) is int and 0 <= source_seed < 2**63
        saved = self.target_context.get("seed_choice")
        if isinstance(saved, dict) and saved.get("value") == seed:
            mode = saved.get("mode")
            if mode == "specific":
                return mode
            if mode == "random" and type(seed) is int and 0 <= seed < 2**63:
                return mode
            if mode == "same" and source_known and seed == source_seed:
                return mode
        return "same" if source_known and seed == source_seed else "specific"

    def choose_seed(self, mode: str, value: int | None = None) -> "GenerationPlan":
        """Resolve a deliberate choice into an exact, portable request revision."""
        previous = self.request.seed
        if mode == "same":
            value = self.source.seed if self.source is not None else None
            if type(value) is not int or not 0 <= value < 2**63:
                raise GenerationPlanValidationError("The source image has no supported recorded seed.")
        elif mode == "random":
            # A reroll always changes the current concrete seed, including zero.
            value = ((previous + 1 + secrets.randbelow(2**63 - 1)) % 2**63
                     if type(previous) is int and 0 <= previous < 2**63
                     else secrets.randbelow(2**63))
        elif mode != "specific":
            raise GenerationPlanValidationError("Choose Same Seed, Specific Seed or Random Seed.")
        if type(value) is not int or not 0 <= value < 2**63:
            raise GenerationPlanValidationError("Enter a whole seed from 0 to 9223372036854775807.")
        changes: dict[str, Any] = {"seed": value}
        stages = [stage.to_dict() for stage in self.request.stages]
        linked = False
        for stage in stages:
            if previous is not None and stage.get("seeds") == [previous]:
                stage["seeds"] = [value]
                linked = True
        if linked:
            changes["stages"] = stages
        context = self.target_context
        context["seed_choice"] = {"mode": mode, "value": value}
        return self.revise({"request": changes, "target_context": context}, source="seed_choice",
                           consequence="Explicit seed choice; matching stage seeds follow the request, source evidence stays unchanged")

    def apply_fragments(self, role: str = "positive") -> "GenerationPlan":
        """Deliberately append selected reviewed fragments to the current prompt.

        Selection alone never changes a prompt.  Application starts from the
        current user-edited text and only appends missing fragments, so a later
        selection change cannot remove or reconstruct manual edits.
        """
        normalized_role = str(role or "").strip().casefold()
        aliases = {
            "positive": "positive_prompt",
            "positive_prompt": "positive_prompt",
            "negative": "negative_prompt",
            "negative_prompt": "negative_prompt",
        }
        field_name = aliases.get(normalized_role)
        if field_name is None:
            raise GenerationPlanValidationError("Knowledge fragments can be applied to the positive or negative prompt.")
        role_key = "positive" if field_name == "positive_prompt" else "negative"
        current = str(getattr(self.request, field_name) or "")
        searchable = " ".join(current.casefold().split())
        additions: list[str] = []
        normalized_additions: set[str] = set()
        applied_ids: list[str] = []
        for fragment in self.knowledge_fragments:
            prompt_role = str(fragment.get("prompt_role") or "positive").strip().casefold()
            if prompt_role not in {role_key, field_name, "both"}:
                continue
            if fragment.get("reviewed") is not True or fragment.get("included") is not True:
                continue
            text = str(fragment.get("text") or "").strip()
            normalized = " ".join(text.casefold().split())
            if not normalized or normalized in searchable or normalized in normalized_additions:
                continue
            additions.append(text)
            normalized_additions.add(normalized)
            applied_ids.append(str(fragment["id"]))
        if not additions:
            return self
        separator = "" if not current else (" " if current.endswith((",", ";", "\n", " ")) else ", ")
        updated = current + separator + ", ".join(additions)
        return self.revise(
            {field_name: updated},
            source="reviewed_knowledge",
            consequence=f"Applied reviewed {normalized_role} prompt fragments: {', '.join(applied_ids)}.",
        )


def _explicit_ids(value: Mapping[str, Any], singular: str, plural: str) -> set[str]:
    result: set[str] = set()
    one = value.get(singular)
    if isinstance(one, str) and one.strip():
        result.add(one.strip())
    many = value.get(plural, [])
    if many is not None:
        if not isinstance(many, (list, tuple)) or any(not isinstance(item, str) or not item.strip() for item in many):
            raise GenerationPlanValidationError(f"{plural} must contain explicit character IDs.")
        result.update(item.strip() for item in many)
    return result


def outfit_scope(
    current_wearer: str | None,
    characters: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    outfits: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    """Group outfits using only caller-supplied wearer and association IDs."""
    if not isinstance(characters, (list, tuple)) or not isinstance(outfits, (list, tuple)):
        raise GenerationPlanValidationError("Outfit scope requires explicit character and outfit lists.")
    character_values = [_json_value(value) for value in characters]
    outfit_values = [_json_value(value) for value in outfits]
    if any(not isinstance(value, dict) for value in character_values + outfit_values):
        raise GenerationPlanValidationError("Outfit scope entries must be objects.")
    character_ids = [str(value.get("id") or "").strip() for value in character_values]
    if any(not value for value in character_ids) or len(set(character_ids)) != len(character_ids):
        raise GenerationPlanValidationError("Characters require unique explicit IDs for outfit scope.")
    selected = str(current_wearer or "").strip()
    if selected and selected not in set(character_ids):
        raise GenerationPlanValidationError("The current wearer must be one of the explicit characters.")
    groups: dict[str, list[dict[str, Any]]] = {
        "wearer": [], "associated": [], "other": [], "generic": [],
    }
    known = set(character_ids)
    seen_outfits: set[str] = set()
    planning_groups = {
        "wearer": "character_wearer_outfit",
        "associated": "character_associated_outfit",
        "other": "character_other",
        "generic": "generic_outfit",
    }
    for outfit in outfit_values:
        outfit_id = str(outfit.get("id") or "").strip()
        if not outfit_id or outfit_id in seen_outfits:
            raise GenerationPlanValidationError("Outfits require unique explicit IDs for outfit scope.")
        seen_outfits.add(outfit_id)
        wearers = _explicit_ids(outfit, "wearer_id", "wearer_ids")
        associated = _explicit_ids(outfit, "associated_character_id", "associated_character_ids")
        unknown = (wearers | associated) - known
        if unknown:
            raise GenerationPlanValidationError(
                f"Outfit {outfit_id!r} references characters outside the explicit scope: {', '.join(sorted(unknown))}."
            )
        if selected and selected in wearers:
            group = "wearer"
        elif selected and selected in associated:
            group = "associated"
        elif wearers or associated:
            group = "other"
        else:
            group = "generic"
        scoped = deepcopy(outfit)
        scoped["planning_group"] = planning_groups[group]
        groups[group].append(scoped)
    return {
        "current_wearer": selected,
        "characters": character_values,
        "groups": groups,
    }


def outfit_association_scope(
    selected_character: str | None,
    characters: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    outfits: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    """Project three browse-only scopes from declared associations, not wearers."""
    if not isinstance(characters, (list, tuple)) or not isinstance(outfits, (list, tuple)):
        raise GenerationPlanValidationError("Outfit browsing requires explicit character and outfit lists.")
    character_values = [_json_value(value) for value in characters]
    outfit_values = [_json_value(value) for value in outfits]
    if any(not isinstance(value, dict) for value in character_values + outfit_values):
        raise GenerationPlanValidationError("Outfit browsing entries must be objects.")
    character_ids = [str(value.get("id") or "").strip() for value in character_values]
    if any(not value for value in character_ids) or len(set(character_ids)) != len(character_ids):
        raise GenerationPlanValidationError("Characters require unique explicit IDs for outfit browsing.")
    selected = str(selected_character or "").strip()
    known = set(character_ids)
    if selected and selected not in known:
        raise GenerationPlanValidationError("Choose a character from the current outfit browser.")
    groups: dict[str, list[dict[str, Any]]] = {"associated": [], "other": [], "generic": []}
    unavailable: dict[str, list[str]] = {}
    seen_outfits: set[str] = set()
    for outfit in outfit_values:
        outfit_id = str(outfit.get("id") or "").strip()
        if not outfit_id or outfit_id in seen_outfits:
            raise GenerationPlanValidationError("Outfits require unique explicit IDs for browsing.")
        seen_outfits.add(outfit_id)
        if "associated_character_id" in outfit or "associated_character_ids" in outfit:
            associations = _explicit_ids(outfit, "associated_character_id", "associated_character_ids")
        else:
            associations = _explicit_ids(outfit, "character_id", "character_ids")
        missing = sorted(associations - known)
        if missing:
            unavailable[outfit_id] = missing
        if selected and selected in associations:
            group = "associated"
        elif associations:
            group = "other"
        else:
            group = "generic"
        groups[group].append(deepcopy(outfit))
    return {"selected_character": selected, "groups": groups,
            "unavailable_associations": unavailable}


def export_plan(plan: GenerationPlan) -> bytes:
    if not isinstance(plan, GenerationPlan):
        raise GenerationPlanValidationError("Only a validated GenerationPlan can be exported.")
    return _document_bytes(plan.to_dict())


def _read_plan_bytes(path: Path, limit: int = MAX_PLAN_BYTES) -> bytes | None:
    _assert_unredirected(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        current = path.stat(follow_symlinks=False)
        _assert_unredirected(path)
        if not stat.S_ISREG(before.st_mode) or _identity(before) != _identity(current):
            raise GenerationPlanValidationError("The generation-plan path changed before it could be read.")
        raw = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
    current_after = path.stat(follow_symlinks=False)
    _assert_unredirected(path)
    if _identity(before) != _identity(after) or _identity(after) != _identity(current_after):
        raise GenerationPlanValidationError("The generation-plan path changed while it was read.")
    if len(raw) > limit:
        raise GenerationPlanValidationError("The saved generation plan exceeds the supported size limit.")
    return raw


def _pending_path(path: Path) -> Path:
    return path.with_name(path.name + ".pending.json")


def _validate_recovery_receipt(value: Any, target: Path) -> dict[str, Any]:
    required = {
        "schema_version", "operation_id", "target_name", "before_absent",
        "before_sha256", "after_sha256",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value.get("schema_version") != 1
        or value.get("target_name") != target.name
        or type(value.get("before_absent")) is not bool
        or not isinstance(value.get("operation_id"), str)
        or not re.fullmatch(r"[0-9a-f]{32}", value["operation_id"])
        or any(
            not isinstance(value.get(key), str)
            or not re.fullmatch(r"[0-9a-f]{64}", value[key])
            for key in ("before_sha256", "after_sha256")
        )
    ):
        raise GenerationPlanRecoveryRequired("The generation-plan recovery receipt is invalid.")
    return value


def _plan_from_bytes(raw: bytes, *, storage_digest: str = "") -> GenerationPlan:
    plan = GenerationPlan.from_dict(_decode(raw))
    if storage_digest:
        object.__setattr__(plan, "_storage_digest", storage_digest)
    return plan


def _guarded_write(path: Path, payload: bytes, *, label: str) -> None:
    _assert_unredirected(path.parent)
    _assert_unredirected(path)
    atomic_verified_bytes(path, payload, label=label)
    _assert_unredirected(path)


def _guarded_json(path: Path, payload: dict[str, Any], *, label: str) -> None:
    _assert_unredirected(path.parent)
    _assert_unredirected(path)
    write_verified_json(path, payload, label=label)
    _assert_unredirected(path)


@contextmanager
def _plan_writer(target: Path):
    """Serialize save/recovery across processes for one exact target path."""
    _assert_unredirected(target.parent)
    target.parent.mkdir(parents=True, exist_ok=True)
    _assert_unredirected(target.parent)
    lock = target.with_name(target.name + ".lock")
    _assert_unredirected(lock)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock, flags, 0o600)
    with os.fdopen(descriptor, "r+b") as handle:
        state = os.fstat(handle.fileno())
        current = lock.stat(follow_symlinks=False)
        _assert_unredirected(lock)
        if not stat.S_ISREG(state.st_mode) or _identity(state) != _identity(current):
            raise GenerationPlanRecoveryRequired("The generation-plan lock path changed.")
        if state.st_size == 0:
            handle.write(b"0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise GenerationPlanStaleError("Another generation-plan save or recovery is active; retry shortly.") from exc
        try:
            _assert_unredirected(target)
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def save_plan(path: str | Path, plan: GenerationPlan, expected_digest: str | None = None) -> str:
    """Save deterministic bytes with optimistic staleness and recovery evidence."""
    if not isinstance(plan, GenerationPlan):
        raise GenerationPlanValidationError("Only a validated GenerationPlan can be saved.")
    target = Path(path).absolute()
    with _plan_writer(target):
        pending = _pending_path(target)
        if _read_plan_bytes(pending, MAX_RECOVERY_RECEIPT_BYTES) is not None:
            raise GenerationPlanRecoveryRequired("A generation-plan save was interrupted; recover it before saving again.")
        before = _read_plan_bytes(target)
        current_digest = _byte_digest(before)
        after = export_plan(plan)
        if expected_digest is not None and expected_digest != current_digest:
            raise GenerationPlanStaleError("The generation plan changed after it was loaded; reload and retry.")
        if before == after:
            return current_digest
        if before is not None and expected_digest is None:
            raise GenerationPlanStaleError(
                "Replacing an existing generation plan requires its exact loaded digest token."
            )
        operation = uuid4().hex
        recovery_root = target.parent / "generation-plan-recovery"
        recovery = recovery_root / operation
        _assert_unredirected(recovery_root)
        _assert_unredirected(recovery)
        recovery.mkdir(parents=True, exist_ok=False)
        _assert_unredirected(recovery)
        before_path = recovery / "before.json"
        after_path = recovery / "after.json"
        _guarded_write(before_path, before or b"", label="Generation plan backup")
        _guarded_write(after_path, after, label="Generation plan proposed state")
        receipt = {
            "schema_version": 1,
            "operation_id": operation,
            "target_name": target.name,
            "before_absent": before is None,
            "before_sha256": current_digest,
            "after_sha256": _byte_digest(after),
        }
        _guarded_json(recovery / "receipt.json", receipt, label="Generation plan recovery receipt")
        _guarded_json(pending, receipt, label="Generation plan pending receipt")
        try:
            if _byte_digest(_read_plan_bytes(target)) != current_digest:
                raise GenerationPlanStaleError("The generation plan changed during publication; recovery is required.")
            _guarded_write(target, after, label="Generation plan")
            restored = _plan_from_bytes(_read_plan_bytes(target) or b"")
            if restored.digest != plan.digest:
                raise GenerationPlanRecoveryRequired("The saved generation plan failed structured read-back verification.")
            _guarded_json(recovery / "result.json", {"operation_id": operation, "outcome": "committed"}, label="Generation plan recovery result")
            if _decode(_read_plan_bytes(pending, MAX_RECOVERY_RECEIPT_BYTES) or b"") != receipt:
                raise GenerationPlanRecoveryRequired("The pending generation-plan receipt changed.")
            _assert_unredirected(pending)
            pending.unlink()
        except BaseException as exc:
            try:
                current = _read_plan_bytes(target)
                expected_before = before
                if current == after:
                    if before is None:
                        _assert_unredirected(target)
                        if _read_plan_bytes(target) != after:
                            raise GenerationPlanRecoveryRequired("The generation plan changed before rollback.")
                        target.unlink()
                        _assert_unredirected(target.parent)
                    else:
                        _guarded_write(target, before, label="Generation plan rollback")
                    current = _read_plan_bytes(target)
                if current != expected_before:
                    raise GenerationPlanRecoveryRequired(
                        "Generation-plan content differs from both transaction states; it was preserved for review."
                    )
                _guarded_json(recovery / "result.json", {"operation_id": operation, "outcome": "rolled_back"}, label="Generation plan recovery result")
                if _decode(_read_plan_bytes(pending, MAX_RECOVERY_RECEIPT_BYTES) or b"") != receipt:
                    raise GenerationPlanRecoveryRequired("The pending generation-plan receipt changed.")
                _assert_unredirected(pending)
                pending.unlink()
            except BaseException as recovery_exc:
                raise GenerationPlanRecoveryRequired(
                    "Generation-plan publication was interrupted and automatic rollback could not be verified."
                ) from recovery_exc
            raise exc
        return _byte_digest(after)


def recover_plan(path: str | Path) -> str:
    """Reconcile an interrupted save without overwriting unrelated bytes."""
    target = Path(path).absolute()
    with _plan_writer(target):
        pending = _pending_path(target)
        raw_receipt = _read_plan_bytes(pending, MAX_RECOVERY_RECEIPT_BYTES)
        if raw_receipt is None:
            return "nothing_pending"
        receipt = _validate_recovery_receipt(_decode(raw_receipt), target)
        operation = receipt["operation_id"]
        recovery = target.parent / "generation-plan-recovery" / operation
        _assert_unredirected(recovery)
        saved_receipt = _read_plan_bytes(recovery / "receipt.json", MAX_RECOVERY_RECEIPT_BYTES)
        if saved_receipt is None or _decode(saved_receipt) != receipt:
            raise GenerationPlanRecoveryRequired("Generation-plan recovery evidence changed or is missing.")
        before = _read_plan_bytes(recovery / "before.json")
        after = _read_plan_bytes(recovery / "after.json")
        if before is None or after is None:
            raise GenerationPlanRecoveryRequired("Generation-plan recovery states are missing.")
        expected_before = None if receipt.get("before_absent") else before
        if _byte_digest(expected_before) != receipt.get("before_sha256") or _byte_digest(after) != receipt.get("after_sha256"):
            raise GenerationPlanRecoveryRequired("Generation-plan recovery states failed verification.")
        _plan_from_bytes(after)
        current = _read_plan_bytes(target)
        if current == after:
            _plan_from_bytes(after)
            outcome = "committed"
        elif current == expected_before:
            outcome = "unchanged"
        else:
            raise GenerationPlanRecoveryRequired("The plan differs from both recovery states and was preserved for review.")
        _guarded_json(recovery / "result.json", {"operation_id": operation, "outcome": outcome}, label="Generation plan recovery result")
        if _decode(_read_plan_bytes(pending, MAX_RECOVERY_RECEIPT_BYTES) or b"") != receipt:
            raise GenerationPlanRecoveryRequired("The pending generation-plan receipt changed.")
        _assert_unredirected(pending)
        pending.unlink()
        return outcome


def load_plan_with_digest(path: str | Path) -> tuple[GenerationPlan, str]:
    """Return a validated plan and save token from the same stable file bytes."""
    target = Path(path).absolute()
    if _read_plan_bytes(_pending_path(target), MAX_RECOVERY_RECEIPT_BYTES) is not None:
        raise GenerationPlanRecoveryRequired("A generation-plan save was interrupted; recover it before loading.")
    raw = _read_plan_bytes(target)
    if raw is None:
        raise FileNotFoundError(target)
    digest = _byte_digest(raw)
    return _plan_from_bytes(raw, storage_digest=digest), digest


def load_plan(path: str | Path) -> GenerationPlan:
    return load_plan_with_digest(path)[0]


def _issue(
    issues: list[ReadinessIssue],
    code: str,
    path: str,
    message: str,
    *,
    consequence: str = "Execution is blocked until this issue is resolved.",
    recovery: str = "Review the request and refresh readiness.",
    details: dict[str, Any] | None = None,
) -> None:
    issues.append(ReadinessIssue(code, path, message, consequence, recovery, details or {}))


def _capability_values(capabilities: Mapping[str, Any], *names: str) -> set[str] | None:
    for name in names:
        if name in capabilities:
            value = capabilities[name]
            if not isinstance(value, (list, tuple, set)) or any(not isinstance(item, str) for item in value):
                return None
            return {item.casefold() for item in value}
    return None


def _capability_version(capabilities: Mapping[str, Any]) -> str:
    return str(capabilities.get("adapter_version") or capabilities.get("version") or "").strip()


def _resource_hash(resource: GenerationResource) -> str:
    value = resource.hashes.get("sha256", "")
    value = str(value or "").strip().casefold()
    if value.startswith("sha256:"):
        value = value[7:]
    return value if re.fullmatch(r"[0-9a-f]{64}", value) else ""


def _input_result(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return _json_value(dict(value))
    if hasattr(value, "to_dict") and callable(value.to_dict):
        result = value.to_dict()
        return _json_value(result) if isinstance(result, dict) else {"available": False}
    return {"available": bool(value), "verified": False}


def _same_source(expected: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    ignored = {"content_safety", "checked_at", "status", "message"}
    expected_identity = {key: value for key, value in expected.items() if key not in ignored}
    current_identity = {key: value for key, value in current.items() if key not in ignored}
    return _canonical_bytes(expected_identity) == _canonical_bytes(current_identity)


def assess_readiness(
    plan: GenerationPlan,
    target_format: str,
    *,
    registry: Any = None,
    capabilities: dict[str, Any] | None = None,
    source_current: dict[str, Any] | None = None,
    input_resolver: Callable[[str], Any] | None = None,
) -> GenerationReadiness:
    """Resolve one frozen request against a probed adapter and exact resources."""
    if not isinstance(plan, GenerationPlan):
        raise GenerationPlanValidationError("Readiness requires a validated GenerationPlan.")
    issues: list[ReadinessIssue] = []
    request = plan.request
    target = _TARGET_ALIASES.get(str(target_format or "").strip().casefold(), str(target_format or "").strip().casefold())
    try:
        translation = translate_generation_record(request, target_format)
    except ValueError as exc:
        translation = None
        _issue(issues, "unsupported_target", "target_format", str(exc), recovery="Choose a supported target or keep the neutral export.")

    safety = plan.safety_policy
    if safety.get("state") != "allowed" or safety.get("reviewed") is not True:
        _issue(
            issues,
            "content_safety_blocked",
            "safety_policy",
            "Content safety has not been explicitly reviewed and allowed.",
            recovery="Review the content-safety state before execution.",
        )

    source_identity = plan.source_identity
    if plan.source is not None:
        if not source_identity:
            _issue(issues, "source_identity_missing", "source_identity", "The source image has no frozen identity fingerprint.", recovery="Refresh the source image identity and create a new plan revision.")
        elif source_identity.get("protected"):
            _issue(issues, "protected_source", "source_identity", "The source image is protected from this generation workflow.", recovery="Use an explicitly permitted source or export the neutral request.")
        elif source_current is None:
            _issue(issues, "source_unverified", "source_identity", "The current source image identity was not verified.", recovery="Resolve the frozen source identity before execution.")
        elif not isinstance(source_current, Mapping) or not _same_source(source_identity, source_current):
            _issue(issues, "stale_source", "source_identity", "The source image identity changed after planning.", recovery="Reload the source and review a new plan.")

    cap: Mapping[str, Any] | None = capabilities if isinstance(capabilities, Mapping) else None
    if cap is None:
        _issue(
            issues,
            "adapter_capabilities_missing",
            "capabilities",
            "No live adapter capability probe is available.",
            consequence="Execution and exact replay are blocked; neutral export remains available.",
            recovery="Connect and probe a supported local generator, or export the request manually.",
        )
    else:
        for name, value in (
            ("adapter_id", cap.get("adapter_id")),
            ("adapter_version", _capability_version(cap)),
            ("fingerprint", cap.get("fingerprint")),
        ):
            if not isinstance(value, str) or not value.strip():
                _issue(issues, "adapter_identity_missing", f"capabilities.{name}", f"The live adapter probe did not provide {name}.", recovery="Probe the adapter again and retain its exact identity.")
        if cap.get("healthy") is not True:
            _issue(issues, "adapter_unhealthy", "capabilities.healthy", "The selected adapter did not report a healthy live connection.", recovery="Recover or reconnect the adapter and probe it again.")
        cap_target = str(cap.get("target_format") or cap.get("format") or "").strip().casefold()
        cap_target = _TARGET_ALIASES.get(cap_target, cap_target)
        if cap_target and cap_target != target:
            _issue(issues, "adapter_target_mismatch", "capabilities.target_format", "The live adapter probe belongs to a different target format.", recovery="Select the matching probed adapter.")
        context = plan.target_context
        live_context = {
            "adapter_id": cap.get("adapter_id"),
            "adapter_version": _capability_version(cap),
            "fingerprint": cap.get("fingerprint"),
        }
        for key in ("adapter_id", "adapter_version", "fingerprint"):
            if context.get(key) and context.get(key) != live_context.get(key):
                _issue(issues, "stale_adapter", f"target_context.{key}", f"The saved {key} no longer matches the live adapter probe.", recovery="Review the changed adapter and save a new target context.")
        if (
            plan.intent == "reproduce"
            and request.generator_version
            and target == _TARGET_ALIASES.get(request.format.casefold(), request.format.casefold())
            and _capability_version(cap) != request.generator_version
        ):
            _issue(issues, "adapter_version_mismatch", "capabilities.adapter_version", "Exact replay requires the recorded generator version.", recovery="Use the recorded version or continue as a reviewed variation.")

    if translation is not None:
        for item in translation.report:
            if item.status in {"approximated", "untranslated", "unsupported"}:
                _issue(
                    issues,
                    "lossy_translation",
                    item.source_path,
                    item.detail or f"The target translation is {item.status}.",
                    consequence="Continuing may change the generated result.",
                    recovery="Choose a compatible target, revise the setting, or use the neutral export.",
                    details={"status": item.status},
                )
        for item in request.adapter_report:
            if item.status in {"approximated", "untranslated", "unsupported"}:
                _issue(issues, "source_loss", item.source_path, item.detail or f"The source record contains {item.status} data.", consequence="Exact source behavior cannot be reconstructed from the neutral request.", recovery="Retain the raw reference and use a compatible source adapter when available.", details={"status": item.status})

    supported_roles = _capability_values(cap, "supported_roles", "supported_resource_roles", "resource_roles", "resources") if cap is not None else None
    families = _capability_values(cap, "supported_families", "base_families", "families") if cap is not None else None
    versions = cap.get("resource_versions", {}) if cap is not None else {}
    if not isinstance(versions, Mapping):
        versions = {}
        _issue(issues, "adapter_capability_invalid", "capabilities.resource_versions", "The adapter resource-version capability is invalid.", recovery="Probe the adapter again.")
    for index, resource in enumerate(request.resources):
        path = f"request.resources[{index}]"
        digest = _resource_hash(resource)
        if not digest:
            _issue(issues, "resource_hash_missing", path, f"{resource.role} resource {resource.name!r} lacks a full SHA-256 identity.", recovery="Resolve or register the exact resource bytes.")
            continue
        if resource.provenance.get("protected"):
            _issue(issues, "protected_resource", path, f"Resource {resource.name!r} is protected from execution.", recovery="Use an explicitly permitted resource or keep the neutral export.")
        role = resource.role.casefold()
        if cap is not None and (supported_roles is None or role not in supported_roles):
            _issue(issues, "resource_role_unsupported", path, f"The live adapter does not declare support for {resource.role} resources.", recovery="Choose a compatible adapter or replace the resource.")
        resource_families = {str(item).casefold() for item in resource.compatibility if str(item).strip()}
        if len(resource_families) > 1 and not families:
            _issue(issues, "resource_family_ambiguous", path, f"Resource {resource.name!r} has multiple possible base families and the adapter did not resolve one.", recovery="Choose and verify the intended base family.")
        elif resource_families and cap is not None and (families is None or not resource_families.intersection(families)):
            _issue(issues, "resource_family_mismatch", path, f"Resource {resource.name!r} is not verified for the adapter's declared family.", recovery="Select a compatible resource and target family.")
        resource_id = f"sha256:{digest}"
        if resource.version_id:
            declared_version = versions.get(resource.id, versions.get(resource_id, versions.get(resource.name)))
            if declared_version != resource.version_id:
                _issue(issues, "resource_version_mismatch", path, f"Resource {resource.name!r} version {resource.version_id!r} was not verified by the adapter.", recovery="Resolve the exact recorded version or revise the request.")
        if registry is None:
            _issue(issues, "resource_registry_missing", path, f"Resource {resource.name!r} was not checked against the local registry.", recovery="Resolve the full hash through the bounded model resource registry.")
            continue
        family = next(iter(resource_families)) if len(resource_families) == 1 else ""
        registry_role = _RESOURCE_ROLE_ALIASES.get(role, role)
        resolution = registry.resolve(resource_id, role=registry_role, family=family)
        status = str(getattr(resolution, "status", "unresolved"))
        if status != "identity_verified":
            code = "resource_ambiguous" if status == "ambiguous" else "resource_unavailable"
            reason = str(getattr(resolution, "reason", "")) or f"Registry resolution returned {status}."
            _issue(issues, code, path, reason, recovery="Register or select the exact local resource bytes and refresh readiness.", details={"status": status, "sha256": digest})

    if request.model and not any(item.role == "checkpoint" for item in request.resources):
        flat_hash = str(request.model_hash or "").casefold().removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", flat_hash):
            _issue(issues, "resource_hash_missing", "request.model_hash", "The compatibility model reference lacks a full SHA-256 identity.", recovery="Add the exact checkpoint resource identity.")
        elif registry is None:
            _issue(issues, "resource_registry_missing", "request.model_hash", "The checkpoint was not checked against the local registry.", recovery="Resolve the full hash through the bounded model resource registry.")
        else:
            resolution = registry.resolve("sha256:" + flat_hash, role="checkpoint")
            status = str(getattr(resolution, "status", "unresolved"))
            if status != "identity_verified":
                code = "resource_ambiguous" if status == "ambiguous" else "resource_unavailable"
                _issue(issues, code, "request.model_hash", str(getattr(resolution, "reason", "")) or f"Registry resolution returned {status}.", recovery="Register or select the exact local checkpoint bytes and refresh readiness.", details={"status": status, "sha256": flat_hash})

    supported_stages = _capability_values(cap, "supported_stages", "stage_kinds", "stages") if cap is not None else None
    supported_inputs = _capability_values(cap, "supported_inputs", "input_kinds", "inputs") if cap is not None else None
    supported_settings = _capability_values(cap, "supported_settings", "settings") if cap is not None else None
    uses_adapter_parameter_names = False
    if cap is not None and supported_settings is None and isinstance(cap.get("parameters"), Mapping):
        supported_settings = {str(item).casefold() for item in cap["parameters"]}
        uses_adapter_parameter_names = True
    if cap is not None and supported_settings is not None:
        for setting in _SCALAR_SETTINGS:
            target_setting = (
                _PARAMETER_ALIASES.get(setting, setting).casefold()
                if uses_adapter_parameter_names else setting.casefold()
            )
            if getattr(request, setting) not in (None, "") and target_setting not in supported_settings:
                _issue(issues, "setting_unsupported", f"request.{setting}", f"The live adapter does not support {setting}.", recovery="Revise the setting or choose a compatible adapter.")
    for stage_index, stage in enumerate(request.stages):
        stage_path = f"request.stages[{stage_index}]"
        if cap is not None and (supported_stages is None or stage.kind.casefold() not in supported_stages):
            _issue(issues, "stage_unsupported", stage_path, f"The live adapter does not declare support for the {stage.kind} stage.", recovery="Choose a compatible adapter or revise the workflow.")
        for input_index, item in enumerate(stage.inputs):
            input_path = f"{stage_path}.inputs[{input_index}]"
            if cap is not None and (supported_inputs is None or item.kind.casefold() not in supported_inputs):
                _issue(issues, "input_unsupported", input_path, f"The live adapter does not declare support for {item.kind} inputs.", recovery="Choose a compatible adapter or remove the input deliberately.")
            if not item.reference:
                _issue(issues, "input_reference_missing", input_path, "A generation input lacks a durable reference.", recovery="Select and fingerprint the exact input.")
                continue
            expected = item.extras.get("sha256", item.extras.get("fingerprint", ""))
            if item.kind == "mask" and not expected and isinstance(item.extras.get("mask_payload"), Mapping):
                expected = item.extras["mask_payload"].get("mask_sha256", "")
            if not expected:
                _issue(issues, "input_fingerprint_missing", input_path, "The generation input lacks an expected fingerprint.", recovery="Fingerprint the exact input and create a new plan revision.")
            if input_resolver is None:
                _issue(issues, "input_resolver_missing", input_path, "The generation input was not resolved on the target system.", recovery="Resolve the durable input reference before execution.")
                continue
            resolved = _input_result(input_resolver(item.reference))
            if resolved.get("protected"):
                _issue(issues, "protected_input", input_path, "The generation input is protected from this workflow.", recovery="Use an explicitly permitted input or keep the neutral export.")
            if not resolved.get("available"):
                _issue(issues, "input_unavailable", input_path, "The generation input is unavailable on the target system.", recovery="Restore or replace the exact input.")
            elif resolved.get("verified") is not True:
                _issue(issues, "input_unverified", input_path, "The generation input identity was not verified.", recovery="Verify the input bytes before execution.")
            actual = resolved.get("sha256", resolved.get("fingerprint", ""))
            if expected and actual != expected:
                _issue(issues, "stale_input", input_path, "The generation input changed after planning.", recovery="Reload the input and review a new plan.")

    exact_request = plan.source is not None and _canonical_bytes(plan.source.to_dict()) == _canonical_bytes(request.to_dict())
    exact = plan.intent == "reproduce" and exact_request and not issues
    ready = translation is not None and not issues
    return GenerationReadiness(ready, exact, tuple(issues), translation, True)
