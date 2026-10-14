from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime,timezone
from hashlib import sha256
from pathlib import Path
from typing import Any,Iterable,Mapping
from uuid import uuid4

from .automation_repository import AutomationRuleRepository
from .automation_rules import AutomationEngine
from .operation_journal import OperationJournal
from .tag_collection import normalize_tag


MAX_REUSABLE_VALUE_AUTHORITY_TAGS = 20_000
MAX_REUSABLE_VALUE_AUTHORITY_VALUES = 20_000
MAX_REUSABLE_VALUE_AUTHORITY_RULES = 20_000
MAX_REUSABLE_VALUE_PARITY_CASES = 1_000

REVIEWED_SCOPED_FAMILIES = {
    "color": "modifier_color",
    "material": "modifier_material",
    "length": "length",
    "size": "size",
}

VARIABLE_IDENTITY_BINDINGS_KEY = "variable_identity_bindings"
VARIABLE_IDENTITY_BINDING_SCHEMA_VERSION = 1
VARIABLE_IDENTITY_MIGRATION_KIND = "automation_variable_identity_migration"
VARIABLE_IDENTITY_MIGRATION_UNDO_KIND = (
    "automation_variable_identity_migration_undo"
)
VARIABLE_AUTHORITY_DECISIONS_KEY = "variable_authority_decisions"
VARIABLE_AUTHORITY_DECISION_SCHEMA_VERSION = 1
VARIABLE_AUTHORITY_DECISION_KIND = "automation_variable_authority_decision"
VARIABLE_AUTHORITY_DECISION_UNDO_KIND = (
    "automation_variable_authority_decision_undo"
)
VARIABLE_AUTHORITY_OPERATION_KINDS = {
    VARIABLE_IDENTITY_MIGRATION_KIND,VARIABLE_AUTHORITY_DECISION_KIND,
}


@dataclass(frozen=True)
class ReusableValueResolution:
    value: str
    outcome: str
    stable_ids: tuple[str,...] = ()
    labels: tuple[str,...] = ()
    categories: tuple[str,...] = ()
    identity_basis: str = ""


@dataclass(frozen=True)
class ReusableValueFamilyAudit:
    name: str
    origin: str
    expected_family_id: str
    classification: str
    values: tuple[ReusableValueResolution,...]
    consumers: tuple[str,...]

    @property
    def resolved_count(self) -> int:
        return sum(item.outcome in {"typed_tag","stable_tag"} for item in self.values)

    @property
    def held_count(self) -> int:
        return len(self.values)-self.resolved_count

    @property
    def classification_label(self) -> str:
        return {
            "typed_tag_review":"Scoped Tag migration candidate",
            "stable_tag_review":"Stable Tag review required",
            "review_required":"Authority review required",
            "unused_review":"No current rule consumer",
        }.get(self.classification,"Authority review required")


@dataclass(frozen=True)
class ReusableValueAuthorityReport:
    families: tuple[ReusableValueFamilyAudit,...]
    processed_tags: int


@dataclass(frozen=True)
class ReusableValueParityCase:
    consumer: str
    legacy_value: str
    input_tags: tuple[str,...]
    before_projection: tuple[str,...]
    after_projection: tuple[str,...]

    @property
    def unchanged(self) -> bool:
        return self.before_projection==self.after_projection


@dataclass(frozen=True)
class ReusableValueMigrationPreview:
    family: ReusableValueFamilyAudit
    proposed_bindings: tuple[tuple[str,str,str,str],...]
    parity_cases: tuple[ReusableValueParityCase,...]
    held_reasons: tuple[str,...]
    plan_fingerprint: str
    bounded_cases: int = 0

    @property
    def ready_for_later_review(self) -> bool:
        return bool(
            self.family.classification in {
                "typed_tag_review","stable_tag_review",
            }
            and self.family.consumers
            and not self.held_reasons
            and not self.bounded_cases
            and self.parity_cases
            and all(item.unchanged for item in self.parity_cases)
        )


@dataclass(frozen=True)
class ReusableValueAuthorityDecisionPreview:
    family: ReusableValueFamilyAudit
    decision: str
    rationale: str
    reviewed_values: tuple[str,...]
    base_plan_fingerprint: str
    plan_fingerprint: str
    blockers: tuple[str,...] = ()

    @property
    def ready(self) -> bool:
        return bool(
            self.decision in {"retain_distinct","retire_unused"}
            and self.family.values
            and not self.blockers
            and self.reviewed_values==tuple(
                item.value for item in self.family.values
            )
        )


class ReusableValueAuthorityAuditService:
    """Classify legacy wildcard values without changing their runtime meaning."""

    _placeholder=re.compile(r"\{([^{}]+)\}")

    def __init__(
        self,tags: Any,categories: Any,*,
        max_tags: int=MAX_REUSABLE_VALUE_AUTHORITY_TAGS,
        max_values: int=MAX_REUSABLE_VALUE_AUTHORITY_VALUES,
        max_rules: int=MAX_REUSABLE_VALUE_AUTHORITY_RULES,
    ):
        self.tags=tags
        self.categories=categories
        self.max_tags=max(1,int(max_tags))
        self.max_values=max(1,int(max_values))
        self.max_rules=max(1,int(max_rules))

    def audit(
        self,
        rules: Mapping[str,Any],
        variables_with_origin: Iterable[tuple[str,str,Iterable[str]]],
    ) -> ReusableValueAuthorityReport:
        tag_count=len(getattr(self.tags,"tags",{}))
        if tag_count>self.max_tags:
            raise ValueError(
                f"Reusable-value authority review supports at most {self.max_tags:,} "
                f"current Tags; {tag_count:,} are loaded. Narrow or repair the "
                "System Knowledge source before retrying."
            )
        rows=tuple(
            (origin,name,tuple(values))
            for origin,name,values in variables_with_origin
        )
        value_count=sum(len(values) for _origin,_name,values in rows)
        if value_count>self.max_values:
            raise ValueError(
                f"Reusable-value authority review supports at most {self.max_values:,} "
                f"effective values; {value_count:,} are loaded."
            )
        rule_count=sum(
            len(rules.get(kind,()))
            for kind in ("merge_rules","wildcard_rules","combine_rules","ignore_rules")
        )
        if rule_count>self.max_rules:
            raise ValueError(
                f"Reusable-value authority review supports at most {self.max_rules:,} "
                f"effective rules; {rule_count:,} are loaded."
            )
        consumers=self._consumers(rules)
        families=[]
        for origin,raw_name,raw_values in rows:
            name=AutomationEngine._variable_key(raw_name)
            expected=REVIEWED_SCOPED_FAMILIES.get(name,"")
            values=tuple(self._resolve(str(value),expected) for value in raw_values)
            family_consumers=tuple(consumers.get(name,()))
            if not family_consumers:
                classification="unused_review"
            elif expected and values and all(item.outcome=="typed_tag" for item in values):
                classification="typed_tag_review"
            elif not expected and values and all(item.outcome=="stable_tag" for item in values):
                classification="stable_tag_review"
            else:
                classification="review_required"
            families.append(ReusableValueFamilyAudit(
                name=name,origin=str(origin),expected_family_id=expected,
                classification=classification,values=values,
                consumers=family_consumers,
            ))
        return ReusableValueAuthorityReport(
            tuple(sorted(families,key=lambda item:(item.name.casefold(),item.origin))),
            tag_count,
        )

    def preview_migration(
        self,
        rules: Mapping[str,Any],
        family: ReusableValueFamilyAudit,
        *,
        max_cases: int=MAX_REUSABLE_VALUE_PARITY_CASES,
        reviewed_stable_values: Iterable[str]=(),
    ) -> ReusableValueMigrationPreview:
        """Preview out-of-band stable bindings while retaining lexical values."""
        max_cases=max(1,int(max_cases))
        reviewed={str(value) for value in reviewed_stable_values}
        proposed=[]; held=[]
        unknown_reviewed=reviewed-{item.value for item in family.values}
        if unknown_reviewed:
            held.append(
                f"{len(unknown_reviewed):,} reviewed value(s) are not in the "
                "current legacy list. Reopen the meaning review."
            )
        for value in family.values:
            approved_stable=(
                family.classification=="stable_tag_review"
                and value.outcome=="stable_tag"
                and value.value in reviewed
            )
            if (
                value.outcome=="typed_tag" or approved_stable
            ) and len(value.stable_ids)==1:
                proposed.append((
                    value.value,value.stable_ids[0],
                    value.labels[0] if value.labels else value.value,
                    value.identity_basis,
                ))
            else:
                held.append(
                    f"{value.value}: {self._held_reason(value,family)}"
                )
        if family.classification not in {
            "typed_tag_review","stable_tag_review",
        }:
            held.append(
                "This family does not yet have complete reviewed scoped modifier authority."
            )
        elif family.classification=="stable_tag_review":
            unreviewed=[
                item.value for item in family.values
                if item.value not in reviewed
            ]
            if unreviewed:
                held.append(
                    f"{len(unreviewed):,} stable Tag mapping(s) still require "
                    "explicit per-value meaning review across every consumer."
                )
        if not family.consumers:
            held.append(
                "No active rule consumes this list; retention or retirement requires a separate review."
            )

        before_rules=deepcopy(dict(rules))
        after_rules=deepcopy(dict(rules))
        # Stable IDs are proposed as companion metadata. Old readers and the
        # current engine continue receiving the exact lexical strings.
        after_rules["variables"]={
            str(name):list(values)
            for name,values in dict(rules.get("variables",{})).items()
        }
        after_rules["variable_identity_bindings_preview"]={
            family.name:{
                legacy:{
                    "tag_id":stable_id,"display":display,
                    "identity_basis":basis,
                    "family_id":family.expected_family_id,
                }
                for legacy,stable_id,display,basis in proposed
            }
        }
        before_engine=AutomationEngine(before_rules)
        after_engine=AutomationEngine(after_rules)
        cases=[]; bounded=0
        records=self._consumer_records(rules,family.name)
        total=len(records)*len(family.values)
        for kind,index,record,label in records:
            for value in family.values:
                if len(cases)>=max_cases:
                    bounded=total-len(cases)
                    break
                inputs=self._representative_inputs(
                    kind,record,family.name,value.value,
                    rules.get("variables",{}),
                )
                before=tuple(before_engine.project_tags(inputs)[0])
                after=tuple(after_engine.project_tags(inputs)[0])
                cases.append(ReusableValueParityCase(
                    label,value.value,inputs,before,after,
                ))
            if bounded:break
        if any(not item.unchanged for item in cases):
            held.append(
                "At least one representative rule projection changes under the proposed binding."
            )
        if bounded:
            held.append(
                f"{bounded:,} parity case(s) exceeded the bounded preview and remain held."
            )
        fingerprint_payload={
            "family":{
                "name":family.name,"origin":family.origin,
                "expected_family_id":family.expected_family_id,
                "classification":family.classification,
                "consumers":list(family.consumers),
                "values":[{
                    "value":item.value,"outcome":item.outcome,
                    "stable_ids":list(item.stable_ids),
                    "labels":list(item.labels),
                    "categories":list(item.categories),
                    "identity_basis":item.identity_basis,
                } for item in family.values],
            },
            "consumer_records":[{
                "kind":kind,"index":index,"record":deepcopy(record),
                "label":label,
            } for kind,index,record,label in records],
            "proposed_bindings":list(proposed),
            "reviewed_stable_values":sorted(reviewed),
            "held_reasons":list(held),
            "bounded_cases":bounded,
            "parity_cases":[{
                "consumer":item.consumer,
                "legacy_value":item.legacy_value,
                "input_tags":list(item.input_tags),
                "before_projection":list(item.before_projection),
                "after_projection":list(item.after_projection),
            } for item in cases],
        }
        fingerprint=sha256(json.dumps(
            fingerprint_payload,sort_keys=True,separators=(",",":"),
            ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        return ReusableValueMigrationPreview(
            family,tuple(proposed),tuple(cases),tuple(dict.fromkeys(held)),
            fingerprint,bounded,
        )

    def _resolve(self,value: str,expected_family_id: str) -> ReusableValueResolution:
        owner_ids=tuple(getattr(self.tags,"identity_owner_ids")(value))
        removed_ids=tuple(getattr(self.tags,"removed_identity_ids",lambda _value:())(value))
        if len(owner_ids)>1:
            return ReusableValueResolution(value,"collision",owner_ids)
        if not owner_ids:
            return ReusableValueResolution(
                value,"tombstoned" if removed_ids else "missing",removed_ids,
            )
        record=getattr(self.tags,"tags",{}).get(owner_ids[0])
        if record is None:
            return ReusableValueResolution(value,"missing",owner_ids)
        category_id=str(getattr(record,"category","") or "")
        path_ids=tuple(
            getattr(self.categories,"path_ids",lambda _value:())(category_id)
        )
        typed=bool(expected_family_id and expected_family_id in path_ids)
        return ReusableValueResolution(
            value,"typed_tag" if typed else "stable_tag",owner_ids,
            (str(getattr(record,"display",value)),),(category_id,),
            "canonical" if normalize_tag(value)==normalize_tag(
                str(getattr(record,"display",value))
            ) else "declared_alias",
        )

    def _consumers(self,rules: Mapping[str,Any]) -> dict[str,list[str]]:
        result: dict[str,list[str]]={}
        for kind in (
            "merge_rules","wildcard_rules","combine_rules","ignore_rules",
            "tag_templates",
        ):
            records=rules.get(kind,())
            for index,record in enumerate(records,1):
                names=set()
                for value in self._strings(record):
                    names.update(
                        AutomationEngine._variable_key(match)
                        for match in self._placeholder.findall(value)
                    )
                for name in sorted(names):
                    label=f"{kind.replace('_',' ').title()} {index}"
                    if label not in result.setdefault(name,[]):result[name].append(label)
        return result

    def _consumer_records(self,rules: Mapping[str,Any],name: str):
        result=[]
        for kind in (
            "merge_rules","wildcard_rules","combine_rules","ignore_rules",
            "tag_templates",
        ):
            for index,record in enumerate(rules.get(kind,()),1):
                names={
                    AutomationEngine._variable_key(match)
                    for value in self._strings(record)
                    for match in self._placeholder.findall(value)
                }
                if name in names:
                    result.append((
                        kind,index,record,
                        f"{kind.replace('_',' ').title()} {index}",
                    ))
        return tuple(result)

    def _representative_inputs(
        self,kind: str,record: Mapping[str,Any],family_name: str,
        family_value: str,variables: Mapping[str,Iterable[str]],
    ) -> tuple[str,...]:
        bindings={
            AutomationEngine._variable_key(name):str(next(iter(values),""))
            for name,values in variables.items()
        }
        bindings[family_name]=family_value
        def render(value):
            return self._placeholder.sub(
                lambda match:bindings.get(
                    AutomationEngine._variable_key(match.group(1)),match.group(0),
                ),str(value),
            )
        if kind=="tag_templates":
            source=(record.get("template",""),)
        elif kind=="wildcard_rules":
            remove=record.get("remove",())
            remove=(remove,) if isinstance(remove,str) else tuple(remove)
            source=(record.get("pattern",""),*remove)
        elif kind in {"merge_rules","combine_rules"}:
            source=record.get("remove",())
            source=(source,) if isinstance(source,str) else tuple(source)
        else:
            source=(record.get("tag",""),)
        return tuple(render(value) for value in source if str(value).strip())

    @staticmethod
    def _held_reason(
        value: ReusableValueResolution,family: ReusableValueFamilyAudit,
    ) -> str:
        if value.outcome=="stable_tag" and family.expected_family_id:
            return "the stable Tag is outside the reviewed modifier-family scope"
        return {
            "stable_tag":"stable Tag semantics require per-value review",
            "collision":"multiple live Tag identities match",
            "tombstoned":"the matching Tag identity was removed",
            "missing":"no live Tag identity matches",
        }.get(value.outcome,"authority remains unresolved")

    @classmethod
    def _strings(cls,value: Any):
        if isinstance(value,str):
            yield value
        elif isinstance(value,Mapping):
            for item in value.values():yield from cls._strings(item)
        elif isinstance(value,(list,tuple)):
            for item in value:yield from cls._strings(item)


class ReusableValueBindingMigrationService:
    """Persist reviewed stable-ID companions without changing lexical rules."""

    def __init__(
        self,
        repository: AutomationRuleRepository,
        tags: Any,
        categories: Any,
        *,
        journal_path: str|Path|None=None,
    ) -> None:
        self.repository=repository
        self.tags=tags
        self.categories=categories
        self.audit_service=ReusableValueAuthorityAuditService(tags,categories)
        path=(
            Path(journal_path) if journal_path is not None else
            repository.user.path.with_name("operation_journal.jsonl")
        )
        self.journal=OperationJournal(path)

    def binding_record(self,name: str) -> dict[str,Any]|None:
        records=self.repository.user._payload_extras.get(
            VARIABLE_IDENTITY_BINDINGS_KEY,{},
        )
        if not isinstance(records,Mapping):return None
        record=records.get(AutomationEngine._variable_key(name))
        return deepcopy(record) if isinstance(record,dict) else None

    def authority_decision(self,name: str) -> dict[str,Any]|None:
        records=self.repository.user._payload_extras.get(
            VARIABLE_AUTHORITY_DECISIONS_KEY,{},
        )
        if not isinstance(records,Mapping):return None
        record=records.get(AutomationEngine._variable_key(name))
        return deepcopy(record) if isinstance(record,dict) else None

    def plan_authority_decision(
        self,
        rules: Mapping[str,Any],
        family: ReusableValueFamilyAudit,
        *,
        decision: str,
        rationale: str,
        reviewed_values: Iterable[str],
    ) -> ReusableValueAuthorityDecisionPreview:
        decision=str(decision or "").strip()
        rationale=" ".join(str(rationale or "").split())
        reviewed=tuple(str(value) for value in reviewed_values)
        expected=tuple(item.value for item in family.values)
        blockers=[]
        if decision not in {"retain_distinct","retire_unused"}:
            blockers.append("Choose whether to retain or retire this legacy list.")
        if len(rationale)<20:
            blockers.append(
                "Record a specific rationale of at least 20 characters."
            )
        if reviewed!=expected:
            blockers.append(
                "Review every current lexical value in its stored order."
            )
        if not expected:
            blockers.append("This legacy list is already empty.")
        if decision=="retire_unused" and family.consumers:
            blockers.append(
                "A legacy list with active consumers cannot be retired."
            )
        base=self.audit_service.preview_migration(rules,family)
        if base.bounded_cases:
            blockers.append(
                "The current consumer parity evidence exceeded its safe bound."
            )
        payload={
            "family":family.name,"origin":family.origin,
            "classification":family.classification,
            "values":list(expected),"consumers":list(family.consumers),
            "decision":decision,"rationale":rationale,
            "reviewed_values":list(reviewed),
            "base_plan_fingerprint":base.plan_fingerprint,
            "blockers":blockers,
        }
        fingerprint=sha256(json.dumps(
            payload,sort_keys=True,separators=(",",":"),ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        return ReusableValueAuthorityDecisionPreview(
            family,decision,rationale,reviewed,base.plan_fingerprint,
            fingerprint,tuple(blockers),
        )

    def _authority_decisions_for_write(self,name: str) -> dict[str,Any]:
        extras=self.repository.user._payload_extras
        if VARIABLE_AUTHORITY_DECISIONS_KEY not in extras:return {}
        records=extras[VARIABLE_AUTHORITY_DECISIONS_KEY]
        if not isinstance(records,dict):
            raise RuntimeError(
                "Saved legacy-authority decisions have an unsupported shape. "
                "They were left unchanged."
            )
        current=records.get(AutomationEngine._variable_key(name))
        if current is not None and (
            not isinstance(current,dict)
            or current.get("schema_version")
                !=VARIABLE_AUTHORITY_DECISION_SCHEMA_VERSION
        ):
            raise RuntimeError(
                "Saved legacy-authority decisions use an unsupported version. "
                "They were left unchanged."
            )
        return deepcopy(records)

    def _binding_records_for_write(self,name: str) -> dict[str,Any]:
        extras=self.repository.user._payload_extras
        if VARIABLE_IDENTITY_BINDINGS_KEY not in extras:
            return {}
        records=extras[VARIABLE_IDENTITY_BINDINGS_KEY]
        if not isinstance(records,dict):
            raise RuntimeError(
                "Saved stable-ID companion metadata has an unsupported shape. "
                "It was left unchanged; update CollectiGen or review the file "
                "before retrying."
            )
        current=records.get(AutomationEngine._variable_key(name))
        if current is not None and (
            not isinstance(current,dict)
            or current.get("schema_version")!=VARIABLE_IDENTITY_BINDING_SCHEMA_VERSION
        ):
            raise RuntimeError(
                "Saved stable-ID companion metadata uses an unsupported version. "
                "It was left unchanged; update CollectiGen before retrying."
            )
        return deepcopy(records)

    def revalidate(
        self,preview: ReusableValueMigrationPreview,
    ) -> ReusableValueMigrationPreview:
        """Reload every authority source and reproduce one exact reviewed plan."""
        self.repository.load()
        loader=getattr(self.tags,"load",None)
        if callable(loader):loader()
        loader=getattr(self.categories,"load",None)
        if callable(loader):loader()
        if getattr(self.repository.defaults,"_source_error",""):
            raise RuntimeError(self.repository.defaults._source_error)
        if getattr(self.repository.user,"_source_error",""):
            raise RuntimeError(self.repository.user._source_error)
        rules=self.repository.combined()
        report=self.audit_service.audit(
            rules,self.repository.variables_with_origin(),
        )
        family=next((
            item for item in report.families
            if item.name==preview.family.name
            and item.origin==preview.family.origin
        ),None)
        if family is None:
            raise RuntimeError(
                "Reusable values changed after this review opened. Reopen the "
                "compatibility review and inspect the current list."
            )
        reviewed=(
            tuple(item[0] for item in preview.proposed_bindings)
            if family.classification=="stable_tag_review" else ()
        )
        current=self.audit_service.preview_migration(
            rules,family,reviewed_stable_values=reviewed,
        )
        if current.plan_fingerprint!=preview.plan_fingerprint:
            raise RuntimeError(
                "Rules, reusable values, or stable Tag authority changed after "
                "this review opened. Nothing was saved; reopen the compatibility "
                "review and inspect the current plan."
            )
        if not current.ready_for_later_review:
            raise RuntimeError(
                "This compatibility plan is no longer fully resolved and cannot "
                "be saved. Review its held reasons against current authority."
            )
        return current

    def apply(self,preview: ReusableValueMigrationPreview) -> str:
        current=self.revalidate(preview)
        existing=self.binding_record(current.family.name)
        if self._record_matches_preview(existing,current):
            return str(existing.get("operation_id") or "")

        authority_bytes=self._authority_bytes()
        self.repository._assert_defaults_current()
        records=self._binding_records_for_write(current.family.name)
        before_bytes=(
            self.repository.user.path.read_bytes()
            if self.repository.user.path.exists() else None
        )
        before_disk=(
            json.loads(before_bytes.decode("utf-8"))
            if before_bytes is not None else {}
        )
        operation_id=self.journal.begin(
            VARIABLE_IDENTITY_MIGRATION_KIND,
            expected=1,
            reason=(
                "Save reviewed stable-ID companions while retaining legacy "
                "Automation strings"
            ),
            metadata={
                "family":current.family.name,
                "plan_fingerprint":current.plan_fingerprint,
                "bindings":len(current.proposed_bindings),
            },
        )
        saved=False
        try:
            record=self._record_for_preview(current,operation_id,existing)
            records[current.family.name]=record
            self.repository.user._payload_extras[
                VARIABLE_IDENTITY_BINDINGS_KEY
            ]=records
            after_payload=deepcopy(self.repository.user._payload())
            self.journal.append_change(
                operation_id,
                item=str(self.repository.user.path.resolve()),
                before=before_disk,
                after=after_payload,
                reason="Save reviewed reusable-value stable-ID companions",
                metadata={"existed_before":before_bytes is not None},
            )
            self._assert_authority_current(authority_bytes)
            self.repository.user.save_verified(
                backup_root=(
                    self.repository.user.path.parent/"backups"/
                    "variable-identity-migration"
                )
            )
            saved=True
            self.repository.load()
            saved_record=self.binding_record(current.family.name)
            if saved_record!=record:
                raise RuntimeError(
                    "Saved stable-ID companions failed read-back verification."
                )
            legacy_values=tuple(
                str(item.value) for item in current.family.values
            )
            effective=tuple(
                self.repository.combined().get("variables",{}).get(
                    current.family.name,(),
                )
            )
            if effective!=legacy_values:
                raise RuntimeError(
                    "Legacy wildcard values changed during stable-ID persistence."
                )
            report=self.audit_service.audit(
                self.repository.combined(),
                self.repository.variables_with_origin(),
            )
            family=next((
                item for item in report.families
                if item.name==current.family.name
                and item.origin==current.family.origin
            ),None)
            verified=(
                self.audit_service.preview_migration(
                    self.repository.combined(),family,
                    reviewed_stable_values=(
                        tuple(item[0] for item in current.proposed_bindings)
                        if family.classification=="stable_tag_review" else ()
                    ),
                ) if family is not None else None
            )
            if (
                verified is None
                or verified.plan_fingerprint!=current.plan_fingerprint
                or not verified.ready_for_later_review
            ):
                raise RuntimeError(
                    "Stable-ID companion parity could not be reproduced after saving."
                )
        except BaseException as exc:
            if saved:
                self._restore_bytes(self.repository.user.path,before_bytes)
            self.repository.load()
            self.journal.finish(operation_id,"failed",message=str(exc))
            raise
        self.journal.finish(operation_id,"complete")
        self.journal.prune()
        return operation_id

    def apply_authority_decision(
        self,preview: ReusableValueAuthorityDecisionPreview,
    ) -> str:
        self.repository.load()
        loader=getattr(self.tags,"load",None)
        if callable(loader):loader()
        loader=getattr(self.categories,"load",None)
        if callable(loader):loader()
        if getattr(self.repository.defaults,"_source_error",""):
            raise RuntimeError(self.repository.defaults._source_error)
        if getattr(self.repository.user,"_source_error",""):
            raise RuntimeError(self.repository.user._source_error)
        existing=self.authority_decision(preview.family.name)
        effective=tuple(
            self.repository.combined().get("variables",{}).get(
                preview.family.name,(),
            )
        )
        if self._decision_record_matches(existing,preview) and (
            preview.decision=="retain_distinct" or not effective
        ):
            return str(existing.get("operation_id") or "")
        rules=self.repository.combined()
        report=self.audit_service.audit(
            rules,self.repository.variables_with_origin(),
        )
        family=next((
            item for item in report.families
            if item.name==preview.family.name
            and item.origin==preview.family.origin
        ),None)
        if family is None:
            raise RuntimeError(
                "Reusable values changed after this authority decision opened. "
                "Reopen it and review the current list."
            )
        current=self.plan_authority_decision(
            rules,family,decision=preview.decision,
            rationale=preview.rationale,
            reviewed_values=preview.reviewed_values,
        )
        if current.plan_fingerprint!=preview.plan_fingerprint:
            raise RuntimeError(
                "Rules, values, consumers, or Tag authority changed after this "
                "decision opened. Nothing was saved; review the current plan."
            )
        if not current.ready:
            raise RuntimeError("This legacy-authority decision is not ready to save.")

        authority_bytes=self._authority_bytes()
        self.repository._assert_defaults_current()
        decisions=self._authority_decisions_for_write(current.family.name)
        bindings=self._binding_records_for_write(current.family.name)
        before_bytes=(
            self.repository.user.path.read_bytes()
            if self.repository.user.path.exists() else None
        )
        before_disk=(
            json.loads(before_bytes.decode("utf-8"))
            if before_bytes is not None else {}
        )
        operation_id=self.journal.begin(
            VARIABLE_AUTHORITY_DECISION_KIND,expected=1,
            reason="Record a reviewed legacy reusable-value authority decision",
            metadata={
                "family":current.family.name,"decision":current.decision,
                "plan_fingerprint":current.plan_fingerprint,
                "values":len(current.reviewed_values),
            },
        )
        saved=False
        try:
            record=self._decision_record_for_preview(
                current,operation_id,existing,
            )
            decisions[current.family.name]=record
            self.repository.user._payload_extras[
                VARIABLE_AUTHORITY_DECISIONS_KEY
            ]=decisions
            bindings.pop(current.family.name,None)
            if (
                VARIABLE_IDENTITY_BINDINGS_KEY
                in self.repository.user._payload_extras or bindings
            ):
                self.repository.user._payload_extras[
                    VARIABLE_IDENTITY_BINDINGS_KEY
                ]=bindings
            if current.decision=="retire_unused":
                self.repository.user.data["variable_overrides"][
                    current.family.name
                ]=[]
                self.repository.user.data["variables"].pop(
                    current.family.name,None,
                )
            after_payload=deepcopy(self.repository.user._payload())
            self.journal.append_change(
                operation_id,item=str(self.repository.user.path.resolve()),
                before=before_disk,after=after_payload,
                reason="Save reviewed legacy reusable-value authority",
                metadata={"existed_before":before_bytes is not None},
            )
            self._assert_authority_current(authority_bytes)
            self.repository.user.save_verified(
                backup_root=(
                    self.repository.user.path.parent/"backups"/
                    "variable-authority-decision"
                )
            )
            saved=True
            self.repository.load()
            if self.authority_decision(current.family.name)!=record:
                raise RuntimeError(
                    "Saved legacy-authority decision failed read-back verification."
                )
            if self.binding_record(current.family.name) is not None:
                raise RuntimeError(
                    "A conflicting stable-ID binding remained after the authority decision."
                )
            actual=tuple(
                self.repository.combined().get("variables",{}).get(
                    current.family.name,(),
                )
            )
            expected=(
                () if current.decision=="retire_unused"
                else current.reviewed_values
            )
            if actual!=expected:
                raise RuntimeError(
                    "Legacy reusable values changed outside the reviewed decision."
                )
        except BaseException as exc:
            if saved:self._restore_bytes(self.repository.user.path,before_bytes)
            self.repository.load()
            self.journal.finish(operation_id,"failed",message=str(exc))
            raise
        self.journal.finish(operation_id,"complete")
        self.journal.prune()
        return operation_id

    def recover_interrupted(self) -> tuple[str,...]:
        """Restore exact pre-migration user rules after an interrupted process."""
        path=self.repository.user.path.resolve()
        recovered=[]
        for operation in self.journal.operations():
            if not (
                operation.kind in VARIABLE_AUTHORITY_OPERATION_KINDS
                and operation.status=="applying"
            ):
                continue
            if len(operation.changes)>1:
                raise RuntimeError(
                    "An interrupted stable-ID migration has unexpected recovery state."
                )
            restores=[]
            for change in operation.changes:
                if Path(change["item"]).resolve()!=path:
                    raise RuntimeError(
                        "An interrupted stable-ID migration contains an unexpected file."
                    )
                try:
                    before=self.journal.load_snapshot(change["before"])
                    after=self.journal.load_snapshot(change["after"])
                except (OSError,json.JSONDecodeError,KeyError,TypeError) as exc:
                    raise RuntimeError(
                        "An interrupted stable-ID migration is missing a valid recovery snapshot."
                    ) from exc
                if not isinstance(before,dict) or not isinstance(after,dict):
                    raise RuntimeError(
                        "An interrupted stable-ID migration has an invalid recovery snapshot."
                    )
                existed=bool(change.get("metadata",{}).get("existed_before",True))
                current=self._disk_payload(path,existed_before=existed)
                if current==before or (current is None and not existed):
                    continue
                if current!=after:
                    raise RuntimeError(
                        "Rules changed after an interrupted stable-ID migration. "
                        "Recovery refused to overwrite them."
                    )
                restores.append((before,after,existed))
            restored=False
            try:
                for before,_after,existed in restores:
                    if existed:self._write_json(path,before)
                    elif path.exists():path.unlink()
                    restored=True
                for before,_after,existed in restores:
                    actual=self._disk_payload(path,existed_before=existed)
                    if actual!=before and not (actual is None and not existed):
                        raise RuntimeError(
                            "Interrupted stable-ID migration recovery failed read-back verification."
                        )
            except BaseException:
                if restored:
                    for _before,after,_existed in restores:
                        self._write_json(path,after)
                raise
            self.journal.finish(
                operation.id,"failed",
                message=(
                    "Recovered exact pre-migration rules after an interrupted process."
                ),
            )
            recovered.append(operation.id)
        if recovered:
            self.repository.load(); self.journal.prune()
        return tuple(recovered)

    def undoable_operation(self):
        operations=(
            operation for operation in self.journal.operations()
            if operation.kind in VARIABLE_AUTHORITY_OPERATION_KINDS
            and operation.status=="complete"
        )
        return next(reversed(tuple(operations)),None)

    def undo_last(self) -> str:
        operation=self.undoable_operation()
        if operation is None:
            raise ValueError(
                "There is no completed reusable-value binding migration to restore."
            )
        if len(operation.changes)!=1:
            raise RuntimeError("The recorded stable-ID migration is incomplete.")
        change=operation.changes[0]
        path=Path(change["item"]).resolve()
        if path!=self.repository.user.path.resolve():
            raise RuntimeError("The recorded stable-ID migration contains an unexpected file.")
        before=self.journal.load_snapshot(change["before"])
        after=self.journal.load_snapshot(change["after"])
        if not isinstance(before,dict) or not isinstance(after,dict):
            raise RuntimeError("The recorded stable-ID migration has invalid snapshots.")
        existed=bool(change.get("metadata",{}).get("existed_before",True))
        current=self._disk_payload(path,existed_before=True)
        if current!=after:
            raise RuntimeError(
                "Rules changed after this stable-ID migration. Review the current "
                "rules before restoring it."
            )
        undo_kind=(
            VARIABLE_AUTHORITY_DECISION_UNDO_KIND
            if operation.kind==VARIABLE_AUTHORITY_DECISION_KIND else
            VARIABLE_IDENTITY_MIGRATION_UNDO_KIND
        )
        undo_id=self.journal.begin(
            undo_kind,
            expected=1,
            reason="Restore reusable-value authority from before the last decision",
            metadata={"restores":operation.id},
        )
        self.journal.append_change(
            undo_id,item=str(path),before=current,after=before,
            reason="Restore exact pre-migration Automation payload",
            metadata={"existed_after":True,"existed_before":existed},
        )
        try:
            if existed:self._write_json(path,before)
            elif path.exists():path.unlink()
            actual=self._disk_payload(path,existed_before=existed)
            if actual!=before and not (actual is None and not existed):
                raise RuntimeError(
                    "Restored reusable-value bindings failed read-back verification."
                )
        except BaseException as exc:
            self._write_json(path,current)
            self.journal.finish(undo_id,"failed",message=str(exc))
            raise
        self.repository.load()
        self.journal.finish(undo_id,"complete")
        self.journal.finish(operation.id,"undone")
        self.journal.prune()
        return operation.id

    @staticmethod
    def _record_for_preview(
        preview: ReusableValueMigrationPreview,operation_id: str,
        existing: Mapping[str,Any]|None=None,
    ) -> dict[str,Any]:
        record=deepcopy(dict(existing)) if isinstance(existing,Mapping) else {}
        prior_bindings={
            str(item.get("legacy_value") or ""):item
            for item in record.get("bindings",())
            if isinstance(item,Mapping)
        }
        bindings=[]
        for legacy,stable_id,display,basis in preview.proposed_bindings:
            binding=deepcopy(dict(prior_bindings.get(legacy,{})))
            binding.update({
                "legacy_value":legacy,
                "tag_id":stable_id,
                "display":display,
                "identity_basis":basis,
            })
            bindings.append(binding)
        record.update({
            "schema_version":VARIABLE_IDENTITY_BINDING_SCHEMA_VERSION,
            "family_id":preview.family.expected_family_id,
            "authority_kind":(
                "typed_modifier" if preview.family.expected_family_id else
                "stable_tag_identity"
            ),
            "legacy_values":[item.value for item in preview.family.values],
            "bindings":bindings,
            "reviewed_consumers":list(preview.family.consumers),
            "plan_fingerprint":preview.plan_fingerprint,
            "operation_id":operation_id,
            "reviewed_at":datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_origin":preview.family.origin,
        })
        return record

    @staticmethod
    def _record_matches_preview(
        record: Mapping[str,Any]|None,preview: ReusableValueMigrationPreview,
    ) -> bool:
        if not isinstance(record,Mapping):return False
        expected=ReusableValueBindingMigrationService._record_for_preview(
            preview,str(record.get("operation_id") or ""),record,
        )
        expected["reviewed_at"]=record.get("reviewed_at")
        return dict(record)==expected

    @staticmethod
    def _decision_record_for_preview(
        preview: ReusableValueAuthorityDecisionPreview,operation_id: str,
        existing: Mapping[str,Any]|None=None,
    ) -> dict[str,Any]:
        record=deepcopy(dict(existing)) if isinstance(existing,Mapping) else {}
        record.update({
            "schema_version":VARIABLE_AUTHORITY_DECISION_SCHEMA_VERSION,
            "family":preview.family.name,
            "source_origin":preview.family.origin,
            "decision":preview.decision,
            "authority_kind":(
                "legacy_lexical_wildcard"
                if preview.decision=="retain_distinct" else
                "retired_legacy_wildcard"
            ),
            "retained_type_id":(
                "legacy_lexical_wildcard"
                if preview.decision=="retain_distinct" else ""
            ),
            "rationale":preview.rationale,
            "legacy_values":list(preview.reviewed_values),
            "reviewed_consumers":list(preview.family.consumers),
            "base_plan_fingerprint":preview.base_plan_fingerprint,
            "plan_fingerprint":preview.plan_fingerprint,
            "operation_id":operation_id,
            "reviewed_at":datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        return record

    @staticmethod
    def _decision_record_matches(
        record: Mapping[str,Any]|None,
        preview: ReusableValueAuthorityDecisionPreview,
    ) -> bool:
        if not isinstance(record,Mapping):return False
        expected=ReusableValueBindingMigrationService._decision_record_for_preview(
            preview,str(record.get("operation_id") or ""),record,
        )
        expected["reviewed_at"]=record.get("reviewed_at")
        return dict(record)==expected

    def _authority_bytes(self) -> dict[Path,bytes|None]:
        paths=[]
        for candidate in (
            getattr(self.tags,"path",None),
            getattr(self.categories,"default_path",None),
            getattr(self.categories,"user_path",None),
        ):
            if candidate is not None:
                path=Path(candidate).resolve()
                if path not in paths:paths.append(path)
        return {
            path:(path.read_bytes() if path.exists() else None)
            for path in paths
        }

    @staticmethod
    def _assert_authority_current(expected: Mapping[Path,bytes|None]) -> None:
        for path,content in expected.items():
            try:current=path.read_bytes() if path.exists() else None
            except OSError as exc:
                raise RuntimeError(
                    "Stable Tag or category authority became unavailable. Nothing "
                    "was saved; restore access and reopen the compatibility review."
                ) from exc
            if current!=content:
                raise RuntimeError(
                    "Stable Tag or category authority changed during this review. "
                    "Nothing was saved; reopen the compatibility review."
                )

    @staticmethod
    def _disk_payload(path: Path,*,existed_before: bool) -> dict|None:
        if not path.exists():
            if existed_before:
                raise RuntimeError(
                    "A rules file needed for stable-ID recovery is missing."
                )
            return None
        try:return json.loads(path.read_text(encoding="utf-8"))
        except (OSError,json.JSONDecodeError) as exc:
            raise RuntimeError(
                "A rules file needed for stable-ID recovery is unavailable or invalid."
            ) from exc

    @staticmethod
    def _write_json(path: Path,payload: dict) -> None:
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix(path.suffix+f".{uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(payload,indent=2,ensure_ascii=False)+"\n",encoding="utf-8",
        )
        temporary.replace(path)

    @staticmethod
    def _restore_bytes(path: Path,content: bytes|None) -> None:
        if content is None:
            if path.exists():path.unlink()
            return
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix(path.suffix+f".{uuid4().hex}.restore.tmp")
        temporary.write_bytes(content); temporary.replace(path)
