from __future__ import annotations

from dataclasses import dataclass, field
import copy
import hashlib
from pathlib import Path
from typing import Callable, Iterable

from .annotation import GenerationRecord,ImageAnnotation
from .automation_templates import has_automation_placeholder
from .confirmed_tag_identity import ConfirmedTagIdentityService
from .image_record import ImageRecord
from .image_knowledge_completeness import ImageKnowledgeCompletenessService
from .image_fact_decisions import (
    ImageFindingDecisionService,image_tag_identity,
    unavailable_confirmed_tag_identities,
)
from .source_inspection import ImageEvidenceInspector
from .ai_interrogation import merge_plain_language_in_source_order,split_plain_language,_preparation_record,_preparation_write_guard
from .caption_import import looks_like_metadata_prose_fragment,metadata_sources_only
from .tag_collection import TagCollection,normalize_tag


@dataclass
class ImagePreparationItem:
    image: Path
    missing_record: bool = False
    has_txt: bool = False
    has_tags: bool = False
    has_prose: bool = False
    metadata_tags: list[str] = field(default_factory=list)
    metadata_prose: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    generation_records: list[GenerationRecord] = field(default_factory=list)
    standard_metadata: dict[str, object] = field(default_factory=dict)
    source_fingerprint: str = ""
    error: str = ""

    @property
    def can_prepare_from_txt(self) -> bool:
        return self.missing_record and self.has_txt

    @property
    def metadata_can_fill_gap(self) -> bool:
        return (
            (not self.has_tags and bool(self.metadata_tags))
            or (not self.has_prose and bool(self.metadata_prose))
        )


@dataclass
class CandidateCacheUpdate:
    changed: bool = False
    applied_rules: list[str] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)


class BatchPreparationService:
    """Assess and safely prepare image records without trusting every clue."""

    def __init__(self, knowledge=None) -> None:
        self.knowledge=knowledge

    @staticmethod
    def source_fingerprint(image: str | Path) -> str:
        """Fingerprint inputs used by inward evidence inspection."""
        image=Path(image)
        digest=hashlib.sha256()
        for path,label,content_hash in (
            (image,"image",False),
            (image.with_suffix(".txt"),"caption",True),
            (image.with_suffix(".tags.json"),"sidecar",True),
        ):
            digest.update(f"{label}:{path}:".encode("utf-8"))
            try:
                stat=path.stat()
            except OSError:
                digest.update(b"missing")
                continue
            digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode("ascii"))
            if content_hash:
                try:
                    digest.update(hashlib.sha256(path.read_bytes()).digest())
                except OSError:
                    digest.update(b"unavailable")
        return digest.hexdigest()

    def assess(self, image: str | Path, *, include_metadata: bool = True) -> ImagePreparationItem:
        path=Path(image)
        try:
            record=ImageRecord(path)
            report=ImageEvidenceInspector(knowledge=self.knowledge).inspect(path,include_metadata=include_metadata)
            metadata=next(
                (source for source in report.sources if source.name == "Image metadata"),
                None,
            ) if report else None
            confirmed_tags={tag.casefold() for tag in record.annotation.all_structured_tags()}
            confirmed_prose={text.casefold() for text in record.annotation.plain_language}
            metadata_tags=[] if metadata is None else [
                tag for tag in metadata.tags if tag.casefold() not in confirmed_tags
            ]
            metadata_prose=[] if metadata is None else [
                text for text in metadata.plain_language
                if text.casefold() not in confirmed_prose
            ]
            evidence=[
                {
                    "name": source.name,
                    "tags": list(source.tags),
                    "plain_language": list(source.plain_language),
                    "ambiguous_fragments": list(source.ambiguous_fragments),
                    "type_issues": list(source.type_issues),
                    "raw_text": source.raw_text,
                    "detected_format": source.detected_format,
                    "app_owned_projection": source.app_owned_projection,
                    "generation_reference": list(source.details),
                    "generation_records": [item.to_dict() for item in source.generation_records],
                    "standard_metadata": dict(source.standard_metadata),
                    "tag_fields": copy.deepcopy(source.tag_fields),
                    "description_fields": copy.deepcopy(source.description_fields),
                    "raw_generation_fields": copy.deepcopy(source.raw_generation_fields),
                    "error": source.error,
                }
                for source in (report.sources if report else [])
                if source.name != "JSON sidecar" and (source.available or source.error)
            ]
            return ImagePreparationItem(
                image=path,
                missing_record=not record.sidecar_path.exists(),
                has_txt=record.caption_path.exists(),
                has_tags=bool(record.annotation.all_structured_tags()),
                has_prose=bool(record.annotation.plain_language),
                metadata_tags=metadata_tags,
                metadata_prose=metadata_prose,
                evidence=evidence,
                generation_records=list(metadata.generation_records) if metadata else [],
                standard_metadata=dict(metadata.standard_metadata) if metadata else {},
                source_fingerprint=self.source_fingerprint(path),
            )
        except Exception as exc:
            return ImagePreparationItem(
                image=path,
                has_txt=path.with_suffix(".txt").exists(),
                error=str(exc),
            )

    def assess_many(
        self,
        images: Iterable[str | Path],
        progress: Callable[[int, int, str], None] | None = None,
    ) -> list[ImagePreparationItem]:
        paths=[Path(image) for image in images]
        items=[]
        for index,image in enumerate(paths,1):
            items.append(self.assess(image))
            if progress:
                progress(index,len(paths),image.name)
        return items

    def needs_tag_analysis(self, image: str | Path) -> bool:
        """Return whether confirmed tags are missing or contextually weak."""
        report=ImageKnowledgeCompletenessService(knowledge=self.knowledge).assess(
            image,include_metadata=False,
        )
        return bool(
            {item.code for item in report.issues}
            & {"missing_tags","thin_tag_coverage"}
        )

    def needs_description_analysis(self,image: str | Path) -> bool:
        report=ImageKnowledgeCompletenessService(knowledge=self.knowledge).assess(image,include_metadata=False)
        return bool({item.code for item in report.issues}&{"missing_description","thin_description"})

    @staticmethod
    def _stable_evidence(values):
        """Ignore regenerated record IDs while comparing otherwise exact source evidence."""
        stable=copy.deepcopy(values)
        for source in stable if isinstance(stable,list) else []:
            if not isinstance(source,dict): continue
            for record in source.get("generation_records",[]) if isinstance(source.get("generation_records"),list) else []:
                if isinstance(record,dict): record.pop("id",None)
        return stable

    @staticmethod
    def update_candidate_findings(
        record,evidence,knowledge=None,automation_engine=None,*,replace_sources=(),
        apply_rules: bool=True,
    ) -> CandidateCacheUpdate:
        """Refresh candidates; portable observations may opt out of rule policy."""
        from .automation_repository import active_automation_engine

        source=record.annotation.source
        original=copy.deepcopy(source.get("candidate_findings",[]))
        original_automation=copy.deepcopy(source.get("candidate_automation",{}))
        # "Automatic rule" was a synthetic source used by the former
        # destructive candidate-merge path. Rebuild those derived findings
        # from real evidence instead of carrying them forward.
        replaced={str(value) for value in replace_sources}|{"Automatic rule"}
        retained=[]; prior={}; invalid_historical=[]
        for raw in original if isinstance(original,list) else []:
            if not isinstance(raw,dict):
                continue
            if raw.get("kind") not in {"tag","description","ambiguous"}:
                invalid_historical.append(copy.deepcopy(raw))
                continue
            raw_value=str(raw.get("value") or "").strip(); kind=str(raw["kind"])
            values=(split_plain_language(raw_value) if kind=="description" else [raw_value])
            for value in values:
                if not value: continue
                item=copy.deepcopy(raw); item["value"]=value
                item["sources"]=[str(name) for name in item.get("sources",[]) if str(name).strip() and str(name) not in replaced]
                if kind=="tag" and has_automation_placeholder(value):
                    kind="ambiguous"; item["kind"]=kind
                    item["reason"]="The source contains an unresolved Automation template, not a concrete SDXL Tag."
                elif (kind=="tag" and metadata_sources_only(item["sources"])
                        and looks_like_metadata_prose_fragment(value)):
                    kind="ambiguous"; item["kind"]=kind
                    item["reason"]="Image Metadata contained sentence-like or broken-quotation content in a tag field."
                key=(kind,normalize_tag(value) if kind=="tag" else value.casefold())
                item.pop("review_required",None); item.pop("conflicts",None)
                prior[key]=item
                if item["sources"]: retained.append(item)

        incoming={}
        for evidence_source in evidence if isinstance(evidence,list) else []:
            if not isinstance(evidence_source,dict): continue
            name=str(evidence_source.get("name") or "Imported source")
            for kind,field_name in (
                ("tag","tags"),("description","plain_language"),
                ("ambiguous","ambiguous_fragments"),
            ):
                values=evidence_source.get(field_name,[])
                for raw in values if isinstance(values,list) else []:
                    raw_value=str(raw or "").strip()
                    candidates=(split_plain_language(raw_value) if kind=="description" else [raw_value])
                    for value in candidates:
                        candidate_kind=(
                            "ambiguous" if kind=="tag" and (
                                has_automation_placeholder(value)
                                or (
                                    metadata_sources_only([name])
                                    and looks_like_metadata_prose_fragment(value)
                                )
                            ) else kind
                        )
                        key=(candidate_kind,normalize_tag(value) if candidate_kind=="tag" else value.casefold())
                        if not value or not key[1]: continue
                        entry=incoming.setdefault(key,{"kind":candidate_kind,"value":value,"sources":[]})
                        if candidate_kind=="ambiguous" and kind=="tag":
                            entry["reason"]=(
                                "The source contains an unresolved Automation template, not a concrete SDXL Tag."
                                if has_automation_placeholder(value)
                                else "Image Metadata contained sentence-like or broken-quotation content in a tag field."
                            )
                        if name not in entry["sources"]: entry["sources"].append(name)

        combined={}
        for item in [*retained,*incoming.values()]:
            kind=str(item["kind"]); value=str(item["value"])
            key=(kind,normalize_tag(value) if kind=="tag" else value.casefold())
            entry=combined.setdefault(key,copy.deepcopy(prior.get(key,item)))
            entry["kind"]=kind; entry["value"]=value
            entry["sources"]=list(dict.fromkeys([
                *entry.get("sources",[]),*item.get("sources",[]),
            ]))

        confirmed_values=TagCollection(record.annotation.all_structured_tags()).to_list()
        confirmed=TagCollection(confirmed_values).normalized_set()
        identity_entries={}
        confirmed_identity_ids=set()
        if knowledge is not None:
            identity_plan=ConfirmedTagIdentityService(knowledge).plan(
                [
                    *confirmed_values,
                    *(
                        str(item.get("value") or "")
                        for (kind,_key),item in combined.items()
                        if kind=="tag"
                    ),
                ],
                stored_ids=getattr(record.annotation,"tag_identities",{}),
                confirmation=False,
            )
            identity_entries={
                entry.normalized_value:entry for entry in identity_plan.entries
            }
            confirmed_identity_ids={
                entry.stable_id
                for value in confirmed_values
                if (
                    entry:=identity_entries.get(normalize_tag(value))
                ) is not None and entry.stable_id and entry.canonical_display
            }
        rejected={normalize_tag(value) for value in source.get("rejected_tags",[])}
        rejected.update(
            normalize_tag(value) for value in record.annotation.confirmed_absent
        )
        rejected.update(
            normalize_tag(tag) for ai in record.annotation.ai_evidence
            for tag,decision in ai.decisions.items()
            if ai.kind=="sdxl_tags" and decision=="rejected"
        )
        unavailable_identities=unavailable_confirmed_tag_identities(
            record.annotation,knowledge,
        )
        rejected_prose={
            sentence.casefold() for value in source.get("rejected_descriptions",[])
            for sentence in split_plain_language(value)
        }
        confirmed_prose={
            sentence.casefold() for value in record.annotation.plain_language
            for sentence in split_plain_language(value)
        }
        engine=automation_engine or active_automation_engine(knowledge)
        tags=[]; tag_sources={}; evidence_rules=[]; suppressed_evidence=[]
        for (kind,key),item in combined.items():
            if kind!="tag" or key in confirmed|rejected: continue
            if image_tag_identity(item["value"],knowledge) in unavailable_identities:
                continue
            identity=identity_entries.get(key)
            if (
                identity is not None
                and identity.canonical_display
                and identity.stable_id in confirmed_identity_ids
            ):
                continue
            # Legacy comma-separated TXT can route a complete sentence through
            # the tag field. Suppress only an exact prose duplicate here; never
            # pass the sentence through tag automation or match partial words.
            if str(item["value"]).strip().casefold() in confirmed_prose|rejected_prose:
                continue
            display=(
                identity.canonical_display
                if (
                    identity is not None
                    and identity.stable_id
                    and identity.canonical_display
                )
                else item["value"]
            )
            normalized=normalize_tag(display)
            if apply_rules and engine.is_evidence_rejected(display):
                evidence_rules.append(f"Evidence ignore: {display} -> suppressed")
                suppressed_evidence.append({
                    "value":display,
                    "sources":list(item.get("sources",[])),
                })
                continue
            if normalized not in tag_sources: tags.append(display); tag_sources[normalized]=[]
            tag_sources[normalized]=list(dict.fromkeys([*tag_sources[normalized],*item["sources"]]))

        # Imported evidence stays maximally descriptive. Existing merge rules
        # are evaluated only so the eventual training projection remains
        # inspectable; they never replace or hide a candidate Image fact.
        projection_rules=[]
        if apply_rules:
            _projected_all,projection_rules=engine.project_tags(
                [*confirmed_values,*tags],automatic_only=True,
            )
        findings=[]
        for value in tags:
            key=("tag",normalize_tag(value)); item=copy.deepcopy(prior.get(key,{"kind":"tag","value":value}))
            item["kind"]="tag"; item["value"]=value
            item["sources"]=tag_sources.get(key[1],[])
            findings.append(item)
        finding_decisions=ImageFindingDecisionService(
            record.annotation,knowledge=knowledge,
        )
        for (kind,key),item in combined.items():
            if kind!="description" or key in confirmed_prose|rejected_prose: continue
            available_sources=[
                source for source in item.get("sources",[])
                if not finding_decisions.source_was_used_as_edited(
                    "description",item["value"],source,
                )
            ]
            if not item.get("sources",[]):
                available_sources=(
                    [] if finding_decisions.source_was_used_as_edited(
                        "description",item["value"],"Unknown",
                    ) else ["Unknown"]
                )
            if available_sources:
                available=copy.deepcopy(item)
                available["sources"]=available_sources
                findings.append(available)
        for (kind,_key),item in combined.items():
            if kind=="ambiguous":
                item["review_required"]=True
                item.setdefault("reason","The source did not establish whether this is an SDXL Tag or Plain Language Description.")
                findings.append(item)
        findings.extend(invalid_historical)

        conflicts=[]
        conflict_engine=(getattr(engine,"conflicts",None) if apply_rules else None)
        if conflict_engine is not None and tags:
            baseline={engine._conflict_key(match) for match in conflict_engine.evaluate(record)}
            trial=ImageRecord(record.image_path,auto_load=False); trial.loaded=True
            trial.annotation=copy.deepcopy(record.annotation)
            trial.annotation.sdxl_tags=TagCollection([*trial.annotation.sdxl_tags,*tags]).to_list()
            for match in conflict_engine.evaluate(trial):
                if engine._conflict_key(match) in baseline: continue
                conflicts.append({
                    "rule_id":match.rule.id,"message":match.message,
                    "tags":list(match.found_tags),"ambiguous":bool(match.ambiguous),
                    "severity":match.rule.severity,"character_slot":match.character_slot,
                })
        conflict_messages={normalize_tag(tag):[] for item in conflicts for tag in item["tags"]}
        for item in conflicts:
            for tag in item["tags"]:
                conflict_messages[normalize_tag(tag)].append(item["message"])
        for item in findings:
            messages=conflict_messages.get(normalize_tag(item["value"]),[]) if item["kind"]=="tag" else []
            if messages:
                item["review_required"]=True; item["conflicts"]=list(dict.fromkeys(messages))

        automation={
            "applied_rules":list(dict.fromkeys(evidence_rules)),
            "projection_rules":list(projection_rules),
            "conflicts":conflicts,
            "suppressed_evidence":suppressed_evidence,
        }
        source["candidate_findings"]=findings
        source["candidate_automation"]=automation
        changed=(
            BatchPreparationService._stable_evidence(original)!=BatchPreparationService._stable_evidence(findings)
            or original_automation!=automation
        )
        return CandidateCacheUpdate(changed,list(dict.fromkeys(evidence_rules)),conflicts)

    @staticmethod
    def _merge_candidate_findings(record,evidence,knowledge=None,automation_engine=None):
        """Compatibility wrapper for the shared source-candidate cache."""
        return BatchPreparationService.update_candidate_findings(
            record,evidence,knowledge,automation_engine,
        ).changed

    @staticmethod
    def apply(
        item: ImagePreparationItem,
        *,
        prepare_from_txt: bool,
        remember_evidence: bool,
        fill_gaps_from_metadata: bool,
        knowledge=None,
        automation_engine=None,
        mutation_callback: Callable[[Path,dict,dict,bool,str],None] | None=None,
        history_operation=None,
    ) -> bool:
        if item.error:
            return False
        record=_preparation_record(item.image, history_operation) if history_operation is not None else None
        if (
            item.source_fingerprint
            and BatchPreparationService.source_fingerprint(item.image)
            != item.source_fingerprint
        ):
            raise RuntimeError(
                "The image, TXT, or Image Knowledge changed after evidence was "
                "inspected; no stale evidence was saved."
            )
        record=record if record is not None else ImageRecord(item.image)
        before=copy.deepcopy(record.annotation.to_dict())
        sidecar_existed=record.sidecar_path.exists()
        # A TXT file beside a new image is an import source, not permission to
        # promote its contents into authoritative Image Knowledge.
        if item.missing_record and not record.sidecar_path.exists():
            record.annotation=ImageAnnotation(image=record.image_path.name)
            record.imported_from_legacy=False
        changed=False
        if remember_evidence and item.evidence:
            if not isinstance(record.annotation.source,dict):
                record.annotation.source={}
            if BatchPreparationService._stable_evidence(record.annotation.source.get("evidence"))!=BatchPreparationService._stable_evidence(item.evidence):
                record.annotation.source["evidence"]=item.evidence
                record.modified=True; changed=True
            if BatchPreparationService._merge_candidate_findings(
                record,item.evidence,knowledge,automation_engine,
            ):
                record.modified=True; changed=True
        if remember_evidence and item.generation_records:
            for generation_record in item.generation_records:
                if record.annotation.add_generation_record(generation_record):
                    record.modified = True
                    changed = True
        if remember_evidence and item.standard_metadata:
            standard=dict(item.standard_metadata)
            if record.annotation.standard_metadata!=standard:
                record.annotation.standard_metadata=standard
                record.modified=True; changed=True
        if fill_gaps_from_metadata:
            if not item.has_tags and item.metadata_tags:
                known_tags=(
                    [
                        known.display
                        for value in item.metadata_tags
                        if (known:=knowledge.find(value)) is not None
                    ]
                    if knowledge is not None else []
                )
                if known_tags:
                    changed=bool(
                        record.confirm_tags(known_tags,knowledge=knowledge)
                    ) or changed
            if not item.has_prose and item.metadata_prose:
                merged=merge_plain_language_in_source_order(
                    record.annotation.plain_language,item.metadata_prose,(item.metadata_prose,),
                )
                if merged!=record.annotation.plain_language:
                    record.annotation.plain_language=merged
                    record.modified=True; changed=True
        if changed:
            ConfirmedTagIdentityService.prune_annotation(record.annotation)
            if mutation_callback is not None and history_operation is None:
                mutation_callback(
                    item.image,before,copy.deepcopy(record.annotation.to_dict()),
                    sidecar_existed,"Refresh inward TXT and Metadata evidence",
                )
            with _preparation_write_guard(record.history_context, item.image):
                record.save_sidecar(force=True)
            if fill_gaps_from_metadata:
                record.export_caption(knowledge=knowledge,force=True)
        return changed
