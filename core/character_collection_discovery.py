from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .character_database import CharacterDatabase
from .character_record_edit import CharacterRecordEditService
from .review_queue import ReviewTask, ReviewTaskDatabase
from .tag_collection import normalize_tag


COLLECTION_TASK_TYPE = "collection_member"
COLLECTION_OWNER_KINDS = ("character", "outfit")
COLLECTION_CATEGORY_ROOTS = {
    "appearance": "appearance",
    "clothing": "apparel",
    "artstyle": "style",
    "style": "style",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_safe(value):
    if isinstance(value, dict):
        return {
            str(key): _json_safe(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _fingerprint(value) -> str:
    encoded = json.dumps(
        _json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _wilson_lower_bound(successes: int, samples: int, z: float = 1.96) -> float:
    if samples <= 0:
        return 0.0
    rate = successes / samples
    denominator = 1.0 + (z * z / samples)
    center = rate + (z * z / (2.0 * samples))
    spread = z * math.sqrt(
        (rate * (1.0 - rate) + z * z / (4.0 * samples)) / samples
    )
    return max(0.0, (center - spread) / denominator)


def collection_source_key(owner_kind: str, owner_id: str, target_tag_id: str) -> str:
    return (
        "km03.collection_member:"
        f"{str(owner_kind)}:{str(owner_id)}:{str(target_tag_id)}"
    )


def _durable_task_decision(task: ReviewTask) -> bool:
    resolution = task.details.get("resolution")
    decision = str(
        resolution.get("decision") or ""
        if isinstance(resolution, dict) else ""
    )
    return (
        task.status in {"ignored", "applying"}
        or decision == "rejected"
    )


@dataclass(frozen=True)
class CharacterCollectionCandidate:
    id: str
    source_key: str
    owner_kind: str
    owner_id: str
    owner_label: str
    representative_tag_id: str
    representative_tag: str
    target_tag_id: str
    target_tag: str
    target_category_id: str
    target_category_path: str
    collection_field: str
    observation_count: int
    sample_count: int
    rate: float
    baseline_count: int
    baseline_sample_count: int
    baseline_rate: float
    lift: float
    confidence: float
    rank: int
    source_scope: tuple[str, ...]
    example_paths: tuple[str, ...]
    evidence_fingerprint: str

    def task_details(self, snapshot_token) -> dict[str, Any]:
        return {
            "roadmap_id": "KM-03",
            "owner_kind": self.owner_kind,
            "owner_id": self.owner_id,
            "owner_label": self.owner_label,
            "representative_tag_id": self.representative_tag_id,
            "representative_tag": self.representative_tag,
            "target_tag_id": self.target_tag_id,
            "target_tag": self.target_tag,
            "target_category_id": self.target_category_id,
            "target_category_path": self.target_category_path,
            "collection_field": self.collection_field,
            "observation_count": self.observation_count,
            "sample_count": self.sample_count,
            "rate": self.rate,
            "baseline_count": self.baseline_count,
            "baseline_sample_count": self.baseline_sample_count,
            "baseline_rate": self.baseline_rate,
            "lift": self.lift,
            "confidence": self.confidence,
            "rank": self.rank,
            "source_scope": list(self.source_scope),
            "example_paths": list(self.example_paths),
            "evidence_fingerprint": self.evidence_fingerprint,
            "evidence_snapshot_token": _json_safe(snapshot_token),
            "queue_health": {
                "category": "meaningful",
                "reason": "Current indexed confirmed Image Knowledge supports a collection review.",
                "recommended_status": "",
            },
            "before": (
                f"{self.target_tag} is not a reviewed member of the "
                f"{self.owner_label} collection."
            ),
            "after": (
                f"If approved, {self.target_tag} becomes one reviewed "
                f"{self.owner_label} collection member. Rejection remains a "
                "durable exact decision."
            ),
            "image_knowledge_effect": (
                "None. Collection review does not confirm or remove a Tag on any image."
            ),
        }


@dataclass(frozen=True)
class CharacterCollectionDiscoveryReport:
    snapshot_token: Any
    evidence_fingerprint: str
    records_scanned: int
    confirmed_records: int
    character_samples: int
    outfit_samples: int
    ambiguous_owner_records: int
    candidates: tuple[CharacterCollectionCandidate, ...]
    existing_members_excluded: int
    durable_decisions_excluded: int
    invalid_representatives_excluded: int
    unresolved_tags_excluded: int
    out_of_scope_tags_excluded: int
    cancelled: bool = False

    @property
    def candidate_count(self) -> int:
        return len(self.candidates)

    @property
    def family_count(self) -> int:
        return len({(item.owner_kind, item.owner_id) for item in self.candidates})


@dataclass(frozen=True)
class CharacterCollectionQueueResult:
    queued: int
    updated: int
    skipped_decisions: int
    task_ids: tuple[str, ...]


@dataclass(frozen=True)
class CharacterCollectionDecisionResult:
    task_id: str
    decision: str
    owner_kind: str
    owner_id: str
    target_tag_id: str
    related_pending_reranked: int
    backup_directory: Path = Path()
    recovered: bool = False


class CharacterCollectionDiscoveryService:
    """Rank conservative collection candidates from current indexed facts."""

    def __init__(
        self,
        knowledge,
        categories,
        characters: CharacterDatabase,
        reviews: ReviewTaskDatabase | None = None,
        *,
        min_observations: int = 3,
        min_rate: float = 0.60,
        min_lift: float = 1.20,
        min_confidence: float = 0.30,
        max_candidates_per_family: int = 50,
        max_examples: int = 3,
    ) -> None:
        self.knowledge = knowledge
        self.categories = categories
        self.characters = characters
        self.reviews = reviews
        self.min_observations = max(1, int(min_observations))
        self.min_rate = max(0.0, min(1.0, float(min_rate)))
        self.min_lift = max(0.0, float(min_lift))
        self.min_confidence = max(0.0, min(1.0, float(min_confidence)))
        self.max_candidates_per_family = max(1, int(max_candidates_per_family))
        self.max_examples = max(1, int(max_examples))

    @staticmethod
    def _confirmed_tags(record) -> tuple[str, ...]:
        result = []
        sources = getattr(record, "tag_sources", {}) or {}
        normalized_sources = {
            normalize_tag(value): labels for value, labels in sources.items()
        }
        for value in (getattr(record, "tags", ()) or ()):
            key = normalize_tag(value)
            labels = normalized_sources.get(key, ())
            if "Confirmed Image Knowledge" in {
                str(label).strip() for label in (labels or ())
            }:
                result.append(str(value))
        return tuple(dict.fromkeys(result))

    def _scope(self, tag_record) -> tuple[str, str]:
        category_id = str(getattr(tag_record, "category", "") or "")
        path_ids = tuple(self.categories.path_ids(category_id))
        scope = next((
            label for root_id, label in COLLECTION_CATEGORY_ROOTS.items()
            if root_id == category_id or root_id in path_ids
        ), "")
        return scope, self.categories.path_label(category_id)

    def _representatives(self):
        representatives: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        by_owner = {}
        invalid = 0
        for owner_kind, records in (
            ("character", self.characters.characters),
            ("outfit", self.characters.outfits),
        ):
            for owner in records.values():
                tag_id = self.characters.entity_tag_id(owner_kind, owner.id)
                tag = self.knowledge.tags.get(tag_id)
                if tag is None:
                    continue
                expected_role = (
                    "character_identity"
                    if owner_kind == "character" else "character_outfit"
                )
                if self.categories.effective_semantic_role(
                    tag.category
                ) != expected_role:
                    invalid += 1
                    continue
                entry = (owner_kind, owner.id, tag.id)
                representatives[normalize_tag(tag.display)].append(entry)
                by_owner[(owner_kind, owner.id)] = (owner, tag)
        return representatives, by_owner, invalid

    def _existing_member_ids(self, owner_kind: str, owner) -> set[str]:
        values = [*getattr(owner, "sdxl_tags", ())]
        if owner_kind == "character":
            values.extend(getattr(owner, "original_style_tags", ()))
        else:
            values.extend(getattr(owner, "style_tags", ()))
        return {
            record.id
            for value in values
            for record in (self.knowledge.find(value),)
            if record is not None
        }

    def _durable_decision_keys(self) -> set[str]:
        if self.reviews is None:
            return set()
        return {
            task.source_key
            for task in self.reviews.tasks.values()
            if task.task_type == COLLECTION_TASK_TYPE
            and task.source_key
            and _durable_task_decision(task)
        }

    def discover(
        self,
        records: Iterable,
        *,
        snapshot_token,
        library_ids_by_path: dict[str, str] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> CharacterCollectionDiscoveryReport:
        rows = tuple(records)
        total = len(rows)
        library_ids_by_path = dict(library_ids_by_path or {})
        representatives, owners, invalid_representatives = self._representatives()
        representative_ids = {
            tag_id for entries in representatives.values()
            for _kind, _owner_id, tag_id in entries
        }
        durable = self._durable_decision_keys()
        baseline_counts: Counter[str] = Counter()
        family_samples: Counter[tuple[str, str]] = Counter()
        observations: Counter[tuple[str, str, str]] = Counter()
        examples: dict[tuple[str, str, str], list[str]] = defaultdict(list)
        scopes: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        confirmed_records = 0
        character_samples = 0
        outfit_samples = 0
        ambiguous_owner_records = 0
        unresolved_tags_excluded = 0
        out_of_scope_tags_excluded = 0

        for position, row in enumerate(rows, 1):
            if should_cancel is not None and should_cancel():
                raise InterruptedError("Character collection discovery cancelled")
            if progress is not None and (
                position == 1 or position == total or position % 250 == 0
            ):
                progress(position, total, f"Analyzing indexed Image Knowledge ({position:,}/{total:,})")
            confirmed_values = self._confirmed_tags(row)
            if confirmed_values:
                confirmed_records += 1
            stable_tags = {}
            owner_entries = set()
            for value in confirmed_values:
                tag = self.knowledge.find(value)
                if tag is None:
                    unresolved_tags_excluded += 1
                    continue
                stable_tags[tag.id] = tag
                owner_entries.update(representatives.get(normalize_tag(tag.display), ()))
            if not stable_tags:
                continue
            for tag_id, tag in stable_tags.items():
                scope, _path = self._scope(tag)
                if scope:
                    baseline_counts[tag_id] += 1

            characters_present = {
                (kind, owner_id) for kind, owner_id, _tag_id in owner_entries
                if kind == "character"
            }
            outfits_present = {
                (kind, owner_id) for kind, owner_id, _tag_id in owner_entries
                if kind == "outfit"
            }
            families = []
            ambiguous = len(characters_present) > 1 or len(outfits_present) > 1
            if ambiguous:
                ambiguous_owner_records += 1
            if len(characters_present) == 1 and not outfits_present:
                families.extend(characters_present)
                character_samples += 1
            if len(outfits_present) == 1 and len(characters_present) <= 1:
                families.extend(outfits_present)
                outfit_samples += 1
            if not families:
                continue

            path = str(getattr(row, "path", "") or "")
            library_id = str(library_ids_by_path.get(path) or "")
            for owner_kind, owner_id in families:
                family_samples[(owner_kind, owner_id)] += 1
                for target_id, tag in stable_tags.items():
                    if target_id in representative_ids:
                        continue
                    scope, _path = self._scope(tag)
                    if not scope:
                        out_of_scope_tags_excluded += 1
                        continue
                    key = (owner_kind, owner_id, target_id)
                    observations[key] += 1
                    if path and path not in examples[key] and len(examples[key]) < self.max_examples:
                        examples[key].append(path)
                    if library_id:
                        scopes[key].add(library_id)

        existing_members_excluded = 0
        durable_decisions_excluded = 0
        candidates_by_family: dict[
            tuple[str, str], list[CharacterCollectionCandidate]
        ] = defaultdict(list)
        for (owner_kind, owner_id, target_id), count in observations.items():
            owner_info = owners.get((owner_kind, owner_id))
            target = self.knowledge.tags.get(target_id)
            if owner_info is None or target is None:
                continue
            owner, representative = owner_info
            if target_id in self._existing_member_ids(owner_kind, owner):
                existing_members_excluded += 1
                continue
            source_key = collection_source_key(owner_kind, owner_id, target_id)
            if source_key in durable:
                durable_decisions_excluded += 1
                continue
            samples = family_samples[(owner_kind, owner_id)]
            rate = count / samples if samples else 0.0
            baseline_count = baseline_counts[target_id]
            baseline_rate = (
                baseline_count / confirmed_records if confirmed_records else 0.0
            )
            lift = rate / baseline_rate if baseline_rate else 0.0
            confidence = _wilson_lower_bound(count, samples)
            if (
                count < self.min_observations
                or rate < self.min_rate
                or lift < self.min_lift
                or confidence < self.min_confidence
            ):
                continue
            category_scope, category_path = self._scope(target)
            collection_field = (
                "original_style_tags"
                if owner_kind == "character" and category_scope == "style"
                else "style_tags"
                if owner_kind == "outfit" and category_scope == "style"
                else "sdxl_tags"
            )
            evidence = {
                "snapshot": _json_safe(snapshot_token),
                "owner_kind": owner_kind,
                "owner_id": owner_id,
                "representative_tag_id": representative.id,
                "target_tag_id": target_id,
                "observation_count": count,
                "sample_count": samples,
                "baseline_count": baseline_count,
                "baseline_sample_count": confirmed_records,
                "examples": examples[(owner_kind, owner_id, target_id)],
                "scope": sorted(scopes[(owner_kind, owner_id, target_id)]),
            }
            candidates_by_family[(owner_kind, owner_id)].append(
                CharacterCollectionCandidate(
                    id=_fingerprint((source_key, evidence))[:24],
                    source_key=source_key,
                    owner_kind=owner_kind,
                    owner_id=owner_id,
                    owner_label=str(owner.name),
                    representative_tag_id=representative.id,
                    representative_tag=representative.display,
                    target_tag_id=target.id,
                    target_tag=target.display,
                    target_category_id=str(target.category),
                    target_category_path=category_path,
                    collection_field=collection_field,
                    observation_count=count,
                    sample_count=samples,
                    rate=round(rate, 6),
                    baseline_count=baseline_count,
                    baseline_sample_count=confirmed_records,
                    baseline_rate=round(baseline_rate, 6),
                    lift=round(lift, 6),
                    confidence=round(confidence, 6),
                    rank=0,
                    source_scope=tuple(sorted(scopes[(owner_kind, owner_id, target_id)])),
                    example_paths=tuple(examples[(owner_kind, owner_id, target_id)]),
                    evidence_fingerprint=_fingerprint(evidence),
                )
            )

        candidates = []
        for family in sorted(candidates_by_family):
            ranked = sorted(
                candidates_by_family[family],
                key=lambda item: (
                    -item.confidence, -item.rate, -item.lift,
                    -item.observation_count, normalize_tag(item.target_tag),
                    item.target_tag_id,
                ),
            )[: self.max_candidates_per_family]
            for rank, candidate in enumerate(ranked, 1):
                candidates.append(replace(candidate, rank=rank))

        report_fingerprint = _fingerprint({
            "snapshot": _json_safe(snapshot_token),
            "records_scanned": total,
            "candidates": [item.evidence_fingerprint for item in candidates],
        })
        return CharacterCollectionDiscoveryReport(
            snapshot_token=_json_safe(snapshot_token),
            evidence_fingerprint=report_fingerprint,
            records_scanned=total,
            confirmed_records=confirmed_records,
            character_samples=character_samples,
            outfit_samples=outfit_samples,
            ambiguous_owner_records=ambiguous_owner_records,
            candidates=tuple(candidates),
            existing_members_excluded=existing_members_excluded,
            durable_decisions_excluded=durable_decisions_excluded,
            invalid_representatives_excluded=invalid_representatives,
            unresolved_tags_excluded=unresolved_tags_excluded,
            out_of_scope_tags_excluded=out_of_scope_tags_excluded,
        )

    @staticmethod
    def queue_candidates(
        report: CharacterCollectionDiscoveryReport,
        reviews: ReviewTaskDatabase,
        *,
        current_snapshot_token,
        selected_candidate_ids: Iterable[str] | None = None,
    ) -> CharacterCollectionQueueResult:
        if _json_safe(current_snapshot_token) != _json_safe(report.snapshot_token):
            raise RuntimeError(
                "Indexed Image Knowledge changed after this collection preview. "
                "Run discovery again before creating Review questions."
            )
        selected = (
            None if selected_candidate_ids is None else
            {str(value) for value in selected_candidate_ids}
        )
        queued = 0
        updated = 0
        skipped = 0
        task_ids = []
        try:
            with reviews.transaction():
                for candidate in report.candidates:
                    if selected is not None and candidate.id not in selected:
                        continue
                    existing = reviews.find_by_source_key(candidate.source_key)
                    if existing is not None and _durable_task_decision(existing):
                        skipped += 1
                        continue
                    before = existing.to_dict() if existing is not None else None
                    if existing is not None and existing.status == "resolved":
                        # A previously accepted member can be removed later in
                        # Character Knowledge. Its current absence supersedes
                        # the old completion state and merits fresh review.
                        existing.status = "pending"
                    task = reviews.add_task(
                        task_type=COLLECTION_TASK_TYPE,
                        title=(
                            f"Add {candidate.target_tag} to the "
                            f"{candidate.owner_label} collection?"
                        ),
                        image_path=(candidate.example_paths[0] if candidate.example_paths else ""),
                        description=(
                            "Current indexed confirmed Image Knowledge repeatedly "
                            "co-occurs with this exact representative identity."
                        ),
                        details=candidate.task_details(report.snapshot_token),
                        priority="normal",
                        source_key=candidate.source_key,
                    )
                    if existing is not None:
                        next_image = (
                            candidate.example_paths[0]
                            if candidate.example_paths else ""
                        )
                        if task.image_path != next_image:
                            task.image_path = next_image
                            task.updated_at = _now()
                        if task.priority != "normal":
                            task.priority = "normal"
                            task.updated_at = _now()
                    task_ids.append(task.id)
                    if before is None:
                        queued += 1
                    elif task.to_dict() != before:
                        updated += 1
                if queued or updated:
                    reviews.save()
        except BaseException:
            reviews.load()
            raise
        return CharacterCollectionQueueResult(
            queued, updated, skipped, tuple(task_ids),
        )


class CharacterCollectionReviewService:
    """Apply one collection decision with a durable cross-store recovery marker."""

    def __init__(
        self,
        characters: CharacterDatabase,
        knowledge,
        categories,
        reviews: ReviewTaskDatabase,
        *,
        snapshot_provider: Callable[[], Any] | None = None,
    ) -> None:
        self.characters = characters
        self.knowledge = knowledge
        self.categories = categories
        self.reviews = reviews
        self.snapshot_provider = snapshot_provider

    def _task(self, task_id: str) -> ReviewTask:
        task = self.reviews.tasks.get(str(task_id))
        if task is None or task.task_type != COLLECTION_TASK_TYPE:
            raise ValueError("The collection Review question is no longer available.")
        return task

    def _validate_snapshot(self, task: ReviewTask) -> None:
        if self.snapshot_provider is None:
            return
        current = _json_safe(self.snapshot_provider())
        expected = _json_safe(task.details.get("evidence_snapshot_token"))
        if current != expected:
            raise RuntimeError(
                "Indexed Image Knowledge changed after this collection question "
                "was prepared. Run Find New Knowledge again before deciding it."
            )

    def _owner_target(self, task: ReviewTask):
        owner_kind = str(task.details.get("owner_kind") or "")
        owner_id = str(task.details.get("owner_id") or "")
        target_id = str(task.details.get("target_tag_id") or "")
        if owner_kind not in COLLECTION_OWNER_KINDS:
            raise ValueError("The collection question has an unsupported owner kind.")
        owners = (
            self.characters.characters
            if owner_kind == "character" else self.characters.outfits
        )
        owner = owners.get(owner_id)
        target = self.knowledge.tags.get(target_id)
        if owner is None or target is None:
            raise RuntimeError(
                "The collection owner or candidate Tag changed after discovery. "
                "Run Find New Knowledge again."
            )
        representative_id = self.characters.entity_tag_id(owner_kind, owner.id)
        expected_representative_id = str(
            task.details.get("representative_tag_id") or ""
        )
        if representative_id != expected_representative_id:
            raise RuntimeError(
                "The reviewed representative Tag changed after discovery. "
                "Run Find New Knowledge again."
            )
        representative = self.knowledge.tags.get(representative_id)
        expected_role = (
            "character_identity"
            if owner_kind == "character" else "character_outfit"
        )
        if (
            representative is None
            or self.categories.effective_semantic_role(
                representative.category
            ) != expected_role
        ):
            raise RuntimeError(
                "The representative Tag no longer has the required Character "
                "Knowledge role. Run Find New Knowledge again."
            )
        if str(target.category) != str(
            task.details.get("target_category_id") or ""
        ):
            raise RuntimeError(
                "The candidate Tag category changed after discovery. Run Find "
                "New Knowledge again."
            )
        path_ids = tuple(self.categories.path_ids(target.category))
        scope = next((
            label for root_id, label in COLLECTION_CATEGORY_ROOTS.items()
            if root_id == target.category or root_id in path_ids
        ), "")
        expected_field = (
            "original_style_tags"
            if owner_kind == "character" and scope == "style"
            else "style_tags"
            if owner_kind == "outfit" and scope == "style"
            else "sdxl_tags"
            if scope in {"appearance", "apparel"}
            else ""
        )
        if expected_field != str(task.details.get("collection_field") or ""):
            raise RuntimeError(
                "The candidate Tag no longer maps to the reviewed collection "
                "field. Run Find New Knowledge again."
            )
        return owner_kind, owner, target

    @staticmethod
    def _field_values(owner_kind: str, owner, field: str) -> list[str]:
        allowed = {
            "character": {"sdxl_tags", "original_style_tags"},
            "outfit": {"sdxl_tags", "style_tags"},
        }
        if field not in allowed[owner_kind]:
            raise ValueError("The collection question names an invalid collection field.")
        return list(getattr(owner, field))

    def _is_member(self, task: ReviewTask) -> bool:
        try:
            owner_kind, owner, target = self._owner_target(task)
            field = str(task.details.get("collection_field") or "")
            values = self._field_values(owner_kind, owner, field)
        except (RuntimeError, ValueError):
            return False
        return any(
            record is not None and record.id == target.id
            for value in values
            for record in (self.knowledge.find(value),)
        )

    def _add_member(self, task: ReviewTask) -> Path:
        owner_kind, owner, target = self._owner_target(task)
        field = str(task.details.get("collection_field") or "")
        values = self._field_values(owner_kind, owner, field)
        if not any(normalize_tag(value) == normalize_tag(target.display) for value in values):
            values.append(target.display)
        editor = CharacterRecordEditService(
            self.characters, self.knowledge, self.categories,
        )
        if owner_kind == "character":
            result = editor.update_character(
                owner.id,
                name=owner.name,
                series_id=owner.series_id,
                identity_type=owner.identity_type,
                aliases=owner.aliases,
                sdxl_tags=(values if field == "sdxl_tags" else owner.sdxl_tags),
                plain_language=owner.plain_language,
                original_style_tags=(
                    values
                    if field == "original_style_tags" else owner.original_style_tags
                ),
                origin_label=owner.origin_label,
                creator=owner.creator,
            )
        else:
            result = editor.update_outfit(
                owner.id,
                name=owner.name,
                series_id=owner.series_id,
                character_ids=owner.character_ids,
                aliases=owner.aliases,
                sdxl_tags=(values if field == "sdxl_tags" else owner.sdxl_tags),
                plain_language=owner.plain_language,
                style_tags=(values if field == "style_tags" else owner.style_tags),
                notes=owner.notes,
                character_ids_explicit=False,
            )
        return result.backup_directory

    def _rerank(self, owner_kind: str, owner_id: str) -> int:
        pending = [
            task for task in self.reviews.tasks.values()
            if task.task_type == COLLECTION_TASK_TYPE
            and task.status == "pending"
            and str(task.details.get("owner_kind") or "") == owner_kind
            and str(task.details.get("owner_id") or "") == owner_id
        ]
        pending.sort(key=lambda task: (
            -float(task.details.get("confidence") or 0),
            -float(task.details.get("rate") or 0),
            -float(task.details.get("lift") or 0),
            -int(task.details.get("observation_count") or 0),
            normalize_tag(task.details.get("target_tag") or task.title),
            task.id,
        ))
        changed = 0
        for rank, task in enumerate(pending, 1):
            if int(task.details.get("rank") or 0) != rank:
                task.details["rank"] = rank
                task.updated_at = _now()
                changed += 1
        return changed

    def recover_interrupted(self) -> tuple[CharacterCollectionDecisionResult, ...]:
        with self.reviews.transaction():
            self.reviews.load()
            if not any(
                task.task_type == COLLECTION_TASK_TYPE
                and task.status == "applying"
                for task in self.reviews.tasks.values()
            ):
                return ()
            return self._recover_interrupted_locked()

    def _recover_interrupted_locked(
        self,
    ) -> tuple[CharacterCollectionDecisionResult, ...]:
        self.characters.load()
        self.knowledge.load()
        self.categories.load()
        recovered = []
        changed = False
        for task in self.reviews.tasks.values():
            if task.task_type != COLLECTION_TASK_TYPE or task.status != "applying":
                continue
            owner_kind = str(task.details.get("owner_kind") or "")
            owner_id = str(task.details.get("owner_id") or "")
            target_id = str(task.details.get("target_tag_id") or "")
            accepted = self._is_member(task)
            task.status = "resolved" if accepted else "pending"
            saved_resolution = task.details.get("resolution")
            resolution = (
                dict(saved_resolution) if isinstance(saved_resolution, dict) else {}
            )
            resolution.update({
                "decision": "accepted" if accepted else "recovered_for_review",
                "recovered_at": _now(),
            })
            task.details["resolution"] = resolution
            task.updated_at = _now()
            reranked = self._rerank(owner_kind, owner_id)
            recovered.append(CharacterCollectionDecisionResult(
                task.id,
                "accepted" if accepted else "recovered_for_review",
                owner_kind,
                owner_id,
                target_id,
                reranked,
                recovered=True,
            ))
            changed = True
        if changed:
            try:
                self.reviews.save()
            except BaseException:
                self.reviews.load()
                raise
        return tuple(recovered)

    def decide(self, task_id: str, decision: str) -> CharacterCollectionDecisionResult:
        with self.reviews.transaction():
            return self._decide_locked(task_id, decision)

    def _decide_locked(
        self, task_id: str, decision: str,
    ) -> CharacterCollectionDecisionResult:
        decision = str(decision)
        if decision not in {"accepted", "rejected"}:
            raise ValueError("Choose accepted or rejected for this collection suggestion.")
        self.characters.load()
        self.knowledge.load()
        self.categories.load()
        self.reviews.load()
        task = self._task(task_id)
        if task.status != "pending":
            raise RuntimeError("This collection question is no longer pending.")
        self._validate_snapshot(task)
        owner_kind, owner, target = self._owner_target(task)
        resolution = {
            "decision": decision,
            "owner_kind": owner_kind,
            "owner_id": owner.id,
            "target_tag_id": target.id,
            "target_tag": target.display,
            "evidence_fingerprint": str(
                task.details.get("evidence_fingerprint") or ""
            ),
            "decided_at": _now(),
            "image_knowledge_effect": "none",
        }
        if decision == "rejected":
            task.status = "ignored"
            task.details["resolution"] = resolution
            task.updated_at = _now()
            reranked = self._rerank(owner_kind, owner.id)
            try:
                self.reviews.save()
            except BaseException:
                self.reviews.load()
                raise
            return CharacterCollectionDecisionResult(
                task.id, decision, owner_kind, owner.id, target.id, reranked,
            )

        if self._is_member(task):
            resolution["decision"] = "already_present"
            task.status = "resolved"
            task.details["resolution"] = resolution
            task.updated_at = _now()
            reranked = self._rerank(owner_kind, owner.id)
            try:
                self.reviews.save()
            except BaseException:
                self.reviews.load()
                raise
            return CharacterCollectionDecisionResult(
                task.id, "already_present", owner_kind, owner.id, target.id,
                reranked,
            )

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
            backup = self._add_member(task)
            self.characters.load()
            if not self._is_member(task):
                raise RuntimeError(
                    "The collection member failed semantic read-back verification."
                )
            task.status = "resolved"
            task.updated_at = _now()
            reranked = self._rerank(owner_kind, owner.id)
            try:
                self.reviews.save()
            except BaseException:
                self.reviews.load()
                raise
        except BaseException:
            self.characters.load()
            if not self._is_member(task):
                task.status = "pending"
                task.updated_at = _now()
                try:
                    self.reviews.save()
                except Exception:
                    self.reviews.load()
            raise
        return CharacterCollectionDecisionResult(
            task.id, decision, owner_kind, owner.id, target.id, reranked, backup,
        )


__all__ = [
    "COLLECTION_TASK_TYPE",
    "COLLECTION_CATEGORY_ROOTS",
    "CharacterCollectionCandidate",
    "CharacterCollectionDecisionResult",
    "CharacterCollectionDiscoveryReport",
    "CharacterCollectionDiscoveryService",
    "CharacterCollectionQueueResult",
    "CharacterCollectionReviewService",
    "collection_source_key",
]
