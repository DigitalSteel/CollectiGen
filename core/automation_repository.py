from __future__ import annotations

import json
from contextlib import contextmanager,nullcontext
from copy import deepcopy
from datetime import datetime,timezone
from pathlib import Path
from typing import Any,Iterable,Mapping

from .automation_rules import AutomationRuleStore
from .automation_rule_conversion import RulePairAdmission
from .rule_intents import REJECT_INCOMING_EVIDENCE, suggested_rule_intent
from .tag_collection import TagCollection,normalize_tag


_ACTIVE_ENGINE_CACHE: tuple[tuple,Any] | None = None


def rule_key(kind: str,record: dict[str,Any]) -> tuple:
    if kind=="ignore_rules":
        return kind,normalize_tag(str(record.get("tag","")))
    if kind=="wildcard_rules":
        remove=record.get("remove",[]); remove=[remove] if isinstance(remove,str) else remove
        return kind,normalize_tag(str(record.get("pattern",""))),tuple(sorted(normalize_tag(str(value)) for value in remove))
    return kind,tuple(sorted(normalize_tag(str(value)) for value in record.get("remove",[]))),normalize_tag(str(record.get("replace","")))


def rule_key_id(kind: str,record: dict[str,Any]) -> str:
    return json.dumps(rule_key(kind,record),ensure_ascii=False,separators=(",",":"))


def combine_rule_layers(default_data: dict[str,Any],user_data: dict[str,Any]) -> dict[str,Any]:
    """Combine captured rule layers without loading or writing an authority."""
    result={"variables":{},"merge_rules":[],"wildcard_rules":[],"combine_rules":[],"ignore_rules":[],"tag_templates":[]}
    variable_names=set(default_data["variables"])|set(user_data["variables"])|set(user_data["variable_overrides"])
    for name in variable_names:
        values=[]; seen=set()
        override=user_data["variable_overrides"].get(name)
        sources=(override if isinstance(override,list) else default_data["variables"].get(name,[]),user_data["variables"].get(name,[]))
        for source in sources:
            for value in source:
                key=normalize_tag(value)
                if key not in seen: seen.add(key); values.append(value)
        result["variables"][name]=values
    disabled=set(user_data["disabled_rules"])
    for kind in ("merge_rules","wildcard_rules","combine_rules","ignore_rules"):
        seen=set()
        for origin,source in (("Default",default_data[kind]),("User",user_data[kind])):
            for record in source:
                key=rule_key(kind,record)
                if origin=="Default" and rule_key_id(kind,record) in disabled: continue
                if key not in seen: seen.add(key); result[kind].append(dict(record))
    templates={}
    for source in (default_data["tag_templates"],user_data["tag_templates"]):
        for record in source if isinstance(source,list) else ():
            if not isinstance(record,dict):continue
            stable_id=str(record.get("id") or "").strip()
            if stable_id:templates[stable_id]=deepcopy(record)
    result["tag_templates"]=[templates[key] for key in sorted(templates)]
    return result


class AutomationRuleRepository:
    """Layer reviewed versioned automation knowledge with private user additions."""

    def __init__(self,defaults_path: str|Path,user_path: str|Path,legacy_path: str|Path|None=None,
                 *, rule_pair_admission: RulePairAdmission | None = None):
        self.rule_pair_admission = rule_pair_admission
        self.defaults=AutomationRuleStore(defaults_path)
        self.user=AutomationRuleStore(user_path, rule_pair_admission=rule_pair_admission)
        self.legacy_path=Path(legacy_path) if legacy_path else None
        if self.legacy_path and self.legacy_path.is_file() and not Path(user_path).exists(): self.upgrade_legacy(self.legacy_path)

    def _assert_defaults_current(self) -> None:
        if getattr(self.defaults,"_source_error",""):
            raise RuntimeError(self.defaults._source_error)
        try:
            current=(
                self.defaults.path.read_bytes()
                if self.defaults.path.exists() else None
            )
        except OSError as exc:
            self.defaults.load()
            raise RuntimeError(
                "Built-in Rules & Automation knowledge is unavailable. Restore "
                "access, then reload and review the current rules."
            ) from exc
        if current!=getattr(self.defaults,"_loaded_bytes",None):
            self.defaults.load()
            raise RuntimeError(
                "Built-in Rules & Automation knowledge changed after this action "
                "opened. Reload and review the current rules before retrying."
            )

    def _mutate_user(self,mutator):
        self._assert_defaults_current()
        return self.user.mutate_verified(mutator)

    def upgrade_legacy(self,legacy_path: str|Path) -> dict[str,int]:
        """Merge one old saved_rules.json without replacing reviewed current values."""
        legacy=AutomationRuleStore(legacy_path)
        if getattr(legacy,"_source_error",""):
            raise RuntimeError(legacy._source_error)
        default_keys={rule_key(kind,record) for kind in ("merge_rules","wildcard_rules","combine_rules","ignore_rules") for record in self.defaults.data[kind]}
        def mutate(data):
            added_variables=added_rules=duplicates=0
            for name,values in legacy.data["variables"].items():
                upgraded_name="held_item" if name=="holding" else name
                defaults={normalize_tag(value) for value in self.defaults.data["variables"].get(upgraded_name,[])}
                rejected={"dark","blonde"} if upgraded_name=="color" else set()
                existing=data["variables"].setdefault(upgraded_name,[])
                seen=defaults|rejected|{normalize_tag(value) for value in existing}
                additions=[value for value in values if normalize_tag(value) and normalize_tag(value) not in seen]
                if additions:
                    existing.extend(additions); added_variables+=len(additions)
                    seen.update(normalize_tag(value) for value in additions)
            for kind in ("merge_rules","wildcard_rules","combine_rules","ignore_rules"):
                confidence=.75 if kind=="combine_rules" else .9 if kind=="wildcard_rules" else .85
                existing_keys={rule_key(kind,record) for record in data[kind]}
                for source in legacy.data[kind]:
                    record={key:(value.replace("{holding}","{held_item}") if isinstance(value,str) else [item.replace("{holding}","{held_item}") for item in value] if isinstance(value,list) else value) for key,value in source.items()}
                    identity=rule_key(kind,record)
                    if identity in default_keys or identity in existing_keys:
                        duplicates+=1; continue
                    record.setdefault("confidence",confidence); record["requires_review"]=True; record["source"]="legacy_upgrade"
                    data[kind].append(record); existing_keys.add(identity); added_rules+=1
            return {"variables_added":added_variables,"rules_added":added_rules,"duplicates_prevented":duplicates}
        return self._mutate_user(mutate)

    def _upgrade_legacy(self):
        """Compatibility wrapper for older callers."""
        if self.legacy_path is None: return {"variables_added":0,"rules_added":0,"duplicates_prevented":0}
        return self.upgrade_legacy(self.legacy_path)

    def load(self): self.defaults.load(); self.user.load(); return self.combined()

    def combined(self) -> dict[str,Any]:
        return combine_rule_layers(self.defaults.data,self.user.data)

    def rules_with_origin(self):
        disabled=set(self.user.data["disabled_rules"])
        seen=set()
        for origin,store in (("Default",self.defaults),("User",self.user)):
            for kind in ("merge_rules","wildcard_rules","combine_rules","ignore_rules"):
                for index,record in enumerate(store.data[kind]):
                    if origin=="Default" and rule_key_id(kind,record) in disabled: continue
                    key=rule_key(kind,record)
                    if key in seen: continue
                    seen.add(key)
                    yield origin,kind,index,record

    def variables_with_origin(self):
        effective=self.combined()["variables"]
        overrides=self.user.data["variable_overrides"]
        names=set(self.defaults.data["variables"])|set(self.user.data["variables"])|set(overrides)
        for name in names:
            has_default=name in self.defaults.data["variables"]
            has_user=name in self.user.data["variables"]
            if name in overrides: origin="User override"
            elif has_default and has_user: origin="Default + User"
            elif has_default: origin="Default"
            else: origin="User"
            yield origin,name,effective.get(name,[])

    def _prepare_rule(self,kind,source,target,*args,exclude_rule_key=None,**kwargs):
        kwargs.setdefault("intent",suggested_rule_intent(kind))
        analysis=None
        if kind in {"merge_rules","combine_rules"} and kwargs["intent"]=="training_projection":
            from .automation_rules import AutomationEngine
            analysis=AutomationEngine(self.combined()).analyze_projection_rule(
                source,target,exclude_rule_key=exclude_rule_key,
            )
            if analysis.errors: raise ValueError(" ".join(analysis.errors))
            if analysis.warnings: raise ValueError(" ".join(analysis.warnings))
            source=analysis.sources
        record=AutomationRuleStore.rule_record(kind,source,target,*args,**kwargs)
        return record,analysis

    def add_rule(self,kind,source,target,*args,**kwargs):
        record,analysis=self._prepare_rule(kind,source,target,*args,**kwargs)
        if analysis is not None and analysis.duplicate:return analysis
        self._mutate_user(
            lambda data:data[kind].append(record) if record not in data[kind] else None
        )
        return analysis

    @staticmethod
    def _projection_rule_key(kind: str,record: dict[str,Any]):
        from .automation_rules import AutomationEngine
        if kind=="wildcard_rules":
            remove=record.get("remove",[])
            remove=[remove] if isinstance(remove,str) else list(remove)
            sources=[str(record.get("pattern") or ""),*map(str,remove)]
            target=str(record.get("pattern") or "")
        elif kind in {"merge_rules","combine_rules"}:
            remove=record.get("remove",[])
            sources=[remove] if isinstance(remove,str) else list(remove)
            target=str(record.get("replace") or "")
        else:return None
        return AutomationEngine.projection_rule_key(sources,target)

    def replace_rule(
        self,origin,kind,index,new_kind,source,target,*args,**kwargs,
    ):
        """Replace or override one repository rule in one verified user write."""
        if origin not in {"Default","User"}:
            raise ValueError("The selected rule does not belong to the Automation rule store.")
        store=self.defaults if origin=="Default" else self.user
        if kind not in {"merge_rules","wildcard_rules","combine_rules","ignore_rules"}:
            raise ValueError("Choose a supported Automation rule type.")
        if index<0 or index>=len(store.data[kind]):
            raise RuntimeError(
                "Rules & Automation changed after this rule was opened. Reload and "
                "review the current rules before retrying."
            )
        old_record=store.data[kind][index]
        record,analysis=self._prepare_rule(
            new_kind,source,target,*args,
            exclude_rule_key=self._projection_rule_key(kind,old_record),**kwargs,
        )
        def mutate(data):
            if origin=="Default":
                key=rule_key_id(kind,self.defaults.data[kind][index])
                if key not in data["disabled_rules"]:data["disabled_rules"].append(key)
            else:
                if index>=len(data[kind]):
                    raise RuntimeError(
                        "Rules & Automation changed after this rule was opened. "
                        "Reload and review the current rules before retrying."
                    )
                del data[kind][index]
            if not (analysis is not None and analysis.duplicate) and record not in data[new_kind]:
                data[new_kind].append(record)
        self._mutate_user(mutate)
        return analysis
    def add_ignore_tags_verified(
        self,values,*,source: str="knowledge_workbench",
        seen_counts: Mapping[str,int]|None=None,
    ) -> tuple[str,...]:
        """Create exact global-ignore rules as one verified user-layer write."""
        cleaned=[]; seen=set()
        for value in values:
            text=str(value).strip(); key=normalize_tag(text)
            if key and key not in seen:seen.add(key); cleaned.append(text)
        existing={
            normalize_tag(str(record.get("tag") or ""))
            for record in self.combined().get("ignore_rules",[])
        }
        additions=[value for value in cleaned if normalize_tag(value) not in existing]
        if not additions:return ()
        counts={
            normalize_tag(str(value)):int(count or 0)
            for value,count in (seen_counts or {}).items()
            if normalize_tag(str(value))
        }
        now=datetime.now(timezone.utc).isoformat(timespec="seconds")
        records=[]
        for value in additions:
            record={
                "tag":value,"source":source,"created_at":now,
                "intent":REJECT_INCOMING_EVIDENCE,
            }
            key=normalize_tag(value)
            if key in counts:record["seen_count"]=counts[key]
            records.append(record)
        self._mutate_user(lambda data:data["ignore_rules"].extend(records))
        reloaded=AutomationRuleRepository(self.defaults.path,self.user.path,
                                          rule_pair_admission=self.rule_pair_admission)
        saved={normalize_tag(str(record.get("tag") or "")) for record in reloaded.combined().get("ignore_rules",[])}
        missing=[value for value in additions if normalize_tag(value) not in saved]
        if missing:
            raise RuntimeError("The always-ignore rules could not be verified after saving.")
        return tuple(additions)
    def delete_rule(self,origin,kind,index):
        self.delete_rules_verified(((origin,kind,index),))
    def delete_rules_verified(
        self,rules: Iterable[tuple[str,str,int]],
    ) -> dict[str,int]:
        """Delete one compatible Automation selection in one verified write."""
        selected=[]; seen=set()
        supported=("merge_rules","wildcard_rules","combine_rules","ignore_rules")
        for origin,kind,index in rules:
            identity=(str(origin),str(kind),int(index))
            if identity in seen:continue
            seen.add(identity); selected.append(identity)
        if not selected:return {"deleted":0,"disabled":0}
        self._assert_defaults_current()
        for origin,kind,index in selected:
            if origin not in {"Default","User"} or kind not in supported:
                raise ValueError("The selected rule does not belong to the Automation rule store.")
            store=self.defaults if origin=="Default" else self.user
            if index<0 or index>=len(store.data[kind]):
                raise RuntimeError("Rules & Automation changed after this selection was made. Reload and select the current rules.")

        disabled=deleted=0
        def mutate(data):
            nonlocal disabled,deleted
            removals: dict[str,list[int]]={kind:[] for kind in supported}
            for origin,kind,index in selected:
                if origin=="Default":
                    key=rule_key_id(kind,self.defaults.data[kind][index])
                    if key not in data["disabled_rules"]:
                        data["disabled_rules"].append(key)
                    disabled+=1
                else:
                    removals[kind].append(index); deleted+=1
            for kind,indices in removals.items():
                for index in sorted(indices,reverse=True):
                    del data[kind][index]
        self._mutate_user(mutate)
        return {"deleted":deleted,"disabled":disabled}
    def set_variable(self,name,values):
        clean_name=str(name).strip().strip("{}")
        if not clean_name:raise ValueError("Enter a variable name.")
        clean_values=TagCollection(values).to_list()
        self._mutate_user(
            lambda data:data["variables"].__setitem__(clean_name,clean_values)
        )
    def override_variable(self,name,values):
        clean_name=str(name).strip().strip("{}")
        if not clean_name:raise ValueError("Enter a variable name.")
        clean_values=TagCollection(values).to_list()
        def mutate(data):
            data["variable_overrides"][clean_name]=clean_values
            data["variables"].pop(clean_name,None)
        self._mutate_user(mutate)
    def edit_variable(self,origin,old_name,new_name,values,*,expected_values=None):
        """Edit or rename one reusable value list in one verified user write."""
        from .automation_rules import AutomationEngine
        clean_name=str(new_name).strip().strip("{}")
        if not clean_name:raise ValueError("Enter a variable name.")
        clean_values=TagCollection(values).to_list(); old_name=str(old_name)
        def mutate(data):
            names=set(self.defaults.data["variables"])|set(data["variables"])|set(data["variable_overrides"])
            if old_name not in names:
                raise ValueError(f"Reusable values for ‘{old_name}’ changed or disappeared. Reload and review this list before saving.")
            current_origin=next((kind for kind,name,_ in self.variables_with_origin() if name==old_name),None)
            current_values=self.combined()["variables"][old_name]
            if current_origin!=origin or (expected_values is not None and list(expected_values)!=current_values):
                raise ValueError(f"Reusable values for ‘{old_name}’ changed. Reload and review this list before saving.")
            target_key=AutomationEngine._variable_key(clean_name)
            if clean_name!=old_name and any(
                name!=old_name and AutomationEngine._variable_key(name)==target_key
                for name in names
            ):
                raise ValueError(f"Reusable values for ‘{clean_name}’ already exist. Choose a different name or edit that list separately.")
            if old_name!=clean_name:
                if old_name in self.defaults.data["variables"]:
                    data["variable_overrides"][old_name]=[]
                    data["variables"].pop(old_name,None)
                elif origin=="User override":
                    data["variable_overrides"].pop(old_name,None)
                else:
                    data["variables"].pop(old_name,None)
            data["variable_overrides"][clean_name]=clean_values
            data["variables"].pop(clean_name,None)
        self._mutate_user(mutate)
    def delete_variable(self,origin,name):
        def mutate(data):
            if origin=="Default":
                data["variable_overrides"][name]=[]
                data["variables"].pop(name,None)
            elif origin=="User override":
                data["variable_overrides"].pop(name,None)
            else:data["variables"].pop(name,None)
        self._mutate_user(mutate)


def active_automation_engine(knowledge=None):
    """Build the one active-rule evaluator used by suggestions, editing, batches, and export."""
    admission = getattr(knowledge, "rule_pair_admission", None)
    with (admission.admit() if admission is not None else nullcontext()):
        return _active_automation_engine_admitted(knowledge, admission)


def _active_automation_engine_inputs(knowledge, admission):
    """The canonical cache inputs, also rechecked by a bounded read lease."""
    from .paths import DEFAULTS_DIR,RELATIONSHIPS_PATH,USER_DATA_DIR
    paths=(DEFAULTS_DIR/"automation_rules.json",
           admission.rules_path if admission is not None else USER_DATA_DIR/"automation_rules.json")
    signatures=[]
    signature_paths=(
        *paths,
        DEFAULTS_DIR/"conflicts.json",
        DEFAULTS_DIR/"relationships.json",
        RELATIONSHIPS_PATH,
    )
    for path in signature_paths:
        try: stat=path.stat(); signatures.append((stat.st_size,stat.st_mtime_ns))
        except OSError: signatures.append((0,0))
    knowledge_rules=tuple(sorted((
        rule.id,
        tuple(rule.source_tags),
        rule.target_tag,
        bool(rule.enabled),
        bool(rule.require_review),
        str(rule.extras.get("intent") or ""),
    ) for rule in knowledge.merge_rules.values())) if knowledge is not None else ()
    from .relationship_evaluator import legacy_specificity_signature
    knowledge_specificity=legacy_specificity_signature(knowledge)
    key=(*signatures,knowledge_rules,knowledge_specificity,
         admission.authority_key if admission is not None else None,
         id(admission), id(knowledge),
         getattr(knowledge,"_relationship_signature_revision",None) if admission is not None else None)
    return paths,key


@contextmanager
def _active_automation_evaluation(knowledge=None):
    """Lease only already-loaded evidence computation, never source I/O or writes."""
    admission=getattr(knowledge,"rule_pair_admission",None)
    with (admission.admit() if admission is not None else nullcontext()):
        engine,key=_active_automation_engine_admitted(knowledge,admission,with_signature=True)
        lookup_revision=getattr(knowledge,"_relationship_signature_revision",None)
        yield engine
        if getattr(knowledge,"rule_pair_admission",None) is not admission:
            raise RuntimeError("Rule authority changed during assessment. Recheck this image.")
        if admission is not None:
            admission.assert_clear()
        if _active_automation_engine_inputs(knowledge,admission)[1]!=key:
            raise RuntimeError("Active rule inputs changed during assessment. Recheck this image.")
        if getattr(knowledge,"_relationship_signature_revision",None)!=lookup_revision:
            raise RuntimeError("System Knowledge changed during assessment. Recheck this image.")


def _active_automation_engine_admitted(knowledge, admission,*,with_signature=False):
    """Load or reuse an evaluator while its explicitly bound pair is clear."""
    global _ACTIVE_ENGINE_CACHE
    from .automation_rules import AutomationEngine
    from .paths import DEFAULTS_DIR
    paths,key=_active_automation_engine_inputs(knowledge,admission)
    if (_ACTIVE_ENGINE_CACHE and _ACTIVE_ENGINE_CACHE[0]==key
            and _ACTIVE_ENGINE_CACHE[1].knowledge is knowledge
            and _ACTIVE_ENGINE_CACHE[1].rule_pair_admission is admission):
        engine=_ACTIVE_ENGINE_CACHE[1]
        return (engine,key) if with_signature else engine
    combined=AutomationRuleRepository(*paths, rule_pair_admission=admission).combined()
    if knowledge is not None:
        for rule in knowledge.merge_rules.values():
            if not rule.enabled: continue
            combined["merge_rules"].append({
                "remove":list(rule.source_tags),"replace":rule.target_tag,
                "confidence":1.0 if not rule.require_review else .85,
                "requires_review":bool(rule.require_review),"rule_id":rule.id,
                **({"intent":str(rule.extras.get("intent"))} if rule.extras.get("intent") else {}),
            })
    from .conflict_rules import ConflictRuleEngine
    engine=AutomationEngine(
        combined,knowledge,
        ConflictRuleEngine.load(DEFAULTS_DIR/"conflicts.json",tags=knowledge),
        rule_pair_admission=admission,
    ); _ACTIVE_ENGINE_CACHE=(key,engine)
    return (engine,key) if with_signature else engine
