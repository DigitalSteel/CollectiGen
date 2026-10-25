"""Versioned, disposable per-image proof for semantic readiness reuse.

Only source-family-matched rows may use this proof. Rule/evaluator contracts
remain hard guards; image authority and completed preparation are never changed.
"""
from dataclasses import asdict
from collections import OrderedDict
import hashlib
import json

from .ai_interrogation import split_plain_language
from .annotation import ImageAnnotation
from .automation_repository import active_automation_engine
from .confirmed_tag_identity import ConfirmedTagIdentityService
from .conflict_policy import conflict_rule_cache_payload
from .export_profile import ExportProfile
from .settings import normalize_plain_language_exclusion
from .tag_collection import normalize_tag


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),
        ensure_ascii=False,allow_nan=False).encode("utf-8")).hexdigest()


class WorkflowSemanticProof:
    VERSION=1
    MAX_NAMES=4096
    MAX_INPUT_CHARS=262144
    MAX_LOOKUPS=8192

    @staticmethod
    def _bounded(value):
        # Check before parsing or traversing persisted proof content. Limit
        # nodes and text as well as depth; a corrupt disposable row fails closed.
        pending=[(value,0)];nodes=0;characters=0
        while pending:
            item,depth=pending.pop();nodes+=1
            if nodes>32768 or depth>32:return False
            if isinstance(item,str):
                characters+=len(item)
                if characters>1048576:return False
            elif isinstance(item,dict):
                if len(item)>4096 or any(not isinstance(key,str) for key in item):return False
                pending.extend((key,depth+1) for key in item)
                pending.extend((child,depth+1) for child in item.values())
            elif isinstance(item,(list,tuple)):
                if len(item)>4096:return False
                pending.extend((child,depth+1) for child in item)
            elif item is not None and not isinstance(item,(bool,int,float)):return False
        return True

    @classmethod
    def _valid_inputs(cls,inputs):
        if not isinstance(inputs,dict) or not cls._bounded(inputs):return False
        for key in ("names","present","outfits","sentences"):
            values=inputs.get(key)
            if not isinstance(values,(list,tuple)) or len(values)>cls.MAX_NAMES:return False
            if any(not isinstance(value,str) for value in values):return False
            if sum(map(len,values))>cls.MAX_INPUT_CHARS:return False
        identities=inputs.get("stored_ids")
        return isinstance(identities,dict) and len(identities)<=cls.MAX_NAMES and all(
            isinstance(key,str) and isinstance(value,str) for key,value in identities.items())
    # These fields are the complete input to the default output projection.
    PROJECTION_FIELDS=frozenset({"schema_version","global","characters","objects",
        "scene","contexts","visual_style","sdxl_tags","plain_language",
        "uncategorized","tag_identities","confirmed_absent","export_excluded",
        "custom_groups"})

    def __init__(self,knowledge,characters,categories,ignored_plain_language,*,inspect_sources=False,engine=None):
        self.knowledge=knowledge;self.characters=characters
        self.ignored={normalize_plain_language_exclusion(value) for value in ignored_plain_language}
        self.inspect_sources=bool(inspect_sources)
        self.profile=ExportProfile()
        engine=active_automation_engine(knowledge) if engine is None else engine
        conflicts=getattr(engine,"conflicts",None)
        evaluator=getattr(conflicts,"relationship_evaluator",None)
        relationships=getattr(getattr(evaluator,"registry",None),"relationships",())
        # No matched-rule shortcut: additions and negative applicability still
        # need a complete rule contract before this guard can be relaxed.
        hard_contract={"version":self.VERSION,"algorithm":3,
            "source_projection":bool(inspect_sources),
            "rules":getattr(engine,"rules",{}),
            "conflicts":[conflict_rule_cache_payload(rule,structured=True) for rule in getattr(conflicts,"rules",())],
            "relationships":[item.to_dict() for item in relationships if item.kind!="identity_alias"],
            "relationship_error":getattr(conflicts,"relationship_error",""),
            "profile":self.profile.to_dict()}
        self.hard=_digest(hard_contract)
        expanded_contract={**hard_contract,"conflicts":[
            conflict_rule_cache_payload(rule,structured=True,explicit_defaults=True)
            for rule in getattr(conflicts,"rules",())]}
        self._expanded_hard=_digest(expanded_contract)
        self._prepare_relationship_scope(relationships,hard_contract,expanded_contract)
        self._lookups=OrderedDict()
        self._plans=OrderedDict()
        self._lookup_revision=getattr(knowledge,"_relationship_signature_revision",None)

    def _prepare_relationship_scope(self,relationships,hard_contract,expanded_contract):
        # Only the understood tag-to-tag graph is scoped. Unknown kinds and
        # non-Tag policies remain hard guards until their consumers are covered.
        scoped={item.id:item for item in relationships
            if item.endpoints and all(endpoint.kind=="tag" or (endpoint.kind=="removed_tag" and (item.disabled or not item.reviewed)) for endpoint in item.endpoints)
            and ((item.kind in {"specificity","entailment"} and item.directed
                  and len(item.endpoints)>1)
                 or (item.kind=="scoped_constraint" and not item.directed))}
        self._relationship_edges={};self._relationship_constraints={}
        self._relationship_payloads={}
        for stable_id,item in scoped.items():
            if item.disabled or not item.reviewed:continue
            self._relationship_payloads[stable_id]=item.to_dict()
            if item.directed:
                self._relationship_edges.setdefault(item.source_endpoint.id,[]).append(item)
            else:
                for endpoint in item.endpoints:
                    self._relationship_constraints.setdefault(endpoint.id,[]).append(stable_id)
        self._scoped_conflicts={}
        remaining_conflicts=[]
        for rule in hard_contract["conflicts"]:
            relationship_id=rule.get("relationship_id")
            if relationship_id in scoped:
                self._scoped_conflicts.setdefault(relationship_id,[]).append(rule)
            else:remaining_conflicts.append(rule)
        self.scoped_hard=_digest({**hard_contract,"relationship_scope":1,
            "conflicts":remaining_conflicts,
            "relationships":[item for item in hard_contract["relationships"]
                             if item["id"] not in scoped]})
        self._expanded_scoped_conflicts={}
        for rule in expanded_contract["conflicts"]:
            if rule.get("relationship_id") in scoped:
                self._expanded_scoped_conflicts.setdefault(rule["relationship_id"],[]).append(rule)
        self._expanded_scoped_hard=_digest({**expanded_contract,"relationship_scope":1,
            "conflicts":[rule for rule in expanded_contract["conflicts"]
                         if rule.get("relationship_id") not in scoped],
            "relationships":[item for item in expanded_contract["relationships"]
                             if item["id"] not in scoped]})

    def _relationship_digest(self,inputs,*,conflicts=None):
        # Traverse outgoing implications/hierarchy from every positive, absent,
        # unknown and pending input, not merely previously matched findings.
        # New edges and constraints are therefore checked as well as removals.
        pending=set(inputs["stored_ids"].values())
        for name in inputs["names"]:
            lookup=self._lookup(name)
            if lookup is not None:pending.update(lookup["records"])
        visited=set();selected=set()
        while pending:
            stable_id=pending.pop()
            if stable_id in visited:continue
            visited.add(stable_id)
            if len(visited)>32768:raise ValueError("Relationship proof exceeds its bound")
            selected.update(self._relationship_constraints.get(stable_id,()))
            for relationship in self._relationship_edges.get(stable_id,()):
                selected.add(relationship.id)
                pending.update(endpoint.id for endpoint in relationship.target_endpoints)
        conflicts=self._scoped_conflicts if conflicts is None else conflicts
        return _digest([(stable_id,self._relationship_payloads[stable_id],
                         conflicts.get(stable_id,()))
                        for stable_id in sorted(selected)])

    def _refresh_revision(self):
        revision=getattr(self.knowledge,"_relationship_signature_revision",None)
        if revision!=self._lookup_revision:
            self._lookups.clear();self._plans.clear();self._lookup_revision=revision

    def _lookup(self,value):
        self._refresh_revision()
        if value in self._lookups:
            self._lookups.move_to_end(value)
            return self._lookups[value]
        knowledge=self.knowledge
        if knowledge is None:result=None
        else:
            known=knowledge.find(value)
            owners=tuple(knowledge.identity_owner_ids(normalize_tag(value)))
            removed=tuple(knowledge.removed_identity_ids(value))
            ids=set((*owners,*removed))
            resolved=knowledge.resolve_tag_id(value)
            if resolved:ids.add(resolved)
            if known is not None:ids.add(known.id)
            records={}
            for stable_id in sorted(ids):
                record=knowledge.tags.get(stable_id)
                archive=knowledge.removed_archive_for_tag(stable_id)
                records[stable_id]={
                    "live":({"id":record.id,"display":record.display,"category":record.category}
                        if record is not None else None),
                    "removed":archive.to_dict() if archive is not None else None,
                }
            result={"known":known.id if known is not None else None,
                "owners":owners,"removed":removed,"resolved":resolved,"records":records,
                "represented":normalize_tag(value) in getattr(knowledge,"_represented_alias_keys",set())}
        self._lookups[value]=result
        if len(self._lookups)>self.MAX_LOOKUPS:self._lookups.popitem(last=False)
        return result

    def _outcomes(self,inputs):
        self._refresh_revision()
        present=inputs["present"]
        key=(tuple(present),tuple(sorted(inputs["stored_ids"].items())))
        if key in self._plans:
            plan=self._plans[key];self._plans.move_to_end(key)
        else:
            plan=(asdict(ConfirmedTagIdentityService.shared(self.knowledge).plan(
                present,stored_ids=inputs["stored_ids"],confirmation=False))
                if self.knowledge is not None else None)
            self._plans[key]=plan
            if len(self._plans)>128:self._plans.popitem(last=False)
        return {"lookups":{name:self._lookup(name) for name in inputs["names"]},
            "confirmed":plan,
            "outfits":{value:value in getattr(self.characters,"outfits",{})
                for value in inputs["outfits"]} if self.characters is not None else None,
            "ignored":sorted(self.ignored.intersection(inputs["sentences"]))}

    def build(self,record,*,candidate_findings=None,source_lookup_names=None,include_output_health=False,output_preview=None):
        if self.inspect_sources and source_lookup_names is None:return None
        annotation=record.annotation
        raw_inputs=(annotation.source,[item.to_dict() for item in annotation.ai_evidence],
            candidate_findings,source_lookup_names,annotation.plain_language,annotation.tag_identities)
        if not self._bounded(raw_inputs):return None
        names=set(annotation.all_structured_tags())
        names.update(annotation.confirmed_absent)
        # Include decision-map keys, negative evidence and all candidate/AI
        # values, even when presently unknown or non-actionable. No raw source
        # text is interpreted as confirmed knowledge by this disposable index.
        def collect(value):
            if isinstance(value,str):
                if value.strip():names.add(value.strip())
            elif isinstance(value,dict):
                for key,item in value.items():collect(key);collect(item)
            elif isinstance(value,(tuple,list)):
                for item in value:collect(item)
        collect(annotation.source)
        collect([item.to_dict() for item in annotation.ai_evidence])
        collect(candidate_findings)
        collect(source_lookup_names)
        collect(annotation.plain_language)
        collect(annotation.tag_identities)
        if len(names)>self.MAX_NAMES or sum(map(len,names))>self.MAX_INPUT_CHARS:return None
        sentences={normalize_plain_language_exclusion(sentence)
            for value in names for sentence in split_plain_language(value)}
        inputs={"names":sorted(names),"present":list(annotation.all_structured_tags()),
            "stored_ids":dict(annotation.tag_identities),
            "outfits":sorted({outfit for slot in annotation.characters for outfit in slot.outfit_ids}),
            "sentences":sorted(sentences-{""})}
        if not self._valid_inputs(inputs):return None
        proof={"version":self.VERSION,"hard":self.hard,"inputs":inputs,
            "digest":_digest(self._outcomes(inputs)),
            "relationship_scope":1,"scoped_hard":self.scoped_hard,
            "relationship_digest":self._relationship_digest(inputs)}
        if include_output_health:
            payload=annotation.to_dict()
            projection={key:value for key,value in payload.items() if key in self.PROJECTION_FIELDS}
            if not self._bounded(projection):return None
            proof["projection"]={"input":projection,
                "digest":_digest(output_preview if output_preview is not None else
                    self.profile.render(annotation,self.characters,self.knowledge))}
        return proof if self._bounded(proof) else None

    def matches(self,proof):
        try:
            if not isinstance(proof,dict) or proof.get("version")!=self.VERSION:
                return False
            if not self._bounded(proof):return False
            inputs=proof["inputs"]
            if not self._valid_inputs(inputs):return False
            if any(key in proof for key in ("relationship_scope","scoped_hard","relationship_digest")):
                if proof.get("relationship_scope")!=1:return False
                matches=(proof.get("scoped_hard")==self.scoped_hard
                         and proof.get("relationship_digest")==self._relationship_digest(inputs))
                if not matches and proof.get("scoped_hard")==self._expanded_scoped_hard:
                    matches=proof.get("relationship_digest")==self._relationship_digest(
                        inputs,conflicts=self._expanded_scoped_conflicts)
                if not matches:
                    return False
            elif proof.get("hard") not in {self.hard,self._expanded_hard}:
                # Legacy complete proofs retain their exact compatibility;
                # installing the scoped proof does not invalidate saved rows.
                return False
            if _digest(self._outcomes(inputs))!=proof["digest"]:return False
            projection=proof.get("projection")
            if projection is not None:
                annotation=ImageAnnotation.from_dict(projection["input"],"cached-image")
                if _digest(self.profile.render(annotation,self.characters,self.knowledge))!=projection["digest"]:
                    return False
            return True
        except (KeyError,AttributeError,TypeError,ValueError,OSError,RuntimeError):
            return False
