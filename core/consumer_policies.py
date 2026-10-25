from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from typing import Any, Iterable, Mapping
from uuid import uuid4

from .knowledge_relationships import CURRENT_RELATIONSHIP_KINDS
from .tag_collection import normalize_tag


CONSUMER_POLICY_SCHEMA_VERSION = 1
CONSUMER_POLICY_VERSION = 1

SEARCH_ORGANIZATION = "search_organization"
IMAGE_IDENTIFICATION = "image_identification"
MAXIMAL_IMAGE_KNOWLEDGE = "maximal_image_knowledge"
TRAINING_PROJECTION = "training_projection"
GENERATION_PROMPT_PLANNING = "generation_prompt_planning"
IMPORT_COMPATIBILITY = "import_compatibility"

CONSUMER_IDS = (
    SEARCH_ORGANIZATION,
    IMAGE_IDENTIFICATION,
    MAXIMAL_IMAGE_KNOWLEDGE,
    TRAINING_PROJECTION,
    GENERATION_PROMPT_PLANNING,
    IMPORT_COMPATIBILITY,
)

CANONICALIZE_IDENTITY = "canonicalize_identity"
DISCOVER_RELATED = "discover_related"
ORGANIZE_ONLY = "organize_only"
SUGGEST_FOR_REVIEW = "suggest_for_review"
OPT_IN_FACT_EXPANSION = "opt_in_fact_expansion"
RETAIN_WHEN_CONFIRMED = "retain_when_confirmed"
EXPLAIN_COMPOSITION_ONLY = "explain_composition_only"
ENFORCE_CONSTRAINT = "enforce_constraint"
CONDITIONAL_PROJECTION = "conditional_projection"
CONDITIONAL_PROMPT = "conditional_prompt"
COMPATIBILITY_RESOLUTION = "compatibility_resolution"
EXCLUDE_FROM_CONSUMER = "exclude_from_consumer"
MANUAL_CONTEXT_ONLY = "manual_context_only"
CLASSIFY_POLICY = "classify_policy"
PRESERVE_RELATIONSHIP = "preserve_relationship"

CONSUMER_POLICY_MODES = frozenset(
    {
        CANONICALIZE_IDENTITY,
        DISCOVER_RELATED,
        ORGANIZE_ONLY,
        SUGGEST_FOR_REVIEW,
        OPT_IN_FACT_EXPANSION,
        RETAIN_WHEN_CONFIRMED,
        EXPLAIN_COMPOSITION_ONLY,
        ENFORCE_CONSTRAINT,
        CONDITIONAL_PROJECTION,
        CONDITIONAL_PROMPT,
        COMPATIBILITY_RESOLUTION,
        EXCLUDE_FROM_CONSUMER,
        MANUAL_CONTEXT_ONLY,
        CLASSIFY_POLICY,
        PRESERVE_RELATIONSHIP,
    }
)

CONSUMER_DEFAULT = "consumer_default"
RELATIONSHIP_KIND = "relationship_kind"
EXACT_RELATIONSHIP = "relationship"
EXACT_TAG = "tag"
COMPATIBILITY_TERM = "compatibility_term"
SELECTOR_TYPES = frozenset(
    {
        CONSUMER_DEFAULT,
        RELATIONSHIP_KIND,
        EXACT_RELATIONSHIP,
        EXACT_TAG,
        COMPATIBILITY_TERM,
    }
)

LAYER_RANK = {"default": 0, "pack": 1, "user": 2}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fingerprint(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest() if path.exists() else "missing"


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def consumer_policy_record_id(
    selector_type: str,
    selector_id: str,
    consumer: str,
    conditions: Mapping[str, Any] | None = None,
) -> str:
    readable = "".join(
        value if value.isalnum() or value in "._-" else "-"
        for value in selector_id.casefold()
    ).strip("-")[:48]
    identity = f"{selector_type}\x1f{selector_id}\x1f{consumer}"
    if conditions:
        identity += "\x1f" + json.dumps(
            dict(conditions),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    digest = sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"consumer-policy.{selector_type}.{readable or 'value'}.{consumer}.{digest}"


@dataclass(frozen=True)
class ConsumerPolicyRecord:
    id: str
    selector_type: str
    selector_id: str
    consumer: str
    mode: str
    explanation: str
    version: int = CONSUMER_POLICY_VERSION
    layer: str = "user"
    reviewed: bool = True
    disabled: bool = False
    source: str = ""
    reviewed_at: str = ""
    conditions: tuple[tuple[str, Any], ...] = ()
    extras: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def policy_key(self) -> tuple[str, str, str, str]:
        encoded = json.dumps(
            dict(self.conditions), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return self.selector_type, self.selector_id, self.consumer, encoded

    @property
    def condition_specificity(self) -> int:
        return len(self.conditions)

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.extras)
        payload.update(
            {
                "id": self.id,
                "version": self.version,
                "selector": {"type": self.selector_type, "id": self.selector_id},
                "consumer": self.consumer,
                "mode": self.mode,
                "explanation": self.explanation,
                "layer": self.layer,
                "reviewed": self.reviewed,
                "disabled": self.disabled,
            }
        )
        if self.source:
            payload["source"] = self.source
        else:
            payload.pop("source", None)
        if self.reviewed_at:
            payload["reviewed_at"] = self.reviewed_at
        else:
            payload.pop("reviewed_at", None)
        if self.conditions:
            payload["conditions"] = dict(self.conditions)
        else:
            payload.pop("conditions", None)
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ConsumerPolicyRecord":
        selector = value.get("selector")
        if not isinstance(selector, Mapping):
            selector = {}
        conditions = value.get("conditions")
        if not isinstance(conditions, Mapping):
            conditions = {}
        known = {
            "id",
            "version",
            "selector",
            "consumer",
            "mode",
            "explanation",
            "layer",
            "reviewed",
            "disabled",
            "source",
            "reviewed_at",
            "conditions",
        }
        return cls(
            id=str(value.get("id") or ""),
            version=max(1, int(value.get("version", 1) or 1)),
            selector_type=str(selector.get("type") or ""),
            selector_id=str(selector.get("id") or ""),
            consumer=str(value.get("consumer") or ""),
            mode=str(value.get("mode") or ""),
            explanation=str(value.get("explanation") or ""),
            layer=str(value.get("layer") or "user"),
            reviewed=bool(value.get("reviewed", False)),
            disabled=bool(value.get("disabled", False)),
            source=str(value.get("source") or ""),
            reviewed_at=str(value.get("reviewed_at") or ""),
            conditions=tuple(sorted((str(key), item) for key, item in conditions.items())),
            extras={key: item for key, item in value.items() if key not in known},
        )

    def validation_errors(self) -> tuple[str, ...]:
        errors = []
        if not self.id:
            errors.append("Policy ID is missing.")
        if self.selector_type not in SELECTOR_TYPES:
            errors.append(f"Policy {self.id!r} has unknown selector type {self.selector_type!r}.")
        if not self.selector_id:
            errors.append(f"Policy {self.id!r} has no selector ID.")
        if self.consumer not in CONSUMER_IDS:
            errors.append(f"Policy {self.id!r} has unknown consumer {self.consumer!r}.")
        if self.mode not in CONSUMER_POLICY_MODES:
            errors.append(f"Policy {self.id!r} has unknown mode {self.mode!r}.")
        if self.layer not in LAYER_RANK:
            errors.append(f"Policy {self.id!r} has unknown layer {self.layer!r}.")
        if not self.explanation.strip():
            errors.append(f"Policy {self.id!r} has no explanation.")
        if self.selector_type == CONSUMER_DEFAULT and self.selector_id != self.consumer:
            errors.append(f"Policy {self.id!r} has a mismatched consumer-default selector.")
        if self.selector_type == RELATIONSHIP_KIND and self.selector_id not in CURRENT_RELATIONSHIP_KINDS:
            errors.append(
                f"Policy {self.id!r} names unknown relationship kind {self.selector_id!r}."
            )
        return tuple(errors)


class ConsumerPolicyStore:
    """One forward-compatible consumer-policy layer with verified writes."""

    SCHEMA_VERSION = CONSUMER_POLICY_SCHEMA_VERSION

    def __init__(self, path: str | Path, *, expected_layer: str | None = None) -> None:
        self.path = Path(path)
        self.expected_layer = str(expected_layer or "")
        self.schema_version = self.SCHEMA_VERSION
        self.policy_version = CONSUMER_POLICY_VERSION
        self.layer = self.expected_layer or "user"
        self.records: tuple[ConsumerPolicyRecord, ...] = ()
        self.extras: dict[str, Any] = {}
        self._loaded_bytes: bytes | None = None
        self.load()

    @property
    def runtime_supported(self) -> bool:
        return self.schema_version <= self.SCHEMA_VERSION

    def load(self) -> None:
        raw = self.path.read_bytes() if self.path.exists() else None
        payload = json.loads(raw.decode("utf-8")) if raw is not None else {}
        if not isinstance(payload, dict):
            raise ValueError("The consumer-policy store must contain one JSON object.")
        version = int(payload.get("schema_version", self.SCHEMA_VERSION) or 0)
        if version < 1:
            raise ValueError("The consumer-policy schema version is invalid.")
        self.schema_version = version
        self.policy_version = max(
            CONSUMER_POLICY_VERSION, int(payload.get("policy_version", 1) or 1)
        )
        self.layer = str(payload.get("layer") or self.expected_layer or "user")
        if self.layer not in LAYER_RANK:
            raise ValueError(f"The consumer-policy layer {self.layer!r} is unknown.")
        if self.expected_layer and self.layer != self.expected_layer:
            raise ValueError(
                f"Expected a {self.expected_layer!r} consumer-policy layer, found {self.layer!r}."
            )
        extras = {
            key: item
            for key, item in payload.items()
            if key not in {"schema_version", "policy_version", "layer", "policies"}
        }
        records = []
        seen_ids = set()
        seen_keys = set()
        for item in payload.get("policies", ()):
            if not isinstance(item, dict):
                raise ValueError("Every consumer-policy record must be an object.")
            record = ConsumerPolicyRecord.from_dict(item)
            if self.runtime_supported and record.layer != self.layer:
                raise ValueError(
                    f"Consumer-policy record {record.id!r} belongs to layer "
                    f"{record.layer!r}, not store layer {self.layer!r}."
                )
            if record.id in seen_ids:
                raise ValueError(f"Consumer-policy ID {record.id!r} is duplicated.")
            semantic_key = (*record.policy_key, record.layer)
            if semantic_key in seen_keys:
                raise ValueError(
                    "The consumer-policy store contains two records for the same "
                    f"subject, consumer, conditions, and layer: {record.policy_key!r}."
                )
            seen_ids.add(record.id)
            seen_keys.add(semantic_key)
            records.append(record)
        self.records = tuple(records)
        self.extras = extras
        self._loaded_bytes = raw

    def payload(self) -> dict[str, Any]:
        payload = dict(self.extras)
        payload.update(
            {
                "schema_version": self.schema_version,
                "policy_version": self.policy_version,
                "layer": self.layer,
                "policies": [
                    record.to_dict()
                    for record in sorted(self.records, key=lambda item: item.id)
                ],
            }
        )
        return payload

    def replace(self, records: Iterable[ConsumerPolicyRecord]) -> None:
        values = tuple(records)
        ids = [record.id for record in values]
        if len(ids) != len(set(ids)):
            raise ValueError("Consumer-policy IDs must be unique within one layer.")
        semantic_keys = [record.policy_key for record in values]
        if len(semantic_keys) != len(set(semantic_keys)):
            raise ValueError(
                "Consumer-policy semantic keys must be unique within one layer."
            )
        if self.runtime_supported and any(record.layer != self.layer for record in values):
            raise ValueError("Every consumer-policy record must match its store layer.")
        self.records = values

    def save_verified(self, *, backup_root: str | Path | None = None) -> Path:
        current = self.path.read_bytes() if self.path.exists() else None
        if current != self._loaded_bytes:
            raise RuntimeError(
                "Consumer policies changed after preview. Reload and review before saving."
            )
        root = (
            Path(backup_root)
            if backup_root is not None
            else self.path.parent / "backups" / "consumer-policy"
        )
        backup = root / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
        backup.mkdir(parents=True, exist_ok=False)
        backup_name = f"0_{self.path.name}"
        if current is not None:
            shutil.copy2(self.path, backup / backup_name)
        manifest_path = backup / "recovery.json"
        manifest = {
            "schema_version": 1,
            "status": "pending",
            "created_at": _utc_now(),
            "files": [
                {
                    "target": str(self.path.resolve()),
                    "backup": backup_name,
                    "existed": current is not None,
                }
            ],
        }
        _atomic_json(manifest_path, manifest)
        try:
            _atomic_json(self.path, self.payload())
            saved = ConsumerPolicyStore(self.path, expected_layer=self.layer)
            if saved.payload() != self.payload():
                raise RuntimeError("Consumer-policy save failed semantic read-back verification.")
            manifest["status"] = "complete"
            manifest["completed_at"] = _utc_now()
            _atomic_json(manifest_path, manifest)
            self._loaded_bytes = self.path.read_bytes()
            return manifest_path
        except BaseException:
            if current is None:
                self.path.unlink(missing_ok=True)
            else:
                temporary = self.path.with_suffix(self.path.suffix + ".restore")
                temporary.write_bytes(current)
                os.replace(temporary, self.path)
            manifest["status"] = "restored"
            manifest["restored_at"] = _utc_now()
            _atomic_json(manifest_path, manifest)
            self._loaded_bytes = current
            raise


@dataclass(frozen=True)
class ConsumerPolicyTraceStep:
    precedence: str
    selector_type: str
    selector_id: str
    status: str
    detail: str
    record_id: str = ""
    mode: str = ""

    @property
    def label(self) -> str:
        subject = f"{self.selector_type} {self.selector_id!r}"
        authority = f" [{self.record_id}]" if self.record_id else ""
        return f"{self.precedence}: {subject}{authority} — {self.detail}"


@dataclass(frozen=True)
class ConsumerPolicyDecision:
    consumer: str
    mode: str
    record_id: str
    selector_type: str
    selector_id: str
    layer: str
    explanation: str
    trace: tuple[ConsumerPolicyTraceStep, ...]
    failed_closed: bool = False

    @property
    def explanation_path(self) -> tuple[str, ...]:
        return tuple(step.label for step in self.trace)

    @property
    def permits_related_review(self) -> bool:
        return self.mode in {SUGGEST_FOR_REVIEW, OPT_IN_FACT_EXPANSION}

    @property
    def permits_fact_expansion(self) -> bool:
        return self.mode == OPT_IN_FACT_EXPANSION


class ConsumerPolicyResolver:
    """Resolve exact policy, Tag exception, kind default, then closed default."""

    def __init__(self, stores: Iterable[ConsumerPolicyStore]) -> None:
        self.stores = tuple(stores)
        self.records = tuple(record for store in self.stores for record in store.records)
        self._by_subject: dict[
            tuple[str, str, str], tuple[ConsumerPolicyRecord, ...]
        ] = {}
        indexed: dict[tuple[str, str, str], list[ConsumerPolicyRecord]] = {}
        for record in self.records:
            indexed.setdefault(
                (record.selector_type, record.selector_id, record.consumer), []
            ).append(record)
        self._by_subject = {key: tuple(values) for key, values in indexed.items()}
        unsupported = [
            store.path.name
            for store in self.stores
            if not store.runtime_supported
        ]
        self.authority_error = (
            "A newer consumer-policy schema is installed: " + ", ".join(unsupported)
            if unsupported
            else ""
        )

    @classmethod
    def from_paths(
        cls,
        default_path: str | Path,
        user_path: str | Path | None = None,
    ) -> "ConsumerPolicyResolver":
        stores = [ConsumerPolicyStore(default_path, expected_layer="default")]
        if user_path is not None:
            stores.append(ConsumerPolicyStore(user_path, expected_layer="user"))
        return cls(stores)

    @staticmethod
    def _conditions_match(
        record: ConsumerPolicyRecord,
        context: Mapping[str, Any],
    ) -> bool:
        for key, expected in record.conditions:
            actual = context.get(key)
            if isinstance(expected, list):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False
        return True

    def _select(
        self,
        selector_type: str,
        selector_id: str,
        consumer: str,
        context: Mapping[str, Any],
    ) -> tuple[ConsumerPolicyRecord | None, str]:
        candidates = [
            record
            for record in self._by_subject.get(
                (selector_type, selector_id, consumer), ()
            )
            if self._conditions_match(record, context)
        ]
        if not candidates:
            return None, ""
        candidates.sort(
            key=lambda record: (
                LAYER_RANK.get(record.layer, -1),
                record.condition_specificity,
                record.version,
            ),
            reverse=True,
        )
        chosen = candidates[0]
        chosen_rank = (
            LAYER_RANK.get(chosen.layer, -1),
            chosen.condition_specificity,
            chosen.version,
        )
        peers = [
            record
            for record in candidates
            if (
                LAYER_RANK.get(record.layer, -1),
                record.condition_specificity,
                record.version,
            )
            == chosen_rank
        ]
        if len({record.mode for record in peers}) > 1:
            return None, "Equally authoritative policy records disagree."
        return chosen, ""

    @staticmethod
    def _closed(
        consumer: str,
        trace: list[ConsumerPolicyTraceStep],
        reason: str,
        *,
        selector_type: str = CONSUMER_DEFAULT,
        selector_id: str = "",
        record_id: str = "",
        layer: str = "",
    ) -> ConsumerPolicyDecision:
        return ConsumerPolicyDecision(
            consumer=consumer,
            mode=EXCLUDE_FROM_CONSUMER,
            record_id=record_id,
            selector_type=selector_type,
            selector_id=selector_id,
            layer=layer,
            explanation=reason,
            trace=tuple(trace),
            failed_closed=True,
        )

    def resolve(
        self,
        consumer: str,
        *,
        relationship_id: str = "",
        tag_id: str = "",
        relationship_kind: str = "",
        compatibility_term: str = "",
        context: Mapping[str, Any] | None = None,
    ) -> ConsumerPolicyDecision:
        context = dict(context or {})
        trace: list[ConsumerPolicyTraceStep] = []
        if consumer not in CONSUMER_IDS:
            return self._closed(consumer, trace, f"Unknown consumer {consumer!r}.")
        if self.authority_error:
            trace.append(
                ConsumerPolicyTraceStep(
                    "authority",
                    CONSUMER_DEFAULT,
                    consumer,
                    "closed",
                    self.authority_error,
                )
            )
            return self._closed(consumer, trace, self.authority_error)

        levels = []
        if relationship_id:
            levels.append(("exact relationship policy", EXACT_RELATIONSHIP, relationship_id))
        if tag_id:
            levels.append(("exact Tag usage policy", EXACT_TAG, tag_id))
        if compatibility_term:
            levels.append(
                (
                    "exact compatibility-term policy",
                    COMPATIBILITY_TERM,
                    normalize_tag(compatibility_term),
                )
            )
        if relationship_kind:
            levels.append(
                ("relationship-kind default", RELATIONSHIP_KIND, relationship_kind)
            )
        levels.append(("consumer default", CONSUMER_DEFAULT, consumer))

        for precedence, selector_type, selector_id in levels:
            record, conflict = self._select(
                selector_type, selector_id, consumer, context
            )
            if conflict:
                trace.append(
                    ConsumerPolicyTraceStep(
                        precedence,
                        selector_type,
                        selector_id,
                        "closed",
                        conflict,
                    )
                )
                return self._closed(
                    consumer,
                    trace,
                    conflict,
                    selector_type=selector_type,
                    selector_id=selector_id,
                )
            if record is None:
                trace.append(
                    ConsumerPolicyTraceStep(
                        precedence,
                        selector_type,
                        selector_id,
                        "not_found",
                        "No matching reviewed record.",
                    )
                )
                continue
            errors = record.validation_errors()
            if errors or not record.reviewed or record.disabled:
                reason = (
                    " ".join(errors)
                    if errors
                    else "The matching policy is unreviewed."
                    if not record.reviewed
                    else "The matching policy is disabled."
                )
                trace.append(
                    ConsumerPolicyTraceStep(
                        precedence,
                        selector_type,
                        selector_id,
                        "closed",
                        reason,
                        record.id,
                        EXCLUDE_FROM_CONSUMER,
                    )
                )
                return self._closed(
                    consumer,
                    trace,
                    reason,
                    selector_type=selector_type,
                    selector_id=selector_id,
                    record_id=record.id,
                    layer=record.layer,
                )
            trace.append(
                ConsumerPolicyTraceStep(
                    precedence,
                    selector_type,
                    selector_id,
                    "selected",
                    record.explanation,
                    record.id,
                    record.mode,
                )
            )
            return ConsumerPolicyDecision(
                consumer=consumer,
                mode=record.mode,
                record_id=record.id,
                selector_type=record.selector_type,
                selector_id=record.selector_id,
                layer=record.layer,
                explanation=record.explanation,
                trace=tuple(trace),
            )
        return self._closed(
            consumer,
            trace,
            "No consumer default was available; applicability fails closed.",
        )


def _default_record(
    selector_type: str,
    selector_id: str,
    consumer: str,
    mode: str,
    explanation: str,
    *,
    source: str,
    reviewed_at: str,
) -> ConsumerPolicyRecord:
    return ConsumerPolicyRecord(
        id=consumer_policy_record_id(selector_type, selector_id, consumer),
        selector_type=selector_type,
        selector_id=selector_id,
        consumer=consumer,
        mode=mode,
        explanation=explanation,
        layer="default",
        reviewed=True,
        disabled=False,
        source=source,
        reviewed_at=reviewed_at,
    )


def compile_reviewed_consumer_policy_defaults(
    matrix: Mapping[str, Any],
    compatibility_subjects: Iterable[Mapping[str, str]],
) -> dict[str, Any]:
    """Compile the accepted KM-05 matrix into deterministic KM-06 authority."""

    consumers = matrix.get("consumers")
    rows = matrix.get("relationship_defaults")
    cases = matrix.get("representative_cases")
    tag_usage_exceptions = matrix.get("tag_usage_exceptions", [])
    relationship_usage_exceptions = matrix.get(
        "relationship_usage_exceptions", []
    )
    if not isinstance(consumers, Mapping) or set(consumers) != set(CONSUMER_IDS):
        raise ValueError("The accepted matrix does not cover every KM-06 consumer.")
    if not isinstance(rows, list):
        raise ValueError("The accepted matrix has no relationship defaults.")
    if not isinstance(tag_usage_exceptions, list):
        raise ValueError("The accepted matrix Tag usage exceptions must be a list.")
    if not isinstance(relationship_usage_exceptions, list):
        raise ValueError(
            "The accepted matrix relationship usage exceptions must be a list."
        )
    indexed = {
        str(row.get("kind") or ""): row
        for row in rows
        if isinstance(row, Mapping)
    }
    if set(indexed) != set(CURRENT_RELATIONSHIP_KINDS):
        raise ValueError("The accepted matrix does not cover every current relationship kind.")
    ambiguous = next(
        (
            item
            for item in cases or ()
            if isinstance(item, Mapping)
            and item.get("case_id") == "ambiguous-compatibility-term"
        ),
        None,
    )
    if not isinstance(ambiguous, Mapping) or not isinstance(ambiguous.get("expected"), Mapping):
        raise ValueError("The accepted matrix lacks the compatibility-term decision.")

    reviewed_at = "2026-08-19"
    source = str(matrix.get("policy_version") or "KM-05 consumer-applicability matrix")
    records: list[ConsumerPolicyRecord] = []
    for consumer in CONSUMER_IDS:
        records.append(
            _default_record(
                CONSUMER_DEFAULT,
                consumer,
                consumer,
                EXCLUDE_FROM_CONSUMER,
                "No more specific reviewed policy applies; this consumer fails closed.",
                source=source,
                reviewed_at=reviewed_at,
            )
        )
    for kind in sorted(indexed):
        row = indexed[kind]
        applicability = row.get("applicability")
        if not isinstance(applicability, Mapping):
            raise ValueError(f"Relationship kind {kind!r} has no applicability decisions.")
        owner = str(row.get("policy_owner") or "reviewed relationship policy")
        for consumer in CONSUMER_IDS:
            decision = applicability.get(consumer)
            mode = str(decision.get("mode") or "") if isinstance(decision, Mapping) else ""
            if mode not in CONSUMER_POLICY_MODES:
                raise ValueError(
                    f"Relationship kind {kind!r} has unknown mode {mode!r} for {consumer!r}."
                )
            records.append(
                _default_record(
                    RELATIONSHIP_KIND,
                    kind,
                    consumer,
                    mode,
                    f"Reviewed {kind.replace('_', ' ')} default owned by {owner}.",
                    source=source,
                    reviewed_at=reviewed_at,
                )
            )
    exact_policy_keys: set[tuple[str, str, str]] = set()
    seen_relationship_exceptions: set[str] = set()
    for item in relationship_usage_exceptions:
        if not isinstance(item, Mapping):
            raise ValueError("Every relationship usage exception must be an object.")
        relationship_id = str(item.get("relationship_id") or "").strip()
        if not relationship_id or relationship_id in seen_relationship_exceptions:
            raise ValueError(
                "Relationship usage exceptions must name unique exact relationship IDs."
            )
        seen_relationship_exceptions.add(relationship_id)
        applicability = item.get("applicability")
        if not isinstance(applicability, Mapping) or not applicability:
            raise ValueError(
                f"Relationship usage exception {relationship_id!r} has no "
                "applicability decisions."
            )
        unknown_consumers = set(applicability) - set(CONSUMER_IDS)
        if unknown_consumers:
            raise ValueError(
                f"Relationship usage exception {relationship_id!r} names unknown "
                "consumers: " + ", ".join(sorted(unknown_consumers))
            )
        explanation = str(item.get("explanation") or "").strip()
        if not explanation:
            raise ValueError(
                f"Relationship usage exception {relationship_id!r} has no reviewed "
                "explanation."
            )
        item_source = str(item.get("source") or source)
        item_reviewed_at = str(item.get("reviewed_at") or reviewed_at)
        for consumer, decision in applicability.items():
            mode = str(decision.get("mode") or "") if isinstance(decision, Mapping) else ""
            if mode not in CONSUMER_POLICY_MODES:
                raise ValueError(
                    f"Relationship usage exception {relationship_id!r} has unknown "
                    f"mode {mode!r} for {consumer!r}."
                )
            key = (EXACT_RELATIONSHIP, relationship_id, str(consumer))
            if key in exact_policy_keys:
                raise ValueError("Exact relationship consumer policies must be unique.")
            exact_policy_keys.add(key)
            records.append(
                _default_record(
                    EXACT_RELATIONSHIP,
                    relationship_id,
                    str(consumer),
                    mode,
                    explanation,
                    source=item_source,
                    reviewed_at=item_reviewed_at,
                )
            )
    seen_tag_exceptions: set[str] = set()
    for item in tag_usage_exceptions:
        if not isinstance(item, Mapping):
            raise ValueError("Every Tag usage exception must be an object.")
        tag_id = str(item.get("tag_id") or "").strip()
        if not tag_id or tag_id in seen_tag_exceptions:
            raise ValueError("Tag usage exceptions must name unique exact Tag IDs.")
        seen_tag_exceptions.add(tag_id)
        applicability = item.get("applicability")
        if not isinstance(applicability, Mapping) or not applicability:
            raise ValueError(
                f"Tag usage exception {tag_id!r} has no applicability decisions."
            )
        unknown_consumers = set(applicability) - set(CONSUMER_IDS)
        if unknown_consumers:
            raise ValueError(
                f"Tag usage exception {tag_id!r} names unknown consumers: "
                + ", ".join(sorted(unknown_consumers))
            )
        explanation = str(item.get("explanation") or "").strip()
        if not explanation:
            raise ValueError(
                f"Tag usage exception {tag_id!r} has no reviewed explanation."
            )
        item_source = str(item.get("source") or source)
        item_reviewed_at = str(item.get("reviewed_at") or reviewed_at)
        for consumer, decision in applicability.items():
            mode = str(decision.get("mode") or "") if isinstance(decision, Mapping) else ""
            if mode not in CONSUMER_POLICY_MODES:
                raise ValueError(
                    f"Tag usage exception {tag_id!r} has unknown mode {mode!r} "
                    f"for {consumer!r}."
                )
            key = (EXACT_TAG, tag_id, str(consumer))
            if key in exact_policy_keys:
                raise ValueError("Exact Tag consumer policies must be unique.")
            exact_policy_keys.add(key)
            records.append(
                _default_record(
                    EXACT_TAG,
                    tag_id,
                    str(consumer),
                    mode,
                    explanation,
                    source=item_source,
                    reviewed_at=item_reviewed_at,
                )
            )
    compatibility_modes = ambiguous["expected"]
    seen_subjects = set()
    for item in sorted(
        compatibility_subjects,
        key=lambda value: normalize_tag(str(value.get("term") or "")),
    ):
        term = normalize_tag(str(item.get("term") or ""))
        tag_id = str(item.get("tag_id") or "")
        selector_type = EXACT_TAG if tag_id else COMPATIBILITY_TERM
        selector_id = tag_id or term
        if not selector_id or (selector_type, selector_id) in seen_subjects:
            raise ValueError("Compatibility subjects must be exact and unique.")
        seen_subjects.add((selector_type, selector_id))
        for consumer in CONSUMER_IDS:
            mode = str(compatibility_modes.get(consumer) or "")
            key = (selector_type, selector_id, consumer)
            if key in exact_policy_keys:
                raise ValueError("Exact consumer policies must be unique.")
            exact_policy_keys.add(key)
            records.append(
                _default_record(
                    selector_type,
                    selector_id,
                    consumer,
                    mode,
                    (
                        f"{term!r} is a reviewed ambiguous historical term; retain exact "
                        "compatibility lookup without promoting it into unsupported use."
                    ),
                    source="KM-04 identity reconciliation / KM-05 consumer audit",
                    reviewed_at=reviewed_at,
                )
            )
    expected_subjects = matrix.get("compatibility_subject_count", 16)
    if not isinstance(expected_subjects, int) or isinstance(expected_subjects, bool) or expected_subjects < 0:
        raise ValueError("Compatibility subject count must be a nonnegative integer.")
    if len(seen_subjects) != expected_subjects:
        raise ValueError(
            f"Expected {expected_subjects} compatibility-only subjects, found {len(seen_subjects)}."
        )
    return {
        "schema_version": CONSUMER_POLICY_SCHEMA_VERSION,
        "policy_version": CONSUMER_POLICY_VERSION,
        "layer": "default",
        "roadmap_id": "KM-06",
        "source_policy_version": source,
        "policies": [
            record.to_dict() for record in sorted(records, key=lambda item: item.id)
        ],
    }


@dataclass(frozen=True)
class ConsumerPolicyMigrationPlan:
    payload: dict[str, Any]
    source_fingerprints: tuple[tuple[str, str], ...]
    target_path: Path
    default_count: int
    compatibility_subject_count: int

    @property
    def plan_fingerprint(self) -> str:
        encoded = json.dumps(
            {
                "payload": self.payload,
                "sources": self.source_fingerprints,
                "target": str(self.target_path.resolve()),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(encoded).hexdigest()


def build_consumer_policy_migration_plan(
    *,
    matrix_path: str | Path,
    identity_review_path: str | Path,
    defaults_dir: str | Path,
    target_path: str | Path,
    user_path: str | Path | None = None,
) -> ConsumerPolicyMigrationPlan:
    from .default_sync import load_effective_tag_defaults
    from .reviewed_tag_semantics import load_reviewed_tag_identity_resolutions

    matrix_path = Path(matrix_path)
    identity_review_path = Path(identity_review_path)
    defaults_dir = Path(defaults_dir)
    target_path = Path(target_path)
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    resolutions = load_reviewed_tag_identity_resolutions(identity_review_path)
    effective = load_effective_tag_defaults(defaults_dir)
    effective_by_id = {
        str(record.get("id") or ""): record
        for record in effective.tags
        if str(record.get("id") or "")
    }
    relationships_path = defaults_dir / "relationships.json"
    relationships_payload = json.loads(
        relationships_path.read_text(encoding="utf-8")
    )
    effective_relationships_by_id = {
        str(record.get("id") or ""): record
        for record in relationships_payload.get("relationships", ())
        if isinstance(record, Mapping) and str(record.get("id") or "")
    }
    for item in matrix.get("relationship_usage_exceptions", ()):
        if not isinstance(item, Mapping):
            continue
        relationship_id = str(item.get("relationship_id") or "").strip()
        record = effective_relationships_by_id.get(relationship_id)
        if record is None:
            raise ValueError(
                f"Relationship usage exception {relationship_id!r} does not resolve "
                "to an effective relationship."
            )
        expected_kind = str(item.get("relationship_kind") or "").strip()
        actual_kind = str(record.get("kind") or "").strip()
        if expected_kind and expected_kind != actual_kind:
            raise ValueError(
                f"Relationship usage exception {relationship_id!r} no longer matches "
                "its reviewed kind."
            )
        expected_endpoints = tuple(
            str(endpoint_id or "").strip()
            for endpoint_id in item.get("endpoint_ids", ())
        )
        actual_endpoints = tuple(
            str(endpoint.get("id") or "").strip()
            for endpoint in record.get("endpoints", ())
            if isinstance(endpoint, Mapping)
        )
        if expected_endpoints and expected_endpoints != actual_endpoints:
            raise ValueError(
                f"Relationship usage exception {relationship_id!r} no longer matches "
                "its reviewed endpoints."
            )
    for item in matrix.get("tag_usage_exceptions", ()):
        if not isinstance(item, Mapping):
            continue
        tag_id = str(item.get("tag_id") or "").strip()
        record = effective_by_id.get(tag_id)
        if record is None:
            raise ValueError(
                f"Tag usage exception {tag_id!r} does not resolve to an effective Tag."
            )
        expected_display = normalize_tag(str(item.get("display") or ""))
        actual_display = normalize_tag(str(record.get("display") or ""))
        if expected_display and expected_display != actual_display:
            raise ValueError(
                f"Tag usage exception {tag_id!r} no longer matches its reviewed display."
            )
    by_name = {
        normalize_tag(str(record.get("display") or "")): str(record.get("id") or "")
        for record in effective.tags
    }
    subjects = [
        {
            "term": str(item.get("compatibility_tag") or ""),
            "tag_id": by_name.get(
                normalize_tag(str(item.get("compatibility_tag") or "")), ""
            ),
        }
        for item in resolutions
        if item.get("decision") == "compatibility_only"
    ]
    payload = compile_reviewed_consumer_policy_defaults(matrix, subjects)
    source_paths = [matrix_path, identity_review_path]
    source_paths.extend(
        defaults_dir / name
        for name in ("foundation_tags.json", "community_tags.json", "promoted_tags.json")
    )
    source_paths.append(relationships_path)
    source_paths.append(target_path)
    if user_path is not None:
        source_paths.append(Path(user_path))
    return ConsumerPolicyMigrationPlan(
        payload=payload,
        source_fingerprints=tuple(
            (str(path.resolve()), _fingerprint(path)) for path in source_paths
        ),
        target_path=target_path,
        default_count=len(CURRENT_RELATIONSHIP_KINDS) * len(CONSUMER_IDS),
        compatibility_subject_count=len(subjects),
    )


def apply_consumer_policy_migration(
    plan: ConsumerPolicyMigrationPlan,
    *,
    expected_plan_fingerprint: str,
    backup_root: str | Path | None = None,
) -> Path:
    if expected_plan_fingerprint != plan.plan_fingerprint:
        raise RuntimeError("The consumer-policy migration does not match the reviewed preview.")
    for path_text, expected in plan.source_fingerprints:
        if _fingerprint(Path(path_text)) != expected:
            raise RuntimeError(
                "A consumer-policy migration source changed after preview. Rebuild the plan."
            )
    store = ConsumerPolicyStore(plan.target_path, expected_layer="default")
    store.schema_version = CONSUMER_POLICY_SCHEMA_VERSION
    store.policy_version = CONSUMER_POLICY_VERSION
    store.extras = {
        key: item
        for key, item in plan.payload.items()
        if key not in {"schema_version", "policy_version", "layer", "policies"}
    }
    store.replace(
        ConsumerPolicyRecord.from_dict(item)
        for item in plan.payload.get("policies", ())
        if isinstance(item, Mapping)
    )
    return store.save_verified(backup_root=backup_root)


def recover_interrupted_consumer_policy_writes(
    backup_root: str | Path,
    *,
    allowed_targets: Iterable[str | Path] = (),
) -> tuple[Path, ...]:
    root = Path(backup_root).resolve()
    if not root.is_dir():
        return ()
    allowed = {Path(path).resolve() for path in allowed_targets}
    recovered = []
    for manifest_path in sorted(root.glob("*/recovery.json")):
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if payload.get("status") != "pending":
            continue
        for item in payload.get("files", ()):
            target = Path(str(item.get("target") or "")).resolve()
            backup = (manifest_path.parent / str(item.get("backup") or "")).resolve()
            if allowed and target not in allowed:
                raise RuntimeError("Consumer-policy recovery target is outside the allowlist.")
            if backup.parent != manifest_path.parent.resolve():
                raise RuntimeError("Consumer-policy recovery backup path is unsafe.")
            if bool(item.get("existed")):
                if not backup.is_file():
                    raise RuntimeError("Consumer-policy recovery backup is missing.")
                temporary = target.with_suffix(target.suffix + ".recover")
                temporary.parent.mkdir(parents=True, exist_ok=True)
                temporary.write_bytes(backup.read_bytes())
                os.replace(temporary, target)
            else:
                target.unlink(missing_ok=True)
        payload["status"] = "recovered_after_interruption"
        payload["recovered_at"] = _utc_now()
        _atomic_json(manifest_path, payload)
        recovered.append(manifest_path)
    return tuple(recovered)
