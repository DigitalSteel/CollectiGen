from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .character_database import CharacterDatabase
from .character_record_edit import CharacterRecordEditService
from .review_queue import ReviewTask, ReviewTaskDatabase
from .tag_alias_relationships import TagAliasChange, TagAliasRelationshipService
from .tag_collection import normalize_tag


SOURCE_LINK_TASK_TYPE = "character_source_link"
SOURCE_ALIAS_TASK_TYPE = "character_source_alias"
SOURCE_TASK_TYPES = frozenset((SOURCE_LINK_TASK_TYPE, SOURCE_ALIAS_TASK_TYPE))


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


def source_relationship_key(
    action: str, character_id: str, source_id: str,
) -> str:
    return (
        "km03.character_source:"
        f"{str(action)}:{str(character_id)}:{str(source_id)}"
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
class CharacterSourceCandidate:
    id: str
    action: str
    source_key: str
    character_id: str
    character_label: str
    character_tag_id: str
    character_tag: str
    source_id: str
    source_label: str
    source_tag_id: str
    source_tag: str
    current_source_id: str
    current_source_label: str
    alias: str
    observation_count: int
    source_scope: tuple[str, ...]
    example_paths: tuple[str, ...]
    evidence_fingerprint: str
    rank: int = 0

    def task_details(self, snapshot_token) -> dict[str, Any]:
        return {
            "roadmap_id": "KM-03",
            "source_action": self.action,
            "character_id": self.character_id,
            "character_label": self.character_label,
            "character_tag_id": self.character_tag_id,
            "character_tag": self.character_tag,
            "source_id": self.source_id,
            "source_label": self.source_label,
            "source_tag_id": self.source_tag_id,
            "source_tag": self.source_tag,
            "current_source_id": self.current_source_id,
            "current_source_label": self.current_source_label,
            "alias": self.alias,
            "observation_count": self.observation_count,
            "source_scope": list(self.source_scope),
            "example_paths": list(self.example_paths),
            "evidence_fingerprint": self.evidence_fingerprint,
            "evidence_snapshot_token": _json_safe(snapshot_token),
            "rank": self.rank,
            "queue_health": {
                "category": "meaningful",
                "reason": (
                    "Current indexed confirmed Character Identity and Character "
                    "Source Tags support this exact stable-record review."
                ),
                "recommended_status": "",
            },
            "before": (
                f"{self.character_label} currently uses "
                f"{self.current_source_label or 'no reviewed Character Source'}."
                if self.action == "link" else
                f"{self.alias} is not a reviewed Other Name for {self.character_tag}."
            ),
            "after": (
                f"If approved, {self.character_label} uses {self.source_label} "
                "as its reviewed Character Source."
                if self.action == "link" else
                f"If approved, {self.alias} becomes a reviewed Other Name for "
                f"{self.character_tag}."
            ),
            "image_knowledge_effect": (
                "None. This review does not confirm, remove, or rewrite a Tag on any image."
            ),
        }


@dataclass(frozen=True)
class CharacterSourceDiscoveryReport:
    snapshot_token: Any
    evidence_fingerprint: str
    records_scanned: int
    confirmed_records: int
    exact_pair_records: int
    ambiguous_pair_records: int
    candidates: tuple[CharacterSourceCandidate, ...]
    existing_links_excluded: int
    existing_aliases_excluded: int
    alias_collisions_excluded: int
    durable_decisions_excluded: int
    invalid_representatives_excluded: int

    @property
    def candidate_count(self) -> int:
        return len(self.candidates)

    @property
    def link_count(self) -> int:
        return sum(item.action == "link" for item in self.candidates)

    @property
    def alias_count(self) -> int:
        return sum(item.action == "alias" for item in self.candidates)


@dataclass(frozen=True)
class CharacterSourceQueueResult:
    queued: int
    updated: int
    skipped_decisions: int
    task_ids: tuple[str, ...]


@dataclass(frozen=True)
class CharacterSourceDecisionResult:
    task_id: str
    decision: str
    action: str
    character_id: str
    source_id: str
    related_pending_reranked: int
    backup_path: Path = Path()
    recovered: bool = False


class CharacterSourceDiscoveryService:
    """Find exact Character Identity + Character Source pairs in cached facts."""

    def __init__(
        self,
        knowledge,
        categories,
        characters: CharacterDatabase,
        reviews: ReviewTaskDatabase | None = None,
        *,
        max_examples: int = 3,
        max_candidates: int = 500,
    ) -> None:
        self.knowledge = knowledge
        self.categories = categories
        self.characters = characters
        self.reviews = reviews
        self.max_examples = max(1, int(max_examples))
        self.max_candidates = max(1, int(max_candidates))

    @staticmethod
    def _confirmed_tags(record) -> tuple[str, ...]:
        sources = getattr(record, "tag_sources", {}) or {}
        normalized_sources = {
            normalize_tag(value): labels for value, labels in sources.items()
        }
        result = []
        for value in (getattr(record, "tags", ()) or ()):
            labels = normalized_sources.get(normalize_tag(value), ())
            if "Confirmed Image Knowledge" in {
                str(label).strip() for label in (labels or ())
            }:
                result.append(str(value))
        return tuple(dict.fromkeys(result))

    def _representatives(self):
        character_by_tag: dict[str, list[str]] = defaultdict(list)
        source_by_tag: dict[str, list[str]] = defaultdict(list)
        invalid = 0
        for character in self.characters.characters.values():
            tag_id = self.characters.entity_tag_id("character", character.id)
            tag = self.knowledge.tags.get(tag_id)
            if tag is None:
                continue
            if self.categories.effective_semantic_role(
                tag.category
            ) != "character_identity":
                invalid += 1
                continue
            character_by_tag[tag.id].append(character.id)
        for source in self.characters.series.values():
            tag_id = self.characters.entity_tag_id("series", source.id)
            tag = self.knowledge.tags.get(tag_id)
            if tag is None:
                continue
            if self.categories.effective_semantic_role(
                tag.category
            ) != "character_source":
                invalid += 1
                continue
            source_by_tag[tag.id].append(source.id)
        return character_by_tag, source_by_tag, invalid

    def _durable_keys(self) -> set[str]:
        if self.reviews is None:
            return set()
        return {
            task.source_key
            for task in self.reviews.tasks.values()
            if task.task_type in SOURCE_TASK_TYPES
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
    ) -> CharacterSourceDiscoveryReport:
        rows = tuple(records)
        total = len(rows)
        libraries = library_ids_by_path or {}
        character_by_tag, source_by_tag, invalid = self._representatives()
        durable = self._durable_keys()
        observations: Counter[tuple[str, str]] = Counter()
        examples: dict[tuple[str, str], list[str]] = defaultdict(list)
        scopes: dict[tuple[str, str], set[str]] = defaultdict(set)
        confirmed_records = 0
        exact_pair_records = 0
        ambiguous_pair_records = 0

        for position, row in enumerate(rows, 1):
            if should_cancel is not None and should_cancel():
                raise InterruptedError("Character Source discovery cancelled")
            if progress is not None and (
                position == 1 or position == total or position % 250 == 0
            ):
                progress(
                    position, total,
                    f"Checking Character Identity and Source evidence ({position:,}/{total:,})",
                )
            values = self._confirmed_tags(row)
            if values:
                confirmed_records += 1
            stable_ids = {
                tag.id
                for value in values
                for tag in (self.knowledge.find(value),)
                if tag is not None
            }
            character_ids = {
                character_id
                for tag_id in stable_ids
                for character_id in character_by_tag.get(tag_id, ())
            }
            source_ids = {
                source_id
                for tag_id in stable_ids
                for source_id in source_by_tag.get(tag_id, ())
            }
            if not character_ids and not source_ids:
                continue
            if len(character_ids) != 1 or len(source_ids) != 1:
                ambiguous_pair_records += 1
                continue
            exact_pair_records += 1
            key = (next(iter(character_ids)), next(iter(source_ids)))
            observations[key] += 1
            path = str(getattr(row, "path", "") or "")
            if path and path not in examples[key] and len(examples[key]) < self.max_examples:
                examples[key].append(path)
            library_id = str(libraries.get(path) or "")
            if library_id:
                scopes[key].add(library_id)

        candidates = []
        existing_links = 0
        existing_aliases = 0
        alias_collisions = 0
        durable_excluded = 0
        for (character_id, source_id), count in observations.items():
            character = self.characters.characters.get(character_id)
            source = self.characters.series.get(source_id)
            if character is None or source is None:
                continue
            character_tag = self.knowledge.tags.get(
                self.characters.entity_tag_id("character", character.id)
            )
            source_tag = self.knowledge.tags.get(
                self.characters.entity_tag_id("series", source.id)
            )
            if character_tag is None or source_tag is None:
                continue
            current_source_id = self.characters.character_source_id(
                "character", character.id,
            )
            current_source = self.characters.series.get(current_source_id)
            common = {
                "character_id": character.id,
                "character_tag_id": character_tag.id,
                "source_id": source.id,
                "source_tag_id": source_tag.id,
                "count": count,
                "paths": examples[(character.id, source.id)],
                "scope": sorted(scopes[(character.id, source.id)]),
            }
            if current_source_id == source.id:
                existing_links += 1
            else:
                source_key = source_relationship_key(
                    "link", character.id, source.id,
                )
                if source_key in durable:
                    durable_excluded += 1
                else:
                    evidence = {"action": "link", **common}
                    candidates.append(CharacterSourceCandidate(
                        _fingerprint((source_key, evidence))[:24], "link", source_key,
                        character.id, character.name, character_tag.id,
                        character_tag.display, source.id, source.name, source_tag.id,
                        source_tag.display, current_source_id,
                        current_source.name if current_source is not None else "",
                        "", count,
                        tuple(sorted(scopes[(character.id, source.id)])),
                        tuple(examples[(character.id, source.id)]),
                        _fingerprint(evidence),
                    ))

            alias = f"{character_tag.display} from {source_tag.display}"
            alias_owner_ids = self.knowledge.identity_owner_ids(alias)
            if alias_owner_ids:
                if alias_owner_ids == (character_tag.id,):
                    existing_aliases += 1
                else:
                    alias_collisions += 1
                continue
            source_key = source_relationship_key(
                "alias", character.id, source.id,
            )
            if source_key in durable:
                durable_excluded += 1
                continue
            evidence = {"action": "alias", "alias": alias, **common}
            candidates.append(CharacterSourceCandidate(
                _fingerprint((source_key, evidence))[:24], "alias", source_key,
                character.id, character.name, character_tag.id,
                character_tag.display, source.id, source.name, source_tag.id,
                source_tag.display, current_source_id,
                current_source.name if current_source is not None else "",
                alias, count,
                tuple(sorted(scopes[(character.id, source.id)])),
                tuple(examples[(character.id, source.id)]),
                _fingerprint(evidence),
            ))

        candidates.sort(key=lambda item: (
            item.character_label.casefold(),
            0 if item.action == "link" else 1,
            -item.observation_count,
            item.source_label.casefold(),
            item.source_key,
        ))
        candidates = [
            replace(item, rank=index)
            for index, item in enumerate(candidates[: self.max_candidates], 1)
        ]
        fingerprint = _fingerprint({
            "snapshot": _json_safe(snapshot_token),
            "records_scanned": total,
            "candidates": [item.evidence_fingerprint for item in candidates],
        })
        return CharacterSourceDiscoveryReport(
            _json_safe(snapshot_token), fingerprint, total, confirmed_records,
            exact_pair_records, ambiguous_pair_records, tuple(candidates),
            existing_links, existing_aliases, alias_collisions,
            durable_excluded, invalid,
        )

    @staticmethod
    def queue_candidates(
        report: CharacterSourceDiscoveryReport,
        reviews: ReviewTaskDatabase,
        *,
        current_snapshot_token,
        selected_candidate_ids: Iterable[str] | None = None,
    ) -> CharacterSourceQueueResult:
        if _json_safe(current_snapshot_token) != _json_safe(report.snapshot_token):
            raise RuntimeError(
                "Indexed Image Knowledge changed after this Character Source preview. "
                "Run discovery again before creating Review questions."
            )
        selected = (
            None if selected_candidate_ids is None else
            {str(value) for value in selected_candidate_ids}
        )
        queued = updated = skipped = 0
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
                        existing.status = "pending"
                    title = (
                        f"Use {candidate.source_label} as the Character Source for "
                        f"{candidate.character_label}?"
                        if candidate.action == "link" else
                        f"Add “{candidate.alias}” as an Other Name?"
                    )
                    task = reviews.add_task(
                        task_type=(
                            SOURCE_LINK_TASK_TYPE
                            if candidate.action == "link" else SOURCE_ALIAS_TASK_TYPE
                        ),
                        title=title,
                        image_path=(candidate.example_paths[0] if candidate.example_paths else ""),
                        description=(
                            "Current indexed confirmed Image Knowledge contains one "
                            "exact reviewed Character Identity and Character Source pair."
                        ),
                        details=candidate.task_details(report.snapshot_token),
                        priority="normal",
                        source_key=candidate.source_key,
                    )
                    if candidate.example_paths:
                        task.image_path = candidate.example_paths[0]
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
        return CharacterSourceQueueResult(
            queued, updated, skipped, tuple(task_ids),
        )


class CharacterSourceReviewService:
    """Apply one reviewed source link or alias with queue recovery markers."""

    def __init__(
        self,
        characters: CharacterDatabase,
        knowledge,
        categories,
        reviews: ReviewTaskDatabase,
        *,
        default_relationship_path: str | Path,
        user_relationship_path: str | Path,
        snapshot_provider: Callable[[], Any] | None = None,
    ) -> None:
        self.characters = characters
        self.knowledge = knowledge
        self.categories = categories
        self.reviews = reviews
        self.default_relationship_path = Path(default_relationship_path)
        self.user_relationship_path = Path(user_relationship_path)
        self.snapshot_provider = snapshot_provider

    def _alias_service(self) -> TagAliasRelationshipService:
        return TagAliasRelationshipService(
            self.knowledge,
            default_path=self.default_relationship_path,
            user_path=self.user_relationship_path,
        )

    def _reload(self) -> TagAliasRelationshipService:
        self.characters.load()
        self.knowledge.load()
        self.categories.load()
        self.reviews.load()
        return self._alias_service()

    def _task(self, task_id: str) -> ReviewTask:
        task = self.reviews.tasks.get(str(task_id))
        if task is None or task.task_type not in SOURCE_TASK_TYPES:
            raise ValueError("The Character Source Review question is no longer available.")
        return task

    def _validate_snapshot(self, task: ReviewTask) -> None:
        if self.snapshot_provider is None:
            return
        if _json_safe(self.snapshot_provider()) != _json_safe(
            task.details.get("evidence_snapshot_token")
        ):
            raise RuntimeError(
                "Indexed Image Knowledge changed after this Character Source "
                "question was prepared. Run Find New Knowledge again."
            )

    def _entities(self, task: ReviewTask):
        expected_action = (
            "link" if task.task_type == SOURCE_LINK_TASK_TYPE else "alias"
        )
        character_id = str(task.details.get("character_id") or "")
        source_id = str(task.details.get("source_id") or "")
        if (
            str(task.details.get("source_action") or "") != expected_action
            or task.source_key != source_relationship_key(
                expected_action, character_id, source_id,
            )
        ):
            raise RuntimeError(
                "The Character Source Review identity changed. Run Find New "
                "Knowledge again."
            )
        character = self.characters.characters.get(character_id)
        source = self.characters.series.get(source_id)
        character_tag = self.knowledge.tags.get(
            str(task.details.get("character_tag_id") or "")
        )
        source_tag = self.knowledge.tags.get(
            str(task.details.get("source_tag_id") or "")
        )
        if any(
            item is None for item in (character, source, character_tag, source_tag)
        ):
            raise RuntimeError(
                "The reviewed Character, Character Source, or SDXL Tag changed. "
                "Run Find New Knowledge again."
            )
        if (
            self.characters.entity_tag_id("character", character.id) != character_tag.id
            or self.characters.entity_tag_id("series", source.id) != source_tag.id
            or self.categories.effective_semantic_role(
                character_tag.category
            ) != "character_identity"
            or self.categories.effective_semantic_role(
                source_tag.category
            ) != "character_source"
        ):
            raise RuntimeError(
                "The reviewed Character Identity or Character Source link changed. "
                "Run Find New Knowledge again."
            )
        expected_alias = f"{character_tag.display} from {source_tag.display}"
        if (
            task.task_type == SOURCE_ALIAS_TASK_TYPE
            and str(task.details.get("alias") or "") != expected_alias
        ):
            raise RuntimeError(
                "The proposed Character/Source Other Name changed. Run Find New "
                "Knowledge again."
            )
        return character, source, character_tag, source_tag, expected_alias

    def _is_applied(
        self, task: ReviewTask, alias_service: TagAliasRelationshipService,
    ) -> bool:
        try:
            character, source, character_tag, _source_tag, alias = self._entities(task)
        except (RuntimeError, ValueError):
            return False
        if task.task_type == SOURCE_LINK_TASK_TYPE:
            return self.characters.character_source_id(
                "character", character.id,
            ) == source.id
        resolution = alias_service.resolve(alias)
        return resolution.owner_ids == (character_tag.id,)

    def _validate_current_authority(self, task: ReviewTask) -> None:
        if task.task_type != SOURCE_LINK_TASK_TYPE:
            return
        character_id = str(task.details.get("character_id") or "")
        source_id = str(task.details.get("source_id") or "")
        expected = str(task.details.get("current_source_id") or "")
        current = self.characters.character_source_id("character", character_id)
        if current not in {expected, source_id}:
            raise RuntimeError(
                "The Character's reviewed Character Source changed after this "
                "question was prepared. Run Find New Knowledge again."
            )

    def _rerank(self, character_id: str) -> int:
        pending = [
            task for task in self.reviews.tasks.values()
            if task.task_type in SOURCE_TASK_TYPES
            and task.status == "pending"
            and str(task.details.get("character_id") or "") == character_id
        ]
        pending.sort(key=lambda task: (
            0 if task.task_type == SOURCE_LINK_TASK_TYPE else 1,
            -int(task.details.get("observation_count") or 0),
            str(task.details.get("source_label") or "").casefold(),
            task.id,
        ))
        changed = 0
        for rank, task in enumerate(pending, 1):
            if int(task.details.get("rank") or 0) != rank:
                task.details["rank"] = rank
                task.updated_at = _now()
                changed += 1
        return changed

    def recover_interrupted(self) -> tuple[CharacterSourceDecisionResult, ...]:
        with self.reviews.transaction():
            self.reviews.load()
            if not any(
                task.task_type in SOURCE_TASK_TYPES and task.status == "applying"
                for task in self.reviews.tasks.values()
            ):
                return ()
            alias_service = self._reload()
            recovered = []
            changed = False
            for task in self.reviews.tasks.values():
                if task.task_type not in SOURCE_TASK_TYPES or task.status != "applying":
                    continue
                applied = self._is_applied(task, alias_service)
                task.status = "resolved" if applied else "pending"
                resolution = dict(task.details.get("resolution") or {})
                resolution.update({
                    "decision": "accepted" if applied else "recovered_for_review",
                    "recovered_at": _now(),
                })
                task.details["resolution"] = resolution
                task.updated_at = _now()
                character_id = str(task.details.get("character_id") or "")
                reranked = self._rerank(character_id)
                recovered.append(CharacterSourceDecisionResult(
                    task.id,
                    "accepted" if applied else "recovered_for_review",
                    str(task.details.get("source_action") or ""),
                    character_id,
                    str(task.details.get("source_id") or ""),
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

    def _apply_link(self, task: ReviewTask) -> Path:
        character, source, _character_tag, source_tag, _alias = self._entities(task)
        result = CharacterRecordEditService(
            self.characters, self.knowledge, self.categories,
        ).update_character(
            character.id,
            name=character.name,
            series_id=source.id,
            identity_type=character.identity_type,
            aliases=character.aliases,
            sdxl_tags=character.sdxl_tags,
            plain_language=character.plain_language,
            original_style_tags=character.original_style_tags,
            origin_label=character.origin_label,
            creator=character.creator,
            source_tag_id=source_tag.id,
            source_tag_explicit=True,
        )
        return result.backup_directory

    def _apply_alias(
        self, task: ReviewTask, alias_service: TagAliasRelationshipService,
    ) -> Path:
        character, source, character_tag, _source_tag, alias = self._entities(task)
        if self.characters.character_source_id("character", character.id) != source.id:
            raise RuntimeError(
                "Review and save this Character Source association before adding "
                "the source-qualified Other Name."
            )
        current = list(alias_service.effective_for_tag(character_tag.id).aliases)
        if normalize_tag(alias) not in {normalize_tag(value) for value in current}:
            current.append(alias)
        result = alias_service.apply((TagAliasChange(
            character_tag.id,
            tuple(current),
            source="km03_character_source_discovery",
            reason="Reviewed current-index Character/Source Other Name",
        ),))
        return result.manifest_path

    def decide(
        self, task_id: str, decision: str,
    ) -> CharacterSourceDecisionResult:
        with self.reviews.transaction():
            decision = str(decision)
            if decision not in {"accepted", "rejected"}:
                raise ValueError("Choose accepted or rejected for this Character Source suggestion.")
            alias_service = self._reload()
            task = self._task(task_id)
            if task.status != "pending":
                raise RuntimeError("This Character Source question is no longer pending.")
            self._validate_snapshot(task)
            character, source, _character_tag, _source_tag, _alias = self._entities(task)
            self._validate_current_authority(task)
            action = str(task.details.get("source_action") or "")
            resolution = {
                "decision": decision,
                "source_action": action,
                "character_id": character.id,
                "source_id": source.id,
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
                reranked = self._rerank(character.id)
                try:
                    self.reviews.save()
                except BaseException:
                    self.reviews.load()
                    raise
                return CharacterSourceDecisionResult(
                    task.id, decision, action, character.id, source.id, reranked,
                )
            if self._is_applied(task, alias_service):
                resolution["decision"] = "already_present"
                task.status = "resolved"
                task.details["resolution"] = resolution
                task.updated_at = _now()
                reranked = self._rerank(character.id)
                self.reviews.save()
                return CharacterSourceDecisionResult(
                    task.id, "already_present", action, character.id, source.id,
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
                backup = (
                    self._apply_link(task)
                    if task.task_type == SOURCE_LINK_TASK_TYPE else
                    self._apply_alias(task, alias_service)
                )
                alias_service = self._reload()
                task = self._task(task_id)
                if not self._is_applied(task, alias_service):
                    raise RuntimeError(
                        "The Character Source decision failed semantic read-back verification."
                    )
                task.status = "resolved"
                task.updated_at = _now()
                reranked = self._rerank(character.id)
                try:
                    self.reviews.save()
                except BaseException:
                    self.reviews.load()
                    raise
            except BaseException:
                alias_service = self._reload()
                task = self._task(task_id)
                if not self._is_applied(task, alias_service):
                    task.status = "pending"
                    task.updated_at = _now()
                    try:
                        self.reviews.save()
                    except Exception:
                        self.reviews.load()
                raise
            return CharacterSourceDecisionResult(
                task.id, decision, action, character.id, source.id, reranked, backup,
            )


__all__ = [
    "SOURCE_LINK_TASK_TYPE",
    "SOURCE_ALIAS_TASK_TYPE",
    "SOURCE_TASK_TYPES",
    "CharacterSourceCandidate",
    "CharacterSourceDiscoveryReport",
    "CharacterSourceQueueResult",
    "CharacterSourceDecisionResult",
    "CharacterSourceDiscoveryService",
    "CharacterSourceReviewService",
    "source_relationship_key",
]
