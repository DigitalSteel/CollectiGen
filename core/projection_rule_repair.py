from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .automation_repository import AutomationRuleRepository, rule_key_id
from .automation_rules import AutomationEngine, AUTOMATION_RULE_SCHEMA_VERSION
from .history_versions import MAX_SNAPSHOT_BYTES, _reject_reparse
from .operation_journal import OperationJournal
from .rule_intents import TRAINING_PROJECTION, explicit_rule_intent
from .tag_database import TagDatabase, TagRecord
from .tag_storage_decode import KnowledgeLifecycleCodec, decode_tag_storage


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


@dataclass(frozen=True)
class _RepairAuthority:
    path: str
    source: bytes
    payload: str


@dataclass(frozen=True)
class ProjectionRuleRepairReview:
    """Detached reviewed proof; candidate IDs alone are not write authority."""

    candidates: tuple[ProjectionRuleRepairCandidate, ...]
    authorities: tuple[_RepairAuthority, ...]


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

    def _review_authorities(self) -> tuple[_RepairAuthority, ...]:
        # Reuse the repository's default guard and the stores' loaded-byte
        # contract, including proof-only stores which this repair won't save.
        self.repository._assert_defaults_current()
        authorities = []
        for store in (self.repository.defaults, self.repository.user, self.knowledge):
            error = getattr(store, "_source_error", "")
            if error:
                raise RuntimeError(error)
            try:
                source = store.path.read_bytes()
                decoded = json.loads(source.decode("utf-8"))
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                raise RuntimeError(
                    "Rules or supporting knowledge are unavailable or invalid. "
                    "Restore access, then reload and review the current rules."
                ) from exc
            if not isinstance(decoded, dict):
                raise RuntimeError(
                    "Rules or supporting knowledge are invalid. "
                    "Reload and review the current rules before retrying."
                )
            if source != getattr(store, "_loaded_bytes", None):
                raise RuntimeError(
                    "Rules or supporting knowledge changed after this action opened. "
                    "Reload and review the current rules before retrying."
                )
            authorities.append(_RepairAuthority(
                path=str(store.path.resolve()),
                source=source,
                payload=json.dumps(store._payload(), sort_keys=True, ensure_ascii=False),
            ))
        return tuple(authorities)

    def review(self) -> ProjectionRuleRepairReview:
        self._require_no_pending()
        before = self._review_authorities()
        candidates = self.candidates()
        if before != self._review_authorities():
            raise RuntimeError("Rules changed during review. Reopen and review the current rules.")
        return ProjectionRuleRepairReview(candidates, before)

    def apply(self, selected_ids, *, review: ProjectionRuleRepairReview) -> str:
        self._require_no_pending()
        selected_ids = tuple(dict.fromkeys(map(str, selected_ids)))
        if not selected_ids:
            raise ValueError("Select at least one related rule to repair.")
        if (
            not isinstance(review, ProjectionRuleRepairReview)
            or review.authorities != self._review_authorities()
        ):
            raise RuntimeError(
                "Rules or supporting knowledge changed after this review opened. "
                "Reopen the repair and review the current rules."
            )
        current = self._candidate_map()
        reviewed = {candidate.id: candidate for candidate in review.candidates}
        missing = [
            candidate_id for candidate_id in selected_ids
            if candidate_id not in current or candidate_id not in reviewed
            or current[candidate_id] != reviewed[candidate_id]
        ]
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

    def pending_operations(self):
        """Read-only hold inspection; damaged evidence must not look like idle."""
        if getattr(self, "_recovery_uncertain", ""):
            raise RuntimeError(self._recovery_uncertain)
        _reject_reparse(self.journal.path)
        if not self.journal.path.exists():
            return ()
        try:
            self.journal.require_intact_events()
            operations = self.journal.operations()
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError(
                "Rule recovery records are unavailable or damaged. Keep the journal "
                "and backups unchanged; restore access or review a verified backup."
            ) from exc
        return tuple(o for o in operations if o.kind in (REPAIR_KIND, REPAIR_UNDO_KIND)
                     and o.status == "applying")

    def _require_no_pending(self):
        if self.pending_operations():
            raise RuntimeError(
                "An interrupted rule repair or restore needs recovery. "
                "Choose Review Interrupted Rule Recovery before another repair or restore."
            )

    def _check_recovery_payload(self, path, value):
        if not isinstance(value, dict):
            raise ValueError("Recovery requires an object snapshot.")
        if path == self.repository.user.path.resolve():
            version = value.get("schema_version", 1)
            if type(version) is not int or not 1 <= version <= AUTOMATION_RULE_SCHEMA_VERSION:
                raise ValueError("Recovery cannot downgrade unsupported rule authority.")
            for name in ("variables", "variable_overrides"):
                if name in value and not isinstance(value[name], dict):
                    raise ValueError("Invalid rule recovery mapping.")
            for name in ("merge_rules", "combine_rules", "wildcard_rules", "ignore_rules", "tag_templates"):
                if name in value and (not isinstance(value[name], list)
                        or any(not isinstance(row, dict) for row in value[name])):
                    raise ValueError("Invalid rule recovery records.")
        else:
            codec = KnowledgeLifecycleCodec("tag", self.knowledge.SCHEMA_VERSION,
                                            self.knowledge.ENROLLED_SCHEMA_VERSION)
            decode_tag_storage(json.dumps(value), TagRecord.from_dict, lifecycle_codec=codec)
            if codec.write_hold or codec.envelope is not None:
                raise ValueError("Enrolled or invalid Knowledge recovery remains held by its native authority.")
            if any(name in value and (not isinstance(value[name], list)
                   or any(not isinstance(row, dict) for row in value[name]))
                   for name in ("tags", "merge_rules")):
                raise ValueError("Invalid Knowledge recovery records.")

    @staticmethod
    def _recovery_bytes(path):
        _reject_reparse(path)
        with path.open("rb") as handle:
            raw = handle.read(MAX_SNAPSHOT_BYTES + 1)
        if len(raw) > MAX_SNAPSHOT_BYTES:
            raise ValueError("Rule recovery file exceeds the verified snapshot bound.")
        return raw

    def _recovery_changes(self, operation):
        _reject_reparse(self.repository.user.path)
        _reject_reparse(self.knowledge.path)
        _reject_reparse(self.journal.snapshot_root)
        allowed = {self.repository.user.path.resolve(), self.knowledge.path.resolve()}
        if (len(allowed) != 2 or os.path.samefile(self.repository.user.path, self.knowledge.path)
                or type(operation.expected) is not int
                or operation.expected not in (1, 2)
                or len(operation.changes) != operation.expected):
            raise ValueError("Incomplete or ambiguous rule recovery records; keep the backups unchanged.")
        result = []
        seen = set()
        for change in operation.changes:
            path = Path(change["item"])
            _reject_reparse(path)
            if (not path.is_absolute() or str(path) != str(path.resolve())
                    or path.resolve() not in allowed or path.resolve() in seen):
                raise ValueError("Unexpected, aliased or repeated rule recovery target.")
            path = path.resolve(); seen.add(path)
            before = self.journal.load_verified_snapshot(change["before"], max_bytes=MAX_SNAPSHOT_BYTES)
            after = self.journal.load_verified_snapshot(change["after"], max_bytes=MAX_SNAPSHOT_BYTES)
            self._check_recovery_payload(path, before)
            self._check_recovery_payload(path, after)
            if before == after:
                raise ValueError("Recovery evidence does not describe a changed rule store.")
            result.append((path, before, after))
        return result

    def recover_interrupted(self, progress=lambda *_: None):
        """Explicit source recovery; never infer admission for enrolled stores.

        Each interrupted operation is rolled back semantically to its own before
        state. Raw bytes are retained separately for guarded in-process rollback.
        This is not interprocess locking or a native startup qualification.
        """
        with self.journal._lock:
            progress(0, 0, "Checking interrupted rule records; no changes yet")
            pending = self.pending_operations()
            if not pending:
                return ()
            if len(pending) != 1:
                raise RuntimeError("Multiple interrupted rule operations need manual review. Keep all recovery records.")
            if any(getattr(store, "rule_pair_admission", None) is not None
                   for store in (self.repository, self.repository.user, self.knowledge)):
                raise RuntimeError("Enrolled rule recovery remains held by its native authority. Keep the journal and backups unchanged.")
            operation = pending[0]
            journal_bytes = self.journal.path.read_bytes()
            try:
                changes = self._recovery_changes(operation)
                if operation.kind == REPAIR_UNDO_KIND:
                    original = next((o for o in self.journal.operations()
                                     if o.id == operation.metadata.get("restores")), None)
                    if original is None or original.kind != REPAIR_KIND or original.status != "complete":
                        raise ValueError("Interrupted Undo has no eligible original completed repair.")
                    original_changes = {p: (b, a) for p, b, a in self._recovery_changes(original)}
                    if {p: (a, b) for p, b, a in changes} != original_changes:
                        raise ValueError("Interrupted Undo does not match its original repair.")
                current = {}
                for path, before, after in changes:
                    raw = self._recovery_bytes(path)
                    value = json.loads(raw.decode("utf-8"))
                    self._check_recovery_payload(path, value)
                    if value != before and value != after:
                        raise ValueError("Newer rule data differs from both recovery states; it was left unchanged.")
                    current[path] = (raw, value)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise RuntimeError(f"Interrupted rule recovery is held: {exc}") from exc
            written = []
            try:
                progress(0, 0, "Restoring recorded rule definitions; finishing safely")
                for path, before, _after in changes:
                    if (self.journal.path.read_bytes() != journal_bytes
                            or any(self._recovery_bytes(p) != raw for p, (raw, _v) in current.items())):
                        raise RuntimeError("Rule recovery authority changed; keep the newer data and review again.")
                    if current[path][1] == before:
                        continue
                    raw = current[path][0]
                    expected = (json.dumps(before, indent=2, ensure_ascii=False) + "\n").replace("\n", os.linesep).encode("utf-8")
                    self._write_json(path, before)
                    written.append((path, raw, expected))
                    if self._recovery_bytes(path) != expected:
                        raise RuntimeError("Rule recovery read-back differed; newer data was left untouched.")
                    current[path] = (expected, before)
                if (self.journal.path.read_bytes() != journal_bytes
                        or any(self._recovery_bytes(p) != raw for p, (raw, _v) in current.items())):
                    raise RuntimeError("Rule recovery changed before completion; keep the newer data.")
                self.repository.load(); self.knowledge.load()
                self.journal.finish(operation.id, "failed", message="Interrupted rule operation rolled back after explicit recovery.",
                                    result={"recovered": True, "kind": operation.kind})
            except BaseException as exc:
                # An append can become visible before its flush/acknowledgement
                # fails. Never roll data forward again under a recorded rollback.
                try:
                    self.journal.require_intact_events()
                    recorded = next((o for o in self.journal.operations() if o.id == operation.id), None)
                    committed = recorded is not None and recorded.status == "failed" and recorded.result.get("recovered") is True
                except (OSError, ValueError, TypeError):
                    committed = False
                if committed:
                    self._recovery_uncertain = (
                        "Recovery reached a recorded rollback but its completion acknowledgement failed. "
                        "Keep current files and backups; reload and review recovery before changing rules."
                    )
                    raise RuntimeError(self._recovery_uncertain) from exc
                rollback_held = False
                for path, raw, expected in reversed(written):
                    try:
                        if self._recovery_bytes(path) != expected:
                            rollback_held = True; continue
                        self._restore_bytes(path, raw)
                        if self._recovery_bytes(path) != raw:
                            rollback_held = True
                    except (OSError, ValueError):
                        rollback_held = True
                # Do not consume the pending operation or overwrite its snapshots.
                self.journal.append_checkpoint(operation.id, item="recovery-attempt",
                    metadata={"failed": True, "rollback_held": rollback_held})
                raise RuntimeError("Rule recovery remains held. Keep the journal and backups. " + str(exc)) from exc
            return (operation.id,)

    def undoable_operation(self):
        if self.pending_operations():
            return None
        return self.journal.latest(kind=REPAIR_KIND, statuses=("complete",))

    def undo_last(self) -> str:
        self._require_no_pending()
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
    "ProjectionRuleRepairReview",
    "ProjectionRuleRepairService",
    "REPAIR_KIND",
    "REPAIR_UNDO_KIND",
]
