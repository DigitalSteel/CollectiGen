from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping
from uuid import uuid4

from .automation_repository import AutomationRuleRepository, rule_key_id
from .automation_rules import AutomationEngine, DEFAULT_RULES
from .operation_journal import OperationJournal
from .rule_intents import (
    IMAGE_CORRECTION_REVIEW,
    REJECT_INCOMING_EVIDENCE,
    RULE_INTENT_LABELS,
    TRAINING_PROJECTION,
    allowed_rule_intents,
    explicit_rule_intent,
    suggested_rule_intent,
)
from .tag_collection import TagCollection, normalize_tag
from .tag_database import TagDatabase


MIGRATION_KIND = "automation_rule_intent_migration"
MIGRATION_UNDO_KIND = "automation_rule_intent_migration_undo"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rule_values(kind: str, record: dict) -> tuple[tuple[str, ...], str]:
    if kind == "ignore_rules":
        tag = str(record.get("tag") or "").strip()
        return ((tag,) if tag else ()), ""
    if kind == "wildcard_rules":
        remove = record.get("remove", [])
        remove = [remove] if isinstance(remove, str) else list(remove)
        pattern = str(record.get("pattern") or "").strip()
        return tuple(value for value in (pattern, *map(str, remove)) if value), pattern
    remove = record.get("remove", [])
    remove = [remove] if isinstance(remove, str) else list(remove)
    return tuple(str(value) for value in remove if str(value).strip()), str(record.get("replace") or "").strip()


@dataclass(frozen=True)
class RuleIntentCandidate:
    id: str
    storage: str
    kind: str
    source_tags: tuple[str, ...]
    target_tag: str
    suggested_intent: str
    allowed_intents: tuple[str, ...]
    record_index: int = -1

    @property
    def rule_summary(self) -> str:
        if self.kind == "ignore_rules":
            return self.source_tags[0] if self.source_tags else "(empty rule)"
        source = " + ".join(self.source_tags)
        return f"{source} → {self.target_tag}" if self.target_tag else source

    @property
    def storage_label(self) -> str:
        return "My Automation rules" if self.storage == "automation" else "System Knowledge"


@dataclass(frozen=True)
class RuleIntentPreview:
    candidate: RuleIntentCandidate
    intent: str
    image_knowledge_before: tuple[str, ...]
    image_knowledge_after: tuple[str, ...]
    projection_before: tuple[str, ...]
    projection_after: tuple[str, ...]
    incoming_evidence_before: tuple[str, ...]
    incoming_evidence_after: tuple[str, ...]
    explanation: str


class RuleIntentMigrationService:
    """Preview, classify, and restore legacy optimization-rule intent.

    This service never reads or writes Image Knowledge. Its examples are built
    from the rule's own reviewed SDXL Tags, and every migrated record retains
    its existing stable identity and payload.
    """

    def __init__(
        self,
        repository: AutomationRuleRepository,
        knowledge: TagDatabase,
        *,
        journal_path: str | Path | None = None,
    ) -> None:
        self.repository = repository
        self.knowledge = knowledge
        path = Path(journal_path) if journal_path is not None else repository.user.path.with_name("operation_journal.jsonl")
        self.journal = OperationJournal(path)

    def candidates(self) -> tuple[RuleIntentCandidate, ...]:
        result: list[RuleIntentCandidate] = []
        for kind in ("merge_rules", "wildcard_rules", "combine_rules", "ignore_rules"):
            for index, record in enumerate(self.repository.user.data[kind]):
                if explicit_rule_intent(kind, record):
                    continue
                sources, target = _rule_values(kind, record)
                result.append(RuleIntentCandidate(
                    f"automation:{rule_key_id(kind, record)}", "automation", kind,
                    sources, target, suggested_rule_intent(kind),
                    allowed_rule_intents(kind), index,
                ))
        for rule in sorted(self.knowledge.merge_rules.values(), key=lambda item: item.id):
            record = rule.to_dict()
            if explicit_rule_intent("merge_rules", record):
                continue
            result.append(RuleIntentCandidate(
                f"system:{rule.id}", "system", "merge_rules",
                tuple(rule.source_tags), rule.target_tag, TRAINING_PROJECTION,
                allowed_rule_intents("merge_rules"),
            ))
        return tuple(result)

    def preview(self, candidate: RuleIntentCandidate, intent: str) -> RuleIntentPreview:
        if intent not in candidate.allowed_intents:
            raise ValueError("Choose one of the meanings available for this rule.")
        facts = TagCollection(candidate.source_tags).to_list()
        if candidate.target_tag and normalize_tag(candidate.target_tag) not in TagCollection(facts).normalized_set():
            facts.append(candidate.target_tag)
        image_before = tuple(facts)
        image_after = image_before
        projection_before = image_before
        projection_after = projection_before
        incoming_before = tuple(candidate.source_tags)
        incoming_after = incoming_before

        if intent == TRAINING_PROJECTION:
            if candidate.kind == "ignore_rules":
                record = {"tag": candidate.source_tags[0], "intent": intent}
            elif candidate.kind == "wildcard_rules":
                record = {
                    "pattern": candidate.target_tag,
                    "remove": list(candidate.source_tags[1:]),
                    "intent": intent,
                }
            else:
                record = {
                    "remove": list(candidate.source_tags),
                    "replace": candidate.target_tag,
                    "intent": intent,
                }
            rules = copy.deepcopy(DEFAULT_RULES)
            rules[candidate.kind] = [record]
            projection_after = tuple(AutomationEngine(rules).project_tags(projection_before)[0])
            explanation = (
                "This rule may shorten an exported training caption. Complete confirmed "
                "Image Knowledge and incoming evidence remain unchanged."
            )
        elif intent == REJECT_INCOMING_EVIDENCE:
            rejected = normalize_tag(candidate.source_tags[0]) if candidate.source_tags else ""
            incoming_after = tuple(
                value for value in incoming_before if normalize_tag(value) != rejected
            )
            explanation = (
                "This exact incoming observation is suppressed before confirmation. "
                "An already confirmed fact remains confirmed and training output is unchanged."
            )
        else:
            explanation = (
                "A match becomes an image-correction review finding. It never removes a "
                "confirmed fact or changes training output automatically."
            )
        return RuleIntentPreview(
            candidate, intent, image_before, image_after, projection_before,
            projection_after, incoming_before, incoming_after, explanation,
        )

    def _candidate_map(self) -> dict[str, RuleIntentCandidate]:
        return {candidate.id: candidate for candidate in self.candidates()}

    def apply(self, selections: Mapping[str, str]) -> str:
        if not selections:
            raise ValueError("Select at least one legacy rule to migrate.")
        current = self._candidate_map()
        missing = [rule_id for rule_id in selections if rule_id not in current]
        if missing:
            raise RuntimeError("Rules changed after this review opened. Reopen the migration and review the current rules.")
        for rule_id, intent in selections.items():
            if intent not in current[rule_id].allowed_intents:
                raise ValueError("One selected rule has an unsupported meaning.")

        before_user = copy.deepcopy(self.repository.user._payload())
        before_knowledge = self.knowledge._payload()
        before_user_bytes = self.repository.user.path.read_bytes() if self.repository.user.path.exists() else None
        before_knowledge_bytes = self.knowledge.path.read_bytes() if self.knowledge.path.exists() else None
        before_user_disk = (
            json.loads(before_user_bytes.decode("utf-8"))
            if before_user_bytes is not None
            else {}
        )
        before_knowledge_disk = (
            json.loads(before_knowledge_bytes.decode("utf-8"))
            if before_knowledge_bytes is not None
            else {"schema_version": self.knowledge.SCHEMA_VERSION, "tags": [], "merge_rules": []}
        )
        operation_id = self.journal.begin(
            MIGRATION_KIND,
            expected=len({current[rule_id].storage for rule_id in selections}),
            reason="Classify legacy rule intent without changing Image Knowledge",
            metadata={"rule_ids": list(selections), "rules_classified": len(selections)},
        )
        stamp = _utc_now()
        saved_user = saved_knowledge = False
        try:
            for rule_id, intent in selections.items():
                candidate = current[rule_id]
                migration = {
                    "schema_version": 1,
                    "operation_id": operation_id,
                    "classified_at": stamp,
                    "previous_intent": "",
                }
                if candidate.storage == "automation":
                    record = self.repository.user.data[candidate.kind][candidate.record_index]
                    if f"automation:{rule_key_id(candidate.kind, record)}" != candidate.id:
                        raise RuntimeError("Rules changed after this review opened. Reopen the migration.")
                    record["intent"] = intent
                    record["intent_migration"] = migration
                else:
                    rule = self.knowledge.merge_rules.get(candidate.id.removeprefix("system:"))
                    if rule is None:
                        raise RuntimeError("System Knowledge changed after this review opened. Reopen the migration.")
                    rule.extras["intent"] = intent
                    rule.extras["intent_migration"] = migration

            after_user = copy.deepcopy(self.repository.user._payload())
            after_knowledge = self.knowledge._payload()
            if after_user != before_user:
                self.journal.append_change(
                    operation_id, item=str(self.repository.user.path.resolve()),
                    before=before_user_disk, after=after_user,
                    reason="Classified user Automation rule intent",
                    metadata={"existed_before": before_user_bytes is not None},
                )
            if after_knowledge != before_knowledge:
                self.journal.append_change(
                    operation_id, item=str(self.knowledge.path.resolve()),
                    before=before_knowledge_disk, after=after_knowledge,
                    reason="Classified stable-ID System Knowledge merge intent",
                    metadata={"existed_before": before_knowledge_bytes is not None},
                )
            backup_root = self.repository.user.path.parent / "backups" / "rule-intent-migration"
            if after_user != before_user:
                self.repository.user.save_verified(backup_root=backup_root)
                saved_user = True
            if after_knowledge != before_knowledge:
                self.knowledge.save_verified(backup_root=backup_root)
                saved_knowledge = True
        except BaseException as exc:
            if saved_user:
                self._restore_bytes(self.repository.user.path, before_user_bytes)
            if saved_knowledge:
                self._restore_bytes(self.knowledge.path, before_knowledge_bytes)
            self.repository.load()
            self.knowledge.load()
            self.journal.finish(operation_id, "failed", message=str(exc))
            raise
        self.journal.finish(operation_id, "complete")
        self.journal.prune()
        return operation_id

    def recover_interrupted(self) -> tuple[str, ...]:
        """Restore exact pre-migration rules left by an interrupted process.

        Recovery never guesses. Every recorded path and snapshot is validated
        before the first write, and a file that differs from both recorded
        states is left untouched for explicit user review.
        """
        allowed = {
            self.repository.user.path.resolve(),
            self.knowledge.path.resolve(),
        }
        recovered: list[str] = []
        operations = tuple(
            operation
            for operation in self.journal.operations()
            if operation.kind == MIGRATION_KIND and operation.status == "applying"
        )
        for operation in operations:
            restores: list[tuple[Path, dict, dict, bool]] = []
            for change in operation.changes:
                path = Path(change["item"]).resolve()
                if path not in allowed:
                    raise RuntimeError(
                        "An interrupted rule-intent migration contains an unexpected file."
                    )
                try:
                    before = self.journal.load_snapshot(change["before"])
                    after = self.journal.load_snapshot(change["after"])
                except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
                    raise RuntimeError(
                        "An interrupted rule-intent migration is missing a valid recovery snapshot."
                    ) from exc
                if not isinstance(before, dict) or not isinstance(after, dict):
                    raise RuntimeError(
                        "An interrupted rule-intent migration has an invalid recovery snapshot."
                    )
                existed_before = bool(
                    change.get("metadata", {}).get("existed_before", True)
                )
                if not path.exists():
                    if existed_before:
                        raise RuntimeError(
                            "A rules file needed for interrupted migration recovery is missing."
                        )
                    current = None
                else:
                    try:
                        current = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError) as exc:
                        raise RuntimeError(
                            "A rules file needed for interrupted migration recovery is unavailable or invalid."
                        ) from exc
                if current == before or (current is None and not existed_before):
                    continue
                if current != after:
                    raise RuntimeError(
                        "Rules changed after an interrupted migration. Recovery refused to overwrite them."
                    )
                restores.append((path, before, after, existed_before))

            restored: list[tuple[Path, dict, dict, bool]] = []
            try:
                for path, before, after, existed_before in restores:
                    if existed_before:
                        self._write_json(path, before)
                    else:
                        path.unlink()
                    restored.append((path, before, after, existed_before))
                for path, before, _after, existed_before in restores:
                    if not existed_before:
                        if path.exists():
                            raise RuntimeError(
                                "Interrupted migration recovery could not remove a newly created rules file."
                            )
                        continue
                    if json.loads(path.read_text(encoding="utf-8")) != before:
                        raise RuntimeError(
                            "Interrupted migration recovery failed read-back verification."
                        )
            except BaseException:
                for path, _before, after, _existed_before in reversed(restored):
                    self._write_json(path, after)
                raise

            self.journal.finish(
                operation.id,
                "failed",
                message=(
                    "Recovered exact pre-migration rules after an interrupted process."
                ),
            )
            recovered.append(operation.id)

        if recovered:
            self.repository.load()
            self.knowledge.load()
            self.journal.prune()
        return tuple(recovered)

    def undoable_operation(self):
        return self.journal.latest(kind=MIGRATION_KIND, statuses=("complete",))

    def undo_last(self) -> str:
        operation = self.undoable_operation()
        if operation is None:
            raise ValueError("There is no completed rule-intent migration to restore.")
        allowed = {
            self.repository.user.path.resolve(),
            self.knowledge.path.resolve(),
        }
        changes = []
        for change in operation.changes:
            path = Path(change["item"]).resolve()
            if path not in allowed:
                raise RuntimeError("The recorded migration contains an unexpected file.")
            before = self.journal.load_snapshot(change["before"])
            after = self.journal.load_snapshot(change["after"])
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("A migrated rules file is unavailable or invalid.") from exc
            if current != after:
                raise RuntimeError(
                    "Rules changed after this migration. Review the current rules before restoring it."
                )
            changes.append((path, current, before))

        undo_id = self.journal.begin(
            MIGRATION_UNDO_KIND,
            expected=len(changes),
            reason="Restore rule intent from before the last migration",
            metadata={"restores": operation.id},
        )
        for path, current, before in changes:
            self.journal.append_change(
                undo_id, item=str(path), before=current, after=before,
                reason="Restore exact pre-migration rule payload",
            )
        try:
            for path, _current, before in changes:
                self._write_json(path, before)
            for path, _current, before in changes:
                if json.loads(path.read_text(encoding="utf-8")) != before:
                    raise RuntimeError("Restored rule intent failed read-back verification.")
        except BaseException as exc:
            for path, current, _before in changes:
                self._write_json(path, current)
            self.journal.finish(undo_id, "failed", message=str(exc))
            raise
        self.repository.load()
        self.knowledge.load()
        self.journal.finish(undo_id, "complete")
        self.journal.finish(operation.id, "undone")
        self.journal.prune()
        return operation.id

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _restore_bytes(path: Path, content: bytes | None) -> None:
        if content is None:
            if path.exists():
                path.unlink()
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + f".{uuid4().hex}.restore.tmp")
        temporary.write_bytes(content)
        temporary.replace(path)


__all__ = [
    "IMAGE_CORRECTION_REVIEW",
    "MIGRATION_KIND",
    "REJECT_INCOMING_EVIDENCE",
    "RULE_INTENT_LABELS",
    "RuleIntentCandidate",
    "RuleIntentMigrationService",
    "RuleIntentPreview",
    "TRAINING_PROJECTION",
]
