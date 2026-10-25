"""Read-only duplicate-cache observations for frozen training candidates.

This module deliberately treats the duplicate cache as diagnostic evidence.  It
never starts a scan, reads source files, constructs a writable cache service,
or changes candidate decisions and split roles.
"""
from __future__ import annotations

import math
import re
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .duplicate_finder import (
    DUPLICATE_ALGORITHM_VERSION,
    DUPLICATE_INDEX_SCHEMA_VERSION,
    MAX_CANDIDATE_EVALUATIONS,
    MAX_VISUAL_INDEX_PROBES,
    DuplicateDecisionStore,
    DuplicateFingerprint,
    DuplicateIndex,
    find_duplicate_groups,
)


TRAINING_DUPLICATE_EVIDENCE_SCHEMA_VERSION = 1
LOWER_SIMILARITY = 88.0
UPPER_SIMILARITY = 100.0
MAX_RELATIONS = 5_000
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_KINDS = frozenset(("byte_exact", "pixel_exact", "similar"))
_MISSING_REASONS = frozenset((
    "source_evidence_unavailable",
    "scope_identity_unavailable",
    "cache_row_unavailable",
    "cache_row_unusable",
    "cache_row_algorithm_mismatch",
    "cache_row_content_mismatch",
    "scope_identity_collision",
    "cache_unavailable",
))
_DIAGNOSTICS = frozenset((
    "Duplicate cache evidence is unavailable; no cache scan was started.",
    "Duplicate cache evidence is partial; some selected images have no current cached fingerprint.",
    "Duplicate comparison reached a bounded limit; omitted relations were not assessed.",
))
_TRUNCATION_REASONS = frozenset((
    "",
    "relation_bound",
    "candidate_evaluation_bound",
    "visual_index_probe_bound",
))
_FEATURE_LABELS = frozenset((
    "full frame", "center 90% crop", "center 75% crop", "center 60% crop",
))
_BYTE_EVIDENCE = "The complete file bytes have the same SHA-256 digest."
_PIXEL_EVIDENCE = (
    "Decoded, orientation-normalized RGBA pixels are identical; file bytes or "
    "Metadata differ."
)
_SIMILAR_EVIDENCE = re.compile(
    r"^(?P<bits>\d{1,3})/256 perceptual bits differ "
    r"\((?:full frame|center (?:90|75|60)% crop) ↔ "
    r"(?:full frame|center (?:90|75|60)% crop)\)$"
)


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and bool(_DIGEST.fullmatch(value))


def _strict_int(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"Duplicate evidence {name} is invalid.")
    return value


def _strict_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Duplicate evidence {name} is invalid.")
    return value


@dataclass(frozen=True)
class TrainingDuplicateEvidenceRecord:
    stable_id: str
    byte_sha256: str
    pixel_sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "stable_id": self.stable_id,
            "byte_sha256": self.byte_sha256,
            "pixel_sha256": self.pixel_sha256,
        }


@dataclass(frozen=True)
class TrainingDuplicateEvidenceRelation:
    first_stable_id: str
    second_stable_id: str
    kind: str
    similarity: float
    evidence: str
    reviewed_distinct: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "first_stable_id": self.first_stable_id,
            "second_stable_id": self.second_stable_id,
            "kind": self.kind,
            "similarity": self.similarity,
            "evidence": self.evidence,
            "reviewed_distinct": self.reviewed_distinct,
        }


@dataclass(frozen=True)
class TrainingDuplicateEvidence:
    covered_images: tuple[TrainingDuplicateEvidenceRecord, ...] = ()
    relations: tuple[TrainingDuplicateEvidenceRelation, ...] = ()
    eligible_count: int = 0
    covered_count: int = 0
    missing_count: int = 0
    missing_reason_counts: tuple[tuple[str, int], ...] = ()
    decision_keys_available: bool = False
    algorithm_version: int = DUPLICATE_ALGORITHM_VERSION
    lower_similarity: float = LOWER_SIMILARITY
    upper_similarity: float = UPPER_SIMILARITY
    truncated: bool = False
    truncation_reason: str = ""
    candidate_evaluations: int = 0
    candidate_evaluation_budget: int = MAX_CANDIDATE_EVALUATIONS
    visual_index_probes: int = 0
    visual_index_probe_budget: int = MAX_VISUAL_INDEX_PROBES
    diagnostics: tuple[str, ...] = ()
    schema_version: int = TRAINING_DUPLICATE_EVIDENCE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "algorithm_version": self.algorithm_version,
            "similarity_range": {
                "lower": self.lower_similarity,
                "upper": self.upper_similarity,
            },
            "coverage": {
                "eligible_count": self.eligible_count,
                "covered_count": self.covered_count,
                "missing_count": self.missing_count,
                "complete": self.complete,
                "missing_reason_counts": [
                    {"reason": reason, "count": count}
                    for reason, count in self.missing_reason_counts
                ],
            },
            "decision_keys_available": self.decision_keys_available,
            "covered_images": [item.to_dict() for item in self.covered_images],
            "relations": [item.to_dict() for item in self.relations],
            "truncated": self.truncated,
            "truncation_reason": self.truncation_reason,
            "comparison_budget": {
                "max_relations": MAX_RELATIONS,
                "candidate_evaluations": self.candidate_evaluations,
                "candidate_evaluation_budget": self.candidate_evaluation_budget,
                "visual_index_probes": self.visual_index_probes,
                "visual_index_probe_budget": self.visual_index_probe_budget,
            },
            "diagnostics": list(self.diagnostics),
        }

    @property
    def complete(self) -> bool:
        return self.missing_count == 0 and not self.truncated

    @classmethod
    def from_dict(
        cls, value: object, *, source_hashes: Mapping[str, str],
    ) -> "TrainingDuplicateEvidence":
        if not isinstance(value, dict):
            raise ValueError("Duplicate evidence must be an object.")
        expected = {
            "schema_version", "algorithm_version", "similarity_range",
            "coverage", "decision_keys_available", "covered_images", "relations",
            "truncated", "truncation_reason", "comparison_budget", "diagnostics",
        }
        if set(value) != expected:
            raise ValueError("Duplicate evidence fields are invalid.")
        if _strict_int(value["schema_version"], "schema_version") != TRAINING_DUPLICATE_EVIDENCE_SCHEMA_VERSION:
            raise ValueError("Duplicate evidence schema version is unsupported.")
        algorithm = _strict_int(value["algorithm_version"], "algorithm_version", minimum=1)
        if algorithm != DUPLICATE_ALGORITHM_VERSION:
            raise ValueError("Duplicate evidence algorithm version is unsupported.")
        similarity = value["similarity_range"]
        if not isinstance(similarity, dict) or set(similarity) != {"lower", "upper"}:
            raise ValueError("Duplicate evidence similarity range is invalid.")
        lower, upper = similarity["lower"], similarity["upper"]
        if (
            isinstance(lower, bool) or isinstance(upper, bool)
            or not isinstance(lower, (int, float)) or not isinstance(upper, (int, float))
            or not math.isfinite(lower) or not math.isfinite(upper)
            or float(lower) != LOWER_SIMILARITY or float(upper) != UPPER_SIMILARITY
        ):
            raise ValueError("Duplicate evidence similarity range is invalid.")
        if not isinstance(value["decision_keys_available"], bool):
            raise ValueError("Duplicate evidence decision availability is invalid.")
        if not isinstance(value["truncated"], bool):
            raise ValueError("Duplicate evidence truncation state is invalid.")
        truncation_reason = _strict_text(value["truncation_reason"], "truncation_reason")
        if truncation_reason not in _TRUNCATION_REASONS:
            raise ValueError("Duplicate evidence truncation reason is invalid.")
        if bool(truncation_reason) != value["truncated"]:
            raise ValueError("Duplicate evidence truncation state is inconsistent.")
        current_hashes = {
            _strict_text(stable_id, "source stable ID"): digest
            for stable_id, digest in source_hashes.items()
        }
        if any(
            not stable_id or not isinstance(digest, str)
            or (digest and not _is_digest(digest))
            for stable_id, digest in current_hashes.items()
        ):
            raise ValueError("Duplicate evidence source hashes are invalid.")
        coverage = value["coverage"]
        if not isinstance(coverage, dict) or set(coverage) != {
            "eligible_count", "covered_count", "missing_count", "complete",
            "missing_reason_counts",
        }:
            raise ValueError("Duplicate evidence coverage is invalid.")
        eligible = _strict_int(coverage["eligible_count"], "eligible_count")
        covered_count = _strict_int(coverage["covered_count"], "covered_count")
        missing_count = _strict_int(coverage["missing_count"], "missing_count")
        if (
            eligible != len(current_hashes) or covered_count + missing_count != eligible
            or not isinstance(coverage["complete"], bool)
            or coverage["complete"] != (missing_count == 0 and not value["truncated"])
        ):
            raise ValueError("Duplicate evidence coverage counts are inconsistent.")
        raw_reasons = coverage["missing_reason_counts"]
        if not isinstance(raw_reasons, list):
            raise ValueError("Duplicate evidence missing reasons are invalid.")
        reasons: list[tuple[str, int]] = []
        for item in raw_reasons:
            if not isinstance(item, dict) or set(item) != {"reason", "count"}:
                raise ValueError("Duplicate evidence missing reasons are invalid.")
            reason = _strict_text(item["reason"], "missing reason")
            count = _strict_int(item["count"], "missing reason count", minimum=1)
            if reason not in _MISSING_REASONS:
                raise ValueError("Duplicate evidence missing reason is invalid.")
            reasons.append((reason, count))
        if tuple(reasons) != tuple(sorted(reasons)) or len({reason for reason, _count in reasons}) != len(reasons) or sum(count for _reason, count in reasons) != missing_count:
            raise ValueError("Duplicate evidence missing reasons are inconsistent.")
        raw_records = value["covered_images"]
        if not isinstance(raw_records, list) or len(raw_records) != covered_count:
            raise ValueError("Duplicate evidence covered images are invalid.")
        records: list[TrainingDuplicateEvidenceRecord] = []
        for item in raw_records:
            if not isinstance(item, dict) or set(item) != {"stable_id", "byte_sha256", "pixel_sha256"}:
                raise ValueError("Duplicate evidence covered image is invalid.")
            stable_id = _strict_text(item["stable_id"], "covered stable ID")
            byte_hash = _strict_text(item["byte_sha256"], "covered byte hash")
            pixel_hash = _strict_text(item["pixel_sha256"], "covered pixel hash")
            if stable_id not in current_hashes or not _is_digest(byte_hash) or not _is_digest(pixel_hash) or current_hashes[stable_id] != byte_hash:
                raise ValueError("Duplicate evidence covered image does not match the current plan.")
            records.append(TrainingDuplicateEvidenceRecord(stable_id, byte_hash, pixel_hash))
        if tuple(record.stable_id for record in records) != tuple(sorted(record.stable_id for record in records)) or len({record.stable_id for record in records}) != len(records):
            raise ValueError("Duplicate evidence covered images are not deterministic.")
        raw_relations = value["relations"]
        if not isinstance(raw_relations, list) or len(raw_relations) > MAX_RELATIONS:
            raise ValueError("Duplicate evidence relations are invalid.")
        record_ids = {record.stable_id for record in records}
        records_by_id = {record.stable_id: record for record in records}
        relations: list[TrainingDuplicateEvidenceRelation] = []
        seen_pairs: set[tuple[str, str]] = set()
        for item in raw_relations:
            expected_relation = {
                "first_stable_id", "second_stable_id", "kind", "similarity", "evidence", "reviewed_distinct",
            }
            if not isinstance(item, dict) or set(item) != expected_relation:
                raise ValueError("Duplicate evidence relation is invalid.")
            first = _strict_text(item["first_stable_id"], "first relation stable ID")
            second = _strict_text(item["second_stable_id"], "second relation stable ID")
            kind = _strict_text(item["kind"], "relation kind")
            score = item["similarity"]
            evidence = _strict_text(item["evidence"], "relation evidence")
            if (
                first >= second or first not in record_ids or second not in record_ids
                or kind not in _KINDS or isinstance(score, bool)
                or not isinstance(score, (int, float)) or not math.isfinite(score)
                or not 0 <= float(score) <= 100 or not evidence
                or not isinstance(item["reviewed_distinct"], bool)
            ):
                raise ValueError("Duplicate evidence relation is invalid.")
            first_record, second_record = records_by_id[first], records_by_id[second]
            if kind == "byte_exact":
                valid_relation = (
                    first_record.byte_sha256 == second_record.byte_sha256
                    and first_record.pixel_sha256 == second_record.pixel_sha256
                    and float(score) == 100.0 and evidence == _BYTE_EVIDENCE
                )
            elif kind == "pixel_exact":
                valid_relation = (
                    first_record.byte_sha256 != second_record.byte_sha256
                    and first_record.pixel_sha256 == second_record.pixel_sha256
                    and float(score) == 100.0 and evidence == _PIXEL_EVIDENCE
                )
            else:
                evidence_match = _SIMILAR_EVIDENCE.fullmatch(evidence)
                bits = int(evidence_match.group("bits")) if evidence_match else -1
                valid_relation = (
                    first_record.byte_sha256 != second_record.byte_sha256
                    and first_record.pixel_sha256 != second_record.pixel_sha256
                    and LOWER_SIMILARITY <= float(score) <= UPPER_SIMILARITY
                    and 0 <= bits <= 256
                    and math.isclose(
                        float(score), 100.0 * (1.0 - bits / 256), abs_tol=1e-9,
                    )
                )
            if not valid_relation or (item["reviewed_distinct"] and not value["decision_keys_available"]):
                raise ValueError("Duplicate evidence relation is invalid.")
            pair = (first, second)
            if pair in seen_pairs:
                raise ValueError("Duplicate evidence repeats a relation pair.")
            seen_pairs.add(pair)
            relations.append(TrainingDuplicateEvidenceRelation(
                first, second, kind, float(score), evidence, item["reviewed_distinct"],
            ))
        if tuple((item.first_stable_id, item.second_stable_id) for item in relations) != tuple(sorted((item.first_stable_id, item.second_stable_id) for item in relations)):
            raise ValueError("Duplicate evidence relations are not deterministic.")
        diagnostics = value["diagnostics"]
        if not isinstance(diagnostics, list) or any(item not in _DIAGNOSTICS for item in diagnostics) or tuple(diagnostics) != tuple(dict.fromkeys(diagnostics)):
            raise ValueError("Duplicate evidence diagnostics are invalid.")
        budget = value["comparison_budget"]
        expected_budget = {
            "max_relations", "candidate_evaluations", "candidate_evaluation_budget",
            "visual_index_probes", "visual_index_probe_budget",
        }
        if not isinstance(budget, dict) or set(budget) != expected_budget:
            raise ValueError("Duplicate evidence comparison budget is invalid.")
        max_relations = _strict_int(budget["max_relations"], "max_relations", minimum=1)
        candidate_evaluations = _strict_int(budget["candidate_evaluations"], "candidate_evaluations")
        candidate_budget = _strict_int(budget["candidate_evaluation_budget"], "candidate_evaluation_budget", minimum=1)
        visual_probes = _strict_int(budget["visual_index_probes"], "visual_index_probes")
        visual_budget = _strict_int(budget["visual_index_probe_budget"], "visual_index_probe_budget", minimum=1)
        if (
            max_relations != MAX_RELATIONS or candidate_evaluations > candidate_budget
            or visual_probes > visual_budget
        ):
            raise ValueError("Duplicate evidence comparison budget is invalid.")
        if (
            truncation_reason == "relation_bound" and len(relations) != MAX_RELATIONS
        ) or (
            truncation_reason == "candidate_evaluation_bound"
            and candidate_evaluations != candidate_budget
        ) or (
            truncation_reason == "visual_index_probe_bound"
            and visual_probes != visual_budget
        ):
            raise ValueError("Duplicate evidence truncation evidence is invalid.")
        return cls(
            tuple(records), tuple(relations), eligible, covered_count, missing_count,
            tuple(reasons), value["decision_keys_available"], algorithm,
            float(lower), float(upper), value["truncated"], truncation_reason,
            candidate_evaluations, candidate_budget, visual_probes, visual_budget,
            tuple(diagnostics), TRAINING_DUPLICATE_EVIDENCE_SCHEMA_VERSION,
        )


def _valid_cache_record(record: DuplicateFingerprint) -> bool:
    return (
        _is_digest(record.byte_sha256)
        and _is_digest(record.pixel_sha256)
        and 1 <= len(record.perceptual_hashes) <= 4
        and len(record.perceptual_hashes) == len(record.feature_labels)
        and len(set(record.perceptual_hashes)) == len(record.perceptual_hashes)
        and len(set(record.feature_labels)) == len(record.feature_labels)
        and all(
            not isinstance(item, bool) and isinstance(item, int) and 0 <= item < 2 ** 256
            for item in record.perceptual_hashes
        )
        and all(item in _FEATURE_LABELS for item in record.feature_labels)
    )


def _read_rows(
    index_path: str | Path, identities: Iterable[str], *,
    progress: Callable[[int, int, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[dict[str, DuplicateFingerprint], frozenset[str]]:
    values = tuple(dict.fromkeys(identities))
    if not values:
        return {}, frozenset()
    uri = Path(index_path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as database:
        database.row_factory = sqlite3.Row
        try:
            database.execute("PRAGMA query_only=ON")
            database.execute("BEGIN")
            metadata = database.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            if metadata is None or int(metadata[0]) != DUPLICATE_INDEX_SCHEMA_VERSION:
                raise ValueError("duplicate cache schema is unavailable")
            rows: dict[str, DuplicateFingerprint] = {}
            malformed: set[str] = set()
            for offset in range(0, len(values), 500):
                if should_cancel and should_cancel():
                    raise InterruptedError("Duplicate comparison cancelled safely during cache inspection.")
                chunk = values[offset:offset + 500]
                placeholders = ",".join("?" for _value in chunk)
                for row in database.execute(
                    f"SELECT * FROM fingerprints WHERE identity IN ({placeholders})", chunk,
                ):
                    identity = str(row["identity"])
                    try:
                        record = DuplicateIndex._record(row)
                    except (TypeError, ValueError, KeyError, OverflowError):
                        malformed.add(identity)
                        continue
                    if not _valid_cache_record(record):
                        malformed.add(identity)
                        continue
                    rows[record.identity] = record
                if progress:
                    progress(min(offset + len(chunk), len(values)), len(values), "Reading cached duplicate fingerprints")
            return rows, frozenset(malformed)
        finally:
            try:
                database.rollback()
            except sqlite3.Error:
                pass


def build_training_duplicate_evidence(
    candidates: Iterable[object], *, index_path: str | Path,
    identity_by_stable_id: Mapping[str, str],
    decision_keys: frozenset[str] | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    callback_read_rows: Callable[..., tuple[dict[str, DuplicateFingerprint], frozenset[str]]] | None = None,
) -> TrainingDuplicateEvidence:
    """Return bounded cached duplicate observations without changing any state."""
    if should_cancel and should_cancel():
        raise InterruptedError("Duplicate comparison cancelled safely before cache inspection.")
    values = tuple(candidates)
    stable_ids: list[str] = []
    seen_stable_ids: set[str] = set()
    requested: dict[str, tuple[str, str]] = {}
    reasons: dict[str, int] = {}
    for candidate_position, candidate in enumerate(values, 1):
        if candidate_position % 256 == 0 and should_cancel and should_cancel():
            raise InterruptedError("Duplicate comparison cancelled safely during candidate intake.")
        stable_id = str(getattr(candidate, "stable_id", ""))
        if not stable_id or stable_id in seen_stable_ids:
            raise ValueError("Training duplicate evidence requires unique stable candidate IDs.")
        stable_ids.append(stable_id)
        seen_stable_ids.add(stable_id)
        evidence = getattr(candidate, "evidence", None)
        byte_hash = getattr(evidence, "sha256", "") if evidence is not None else ""
        identity = identity_by_stable_id.get(stable_id)
        if not _is_digest(byte_hash):
            reasons["source_evidence_unavailable"] = reasons.get("source_evidence_unavailable", 0) + 1
        elif not isinstance(identity, str) or not identity:
            reasons["scope_identity_unavailable"] = reasons.get("scope_identity_unavailable", 0) + 1
        else:
            requested[stable_id] = (identity, byte_hash)
    identity_counts = Counter(identity for identity, _hash in requested.values())
    collisions = {identity for identity, count in identity_counts.items() if count > 1}
    if collisions:
        for stable_id, (identity, _hash) in tuple(requested.items()):
            if identity in collisions:
                requested.pop(stable_id)
                reasons["scope_identity_collision"] = reasons.get("scope_identity_collision", 0) + 1
    rows: dict[str, DuplicateFingerprint] = {}
    malformed_rows: frozenset[str] = frozenset()
    cache_unavailable = False
    try:
        reader = callback_read_rows or _read_rows
        rows, malformed_rows = reader(
            index_path, tuple(identity for identity, _hash in requested.values()),
            progress=progress, should_cancel=should_cancel,
        )
    except InterruptedError:
        raise
    except (OSError, ValueError, sqlite3.Error):
        cache_unavailable = True
    accepted: dict[str, tuple[str, DuplicateFingerprint]] = {}
    for record_position, (stable_id, (identity, byte_hash)) in enumerate(
        requested.items(), 1,
    ):
        if record_position % 256 == 0 and should_cancel and should_cancel():
            raise InterruptedError("Duplicate comparison cancelled safely during cache matching.")
        if cache_unavailable:
            reason = "cache_unavailable"
        else:
            record = rows.get(identity)
            if record is None:
                reason = "cache_row_unusable" if identity in malformed_rows else "cache_row_unavailable"
            elif not record.usable or not _valid_cache_record(record):
                reason = "cache_row_unusable"
            elif record.algorithm_version != DUPLICATE_ALGORITHM_VERSION:
                reason = "cache_row_algorithm_mismatch"
            elif record.byte_sha256 != byte_hash:
                reason = "cache_row_content_mismatch"
            else:
                reason = ""
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1
            continue
        accepted[stable_id] = (identity, record)
    byte_signatures: dict[str, set[tuple[object, ...]]] = {}
    pixel_geometries: dict[str, set[tuple[int, int]]] = {}
    for accepted_position, (_stable_id, (_identity, record)) in enumerate(
        accepted.items(), 1,
    ):
        if accepted_position % 256 == 0 and should_cancel and should_cancel():
            raise InterruptedError("Duplicate comparison cancelled safely during cache consistency checks.")
        byte_signatures.setdefault(record.byte_sha256, set()).add((
            record.pixel_sha256, record.width, record.height,
            record.perceptual_hashes, record.feature_labels,
        ))
        pixel_geometries.setdefault(record.pixel_sha256, set()).add((
            record.width, record.height,
        ))
    inconsistent_bytes = {
        digest for digest, values in byte_signatures.items() if len(values) > 1
    }
    inconsistent_pixels = {
        digest for digest, values in pixel_geometries.items() if len(values) > 1
    }
    covered: list[TrainingDuplicateEvidenceRecord] = []
    records: dict[str, DuplicateFingerprint] = {}
    stable_by_identity: dict[str, str] = {}
    for accepted_position, (stable_id, (identity, record)) in enumerate(
        accepted.items(), 1,
    ):
        if accepted_position % 256 == 0 and should_cancel and should_cancel():
            raise InterruptedError("Duplicate comparison cancelled safely during cache consistency checks.")
        if (
            record.byte_sha256 in inconsistent_bytes
            or record.pixel_sha256 in inconsistent_pixels
        ):
            reasons["cache_row_unusable"] = reasons.get("cache_row_unusable", 0) + 1
            continue
        covered.append(TrainingDuplicateEvidenceRecord(
            stable_id, record.byte_sha256, record.pixel_sha256,
        ))
        records[identity] = record
        stable_by_identity[identity] = stable_id
    covered.sort(key=lambda item: item.stable_id)
    metrics: dict[str, int | str] = {}
    groups = ()
    truncated = False
    if records:
        groups, truncated = find_duplicate_groups(
            records, lower_similarity=LOWER_SIMILARITY,
            upper_similarity=UPPER_SIMILARITY, decisions=None,
            max_relations=MAX_RELATIONS,
            max_candidate_evaluations=MAX_CANDIDATE_EVALUATIONS,
            max_visual_index_probes=MAX_VISUAL_INDEX_PROBES,
            progress=progress, should_cancel=should_cancel, metrics=metrics,
        )
    relations: list[TrainingDuplicateEvidenceRelation] = []
    for group_position, group in enumerate(groups, 1):
        if group_position % 256 == 0 and should_cancel and should_cancel():
            raise InterruptedError("Duplicate comparison cancelled safely during relation assembly.")
        for relation in group.relations:
            first = stable_by_identity[relation.first_identity]
            second = stable_by_identity[relation.second_identity]
            first, second = sorted((first, second))
            first_record = records[relation.first_identity]
            second_record = records[relation.second_identity]
            distinct_key = DuplicateDecisionStore.pair_key(first_record, second_record)
            relations.append(TrainingDuplicateEvidenceRelation(
                first, second, relation.kind, relation.similarity, relation.evidence,
                bool(decision_keys is not None and distinct_key in decision_keys),
            ))
    relations.sort(key=lambda item: (item.first_stable_id, item.second_stable_id))
    if len({(item.first_stable_id, item.second_stable_id) for item in relations}) != len(relations):
        raise RuntimeError("Duplicate cache returned repeated relation pairs.")
    diagnostics: list[str] = []
    if cache_unavailable:
        diagnostics.append("Duplicate cache evidence is unavailable; no cache scan was started.")
    elif reasons:
        diagnostics.append("Duplicate cache evidence is partial; some selected images have no current cached fingerprint.")
    if truncated:
        diagnostics.append("Duplicate comparison reached a bounded limit; omitted relations were not assessed.")
    ordered_reasons = tuple(sorted(reasons.items()))
    return TrainingDuplicateEvidence(
        tuple(covered), tuple(relations), len(stable_ids), len(covered),
        len(stable_ids) - len(covered), ordered_reasons,
        decision_keys is not None, DUPLICATE_ALGORITHM_VERSION,
        LOWER_SIMILARITY, UPPER_SIMILARITY, truncated,
        str(metrics.get("truncation_reason", "")) if truncated else "",
        int(metrics.get("candidate_evaluations", 0)),
        int(metrics.get("candidate_budget", MAX_CANDIDATE_EVALUATIONS)),
        int(metrics.get("visual_index_probes", 0)),
        int(metrics.get("visual_index_probe_budget", MAX_VISUAL_INDEX_PROBES)),
        tuple(diagnostics), TRAINING_DUPLICATE_EVIDENCE_SCHEMA_VERSION,
    )
