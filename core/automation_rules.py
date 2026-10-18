from __future__ import annotations

import json
import re
import shutil
from copy import deepcopy
from contextlib import nullcontext
from functools import wraps
from dataclasses import asdict,dataclass,field
from datetime import datetime,timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from uuid import uuid4

from .annotation import ImageAnnotation
from .automation_templates import (
    AUTOMATION_PLACEHOLDER,
    concrete_sdxl_tags,
    has_automation_placeholder,
)
from .image_record import ImageRecord
from .rule_intents import (
    REJECT_INCOMING_EVIDENCE,
    TRAINING_PROJECTION,
    explicit_rule_intent,
    suggested_rule_intent,
)
from .tag_collection import TagCollection, normalize_tag
from .operation_journal import OperationJournal
from .automation_rule_conversion import RulePairAdmission


DEFAULT_RULES = {
    "variables": {}, "merge_rules": [], "wildcard_rules": [], "combine_rules": [], "ignore_rules": [],
    "disabled_rules": [], "variable_overrides": {}, "tag_templates": [],
}

AUTOMATION_RULE_SCHEMA_VERSION = 1


def _admitted_rule_use(method):
    """Recheck retained bound authority; publication still needs its own lease."""
    @wraps(method)
    def guarded(self, *args, **kwargs):
        admission = self._bound_rule_admission or self.rule_pair_admission
        if admission is not None:
            admission.assert_clear()
            if (self.rule_pair_admission is not self._bound_rule_admission
                    or self.knowledge is not self._bound_knowledge
                    or getattr(self.knowledge, "rule_pair_admission", None) is not self._bound_knowledge_admission
                    or getattr(self.knowledge, "_relationship_signature_revision", None) != self._bound_knowledge_revision):
                raise RuntimeError("System Knowledge changed. Acquire a fresh evaluator before continuing.")
        return method(self, *args, **kwargs)
    return guarded


class AutomationRuleStore:
    """Read and safely edit the current layered automation-rule format."""

    def __init__(self, path: str | Path, *, rule_pair_admission: RulePairAdmission | None = None) -> None:
        self.path = Path(path)
        if rule_pair_admission is not None and not rule_pair_admission.owns(self.path, "rules"):
            raise ValueError("Rules admission must bind this exact user-rule path.")
        self.rule_pair_admission = rule_pair_admission
        self.data: dict[str, Any] = {}
        self.load()

    def load(self) -> dict[str, Any]:
        raw=None; source_error=""
        try:
            raw=self.path.read_bytes() if self.path.exists() else None
        except OSError:
            source_error=(
                "Rules & Automation storage is unavailable. Leave it unchanged, "
                "restore access, then reload and review the current rules."
            )
        try:
            payload = json.loads(raw.decode("utf-8")) if raw is not None else {}
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            payload = {}
            source_error=(
                "Rules & Automation storage is malformed. It was left unchanged; "
                "repair or replace the file, then reload and review the current rules."
            )
        if not isinstance(payload,dict):
            payload={}
            source_error=(
                "Rules & Automation storage is malformed because its top level is "
                "not an object. It was left unchanged; repair or replace the file, "
                "then reload and review the current rules."
            )
        version=payload.get("schema_version")
        if "schema_version" in payload and (
            isinstance(version,bool) or not isinstance(version,int)
            or version<1 or version>AUTOMATION_RULE_SCHEMA_VERSION
        ):
            source_error=(
                "Rules & Automation storage uses an unsupported schema. It was left "
                "unchanged; update CollectiGen or replace the file, then reload and "
                "review the current rules."
            )
        self._payload_extras={
            key:value for key,value in payload.items() if key not in DEFAULT_RULES
        }
        self.data = {key: payload.get(key, value.copy() if isinstance(value, dict) else list(value)) for key, value in DEFAULT_RULES.items()}
        self._loaded_bytes=raw
        self._source_error=source_error
        return self.data

    def _payload(self) -> dict[str,Any]:
        payload=deepcopy(self._payload_extras)
        payload.update(deepcopy(self.data))
        return payload

    def save(self) -> None:
        with (self.rule_pair_admission.admit() if self.rule_pair_admission else nullcontext()):
            self._save_unadmitted()

    def _save_unadmitted(self) -> None:
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.path.with_suffix(self.path.suffix+".tmp")
        temporary.write_text(json.dumps(self._payload(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(self.path)
        self._loaded_bytes=self.path.read_bytes()
        self._source_error=""

    def mutate_verified(self,mutator) -> Any:
        """Apply one in-memory mutation and persist it as one verified action."""
        before_data=deepcopy(self.data); before_extras=deepcopy(self._payload_extras)
        try:
            result=mutator(self.data)
        except BaseException:
            self.data=before_data; self._payload_extras=before_extras
            raise
        try:
            self.save_verified()
        except BaseException:
            # A stale rejection adopts the newer readable authority for an
            # immediate UI retry. Malformed/unavailable/future input remains a
            # held read-only state with its exact source bytes untouched.
            self.load()
            raise
        return result

    def save_verified(self, *, backup_root: str | Path | None = None) -> Path:
        """Save one rule-file transaction with stale guard, backup, and read-back."""
        with (self.rule_pair_admission.admit() if self.rule_pair_admission else nullcontext()):
            return self._save_verified_unadmitted(backup_root=backup_root)

    def _save_verified_unadmitted(self, *, backup_root: str | Path | None = None) -> Path:
        if getattr(self,"_source_error",""):
            raise RuntimeError(self._source_error)
        try:current=self.path.read_bytes() if self.path.exists() else None
        except OSError as exc:
            raise RuntimeError(
                "Rules & Automation storage became unavailable. It was left "
                "unchanged; restore access, then reload and review the current rules."
            ) from exc
        if current!=getattr(self,"_loaded_bytes",None):
            raise RuntimeError(
                "Rules & Automation changed after this action opened. The newer "
                "saved rules were left unchanged. Reload and review them before retrying."
            )
        stamp=datetime.now().strftime("%Y%m%d_%H%M%S")
        root=Path(backup_root) if backup_root is not None else self.path.parent/"backups"/"automation-rules"
        backup=root/f"{stamp}_{uuid4().hex[:8]}"; backup.mkdir(parents=True,exist_ok=False)
        saved=backup/self.path.name
        if current is not None:shutil.copy2(self.path,saved)
        expected=json.loads(json.dumps(self._payload()))
        try:
            self.save()
            actual=json.loads(self.path.read_text(encoding="utf-8"))
            if actual!=expected:raise RuntimeError("Saved Rules & Automation changes failed read-back verification.")
        except BaseException:
            if current is None:
                if self.path.exists():self.path.unlink()
            else:
                temporary=self.path.with_suffix(self.path.suffix+".restore.tmp"); temporary.write_bytes(current); temporary.replace(self.path)
            self.load(); raise
        return backup

    def set_variable(self, name: str, values: Iterable[str]) -> None:
        clean_name = name.strip().strip("{}")
        if not clean_name: raise ValueError("Enter a variable name.")
        clean_values=TagCollection(values).to_list()
        self.mutate_verified(
            lambda data:data["variables"].__setitem__(clean_name,clean_values)
        )

    def delete_variable(self, name: str) -> None:
        self.mutate_verified(lambda data:data["variables"].pop(name,None))

    @staticmethod
    def rule_record(
        kind: str,source: Iterable[str],target: str,*,pattern: str="",intent: str="",
    ) -> dict[str,Any]:
        target=target.strip(); source=[value.strip() for value in source if value.strip()]
        if kind == "ignore_rules":
            if len(source) != 1: raise ValueError("Select one tag to always ignore.")
            record = {"tag": source[0]}
        elif kind == "wildcard_rules":
            if not pattern.strip() or not source: raise ValueError("Enter a specific pattern and at least one broader tag to remove.")
            record = {"pattern": pattern.strip(), "remove": source[0] if len(source) == 1 else source}
        elif kind in {"merge_rules","combine_rules"}:
            if len(source) < 2 or not target: raise ValueError("Enter at least two matching tags and a result tag.")
            record = {"remove": source, "replace": target}
        else:
            raise ValueError("Choose a supported Automation rule type.")
        record["intent"] = intent or suggested_rule_intent(kind)
        return record

    def add_rule(
        self,
        kind: str,
        source: Iterable[str],
        target: str,
        *,
        pattern: str = "",
        intent: str = "",
    ) -> None:
        record=self.rule_record(kind,source,target,pattern=pattern,intent=intent)
        self.mutate_verified(
            lambda data:data[kind].append(record) if record not in data[kind] else None
        )

    def replace_rule(self, kind: str, index: int, record: dict[str, Any]) -> None:
        self.mutate_verified(lambda data:data[kind].__setitem__(index,record))

    def delete_rule(self, kind: str, index: int) -> None:
        self.mutate_verified(lambda data:data[kind].pop(index))


@dataclass
class RuleStep:
    label: str
    before: list[str]
    after: list[str]
    confidence: float = 1.0
    kind: str = ""
    sources: list[str] = field(default_factory=list)
    target: str = ""


@dataclass
class RuleImpact:
    image: Path
    before: list[str]
    after: list[str]
    rules: list[str]
    confidence: float = 1.0
    steps: list[RuleStep] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProjectionRuleAnalysis:
    """One safe normalization decision for a training-projection rule."""

    original_sources: tuple[str, ...]
    sources: tuple[str, ...]
    target: str
    redundant_sources: tuple[str, ...] = ()
    duplicate: bool = False
    related_rules: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return self.sources != self.original_sources


@dataclass
class AutomationChange:
    image: str
    before: dict[str, Any]
    after: dict[str, Any]
    rules: list[str]


class AutomationCheckpointStore:
    """Compatibility facade over the shared append-only operation journal."""

    def __init__(self,path: str | Path) -> None:
        self.path=Path(path)
        journal_path=self.path if self.path.suffix==".jsonl" else self.path.with_name("operation_journal.jsonl")
        self.journal=OperationJournal(journal_path)
        self.operation_id=""
        if self.path!=journal_path and self.path.exists() and not journal_path.exists():
            try:
                legacy=json.loads(self.path.read_text(encoding="utf-8")); changes=list(legacy.get("changes",[]))
                operation_id=self.journal.begin("automation",expected=int(legacy.get("expected") or len(changes)),reason="Imported legacy automation checkpoint")
                for item in changes:
                    self.journal.append_change(operation_id,item=str(item["image"]),before=item["before"],after=item.get("after",{}),reason="; ".join(item.get("rules",[])),metadata={"rules":list(item.get("rules",[]))})
                self.journal.finish(operation_id,"complete" if legacy.get("status")=="complete" else "failed")
            except (OSError,json.JSONDecodeError,KeyError,TypeError,ValueError): pass

    def _write(self,payload: dict[str,Any]) -> None:
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.path.with_suffix(self.path.suffix+".tmp")
        temporary.write_text(json.dumps(payload,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
        temporary.replace(self.path)

    def begin(self,total: int, impacts: Iterable[RuleImpact] = ()) -> str:
        plan=[{
            "image":str(item.image),"before":list(item.before),"after":list(item.after),
            "rules":list(item.rules),"confidence":float(item.confidence),
        } for item in impacts]
        self.operation_id=self.journal.begin(
            "automation",expected=total,reason="Apply reviewed automation rules",
            metadata={"plan":plan},
        )
        return self.operation_id

    def append(self,change: AutomationChange) -> None:
        operation_id=self.operation_id or (self.journal.latest(kind="automation",statuses=("applying",)).id if self.journal.latest(kind="automation",statuses=("applying",)) else "")
        if not operation_id: raise RuntimeError("Begin the automation operation before recording changes.")
        self.journal.append_change(
            operation_id,item=change.image,before=change.before,after=change.after,
            reason="; ".join(change.rules),metadata={"rules":list(change.rules)},
        )

    def complete(self) -> None:
        if self.operation_id: self.journal.finish(self.operation_id,"complete")
        self.journal.prune()

    def cancel(self) -> None:
        if self.operation_id: self.journal.finish(self.operation_id,"cancelled")

    def fail(self,message: str) -> None:
        if self.operation_id: self.journal.finish(self.operation_id,"failed",message=message)

    def load_payload(self) -> dict[str,Any]:
        latest=self.journal.latest(kind="automation")
        if latest and latest.status in {"applying","complete","cancelled","failed"}:
            return {"schema_version":1,"status":latest.status,"expected":latest.expected,"changes":[
                {"image":item["item"],"before":self.journal.load_snapshot(item["before"]),
                 "after":self.journal.load_snapshot(item["after"]),"rules":item["metadata"].get("rules",[])}
                for item in latest.changes
            ]}
        if latest:
            return {}
        try: payload=json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError,json.JSONDecodeError,TypeError): payload={}
        return payload if isinstance(payload,dict) else {}

    def changes(self) -> list[AutomationChange]:
        result=[]
        for item in self.load_payload().get("changes",[]):
            try: result.append(AutomationChange(str(item["image"]),dict(item["before"]),dict(item.get("after",{})),list(item.get("rules",[]))))
            except (KeyError,TypeError,ValueError): continue
        return result

    def exists(self) -> bool:
        return bool(self.changes())

    def pending(self) -> bool:
        latest=self.journal.latest(kind="automation")
        return bool(latest and latest.status=="applying")

    def remaining_impacts(self) -> list[RuleImpact]:
        operation=self.journal.latest(kind="automation")
        if operation is None or operation.status!="applying": return []
        completed={item["item"] for item in operation.changes}
        result=[]
        for item in operation.metadata.get("plan",[]):
            if str(item.get("image")) in completed: continue
            try: result.append(RuleImpact(Path(item["image"]),list(item["before"]),list(item["after"]),list(item["rules"]),float(item.get("confidence",1))))
            except (KeyError,TypeError,ValueError): continue
        self.operation_id=operation.id
        return result

    def clear(self) -> None:
        latest=self.journal.latest(kind="automation")
        if latest and latest.status in {"complete","cancelled","failed","applying"}: self.journal.finish(latest.id,"undone")
        try: self.path.unlink()
        except FileNotFoundError: pass


class AutomationEngine:
    """Project complete Image Knowledge into a concise training-tag view.

    Merge, wildcard, combine, and ignore rules are projection policy. They do
    not establish that a confirmed image fact is false and therefore must not
    be used to rewrite authoritative Image Knowledge.
    """

    placeholder = AUTOMATION_PLACEHOLDER

    @staticmethod
    def _variable_key(value: str) -> str:
        """Normalize a variable identifier without changing its separators."""
        return " ".join(str(value or "").strip().strip("{}").casefold().split())

    @classmethod
    def _normalize_template(cls, template: str) -> str:
        """Normalize tag literals while preserving placeholder identifiers."""
        protected: list[str] = []

        def protect(match: re.Match[str]) -> str:
            protected.append(match.group(1))
            return f"{{tmplaceholder{len(protected)-1}token}}"

        normalized = normalize_tag(cls.placeholder.sub(protect, str(template or "")))
        for index, name in enumerate(protected):
            normalized = normalized.replace(
                f"{{tmplaceholder{index}token}}", "{" + cls._variable_key(name) + "}"
            )
        return normalized

    def __init__(self, rules: dict[str, Any], knowledge=None, conflicts=None,
                 *, rule_pair_admission: RulePairAdmission | None = None) -> None:
        self.rules = rules
        self.knowledge = knowledge
        self.conflicts = conflicts
        self.rule_pair_admission = rule_pair_admission
        self._bound_rule_admission = rule_pair_admission
        self._bound_knowledge = knowledge
        self._bound_knowledge_admission = getattr(knowledge, "rule_pair_admission", None)
        self._bound_knowledge_revision = getattr(knowledge, "_relationship_signature_revision", None)
        self.variables = {
            self._variable_key(str(key)): [normalize_tag(v) for v in values]
            for key, values in rules.get("variables", {}).items()
        }

    def _prepare_match(
        self, template: str, bindings: dict[str, str],
    ) -> Callable[[str], dict[str, str] | None] | None:
        """Prepare one DFS branch; the returned matcher accepts normalized tags.

        Keep preparation local to the search so later rule/variable edits are
        observed without retaining a compiled authority or image-result cache.
        """
        template = self._normalize_template(template)
        bindings = {self._variable_key(key): value for key, value in bindings.items()}
        if "{" not in template:
            return lambda value: dict(bindings) if template == value else None
        parts=[]; position=0; names=[]
        for match in self.placeholder.finditer(template):
            parts.append(re.escape(template[position:match.start()]))
            name=self._variable_key(match.group(1)); names.append(name)
            if name in bindings: parts.append(re.escape(bindings[name]))
            else:
                choices=self.variables.get(name, [])
                if not choices: return None
                parts.append(f"(?P<v{len(names)-1}>"+"|".join(re.escape(item) for item in sorted(choices,key=len,reverse=True))+")")
            position=match.end()
        parts.append(re.escape(template[position:]))
        pattern=re.compile("".join(parts),re.IGNORECASE)

        def match_value(value: str) -> dict[str, str] | None:
            found=pattern.fullmatch(value)
            if not found: return None
            result=dict(bindings)
            for group_index,name in enumerate(names):
                if name not in result:
                    captured=found.groupdict().get(f"v{group_index}")
                    if captured: result[name]=normalize_tag(captured)
            return result

        return match_value

    def _match(self, template: str, value: str, bindings: dict[str, str]) -> dict[str, str] | None:
        matcher=self._prepare_match(template,bindings)
        return matcher(normalize_tag(value)) if matcher is not None else None

    def _find_sources(self, templates: list[str], tags: list[str]) -> tuple[list[str],dict[str,str]] | None:
        if all("{" not in template for template in templates):
            available={normalize_tag(tag):tag for tag in tags}
            keys=[normalize_tag(template) for template in templates]
            if len(set(keys))==len(keys) and all(key in available for key in keys):
                return [available[key] for key in keys],{}
            return None
        normalized_tags=[normalize_tag(tag) for tag in tags]
        def visit(index: int, used: set[int], bindings: dict[str,str], matched: list[str]):
            if index == len(templates): return matched, bindings
            if len(used) == len(tags): return None
            matcher=self._prepare_match(templates[index],bindings)
            if matcher is None: return None
            for tag_index, tag in enumerate(tags):
                if tag_index in used: continue
                updated=matcher(normalized_tags[tag_index])
                if updated is None: continue
                result=visit(index+1,used|{tag_index},updated,matched+[tag])
                if result: return result
            return None
        return visit(0,set(),{},[])

    @classmethod
    def _render(cls,template: str,bindings: dict[str,str]) -> str | None:
        rendered=cls.placeholder.sub(
            lambda match: bindings.get(cls._variable_key(match.group(1)),match.group(0)),
            template,
        )
        return None if has_automation_placeholder(rendered) else rendered

    @classmethod
    def projection_rule_key(cls,sources: Iterable[str],target: str) -> tuple[tuple[str,...],str]:
        return (
            tuple(sorted(cls._normalize_template(value) for value in sources if str(value).strip())),
            cls._normalize_template(target),
        )

    @_admitted_rule_use
    def analyze_projection_rule(
        self,
        sources: Iterable[str],
        target: str,
        *,
        exclude_rule_id: str = "",
        exclude_rule_key: tuple[tuple[str,...],str] | None = None,
    ) -> ProjectionRuleAnalysis:
        """Normalize only rule prerequisites proven redundant by active rules.

        This intentionally does not use fuzzy text similarity. A prerequisite is
        removed only when an active projection rule already reduces that exact
        prerequisite set to another source retained by the proposed rule.
        """
        original=tuple(TagCollection(str(value).strip() for value in sources if str(value).strip()).to_list())
        target=str(target or "").strip()
        current=list(original); removed=[]; related=[]; warnings=[]; errors=[]
        target_variables={self._variable_key(name) for name in self.placeholder.findall(target)}
        source_variables={
            self._variable_key(name)
            for source in current
            for name in self.placeholder.findall(source)
        }
        missing=sorted(target_variables-source_variables)
        if missing:
            errors.append(
                "The result uses reusable value(s) not bound by the matching tags: "
                + ", ".join(f"{{{name}}}" for name in missing)
            )
        undefined=sorted(
            {
                self._variable_key(name)
                for value in (*current,target)
                for name in self.placeholder.findall(value)
                if self._variable_key(name) not in self.variables
            }
        )
        if undefined:
            errors.append(
                "The rule uses undefined reusable value(s): "
                + ", ".join(f"{{{name}}}" for name in undefined)
            )

        existing=[]; skipped_key=False
        for kind in ("combine_rules","merge_rules","wildcard_rules"):
            for record in self.rules.get(kind,[]):
                if explicit_rule_intent(kind,record) not in {"",TRAINING_PROJECTION}: continue
                if exclude_rule_id and str(record.get("rule_id") or "")==exclude_rule_id: continue
                if kind=="wildcard_rules":
                    remove=record.get("remove",[]); remove=[remove] if isinstance(remove,str) else list(remove)
                    rule_sources=[str(record.get("pattern") or ""),*map(str,remove)]
                    rule_target=str(record.get("pattern") or "")
                else:
                    remove=record.get("remove",[]); remove=[remove] if isinstance(remove,str) else list(remove)
                    rule_sources=list(map(str,remove)); rule_target=str(record.get("replace") or "")
                key=self.projection_rule_key(rule_sources,rule_target)
                if exclude_rule_key is not None and not skipped_key and key==exclude_rule_key:
                    skipped_key=True; continue
                if len(key[0])>=2 and key[1]: existing.append((rule_sources,rule_target,key))

        changed=True
        while changed:
            changed=False
            current_key=set(self._normalize_template(value) for value in current)
            for rule_sources,rule_target,key in existing:
                source_key=set(key[0]); target_key=key[1]
                # Safe composition: A + B -> B makes A redundant in a new
                # rule that already retains B as one of its prerequisites.
                if target_key not in source_key or target_key not in current_key:
                    continue
                if not source_key.issubset(current_key):
                    continue
                redundant=source_key-{target_key}
                candidate=[value for value in current if self._normalize_template(value) not in redundant]
                if len(candidate)<2:
                    continue
                removed_now=[value for value in current if self._normalize_template(value) in redundant]
                if not removed_now:
                    continue
                current=candidate; removed.extend(value for value in removed_now if value not in removed)
                summary=f"{' + '.join(rule_sources)} → {rule_target}"
                if summary not in related: related.append(summary)
                changed=True
                break

        proposed_key=self.projection_rule_key(current,target)
        duplicate=any(key==proposed_key for _sources,_target,key in existing)
        proposed_sources=set(proposed_key[0])
        for rule_sources,rule_target,key in existing:
            if set(key[0])==proposed_sources and key[1]!=proposed_key[1]:
                warnings.append(
                    f"The same matching tags already project to {rule_target}: "
                    f"{' + '.join(rule_sources)}."
                )
        if len(current)<2:
            errors.append("A merge projection must retain at least two distinct matching tags.")
        return ProjectionRuleAnalysis(
            original,tuple(current),target,tuple(removed),duplicate,
            tuple(related),tuple(dict.fromkeys(warnings)),tuple(dict.fromkeys(errors)),
        )

    @_admitted_rule_use
    def project_tags(self, values: Iterable[str], *, automatic_only: bool = False) -> tuple[list[str],list[str]]:
        """Return one non-authoritative training projection of ``values``."""
        tags,steps=self._evaluate_steps(values,automatic_only=automatic_only)
        return tags,[step.label for step in steps]

    @_admitted_rule_use
    def apply_to_tags(self, values: Iterable[str], *, automatic_only: bool = False) -> tuple[list[str],list[str]]:
        """Compatibility name for :meth:`project_tags`.

        Callers that own confirmed Image Knowledge must retain their source
        values and treat this result only as an export or preview projection.
        """
        return self.project_tags(values,automatic_only=automatic_only)

    @_admitted_rule_use
    def project_tags_with_steps(
        self, values: Iterable[str], *, automatic_only: bool = False
    ) -> tuple[list[str], tuple[RuleStep, ...]]:
        """Return the training projection plus structured rule evidence."""
        tags, steps = self._evaluate_steps(values, automatic_only=automatic_only)
        return tags, tuple(steps)

    @_admitted_rule_use
    def is_evidence_rejected(self, candidate: str) -> bool:
        """Whether an exact incoming tag is suppressed by a global ignore rule."""
        key=normalize_tag(candidate)
        return bool(key and any(
            normalize_tag(str(record.get("tag","")))==key
            for record in self.rules.get("ignore_rules",[])
            if (
                str(record.get("tag","")).strip()
                and explicit_rule_intent("ignore_rules",record)
                in {"",REJECT_INCOMING_EVIDENCE}
            )
        ))

    @_admitted_rule_use
    def is_redundant_addition(self,values: Iterable[str],candidate: str) -> bool:
        """Whether the training projection reduces a prospective tag back out."""
        key=normalize_tag(candidate)
        after,_=self.project_tags([*values,candidate],automatic_only=True)
        return key not in TagCollection(after).normalized_set()

    @staticmethod
    def _conflict_key(match) -> tuple:
        return (
            match.rule.id,match.character_slot,
            getattr(match,"object_slot",None),bool(match.ambiguous),
            str(getattr(match,"context_id","") or ""),
            str(getattr(match,"owner_kind","") or ""),
            str(getattr(match,"owner_id","") or ""),
            tuple(sorted(normalize_tag(value) for value in match.found_tags)),
        )

    @_admitted_rule_use
    def conflicts_for_tags(self,image: str|Path|ImageRecord,values: Iterable[str]):
        """Return canonical diagnostic conflicts for one prospective tag state."""
        if self.conflicts is None: return ()
        record=image if isinstance(image,ImageRecord) else ImageRecord(image)
        trial=ImageRecord(record.image_path)
        trial.annotation=deepcopy(record.annotation)
        desired_values=TagCollection(values).to_list()
        desired=TagCollection(desired_values).normalized_set()
        for tag in list(trial.annotation.all_structured_tags()):
            if normalize_tag(tag) not in desired: trial.remove_tag(tag)
        current=TagCollection(trial.annotation.all_structured_tags()).normalized_set()
        for tag in desired_values:
            if normalize_tag(tag) not in current:
                trial.add_tag(tag); current.add(normalize_tag(tag))
        return tuple(self.conflicts.evaluate(trial))

    @_admitted_rule_use
    def introduced_conflicts(self,image: str|Path,before: Iterable[str],after: Iterable[str]):
        """Return structured canonical conflicts introduced by a proposed state."""
        if self.conflicts is None: return ()
        baseline={
            self._conflict_key(item)
            for item in self.conflicts_for_tags(image,before)
        }
        return tuple(
            item for item in self.conflicts_for_tags(image,after)
            if self._conflict_key(item) not in baseline
        )

    @_admitted_rule_use
    def conflict_warnings(self,image: str|Path,before: Iterable[str],after: Iterable[str]) -> list[str]:
        """Return semantic conflicts introduced by a proposed tag projection."""
        return [
            item.message
            for item in self.introduced_conflicts(image,before,after)
        ]

    @_admitted_rule_use
    def introduced_record_conflicts(
        self,before: ImageRecord,after: ImageRecord,
    ):
        """Compare complete owner/context placement without flattening to values."""
        if self.conflicts is None:
            return ()
        baseline={
            self._conflict_key(item) for item in self.conflicts.evaluate(before)
        }
        return tuple(
            item for item in self.conflicts.evaluate(after)
            if self._conflict_key(item) not in baseline
        )

    def _evaluate_steps(self,values: Iterable[str], *, automatic_only: bool = False) -> tuple[list[str],list[RuleStep]]:
        canonical=(
            self.knowledge.find(value).display
            if self.knowledge is not None and self.knowledge.find(value) is not None
            else value
            for value in values
        )
        original=TagCollection(canonical).to_list()
        tags=concrete_sdxl_tags(original); steps=[]; rules=[]
        for value in original:
            if not has_automation_placeholder(value):
                continue
            steps.append(RuleStep(
                f"Hold unresolved template: {value} → omitted from projection",
                [*tags,value],
                list(tags),
                1.0,
                kind="omit",
                sources=[value],
            ))
        ignored={
            normalize_tag(str(record.get("tag","")))
            for record in self.rules.get("ignore_rules",[])
            if (
                str(record.get("tag","")).strip()
                and explicit_rule_intent("ignore_rules",record)
                in {"",TRAINING_PROJECTION}
            )
        }
        for tag in list(tags):
            if normalize_tag(tag) not in ignored: continue
            before=list(tags); tags=[value for value in tags if normalize_tag(value)!=normalize_tag(tag)]
            steps.append(
                RuleStep(
                    f"Ignore: {tag} → removed",
                    before,
                    list(tags),
                    1.0,
                    kind="omit",
                    sources=[tag],
                )
            )
        for kind,label in (("combine_rules","Merge"),("merge_rules","Merge")):
            for record in self.rules.get(kind,[]):
                if explicit_rule_intent(kind,record) not in {"",TRAINING_PROJECTION}: continue
                if automatic_only and (record.get("requires_review") or float(record.get("confidence",1))<.95): continue
                rules.append((label,list(record.get("remove",[])),str(record.get("replace",'')),float(record.get("confidence",.85 if kind=="combine_rules" else 1.0))))
        for record in self.rules.get("wildcard_rules",[]):
            if explicit_rule_intent("wildcard_rules",record) not in {"",TRAINING_PROJECTION}: continue
            if automatic_only and (record.get("requires_review") or float(record.get("confidence",1))<.95): continue
            remove=record.get("remove",[]); remove=[remove] if isinstance(remove,str) else list(remove)
            pattern=str(record.get("pattern",'')); rules.append(("Merge",[pattern,*remove],pattern,float(record.get("confidence",.98))))
        for label,sources,target,confidence in rules:
            if not sources or not target: continue
            match=self._find_sources(sources,tags)
            if not match: continue
            matched,bindings=match; replacement=self._render(target,bindings)
            if replacement is None:
                continue
            before=list(tags)
            remove={normalize_tag(value) for value in matched}; tags=[tag for tag in tags if normalize_tag(tag) not in remove]
            if replacement and normalize_tag(replacement) not in {normalize_tag(tag) for tag in tags}: tags.append(replacement)
            tags=TagCollection(tags).to_list()
            steps.append(
                RuleStep(
                    f"{label}: {', '.join(matched)} → {replacement}",
                    before,
                    list(tags),
                    confidence,
                    kind="combine",
                    sources=list(matched),
                    target=replacement,
                )
            )
        return tags,steps

    @staticmethod
    def compose_impact(impact: RuleImpact, selected_steps: Iterable[int]) -> RuleImpact:
        """Build a stale-guarded partial impact from independently approved preview steps."""
        selected={int(index) for index in selected_steps}; tags=TagCollection(impact.before).to_list(); applied=[]; confidences=[]; kept=[]
        for index,step in enumerate(impact.steps):
            if index not in selected: continue
            before_keys=TagCollection(step.before).normalized_set(); after_keys=TagCollection(step.after).normalized_set()
            removed=before_keys-after_keys; current=TagCollection(tags); current_keys=current.normalized_set()
            if removed and not removed.issubset(current_keys): continue
            tags=[tag for tag in tags if normalize_tag(tag) not in removed]
            for tag in step.after:
                if normalize_tag(tag) not in before_keys and normalize_tag(tag) not in TagCollection(tags).normalized_set(): tags.append(tag)
            tags=TagCollection(tags).to_list(); applied.append(step.label); confidences.append(step.confidence); kept.append(step)
        return RuleImpact(impact.image,list(impact.before),tags,applied,min(confidences) if confidences else impact.confidence,kept)

    @_admitted_rule_use
    def preview(self, images: Iterable[Path], progress=None, should_cancel=None,
                *, automatic_only: bool = False) -> list[RuleImpact]:
        """Preview training projections without changing Image Knowledge."""
        paths=list(images); impacts=[]
        for index,image in enumerate(paths,start=1):
            if should_cancel and should_cancel(): break
            record=ImageRecord(image); before=record.annotation.all_structured_tags(); after,steps=self._evaluate_steps(before,automatic_only=automatic_only)
            if steps:
                warnings=self.conflict_warnings(image,before,after)
                impacts.append(RuleImpact(image,before,after,[step.label for step in steps],min(step.confidence for step in steps),steps,warnings))
            if progress: progress(index,len(paths),image.name)
        return impacts

    def apply(self, impact: RuleImpact, *, operation_id: str = "") -> AutomationChange:
        raise RuntimeError(
            "Merge and ignore rules are training-projection policy and cannot "
            "rewrite confirmed Image Knowledge. Preview or export the "
            "projection instead."
        )

    @staticmethod
    def restore(change: AutomationChange, knowledge=None) -> None:
        image=Path(change.image); record=ImageRecord(image)
        record.annotation=ImageAnnotation.from_dict(change.before,image_name=image.name)
        record.modified=True; record.save_sidecar(force=True); record.export_caption(knowledge=knowledge,force=True)


# Import compatibility for third-party scripts and old tests. Active application
# code uses the current names above; remove these aliases only at a documented
# breaking-version boundary.
LegacyRuleStore = AutomationRuleStore
LegacyAutomationEngine = AutomationEngine
