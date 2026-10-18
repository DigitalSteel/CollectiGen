from __future__ import annotations

from collections import Counter
from dataclasses import dataclass,field
from pathlib import Path
from typing import Iterable

from .automation_rules import AutomationEngine
from .image_record import ImageRecord
from .tag_collection import TagCollection,normalize_tag


@dataclass(frozen=True)
class RepresentativeRuleCase:
    name: str
    before: tuple[str,...]
    expected: tuple[str,...]
    automatic: bool = True


@dataclass
class AutomationRuleAuditReport:
    checked_rules: int = 0
    checked_cases: int = 0
    passed_cases: int = 0
    issues: list[str] = field(default_factory=list)
    candidates: list[tuple[str,str]] = field(default_factory=list)


@dataclass(frozen=True)
class MergeRuleCandidate:
    broad_tag: str
    specific_tag: str
    image_count: int
    examples: tuple[str,...] = ()


class AutomationRuleAuditService:
    """Deterministic safety audit for merge, wildcard, and combination semantics."""

    def __init__(self,engine: AutomationEngine) -> None:
        self.engine=engine

    def audit(self,cases: Iterable[RepresentativeRuleCase]=(),tag_sets: Iterable[Iterable[str]]=()) -> AutomationRuleAuditReport:
        report=AutomationRuleAuditReport()
        seen=set()
        for kind in ("merge_rules","wildcard_rules","combine_rules","ignore_rules"):
            for index,record in enumerate(self.engine.rules.get(kind,[])):
                report.checked_rules+=1
                if kind=="ignore_rules":
                    sources=[record.get("tag","")]; target="[removed]"
                elif kind=="wildcard_rules":
                    sources=[record.get("pattern","")]; remove=record.get("remove",[]); sources.extend([remove] if isinstance(remove,str) else remove); target=str(record.get("pattern","")).strip()
                else:
                    sources=list(record.get("remove",[])); target=str(record.get("replace","")).strip()
                identity=(kind,tuple(sorted(normalize_tag(value) for value in sources)),normalize_tag(target))
                if not target or any(not str(value).strip() for value in sources): report.issues.append(f"{kind}[{index}] is incomplete")
                if identity in seen: report.issues.append(f"{kind}[{index}] duplicates an earlier active rule")
                seen.add(identity)
                confidence=float(record.get("confidence",1))
                if confidence<.95 and not record.get("requires_review"):
                    report.issues.append(f"{kind}[{index}] is below 95% confidence but is not marked for review")
                for placeholder in self.engine.placeholder.findall(" ".join([*map(str,sources),target])):
                    if self.engine._variable_key(placeholder) not in self.engine.variables: report.issues.append(f"{kind}[{index}] references missing value list {{{placeholder}}}")
        for case in cases:
            report.checked_cases+=1
            after,_=self.engine.apply_to_tags(case.before,automatic_only=case.automatic)
            expected=TagCollection(case.expected).normalized_set()
            if TagCollection(after).normalized_set()!=expected:
                report.issues.append(f"{case.name}: expected {list(case.expected)}, got {after}")
                continue
            repeated,_=self.engine.apply_to_tags(after,automatic_only=case.automatic)
            if repeated!=after:
                report.issues.append(f"{case.name}: active rules are not idempotent")
                continue
            report.passed_cases+=1
        known_pairs={(normalize_tag(a),normalize_tag(b)) for kind in ("merge_rules","wildcard_rules","combine_rules") for record in self.engine.rules.get(kind,[]) for a in ([record.get("remove")] if isinstance(record.get("remove"),str) else record.get("remove",[])) for b in [record.get("pattern") or record.get("replace")] if a and b}
        candidates=set()
        for values in tag_sets:
            tags=TagCollection(values).to_list()
            for broad in tags:
                broad_key=normalize_tag(broad)
                if len(broad_key)<3: continue
                for specific in tags:
                    specific_key=normalize_tag(specific)
                    if broad_key==specific_key or not specific_key.endswith(" "+broad_key): continue
                    if (broad_key,specific_key) not in known_pairs: candidates.add((broad,specific))
        report.candidates=sorted(candidates,key=lambda item:(normalize_tag(item[0]),normalize_tag(item[1])))
        return report

    def suggest_merges(self,images: Iterable[Path],progress=None,should_cancel=None,limit: int=200) -> list[MergeRuleCandidate]:
        """Rank conservative broad/specific merge candidates found repeatedly in confirmed image tags."""
        paths=list(images); counts=Counter(); labels={}; examples={}
        for index,path in enumerate(paths,1):
            if should_cancel and should_cancel(): break
            tags=TagCollection(ImageRecord(path).annotation.all_structured_tags()).to_list(); seen=set()
            for broad in tags:
                broad_key=normalize_tag(broad)
                if len(broad_key)<3: continue
                for specific in tags:
                    specific_key=normalize_tag(specific)
                    if broad_key==specific_key or not specific_key.endswith(" "+broad_key): continue
                    pair=(broad_key,specific_key)
                    if pair in seen: continue
                    projected,_=self.engine.apply_to_tags([broad,specific])
                    if broad_key not in TagCollection(projected).normalized_set(): continue
                    seen.add(pair); counts[pair]+=1; labels.setdefault(pair,(broad,specific))
                    sample=examples.setdefault(pair,[])
                    if len(sample)<3: sample.append(Path(path).name)
            if progress: progress(index,len(paths),Path(path).name)
        ordered=sorted(counts,key=lambda pair:(-counts[pair],pair[0],pair[1]))[:max(1,int(limit))]
        return [MergeRuleCandidate(*labels[pair],counts[pair],tuple(examples[pair])) for pair in ordered]
