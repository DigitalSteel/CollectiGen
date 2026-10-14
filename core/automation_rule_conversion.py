"""Exact state classification and fail-closed admission for rule conversion.

No production conversion publisher or recovery command is enabled. A pending
entry holds enrolled startup, writers and evaluators; this does not enroll the
other direct Knowledge writers or qualify cross-store publication.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from .knowledge_write_admission import KnowledgeWriteAdmission, _checked_path


class RuleConversionAdmissionError(RuntimeError):
    """A rule conversion requires explicit recovery before ordinary work."""


class CategoryRetirementAdmissionError(RuleConversionAdmissionError):
    """A Category-owned pending reference refuses conflicting work."""


@dataclass(frozen=True, init=False)
class RulePairAdmission:
    """Bind the exact native Rules and System Knowledge pair in one user root.

    The pending namespace is deliberately opaque here: *any* entry, including
    an unknown or damaged receipt, holds work. Only a future reviewed recovery
    command may interpret and remove unresolved entries. The private native
    Category publisher installs only a pending reference here, retaining it after
    terminal publication until shared-authority handoff is qualified. An exact
    automatic restoration can clear its own reference; the backup receipt remains
    the sole transaction authority. Production conversion and UI retry stay disabled.
    """

    root: Path
    rules_path: Path
    knowledge_path: Path
    pending_root: Path
    authority_key: str

    def __init__(self, root: str | Path):
        path = Path(root)
        if not path.is_absolute() or ".." in path.parts:
            raise RuleConversionAdmissionError("Bind an exact absolute Rules authority root.")
        rules = path / "automation_rules.json"
        knowledge = path / "tags.json"
        identity = json.dumps(
            (os.path.normcase(str(rules)), os.path.normcase(str(knowledge))),
            separators=(",", ":"),
        ).encode("utf-8")
        object.__setattr__(self, "root", path)
        object.__setattr__(self, "rules_path", rules)
        object.__setattr__(self, "knowledge_path", knowledge)
        object.__setattr__(self, "pending_root", path / ".automation-rule-conversion-pending")
        object.__setattr__(self, "authority_key", hashlib.sha256(identity).hexdigest())

    def owns(self, path: str | Path, role: Literal["rules", "knowledge"]) -> bool:
        if role not in {"rules", "knowledge"}:
            raise ValueError("Choose an exact rule-pair participant.")
        target = self.rules_path if role == "rules" else self.knowledge_path
        return Path(path) == target

    def assert_clear(self) -> None:
        """Fail closed at a retained evaluator's next entry point.

        This check alone is not an operation-level lease through later image or
        export publication. Live conversion remains disabled for that reason.
        """
        try:
            # This path is queried for each retained evaluator entry. A missing
            # namespace is the ordinary case; avoid canonicalizing its parent
            # on every tag while still treating access errors as a hold.
            try:
                os.lstat(self.pending_root)
            except FileNotFoundError:
                return
            state = _checked_path(self.pending_root, directory=True)
            if state is None:
                raise RuleConversionAdmissionError(
                    "Rule conversion state changed. Retry after reviewing recovery."
                )
            with os.scandir(self.pending_root) as entries:
                for _entry in entries:
                    if _entry.name.startswith("category-retirement."):
                        raise CategoryRetirementAdmissionError(
                            "Category retirement recovery is pending. Keep its backup and receipt unchanged; review recovery before editing or using rules."
                        )
                    raise RuleConversionAdmissionError(
                        "Rule conversion recovery is pending. Review recovery before editing or using rules."
                    )
        except RuleConversionAdmissionError:
            raise
        except (OSError, RuntimeError) as exc:
            raise RuleConversionAdmissionError(
                "Rule conversion state is unavailable. Restore access before editing or using rules."
            ) from exc

    @contextmanager
    def admit(self):
        """Hold native exclusion through a writer's entire save/rollback."""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuleConversionAdmissionError(
                "Rules authority exclusion is unavailable. Restore access before retrying."
            ) from exc
        with KnowledgeWriteAdmission(self.root).locked():
            self.assert_clear()
            yield


@dataclass(frozen=True)
class RuleAuthorityPair:
    """Exact saved bytes; ``None`` means the authority file did not exist."""

    automation_rules: bytes | None
    system_knowledge: bytes | None


@dataclass(frozen=True)
class RuleConversionState:
    outcome: Literal["unchanged", "committed", "interrupted", "diverged"]
    automation_rules: Literal["before", "after", "both", "other"]
    system_knowledge: Literal["before", "after", "both", "other"]

    @property
    def needs_resolution(self) -> bool:
        """A mixed or diverged pair cannot be finalized as an ordinary save."""
        return self.outcome in {"interrupted", "diverged"}


def _participant_state(before: bytes | None, after: bytes | None,
                       current: bytes | None) -> Literal["before", "after", "both", "other"]:
    if current == before == after:
        return "both"
    if current == before:
        return "before"
    if current == after:
        return "after"
    return "other"


def classify_rule_conversion(
    before: RuleAuthorityPair,
    after: RuleAuthorityPair,
    current: RuleAuthorityPair,
) -> RuleConversionState:
    """Classify exact pair evidence, never guessing from a receipt phase label.

    A later recovery command must still validate receipt integrity, participant
    paths, locks, and unchanged current bytes before restoring anything. This
    classifier alone grants no write or execution authority, even when it sees
    an unchanged or committed pair: a pending receipt still needs admission and
    finalization before automation may run.
    """
    if before == after:
        raise ValueError("A rule conversion must change at least one authority.")
    rules = _participant_state(before.automation_rules, after.automation_rules,
                               current.automation_rules)
    knowledge = _participant_state(before.system_knowledge, after.system_knowledge,
                                   current.system_knowledge)
    if current == before:
        outcome = "unchanged"
    elif current == after:
        outcome = "committed"
    elif "other" in (rules, knowledge):
        outcome = "diverged"
    else:
        outcome = "interrupted"
    return RuleConversionState(outcome, rules, knowledge)


# This executable prototype is deliberately isolated from application profiles.
# It qualifies exact-byte transaction mechanics, not semantic rule conversion,
# complete writer enrollment, or leases through rule-derived publication.
MAX_CONVERSION_PARTICIPANT_BYTES = 8 * 1024 * 1024
MAX_CONVERSION_RECEIPT_BYTES = 16 * 1024
_SANDBOX_MARKER = ".synthetic-rule-conversion.json"
_ROLES = ("automation_rules", "system_knowledge")


@dataclass(frozen=True)
class RuleConversionPlan:
    """Detached immutable synthetic inputs; absence is distinct from empty bytes."""

    before: RuleAuthorityPair
    after: RuleAuthorityPair

    def __post_init__(self):
        for pair in (self.before, self.after):
            if not isinstance(pair, RuleAuthorityPair):
                raise ValueError("Declare exact before and after authority pairs.")
            for role in _ROLES:
                raw = getattr(pair, role)
                if raw is not None and (
                    not isinstance(raw, bytes)
                    or len(raw) > MAX_CONVERSION_PARTICIPANT_BYTES
                ):
                    raise ValueError("Synthetic authority bytes are invalid or oversized.")
        classify_rule_conversion(self.before, self.after, self.before)


@dataclass(frozen=True)
class DetachedLiteralMergeConversion:
    """Read-only caption-rule preview; it is not a production save capability.

    This moves a training projection, never an image-identification fact or a
    reusable Tag identity. The caller must still review the proposed caption
    effect before any future production publication.
    """

    pair: RuleConversionPlan
    defaults_sha256: str | None
    source_index: int
    destination_rule_id: str
    sources: tuple[str, ...]
    target: str


def _detached_object(raw: bytes | None, *, name: str) -> dict:
    """Decode bounded captured bytes, rejecting ambiguous duplicate fields."""
    if raw is not None and (not isinstance(raw, bytes)
                            or len(raw) > MAX_CONVERSION_PARTICIPANT_BYTES):
        raise ValueError(f"{name} is invalid or exceeds the detached planning bound.")
    def unique_fields(pairs):
        result={}
        for key,value in pairs:
            if key in result:
                raise ValueError(f"{name} contains duplicate JSON fields.")
            result[key]=value
        return result
    def invalid_constant(value):
        raise ValueError(f"{name} contains a non-JSON number: {value}.")
    try:
        value=(json.loads(raw.decode("utf-8"),object_pairs_hook=unique_fields,
                          parse_constant=invalid_constant)
               if raw is not None else {})
    except (UnicodeError,ValueError) as exc:
        raise ValueError(f"{name} is malformed or ambiguous.") from exc
    if not isinstance(value,dict):
        raise ValueError(f"{name} must be a JSON object.")
    return value


def _detached_rule_layer(raw: bytes | None, *, name: str) -> tuple[dict,dict]:
    from .automation_rules import AUTOMATION_RULE_SCHEMA_VERSION, DEFAULT_RULES
    payload=_detached_object(raw,name=name)
    version=payload.get("schema_version",1)
    if type(version) is not int or not 1<=version<=AUTOMATION_RULE_SCHEMA_VERSION:
        raise ValueError(f"{name} has an unsupported schema.")
    layer={key:deepcopy(payload.get(key,default)) for key,default in DEFAULT_RULES.items()}
    if (not isinstance(layer["variables"],dict)
            or not isinstance(layer["variable_overrides"],dict)
            or any(not isinstance(layer[key],list) for key in (
                "merge_rules","wildcard_rules","combine_rules","ignore_rules",
                "disabled_rules","tag_templates",
            ))):
        raise ValueError(f"{name} has an unsupported rule collection.")
    for key in ("merge_rules","wildcard_rules","combine_rules","ignore_rules"):
        if any(not isinstance(item,dict) for item in layer[key]):
            raise ValueError(f"{name} contains a malformed {key} record.")
    for kind in ("merge_rules","combine_rules","wildcard_rules","ignore_rules"):
        for record in layer[kind]:
            if kind=="ignore_rules":
                valid=isinstance(record.get("tag"),str)
            elif kind=="wildcard_rules":
                remove=record.get("remove")
                valid=(isinstance(record.get("pattern"),str)
                       and (isinstance(remove,str) or isinstance(remove,list)
                            and all(isinstance(item,str) for item in remove)))
            else:
                remove=record.get("remove")
                valid=(isinstance(remove,list)
                       and all(isinstance(item,str) for item in remove)
                       and isinstance(record.get("replace"),str))
            if not valid:
                raise ValueError(f"{name} contains an unsupported {kind} record.")
    if (any(not isinstance(value,list) or any(not isinstance(item,str) for item in value)
            for value in layer["variables"].values())
            or any(not isinstance(value,list) or any(not isinstance(item,str) for item in value)
                   for value in layer["variable_overrides"].values())
            or any(not isinstance(value,str) for value in layer["disabled_rules"])):
        raise ValueError(f"{name} contains an unsupported value or disablement list.")
    return payload,layer


def plan_user_literal_merge_to_knowledge(
    before: RuleAuthorityPair,
    *,
    default_rules: bytes | None,
    source_index: int,
    selected_record: dict,
    sources: tuple[str, ...],
    target: str,
) -> DetachedLiteralMergeConversion:
    """Plan one exact User merge-rule move without reading or writing files.

    The narrow first case is a caption-building projection over existing,
    reviewed Tags. It refuses source metadata or semantics that cannot yet be
    mapped exactly, and never alters Image Knowledge or Tag identities. It
    retains whole before bytes for a future transaction, but neither this plan
    nor the synthetic sandbox authorizes a live save.
    """
    from .automation_repository import combine_rule_layers,rule_key,rule_key_id
    from .automation_rules import AutomationEngine,AutomationRuleStore
    from .tag_collection import normalize_tag
    from .tag_database import MergeRule,TagDatabase,TagRecord,_slug
    from .tag_storage_decode import KnowledgeLifecycleCodec,decode_tag_storage
    from .rule_intents import TRAINING_PROJECTION

    if not isinstance(before,RuleAuthorityPair):
        raise TypeError("Capture the exact current Rules and Knowledge pair.")
    if before.automation_rules is None or before.system_knowledge is None:
        raise ValueError("Both saved authorities must exist for this conversion.")
    if default_rules is None:
        raise ValueError("Capture the built-in Rules before planning a training caption conversion.")
    if type(source_index) is not int or source_index<0:
        raise ValueError("Select an exact User rule index.")
    user_payload,user_layer=_detached_rule_layer(before.automation_rules,name="User Rules")
    _default_payload,default_layer=_detached_rule_layer(default_rules,name="Built-in Rules")
    user_rules=user_layer["merge_rules"]
    if source_index>=len(user_rules) or user_rules[source_index]!=selected_record:
        raise ValueError("The selected User rule changed; review the current rule again.")
    selected=user_rules[source_index]
    if (set(selected)!={"remove","replace","intent"}
            or selected["intent"]!=TRAINING_PROJECTION
            or not isinstance(selected["remove"],list)
            or len(selected["remove"])<2
            or any(not isinstance(value,str) or not value.strip()
                   or "{" in value or "}" in value for value in selected["remove"])
            or not isinstance(selected["replace"],str)
            or not selected["replace"].strip()
            or "{" in selected["replace"] or "}" in selected["replace"]):
        raise ValueError("This source rule has template or unsupported execution metadata; leave it unchanged.")
    selected_key=rule_key("merge_rules",selected)
    if sum(rule_key("merge_rules",item)==selected_key for item in user_rules)!=1:
        raise ValueError("This User rule has an ambiguous duplicate selection.")
    active_defaults={
        rule_key("merge_rules",item)
        for item in default_layer["merge_rules"]
        if rule_key_id("merge_rules",item) not in user_layer["disabled_rules"]
    }
    if selected_key in active_defaults:
        raise ValueError("This User rule is hidden by another effective rule.")
    if selected not in combine_rule_layers(default_layer,user_layer)["merge_rules"]:
        raise ValueError("This User rule is hidden by another effective rule.")

    knowledge_payload=_detached_object(before.system_knowledge,name="System Knowledge")
    if not isinstance(knowledge_payload.get("tags"),list):
        raise ValueError("System Knowledge has no canonical Tag inventory.")
    if any(not isinstance(item,dict) or not isinstance(item.get("id"),str)
           or not item["id"].strip() for item in knowledge_payload["tags"]):
        raise ValueError("System Knowledge has ambiguous Tag identities.")
    if not isinstance(knowledge_payload.get("merge_rules",[]),list):
        raise ValueError("System Knowledge has no canonical merge-rule inventory.")
    codec=KnowledgeLifecycleCodec("tag",TagDatabase.SCHEMA_VERSION,TagDatabase.ENROLLED_SCHEMA_VERSION)
    _decoded,parsed_tags=decode_tag_storage(
        before.system_knowledge.decode("utf-8"),TagRecord.from_dict,
        lifecycle_codec=codec,
    )
    if parsed_tags is None or len(parsed_tags)!=len(knowledge_payload["tags"]):
        raise ValueError("System Knowledge has ambiguous Tag identities.")
    removed=knowledge_payload.get("removed_tag_ids",[])
    redirects=knowledge_payload.get("merged_tag_ids",{})
    if not isinstance(removed,list) or not isinstance(redirects,dict):
        raise ValueError("System Knowledge retirement state is malformed.")
    codec.bind_current(tuple(parsed_tags),removed_ids=removed,redirects=redirects)
    if codec.write_hold:
        raise ValueError("System Knowledge lifecycle requires review: "+codec.write_hold)
    canonical={}
    for record in parsed_tags.values():
        key=normalize_tag(record.display)
        if not key or key in canonical:
            raise ValueError("System Knowledge preferred Tag names are ambiguous.")
        canonical[key]=record
    current_rules={}
    for item in knowledge_payload.get("merge_rules",[]):
        if (not isinstance(item,dict) or not isinstance(item.get("id"),str)
                or not item["id"].strip()):
            raise ValueError("System Knowledge contains an ambiguous merge rule.")
        rule=MergeRule.from_dict(item)
        if rule.id in current_rules or not rule.source_tags or not rule.target_tag:
            raise ValueError("System Knowledge contains an ambiguous merge rule.")
        current_rules[rule.id]=rule

    if not isinstance(sources,(tuple,list)) or any(not isinstance(value,str) for value in sources):
        raise ValueError("Choose exact literal source Tags.")
    proposed=AutomationRuleStore.rule_record(
        "merge_rules",sources,target,intent=TRAINING_PROJECTION,
    )
    values=(*proposed["remove"],proposed["replace"])
    if any("{" in value or "}" in value for value in values):
        raise ValueError("Template rules need their own reviewed conversion plan.")
    if any(normalize_tag(value) not in canonical for value in values):
        raise ValueError("Create and review every source and result Tag first.")
    user_after_layer=deepcopy(user_layer)
    del user_after_layer["merge_rules"][source_index]
    effective=combine_rule_layers(default_layer,user_after_layer)
    for rule in current_rules.values():
        if rule.enabled:
            effective["merge_rules"].append({
                "remove":list(rule.source_tags),"replace":rule.target_tag,
                "intent":str(rule.extras.get("intent") or TRAINING_PROJECTION),
                "rule_id":rule.id,
            })
    analysis=AutomationEngine(effective).analyze_projection_rule(
        proposed["remove"],proposed["replace"],
    )
    if analysis.errors or analysis.warnings or analysis.duplicate:
        raise ValueError("The destination conflicts with a current projection rule; review the exact rules again.")
    if tuple(analysis.sources)!=tuple(proposed["remove"]):
        raise ValueError("Projection analysis would change the requested sources; review that reduction first.")
    destination_id="merge_"+_slug(proposed["replace"])
    suffix=2
    while destination_id in current_rules:
        destination_id="merge_"+_slug(proposed["replace"])+f"_{suffix}"
        suffix+=1
    destination=MergeRule(
        destination_id,list(analysis.sources),proposed["replace"],
        require_review=False,description="Converted from a reviewed Automation rule",
        extras={"intent":TRAINING_PROJECTION},
    )
    user_payload=deepcopy(user_payload)
    del user_payload["merge_rules"][source_index]
    knowledge_payload=deepcopy(knowledge_payload)
    knowledge_payload.setdefault("merge_rules",[]).append(destination.to_dict())
    after=RuleAuthorityPair(*(
        (json.dumps(payload,indent=2,ensure_ascii=False)+"\n").encode("utf-8")
        for payload in (user_payload,knowledge_payload)
    ))
    return DetachedLiteralMergeConversion(
        RuleConversionPlan(before,after),
        hashlib.sha256(default_rules).hexdigest() if default_rules is not None else None,
        source_index,destination_id,tuple(analysis.sources),proposed["replace"],
    )


def _conversion_json(raw: bytes):
    def unique_pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate receipt fields are not supported.")
            result[key] = value
        return result

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs)
    except (ValueError, UnicodeError) as exc:
        raise RuleConversionAdmissionError("Synthetic conversion evidence is malformed.") from exc


def _conversion_read(path: Path, limit: int) -> bytes | None:
    """Bound reads and refuse redirection, special files and observed replacement."""
    initial = _checked_path(path)
    if initial is None:
        return None
    if initial.st_size > limit:
        raise RuleConversionAdmissionError("Synthetic conversion evidence is oversized.")
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        raw = handle.read(limit + 1)
        final = os.fstat(handle.fileno())
    current = _checked_path(path, required=True)
    # Windows fstat and lstat expose different creation/change-time precision;
    # use the same file identity fields as the canonical bounded-read helper.
    identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    if (len(raw) > limit or identity(initial) != identity(opened)
            or identity(opened) != identity(final) or identity(final) != identity(current)):
        raise RuleConversionAdmissionError("Synthetic conversion evidence changed during reading.")
    return raw


def _conversion_write(path: Path, raw: bytes) -> None:
    """Flush one sibling temporary then replace; no power-loss guarantee is claimed."""
    _checked_path(path.parent, directory=True, required=True)
    _checked_path(path)
    temporary = path.parent / ("." + path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _conversion_write_json(path: Path, payload: dict) -> None:
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    _conversion_write(path, raw)
    if _conversion_read(path, MAX_CONVERSION_RECEIPT_BYTES) != raw:
        raise RuleConversionAdmissionError("Synthetic conversion evidence failed read-back.")


class SyntheticRuleConversionSandbox:
    """Restartable byte-pair experiment; never attach this to a profile.

    ``create`` exclusively creates a new directory, seeds synthetic authorities,
    and writes a marker last. Reopening requires that exact marker. There is no
    production publisher, semantic plan builder, or application entry point.
    All cooperating writers must use the shared admission lock. External writes
    are detected by exact-state checks, not prevented by this advisory lock.
    """

    @classmethod
    def create(cls, root: str | Path, initial: RuleAuthorityPair):
        admission = RulePairAdmission(root)
        # Validate even an all-absent initial pair without inventing a change.
        for role in _ROLES:
            raw = getattr(initial, role)
            if raw is not None and (not isinstance(raw, bytes)
                                    or len(raw) > MAX_CONVERSION_PARTICIPANT_BYTES):
                raise ValueError("Synthetic authority bytes are invalid or oversized.")
        _checked_path(admission.root.parent, directory=True, required=True)
        admission.root.mkdir(exist_ok=False)
        for role, path in zip(_ROLES, (admission.rules_path, admission.knowledge_path)):
            raw = getattr(initial, role)
            if raw is not None:
                _conversion_write(path, raw)
        _conversion_write_json(admission.root / _SANDBOX_MARKER, {
            "schema_version": 1, "kind": "synthetic-rule-conversion",
            "authority_key": admission.authority_key, "sandbox_id": str(uuid4()),
        })
        return cls(admission.root)

    def __init__(self, root: str | Path):
        self.admission = RulePairAdmission(root)
        self.root = self.admission.root
        raw = _conversion_read(self.root / _SANDBOX_MARKER, MAX_CONVERSION_RECEIPT_BYTES)
        marker = _conversion_json(raw) if raw is not None else None
        if (not isinstance(marker, dict)
                or set(marker) != {"schema_version", "kind", "authority_key", "sandbox_id"}
                or type(marker["schema_version"]) is not int or marker["schema_version"] != 1
                or marker["kind"] != "synthetic-rule-conversion"
                or marker["authority_key"] != self.admission.authority_key):
            raise RuleConversionAdmissionError("An exact synthetic sandbox marker is required.")
        self._uuid(marker["sandbox_id"])
        self._marker = raw
        self.sandbox_id = marker["sandbox_id"]
        self.evidence_root = self.root / ".automation-rule-conversion-evidence"

    @staticmethod
    def _uuid(value):
        try:
            if not isinstance(value, str) or str(UUID(value)) != value:
                raise ValueError()
        except (ValueError, AttributeError) as exc:
            raise RuleConversionAdmissionError("Synthetic operation identity is invalid.") from exc
        return value

    def _check_marker(self):
        if _conversion_read(self.root / _SANDBOX_MARKER, MAX_CONVERSION_RECEIPT_BYTES) != self._marker:
            raise RuleConversionAdmissionError("The synthetic sandbox identity changed.")
        # The pair shares exclusion with linked Knowledge work; neither kind of
        # transaction may recover through the other's unresolved receipt.
        if KnowledgeWriteAdmission(self.root).inspect_pending():
            raise RuleConversionAdmissionError("Linked Knowledge recovery is pending.")

    def read_pair(self) -> RuleAuthorityPair:
        return RuleAuthorityPair(*(
            _conversion_read(path, MAX_CONVERSION_PARTICIPANT_BYTES)
            for path in (self.admission.rules_path, self.admission.knowledge_path)
        ))

    def _pending(self, operation_id: str | None = None):
        if operation_id is None:
            self.admission.assert_clear()
            return
        self._uuid(operation_id)
        _checked_path(self.admission.pending_root, directory=True, required=True)
        with os.scandir(self.admission.pending_root) as entries:
            names = []
            for entry in entries:
                names.append(entry.name)
                if len(names) > 1:
                    break
        if names != [operation_id + ".json"]:
            raise RuleConversionAdmissionError("Unknown or additional pending work holds recovery.")

    def _snapshot(self, bundle: Path, name: str, raw: bytes | None):
        if raw is None:
            return {"exists": False, "sha256": None, "size": 0}
        _conversion_write(bundle / name, raw)
        if _conversion_read(bundle / name, MAX_CONVERSION_PARTICIPANT_BYTES) != raw:
            raise RuleConversionAdmissionError("Synthetic snapshot failed read-back.")
        return {"exists": True, "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}

    def _load(self, operation_id: str):
        self._pending(operation_id)
        receipt_path = self.admission.pending_root / (operation_id + ".json")
        raw = _conversion_read(receipt_path, MAX_CONVERSION_RECEIPT_BYTES)
        receipt = _conversion_json(raw) if raw is not None else None
        if (not isinstance(receipt, dict)
                or set(receipt) != {"schema_version", "kind", "authority_key", "sandbox_id", "operation_id", "snapshots"}
                or type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1
                or receipt["kind"] != "synthetic-rule-conversion"
                or receipt["authority_key"] != self.admission.authority_key
                or receipt["sandbox_id"] != self.sandbox_id
                or receipt["operation_id"] != operation_id):
            raise RuleConversionAdmissionError("Synthetic receipt has an unsupported identity or schema.")
        bundle = self.evidence_root / operation_id
        _checked_path(bundle, directory=True, required=True)
        if _conversion_read(bundle / "receipt.json", MAX_CONVERSION_RECEIPT_BYTES) != raw:
            raise RuleConversionAdmissionError("Pending and retained synthetic receipts disagree.")
        expected_names = {phase + "_" + role for phase in ("before", "after") for role in _ROLES}
        snapshots = receipt["snapshots"]
        if not isinstance(snapshots, dict) or set(snapshots) != expected_names:
            raise RuleConversionAdmissionError("Synthetic receipt participants are invalid.")
        values = {}
        for name in sorted(expected_names):
            descriptor = snapshots[name]
            if (not isinstance(descriptor, dict) or set(descriptor) != {"exists", "sha256", "size"}
                    or type(descriptor["exists"]) is not bool or type(descriptor["size"]) is not int
                    or not 0 <= descriptor["size"] <= MAX_CONVERSION_PARTICIPANT_BYTES):
                raise RuleConversionAdmissionError("Synthetic snapshot descriptor is invalid.")
            value = _conversion_read(bundle / (name + ".bin"), MAX_CONVERSION_PARTICIPANT_BYTES)
            digest = hashlib.sha256(value).hexdigest() if value is not None else None
            if (descriptor["exists"] != (value is not None)
                    or descriptor["sha256"] != digest
                    or descriptor["size"] != (len(value) if value is not None else 0)):
                raise RuleConversionAdmissionError("Synthetic snapshot integrity failed.")
            values[name] = value
        plan = RuleConversionPlan(*(
            RuleAuthorityPair(*(values[phase + "_" + role] for role in _ROLES))
            for phase in ("before", "after")
        ))
        return plan, bundle, receipt_path, raw

    def _replace_participant(self, path: Path, raw: bytes | None):
        if raw is None:
            if _checked_path(path) is not None:
                path.unlink()
        else:
            _conversion_write(path, raw)
        if _conversion_read(path, MAX_CONVERSION_PARTICIPANT_BYTES) != raw:
            raise RuleConversionAdmissionError("Synthetic participant failed read-back.")

    def _complete(self, operation_id: str, action: str):
        plan, bundle, receipt_path, receipt_raw = self._load(operation_id)
        terminal_path = bundle / "terminal.json"
        terminal_raw = _conversion_read(terminal_path, MAX_CONVERSION_RECEIPT_BYTES)
        terminal = _conversion_json(terminal_raw) if terminal_raw is not None else None
        expected_terminal = {"operation_id": operation_id, "action": action}
        if terminal is not None and terminal != expected_terminal:
            raise RuleConversionAdmissionError("A finalized transaction cannot change recovery direction.")
        decision_path = bundle / "resolution.json"
        decision_raw = _conversion_read(decision_path, MAX_CONVERSION_RECEIPT_BYTES)
        if decision_raw is not None and _conversion_json(decision_raw) != expected_terminal:
            raise RuleConversionAdmissionError("Resume the already chosen recovery direction.")
        goal = plan.after if action == "finish" else plan.before
        current = self.read_pair()
        state = classify_rule_conversion(plan.before, plan.after, current)
        if state.outcome == "diverged":
            raise RuleConversionAdmissionError("Authorities diverged; no automatic overwrite is allowed.")
        if terminal is not None and current != goal:
            raise RuleConversionAdmissionError("Finalized authorities changed; recovery remains held.")
        for role, path in zip(_ROLES, (self.admission.rules_path, self.admission.knowledge_path)):
            self._check_marker()
            self._pending(operation_id)
            if (self.read_pair() != current
                    or _conversion_read(receipt_path, MAX_CONVERSION_RECEIPT_BYTES) != receipt_raw
                    or _conversion_read(decision_path, MAX_CONVERSION_RECEIPT_BYTES) != decision_raw):
                raise RuleConversionAdmissionError("Transaction evidence changed before publication.")
            desired = getattr(goal, role)
            if getattr(current, role) != desired:
                self._replace_participant(path, desired)
                current = RuleAuthorityPair(*(
                    desired if key == role else getattr(current, key) for key in _ROLES
                ))
        if self.read_pair() != goal:
            raise RuleConversionAdmissionError("Synthetic pair failed final verification.")
        _conversion_write_json(terminal_path, expected_terminal)
        self._check_marker()
        self._pending(operation_id)
        if (self.read_pair() != goal
                or _conversion_read(receipt_path, MAX_CONVERSION_RECEIPT_BYTES) != receipt_raw
                or _conversion_read(decision_path, MAX_CONVERSION_RECEIPT_BYTES) != decision_raw):
            raise RuleConversionAdmissionError("Transaction changed before finalization.")
        receipt_path.unlink()
        return "committed" if action == "finish" else "restored"

    def commit(self, plan: RuleConversionPlan) -> str:
        """Publish a synthetic pending receipt before writing either participant."""
        if not isinstance(plan, RuleConversionPlan):
            raise TypeError("An immutable exact-pair plan is required.")
        with KnowledgeWriteAdmission(self.root).locked():
            self._check_marker()
            self._pending()
            if self.read_pair() != plan.before:
                raise RuleConversionAdmissionError("Authorities changed after the synthetic plan was prepared.")
            for directory in (self.evidence_root, self.admission.pending_root):
                directory.mkdir(exist_ok=True)
                _checked_path(directory, directory=True, required=True)
            operation_id = str(uuid4())
            bundle = self.evidence_root / operation_id
            bundle.mkdir()
            snapshots = {}
            for phase in ("before", "after"):
                for role in _ROLES:
                    name = phase + "_" + role
                    snapshots[name] = self._snapshot(bundle, name + ".bin", getattr(getattr(plan, phase), role))
            self._check_marker()
            self._pending()
            if self.read_pair() != plan.before:
                raise RuleConversionAdmissionError("Authorities changed before pending publication.")
            receipt = {
                "schema_version": 1, "kind": "synthetic-rule-conversion",
                "authority_key": self.admission.authority_key, "sandbox_id": self.sandbox_id,
                "operation_id": operation_id, "snapshots": snapshots,
            }
            _conversion_write_json(bundle / "receipt.json", receipt)
            _conversion_write_json(self.admission.pending_root / (operation_id + ".json"), receipt)
            self._complete(operation_id, "finish")
            return operation_id

    def recover(self, operation_id: str, *, action: Literal["finish", "restore"]) -> str:
        """Explicit recovery; a durable chosen direction survives interrupted retry."""
        if action not in {"finish", "restore"}:
            raise ValueError("Choose finish or restore.")
        with KnowledgeWriteAdmission(self.root).locked():
            self._check_marker()
            plan, bundle, _path, _raw = self._load(operation_id)
            if classify_rule_conversion(plan.before, plan.after, self.read_pair()).outcome == "diverged":
                raise RuleConversionAdmissionError("Authorities diverged; recovery must not overwrite them.")
            terminal_raw = _conversion_read(bundle / "terminal.json", MAX_CONVERSION_RECEIPT_BYTES)
            if terminal_raw is not None:
                if _conversion_json(terminal_raw) != {"operation_id": operation_id, "action": action}:
                    raise RuleConversionAdmissionError("A finalized transaction cannot change recovery direction.")
                return self._complete(operation_id, action)
            decision_path = bundle / "resolution.json"
            raw = _conversion_read(decision_path, MAX_CONVERSION_RECEIPT_BYTES)
            decision = {"operation_id": operation_id, "action": action}
            if raw is not None and _conversion_json(raw) != decision:
                raise RuleConversionAdmissionError("Resume the already chosen recovery direction.")
            if raw is None:
                _conversion_write_json(decision_path, decision)
            return self._complete(operation_id, action)
