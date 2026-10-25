from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass,replace
from typing import Iterable,Mapping

from .training_duplicate_evidence import TrainingDuplicateEvidence
from .video_temporal import SplitCandidate, transitive_split_groups


SPLIT_RULE_VERSION="collectigen.grouped-hash-v1"
VIDEO_SPLIT_RULE_VERSION="collectigen.grouped-video-lineage-v2"


def source_video_components(candidates: Iterable[object]) -> tuple[frozenset[str], ...]:
    """Join frozen source hashes without opening any media or referenced path.

    Callers supply the recommended-included members. Manifest mappings and live
    candidate values use the same algorithm so saved-plan validation cannot drift.
    """
    samples=[]; original_ids={}
    for index,candidate in enumerate(candidates):
        if isinstance(candidate, Mapping):
            stable_id=str(candidate["stable_id"])
            state=candidate.get("source_state") or {}
            asset_sha=(state.get("file") or {}).get("sha256")
            reference=candidate.get("derived_frame")
            clip_sha=reference.get("source_sha256") if reference else None
        else:
            stable_id=str(candidate.stable_id)
            asset_sha=getattr(getattr(candidate,"evidence",None),"sha256",None)
            clip_sha=getattr(getattr(candidate,"derived_frame",None),"source_sha256",None)
        identity_sha=hashlib.sha256(stable_id.encode("utf-8")).hexdigest()
        sample_id=f"{identity_sha}:{index}"
        original_ids[sample_id]=stable_id
        asset_sha=asset_sha or identity_sha
        samples.append(SplitCandidate(sample_id,asset_sha,clip_sha or asset_sha))
    return tuple(frozenset(original_ids[value] for value in group.member_ids)
                 for group in transitive_split_groups(tuple(samples)))
ROLE_TARGETS=(("training",80),("validation",10),("holdout",10))
MAX_CAPTION_TOKENS_PER_IMAGE=256
MAX_TRACKED_CAPTION_TERMS=4_096
_STOP_WORDS=frozenset({
    "and","are","for","from","has","have","image","into","its","the",
    "this","with",
})


def _stable_hash(value: object) -> str:
    encoded=json.dumps(
        value,ensure_ascii=False,sort_keys=True,separators=(",",":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bounded_words(
    value: str,*,limit: int=MAX_CAPTION_TOKENS_PER_IMAGE,
) -> tuple[tuple[str,...],bool]:
    words=[]
    for match in re.finditer(r"[a-z0-9]+",str(value).casefold()):
        word=match.group(0)
        if len(word)<3 or word in _STOP_WORDS:
            continue
        if len(words)>=max(1,int(limit)):
            return tuple(words),True
        words.append(word)
    return tuple(words),False


def _words(value: str,*,limit: int=MAX_CAPTION_TOKENS_PER_IMAGE) -> tuple[str,...]:
    return _bounded_words(value,limit=limit)[0]


@dataclass(frozen=True)
class TrainingSplitGroup:
    group_id: str
    evidence_kind: str
    member_stable_ids: tuple[str,...]
    recommended_role: str
    evidence: tuple[str,...]=()

    def to_dict(self) -> dict[str,object]:
        return {
            "group_id":self.group_id,
            "evidence_kind":self.evidence_kind,
            "member_stable_ids":list(self.member_stable_ids),
            "recommended_role":self.recommended_role,
            "evidence":list(self.evidence),
        }


@dataclass(frozen=True)
class TrainingDatasetFinding:
    code: str
    category: str
    level: str
    summary: str
    reason: str
    affected_count: int
    denominator: int
    direct_fix: str=""
    stable_ids: tuple[str,...]=()
    evidence: tuple[str,...]=()

    def to_dict(self) -> dict[str,object]:
        return {
            "code":self.code,
            "category":self.category,
            "level":self.level,
            "summary":self.summary,
            "reason":self.reason,
            "affected_count":self.affected_count,
            "denominator":self.denominator,
            "direct_fix":self.direct_fix,
            "stable_ids":list(self.stable_ids),
            "evidence":list(self.evidence),
        }


@dataclass(frozen=True)
class TrainingDatasetAnalysis:
    split_rule_version: str
    split_seed: str
    role_targets: tuple[tuple[str,int],...]
    groups: tuple[TrainingSplitGroup,...]
    findings: tuple[TrainingDatasetFinding,...]
    limitations: tuple[str,...]
    duplicate_evidence: TrainingDuplicateEvidence|None=None

    def role_for(self,stable_id: str) -> str:
        return next((
            group.recommended_role for group in self.groups
            if stable_id in group.member_stable_ids
        ),"training")

    def group_for(self,stable_id: str) -> TrainingSplitGroup|None:
        return next((
            group for group in self.groups if stable_id in group.member_stable_ids
        ),None)

    def to_dict(self) -> dict[str,object]:
        return {
            "split_rule":{
                "version":self.split_rule_version,
                "seed":self.split_seed,
                "role_targets_percent":{
                    role:percent for role,percent in self.role_targets
                },
            },
            "groups":[group.to_dict() for group in self.groups],
            "findings":[finding.to_dict() for finding in self.findings],
            "limitations":list(self.limitations),
            "duplicate_evidence":(
                self.duplicate_evidence.to_dict() if self.duplicate_evidence is not None else None
            ),
        }


def _assign_roles(grouped: Mapping[str,list[object]],seed: str) -> dict[str,str]:
    count=len(grouped); total=sum(len(members) for members in grouped.values())
    if count<5:
        return {key:"training" for key in grouped}
    validation=max(1,round(total*.10))
    holdout=0 if count<10 else max(1,round(total*.10))
    training=max(1,total-validation-holdout)
    targets={"training":training,"validation":validation,"holdout":holdout}
    current={"training":0,"validation":0,"holdout":0}
    allowed=("training","validation") if not holdout else (
        "training","validation","holdout",
    )
    ordered=sorted(
        grouped,key=lambda key:(
            -len(grouped[key]),_stable_hash({"seed":seed,"group":key}),key,
        ),
    )
    result={}
    for key in ordered:
        role=max(
            allowed,key=lambda value:(targets[value]-current[value],
                                      -allowed.index(value)),
        )
        result[key]=role; current[role]+=len(grouped[key])
    return result


def _build_findings(
    candidates: tuple[object,...],groups: tuple[TrainingSplitGroup,...],*,
    goal: Mapping[str,object],seed: str,scope_fingerprint: str,
    rule_version: str=SPLIT_RULE_VERSION,
) -> tuple[tuple[TrainingDatasetFinding,...],bool]:
    included=tuple(
        candidate for candidate in candidates
        if getattr(candidate,"decision","")=="include"
    )
    by_id={str(candidate.stable_id):candidate for candidate in included}
    active_groups=[]
    for group in groups:
        members=tuple(
            by_id[stable_id] for stable_id in group.member_stable_ids
            if stable_id in by_id
        )
        if members:
            active_groups.append((group,members))
    grouped_ids={
        stable_id for group,_members in active_groups
        for stable_id in group.member_stable_ids if stable_id in by_id
    }
    for candidate in included:
        if candidate.stable_id not in grouped_ids:
            active_groups.append((TrainingSplitGroup(
                "override."+_stable_hash(candidate.stable_id)[:20],"independent",
                (candidate.stable_id,),candidate.role,
            ),(candidate,)))
    denominator=len(included); findings=[]
    counts={"training":0,"validation":0,"holdout":0}
    for candidate in included:
        counts[str(candidate.role)]=counts.get(str(candidate.role),0)+1
    exact_groups=tuple(
        (group,members) for group,members in active_groups
        if group.evidence_kind=="byte_identical" and len(members)>1
    )
    video_groups=tuple((group,members) for group,members in active_groups
                       if group.evidence_kind=="source_video")
    standalone_count=len(active_groups)-len(exact_groups)-len(video_groups)
    findings.append(TrainingDatasetFinding(
        "split_role_distribution","Split integrity","observed",
        "Current grouped role distribution",
        (
            f"{len(active_groups):,} active split group(s)—{standalone_count:,} "
            f"standalone, {len(exact_groups):,} byte-identical and {len(video_groups):,} source-video—produce "
            f"{counts['training']:,} training, {counts['validation']:,} validation, "
            f"and {counts['holdout']:,} holdout image(s). Recommendations use the "
            "recorded rule and membership-derived seed, never display order."
        ),denominator,denominator,
        "Use per-image overrides only with a recorded reason.",
        tuple(candidate.stable_id for candidate in included),(
            f"rule={rule_version}",f"seed={seed}",
            f"scope_fingerprint={scope_fingerprint}",
        ),
    ))
    if exact_groups:
        exact_ids=tuple(
            item.stable_id for _group,members in exact_groups for item in members
        )
        findings.append(TrainingDatasetFinding(
            "byte_identical_split_groups","Split integrity","caution",
            "Byte-identical images are grouped for split integrity",
            f"{len(exact_ids):,} included image(s) form {len(exact_groups):,} active exact group(s). Separating a group can leak the same source into evaluation.",
            len(exact_ids),denominator,
            "Keep each exact group together, or record why a deliberate override is needed.",
            exact_ids,tuple(group.group_id for group,_members in exact_groups),
        ))
    if video_groups:
        video_ids=tuple(item.stable_id for _group,members in video_groups for item in members)
        findings.append(TrainingDatasetFinding(
            "source_video_split_groups","Split integrity","caution",
            "Frames from a shared video stay in one split group",
            "Canonical source-video hashes and identical frame bytes are joined transitively. Different frames from one clip do not establish independent evaluation evidence.",
            len(video_ids),denominator,
            "Keep these groups together; use other source videos for independent evaluation.",
            video_ids,tuple(group.group_id for group,_members in video_groups),
        ))
    if len(active_groups)<5 and denominator:
        findings.append(TrainingDatasetFinding(
            "independent_split_groups_limited","Split integrity","recommendation",
            "Too few split groups for a separate evaluation role",
            f"Only {len(active_groups):,} split group(s) are active. The conservative recommendation stays training-only instead of claiming independent evaluation evidence.",
            len(active_groups),len(active_groups),
            "Add distinct goal-relevant images or deliberately assign an evaluation role.",
        ))
    split_ids=[]; split_evidence=[]
    for group,members in (*exact_groups,*video_groups):
        roles={str(item.role) for item in members}
        if len(roles)>1:
            split_ids.extend(item.stable_id for item in members)
            split_evidence.append(f"{group.group_id}={','.join(sorted(roles))}")
    if split_ids:
        findings.append(TrainingDatasetFinding(
            "split_group_override","Split integrity","caution",
            "A grouped source is deliberately split across roles",
            "Recorded per-image overrides put one related-source group into multiple included roles, which can leak shared source evidence into evaluation.",
            len(set(split_ids)),denominator,
            "Use one role for the whole group, or keep the recorded reasons and review the leakage consequence.",
            tuple(sorted(set(split_ids))),tuple(split_evidence),
        ))

    captions: dict[str,list[object]]={}
    for candidate in included:
        captions.setdefault(str(getattr(candidate,"caption_sha256","") or ""),[]).append(candidate)
    repeated=tuple(
        item for members in captions.values() if len(members)>1 for item in members
    )
    findings.append(TrainingDatasetFinding(
        "caption_projection_diversity","Diversity","observed",
        "Exact caption projection distribution",
        f"{len(captions):,} distinct exact caption(s) are projected across {denominator:,} included image(s). This measures caption variation, not visual diversity.",
        len(captions),denominator,
        "Review repeated projections alongside the images before packaging.",
        tuple(item.stable_id for item in repeated),
    ))
    if repeated:
        findings.append(TrainingDatasetFinding(
            "repeated_caption_projection","Balance","caution",
            "Some included images have identical projected captions",
            "Repeated exact captions may be intentional, but can hide under-described variation or repeated weighting.",
            len(repeated),denominator,
            "Compare the affected images and confirm the repeated projection is deliberate.",
            tuple(item.stable_id for item in repeated),
        ))

    goal_words=set(_words(str(goal.get("text") or "")))
    token_counts: dict[str,int]={}; token_truncated=False
    for candidate in included:
        caption=str(getattr(candidate,"caption","") or "")
        bounded,was_truncated=_bounded_words(caption)
        tokens=set(bounded)-goal_words
        token_truncated=token_truncated or was_truncated
        for word in tokens:
            if word in token_counts:
                token_counts[word]+=1
            elif len(token_counts)<MAX_TRACKED_CAPTION_TERMS:
                token_counts[word]=1
            else:
                token_truncated=True
    correlated=sorted((
        (word,count) for word,count in token_counts.items()
        if (denominator>=3 and count==denominator)
        or (denominator>=5 and count/denominator>=.8)
    ),key=lambda item:(-item[1],item[0]))[:12]
    correlated_words={word for word,_count in correlated}
    correlated_ids={word:[] for word in correlated_words}
    for candidate in included:
        tokens=set(_words(str(getattr(candidate,"caption","") or "")))
        for word in correlated_words.intersection(tokens):
            correlated_ids[word].append(candidate.stable_id)
    for word,count in correlated:
        findings.append(TrainingDatasetFinding(
            f"caption_term_correlation.{_stable_hash(word)[:12]}",
            "Correlation","caution",
            f"Caption term “{word}” appears in most included examples",
            f"The term occurs in {count:,} of {denominator:,} exact captions. This is measured co-occurrence, not proof of an unwanted or causal correlation.",
            count,denominator,
            "Check whether the term is essential to the goal or needs more contrasting examples.",
            tuple(correlated_ids[word]),(word,),
        ))

    aspect={"portrait":0,"near_square":0,"landscape":0}
    resolution={"under_512":0,"512_to_767":0,"768_to_1023":0,"1024_plus":0}
    for candidate in included:
        evidence=getattr(candidate,"evidence",None)
        if evidence is None:
            continue
        ratio=evidence.width/max(1,evidence.height)
        aspect["portrait" if ratio<.8 else "landscape" if ratio>1.25 else "near_square"]+=1
        edge=min(evidence.width,evidence.height)
        resolution[
            "under_512" if edge<512 else "512_to_767" if edge<768
            else "768_to_1023" if edge<1024 else "1024_plus"
        ]+=1
    findings.append(TrainingDatasetFinding(
        "technical_distribution","Balance","observed",
        "Resolution and aspect distribution",
        "The source preflight is summarized by measured aspect and shorter-edge buckets; no universal quality score is inferred.",
        denominator,denominator,"Review target-specific crop and bucket consequences.",
        evidence=tuple(f"{key}={count}" for key,count in (*aspect.items(),*resolution.items())),
    ))
    for code,category,summary,reason,direct_fix in (
        ("literal_goal_coverage_unconfirmed","Balance","Some captions have no literal term from the stated goal","Aliases or visual evidence may still cover the goal, so this is a review set rather than an automatic exclusion.","Review the affected images and exact captions against the stated goal."),
        ("possible_filename_leakage","Leakage","Filename terms overlap exact captions","The overlap may be a valid confirmed fact, but it names a possible source correlation that needs provenance review.","Inspect the affected provenance before packaging."),
    ):
        affected=tuple(
            candidate for candidate in included
            if any(issue.code==code for issue in candidate.issues)
        )
        if affected:
            findings.append(TrainingDatasetFinding(
                "literal_goal_coverage_distribution" if code.startswith("literal") else "filename_leakage_distribution",
                category,"caution",summary,reason,len(affected),denominator,direct_fix,
                tuple(item.stable_id for item in affected),
            ))
    return tuple(findings),token_truncated


def analyze_training_candidates(
    candidates: Iterable[object],*,goal: Mapping[str,object],
    scope_fingerprint: str,profile_provenance: Mapping[str,object],
) -> TrainingDatasetAnalysis:
    values=tuple(candidates)
    recommended=tuple(
        candidate for candidate in values
        if getattr(candidate,"decision","")=="include"
        and not getattr(candidate,"blocking",())
    )
    has_video=any(getattr(candidate,"derived_frame",None) is not None for candidate in recommended)
    rule_version=VIDEO_SPLIT_RULE_VERSION if has_video else SPLIT_RULE_VERSION
    seed=_stable_hash({
        "rule":rule_version,
        "scope_members":sorted(str(getattr(candidate,"stable_id","")) for candidate in values),
        "goal":dict(goal),"profile":dict(profile_provenance),
    })[:24]
    grouped: dict[str,list[object]]={}
    if has_video:
        by_id: dict[str,list[object]]={}
        for candidate in recommended:
            by_id.setdefault(candidate.stable_id,[]).append(candidate)
        for component in source_video_components(recommended):
            grouped["component:"+_stable_hash(sorted(component))]=[
                candidate for stable_id in sorted(component) for candidate in by_id[stable_id]]
    else:
        for candidate in recommended:
            exact=str(getattr(candidate,"exact_duplicate_group","") or "")
            grouped.setdefault(
                exact or f"independent:{getattr(candidate,'stable_id','')}",[],
            ).append(candidate)
    roles=_assign_roles(grouped,seed)
    groups=[]
    for key,members in sorted(grouped.items()):
        stable_ids=tuple(sorted(str(item.stable_id) for item in members))
        clips=tuple(sorted({item.derived_frame.source_sha256 for item in members
                            if getattr(item,"derived_frame",None) is not None}))
        exact=key.startswith("exact:") or (not clips and len(members)>1)
        groups.append(TrainingSplitGroup(
            "split."+_stable_hash(stable_ids)[:20],
            "source_video" if clips else "byte_identical" if exact else "independent",
            stable_ids,roles[key],
            (tuple("source_sha256="+value for value in clips) if clips else
             ("sha256="+str(members[0].evidence.sha256),) if exact else ()),
        ))
    groups=tuple(groups)
    role_by_id={
        stable_id:group.recommended_role for group in groups
        for stable_id in group.member_stable_ids
    }
    projected=tuple(
        candidate
        if role_by_id.get(candidate.stable_id,candidate.role)==candidate.role
        else replace(candidate,role=role_by_id[candidate.stable_id])
        for candidate in values
    )
    findings,truncated=_build_findings(
        projected,groups,goal=goal,seed=seed,scope_fingerprint=scope_fingerprint,
        rule_version=rule_version,
    )
    limitations=[
        "Canonical frame references bind source-video hashes to current frame bytes; source clips are not reopened by this planner. Other derivative, crop, recompression, burst and series relationships remain unchecked.",
        "Perceptual similarity was not run by this planner; use Duplicate Review for separate candidate evidence.",
        "Caption terms, aspect, and resolution are bounded distribution evidence, not visual-diversity or causal-correlation scores.",
    ]
    if truncated:
        limitations.append(
            f"Caption-term analysis was bounded to {MAX_CAPTION_TOKENS_PER_IMAGE:,} tokens per image and {MAX_TRACKED_CAPTION_TERMS:,} distinct terms; omitted terms were not reported as checked."
        )
    return TrainingDatasetAnalysis(
        rule_version,seed,ROLE_TARGETS,groups,findings,tuple(limitations),
    )


def with_effective_split_findings(
    analysis: TrainingDatasetAnalysis,candidates: Iterable[object],*,
    goal: Mapping[str,object],scope_fingerprint: str,
) -> TrainingDatasetAnalysis:
    values=tuple(candidates)
    findings,truncated=_build_findings(
        values,analysis.groups,goal=goal,seed=analysis.split_seed,
        scope_fingerprint=scope_fingerprint,
        rule_version=analysis.split_rule_version,
    )
    limitations=tuple(
        item for item in analysis.limitations
        if not item.startswith("Caption-term analysis was bounded")
    )
    if truncated:
        limitations=(*limitations,
            f"Caption-term analysis was bounded to {MAX_CAPTION_TOKENS_PER_IMAGE:,} tokens per image and {MAX_TRACKED_CAPTION_TERMS:,} distinct terms; omitted terms were not reported as checked."
        )
    return with_duplicate_findings(
        replace(analysis,findings=findings,limitations=limitations),values,
    )


def with_duplicate_findings(analysis,candidates):
    """Reevaluate current included pairs without changing any recommendation."""
    snapshot=analysis.duplicate_evidence
    if snapshot is None:return analysis
    included={item.stable_id:item for item in candidates if item.decision=="include"}
    findings=[item for item in analysis.findings if not item.code.startswith("duplicate_evidence.")]
    active=[pair for pair in snapshot.relations
            if pair.first_stable_id in included and pair.second_stable_id in included]
    for kind,label in (("pixel_exact","Decoded pixels match"),("similar","Visually similar candidates")):
        pairs=[pair for pair in active if pair.kind==kind and not pair.reviewed_distinct]
        if not pairs:continue
        ids=tuple(sorted({value for pair in pairs for value in (pair.first_stable_id,pair.second_stable_id)}))
        cross=sum(included[pair.first_stable_id].role!=included[pair.second_stable_id].role for pair in pairs)
        findings.append(TrainingDatasetFinding(
            "duplicate_evidence."+kind,"Leakage","caution",label,
            f"{len(pairs):,} cached relation(s) affect included images; {cross:,} cross current training/evaluation roles. "
            "These observations do not change inclusion or split recommendations.",
            len(ids),len(included),"Compare Similar Images, then keep or override each candidate with a reason.",ids,
        ))
    limitations=tuple(item for item in analysis.limitations
                      if not item.startswith("Perceptual similarity was not run"))
    cached_limit="Cached similarity covers only matching current fingerprints and the displayed range/bounds; missing evidence and unsurfaced pairs remain unchecked."
    if cached_limit not in limitations:limitations=(*limitations,cached_limit)
    return replace(analysis,findings=tuple(findings),limitations=limitations)
