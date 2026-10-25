from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Callable, Iterable

from .annotation import ImageAnnotation
from .automation_repository import active_automation_engine
from .category_registry import CategoryRegistry
from .character_database import CharacterDatabase
from .image_questions import CharacterQuestionIndex
from .image_fact_decisions import ImageFindingDecisionService
from .image_record import ImageRecord
from .operation_journal import OperationJournal
from .tag_collection import TagCollection, normalize_tag
from .typed_boundary import typed_tag_sources


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class GuidedReviewTopic:
    id: str
    label: str
    category_roots: tuple[str, ...] = ()
    optional: bool = False
    character_topic: bool = False


GUIDED_REVIEW_TOPICS = (
    GuidedReviewTopic("character", "Character Identity", character_topic=True),
    GuidedReviewTopic("species", "Species", ("species",)),
    GuidedReviewTopic("subject", "Subject Count / Type", ("image_subject_count",)),
    GuidedReviewTopic("clothing", "Clothing / Outfit", ("clothing",)),
    GuidedReviewTopic("action_pose", "Action / Pose", ("action", "pose")),
    GuidedReviewTopic("scene_location", "Scene / Location", ("scene", "scene_location")),
    GuidedReviewTopic("visual_style", "Visual Style", ("style",), optional=True),
)
GUIDED_REVIEW_TOPIC_BY_ID = {topic.id: topic for topic in GUIDED_REVIEW_TOPICS}
CHARACTER_ROLES = {
    "original_generated": "Original / generated character",
    "generic": "Generic unnamed character",
    "unidentified": "Unidentified character",
}


@dataclass(frozen=True)
class GuidedReviewCandidate:
    kind: str
    value: str
    source: str
    stable_id: str = ""
    confidence: float | None = None


@dataclass(frozen=True)
class GuidedReviewChoice:
    kind: str
    stable_id: str
    label: str
    detail: str = ""


@dataclass(frozen=True)
class GuidedChoicePage:
    choices: tuple[GuidedReviewChoice, ...]
    total: int


@dataclass(frozen=True)
class GuidedReviewEntry:
    image: Path
    topic_id: str
    fingerprint: str
    candidates: tuple[GuidedReviewCandidate, ...] = ()
    existing_assignments: tuple[str, ...] = ()
    deferred: bool = False


@dataclass
class GuidedQueueResult:
    entries: list[GuidedReviewEntry] = field(default_factory=list)
    scanned: int = 0
    errors: int = 0
    already_complete: int = 0
    deferred: int = 0
    cancelled: bool = False


@dataclass(frozen=True)
class GuidedDecisionResult:
    operation_id: str
    image: Path
    topic_id: str
    decision: str
    value: str = ""
    history_operation_id: str = ""
    status: str = "complete"
    version_ids: tuple[str, ...] = ()
    error: str = ""
    txt_verified: bool = False


class GuidedReviewStaleError(RuntimeError):
    pass


class GuidedReviewService:
    """Build and apply reusable per-image topic review decisions.

    Topic completion and candidate rejection deliberately remain different
    assertions.  A broad topic absence is stored under the reviewed topic while
    one rejected finding is stored as an exact per-image candidate decision.
    """

    REVIEW_VERSION = 1

    def __init__(
        self,
        knowledge,
        categories: CategoryRegistry,
        characters: CharacterDatabase,
        journal: OperationJournal,
    ) -> None:
        self.knowledge = knowledge
        self.categories = categories
        self.characters = characters
        self.journal = journal
        self.character_index = CharacterQuestionIndex(characters)

    @staticmethod
    def fingerprint(image: str | Path) -> str:
        image = Path(image)
        sidecar = image.with_suffix(".tags.json")
        try:
            sidecar_bytes=sidecar.read_bytes()
        except OSError:
            sidecar_bytes=None
        return GuidedReviewService._fingerprint_from_bytes(image,sidecar_bytes)

    @staticmethod
    def _fingerprint_from_bytes(image: Path, sidecar_bytes: bytes | None) -> str:
        image=Path(image)
        digest = hashlib.sha256()
        try:
            stat=image.stat()
            digest.update(f"{image}:{stat.st_size}:{stat.st_mtime_ns}:".encode("utf-8"))
        except OSError:
            digest.update(f"{image}:missing:".encode("utf-8"))
        if sidecar_bytes is None:
            digest.update(b"sidecar:missing")
        else:
            # Exact content is the authority-relevant stale guard. Rewriting an
            # identical sidecar does not change the decision being reviewed.
            digest.update(sidecar_bytes)
        return digest.hexdigest()

    @staticmethod
    def _load_record(image: Path) -> tuple[ImageRecord,bytes]:
        image=Path(image); sidecar=image.with_suffix(".tags.json")
        raw=sidecar.read_bytes(); payload=json.loads(raw.decode("utf-8"))
        if not isinstance(payload,dict):
            raise ValueError(f"Invalid sidecar root in {sidecar}")
        record=ImageRecord(image,auto_load=False)
        record.annotation=ImageAnnotation.from_dict(payload,image.name); record.loaded=True
        return record,raw

    def _topic(self, topic_id: str) -> GuidedReviewTopic:
        try:
            return GUIDED_REVIEW_TOPIC_BY_ID[str(topic_id)]
        except KeyError as exc:
            raise ValueError(f"Unknown guided-review topic: {topic_id}") from exc

    def _in_topic(self, category_id: str, topic: GuidedReviewTopic) -> bool:
        path = set(self.categories.path_ids(category_id))
        return bool(path.intersection(topic.category_roots))

    @staticmethod
    def _review_payload(annotation) -> dict:
        source = annotation.source
        payload = source.get("guided_review")
        if not isinstance(payload, dict):
            payload = {"version": GuidedReviewService.REVIEW_VERSION, "topics": {}, "rejected_candidates": []}
            source["guided_review"] = payload
        payload["version"] = GuidedReviewService.REVIEW_VERSION
        if not isinstance(payload.get("topics"), dict):
            payload["topics"] = {}
        if not isinstance(payload.get("rejected_candidates"), list):
            payload["rejected_candidates"] = []
        return payload

    @staticmethod
    def _existing_review_payload(annotation) -> dict:
        payload = annotation.source.get("guided_review")
        return payload if isinstance(payload, dict) else {}

    def _topic_present(self, record: ImageRecord, topic: GuidedReviewTopic) -> bool:
        annotation = record.annotation
        if topic.character_topic:
            return any(item.character_id or item.role for item in annotation.characters)
        if topic.id == "clothing" and any(item.outfit_ids for item in annotation.characters):
            return True
        if topic.id == "visual_style" and (
            annotation.visual_style.relationship != "unknown" or annotation.visual_style.style_tags
        ):
            return True
        for value in annotation.all_structured_tags(
            include_contexts=topic.id != "scene_location",
        ):
            known = self.knowledge.find(value)
            if known is not None and self._in_topic(known.category, topic):
                return True
        return False

    @staticmethod
    def _rejected_candidate_keys(annotation, topic_id: str) -> set[tuple[str, str]]:
        payload = GuidedReviewService._existing_review_payload(annotation)
        result = set()
        for item in payload.get("rejected_candidates", []):
            if not isinstance(item, dict) or str(item.get("topic_id") or "") != topic_id:
                continue
            kind = str(item.get("kind") or "tag")
            identity = str(item.get("stable_id") or "") or normalize_tag(item.get("value", ""))
            if identity:
                result.add((kind, identity))
        return result

    def _tag_candidates(self, record: ImageRecord, topic: GuidedReviewTopic) -> list[GuidedReviewCandidate]:
        present = TagCollection(record.annotation.all_structured_tags()).normalized_set()
        rejected = TagCollection([
            *record.annotation.confirmed_absent,
            *record.annotation.source.get("rejected_tags", []),
        ]).normalized_set()
        explicitly_rejected = self._rejected_candidate_keys(record.annotation, topic.id)
        candidates: list[GuidedReviewCandidate] = []

        def add(value: str, source: str, confidence=None) -> None:
            key = normalize_tag(value)
            known = self.knowledge.find(value)
            if (
                not key or key in present or key in rejected or known is None
                or not self._in_topic(known.category, topic)
                or ("tag", known.id) in explicitly_rejected
                or ("tag", key) in explicitly_rejected
            ):
                return
            candidates.append(GuidedReviewCandidate("tag", known.display, source, known.id, confidence))

        findings = record.annotation.source.get("candidate_findings", [])
        for finding in findings if isinstance(findings, list) else ():
            if not isinstance(finding, dict) or finding.get("kind") != "tag":
                continue
            sources = ", ".join(str(value) for value in finding.get("sources", []) if str(value).strip())
            add(str(finding.get("value") or ""), sources or "Saved source finding")
        for evidence in record.annotation.ai_evidence:
            for prediction in evidence.typed_tags():
                value = str(prediction.get("tag") or "")
                if evidence.decisions.get(normalize_tag(value)) in {"accepted", "rejected", "dismissed"}:
                    continue
                try:
                    confidence = float(prediction.get("confidence")) if prediction.get("confidence") is not None else None
                except (TypeError, ValueError):
                    confidence = None
                add(value, f"AI: {evidence.model_id or 'local model'}", confidence)
        deduplicated: dict[tuple[str, str], GuidedReviewCandidate] = {}
        for item in candidates:
            deduplicated.setdefault((item.kind, item.stable_id or normalize_tag(item.value)), item)
        return list(deduplicated.values())

    def _character_candidates(self, record: ImageRecord) -> list[GuidedReviewCandidate]:
        typed = TagCollection(typed_tag_sources(record, self.knowledge)).normalized_set()
        assigned = {item.character_id for item in record.annotation.characters if item.character_id}
        rejected = self._rejected_candidate_keys(record.annotation, "character")
        result = []
        for question in self.character_index.questions(typed, assigned, record.annotation.plain_language):
            character_id = str(question.values[0] if question.values else "")
            character = self.characters.characters.get(character_id)
            if character is None or ("character", character_id) in rejected:
                continue
            result.append(GuidedReviewCandidate(
                "character", character.name, question.source, character.id, question.confidence,
            ))
        return result

    def _entry_and_status(
        self, image: str | Path, topic_id: str, *, include_deferred: bool = False,
    ) -> tuple[GuidedReviewEntry | None,str]:
        image = Path(image)
        topic = self._topic(topic_id)
        try:
            record,sidecar_bytes = self._load_record(image)
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"Authoritative Image Knowledge is unavailable for {image.name}.") from exc
        payload = self._existing_review_payload(record.annotation)
        topic_review = payload.get("topics", {}).get(topic.id, {}) if isinstance(payload.get("topics"), dict) else {}
        decision = str(topic_review.get("decision") or "") if isinstance(topic_review, dict) else ""
        deferred = decision == "deferred"
        candidates = self._character_candidates(record) if topic.character_topic else self._tag_candidates(record, topic)
        if decision == "confirmed_absent":
            return None,"complete"
        if self._topic_present(record, topic) and not (topic.character_topic and candidates):
            return None,"complete"
        if deferred and not include_deferred:
            return None,"deferred"
        assignments = tuple(
            self.characters.character_label(item.character_id) or CHARACTER_ROLES.get(str(item.role or ""), str(item.role or "Unassigned"))
            for item in record.annotation.characters if item.character_id or item.role
        )
        return GuidedReviewEntry(
            image, topic.id, self._fingerprint_from_bytes(image,sidecar_bytes), tuple(candidates), assignments, deferred,
        ),"queued"

    def entry_for_image(
        self, image: str | Path, topic_id: str, *, include_deferred: bool = False,
    ) -> GuidedReviewEntry | None:
        return self._entry_and_status(image,topic_id,include_deferred=include_deferred)[0]

    def build_queue(
        self,
        images: Iterable[str | Path],
        topic_id: str,
        *,
        include_deferred: bool = False,
        progress: Callable[[int, int, str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> GuidedQueueResult:
        self._topic(topic_id)
        paths = [Path(image) for image in images]
        result = GuidedQueueResult()
        for index, image in enumerate(paths, 1):
            if should_cancel and should_cancel():
                result.cancelled = True
                break
            try:
                entry,status = self._entry_and_status(image, topic_id, include_deferred=include_deferred)
                if entry is None:
                    if status=="deferred":result.deferred += 1
                    else:result.already_complete += 1
                else:
                    result.entries.append(entry)
            except (OSError, ValueError, TypeError):
                result.errors += 1
            result.scanned += 1
            if progress:
                progress(index, len(paths), image.name)
        return result

    def search_choices(self, topic_id: str, query: str = "", *, limit: int = 100) -> GuidedChoicePage:
        topic = self._topic(topic_id)
        key = normalize_tag(query)
        choices: list[GuidedReviewChoice] = []
        if topic.character_topic:
            for record in self.characters.search_characters(query):
                source_id = self.characters.character_source_id("character", record.id)
                source = self.characters.series.get(source_id) if source_id else None
                identity = {
                    "canonical": "Known character", "original": "Reviewed original character",
                    "generic": "Generic character", "unknown": "Identity not classified",
                }.get(record.identity_type, record.identity_type.replace("_", " ").title())
                detail = f"Character Source: {source.name if source else 'No Character Source'} • {identity}"
                choices.append(GuidedReviewChoice("character", record.id, record.name, detail))
        else:
            for record in self.knowledge.tags.values():
                if not self._in_topic(record.category, topic):
                    continue
                haystack = " ".join(self.knowledge.identity_names(record)).casefold()
                if key and key not in normalize_tag(haystack):
                    continue
                choices.append(GuidedReviewChoice(
                    "tag", record.id, record.display, self.categories.path_label(record.category),
                ))
            if topic.id == "clothing":
                for outfit in self.characters.outfits.values():
                    source_id = self.characters.character_source_id("outfit", outfit.id)
                    source = self.characters.series.get(source_id) if source_id else None
                    associated = [
                        self.characters.characters[item].name
                        for item in self.characters.associated_character_ids(outfit.id)
                        if item in self.characters.characters
                    ]
                    haystack = " ".join([outfit.name,*outfit.aliases,*outfit.sdxl_tags,*associated,source.name if source else ""])
                    if key and key not in normalize_tag(haystack):
                        continue
                    if associated:
                        association = f"Character outfit • Associated with: {', '.join(associated)}"
                    else:
                        association = "Generic outfit • No assigned character"
                    detail = f"{association} • Character Source: {source.name if source else 'No Character Source'}"
                    choices.append(GuidedReviewChoice("outfit",outfit.id,outfit.name,detail))
            choices.sort(key=lambda item: (normalize_tag(item.label) != key, item.label.casefold(), item.stable_id))
        total = len(choices)
        return GuidedChoicePage(tuple(choices[:max(1, int(limit))]), total)

    def preview_conflicts(self, entry: GuidedReviewEntry, choice: GuidedReviewChoice) -> list[str]:
        if choice.kind != "tag":
            return []
        record = ImageRecord(entry.image)
        tag = self.knowledge.tags.get(choice.stable_id)
        if tag is None:
            return []
        before = record.annotation.all_structured_tags()
        return active_automation_engine(self.knowledge).conflict_warnings(
            entry.image, before, [*before, tag.display],
        )

    def _verify_entry(self, entry: GuidedReviewEntry) -> None:
        if self.fingerprint(entry.image) != entry.fingerprint:
            raise GuidedReviewStaleError(
                "The image or its Image Knowledge changed after this queue was built. Refresh the current item before saving a decision."
            )

    def _restore_snapshot(self, image: Path, snapshot: dict) -> None:
        """Restore the exact sidecar state and its derived TXT projection."""
        restored = ImageRecord(image)
        restored.annotation = ImageAnnotation.from_dict(copy.deepcopy(snapshot), image.name)
        restored.modified = True
        restored.save_sidecar(force=True)
        restored.export_caption(knowledge=self.knowledge, force=True)
        # Caption export may update projection bookkeeping. The journal snapshot
        # is authoritative for recovery, so persist it again after rebuilding TXT.
        restored.annotation = ImageAnnotation.from_dict(copy.deepcopy(snapshot), image.name)
        restored.modified = True
        restored.save_sidecar(force=True)
        if ImageRecord(image).annotation.to_dict() != snapshot:
            raise RuntimeError("The restored Image Knowledge did not match the journaled snapshot.")

    def _mutate(
        self,
        entry: GuidedReviewEntry,
        decision: str,
        value: str,
        mutate: Callable[[ImageRecord], None],
        *, history_operation=None, should_cancel=None,
    ) -> GuidedDecisionResult:
        if history_operation is not None:
            return self._mutate_with_history(entry, decision, value, mutate,
                history_operation, should_cancel=should_cancel)
        self._verify_entry(entry)
        record = ImageRecord(entry.image)
        # ImageAnnotation intentionally preserves nested forward-compatible source
        # fields.  Keep transaction snapshots independent from later in-memory
        # mutation rather than relying on its shallow presentation copy.
        before = copy.deepcopy(record.annotation.to_dict())
        operation_id = self.journal.begin(
            "guided_review", expected=1, reason=f"Guided {entry.topic_id}: {decision}",
            metadata={"image": str(entry.image), "topic_id": entry.topic_id, "decision": decision},
        )
        mutate(record)
        record.annotation.source["latest_operation"] = {"id": operation_id, "kind": "guided_review"}
        record.modified = True
        try:
            record.save_sidecar(force=True, knowledge=self.knowledge)
            record.export_caption(knowledge=self.knowledge, force=True)
            after = copy.deepcopy(record.annotation.to_dict())
            if ImageRecord(entry.image).annotation.to_dict() != after:
                raise RuntimeError("The saved Image Knowledge did not match the reviewed decision.")
            self.journal.append_change(
                operation_id, item=str(entry.image), before=before, after=after,
                reason=f"{entry.topic_id}: {decision}", metadata={"topic_id": entry.topic_id, "decision": decision},
            )
            self.journal.finish(operation_id)
        except Exception as exc:
            try:
                self._restore_snapshot(entry.image, before)
            except Exception as rollback_exc:
                self.journal.finish(operation_id, "failed", message=f"{exc}; rollback failed: {rollback_exc}")
                raise RuntimeError(f"The guided decision failed and automatic recovery also failed: {rollback_exc}") from exc
            self.journal.finish(operation_id, "failed", message=str(exc))
            raise
        self.journal.prune()
        return GuidedDecisionResult(operation_id, entry.image, entry.topic_id, decision, value)

    def _mutate_with_history(self, entry, decision, value, mutate, operation, *, should_cancel=None):
        from .history_workflow import HistoryCaptureCancelled
        from .history_versions import HistoryError
        from .image_knowledge_history import HistoryWriteInterrupted

        versions = []
        phase = "admission"
        txt_verified = False
        try:
            if should_cancel is not None and should_cancel():
                raise HistoryCaptureCancelled("Stopped before this image decision was saved.")
            context = operation.context_for(entry.image)
            if context is None or context.operation_id != operation.operation_id:
                raise HistoryError("The exact guided-review history operation is unavailable.")
            with context.locked(entry.image):
                if context.store.has_pending(context.entity):
                    raise HistoryWriteInterrupted("This image needs saved-history recovery before another guided decision.")
                self._verify_entry(entry)
                record = ImageRecord(entry.image, history_context=context)
                mutate(record)
                record.annotation.source["latest_operation"] = {"id": operation.operation_id, "kind": "guided_review"}
                record.modified = True

                def receipt():
                    head = context.store.current_head(context.entity)
                    if head is None or head.operation_id != operation.operation_id:
                        raise HistoryWriteInterrupted("The exact saved guided-review version could not be verified.")
                    if head.version_id not in versions:
                        versions.append(head.version_id)
                    if ImageRecord(entry.image).annotation.to_dict() != record.annotation.to_dict():
                        raise HistoryWriteInterrupted("Saved guided-review Image Knowledge did not match read-back.")

                phase = "facts"
                record.save_sidecar(force=True, knowledge=self.knowledge)
                receipt()
                # Finish the selected image's projection even if cancellation arrives
                # after its fact commit. History restores Image Knowledge, not TXT bytes.
                phase = "projection"
                record.export_caption(knowledge=self.knowledge, character_db=self.characters, force=True)
                receipt()
                txt_verified = True
        except Exception as exc:
            status = (
                "cancelled" if isinstance(exc, HistoryCaptureCancelled) else
                "stale" if isinstance(exc, GuidedReviewStaleError) else
                "interrupted" if isinstance(exc, HistoryWriteInterrupted) or phase == "facts" else
                "projection_failed" if phase == "projection" else "held"
            )
            message = str(exc)
            if versions:
                message = "The saved image decision is retained. " + message
            if status == "projection_failed":
                message += " Training TXT is not verified; open this exact image in the editor before continuing."
            elif status == "interrupted":
                message += " Review this exact image's saved history and recovery before continuing."
            return GuidedDecisionResult("", entry.image, entry.topic_id, decision, value,
                operation.operation_id, status, tuple(versions), message, txt_verified)
        return GuidedDecisionResult("", entry.image, entry.topic_id, decision, value,
            operation.operation_id, "complete", tuple(versions), "", txt_verified)

    @staticmethod
    def _record_topic(record: ImageRecord, topic_id: str, decision: str, **values) -> None:
        payload = GuidedReviewService._review_payload(record.annotation)
        payload["topics"][topic_id] = {
            "decision": decision, "reviewed_at": _utc_now(),
            "provenance": "user_guided_review", **values,
        }

    def outfit_owner_choices(self, image: str | Path, outfit_id: str) -> tuple[tuple[str,str], ...]:
        if str(outfit_id) not in self.characters.outfits:
            return ()
        return self._outfit_owner_choices(ImageRecord(Path(image)), outfit_id)

    def _outfit_owner_choices(self, record: ImageRecord, outfit_id: str) -> tuple[tuple[str,str], ...]:
        outfit=self.characters.outfits.get(str(outfit_id))
        if outfit is None:
            return ()
        result=[]
        for assignment in record.annotation.characters:
            label=self.characters.character_label(assignment.character_id) or CHARACTER_ROLES.get(str(assignment.role or ""),f"Character slot {assignment.slot}")
            result.append((f"slot:{assignment.slot}",label))
        assigned={item.character_id for item in record.annotation.characters if item.character_id}
        for character_id in self.characters.associated_character_ids(outfit.id):
            if character_id in assigned or character_id not in self.characters.characters:
                continue
            result.append((f"character:{character_id}",f"Add assignment: {self.characters.character_label(character_id)}"))
        if not any(item.role=="generic" and not item.character_id for item in record.annotation.characters):
            result.append(("role:generic","Add assignment: Generic unnamed character"))
        return tuple(result)

    def apply_choice(
        self, entry: GuidedReviewEntry, choice: GuidedReviewChoice, *, outfit_owner: str = "",
        **options,
    ) -> GuidedDecisionResult:
        topic = self._topic(entry.topic_id)
        if choice.kind == "character":
            if not topic.character_topic or choice.stable_id not in self.characters.characters:
                raise ValueError("The selected character is not available in this topic.")

            def assign(record: ImageRecord) -> None:
                if not any(item.character_id == choice.stable_id for item in record.annotation.characters):
                    record.annotation.add_character(character_id=choice.stable_id)
                self._record_topic(record, topic.id, "assigned_character", stable_id=choice.stable_id, value=choice.label)

            return self._mutate(entry, "assigned_character", choice.label, assign, **options)
        if choice.kind == "outfit":
            outfit=self.characters.outfits.get(choice.stable_id)
            if topic.id!="clothing" or outfit is None:
                raise ValueError("The selected outfit is not available in this topic.")
            if options.get("history_operation") is None and outfit_owner not in dict(self.outfit_owner_choices(entry.image,outfit.id)):
                raise ValueError("Choose who is wearing this outfit. Open the universal editor when the correct subject is not available.")

            def assign_outfit(record: ImageRecord) -> None:
                if outfit_owner not in dict(self._outfit_owner_choices(record, outfit.id)):
                    raise ValueError("The selected outfit owner is no longer available.")
                if outfit_owner.startswith("slot:"):
                    assignment=record.annotation.get_character(int(outfit_owner.split(":",1)[1]))
                elif outfit_owner.startswith("character:"):
                    character_id=outfit_owner.split(":",1)[1]
                    assignment=next((item for item in record.annotation.characters if item.character_id==character_id),None)
                    if assignment is None: assignment=record.annotation.add_character(character_id=character_id)
                else:
                    assignment=next((item for item in record.annotation.characters if item.role=="generic" and not item.character_id),None)
                    if assignment is None: assignment=record.annotation.add_character(role="generic")
                if assignment is None:
                    raise ValueError("The selected outfit owner is no longer available.")
                assignment.outfit_ids=TagCollection([*assignment.outfit_ids,outfit.id]).to_list()
                self._record_topic(record,topic.id,"assigned_outfit",stable_id=outfit.id,value=outfit.name,wearer_slot=assignment.slot,wearer_character_id=assignment.character_id or "",wearer_role=assignment.role or "")

            return self._mutate(entry,"assigned_outfit",outfit.name,assign_outfit, **options)
        tag = self.knowledge.tags.get(choice.stable_id)
        if choice.kind != "tag" or tag is None or not self._in_topic(tag.category, topic):
            raise ValueError("The selected SDXL Tag is outside this review topic.")

        def add_tag(record: ImageRecord) -> None:
            present = TagCollection(record.annotation.all_structured_tags()).normalized_set()
            if normalize_tag(tag.display) not in present:
                record.confirm_sdxl_tags([tag.display], knowledge=self.knowledge)
            self._record_topic(record, topic.id, "added_known_tag", stable_id=tag.id, value=tag.display)

        return self._mutate(entry, "added_known_tag", tag.display, add_tag, **options)

    def assign_character_role(self, entry: GuidedReviewEntry, role: str, **options) -> GuidedDecisionResult:
        if entry.topic_id != "character" or role not in CHARACTER_ROLES:
            raise ValueError("Choose a supported image-level character identity type.")

        def assign(record: ImageRecord) -> None:
            if not any(item.role == role and not item.character_id for item in record.annotation.characters):
                record.annotation.add_character(role=role)
            record.annotation.source["character_type"] = CHARACTER_ROLES[role]
            self._record_topic(record, "character", "assigned_character_role", role=role, value=CHARACTER_ROLES[role])

        return self._mutate(entry, "assigned_character_role", CHARACTER_ROLES[role], assign, **options)

    def confirm_absent(self, entry: GuidedReviewEntry, **options) -> GuidedDecisionResult:
        def confirm(record: ImageRecord) -> None:
            self._record_topic(record, entry.topic_id, "confirmed_absent")

        return self._mutate(entry, "confirmed_absent", "", confirm, **options)

    def defer(self, entry: GuidedReviewEntry, **options) -> GuidedDecisionResult:
        def defer_topic(record: ImageRecord) -> None:
            self._record_topic(record, entry.topic_id, "deferred")

        return self._mutate(entry, "deferred", "", defer_topic, **options)

    def reject_candidate(
        self, entry: GuidedReviewEntry, candidate: GuidedReviewCandidate,
        **options,
    ) -> GuidedDecisionResult:
        if candidate not in entry.candidates:
            raise ValueError("The selected proposal is no longer part of this queue item.")

        def reject(record: ImageRecord) -> None:
            payload = self._review_payload(record.annotation)
            identity = candidate.stable_id or normalize_tag(candidate.value)
            kept = [
                item for item in payload["rejected_candidates"]
                if not (
                    isinstance(item, dict) and item.get("topic_id") == entry.topic_id
                    and item.get("kind") == candidate.kind
                    and (item.get("stable_id") or normalize_tag(item.get("value", ""))) == identity
                )
            ]
            kept.append({
                "topic_id": entry.topic_id, "kind": candidate.kind,
                "stable_id": candidate.stable_id, "value": candidate.value,
                "source": candidate.source, "reviewed_at": _utc_now(),
            })
            payload["rejected_candidates"] = kept
            if candidate.kind == "tag":
                ImageFindingDecisionService(
                    record.annotation,knowledge=self.knowledge,
                ).ignore_for_image(tags=(candidate.value,))

        return self._mutate(entry, "rejected_candidate", candidate.value, reject, **options)

    def undo(self, operation_id: str) -> Path:
        operation = next((item for item in self.journal.operations() if item.id == operation_id), None)
        if operation is None or operation.kind != "guided_review" or operation.status != "complete" or len(operation.changes) != 1:
            raise ValueError("That guided-review decision is not available to undo.")
        change = operation.changes[0]
        image = Path(change["item"])
        before = self.journal.load_snapshot(change["before"])
        after = self.journal.load_snapshot(change["after"])
        current = ImageRecord(image)
        if current.annotation.to_dict() != after:
            raise GuidedReviewStaleError(
                "Image Knowledge changed after this guided decision, so it cannot be undone automatically. Open the universal editor to review the current record."
            )
        try:
            self._restore_snapshot(image, before)
        except Exception as exc:
            try:
                self._restore_snapshot(image, after)
            except Exception as rollback_exc:
                raise RuntimeError(
                    f"Undo failed and automatic recovery also failed: {rollback_exc}"
                ) from exc
            raise RuntimeError(
                "Undo could not be completed. The guided decision was restored, so Image Knowledge remains unchanged."
            ) from exc
        self.journal.finish(operation_id, "undone")
        return image
