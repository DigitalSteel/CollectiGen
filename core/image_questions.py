from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Callable, Iterable, Mapping

from .folder_audit import RATINGS, STYLE_ALIASES, folder_context
from .character_database import CharacterDatabase
from .automation_repository import active_automation_engine
from .image_fact_decisions import (
    ImageFindingDecisionService,
    tag_addition_decision_issues,
    unavailable_confirmed_tag_identities,
)
from .image_record import ImageRecord
from .source_inspection import ImageEvidenceInspector
from .tag_collection import TagCollection, normalize_tag
from .paths import (
    CONSUMER_POLICIES_PATH, DEFAULTS_DIR, RELATIONSHIPS_PATH, USER_DATA_DIR,
)
from .related_tag_suggestions import RelatedTagSuggestionService
from .review_queue import ReviewTaskDatabase
from .typed_boundary import typed_tag_sources


@dataclass(frozen=True)
class ImageQuestion:
    code: str
    title: str
    question: str
    action: str
    values: list[str] = field(default_factory=list)
    source: str = ""
    confidence: float = 1.0
    context: dict = field(default_factory=dict)

    def details(self) -> dict:
        details = {
            "code": self.code,
            "question": self.question,
            "action": self.action,
            "values": list(self.values),
            "source": self.source,
            "confidence": self.confidence,
        }
        if self.context:
            details["derived_context"] = dict(self.context)
        return details

    @classmethod
    def from_details(cls, title: str, value: dict) -> ImageQuestion:
        return cls(
            str(value.get("code") or "question"),str(title or "Review image information"),
            str(value.get("question") or "Review this image information."),
            str(value.get("action") or ""),[str(item) for item in value.get("values",[])],
            str(value.get("source") or ""),float(value.get("confidence") or 0),
            dict(value.get("derived_context") or {})
            if isinstance(value.get("derived_context"),dict) else {},
        )


class CharacterQuestionIndex:
    """Precomputed identity/series/trait lookup for library-scale image scans."""

    def __init__(self,characters: CharacterDatabase) -> None:
        self.characters=characters; self.identity_exact={}; self.identity_phrases={}; self.series_exact={}; self.series_phrases={}
        self.association_exact={}; self.association_phrases={}; self.profiles={}; self.phrase_lengths=set()
        for character in characters.characters.values():
            for display in [character.name,*character.aliases]:
                key=normalize_tag(display)
                if key: self.identity_exact.setdefault(key,[]).append((character,display)); self._add_phrase(self.identity_phrases,key,(character,display))
            series=characters.series.get(
                characters.character_source_id("character", character.id)
            )
            if series:
                for display in [series.name,*series.aliases]:
                    key=normalize_tag(display)
                    if key: self.series_exact.setdefault(key,set()).add(character.id); self._add_phrase(self.series_phrases,key,character.id)
            strong=[]
            for item in character.tag_associations:
                if not isinstance(item,dict): continue
                try:
                    samples=int(item.get("sample_count") or 0); rate=float(item.get("rate") or 0); lift=float(item.get("lift") or 0); lower=float(item.get("confidence") or 0)
                except (TypeError,ValueError): continue
                if samples>=5 and rate>=.75 and lift>=3 and lower>=.45: strong.append(item)
            strong=strong[:12]
            if strong:
                total=sum(max(.1,float(item.get("confidence") or 0))*math.log2(max(2,float(item.get("lift") or 2))) for item in strong)
                self.profiles[character.id]=(character,strong,total)
                for item in strong:
                    key=normalize_tag(item.get("tag",""))
                    if key: self.association_exact.setdefault(key,[]).append((character.id,item)); self._add_phrase(self.association_phrases,key,(character.id,item))

    @staticmethod
    def _tokens(value: str) -> tuple[str,...]:
        return tuple(re.findall(r"[a-z0-9]+",normalize_tag(value)))

    def _add_phrase(self,index: dict,key: str,value) -> None:
        tokens=self._tokens(key)
        if not tokens or len("".join(tokens))<4: return
        index.setdefault(tokens,[]).append(value); self.phrase_lengths.add(len(tokens))

    def _prose_phrases(self,plain_language: Iterable[str]) -> set[tuple[str,...]]:
        tokens=self._tokens(" ".join(str(value) for value in plain_language if str(value).strip()))
        return {tokens[start:start+length] for length in self.phrase_lengths for start in range(0,len(tokens)-length+1)}

    def _series_evidence(self,character,structured_keys: set[str],phrases: set[tuple[str,...]]) -> bool:
        return character.id in {character_id for key in structured_keys for character_id in self.series_exact.get(key,())} or any(character.id in self.series_phrases.get(phrase,()) for phrase in phrases)

    def _disambiguate(self,candidates,structured_keys,phrases):
        if len(candidates)<=1: return candidates
        matched=[item for item in candidates if self._series_evidence(item[0],structured_keys,phrases)]
        return matched if len(matched)==1 else []

    def questions(self,structured_keys: set[str],assigned_ids: set[str],plain_language: Iterable[str]=()) -> list[ImageQuestion]:
        phrases=self._prose_phrases(plain_language)
        direct_by_id={}
        for key in structured_keys:
            for character,display in self.identity_exact.get(key,()):
                if character.id not in assigned_ids:
                    source_id = self.characters.character_source_id(
                        "character", character.id
                    )
                    direct_by_id.setdefault(
                        character.id,
                        (character, display, self.characters.series.get(source_id)),
                    )
        direct=self._disambiguate(list(direct_by_id.values()),structured_keys,phrases)
        if direct:
            result=[]; single_subject=bool(structured_keys.intersection({"solo","1girl","1boy","1other"})) and not structured_keys.intersection({"2girls","2boys","multiple characters","group"})
            for character,matched,series in direct:
                replace_id=next(iter(assigned_ids)) if len(assigned_ids)==1 and single_subject else ""; old=self.characters.characters.get(replace_id) if replace_id else None
                result.append(ImageQuestion(
                    f"character_identity_{'mismatch' if replace_id else 'tag'}_{character.id}",f"Review character: {character.name}" if replace_id else f"Assign character: {character.name}",
                    (f'The image is assigned to {old.name if old else "another character"}, but contains the exact known identity tag “{matched}”. Replace that assignment with {character.name}?' if replace_id else f'The image contains the known identity tag “{matched}”, but its structured character assignment is missing. Assign {character.name}?'),
                    "replace_character" if replace_id else "assign_character",[character.id,replace_id,character.name,series.name if series else ""] if replace_id else [character.id,character.name,series.name if series else ""],
                    source="known character identity tag"+(" with series evidence" if self._series_evidence(character,structured_keys,phrases) else ""),confidence=.99,
                ))
            return result
        prose_by_id={}
        for phrase in phrases:
            for character,display in self.identity_phrases.get(phrase,()):
                if character.id not in assigned_ids:
                    source_id = self.characters.character_source_id(
                        "character", character.id
                    )
                    prose_by_id.setdefault(
                        character.id,
                        (character, display, self.characters.series.get(source_id)),
                    )
        prose_direct=self._disambiguate(list(prose_by_id.values()),structured_keys,phrases)
        if prose_direct: return [ImageQuestion(
            f"character_identity_description_{character.id}",f"Assign character: {character.name}",f'The written description names “{matched}”. Assign the known character {character.name}?',
            "assign_character",[character.id,character.name,series.name if series else ""],source="plain-language character mention"+(" with series evidence" if self._series_evidence(character,structured_keys,phrases) else ""),confidence=.96,
        ) for character,matched,series in prose_direct]
        if assigned_ids: return []
        matched_by_id={}; prose_matches_by_id={}
        for key in structured_keys:
            for character_id,item in self.association_exact.get(key,()): matched_by_id.setdefault(character_id,{})[normalize_tag(item.get("tag",""))]=item
        for phrase in phrases:
            for character_id,item in self.association_phrases.get(phrase,()):
                key=normalize_tag(item.get("tag","")); matched_by_id.setdefault(character_id,{})[key]=item; prose_matches_by_id.setdefault(character_id,set()).add(key)
        ranked=[]
        for character_id,values in matched_by_id.items():
            if len(values)<3 or character_id not in self.profiles: continue
            character,strong,total=self.profiles[character_id]; matched=list(values.values()); prose_matches=sorted(prose_matches_by_id.get(character_id,set()))
            found=sum(max(.1,float(item.get("confidence") or 0))*math.log2(max(2,float(item.get("lift") or 2))) for item in matched); coverage=found/max(.01,total)
            if coverage<.55: continue
            confidence=min(.90 if prose_matches else .94,.68+.28*coverage); ranked.append((confidence,coverage,character,matched,prose_matches))
        ranked.sort(key=lambda value:(value[0],value[1]),reverse=True)
        if not ranked or (len(ranked)>1 and ranked[0][0]-ranked[1][0]<.08): return []
        confidence,_,character,matched,prose_matches=ranked[0]
        series=self.characters.series.get(
            self.characters.character_source_id("character", character.id)
        )
        evidence=[str(item.get("tag")) for item in matched[:8]]
        return [ImageQuestion(
            f"character_trait_match_{character.id}",f"Possible character: {character.name}",f"Several distinctive tags match the learned tendencies for {character.name}. Does this image contain that character?",
            "assign_character",[character.id,character.name,series.name if series else ""],source=("structured tags and plain-language traits: " if prose_matches else "learned character tag tendencies: ")+", ".join(evidence),confidence=confidence,
        )]


class ImageQuestionService:
    """Create and apply explainable questions about missing per-image data."""

    CACHE_VERSION = 5

    def __init__(self, dataset_root: Path, *, inspect_metadata: bool = True, knowledge=None,
                 cache_path: Path | None = None,library_id: str="",
                 external_evidence: bool = True,related_service=None,
                 characters: CharacterDatabase | None = None) -> None:
        self.dataset_root = Path(dataset_root)
        self.library_id=str(library_id or "")
        self.inspect_metadata = inspect_metadata
        self.external_evidence=bool(external_evidence)
        self.knowledge = knowledge
        self.inspector = ImageEvidenceInspector(knowledge=knowledge)
        self.characters = characters or CharacterDatabase(USER_DATA_DIR)
        self._character_questions=CharacterQuestionIndex(self.characters)
        self._related_service=related_service
        self.cache_path=Path(cache_path) if cache_path else USER_DATA_DIR/"image_question_scan_cache.json"
        self._knowledge_fingerprint=self._knowledge_signature()

    @staticmethod
    def _file_signature(path: Path, *, content: bool = False) -> str:
        try:
            stat=path.stat()
            base=f"{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}"
            if content and stat.st_size<=8*1024*1024:
                base+=":"+hashlib.sha256(path.read_bytes()).hexdigest()
            return base
        except OSError:
            return "missing"

    def _knowledge_signature(self) -> str:
        paths=[
            USER_DATA_DIR/"characters.json",USER_DATA_DIR/"series.json",
            USER_DATA_DIR/"outfits.json",DEFAULTS_DIR/"relationships.json",
            DEFAULTS_DIR/"consumer_policies.json",RELATIONSHIPS_PATH,
            CONSUMER_POLICIES_PATH,
        ]
        knowledge_path=getattr(self.knowledge,"path",None)
        if knowledge_path:
            knowledge_path=Path(knowledge_path)
            paths.extend((
                knowledge_path,knowledge_path.with_name("relationships.json"),
                knowledge_path.with_name("consumer_policies.json"),
            ))
        raw="|".join(self._file_signature(path,content=True) for path in paths)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def fingerprint(self,image: Path,derived_result: Mapping|None=None,*,
                    identity_key: str|None=None) -> str:
        image=Path(image)
        identity_key=self.image_identity_key(image) if identity_key is None else identity_key
        if self.external_evidence:
            parts=[
                "external",identity_key,
                self._file_signature(image),
                self._file_signature(image.with_suffix(".txt")),
                self._file_signature(image.with_suffix(".tags.json")),
                str(self.inspect_metadata),self._knowledge_fingerprint,
            ]
        else:
            derived=derived_result if isinstance(derived_result,Mapping) else {}
            parts=[
                "saved_authority",identity_key,
                self._file_signature(image.with_suffix(".tags.json"),content=True),
                str(derived.get("rules_fingerprint") or ""),
                self._knowledge_fingerprint,
            ]
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

    def dependency_details(
        self,image: Path,questions: Iterable[ImageQuestion],
    ) -> dict[str,object]:
        """Return the stable IDs consumed by one saved-authority projection."""
        tag_ids=set(); relationship_ids=set(); complete=True
        image=Path(image)
        if self.knowledge is not None and image.with_suffix(".tags.json").is_file():
            record=ImageRecord(image)
            rejected=record.annotation.source.get("rejected_tags",[])
            rejected=rejected if isinstance(rejected,list) else ()
            values=[
                *record.annotation.all_structured_tags(),
                *record.annotation.confirmed_absent,
                *rejected,
            ]
            values.extend(
                value
                for evidence in record.annotation.ai_evidence
                if evidence.kind=="sdxl_tags"
                for value,decision in evidence.decisions.items()
                if decision=="rejected"
            )
            for value in values:
                known=self.knowledge.find(str(value))
                if known is not None:
                    tag_ids.add(str(known.id))
            identities=record.annotation.tag_identities
            if isinstance(identities,dict):
                tag_ids.update(
                    str(stable_id) for stable_id in identities.values()
                    if str(stable_id) in self.knowledge.tags
                )
            for assignment in record.annotation.objects:
                stable_id=str(assignment.identity_tag_id or "")
                if stable_id in self.knowledge.tags:
                    tag_ids.add(stable_id)
            if self._related_service is None:
                self._related_service=RelatedTagSuggestionService(
                    self.knowledge,characters=self.characters,
                )
            dependency_ids=getattr(
                self._related_service,"dependency_tag_ids",None,
            )
            if callable(dependency_ids):
                tag_ids.update(dependency_ids(
                    record.annotation.all_structured_tags(),
                    confirmed_absent=record.annotation.confirmed_absent,
                    rejected_tags=rejected,annotation=record.annotation,
                ))
            elif record.annotation.characters:
                # Alternative suggestion services must enumerate any
                # character-derived roots before exact coverage is trusted.
                complete=False
        for question in questions:
            context=question.context if isinstance(question.context,dict) else {}
            suggestions=context.get("related_tag_suggestions",())
            for suggestion in suggestions if isinstance(suggestions,list) else ():
                if not isinstance(suggestion,dict):
                    continue
                for key in ("target_tag_id","supporting_tag_id"):
                    value=str(suggestion.get(key) or "")
                    if value:
                        tag_ids.add(value)
                tag_ids.update(
                    str(value) for value in suggestion.get("path_ids",())
                    if str(value)
                )
                relationship_ids.update(
                    str(value)
                    for value in suggestion.get("relationship_ids",())
                    if str(value)
                )
        return {
            "tag_ids":tuple(sorted(tag_ids)),
            "relationship_ids":tuple(sorted(relationship_ids)),
            "complete":complete,
        }

    def image_identity_key(self,image: Path) -> str:
        image=Path(image).resolve()
        if not self.library_id:
            return str(image)
        try:
            relative=image.relative_to(
                self.dataset_root.resolve()
            ).as_posix()
        except ValueError:
            return str(image)
        return f"{self.library_id}:{relative}"

    @classmethod
    def _cached_dependencies(cls,entry: dict) -> dict|None:
        """Reuse only complete, intact dependencies from the same question entry."""
        details=entry.get("dependencies")
        if not isinstance(details,dict) or details.get("complete") is not True:
            return None
        for key in ("tag_ids","relationship_ids"):
            values=details.get(key)
            if not isinstance(values,(list,tuple)) or any(
                not isinstance(value,str) or not value for value in values
            ):
                return None
        if entry.get("dependency_digest")!=cls._digest({
            "fingerprint":entry.get("fingerprint"),
            "questions":entry.get("questions"),"dependencies":details,
        }):
            return None
        return {"tag_ids":tuple(details["tag_ids"]),
                "relationship_ids":tuple(details["relationship_ids"]),"complete":True}

    def _load_cache(self) -> dict:
        try: payload=json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError,json.JSONDecodeError,TypeError): return {"version":self.CACHE_VERSION,"images":{}}
        if not isinstance(payload,dict) or payload.get("version")!=self.CACHE_VERSION: return {"version":self.CACHE_VERSION,"images":{}}
        if not isinstance(payload.get("images"),dict):
            payload["images"]={}
        return payload

    def _save_cache(self,payload: dict) -> None:
        self.cache_path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.cache_path.with_suffix(self.cache_path.suffix+".tmp")
        temporary.write_text(json.dumps(payload,ensure_ascii=False,separators=(",",":"))+"\n",encoding="utf-8")
        temporary.replace(self.cache_path)

    @staticmethod
    def _digest(value) -> str:
        encoded=json.dumps(
            value,sort_keys=True,separators=(",",":"),ensure_ascii=False,
            default=str,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]

    def _cached_evidence_questions(
        self,record: ImageRecord,derived_result: Mapping|None,
    ) -> list[ImageQuestion]:
        derived=derived_result if isinstance(derived_result,Mapping) else {}
        findings=derived.get("candidate_findings")
        if not isinstance(findings,list):
            findings=record.annotation.source.get("candidate_findings",[])
        grouped: dict[tuple,list[dict]]={}
        for raw in findings if isinstance(findings,list) else []:
            if not isinstance(raw,dict):
                continue
            kind=str(raw.get("kind") or "").strip()
            value=str(raw.get("value") or "").strip()
            if kind not in {"tag","description","ambiguous"} or not value:
                continue
            sources=tuple(sorted({
                str(source).strip() for source in raw.get("sources",[])
                if str(source).strip()
            })) or ("Saved imported evidence",)
            conflicts=tuple(sorted({
                str(message).strip() for message in raw.get("conflicts",[])
                if str(message).strip()
            }))
            grouped.setdefault((kind,sources,bool(conflicts)),[]).append({
                **raw,"value":value,"sources":list(sources),
                "conflicts":list(conflicts),
            })
        questions=[]
        for (kind,sources,conflicted),items in sorted(
            grouped.items(),key=lambda item:str(item[0]),
        ):
            values=list(dict.fromkeys(str(item["value"]) for item in items))
            source=", ".join(sources)
            digest=self._digest({
                "kind":kind,"sources":sources,"values":values,
                "conflicts":[item.get("conflicts",[]) for item in items],
            })
            if kind=="tag" and not conflicted:
                action="add_sdxl_tags"
                title=f"Review {len(values):,} saved Tag finding"+(
                    "" if len(values)==1 else "s"
                )
                question=(
                    f"Saved evidence from {source} contains these Tags. "
                    "Which belong in confirmed Image Knowledge?"
                )
            elif kind=="description" and not conflicted:
                action="add_plain_language"
                title=f"Review {len(values):,} saved Description finding"+(
                    "" if len(values)==1 else "s"
                )
                question=(
                    f"Saved evidence from {source} contains this Plain Language "
                    "Description. Add it to confirmed Image Knowledge?"
                )
            else:
                action="review_in_editor"
                title=(
                    "Review conflicting saved evidence" if conflicted
                    else "Classify ambiguous saved evidence"
                )
                question=(
                    "Open Image Data to inspect the saved evidence, its exact "
                    "source, and the current reviewed rule or type boundary."
                )
            confidence_values=[]
            for item in items:
                try:
                    confidence_values.append(float(item.get("confidence",.85)))
                except (TypeError,ValueError):
                    confidence_values.append(.85)
            questions.append(ImageQuestion(
                f"saved_{kind}_{digest}",title,question,action,values,
                source=source,confidence=min(confidence_values or [.85]),
                context={"candidate_findings":items},
            ))
        return questions

    def _derived_rule_questions(
        self,derived_result: Mapping|None,
    ) -> list[ImageQuestion]:
        derived=derived_result if isinstance(derived_result,Mapping) else {}
        state=derived.get("automatic_rule_state",{})
        conflict_state=(
            state.get("conflict_rules",{}) if isinstance(state,dict) else {}
        )
        values=[]
        for collection in (
            conflict_state.get("matches",[]) if isinstance(conflict_state,dict) else [],
            derived.get("candidate_conflicts",[]),
        ):
            for raw in collection if isinstance(collection,list) else []:
                if isinstance(raw,dict):
                    values.append(dict(raw))
        unique={
            self._digest(value):value for value in values
        }
        conflicts=[unique[key] for key in sorted(unique)]
        if not conflicts:
            return []
        digest=self._digest(conflicts)
        labels=[]
        for conflict in conflicts:
            message=str(conflict.get("message") or "Reviewed conflict").strip()
            tags=[str(tag) for tag in conflict.get("tags",[]) if str(tag).strip()]
            labels.append(message+(f" — {', '.join(tags)}" if tags else ""))
        return [ImageQuestion(
            f"diagnostic_conflicts_{digest}",
            f"Review {len(conflicts):,} diagnostic conflict"+(
                "" if len(conflicts)==1 else "s"
            ),
            "Current reviewed conflict rules found facts or saved evidence that "
            "need a person to inspect. No rule changed the image.",
            "review_in_editor",labels,source="Reviewed diagnostic rules",
            confidence=1.0,context={"diagnostic_conflicts":conflicts},
        )]

    def _related_questions(self,record: ImageRecord) -> list[ImageQuestion]:
        if self.knowledge is None:
            return []
        if self._related_service is None:
            self._related_service=RelatedTagSuggestionService(
                self.knowledge,characters=self.characters,
            )
        rejected=[
            str(value) for value in record.annotation.source.get("rejected_tags",[])
            if str(value).strip()
        ]
        rejected.extend(
            str(value)
            for evidence in record.annotation.ai_evidence
            if evidence.kind=="sdxl_tags"
            for value,decision in evidence.decisions.items()
            if decision=="rejected" and str(value).strip()
        )
        suggestions=tuple(
            suggestion for suggestion in self._related_service.suggest(
                record.annotation.all_structured_tags(),
                confirmed_absent=record.annotation.confirmed_absent,
                rejected_tags=rejected,annotation=record.annotation,
            )
            if suggestion.eligible
        )
        if not suggestions:
            return []
        serialized=[]; labels=[]
        for suggestion in suggestions:
            serialized.append({
                "target_tag_id":suggestion.tag_id,
                "target_display":suggestion.display,
                "supporting_tag_id":suggestion.supporting_tag_id,
                "supporting_display":suggestion.supporting_display,
                "path_ids":list(suggestion.path_ids),
                "path_displays":list(suggestion.path_displays),
                "relationship_ids":list(suggestion.relationship_ids),
                "relationship_kinds":list(suggestion.canonical_relationship_kinds),
                "depth":len(suggestion.relationship_ids),
                "scope":suggestion.scope,"owner_kind":suggestion.owner_kind,
                "owner_id":suggestion.owner_id,"owner_group":suggestion.owner_group,
                "source":suggestion.source,
            })
            labels.append(
                f"{suggestion.display} — {suggestion.path_label} — "
                f"{suggestion.scope}"
            )
        digest=self._digest(serialized)
        return [ImageQuestion(
            f"related_tags_{digest}",
            f"Review {len(suggestions):,} related Tag suggestion"+(
                "" if len(suggestions)==1 else "s"
            ),
            "Reviewed System Knowledge has bounded missing-association paths for "
            "this image. Open Image Data to inspect exact paths and owner scope; "
            "nothing is confirmed automatically.",
            "review_in_editor",labels,
            source="Reviewed System Knowledge relationships",confidence=.9,
            context={"related_tag_suggestions":serialized},
        )]

    def questions_for(
        self,image: Path,*,derived_result: Mapping|None=None,
    ) -> list[ImageQuestion]:
        image = Path(image)
        if not self.external_evidence and not image.with_suffix(".tags.json").is_file():
            return []
        record = ImageRecord(image)
        structured = TagCollection(typed_tag_sources(record,self.knowledge))
        structured_keys = structured.normalized_set()
        unavailable = unavailable_confirmed_tag_identities(
            record.annotation, self.knowledge
        )

        def is_unavailable(value: str) -> bool:
            known = self.knowledge.find(value) if self.knowledge is not None else None
            identity = f"id:{known.id}" if known is not None else f"name:{normalize_tag(value)}"
            return identity in unavailable

        questions: list[ImageQuestion] = []
        assigned_ids={value.character_id for value in record.annotation.characters if value.character_id}
        questions.extend(self._character_questions.questions(structured_keys,assigned_ids,record.annotation.plain_language))

        if (
            self.external_evidence and image.with_suffix(".txt").exists()
            and not image.with_suffix(".tags.json").exists()
        ):
            questions.append(
                ImageQuestion(
                    "create_structured_sidecar",
                    "Create structured data for this image",
                    "A TXT caption exists, but the image has no structured JSON sidecar. Create it?",
                    "create_sidecar",
                    source="TXT caption",
                )
            )

        context = folder_context(image, self.dataset_root)
        expected: list[tuple[str, str, str]] = []
        if context.rating and context.rating not in structured_keys:
            expected.append(("folder_rating", context.rating, "rating folder"))
        if context.style and not structured_keys.intersection(STYLE_ALIASES[context.style]):
            expected.append(("folder_style", context.style, "style folder"))
        if context.character:
            for character in (
                value.strip() for value in context.character.split(" and ") if value.strip()
            ):
                key = normalize_tag(character)
                if not any(tag == key or tag.startswith(key + " (") for tag in structured_keys):
                    expected.append(("folder_character", character, "character folder"))
        for code, value, source in expected:
            if is_unavailable(value):
                continue
            questions.append(
                ImageQuestion(
                    code,
                    f'Confirm missing tag: {value}',
                    f'This image is stored in a {source} named “{value}”, but that tag is missing. Add it?',
                    "add_sdxl_tags",
                    [value],
                    source=source,
                    confidence=0.95,
                )
            )

        if context.series and context.character:
            series = self.characters.find_series(context.series)
            character = self.characters.find_character(
                context.character, series_id=series.id if series else None
            )
            if character is not None and not any(
                item.character_id == character.id for item in record.annotation.characters
            ):
                questions.append(
                    ImageQuestion(
                        "assign_character_identity",
                        f"Assign character: {context.character}",
                        f"Does this image contain {context.character} from {context.series}?",
                        "assign_character",
                        [character.id, context.character, context.series],
                        source="folder organization",
                        confidence=0.95,
                    )
                )

        if context.style and record.annotation.visual_style.relationship == "unknown":
            questions.append(
                ImageQuestion(
                    "classify_visual_style",
                    f"Classify visual style: {context.style}",
                    (
                        f'The folder identifies the visual style as “{context.style}”. '
                        "Is this the character's original source style, a modified style, "
                        "or the style of another source?"
                    ),
                    "classify_visual_style",
                    [context.style, context.series or ""],
                    source="folder organization",
                    confidence=0.90,
                )
            )

        if not self.external_evidence:
            questions.extend(self._cached_evidence_questions(record,derived_result))
            questions.extend(self._derived_rule_questions(derived_result))
            questions.extend(self._related_questions(record))
            return questions

        report = self.inspector.inspect(image, include_metadata=self.inspect_metadata)
        for source in report.sources:
            if source.name == "JSON sidecar" or not source.available:
                continue
            if source.name == "Image metadata" and not self.inspect_metadata:
                continue
            missing_tags = [
                tag
                for tag in source.tags
                if normalize_tag(tag) not in structured_keys
                and not is_unavailable(tag)
            ]
            if missing_tags:
                questions.append(
                    ImageQuestion(
                        f"missing_from_json_{normalize_tag(source.name).replace(' ', '_')}",
                        f"Review {len(missing_tags)} tag(s) found only in {source.name}",
                        f"These tags appear in {source.name} but not in structured data. Add them?",
                        "add_sdxl_tags",
                        missing_tags,
                        source=source.name,
                        confidence=0.85,
                    )
                )
            existing_prose = {
                normalize_tag(value) for value in record.annotation.plain_language
            }
            missing_prose = [
                value for value in source.plain_language
                if normalize_tag(value) not in existing_prose
            ]
            if missing_prose:
                questions.append(
                    ImageQuestion(
                        f"missing_prose_{normalize_tag(source.name).replace(' ', '_')}",
                        f"Plain-language description found in {source.name}",
                        "Add this description to the image's structured plain-language data?",
                        "add_plain_language",
                        missing_prose,
                        source=source.name,
                        confidence=0.85,
                    )
                )
        return questions

    @staticmethod
    def character_questions(structured_keys: set[str],assigned_ids: set[str],characters: CharacterDatabase,plain_language: Iterable[str]=()) -> list[ImageQuestion]:
        """Propose identity fixes from structured tags and conservative prose evidence."""
        return CharacterQuestionIndex(characters).questions(structured_keys,assigned_ids,plain_language)

    def queue_for_images(
        self,
        images: Iterable[Path],
        tasks: ReviewTaskDatabase,
        *,
        derived_results: Mapping|None = None,
        cause: str = "Review data changed",
        progress: Callable[[int, int, str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        """Create or refresh deduplicated Review Center tasks for a dataset."""
        paths=[Path(image) for image in images]
        created=refreshed=retired=errors=cached=skipped=processed=0
        legacy_retired=0; cancelled=False; by_type: dict[str,int]={}
        error_messages=[]
        derived_results=derived_results if isinstance(derived_results,Mapping) else {}
        cache=self._load_cache(); cached_images=cache["images"]
        cache_dirty=False; queue_dirty=False
        mode="external_scan" if self.external_evidence else "saved_authority"
        prepared=[]; dependencies={}
        for index,image in enumerate(paths,1):
            if should_cancel and should_cancel():
                cancelled=True; break
            try:
                derived=(
                    derived_results.get(image)
                    or derived_results.get(str(image))
                    or {}
                )
                identity_key=self.image_identity_key(image)
                fingerprint=self.fingerprint(image,derived,identity_key=identity_key)
                cache_key=f"{mode}:{identity_key}"
                entry=cached_images.get(cache_key,{})
                entry=entry if isinstance(entry,dict) else {}
                cache_hit=entry.get("fingerprint")==fingerprint
                reusable_dependencies=(self._cached_dependencies(entry)
                    if cache_hit and not self.external_evidence else None)
                if cache_hit and not self.external_evidence and reusable_dependencies is None:
                    cache_hit=False
                if cache_hit:
                    questions=[
                        ImageQuestion.from_details(
                            str(item.get("title") or ""),item,
                        )
                        for item in entry.get("questions",[])
                        if isinstance(item,dict)
                    ]
                    cached+=1
                else:
                    questions=(
                        self.questions_for(image)
                        if self.external_evidence else
                        self.questions_for(image,derived_result=derived)
                    )
                    entry={
                        "fingerprint":fingerprint,"mode":mode,
                        "questions":[
                            {"title":item.title,**item.details()}
                            for item in questions
                        ],
                    }
                source_prefix=(
                    f"image_question:{identity_key}:"
                    if self.external_evidence else
                    f"image_question:derived:{identity_key}:"
                )
                dependency_details=(reusable_dependencies if cache_hit and reusable_dependencies is not None
                    else self.dependency_details(image,questions))
                if not cache_hit:
                    if not self.external_evidence:
                        # Never bless questions/dependencies read across an authority change.
                        if self.fingerprint(image,derived,identity_key=identity_key)!=fingerprint:
                            raise RuntimeError("Image Knowledge changed while preparing Review findings.")
                        entry["dependencies"]=dependency_details
                        entry["dependency_digest"]=self._digest({
                            "fingerprint":fingerprint,"questions":entry["questions"],
                            "dependencies":dependency_details,
                        })
                    cached_images[cache_key]=entry
                    cache_dirty=True
                dependencies[str(image)]=dependency_details
                prepared.append((
                    image,derived,questions,cache_hit,source_prefix,
                ))
            except Exception as exc:
                errors+=1
                error_messages.append(f"{image.name}: {exc}")
            processed+=1
            if progress:
                progress(index,2*len(paths),"Checking Review findings")
        with tasks.transaction():
            # A transactional Review workflow may have saved through another
            # database instance while derived work was being prepared. Merge
            # from that committed state before reconciling this projection.
            tasks.load()
            existing_by_prefix: dict[str,list] = {}
            pending_legacy_by_image: dict[Path,list] = {}
            for task in tasks.tasks.values():
                if task.task_type!="image_question" or not task.source_key.startswith("image_question:"):
                    if (
                        not self.external_evidence
                        and task.status=="pending"
                        and task.task_type=="merge_suggestion"
                        and task.image_path
                    ):
                        pending_legacy_by_image.setdefault(
                            Path(task.image_path),[],
                        ).append(task)
                    continue
                prefix=task.source_key.rsplit(":",1)[0]+":"
                existing_by_prefix.setdefault(prefix,[]).append(task)
            for position,(image,derived,questions,cache_hit,source_prefix) in enumerate(prepared,1):
                if progress and (position==1 or position%64==0 or position==len(prepared)):
                    progress(len(paths)+position,2*len(paths),"Reconciling Review results")
                active_keys=set()
                if not self.external_evidence:
                    for existing in pending_legacy_by_image.get(image,()):
                        if existing.status!="pending":
                            continue
                        existing.details["resolution"]={
                            "outcome":"legacy_projection_retired",
                            "reason":(
                                "Training projection rules no longer create "
                                "Image Knowledge cleanup questions."
                            ),
                            "cause":str(cause or "Derived data changed"),
                        }
                        changed=tasks.set_status(existing.id,"ignored")
                        legacy_retired+=int(changed)
                        queue_dirty=queue_dirty or changed
                if cache_hit:
                    expected_keys={
                        source_prefix+question.code for question in questions
                    }
                    if all(
                        tasks.find_by_source_key(source_key) is not None
                        for source_key in expected_keys
                    ):
                        skipped+=1
                        continue
                for question in questions:
                    source_key=source_prefix+question.code
                    active_keys.add(source_key)
                    existing=tasks.find_by_source_key(source_key)
                    before=existing.to_dict() if existing is not None else None
                    task=tasks.add_task(
                        task_type="image_question",title=question.title,
                        image_path=image,description=question.question,
                        details={
                            **question.details(),"derivation_mode":mode,
                            "derived_cause":str(
                                cause or "Derived data changed"
                            ),
                            "derived_rules_fingerprint":str(
                                derived.get("rules_fingerprint") or ""
                            ),
                            **({
                                "library_id":self.library_id,
                                "relative_image_path":image.resolve().relative_to(
                                    self.dataset_root.resolve()
                                ).as_posix(),
                            } if self.library_id else {}),
                        },
                        priority="normal",source_key=source_key,
                    )
                    if existing is None:
                        created+=1; queue_dirty=True
                        by_type[question.code]=by_type.get(question.code,0)+1
                    elif existing.status=="pending" and task.to_dict()!=before:
                        refreshed+=1; queue_dirty=True
                if not cache_hit:
                    for existing in existing_by_prefix.get(source_prefix,()):
                        if (
                            existing.status=="pending"
                            and existing.source_key.startswith(source_prefix)
                            and existing.source_key not in active_keys
                            and (
                                self.external_evidence
                                or existing.details.get("derivation_mode")==mode
                            )
                        ):
                            existing.details["resolution"]={
                                "outcome":"no_longer_applicable",
                                "reason":(
                                    "The saved Image Knowledge or reviewed "
                                    "dependencies changed and this question is "
                                    "no longer produced."
                                ),
                                "cause":str(cause or "Derived data changed"),
                            }
                            changed=tasks.set_status(existing.id,"ignored")
                            retired+=int(changed)
                            queue_dirty=queue_dirty or changed
            if queue_dirty:
                tasks.save()
        if cache_dirty:
            self._save_cache(cache)
        return {
            "images":len(paths),"created":created,"refreshed":refreshed,
            "errors":errors,"cached":cached,"skipped":skipped,
            "retired":retired,"legacy_retired":legacy_retired,
            "cancelled":cancelled,"processed":processed,"by_type":by_type,
            "messages":tuple(error_messages),"dependencies":dependencies,
        }

    @staticmethod
    def apply(image: Path, details: dict, knowledge=None) -> bool:
        record = ImageRecord(image)
        action = str(details.get("action") or "")
        values = [str(value).strip() for value in details.get("values", []) if str(value).strip()]
        changed = False
        if action == "create_sidecar":
            from .image_authority_initialization import ImageAuthorityInitializationService
            from .operation_journal import OperationJournal

            result=ImageAuthorityInitializationService(
                knowledge=knowledge,
                journal=OperationJournal(USER_DATA_DIR/"operation_journal.jsonl"),
            ).initialize_many([image])
            changed=bool(result.created or image.with_suffix(".tags.json").is_file())
        elif action == "add_sdxl_tags":
            automation = active_automation_engine(knowledge)
            issues = tag_addition_decision_issues(
                record.annotation,
                values,
                knowledge=knowledge,
                automation_engine=automation,
            )
            if issues:
                raise ValueError(
                    "This review action cannot silently reverse a reviewed image "
                    "decision. Open the image data editor to review: "
                    + "; ".join(issue.message for issue in issues)
                )
            before = record.annotation.all_structured_tags()
            trial = TagCollection(before)
            trial.extend(values)
            warnings = automation.conflict_warnings(image, before, trial)
            if warnings:
                raise ValueError(
                    "This review action would introduce a tag conflict. Open the "
                    "image data editor to resolve it: " + "; ".join(warnings)
                )
            changed = record.confirm_sdxl_tags(values, knowledge=knowledge)
        elif action == "add_plain_language":
            prose = TagCollection(record.annotation.plain_language)
            changed = bool(prose.extend(values))
            if changed:
                record.annotation.plain_language = prose.to_list()
                ImageFindingDecisionService(
                    record.annotation,knowledge=knowledge,
                ).accept_confirmed_descriptions(
                    record.annotation.plain_language
                )
        elif action == "register_character":
            raise ValueError(
                "This saved question uses a retired Character Knowledge writer. "
                "Create or link the Character Identity and Character Source through Tag Details, then rescan questions."
            )
        elif action == "assign_character":
            if not values:
                return False
            if not any(item.character_id == values[0] for item in record.annotation.characters):
                record.annotation.add_character(character_id=values[0])
                changed = True
        elif action == "replace_character":
            if len(values)<2: return False
            matches=[item for item in record.annotation.characters if item.character_id==values[1]]
            if len(matches)!=1: return False
            matches[0].character_id=values[0]; changed=True
        elif action == "classify_visual_style":
            relationship = str(details.get("selected_relationship") or "unknown")
            source_series_id = str(details.get("selected_style_series_id") or "")
            style_tag = values[0] if values else ""
            record.annotation.visual_style.relationship = relationship
            record.annotation.visual_style.style_tags = [style_tag] if style_tag else []
            record.annotation.visual_style.source_series_ids = [source_series_id] if source_series_id else []
            changed = relationship != "unknown" or bool(style_tag)
        if changed and action != "create_sidecar":
            record.modified = True
            if action in {"assign_character", "replace_character"}:
                # These actions change only the structured character slot. The
                # identity Tag that prompted the question is already confirmed,
                # so the training projection is unchanged. Preserve unrelated
                # confirmed-Tag lifecycle links exactly; their dedicated repair
                # review must not block this scoped image decision.
                record.save_sidecar(force=True)
            else:
                record.save_sidecar(force=True, knowledge=knowledge)
                record.export_caption(knowledge=knowledge)
        return changed
