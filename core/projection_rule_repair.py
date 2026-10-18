from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .automation_repository import AutomationRuleRepository, rule_key_id
from .automation_rules import AutomationEngine
from .operation_journal import OperationJournal
from .rule_intents import TRAINING_PROJECTION, explicit_rule_intent
from .tag_database import TagDatabase


REPAIR_KIND = "projection_rule_overlap_repair"
REPAIR_UNDO_KIND = "projection_rule_overlap_repair_undo"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rule_values(kind: str, record: dict) -> tuple[tuple[str, ...], str]:
    remove = record.get("remove", [])
    remove = [remove] if isinstance(remove, str) else list(remove)
    return (
        tuple(str(value) for value in remove if str(value).strip()),
        str(record.get("replace") or "").strip(),
    )


@dataclass(frozen=True)
class ProjectionRuleRepairCandidate:
    id: str
    storage: str
    kind: str
    before_sources: tuple[str, ...]
    after_sources: tuple[str, ...]
    target_tag: str
    redundant_sources: tuple[str, ...]
    related_rules: tuple[str, ...]
    record_index: int = -1
    rule_id: str = ""

    @property
    def storage_label(self) -> str:
        return "My Automation rules" if self.storage == "automation" else "System Knowledge"

    @property
    def before_summary(self) -> str:
        return f"{' + '.join(self.before_sources)} \N{RIGHTWARDS ARROW} {self.target_tag}"

    @property
    def after_summary(self) -> str:
        return f"{' + '.join(self.after_sources)} \N{RIGHTWARDS ARROW} {self.target_tag}"

    @property
    def explanation(self) -> str:
        removed = ", ".join(self.redundant_sources)
        basis = "; ".join(self.related_rules)
        return (
            f"Remove redundant prerequisite(s): {removed}. "
            f"Existing projection rule(s) already establish that reduction: {basis}."
        )


class ProjectionRuleRepairService:
    """Preview and repair safely composable training-projection rules.

    The service changes rule definitions only. It never reads or writes Image
    Knowledge, captions, Metadata, TXT, raw evidence, or image files.
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
        path = (
            Path(journal_path)
            if journal_path is not None
            else repository.user.path.with_name("operation_journal.jsonl")
        )
        self.journal = OperationJournal(path)

    def _active_rules(self) -> dict:
        rules = copy.deepcopy(self.repository.combined())
        for rule in self.knowledge.merge_rules.values():
            if not rule.enabled:
                continue
            rules["merge_rules"].append(
                {
                    "remove": list(rule.source_tags),
                    "replace": rule.target_tag,
                    "confidence": 1.0 if not rule.require_review else 0.85,
                    "requires_review": bool(rule.require_review),
                    "rule_id": rule.id,
                    "intent": str(
                        rule.extras.get("intent") or TRAINING_PROJECTION
                    ),
                }
            )
        return rules

    def analyze_rule(
        self,
        sources,
        target,
        *,
        exclude_rule_id: str = "",
        exclude_rule_key=None,
    ):
        return AutomationEngine(self._active_rules()).analyze_projection_rule(
            sources,
            target,
            exclude_rule_id=exclude_rule_id,
            exclude_rule_key=exclude_rule_key,
        )

    def candidates(self) -> tuple[ProjectionRuleRepairCandidate, ...]:
        engine = AutomationEngine(self._active_rules())
        result: list[ProjectionRuleRepairCandidate] = []
        for kind in ("merge_rules", "combine_rules"):
            for index, record in enumerate(self.repository.user.data[kind]):
                if explicit_rule_intent(kind, record) != TRAINING_PROJECTION:
                    continue
                sources, target = _rule_values(kind, record)
                analysis = engine.analyze_projection_rule(
                    sources,
                    target,
                    exclude_rule_key=AutomationEngine.projection_rule_key(
                        sources, target
                    ),
                )
                if not analysis.changed or analysis.errors or analysis.warnings:
                    continue
                result.append(
                    ProjectionRuleRepairCandidate(
                        id=(
                            f"automation:{kind}:{index}:"
                            f"{rule_key_id(kind, record)}"
                        ),
                        storage="automation",
                        kind=kind,
                        before_sources=sources,
                        after_sources=analysis.sources,
                        target_tag=target,
                        redundant_sources=analysis.redundant_sources,
                        related_rules=analysis.related_rules,
                        record_index=index,
                    )
                )
        for rule in sorted(
            self.knowledge.merge_rules.values(), key=lambda item: item.id
        ):
            if not rule.enabled:
                continue
            record = rule.to_dict()
            if (
                explicit_rule_intent("merge_rules", record)
                != TRAINING_PROJECTION
            ):
                continue
            analysis = engine.analyze_projection_rule(
                rule.source_tags,
                rule.target_tag,
                exclude_rule_id=rule.id,
            )
            if not analysis.changed or analysis.errors or analysis.warnings:
                continue
            result.append(
                ProjectionRuleRepairCandidate(
                    id=f"system:{rule.id}",
                    storage="system",
                    kind="merge_rules",
                    before_sources=tuple(rule.source_tags),
                    after_sources=analysis.sources,
                    target_tag=rule.target_tag,
                    redundant_sources=analysis.redundant_sources,
                    related_rules=analysis.related_rules,
                    rule_id=rule.id,
                )
            )
        return tuple(result)

    def _candidate_map(self) -> dict[str, ProjectionRuleRepairCandidate]:
        return {candidate.id: candidate for candidate in self.candidates()}

    def apply(self, selected_ids) -> str:
        selected_ids = tuple(dict.fromkeys(map(str, selected_ids)))
        if not selected_ids:
            raise ValueError("Select at least one related rule to repair.")
        current = self._candidate_map()
        missing = [candidate_id for candidate_id in selected_ids if candidate_id not in current]
        if missing:
            raise RuntimeError(
                "Rules changed after this review opened. Reopen the repair and "
                "review the current rules."
            )

        before_user = copy.deepcopy(self.repository.user._payload())
        before_knowledge = self.knowledge._payload()
        before_user_bytes = (
            self.repository.user.path.read_bytes()
            if self.repository.user.path.exists()
            else None
        )
        before_knowledge_bytes = (
            self.knowledge.path.read_bytes()
            if self.knowledge.path.exists()
            else None
        )
        before_user_disk = (
            json.loads(before_user_bytes.decode("utf-8"))
            if before_user_bytes is not None
            else {}
        )
        before_knowledge_disk = (
            json.loads(before_knowledge_bytes.decode("utf-8"))
            if before_knowledge_bytes is not None
            else {
                "schema_version": self.knowledge.SCHEMA_VERSION,
                "tags": [],
                "merge_rules": [],
                "removed_tag_ids": [],
            }
        )
        storages = {current[candidate_id].storage for candidate_id in selected_ids}
        operation_id = self.journal.begin(
            REPAIR_KIND,
            expected=len(storages),
            reason=(
                "Repair safely composable training-projection rules without "
                "changing Image Knowledge"
            ),
            metadata={
                "candidate_ids": list(selected_ids),
                "rules_repaired": len(selected_ids),
            },
        )
        stamp = _utc_now()
        saved_user = saved_knowledge = False
        try:
            for candidate_id in selected_ids:
                candidate = current[candidate_id]
                repair = {
                    "schema_version": 1,
                    "operation_id": operation_id,
                    "repaired_at": stamp,
                    "removed_redundant_sources": list(
                        candidate.redundant_sources
                    ),
                }
                if candidate.storage == "automation":
                    record = self.repository.user.data[candidate.kind][
                        candidate.record_index
                    ]
                    expected_id = (
                        f"automation:{candidate.kind}:{candidate.record_index}:"
                        f"{rule_key_id(candidate.kind, record)}"
                    )
                    if expected_id != candidate.id:
                        raise RuntimeError(
                            "Rules changed after this review opened. Reopen the repair."
                        )
                    record["remove"] = list(candidate.after_sources)
                    record["overlap_repair"] = repair
                else:
                    rule = self.knowledge.merge_rules.get(candidate.rule_id)
                    if (
                        rule is None
                        or tuple(rule.source_tags) != candidate.before_sources
                        or rule.target_tag != candidate.target_tag
                    ):
                        raise RuntimeError(
                            "System Knowledge changed after this review opened. "
                            "Reopen the repair."
                        )
                    rule.source_tags = list(candidate.after_sources)
                    rule.extras["overlap_repair"] = repair

            after_user = copy.deepcopy(self.repository.user._payload())
            after_knowledge = self.knowledge._payload()
            if after_user != before_user:
                self.journal.append_change(
                    operation_id,
                    item=str(self.repository.user.path.resolve()),
                    before=before_user_disk,
                    after=after_user,
                    reason="Removed redundant prerequisites from user projection rules",
                )
            if after_knowledge != before_knowledge:
                self.journal.append_change(
                    operation_id,
                    item=str(self.knowledge.path.resolve()),
                    before=before_knowledge_disk,
                    after=after_knowledge,
                    reason=(
                        "Removed redundant prerequisites from stable-ID System "
                        "Knowledge projection rules"
                    ),
                )
            backup_root = (
                self.repository.user.path.parent
                / "backups"
                / "projection-rule-repair"
            )
            if after_user != before_user:
                self.repository.user.save_verified(backup_root=backup_root)
                saved_user = True
            if after_knowledge != before_knowledge:
                self.knowledge.save_verified(backup_root=backup_root)
                saved_knowledge = True
        except BaseException as exc:
            if saved_user:
                self._restore_bytes(
                    self.repository.user.path, before_user_bytes
                )
            if saved_knowledge:
                self._restore_bytes(self.knowledge.path, before_knowledge_bytes)
            self.repository.load()
            self.knowledge.load()
            self.journal.finish(operation_id, "failed", message=str(exc))
            raise
        self.journal.finish(operation_id, "complete")
        self.journal.prune()
        return operation_id

    def undoable_operation(self):
        return self.journal.latest(kind=REPAIR_KIND, statuses=("complete",))

    def undo_last(self) -> str:
        operation = self.undoable_operation()
        if operation is None:
            raise ValueError("There is no completed related-rule repair to restore.")
        allowed = {
            self.repository.user.path.resolve(),
            self.knowledge.path.resolve(),
        }
        changes = []
        for change in operation.changes:
            path = Path(change["item"]).resolve()
            if path not in allowed:
                raise RuntimeError(
                    "The recorded related-rule repair contains an unexpected file."
                )
            before = self.journal.load_snapshot(change["before"])
            after = self.journal.load_snapshot(change["after"])
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    "A repaired rules file is unavailable or invalid."
                ) from exc
            if current != after:
                raise RuntimeError(
                    "Rules changed after this repair. Review the current rules "
                    "before restoring it."
                )
            changes.append((path, current, before))

        undo_id = self.journal.begin(
            REPAIR_UNDO_KIND,
            expected=len(changes),
            reason="Restore exact rule definitions from before related-rule repair",
            metadata={"restores": operation.id},
        )
        for path, current, before in changes:
            self.journal.append_change(
                undo_id,
                item=str(path),
                before=current,
                after=before,
                reason="Restore exact pre-repair rule payload",
            )
        try:
            for path, _current, before in changes:
                self._write_json(path, before)
            for path, _current, before in changes:
                if json.loads(path.read_text(encoding="utf-8")) != before:
                    raise RuntimeError(
                        "Restored related-rule definitions failed read-back verification."
                    )
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
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    @staticmethod
    def _restore_bytes(path: Path, content: bytes | None) -> None:
        if content is None:
            if path.exists():
                path.unlink()
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(
            path.suffix + f".{uuid4().hex}.restore.tmp"
        )
        temporary.write_bytes(content)
        temporary.replace(path)


__all__ = [
    "ProjectionRuleRepairCandidate",
    "ProjectionRuleRepairService",
    "REPAIR_KIND",
    "REPAIR_UNDO_KIND",
]
