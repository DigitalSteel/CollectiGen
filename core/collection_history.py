"""Typed Collection history. Images are accessed only by explicit proof callbacks.

Canonical document bytes are recovery witnesses, never a restore instruction.
The native UUID identifies a collection for its lifetime within one binding.
Membership has only exact library/path/hash witnesses, not invented image UUIDs.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from .concept_groups import (
    ConceptGroupError, MAX_ADD, MAX_BYTES, _bytes, _hash, _json, _key, _member,
    _rule_group_update, _validate, group_fingerprint,
)
from .structured_history import (
    CanonicalEvidence, DomainChange, DomainEntityRef, DomainState, DomainUnit, DomainVersionRef,
    _meaning, _state_json,
)

AUTHORITY = "collections:collections"
UNIT_KEYS = ("identity", "name", "description", "rules", "membership", "other")
MAX_PAGE = 200


def _json_text(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _decisions(group):
    return {_key(row): {"status": status, "row": deepcopy(row)}
        for status, rows in (("active", group["members"]), ("ignored", group.get("ignored_members", [])))
        for row in rows}


def _decision_json(value):
    return _json_text(value if value is not None else {"status": "absent", "row": None})


def _same_membership_proof(left, right):
    if left is None or right is None: return left is right
    return left["status"] == right["status"] and all(
        left["row"].get(key, "manual" if key == "membership_source" else None)
        == right["row"].get(key, "manual" if key == "membership_source" else None)
        for key in ("library_id", "relative_path", "image_sha256", "membership_source"))


@dataclass(frozen=True)
class CollectionDecisionRow:
    """JSON keeps frozen previews immutable, including unknown member fields."""
    kind: str
    key: str | tuple[str, str]
    current_json: str
    proposed_json: str
    conflict: bool
    companion: bool = False


@dataclass(frozen=True)
class CollectionMemberPage:
    rows: tuple[CollectionDecisionRow, ...]
    total: int
    next_offset: int | None


@dataclass(frozen=True)
class CollectionObservationPlan:
    scope_key: str
    admission: str
    authority_path: str
    entity: DomainEntityRef
    expected_revision: str
    expected_head_id: str
    expected_head_payload: str
    current_state: DomainState
    operation_id: str
    mode: str
    rows: tuple[CollectionDecisionRow, ...]
    held_reasons: tuple[str, ...]
    digest: str

    @property
    def can_confirm(self):
        return not self.held_reasons

    def page(self, *, offset=0, limit=100):
        _page_args(offset, limit)
        return CollectionMemberPage(self.rows[offset:offset+limit], len(self.rows),
            offset+limit if offset+limit < len(self.rows) else None)


def _observation_digest(plan):
    values = {key: value for key, value in plan.__dict__.items()
        if key not in {"digest", "entity", "current_state", "rows"}}
    values.update(entity=plan.entity.__dict__, current_state=_state_json(plan.current_state),
        rows=[row.__dict__ for row in plan.rows])
    return _hash(_bytes(values))


@dataclass(frozen=True)
class CollectionRestorePlan:
    scope_key: str
    admission: str
    source_version: DomainVersionRef
    expected_revision: str
    group_fingerprint: str
    fields: tuple[str, ...]
    member_keys: tuple[tuple[str, str], ...]
    overwrite: bool
    proposed_json: str
    proof_rows_json: str
    rows: tuple[CollectionDecisionRow, ...]
    companions: tuple[CollectionDecisionRow, ...]
    held_reasons: tuple[str, ...]
    digest: str

    @property
    def can_apply(self):
        return not self.held_reasons and (self.overwrite or not any(row.conflict for row in self.rows))

    def companion_page(self, *, offset=0, limit=100):
        _page_args(offset, limit)
        return CollectionMemberPage(self.companions[offset:offset+limit], len(self.companions),
            offset+limit if offset+limit < len(self.companions) else None)


def _page_args(offset, limit):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise ConceptGroupError("Choose a bounded Collection history page.")


def _plan_digest(plan):
    values = {key: value for key, value in plan.__dict__.items()
        if key not in {"digest", "rows", "companions", "source_version"}}
    values.update(source_version={**plan.source_version.__dict__, "entity": plan.source_version.entity.__dict__},
        rows=[row.__dict__ for row in plan.rows], companions=[row.__dict__ for row in plan.companions])
    return _hash(_bytes(values))


class CollectionHistoryAdapter:
    def __init__(self, history, *, authority_path):
        self.history = history
        self.authority_path = Path(authority_path).absolute()
        scope = history.scope
        if AUTHORITY not in scope.authority_ids or dict(scope.domain_versions).get("collections") != 1:
            raise ConceptGroupError("The configured history does not include Collection authority.")

    @classmethod
    def from_configuration(cls, service, descriptor):
        if descriptor.domain != "collections" or descriptor.status != "configured":
            raise ConceptGroupError("Collection history is not configured for this operation.")
        binding = json.loads(descriptor.binding_json)
        location = binding["domains"]["collections"]["authorities"]["collections"]
        if os.path.normcase(str(Path(location).absolute())) != os.path.normcase(
                str(Path(service.authorities["collections"]["collections"]).absolute())):
            raise ConceptGroupError("Configured Collection authority location changed.")
        return cls(service.store(descriptor), authority_path=location)

    def require_path(self, path):
        if os.path.normcase(str(Path(path).absolute())) != os.path.normcase(str(self.authority_path)):
            raise ConceptGroupError("This Collection store is outside the configured exact authority path.")

    def entity(self, group_id):
        if type(group_id) is not str or not re.fullmatch(r"concept\.[0-9a-f]{32}", group_id):
            raise ConceptGroupError("Collection history needs the exact native Collection UUID.")
        return DomainEntityRef("collections", AUTHORITY, "collection", group_id, group_id)

    def state(self, raw, group_id):
        document = _validate(_json(raw) if raw is not None else {"schema_version": 1, "groups": {}})
        group = document["groups"].get(group_id)
        values = dict.fromkeys(UNIT_KEYS)
        if group is not None:
            values = {"identity": {"id": group["id"], "created_at": group["created_at"]},
                "name": group["name"], "description": group["description"], "rules": group.get("rules"),
                "membership": {"members": group["members"], "ignored_members": group.get("ignored_members", [])},
                "other": {k: v for k, v in group.items() if k not in {
                    "id", "created_at", "name", "description", "rules", "members", "ignored_members"}}}
        return DomainState(self.entity(group_id), group is not None,
            tuple(DomainUnit(key, 1, _json_text(values[key]) if group is not None else None) for key in UNIT_KEYS),
            (CanonicalEvidence(AUTHORITY, raw),))

    def _group(self, state):
        if state.entity != self.entity(state.entity.key) or tuple(u.key for u in state.units) != UNIT_KEYS:
            raise ConceptGroupError("The saved Collection identity or typed scope is unsupported.")
        # Validate immutable units against their exact canonical witness before interpreting them.
        if len(state.evidence) != 1 or state != self.state(state.evidence[0].raw, state.entity.key):
            raise ConceptGroupError("Collection history units disagree with canonical evidence.")
        if not state.present: return None
        return deepcopy(_json(state.evidence[0].raw)["groups"][state.entity.key])

    def prepare(self, before, after, group_id, operation_id, *, restored_from=None):
        old, new = self.state(before, group_id), self.state(after, group_id)
        head = self.history.current_head(old.entity)
        if head is not None:
            previous, current = self._group(self.history.payload(head)), self._group(old)
            if previous is not None and (current is None or previous["created_at"] != current["created_at"]):
                raise ConceptGroupError("Collection creation evidence changed; its incarnation is held for review.")
        return self.history.prepare((DomainChange(old, new),), operation_kind="collection_restore" if restored_from else "collection_save",
            operation_id=operation_id, restored_from={old.entity: restored_from} if restored_from else None)

    def resolve_native(self, native, before, after, current, outcome):
        if native.get("history_scope") != self.history.scope.key:
            raise ConceptGroupError("Interrupted Collection history belongs to another exact binding.")
        receipt = self.history.receipt(native.get("history_transaction", ""))
        group_id = native.get("history_group")
        if (receipt.operation_id != native["operation_id"] or len(receipt.members) != 1
                or receipt.members[0].entity != self.entity(group_id)):
            raise ConceptGroupError("Collection recovery journals do not identify the same operation.")
        member = receipt.members[0]
        old = None if native["before_absent"] else before
        if (self.history._payload(member.before) != self.state(old, group_id)
                or self.history._payload(member.after) != self.state(after, group_id)):
            raise ConceptGroupError("Collection recovery witnesses disagree; both journals were preserved.")
        resolved = self.history.reconcile(receipt.transaction_id, read_current=lambda: (self.state(current, group_id),))
        if resolved != ("completed" if outcome == "committed" else "unchanged"):
            raise ConceptGroupError("Collection recovery is held; both journals were preserved.")

    def recover_unlinked(self, canonical):
        """Handle a prepared intent interrupted before publishing a native receipt."""
        self.require_path(canonical.path)
        count = 0
        for receipt in self.history.pending():
            if not any(AUTHORITY in m.authority_ids for m in receipt.members): continue
            if len(receipt.members) != 1 or receipt.members[0].entity != self.entity(receipt.members[0].entity.key):
                raise ConceptGroupError("Unsupported linked Collection recovery remains held.")
            member = receipt.members[0]
            state = self.state(canonical._read(), member.entity.key)
            if state != self.history._payload(member.before):
                raise ConceptGroupError("Collection recovery is missing its native receipt; current data was preserved.")
            if self.history.reconcile(receipt.transaction_id, read_current=lambda: (state,)) != "unchanged":
                raise ConceptGroupError("Collection recovery remained held.")
            count += 1
        return "unchanged" if count else "No interrupted collection save."

    def page(self, group_id, *, cursor=None, limit=50):
        return self.history.query(self.entity(group_id), cursor=cursor, limit=limit)

    def _observation_pending(self, canonical):
        if canonical._read(canonical.pending, 4096) is not None:
            raise ConceptGroupError("Recover the interrupted Collection save before observing its current state.")
        if any(AUTHORITY in member.authority_ids for receipt in self.history.pending() for member in receipt.members):
            raise ConceptGroupError("Recover pending Collection history before observing its current state.")

    def plan_observation(self, canonical, group_id):
        """Review stored Collection metadata only, without resolving any image."""
        self.require_path(canonical.path)
        entity = self.entity(group_id)
        with self.history.locked():
            with canonical._writer():
                self._observation_pending(canonical)
                raw = canonical._read()
                current_state = self.state(raw, group_id)
                current = self._group(current_state)
                if current is None:
                    raise ConceptGroupError("An absent Collection cannot be enrolled or observed as a deletion.")
                head = self.history.current_head(entity)
                recorded_state = self.history.payload(head) if head is not None else None
                recorded = self._group(recorded_state) if recorded_state is not None else None
                if recorded is not None and recorded["created_at"] != current["created_at"]:
                    raise ConceptGroupError("Collection creation evidence changed; its incarnation is held for review.")
                mode = ("initial_baseline" if head is None else
                    "unchanged" if _meaning(recorded_state) == _meaning(current_state) else "gap")
                before_units = {unit.key: unit.value_json for unit in recorded_state.units} if recorded_state else {}
                rows = tuple(CollectionDecisionRow("field", unit.key, before_units.get(unit.key) or "null",
                    unit.value_json or "null", False) for unit in current_state.units
                    if unit.key != "membership" and before_units.get(unit.key) != unit.value_json)
                before_members = _decisions(recorded) if recorded is not None else {}
                current_members = _decisions(current)
                rows += tuple(CollectionDecisionRow("member", key, _decision_json(before_members.get(key)),
                    _decision_json(current_members.get(key)), False) for key in sorted(set(before_members) | set(current_members))
                    if before_members.get(key) != current_members.get(key))
                held = () if "collections" in self.history.scope.writable_domains else (
                    "Collection history is read-only. Enable this exact binding before recording current state.",)
                plan = CollectionObservationPlan(self.history.scope.key, self.history.scope.configuration_fingerprint,
                    str(self.authority_path), entity, _hash(raw), head.version_id if head else "", head.payload if head else "",
                    current_state, "observation_" + uuid4().hex, mode, rows, held, "")
                return CollectionObservationPlan(**{**plan.__dict__, "digest": _observation_digest(plan)})

    def confirm_observation(self, canonical, plan):
        self.require_path(canonical.path)
        if not isinstance(plan, CollectionObservationPlan) or plan.digest != _observation_digest(plan):
            raise ConceptGroupError("Collection observation review changed; review again.")
        if (plan.scope_key != self.history.scope.key or plan.admission != self.history.scope.configuration_fingerprint
                or plan.authority_path != str(self.authority_path) or plan.entity != self.entity(plan.entity.key)
                or plan.current_state.entity != plan.entity):
            raise ConceptGroupError("Collection observation admission or identity changed; reopen the review.")
        if not plan.can_confirm: raise ConceptGroupError("; ".join(plan.held_reasons))
        with self.history.locked():
            with canonical._writer():
                self._observation_pending(canonical)
                if plan.expected_head_id:
                    recorded_version = self.history.version(plan.entity, plan.expected_head_id)
                    if recorded_version.payload != plan.expected_head_payload:
                        raise ConceptGroupError("Reviewed Collection history evidence changed.")
                    recorded = self._group(self.history.payload(recorded_version))
                else:
                    if plan.expected_head_payload: raise ConceptGroupError("Unexpected Collection baseline evidence.")
                    recorded = None

                def read_current():
                    self._observation_pending(canonical)
                    raw = canonical._read()
                    if _hash(raw) != plan.expected_revision:
                        raise ConceptGroupError("Saved Collections changed after observation review; review again.")
                    current_state = self.state(raw, plan.entity.key)
                    current = self._group(current_state)
                    if current is None or current_state != plan.current_state:
                        raise ConceptGroupError("The exact Collection state changed after observation review.")
                    if recorded is not None and recorded["created_at"] != current["created_at"]:
                        raise ConceptGroupError("Collection creation evidence changed; its incarnation is held for review.")
                    return current_state

                return self.history.observe_current(plan.current_state, expected_head_id=plan.expected_head_id,
                    operation_id=plan.operation_id, read_current=read_current,
                    reason="Explicit review of current saved Collection state; unrecorded operations are unknown.")

    def member_page(self, version, *, canonical=None, offset=0, limit=100):
        _page_args(offset, limit)
        with self.history.locked():
            group = self._group(self.history.payload(version))
            current = {}
            if canonical is not None:
                self.require_path(canonical.path)
                with canonical._writer():
                    document, _revision = canonical.snapshot()
                    present = document["groups"].get(version.entity.key)
                    if group is not None and present is not None and group["created_at"] != present["created_at"]:
                        raise ConceptGroupError("Collection creation evidence changed; its incarnation is held for review.")
                    current = _decisions(present) if present is not None else {}
            decisions = _decisions(group) if group is not None else {}
            keys = sorted(set(current) | set(decisions))
            rows = tuple(CollectionDecisionRow("member", key, _decision_json(current.get(key)),
                _decision_json(decisions.get(key)), canonical is not None and current.get(key) != decisions.get(key))
                for key in keys[offset:offset+limit])
            return CollectionMemberPage(rows, len(keys), offset+limit if offset+limit < len(keys) else None)

    @contextmanager
    def _proof(self, proof, rows):
        if not rows:
            yield
            return
        if not callable(proof):
            raise ConceptGroupError("Current exact library, path and image-byte proof is required for the selected membership.")
        # The callback must guard current Dataset scope for the whole transaction.
        # Copies prevent the caller from mutating the frozen plan through this boundary.
        with proof(tuple(deepcopy(rows))):
            yield

    def _overlay(self, current, historical, fields, keys, prepared_rules, dataset):
        result = deepcopy(current)
        held = []
        if "rules" in fields:
            if keys:
                held.append("Restore rule definitions and selected membership in separate reviewed operations.")
            if historical.get("rules") is None:
                result.pop("rules", None)
                for row in result["members"]: row.pop("membership_source", None)
            elif prepared_rules is None or dataset is None:
                held.append("Preview these historical rules against current Dataset evidence before restoring them.")
            elif prepared_rules.preview.rules != historical["rules"]:
                held.append("The rule preview does not match the selected historical definition.")
            else:
                result = _rule_group_update(current, prepared_rules, dataset)
        for field in fields:
            if field != "rules": result[field] = deepcopy(historical[field])
        old, selected = _decisions(current), _decisions(historical)
        merged = _decisions(result)
        needs_rules = bool(keys and current.get("rules") is not None)
        rule_result = None
        if needs_rules:
            if prepared_rules is None or dataset is None or prepared_rules.preview.rules != current["rules"]:
                held.append("Preview current automatic rules before restoring exact membership; manual re-add is a separate action.")
            else:
                rule_result = _decisions(_rule_group_update(current, prepared_rules, dataset))
        for key in keys:
            target, existing = selected.get(key), old.get(key)
            if target and existing and target["row"]["image_sha256"] != existing["row"]["image_sha256"]:
                held.append("A selected member's image bytes changed; explicit replacement review is required.")
            if target and target["status"] == "active" and target["row"].get("membership_source", "manual") == "automatic":
                # Ignored/current-manual rows are not implicitly made automatic by restoration.
                if historical.get("rules") != current.get("rules") or rule_result is None or not _same_membership_proof(rule_result.get(key), target):
                    held.append("Historical automatic membership lacks compatible current rule proof; use explicit manual re-add instead.")
            if target is None and rule_result is not None and key in rule_result:
                held.append("Current rule proof cannot preserve this historically absent decision after refresh; review an explicit ignore decision instead.")
            if target is None: merged.pop(key, None)
            else:
                # The typed selector controls membership/provenance, not unknown extensions.
                row = {**deepcopy(target["row"]), **(deepcopy(existing["row"]) if existing else {})}
                for field in ("library_id", "relative_path", "image_sha256", "membership_source"):
                    if field in target["row"]: row[field] = target["row"][field]
                if "membership_source" not in target["row"]: row.pop("membership_source", None)
                merged[key] = {"status": target["status"], "row": row}
        if keys:
            result["members"] = [decision["row"] for decision in merged.values() if decision["status"] == "active"]
            result["ignored_members"] = [decision["row"] for decision in merged.values() if decision["status"] == "ignored"]
        return result, tuple(dict.fromkeys(held))

    def plan_restore(self, canonical, version, *, fields=(), member_keys=(), overwrite=False,
                     proof=None, prepared_rules=None, dataset=None):
        self.require_path(canonical.path)
        fields, keys = tuple(fields), tuple(member_keys)
        if (len(set(fields)) != len(fields) or set(fields) - {"name", "description", "rules"}
                or type(overwrite) is not bool or len(keys) > MAX_ADD or not (fields or keys)):
            raise ConceptGroupError("Select bounded typed Collection fields or up to 5,000 exact membership decisions.")
        if any(type(key) is not tuple or len(key) != 2 or any(type(v) is not str or not v for v in key) for key in keys):
            raise ConceptGroupError("Select exact library and relative-path keys, not display names.")
        keys = tuple((a.casefold(), b.casefold()) for a, b in keys)
        if len(set(keys)) != len(keys): raise ConceptGroupError("Select each membership decision once.")
        with self.history.locked():
            with canonical._writer():
                historical = self._group(self.history.payload(version))
                document, revision = canonical.snapshot()
                current = document["groups"].get(version.entity.key)
                if historical is None or current is None:
                    raise ConceptGroupError("Restoring an absent Collection is unsupported: native deletion and dependent references are not qualified.")
                if current["created_at"] != historical["created_at"]:
                    raise ConceptGroupError("Collection creation evidence changed; its incarnation is held for review.")
                old, target = _decisions(current), _decisions(historical)
                if any(key not in old and key not in target for key in keys):
                    raise ConceptGroupError("A selected absent decision has no exact current or historical member witness.")
                proposed, held = self._overlay(current, historical, fields, keys, prepared_rules, dataset)
                if "collections" not in self.history.scope.writable_domains:
                    held = (*held, "Collection history is read-only. Enable this exact binding before restoring saved decisions.")
                candidate = deepcopy(document)
                candidate["groups"][current["id"]] = proposed
                if proposed.get("rules") is not None or proposed.get("ignored_members"):
                    candidate["schema_version"] = 2
                try:
                    _validate(candidate)
                    if len(_bytes(candidate)) > MAX_BYTES:
                        raise ConceptGroupError("Training collections exceed the supported storage size.")
                except (ValueError, TypeError) as exc:
                    held = (*held, str(exc))
                rows = tuple(CollectionDecisionRow("field", field, _json_text(current.get(field)),
                    _json_text(proposed.get(field)), current.get(field) != proposed.get(field)) for field in fields)
                updated = _decisions(proposed)
                rows += tuple(CollectionDecisionRow("member", key, _decision_json(old.get(key)),
                    _decision_json(updated.get(key)), old.get(key) != updated.get(key)) for key in keys)
                companions = tuple(CollectionDecisionRow("member", key, _decision_json(old.get(key)),
                    _decision_json(updated.get(key)), False, True) for key in sorted(set(old) | set(updated))
                    if key not in keys and old.get(key) != updated.get(key))
                witnesses = {}
                for key in (*keys, *(row.key for row in companions)):
                    for decisions in (old, target if key in keys else {}, updated):
                        decision = decisions.get(key)
                        if decision is not None:
                            row = _member(decision["row"])
                            witnesses[(row["library_id"], row["relative_path"], row["image_sha256"])] = row
                proof_rows = tuple(witnesses.values())
                with self._proof(proof, proof_rows):
                    plan = CollectionRestorePlan(self.history.scope.key, self.history.scope.configuration_fingerprint,
                        version, revision, group_fingerprint(current), fields, keys, overwrite,
                        _json_text(proposed), _json_text(proof_rows), rows, companions, held, "")
                    return CollectionRestorePlan(**{**plan.__dict__, "digest": _plan_digest(plan)})

    def apply_restore(self, canonical, plan, *, proof=None, prepared_rules=None, dataset=None):
        self.require_path(canonical.path)
        if not isinstance(plan, CollectionRestorePlan) or plan.digest != _plan_digest(plan):
            raise ConceptGroupError("Collection restore preview changed; preview again.")
        if plan.scope_key != self.history.scope.key or plan.admission != self.history.scope.configuration_fingerprint:
            raise ConceptGroupError("Collection history admission changed; reopen the restore preview.")
        if not plan.can_apply:
            raise ConceptGroupError("; ".join(plan.held_reasons) or "Review and explicitly accept overwriting the selected current decisions.")
        rows = json.loads(plan.proof_rows_json)
        def change(document):
            current = document["groups"][plan.source_version.entity.key]
            if group_fingerprint(current) != plan.group_fingerprint:
                raise ConceptGroupError("Collection changed after preview; preview again.")
            historical = self._group(self.history.payload(plan.source_version))
            proposed, held = self._overlay(current, historical, plan.fields, plan.member_keys, prepared_rules, dataset)
            if held or _json_text(proposed) != plan.proposed_json:
                raise ConceptGroupError("Collection restore dependencies changed; preview again.")
            if proposed != current:
                proposed["updated_at"] = datetime.now(timezone.utc).isoformat()
                document["groups"][current["id"]] = proposed
                if proposed.get("rules") is not None or proposed.get("ignored_members"):
                    document["schema_version"] = 2
            _validate(document)
            return document["groups"][current["id"]]
        return canonical._change(plan.source_version.entity.key, plan.expected_revision, change,
            history_adapter=self, restored_from=plan.source_version.version_id,
            proof_guard=lambda: self._proof(proof, rows))
