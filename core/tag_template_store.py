from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass,field,replace
from datetime import datetime,timezone
from hashlib import sha256
from pathlib import Path
from typing import Any,Mapping

from .tag_template_expansion import (
    MAX_TAG_TEMPLATE_EXPANSION_VALUES,TagTemplateExpansionReport,
)


TAG_TEMPLATE_FORMAT_VERSION = 2
DEFAULT_TAG_TEMPLATE_MAX_PER_RUN = 1_000


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ordered_ids(values: Any) -> tuple[str,...]:
    result=[]; seen=set()
    for value in values if isinstance(values,(list,tuple)) else ():
        stable_id=str(value or "").strip()
        if stable_id and stable_id not in seen:
            seen.add(stable_id); result.append(stable_id)
    return tuple(result)


def _fingerprint(payload: Mapping[str,Any]) -> str:
    return sha256(json.dumps(
        payload,sort_keys=True,separators=(",",":"),ensure_ascii=False,
    ).encode("utf-8")).hexdigest()


def _merged_payload(extras: Any,values: Mapping[str,Any]) -> dict[str,Any]:
    payload=deepcopy(dict(extras)) if isinstance(extras,Mapping) else {}
    payload.update(values)
    return payload


def tag_template_id(candidate_id: str) -> str:
    prefix="tag_template_candidate."
    value=str(candidate_id or "").strip()
    if not value.startswith(prefix) or not value[len(prefix):]:
        raise ValueError("The selected reusable Tag family has no stable template identity.")
    return "tag_template."+value[len(prefix):]


def tag_template_selection_fingerprint(
    expansion_fingerprint: str,selected_result_ids: Any,
) -> str:
    return _fingerprint({
        "expansion_fingerprint":str(expansion_fingerprint or ""),
        "result_ids":list(_ordered_ids(selected_result_ids)),
    })


@dataclass(frozen=True)
class ReviewedTagTemplate:
    """One versioned, reviewed template stored in Rules & Automation."""

    id: str
    version: int
    template: str
    anchor_ids: tuple[str,...]
    anchor_labels: tuple[str,...]
    modifier_family_id: str
    modifier_family: str
    placeholder: str
    token_position: str
    included_value_ids: tuple[str,...]
    excluded_value_ids: tuple[str,...] = ()
    invalid_value_ids: tuple[str,...] = ()
    held_value_ids: tuple[str,...] = ()
    selected_result_ids: tuple[str,...] = ()
    selection_reviewed: bool = False
    selection_fingerprint: str = ""
    policy_fingerprint: str = ""
    candidate_fingerprint: str = ""
    expansion_fingerprint: str = ""
    reviewed_at: str = ""
    source: str = "user_review"
    generation_enabled: bool = False
    fact_expansion_enabled: bool = False
    max_per_run: int = DEFAULT_TAG_TEMPLATE_MAX_PER_RUN
    format_version: int = TAG_TEMPLATE_FORMAT_VERSION
    extras: Mapping[str,Any] = field(default_factory=dict,repr=False,compare=False)
    nested_extras: Mapping[str,Any] = field(
        default_factory=dict,repr=False,compare=False,
    )
    raw_payload: Mapping[str,Any] | None = field(
        default=None,repr=False,compare=False,
    )
    anchor_roles: tuple[str,...] = ()
    structured_slots: tuple[Mapping[str,Any],...] = field(
        default=(),repr=False,compare=False,
    )
    structured_tokens: tuple[Mapping[str,Any],...] = field(
        default=(),repr=False,compare=False,
    )
    constraints: Mapping[str,Any] = field(
        default_factory=dict,repr=False,compare=False,
    )
    destination_policy: Mapping[str,Any] = field(
        default_factory=dict,repr=False,compare=False,
    )
    projection_rule: Mapping[str,Any] = field(
        default_factory=dict,repr=False,compare=False,
    )
    policy_review: Mapping[str,Any] = field(
        default_factory=dict,repr=False,compare=False,
    )

    @property
    def supported(self) -> bool:
        return self.format_version<=TAG_TEMPLATE_FORMAT_VERSION

    @property
    def semantic_fingerprint(self) -> str:
        payload=self.to_dict()
        payload.pop("version",None); payload.pop("reviewed_at",None)
        return _fingerprint(payload)

    @property
    def slot_records(self) -> tuple[Mapping[str,Any],...]:
        if self.structured_slots:
            return tuple(deepcopy(dict(item)) for item in self.structured_slots)
        return ({
            "kind":"modifier_slot","id":"slot_1",
            "family_id":self.modifier_family_id,
            "family_label":self.modifier_family,"placeholder":self.placeholder,
            "included_value_ids":list(self.included_value_ids),
            "excluded_value_ids":list(self.excluded_value_ids),
            "invalid_value_ids":list(self.invalid_value_ids),
            "held_value_ids":list(self.held_value_ids),"required":False,
        },)

    @property
    def token_records(self) -> tuple[Mapping[str,Any],...]:
        if self.structured_tokens:
            return tuple(deepcopy(dict(item)) for item in self.structured_tokens)
        modifier={"kind":"modifier_slot","slot_id":"slot_1"}
        anchor={"kind":"anchor","tag_id":self.anchor_ids[0]}
        return (modifier,anchor) if self.token_position=="prefix" else (anchor,modifier)

    @property
    def modifier_family_ids(self) -> tuple[str,...]:
        return _ordered_ids(tuple(
            str(item.get("family_id") or "") for item in self.slot_records
        ))

    @property
    def policy_scope_fingerprint(self) -> str:
        return _fingerprint({
            "template_id":self.id,"candidate":self.candidate_fingerprint,
            "expansion":self.expansion_fingerprint,
            "selection":self.selection_fingerprint,
            "selected_result_ids":list(self.selected_result_ids),
            "slots":[dict(item) for item in self.slot_records],
            "tokens":[dict(item) for item in self.token_records],
            "constraints":dict(self.constraints),
            "destination_policy":dict(self.destination_policy),
            "max_per_run":self.max_per_run,
        })

    def to_dict(self) -> dict[str,Any]:
        if not self.supported and self.raw_payload is not None:
            return deepcopy(dict(self.raw_payload))
        anchor_extras=self.nested_extras.get("anchors",())
        anchors=[
            _merged_payload(
                anchor_extras[index] if index<len(anchor_extras) else {},
                {"kind":"tag","id":stable_id,"label":label,"role":(
                    self.anchor_roles[index] if index<len(self.anchor_roles) else
                    "base" if index==0 else "component"
                )},
            )
            for index,(stable_id,label) in enumerate(zip(self.anchor_ids,self.anchor_labels))
        ]
        if self.structured_slots:
            slots=[deepcopy(dict(item)) for item in self.structured_slots]
        else:
            slot_values={
                "kind":"modifier_slot","id":"slot_1",
                "family_id":self.modifier_family_id,
                "family_label":self.modifier_family,"placeholder":self.placeholder,
                "included_value_ids":list(self.included_value_ids),
                "excluded_value_ids":list(self.excluded_value_ids),
                "invalid_value_ids":list(self.invalid_value_ids),
                "held_value_ids":list(self.held_value_ids),"required":False,
            }
            slot_extras=self.nested_extras.get("slots",())
            slots=[_merged_payload(
                slot_extras[0] if slot_extras else {},slot_values,
            )]
        if self.structured_tokens:
            tokens=[deepcopy(dict(item)) for item in self.structured_tokens]
        else:
            slot_values=deepcopy(slots[0]); slot_values.pop("id",None)
            slot_values.pop("required",None)
            token_extras=self.nested_extras.get("tokens",())
            modifier_index=0 if self.token_position=="prefix" else 1
            anchor_index=1-modifier_index
            tokens=[{},{}]
            tokens[modifier_index]=_merged_payload(
                token_extras[modifier_index] if modifier_index<len(token_extras) else {},
                slot_values,
            )
            tokens[anchor_index]=_merged_payload(
                token_extras[anchor_index] if anchor_index<len(token_extras) else {},
                {"kind":"anchor","tag_id":self.anchor_ids[0]},
            )
        payload=deepcopy(dict(self.extras)); payload.update({
            "id":self.id,"format_version":self.format_version,
            "version":self.version,"template":self.template,
            "anchors":anchors,"slots":slots,"tokens":tokens,
            "token_position":self.token_position,
            "selection":_merged_payload(
                self.nested_extras.get("selection",{}),{
                    "reviewed":self.selection_reviewed,
                    "result_ids":list(self.selected_result_ids),
                    "fingerprint":self.selection_fingerprint,
                },
            ),
            "policy_fingerprint":self.policy_fingerprint,
            "candidate_fingerprint":self.candidate_fingerprint,
            "expansion_fingerprint":self.expansion_fingerprint,
            "reviewed_at":self.reviewed_at,"source":self.source,
            "generation_enabled":self.generation_enabled,
            "fact_expansion_enabled":self.fact_expansion_enabled,
            "max_per_run":self.max_per_run,
            # A builder draft may hold exclusions as tuples. Publish the same
            # JSON-shaped value used by read-back verification so an empty
            # reviewed exclusion set does not fail and roll back approval.
            "constraints":json.loads(json.dumps(dict(self.constraints),ensure_ascii=False)),
            "destination_policy":deepcopy(dict(self.destination_policy)),
            "projection_rule":deepcopy(dict(self.projection_rule)),
            "policy_review":deepcopy(dict(self.policy_review)),
        })
        return payload

    @classmethod
    def from_dict(cls,payload: Any) -> "ReviewedTagTemplate":
        if not isinstance(payload,dict):
            raise ValueError("A reviewed Tag template record is not an object.")
        format_version=max(1,int(payload.get("format_version") or 1))
        known={
            "id","format_version","version","template","anchors","slots","tokens",
            "token_position","selection","policy_fingerprint","candidate_fingerprint",
            "expansion_fingerprint","reviewed_at","source","generation_enabled",
            "fact_expansion_enabled","max_per_run","constraints",
            "destination_policy","projection_rule","policy_review",
        }
        anchors=payload.get("anchors") if isinstance(payload.get("anchors"),list) else []
        anchor_ids=tuple(
            str(item.get("id") or "").strip() for item in anchors
            if isinstance(item,dict) and str(item.get("id") or "").strip()
        )
        anchor_labels=tuple(
            str(item.get("label") or item.get("id") or "").strip() for item in anchors
            if isinstance(item,dict) and str(item.get("id") or "").strip()
        )
        slots=payload.get("slots") if isinstance(payload.get("slots"),list) else []
        slot=next((item for item in slots if isinstance(item,dict)),{})
        selection=(
            payload.get("selection")
            if isinstance(payload.get("selection"),dict) else {}
        )
        anchor_known={"kind","id","label","role"}
        slot_known={
            "kind","id","family_id","family_label","placeholder","required",
            "included_value_ids","excluded_value_ids","invalid_value_ids",
            "held_value_ids",
        }
        token_known=slot_known|{"tag_id","slot_id","text","role"}
        selection_known={"reviewed","result_ids","fingerprint"}
        nested_extras={
            "anchors":tuple(
                {key:deepcopy(value) for key,value in item.items() if key not in anchor_known}
                for item in anchors if isinstance(item,dict)
            ),
            "slots":tuple(
                {key:deepcopy(value) for key,value in item.items() if key not in slot_known}
                for item in slots if isinstance(item,dict)
            ),
            "tokens":tuple(
                {key:deepcopy(value) for key,value in item.items() if key not in token_known}
                for item in payload.get("tokens",[]) if isinstance(item,dict)
            ) if isinstance(payload.get("tokens"),list) else (),
            "selection":{
                key:deepcopy(value) for key,value in selection.items()
                if key not in selection_known
            },
        }
        record=cls(
            id=str(payload.get("id") or "").strip(),
            format_version=format_version,
            version=max(1,int(payload.get("version") or 1)),
            template=str(payload.get("template") or "").strip(),
            anchor_ids=anchor_ids,anchor_labels=anchor_labels,
            modifier_family_id=str(slot.get("family_id") or "").strip(),
            modifier_family=str(slot.get("family_label") or slot.get("family_id") or "").strip(),
            placeholder=str(slot.get("placeholder") or "").strip(),
            token_position=str(payload.get("token_position") or "").strip(),
            included_value_ids=_ordered_ids(slot.get("included_value_ids")),
            excluded_value_ids=_ordered_ids(slot.get("excluded_value_ids")),
            invalid_value_ids=_ordered_ids(slot.get("invalid_value_ids")),
            held_value_ids=_ordered_ids(slot.get("held_value_ids")),
            selected_result_ids=_ordered_ids(selection.get("result_ids")),
            selection_reviewed=bool(selection.get("reviewed",False)),
            selection_fingerprint=str(selection.get("fingerprint") or ""),
            policy_fingerprint=str(payload.get("policy_fingerprint") or ""),
            candidate_fingerprint=str(payload.get("candidate_fingerprint") or ""),
            expansion_fingerprint=str(payload.get("expansion_fingerprint") or ""),
            reviewed_at=str(payload.get("reviewed_at") or ""),
            source=str(payload.get("source") or "user_review"),
            generation_enabled=bool(payload.get("generation_enabled",False)),
            fact_expansion_enabled=bool(payload.get("fact_expansion_enabled",False)),
            max_per_run=max(1,int(payload.get("max_per_run") or DEFAULT_TAG_TEMPLATE_MAX_PER_RUN)),
            extras={key:deepcopy(value) for key,value in payload.items() if key not in known},
            nested_extras=nested_extras,
            raw_payload=(
                deepcopy(payload)
                if format_version>TAG_TEMPLATE_FORMAT_VERSION else None
            ),
            anchor_roles=tuple(
                str(item.get("role") or ("base" if index==0 else "component"))
                for index,item in enumerate(anchors) if isinstance(item,dict)
            ),
            structured_slots=(
                tuple(deepcopy(item) for item in slots if isinstance(item,dict))
                if format_version>=2 else ()
            ),
            structured_tokens=(
                tuple(deepcopy(item) for item in payload.get("tokens",()) if isinstance(item,dict))
                if format_version>=2 and isinstance(payload.get("tokens"),list) else ()
            ),
            constraints=deepcopy(payload.get("constraints",{}))
            if isinstance(payload.get("constraints"),Mapping) else {},
            destination_policy=deepcopy(payload.get("destination_policy",{}))
            if isinstance(payload.get("destination_policy"),Mapping) else {},
            projection_rule=deepcopy(payload.get("projection_rule",{}))
            if isinstance(payload.get("projection_rule"),Mapping) else {},
            policy_review=deepcopy(payload.get("policy_review",{}))
            if isinstance(payload.get("policy_review"),Mapping) else {},
        )
        if format_version>TAG_TEMPLATE_FORMAT_VERSION:
            if not record.id.startswith("tag_template.") or not record.template:
                raise ValueError(
                    "A newer reviewed Tag template is missing its stable ID or visible template."
                )
            return record
        record.validate()
        if format_version==1:
            if (
                len(anchors)!=1 or anchors[0].get("kind")!="tag"
                or str(anchors[0].get("id") or "").strip()!=record.anchor_ids[0]
                or len(slots)!=1 or slot.get("kind")!="modifier_slot"
            ):
                raise ValueError("A reviewed Tag template has no valid anchor and typed slot plan.")
            tokens=payload.get("tokens")
            if not isinstance(tokens,list) or len(tokens)!=2 or not all(
                isinstance(token,dict) for token in tokens
            ):
                raise ValueError("A reviewed Tag template has no valid ordered token plan.")
            modifier_index=0 if record.token_position=="prefix" else 1
            anchor_index=1-modifier_index
            modifier=tokens[modifier_index]; anchor=tokens[anchor_index]
            if (
                modifier.get("kind")!="modifier_slot"
                or str(modifier.get("family_id") or "").strip()!=record.modifier_family_id
                or str(modifier.get("placeholder") or "").strip()!=record.placeholder
                or _ordered_ids(modifier.get("included_value_ids"))!=record.included_value_ids
                or _ordered_ids(modifier.get("excluded_value_ids"))!=record.excluded_value_ids
                or _ordered_ids(modifier.get("invalid_value_ids"))!=record.invalid_value_ids
                or _ordered_ids(modifier.get("held_value_ids"))!=record.held_value_ids
                or anchor.get("kind")!="anchor"
                or str(anchor.get("tag_id") or "").strip()!=record.anchor_ids[0]
            ):
                raise ValueError("A reviewed Tag template token plan contradicts its anchor or modifier slot.")
        return record

    def validate(self) -> None:
        if not self.id.startswith("tag_template."):
            raise ValueError("A reviewed Tag template has no supported stable ID.")
        if (
            not self.template or not self.anchor_ids
            or len(self.anchor_ids)!=len(self.anchor_labels)
        ):
            raise ValueError("A reviewed template needs visible stable Tag anchors.")
        roles=self.anchor_roles or tuple(
            "base" if index==0 else "component"
            for index in range(len(self.anchor_ids))
        )
        if len(roles)!=len(self.anchor_ids) or any(
            role not in {"base","component","implication"} for role in roles
        ):
            raise ValueError("Every literal anchor needs one reviewed semantic role.")
        if sum(role=="base" for role in roles)>1:
            raise ValueError("A composite template may have at most one primary Base SDXL Tag.")
        if not self.modifier_family_id or not self.placeholder:
            raise ValueError("A reviewed Tag template has no typed modifier slot.")
        if self.format_version==1 and self.token_position not in {"prefix","suffix"}:
            raise ValueError("A reviewed Tag template has invalid token order.")
        if not self.included_value_ids:
            raise ValueError("A reviewed Tag template must include at least one reviewed value.")
        if not self.policy_fingerprint or not self.candidate_fingerprint or not self.expansion_fingerprint:
            raise ValueError("A reviewed Tag template is missing its current authority fingerprint.")
        if self.max_per_run>MAX_TAG_TEMPLATE_EXPANSION_VALUES:
            raise ValueError("A reviewed Tag template exceeds the supported per-run expansion limit.")
        groups=(
            set(self.included_value_ids),set(self.excluded_value_ids),
            set(self.invalid_value_ids),
        )
        if groups[0]&groups[1] or groups[0]&groups[2] or groups[1]&groups[2]:
            raise ValueError("A reviewed value cannot be included, excluded, and invalid at the same time.")
        if not set(self.held_value_ids).issubset(set().union(*groups)):
            raise ValueError("A held reviewed value must belong to this template's reviewed value plan.")
        if self.selection_reviewed:
            if not self.selection_fingerprint:
                raise ValueError("A reviewed result selection is missing its expansion fingerprint.")
            if any(
                not stable_id.startswith((
                    "tag_template_expansion.","tag_template_result.",
                ))
                for stable_id in self.selected_result_ids
            ):
                raise ValueError("A reviewed result selection contains an invalid stable result ID.")
            if (
                self.supported and self.selection_fingerprint
                !=tag_template_selection_fingerprint(
                    self.expansion_fingerprint,self.selected_result_ids,
                )
            ):
                raise ValueError("A reviewed result selection fingerprint does not match its stored scope.")
        elif self.selected_result_ids or self.selection_fingerprint:
            raise ValueError("A stored result selection must record that it was explicitly reviewed.")
        if len(self.selected_result_ids)>self.max_per_run:
            raise ValueError("A reviewed result selection exceeds this template's per-run limit.")
        if self.format_version>=2:
            slots=self.slot_records; tokens=self.token_records
            slot_ids=[]
            for index,slot in enumerate(slots,1):
                if slot.get("kind")!="modifier_slot":
                    raise ValueError("A reviewed template contains an untyped modifier slot.")
                slot_id=str(slot.get("id") or f"slot_{index}").strip()
                family_id=str(slot.get("family_id") or "").strip()
                placeholder=str(slot.get("placeholder") or "").strip().strip("{}")
                if not slot_id or not family_id or not placeholder:
                    raise ValueError("Every reviewed modifier slot needs stable slot, family, and placeholder identities.")
                if slot_id in slot_ids:
                    raise ValueError("A reviewed template repeats a slot identity instead of referencing it.")
                slot_ids.append(slot_id)
            if not tokens:
                raise ValueError("A reviewed template has no ordered chip plan.")
            anchor_tokens=set(); slot_tokens=set()
            for token in tokens:
                kind=str(token.get("kind") or "")
                if kind=="anchor":
                    stable_id=str(token.get("tag_id") or "")
                    if stable_id not in self.anchor_ids:
                        raise ValueError("A template chip references an unavailable stable anchor.")
                    anchor_tokens.add(stable_id)
                elif kind=="modifier_slot":
                    slot_id=str(token.get("slot_id") or "")
                    if not slot_id:
                        family_id=str(token.get("family_id") or "")
                        matches=[
                            str(slot.get("id") or f"slot_{index}")
                            for index,slot in enumerate(slots,1)
                            if str(slot.get("family_id") or "")==family_id
                        ]
                        slot_id=matches[0] if len(matches)==1 else ""
                    if slot_id not in slot_ids:
                        if "family_id" in token:
                            raise ValueError(
                                "A reviewed Tag template token plan contradicts its anchor or modifier slot."
                            )
                        raise ValueError("A template chip references an unavailable typed slot.")
                    referenced=next(
                        slot for index,slot in enumerate(slots,1)
                        if str(slot.get("id") or f"slot_{index}")==slot_id
                    )
                    for field_name in (
                        "family_id","placeholder","included_value_ids",
                        "excluded_value_ids","invalid_value_ids","held_value_ids",
                    ):
                        if field_name in token and token.get(field_name)!=referenced.get(field_name):
                            raise ValueError(
                                "A reviewed Tag template token plan contradicts its anchor or modifier slot."
                            )
                    slot_tokens.add(slot_id)
                elif kind=="separator":
                    text=str(token.get("text") or "")
                    if not text or any(character.isalnum() for character in text):
                        raise ValueError("A separator chip may contain punctuation or spacing only.")
                else:
                    raise ValueError("A reviewed template contains an unsupported chip kind.")
            if anchor_tokens!=set(self.anchor_ids) or slot_tokens!=set(slot_ids):
                raise ValueError("Every reviewed anchor and modifier slot must appear in the ordered chip plan.")
        if self.generation_enabled or self.fact_expansion_enabled:
            review=deepcopy(dict(self.policy_review))
            fingerprint=str(review.pop("fingerprint","") or "")
            reviewed_version=int(self.policy_review.get("template_version") or 0)
            generation_review=self.policy_review.get("generation",{})
            fact_review=self.policy_review.get("fact_expansion",{})
            if not isinstance(generation_review,Mapping):generation_review={}
            if not isinstance(fact_review,Mapping):fact_review={}
            if (
                not fingerprint or _fingerprint(review)!=fingerprint
                or reviewed_version!=self.version
                or str(self.policy_review.get("template_id") or "")!=self.id
                or str(self.policy_review.get("scope_fingerprint") or "")
                    !=self.policy_scope_fingerprint
                or bool(generation_review.get("enabled",False))
                    !=self.generation_enabled
                or bool(fact_review.get("enabled",False))
                    !=self.fact_expansion_enabled
            ):
                raise ValueError("Enabled template policies need a separate review for this exact template version.")


@dataclass(frozen=True)
class TagTemplateApprovalResult:
    record: ReviewedTagTemplate
    backup_path: Path | None
    created: bool
    updated: bool


class TagTemplateStoreService:
    """Layer and save reviewed templates through the Automation repository."""

    def __init__(self,repository: Any) -> None:
        self.repository=repository

    @staticmethod
    def _records(raw: Any) -> tuple[ReviewedTagTemplate,...]:
        if not isinstance(raw,list):
            raise ValueError("The reviewed Tag template collection is not a list.")
        records=[]
        for index,item in enumerate(raw,1):
            try:
                records.append(ReviewedTagTemplate.from_dict(item))
            except (TypeError,ValueError,OverflowError) as exc:
                stable_id=(
                    str(item.get("id") or "").strip()
                    if isinstance(item,dict) else ""
                )
                identity=f" {stable_id}" if stable_id else f" #{index}"
                raise ValueError(
                    f"Reviewed Tag template{identity} needs repair: {exc}"
                ) from exc
        records=tuple(records)
        ids=[record.id for record in records]
        if len(ids)!=len(set(ids)):
            raise ValueError("The reviewed Tag template collection contains duplicate stable IDs.")
        return records

    def records_with_origin(self) -> tuple[tuple[str,ReviewedTagTemplate],...]:
        layered={}
        for origin,store in (("Default",self.repository.defaults),("User",self.repository.user)):
            for record in self._records(store.data.get("tag_templates",[])):
                layered[record.id]=(origin,record)
        return tuple(sorted(
            layered.values(),key=lambda item:(item[1].template.casefold(),item[1].id),
        ))

    def save_record(
        self,record: ReviewedTagTemplate,*,
        expected_current_fingerprint: str="",
    ) -> TagTemplateApprovalResult:
        """Verified-save one already reviewed structured template record."""

        record.validate()
        user_records=list(self._records(
            self.repository.user.data.get("tag_templates",[]),
        ))
        effective={item.id:item for _origin,item in self.records_with_origin()}
        current=effective.get(record.id)
        if current is not None and not current.supported:
            raise ValueError(
                "This template uses a newer format and cannot be overwritten by this version of CollectiGen."
            )
        if expected_current_fingerprint and (
            current is None or current.semantic_fingerprint!=expected_current_fingerprint
        ):
            raise RuntimeError(
                "This reviewed template changed after editing opened. Reopen the current version before saving."
            )
        if current is not None and current.semantic_fingerprint==record.semantic_fingerprint:
            return TagTemplateApprovalResult(current,None,False,False)
        if current is not None and record.version<=current.version:
            raise ValueError("A changed reviewed template must increment its version.")

        before=deepcopy(self.repository.user.data.get("tag_templates",[]))
        user_path=Path(self.repository.user.path)
        before_bytes=user_path.read_bytes() if user_path.exists() else None
        user_records=[item for item in user_records if item.id!=record.id]
        user_records.append(record); user_records.sort(key=lambda item:item.id)
        self.repository.user.data["tag_templates"]=[
            item.to_dict() for item in user_records
        ]
        try:
            backup=self.repository.user.save_verified()
        except BaseException:
            self.repository.user.data["tag_templates"]=before
            raise
        try:
            from .automation_repository import AutomationRuleRepository
            verified=AutomationRuleRepository(
                self.repository.defaults.path,self.repository.user.path,
                rule_pair_admission=self.repository.rule_pair_admission,
            )
            saved=next((
                item for _origin,item in TagTemplateStoreService(verified).records_with_origin()
                if item.id==record.id
            ),None)
            if saved is None or saved.to_dict()!=record.to_dict():
                raise RuntimeError(
                    "The reviewed Tag template failed semantic read-back verification."
                )
        except BaseException:
            temporary=user_path.with_suffix(
                user_path.suffix+".template-restore.tmp"
            )
            if before_bytes is None:
                if user_path.exists():user_path.unlink()
            else:
                temporary.write_bytes(before_bytes); temporary.replace(user_path)
            self.repository.load()
            raise
        self.repository.load()
        return TagTemplateApprovalResult(
            saved,backup,current is None,current is not None,
        )

    @staticmethod
    def from_report(
        report: TagTemplateExpansionReport,*,version: int=1,
        reviewed_at: str | None=None,extras: Mapping[str,Any] | None=None,
        nested_extras: Mapping[str,Any] | None=None,
        selected_result_ids: Any=(),
        max_per_run: int=DEFAULT_TAG_TEMPLATE_MAX_PER_RUN,
    ) -> ReviewedTagTemplate:
        candidate=report.candidate
        if report.bounded_values:
            raise ValueError("Approve only a complete expansion preview; this report was bounded.")
        if not candidate.ready_for_review or len(candidate.anchor_ids)!=1:
            raise ValueError("This reusable Tag family is not ready for approval.")
        requested=_ordered_ids(selected_result_ids)
        by_id={item.id:item for item in report.items}
        missing=tuple(stable_id for stable_id in requested if stable_id not in by_id)
        if missing:
            raise ValueError("The reviewed result selection no longer belongs to this expansion preview.")
        held=tuple(stable_id for stable_id in requested if by_id[stable_id].held)
        if held:
            raise ValueError("Held expansion results cannot be selected for later creation planning.")
        requested_set=set(requested)
        selected=tuple(item.id for item in report.items if item.id in requested_set)
        selection_fingerprint=tag_template_selection_fingerprint(
            report.approval_fingerprint,selected,
        )
        record=ReviewedTagTemplate(
            id=tag_template_id(candidate.id),version=max(1,int(version)),
            template=candidate.template,anchor_ids=candidate.anchor_ids,
            anchor_labels=(candidate.anchor,),
            anchor_roles=("base",),
            modifier_family_id=candidate.modifier_family_id,
            modifier_family=candidate.modifier_family,
            placeholder=candidate.placeholder,token_position=candidate.token_position,
            included_value_ids=report.included_value_ids,
            excluded_value_ids=report.excluded_value_ids,
            invalid_value_ids=report.invalid_value_ids,
            held_value_ids=report.held_value_ids,
            selected_result_ids=selected,selection_reviewed=True,
            selection_fingerprint=selection_fingerprint,
            policy_fingerprint=candidate.policy_fingerprint,
            candidate_fingerprint=candidate.review_fingerprint,
            expansion_fingerprint=report.approval_fingerprint,
            reviewed_at=reviewed_at or _utc_now(),
            max_per_run=max(1,int(max_per_run)),extras=dict(extras or {}),
            nested_extras=deepcopy(dict(nested_extras or {})),
        )
        record.validate(); return record

    def approve(
        self,report: TagTemplateExpansionReport,*,reviewed_at: str | None=None,
        selected_result_ids: Any=(),
    ) -> TagTemplateApprovalResult:
        user_records=list(self._records(self.repository.user.data.get("tag_templates",[])))
        effective={record.id:record for _origin,record in self.records_with_origin()}
        template_id=tag_template_id(report.candidate.id)
        current=effective.get(template_id)
        if current is not None and not current.supported:
            raise ValueError("This template uses a newer format and cannot be overwritten by this version of CollectiGen.")
        user_current=next((record for record in user_records if record.id==template_id),None)
        next_version=(current.version+1) if current is not None else 1
        proposed=self.from_report(
            report,version=next_version,reviewed_at=reviewed_at,
            selected_result_ids=selected_result_ids,
            max_per_run=(
                current.max_per_run if current is not None else
                DEFAULT_TAG_TEMPLATE_MAX_PER_RUN
            ),
            extras=(
                user_current.extras if user_current is not None else
                current.extras if current is not None else {}
            ),
            nested_extras=(
                user_current.nested_extras if user_current is not None else
                current.nested_extras if current is not None else {}
            ),
        )
        if current is not None and current.semantic_fingerprint==proposed.semantic_fingerprint:
            return TagTemplateApprovalResult(current,None,False,False)

        before=deepcopy(self.repository.user.data.get("tag_templates",[]))
        user_path=Path(self.repository.user.path)
        before_bytes=user_path.read_bytes() if user_path.exists() else None
        user_records=[record for record in user_records if record.id!=template_id]
        user_records.append(proposed); user_records.sort(key=lambda item:item.id)
        self.repository.user.data["tag_templates"]=[record.to_dict() for record in user_records]
        try:
            backup=self.repository.user.save_verified()
        except BaseException:
            self.repository.user.data["tag_templates"]=before
            raise

        try:
            from .automation_repository import AutomationRuleRepository
            verified=AutomationRuleRepository(
                self.repository.defaults.path,self.repository.user.path,
                rule_pair_admission=self.repository.rule_pair_admission,
            )
            saved=next((
                record for _origin,record in TagTemplateStoreService(verified).records_with_origin()
                if record.id==template_id
            ),None)
            if saved is None or saved.to_dict()!=proposed.to_dict():
                raise RuntimeError("The reviewed Tag template failed semantic read-back verification.")
        except BaseException:
            temporary=user_path.with_suffix(user_path.suffix+".template-restore.tmp")
            if before_bytes is None:
                if user_path.exists():user_path.unlink()
            else:
                temporary.write_bytes(before_bytes); temporary.replace(user_path)
            self.repository.load()
            raise
        self.repository.load()
        return TagTemplateApprovalResult(
            saved,backup,current is None,current is not None,
        )
