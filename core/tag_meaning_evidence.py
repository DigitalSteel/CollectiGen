from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .review_queue import ReviewTask, ReviewTaskDatabase
from .tag_collection import normalize_tag
from .tag_database import TagDatabase, TagRecord


TAG_MEANING_EVIDENCE_SCHEMA_VERSION = 1
TAG_MEANING_TASK_TYPE = "tag_meaning_proposal"
TAG_MEANING_SOURCE_KINDS = frozenset({"imported_attributed", "cached_src_01"})
TAG_MEANING_FRESHNESS = frozenset({"current", "stale", "unavailable"})
MAX_MEANING_OBSERVATIONS = 10_000
MAX_MEANING_VARIANTS_PER_TAG = 20
MAX_MEANING_SOURCES_PER_VARIANT = 50
MAX_MEANING_EVIDENCE_BYTES = 16 * 1024 * 1024

_PLACEHOLDER_MEANINGS = frozenset({
    "community-confirmed reusable vocabulary.",
    "community confirmed reusable vocabulary.",
    "recorded source.",
    "reviewed meaning.",
    "reviewed tag.",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fingerprint(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _bytes_fingerprint(value: bytes | None) -> str:
    return hashlib.sha256(value or b"").hexdigest()


def _summary_fingerprint(value: str) -> str:
    return _fingerprint(" ".join(str(value or "").split()).casefold())


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _meaning_quality_error(definition: str, guidance: str) -> str:
    summary = _clean_text(f"{definition} {guidance}")
    if len(definition) < 20:
        return "definition_too_short"
    if len(definition) > 500 or len(guidance) > 240 or len(summary) > 600:
        return "meaning_too_long"
    if "[[" in definition or "{{" in definition or "\n" in definition:
        return "copied_markup"
    if _clean_text(definition).casefold() in _PLACEHOLDER_MEANINGS:
        return "generic_boilerplate"
    return ""


@dataclass(frozen=True)
class TagMeaningEvidenceIssue:
    observation_id: str
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class TagMeaningObservation:
    id: str
    tag_id: str
    tag_name: str
    definition: str
    community_guidance: str
    source_kind: str
    source_label: str
    source_url: str
    source_version: str
    source_page_id: str
    retrieved_at: str
    freshness: str
    confidence: float = 0.0
    extras: dict[str, Any] = field(default_factory=dict, compare=False)

    @classmethod
    def from_dict(cls, value: Any, *, index: int) -> "TagMeaningObservation":
        if not isinstance(value, dict):
            raise ValueError("Observation must be a JSON object.")
        known = {
            "id", "tag_id", "tag_name", "definition", "community_guidance",
            "source_kind", "source_label", "source_url", "source_version",
            "source_page_id", "retrieved_at", "freshness", "confidence",
        }
        observation_id = str(value.get("id") or "").strip()
        tag_id = str(value.get("tag_id") or "").strip()
        tag_name = _clean_text(value.get("tag_name"))
        raw_definition = str(value.get("definition") or "")
        raw_guidance = str(value.get("community_guidance") or "")
        if "\n" in raw_definition or "\r" in raw_definition:
            raise ValueError("copied_markup")
        definition = _clean_text(raw_definition)
        guidance = _clean_text(raw_guidance)
        source_kind = str(value.get("source_kind") or "").strip()
        source_label = _clean_text(value.get("source_label"))
        source_url = str(value.get("source_url") or "").strip()
        source_version = _clean_text(value.get("source_version"))
        source_page_id = _clean_text(value.get("source_page_id"))
        retrieved_at = str(value.get("retrieved_at") or "").strip()
        freshness = str(value.get("freshness") or "").strip()
        try:
            confidence = max(0.0, min(1.0, float(value.get("confidence") or 0)))
        except (TypeError, ValueError):
            confidence = 0.0
        if not observation_id:
            raise ValueError("Observation ID is required.")
        if not tag_id and not tag_name:
            raise ValueError("A stable Tag ID or exact Tag name is required.")
        if source_kind not in TAG_MEANING_SOURCE_KINDS:
            raise ValueError("Source kind is not supported by this local evidence inbox.")
        if not source_label or not (source_url or source_version or source_page_id):
            raise ValueError("Source label plus URL, version, or page identity is required.")
        if not retrieved_at:
            raise ValueError("Evidence retrieval time is required.")
        if freshness not in TAG_MEANING_FRESHNESS:
            raise ValueError("Freshness must be current, stale, or unavailable.")
        quality_error = _meaning_quality_error(definition, guidance)
        if quality_error:
            raise ValueError(quality_error)
        return cls(
            observation_id, tag_id, tag_name, definition, guidance, source_kind,
            source_label, source_url, source_version, source_page_id,
            retrieved_at, freshness, confidence,
            {key: value[key] for key in value if key not in known},
        )

    @property
    def summary(self) -> str:
        return _clean_text(f"{self.definition} {self.community_guidance}")

    def attribution(self) -> dict[str, Any]:
        result = {
            "observation_id": self.id,
            "source_kind": self.source_kind,
            "source_label": self.source_label,
            "source_url": self.source_url,
            "source_version": self.source_version,
            "source_page_id": self.source_page_id,
            "retrieved_at": self.retrieved_at,
            "freshness": self.freshness,
            "confidence": self.confidence,
        }
        return {key: value for key, value in result.items() if value != ""}

    def fingerprint(self) -> str:
        return _fingerprint({
            "id": self.id,
            "tag_id": self.tag_id,
            "tag_name": self.tag_name,
            "definition": self.definition,
            "community_guidance": self.community_guidance,
            **self.attribution(),
            "extras": self.extras,
        })


class TagMeaningEvidenceStore:
    """Read one bounded, attributed local evidence inbox without provider access."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.observations: tuple[TagMeaningObservation, ...] = ()
        self.issues: tuple[TagMeaningEvidenceIssue, ...] = ()
        self._loaded_bytes: bytes | None = None
        self.load()

    @property
    def fingerprint(self) -> str:
        return _bytes_fingerprint(self._loaded_bytes)

    def load(self) -> None:
        if self.path.exists() and self.path.stat().st_size > MAX_MEANING_EVIDENCE_BYTES:
            raise ValueError("Attributed Meaning & Usage evidence exceeds the 16 MiB local inbox limit.")
        raw = self.path.read_bytes() if self.path.exists() else None
        self._loaded_bytes = raw
        if raw is None:
            self.observations = ()
            self.issues = ()
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise ValueError("Attributed Meaning & Usage evidence is not valid UTF-8 JSON.") from exc
        if not isinstance(payload, dict):
            raise ValueError("Attributed Meaning & Usage evidence must use an object envelope.")
        if int(payload.get("schema_version") or 0) != TAG_MEANING_EVIDENCE_SCHEMA_VERSION:
            raise ValueError("Attributed Meaning & Usage evidence uses an unsupported schema version.")
        values = payload.get("observations")
        if not isinstance(values, list):
            raise ValueError("Attributed Meaning & Usage evidence requires an observations list.")
        observations = []
        issues = []
        seen_ids = set()
        for index, value in enumerate(values):
            observation_id = (
                str(value.get("id") or f"row_{index}").strip()
                if isinstance(value, dict) else f"row_{index}"
            )
            try:
                observation = TagMeaningObservation.from_dict(value, index=index)
            except ValueError as exc:
                issues.append(TagMeaningEvidenceIssue(observation_id, "invalid_observation", str(exc)))
                continue
            if observation.id in seen_ids:
                issues.append(TagMeaningEvidenceIssue(observation.id, "duplicate_observation_id"))
                continue
            seen_ids.add(observation.id)
            observations.append(observation)
        self.observations = tuple(observations)
        self.issues = tuple(issues)


@dataclass(frozen=True)
class TagMeaningProposal:
    tag_id: str
    tag_display: str
    current_summary: str
    variants: tuple[dict[str, Any], ...]
    source_key: str
    evidence_fingerprint: str

    @property
    def confidence(self) -> float:
        return max((float(item.get("confidence") or 0) for item in self.variants), default=0.0)

    def task_details(self, report: "TagMeaningDiscoveryReport") -> dict[str, Any]:
        return {
            "tag_id": self.tag_id,
            "tag": self.tag_display,
            "current_summary": self.current_summary,
            "current_summary_fingerprint": _summary_fingerprint(self.current_summary),
            "variants": [dict(item) for item in self.variants],
            "evidence_fingerprint": self.evidence_fingerprint,
            "evidence_store_fingerprint": report.evidence_store_fingerprint,
            "knowledge_fingerprint": report.knowledge_fingerprint,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class TagMeaningDiscoveryReport:
    candidates: tuple[TagMeaningProposal, ...]
    issues: tuple[TagMeaningEvidenceIssue, ...]
    evidence_store_fingerprint: str
    knowledge_fingerprint: str
    processed_observations: int
    bounded_observations: int

    def unrecorded(self, reviews: ReviewTaskDatabase) -> tuple[TagMeaningProposal, ...]:
        return tuple(
            candidate for candidate in self.candidates
            if reviews.find_by_source_key(candidate.source_key) is None
        )


@dataclass(frozen=True)
class TagMeaningQueueResult:
    queued: int
    updated: int
    skipped: int
    task_ids: tuple[str, ...]
    superseded: int = 0


@dataclass(frozen=True)
class TagMeaningDecisionResult:
    task_id: str
    decision: str
    tag_id: str
    variant_fingerprint: str
    related_pending_reranked: int = 0
    backup_directory: Path = Path()
    recovered: bool = False


class TagMeaningDiscoveryService:
    def __init__(self, knowledge: TagDatabase, evidence: TagMeaningEvidenceStore) -> None:
        self.knowledge = knowledge
        self.evidence = evidence

    def _resolve(self, observation: TagMeaningObservation) -> tuple[TagRecord | None, str]:
        if observation.tag_id:
            record = self.knowledge.tags.get(observation.tag_id)
            if record is None:
                return None, "missing_stable_tag"
            if observation.tag_name:
                names = {normalize_tag(value) for value in self.knowledge.identity_names(record)}
                if normalize_tag(observation.tag_name) not in names:
                    return None, "tag_identity_mismatch"
            return record, ""
        owners = self.knowledge.identity_owner_ids(observation.tag_name)
        if len(owners) != 1:
            return None, "ambiguous_tag_name" if owners else "missing_exact_tag"
        return self.knowledge.tags.get(owners[0]), ""

    @staticmethod
    def _variant(observations: Iterable[TagMeaningObservation]) -> dict[str, Any]:
        values = tuple(observations)
        first = values[0]
        freshness_order = {"current": 0, "stale": 1, "unavailable": 2}
        freshness = min(values, key=lambda item: freshness_order[item.freshness]).freshness
        observation_fingerprints = tuple(sorted(item.fingerprint() for item in values))
        variant_fingerprint = _fingerprint(observation_fingerprints)
        attributed = sorted(values, key=lambda item: item.id)
        visible_sources = attributed[:MAX_MEANING_SOURCES_PER_VARIANT]
        return {
            "fingerprint": variant_fingerprint,
            "summary": first.summary,
            "definition": first.definition,
            "community_guidance": first.community_guidance,
            "confidence": max(item.confidence for item in values),
            "freshness": freshness,
            "source_observation_count": len(attributed),
            "sources_omitted": max(0, len(attributed) - len(visible_sources)),
            "sources": [item.attribution() for item in visible_sources],
        }

    def discover(self) -> TagMeaningDiscoveryReport:
        observations = sorted(
            self.evidence.observations,
            key=lambda item: (item.tag_id.casefold(), normalize_tag(item.tag_name), item.id),
        )
        bounded = max(0, len(observations) - MAX_MEANING_OBSERVATIONS)
        observations = observations[:MAX_MEANING_OBSERVATIONS]
        issues = list(self.evidence.issues)
        grouped: dict[str, dict[str, Any]] = {}
        for observation in observations:
            record, reason = self._resolve(observation)
            if record is None:
                issues.append(TagMeaningEvidenceIssue(observation.id, reason))
                continue
            if _summary_fingerprint(observation.summary) == _summary_fingerprint(record.notes):
                issues.append(TagMeaningEvidenceIssue(observation.id, "already_reviewed"))
                continue
            entry = grouped.setdefault(record.id, {"record": record, "variants": {}})
            key = _summary_fingerprint(observation.summary)
            entry["variants"].setdefault(key, []).append(observation)
        candidates = []
        freshness_order = {"current": 0, "stale": 1, "unavailable": 2}
        for tag_id, entry in sorted(grouped.items()):
            variants = [self._variant(values) for values in entry["variants"].values()]
            variants.sort(key=lambda item: (
                freshness_order[str(item["freshness"])],
                -float(item["confidence"]),
                str(item["summary"]).casefold(),
                str(item["fingerprint"]),
            ))
            if len(variants) > MAX_MEANING_VARIANTS_PER_TAG:
                for item in variants[MAX_MEANING_VARIANTS_PER_TAG:]:
                    issues.append(TagMeaningEvidenceIssue(
                        str(item["fingerprint"]), "per_tag_variant_limit",
                        f"Only the first {MAX_MEANING_VARIANTS_PER_TAG} ranked variants are queued at once.",
                    ))
                variants = variants[:MAX_MEANING_VARIANTS_PER_TAG]
            evidence_fingerprint = _fingerprint(
                tuple(str(item["fingerprint"]) for item in variants)
            )
            record = entry["record"]
            candidates.append(TagMeaningProposal(
                tag_id, record.display, record.notes, tuple(variants),
                f"tag_meaning:{tag_id}:{evidence_fingerprint[:24]}",
                evidence_fingerprint,
            ))
        if bounded:
            issues.append(TagMeaningEvidenceIssue(
                "evidence_store", "observation_limit",
                f"{bounded:,} observations were left for a later bounded pass.",
            ))
        return TagMeaningDiscoveryReport(
            tuple(candidates), tuple(issues), self.evidence.fingerprint,
            _bytes_fingerprint(getattr(self.knowledge, "_loaded_bytes", None)),
            len(observations), bounded,
        )

    def queue(
        self,
        report: TagMeaningDiscoveryReport,
        reviews: ReviewTaskDatabase,
        *,
        selected_source_keys: Iterable[str] | None = None,
    ) -> TagMeaningQueueResult:
        self.evidence.load()
        if self.evidence.fingerprint != report.evidence_store_fingerprint:
            raise RuntimeError("Attributed Meaning & Usage evidence changed after this report. Run Find New Knowledge again.")
        current = self.knowledge.path.read_bytes() if self.knowledge.path.exists() else None
        if _bytes_fingerprint(current) != report.knowledge_fingerprint:
            raise RuntimeError("System Knowledge changed after this report. Run Find New Knowledge again.")
        selected = None if selected_source_keys is None else {str(value) for value in selected_source_keys}
        queued = updated = skipped = superseded = 0
        task_ids = []
        try:
            with reviews.transaction():
                for candidate in report.candidates:
                    if selected is not None and candidate.source_key not in selected:
                        continue
                    existing = reviews.find_by_source_key(candidate.source_key)
                    if existing is not None and existing.status in {"resolved", "ignored"}:
                        skipped += 1
                        continue
                    for previous in reviews.tasks.values():
                        if (
                            previous.status == "pending"
                            and previous.task_type == TAG_MEANING_TASK_TYPE
                            and str(previous.details.get("tag_id") or "") == candidate.tag_id
                            and previous.source_key != candidate.source_key
                        ):
                            previous.status = "resolved"
                            previous.details["resolution"] = {
                                "decision": "superseded_by_evidence_refresh",
                                "replacement_source_key": candidate.source_key,
                                "resolved_at": _now(),
                                "system_knowledge_effect": "none",
                            }
                            previous.updated_at = _now()
                            superseded += 1
                    before = existing.to_dict() if existing is not None else None
                    task = reviews.add_task(
                        task_type=TAG_MEANING_TASK_TYPE,
                        title=f"Review Meaning & Usage for {candidate.tag_display}",
                        description=(
                            "Attributed local or explicitly cached evidence proposed one or more "
                            "concise summaries. Conflicting text remains separate until review."
                        ),
                        details=candidate.task_details(report),
                        priority="normal",
                        source_key=candidate.source_key,
                    )
                    task_ids.append(task.id)
                    if before is None:
                        queued += 1
                    elif task.to_dict() != before:
                        updated += 1
                if queued or updated or superseded:
                    reviews.save()
        except BaseException:
            reviews.load()
            raise
        return TagMeaningQueueResult(
            queued, updated, skipped, tuple(task_ids), superseded,
        )


class TagMeaningReviewService:
    """Apply one attributed meaning decision through the verified Tag writer."""

    def __init__(
        self,
        knowledge: TagDatabase,
        reviews: ReviewTaskDatabase,
        evidence_path: str | Path,
        *,
        backup_root: str | Path | None = None,
    ) -> None:
        self.knowledge = knowledge
        self.reviews = reviews
        self.evidence_path = Path(evidence_path)
        self.backup_root = (
            Path(backup_root) if backup_root is not None
            else knowledge.path.parent / "backups" / "tag-meaning-review"
        )

    def _task(self, task_id: str) -> ReviewTask:
        task = self.reviews.tasks.get(str(task_id))
        if task is None or task.task_type != TAG_MEANING_TASK_TYPE:
            raise ValueError("The Meaning & Usage Review question is no longer available.")
        return task

    @staticmethod
    def _variants(task: ReviewTask) -> tuple[dict[str, Any], ...]:
        return tuple(
            dict(item) for item in task.details.get("variants", ())
            if isinstance(item, dict) and str(item.get("fingerprint") or "")
        )

    def _variant(self, task: ReviewTask, variant_fingerprint: str) -> dict[str, Any]:
        variants = self._variants(task)
        selected = next(
            (item for item in variants if str(item.get("fingerprint")) == str(variant_fingerprint)),
            None,
        )
        if selected is None:
            raise ValueError("Choose one current attributed Meaning & Usage proposal.")
        return selected

    def _validate_accept(self, task: ReviewTask, record: TagRecord) -> None:
        expected_store = str(task.details.get("evidence_store_fingerprint") or "")
        if self.evidence_path.exists() and self.evidence_path.stat().st_size > MAX_MEANING_EVIDENCE_BYTES:
            raise RuntimeError("Attributed evidence exceeds the 16 MiB local inbox limit. Reduce it and run Find New Knowledge again.")
        current_bytes = self.evidence_path.read_bytes() if self.evidence_path.exists() else None
        if _bytes_fingerprint(current_bytes) != expected_store:
            raise RuntimeError("Attributed evidence changed after this question was prepared. Run Find New Knowledge again.")
        expected_note = str(task.details.get("current_summary_fingerprint") or "")
        if _summary_fingerprint(record.notes) != expected_note:
            raise RuntimeError("Meaning & Usage changed after this question was prepared. Review the current Tag before applying evidence.")

    @staticmethod
    def _has_applied_provenance(record: TagRecord, fingerprint: str) -> bool:
        return any(
            isinstance(item, dict)
            and item.get("action") == "reviewed_meaning_usage"
            and str(item.get("evidence_fingerprint") or "") == fingerprint
            for item in record.provenance
        )

    def _rerank_related(self, tag_id: str, current_summary: str) -> int:
        related = [
            task for task in self.reviews.tasks.values()
            if task.status == "pending"
            and task.task_type == TAG_MEANING_TASK_TYPE
            and str(task.details.get("tag_id") or "") == str(tag_id)
        ]
        freshness_order = {"current": 0, "stale": 1, "unavailable": 2}

        def order(task: ReviewTask):
            variants = self._variants(task)
            best = min(
                variants,
                key=lambda item: (
                    freshness_order.get(str(item.get("freshness") or "unavailable"), 2),
                    -float(item.get("confidence") or 0),
                    str(item.get("summary") or "").casefold(),
                ),
                default={},
            )
            return (
                freshness_order.get(str(best.get("freshness") or "unavailable"), 2),
                -float(best.get("confidence") or 0), task.source_key, task.id,
            )

        related.sort(key=order)
        changed = 0
        current_fingerprint = _summary_fingerprint(current_summary)
        for rank, task in enumerate(related, 1):
            task_changed = False
            rerank = {"tag_id": tag_id, "rank": rank, "family_size": len(related)}
            if task.details.get("meaning_rerank") != rerank:
                task.details["meaning_rerank"] = rerank
                task_changed = True
            if task.details.get("current_summary") != current_summary:
                task.details["current_summary"] = current_summary
                task_changed = True
            if task.details.get("current_summary_fingerprint") != current_fingerprint:
                task.details["current_summary_fingerprint"] = current_fingerprint
                task_changed = True
            if task_changed:
                task.updated_at = _now()
                changed += 1
        return changed

    def recover_interrupted(self) -> tuple[TagMeaningDecisionResult, ...]:
        with self.reviews.transaction():
            if not any(
                task.task_type == TAG_MEANING_TASK_TYPE
                and task.status == "applying"
                for task in self.reviews.tasks.values()
            ):
                return ()
            self.knowledge.load()
            self.reviews.load()
            recovered = []
            changed = False
            for task in self.reviews.tasks.values():
                if task.task_type != TAG_MEANING_TASK_TYPE or task.status != "applying":
                    continue
                resolution = task.details.get("resolution")
                resolution = dict(resolution) if isinstance(resolution, dict) else {}
                fingerprint = str(resolution.get("variant_fingerprint") or "")
                record = self.knowledge.tags.get(str(task.details.get("tag_id") or ""))
                applied = bool(
                    record is not None and fingerprint
                    and self._has_applied_provenance(record, fingerprint)
                )
                task.status = "resolved" if applied else "pending"
                decision = "accepted" if applied else "recovered_for_review"
                resolution.update({"decision": decision, "recovered_at": _now()})
                task.details["resolution"] = resolution
                task.updated_at = _now()
                reranked = self._rerank_related(
                    str(task.details.get("tag_id") or ""),
                    record.notes if record is not None else "",
                )
                recovered.append(TagMeaningDecisionResult(
                    task.id, decision, str(task.details.get("tag_id") or ""),
                    fingerprint, reranked, recovered=True,
                ))
                changed = True
            if changed:
                try:
                    self.reviews.save()
                except BaseException:
                    self.reviews.load()
                    raise
            return tuple(recovered)

    def decide(
        self, task_id: str, decision: str, *, variant_fingerprint: str = "",
    ) -> TagMeaningDecisionResult:
        decision = str(decision)
        if decision not in {"accepted", "rejected"}:
            raise ValueError("Choose accepted or rejected for this Meaning & Usage proposal.")
        with self.reviews.transaction():
            self.knowledge.load()
            self.reviews.load()
            task = self._task(task_id)
            if task.status != "pending":
                raise RuntimeError("This Meaning & Usage question is no longer pending.")
            tag_id = str(task.details.get("tag_id") or "")
            record = self.knowledge.tags.get(tag_id)
            if record is None:
                raise RuntimeError("The exact SDXL Tag changed after discovery. Run Find New Knowledge again.")
            if decision == "rejected":
                task.status = "ignored"
                task.details["resolution"] = {
                    "decision": "rejected",
                    "evidence_fingerprint": str(task.details.get("evidence_fingerprint") or ""),
                    "decided_at": _now(),
                    "system_knowledge_effect": "none",
                }
                task.updated_at = _now()
                reranked = self._rerank_related(tag_id, record.notes)
                try:
                    self.reviews.save()
                except BaseException:
                    self.reviews.load()
                    raise
                return TagMeaningDecisionResult(task.id, decision, tag_id, "", reranked)

            variant = self._variant(task, variant_fingerprint)
            self._validate_accept(task, record)
            summary = _clean_text(variant.get("summary"))
            if not summary:
                raise ValueError("The selected Meaning & Usage proposal is empty.")
            resolution = {
                "decision": "accepted",
                "tag_id": tag_id,
                "variant_fingerprint": str(variant["fingerprint"]),
                "evidence_fingerprint": str(task.details.get("evidence_fingerprint") or ""),
                "decided_at": _now(),
                "image_knowledge_effect": "none",
            }
            task.status = "applying"
            task.details["resolution"] = resolution
            task.updated_at = _now()
            try:
                self.reviews.save()
            except BaseException:
                self.reviews.load()
                raise
            backup = Path()
            try:
                record.notes = summary
                provenance = {
                    "source": "reviewed_attributed_meaning",
                    "action": "reviewed_meaning_usage",
                    "review_status": "approved",
                    "reviewed_at": _now(),
                    "evidence_fingerprint": str(variant["fingerprint"]),
                    "summary_fingerprint": _summary_fingerprint(summary),
                    "evidence_sources": [
                        dict(item) for item in variant.get("sources", ())
                        if isinstance(item, dict)
                    ],
                    "evidence_source_count": int(
                        variant.get("source_observation_count")
                        or len(variant.get("sources", ()))
                    ),
                    "evidence_sources_omitted": int(
                        variant.get("sources_omitted") or 0
                    ),
                    "freshness": str(variant.get("freshness") or "unavailable"),
                }
                if provenance not in record.provenance:
                    record.provenance.append(provenance)
                backup = self.knowledge.save_verified(backup_root=self.backup_root)
                self.knowledge.load()
                saved = self.knowledge.tags.get(tag_id)
                if saved is None or saved.notes != summary or not self._has_applied_provenance(saved, str(variant["fingerprint"])):
                    raise RuntimeError("The reviewed Meaning & Usage change failed semantic read-back verification.")
                task.status = "resolved"
                task.updated_at = _now()
                reranked = self._rerank_related(tag_id, saved.notes)
                self.reviews.save()
            except BaseException:
                self.knowledge.load()
                saved = self.knowledge.tags.get(tag_id)
                if saved is None or not self._has_applied_provenance(saved, str(variant["fingerprint"])):
                    task.status = "pending"
                    task.updated_at = _now()
                    try:
                        self.reviews.save()
                    except Exception:
                        self.reviews.load()
                raise
            return TagMeaningDecisionResult(
                task.id, decision, tag_id, str(variant["fingerprint"]),
                reranked, backup,
            )


__all__ = [
    "MAX_MEANING_OBSERVATIONS",
    "MAX_MEANING_EVIDENCE_BYTES",
    "MAX_MEANING_SOURCES_PER_VARIANT",
    "MAX_MEANING_VARIANTS_PER_TAG",
    "TAG_MEANING_EVIDENCE_SCHEMA_VERSION",
    "TAG_MEANING_FRESHNESS",
    "TAG_MEANING_SOURCE_KINDS",
    "TAG_MEANING_TASK_TYPE",
    "TagMeaningDecisionResult",
    "TagMeaningDiscoveryReport",
    "TagMeaningDiscoveryService",
    "TagMeaningEvidenceIssue",
    "TagMeaningEvidenceStore",
    "TagMeaningObservation",
    "TagMeaningProposal",
    "TagMeaningQueueResult",
    "TagMeaningReviewService",
]
