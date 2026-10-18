from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Iterable

from .confirmed_tag_identity import ConfirmedTagIdentityService
from .consumer_policies import IMAGE_IDENTIFICATION
from .image_fact_decisions import tag_addition_decision_issues
from .knowledge_relationships import CHARACTER_SOURCE


SOURCE_AUTOFILL_KEY="character_source_autofill"
DESCRIPTION_AUTOFILL_KEY="character_description_autofill"


def character_description_autofill_state(annotation):
    value=annotation.source.get(DESCRIPTION_AUTOFILL_KEY,{})
    if (not isinstance(value,dict) or value.get("version",1)!=1
            or not isinstance(value.get("enabled",True),bool)
            or not isinstance(value.get("sentence",""),str)
            or not isinstance(value.get("original_sentence",""),str)):
        return None
    return {**value,"version":1,"enabled":value.get("enabled",True),
            "sentence":value.get("sentence",""),
            "original_sentence":value.get("original_sentence","")}


def apply_character_description_autofill_state(annotation,state):
    if (state is not None
            and character_description_autofill_state(annotation) is not None
            and character_description_autofill_state(annotation)!=state):
        annotation.source[DESCRIPTION_AUTOFILL_KEY]=dict(state)


def reconcile_character_description(text,state,identity):
    """Add an attributed identity sentence while retaining every existing sentence.

    Only our exact untouched sentence can be replaced or removed on a Tag change.
    Inline source additions retain the original line for exact restoration.
    A user edit/removal turns automation off until deliberately enabled again.
    """
    if state is None or not state["enabled"]:
        return text,state
    result=dict(state)
    previous=state["sentence"]
    lines=text.split("\n")
    if previous and previous not in lines:
        result["enabled"]=False
        return text,result
    if previous:
        index=lines.index(previous)
        if state.get("original_sentence"):
            lines[index]=state["original_sentence"]
        else:
            lines.pop(index)
    original="\n".join(lines)
    result["sentence"]=""
    result["original_sentence"]=""
    if not identity:
        return original,result
    name,names,source_name,source_names=identity
    # Tag-style qualifiers disambiguate stored identities, not prose names.
    # Never infer a confirmed source from their contents.
    def prose_name(value):
        value=value.replace("_"," ").strip()
        while re.search(r"\s+\([^()]*\)$",value):
            value=re.sub(r"\s+\([^()]*\)$","",value).strip()
        return value

    names=tuple(dict.fromkeys((*names,*(prose_name(value) for value in (name,*names)))))
    name=prose_name(name)

    def normalized(value):
        return " ".join(unicodedata.normalize("NFKC",value).casefold().replace("_"," ").split())

    haystack=normalized(original)
    def mentions(values):
        return any(re.search(r"(?<!\w)"+re.escape(normalized(value))+r"(?!\w)",haystack)
                   for value in values if normalized(value))

    # Enrich an existing name in place instead of repeating it in a new line.
    if mentions(names):
        if not source_name or mentions(source_names):
            return original,result
        patterns=[re.compile(
            r"(?<!\w)"+r"[\t _]+".join(re.escape(part) for part in value.replace("_"," ").split())+r"(?!\w)",
            re.IGNORECASE,
        ) for value in names if value.strip()]
        for index,line in enumerate(lines):
            matches=[match for pattern in patterns for match in pattern.finditer(line)]
            if not matches:
                continue
            match=min(matches,key=lambda item:(item.start(),-len(item.group())))
            # Keep an explicit existing source or possessive construction intact.
            if re.match(r"(?:['’]s\b|\s+from\b)",line[match.end():],re.IGNORECASE):
                return original,result
            updated=line[:match.end()]+f" from {source_name}"+line[match.end():]
            lines[index]=updated
            result["sentence"]=updated
            result["original_sentence"]=line
            return "\n".join(lines),result
        # Normalization may recognize a name whose exact span is not safe to edit.
        return original,result
    sentence=f"The image depicts {name}"+(f" from {source_name}" if source_name else "")+"."
    result["sentence"]=sentence
    return (sentence+("\n"+original if original else "") if sentence else original),result


def character_source_autofill_state(annotation):
    """Read the image's override without interpreting future policy versions."""
    value=annotation.source.get(SOURCE_AUTOFILL_KEY,{})
    if (not isinstance(value,dict) or value.get("version",1)!=1
            or not isinstance(value.get("enabled",True),bool)):
        return None
    excluded=value.get("excluded_tag_ids",[])
    if not isinstance(excluded,list) or not all(isinstance(item,str) for item in excluded):
        return None
    return bool(value.get("enabled",True)),frozenset(excluded)


def apply_character_source_autofill_state(annotation,state):
    """Persist only a changed staged override; preserve unrelated source fields."""
    if state is None or character_source_autofill_state(annotation)==state:
        return
    current=annotation.source.get(SOURCE_AUTOFILL_KEY,{})
    if character_source_autofill_state(annotation) is None:
        return
    annotation.source[SOURCE_AUTOFILL_KEY]={
        **current,"version":1,"enabled":state[0],
        "excluded_tag_ids":sorted(state[1]),
    }

CHARACTER_ROLE_LABELS={
    "character_identity":"Character Identity",
    "character_source":"Character Source",
    "character_outfit":"Character Outfit",
}


def _identity_name_pattern(names):
    alternatives=[r"[\s_]+".join(re.escape(part) for part in name.replace("_"," ").split())
                  for name in sorted(set(names),key=lambda value:(-len(value),value)) if name.strip()]
    return re.compile(r"(?<!\w)(?:"+"|".join(alternatives)+r")(?!\w)",re.IGNORECASE) if alternatives else None


def _description_aliases(aliases):
    # Single-word aliases often double as ordinary prose (May, Red, etc.).
    return tuple(value for value in aliases
                 if len(value.replace("_"," ").split())>1 or any(char.isdigit() for char in value))


@dataclass(frozen=True)
class CharacterIdentityClearPlan:
    removed_tags: tuple[str,...]
    rejected_tags: tuple[str,...]
    description: str
    changed_descriptions: tuple[str,...]
    removed_descriptions: tuple[str,...]
    association_count: int


def clear_character_description(text,character_names,source_names):
    """Anonymize exact known names; discard unsafe sentences, never raw evidence.

    This is an explicit correction, not the automatic description reconciler.
    Only source clauses attached directly to a known name can be removed safely.
    Other source wording drops the complete sentence and is reported to the editor.
    """
    names=_identity_name_pattern(character_names)
    sources=_identity_name_pattern(source_names)
    changed=[]; removed=[]; output=[]
    # Keep untouched prose and separators byte-for-byte, including repeated lines.
    parts=re.split(r'(\n+|(?<=[.!?])[^\S\n]+(?=[A-Z0-9"“‘(]))',text)
    for index in range(0,len(parts),2):
        sentence=parts[index]
        separator=parts[index+1] if index+1<len(parts) else ""
        if not ((names and names.search(sentence)) or (sources and sources.search(sentence))):
            output.append(sentence+separator); continue
        updated=sentence
        compound=names and any(
            (match.start()>0 and sentence[match.start()-1] in "-–—")
            or (match.end()<len(sentence) and sentence[match.end()] in "-–—")
            for match in names.finditer(sentence)
        )
        if compound:updated=""
        if names and sources:
            # Keep the name as the grammatical subject, removing its attached origin.
            attached=re.compile(
                "("+names.pattern+r")(?:\s+from\s+"+sources.pattern
                +r"(?:\s+(?:series|franchise|universe|anime|manga))?"
                +r"(?=$|[.,;:!?]|\s+(?:is|are|was|were|has|have|had|wears|wearing|stands|standing|sits|sitting|poses|posing|looks|looking|with|in|on|at|who|whose)\b)"
                +r"|\s*\(from\s+"+sources.pattern+r"\))",
                re.IGNORECASE,
            )
            updated=attached.sub(r"\1",updated)
        if sources and sources.search(updated):
            updated=""  # Unknown clause structure: no arbitrary word deletion.
        if updated and names:
            identity_only=re.fullmatch(
                r"\s*(?:(?:the image|this image|the picture|this picture)\s+(?:depicts|shows|features)\s+|(?:this|it)\s+is\s+)?"
                +names.pattern+r"[.!?]?\s*",updated,re.IGNORECASE,
            )
            naming_only=re.search(
                r"\b(?:name is|named|called|known as|identified as|character is|subject is)\s+"
                +names.pattern+r"[.!?]?\s*$",updated,re.IGNORECASE,
            )
            if identity_only or naming_only:
                updated=""
            else:
                updated=re.sub(r"\b((?:a|the) character) (?:named|called|known as)\s+"+names.pattern,
                               r"\1",updated,flags=re.IGNORECASE)
                updated=names.sub("the character",updated)
                updated=re.sub(r"\b(?:an?|the)\s+the character\b","the character",updated,flags=re.IGNORECASE)
                if updated.lstrip().startswith("the character"):
                    offset=len(updated)-len(updated.lstrip())
                    updated=updated[:offset]+"T"+updated[offset+1:]
        changed.append(sentence.strip())
        if updated:
            output.append(updated+separator)
        else:
            removed.append(sentence.strip())
    return "".join(output).rstrip() if changed else text,tuple(changed),tuple(removed)


@dataclass(frozen=True)
class CharacterImageFact:
    tag_id: str
    display: str
    role: str
    linked_kind: str=""
    linked_id: str=""
    linked_name: str=""
    character_ids: tuple[str,...]=()
    source_id: str=""
    link_state: str="Unlinked reusable Tag"

    @property
    def role_label(self) -> str:
        return CHARACTER_ROLE_LABELS.get(self.role,self.role.replace("_"," ").title())


@dataclass(frozen=True)
class LegacyCharacterContext:
    assignment_id: str
    slot: int
    identity: str
    source: str
    outfits: tuple[str,...]
    role: str
    owned_tags: tuple[str,...]
    suggested_tag_ids: tuple[str,...]
    state: str


@dataclass(frozen=True)
class CharacterImageKnowledgeView:
    facts: tuple[CharacterImageFact,...]
    legacy_contexts: tuple[LegacyCharacterContext,...]
    suggested_tag_ids: tuple[str,...]
    suggested_values: tuple[str,...]
    identity_count: int
    ownership_state: str


class CharacterImageKnowledgeService:
    """Build a no-write tag-rooted character view over legacy image context."""

    ROLE_ORDER={
        "character_identity":0,
        "character_source":1,
        "character_outfit":2,
    }

    def __init__(self,tags,characters,categories=None) -> None:
        self.tags=tags
        self.characters=characters
        self.categories=categories

    def _role(self,tag) -> str:
        role=(
            self.categories.effective_semantic_role(tag.category)
            if self.categories is not None else tag.category
        )
        if role=="copyright_series":
            role="character_source"
        return role if role in CHARACTER_ROLE_LABELS else ""

    def _reviewed_tag(self,record,role: str,links):
        if self.tags is None or record is None:
            return None
        kind={
            "character_identity":"character",
            "character_source":"series",
            "character_outfit":"outfit",
        }[role]
        link=self.characters.effective_knowledge_tag(kind,record.id)
        if not isinstance(link,dict) or link.get("status")!="reviewed":
            return None
        tag=self.tags.tags.get(str(link.get("tag_id") or ""))
        if tag is None or self._role(tag)!=role:
            return None
        if tuple(links[role].get(tag.id,()))!=(record,):
            return None
        return tag

    def _reviewed_links(self):
        links={role:{} for role in CHARACTER_ROLE_LABELS}
        for role,records in (
            ("character_identity",self.characters.characters.values()),
            ("character_source",self.characters.series.values()),
            ("character_outfit",self.characters.outfits.values()),
        ):
            kind={
                "character_identity":"character",
                "character_source":"series",
                "character_outfit":"outfit",
            }[role]
            for record in records:
                tag_id=self.characters.reviewed_knowledge_tag_id(kind,record.id)
                if tag_id:
                    links[role].setdefault(tag_id,[]).append(record)
        return links

    def _fact(self,tag,role: str,links) -> CharacterImageFact:
        linked=tuple(links[role].get(tag.id,()))
        if len(linked)!=1:
            return CharacterImageFact(
                tag.id,tag.display,role,
                link_state=(
                    "Conflicting reusable links" if linked
                    else "No reviewed reusable link"
                ),
            )
        record=linked[0]
        if role=="character_identity":
            return CharacterImageFact(
                tag.id,tag.display,role,"character",record.id,record.name,
                (record.id,),self.characters.character_source_id("character",record.id),
                "Reviewed reusable link",
            )
        if role=="character_source":
            character_ids=self.characters.character_ids_for_source(record.id)
            return CharacterImageFact(
                tag.id,tag.display,role,"series",record.id,record.name,
                character_ids,record.id,"Reviewed reusable link",
            )
        return CharacterImageFact(
            tag.id,tag.display,role,"outfit",record.id,record.name,
            self.characters.associated_character_ids(record.id),
            self.characters.character_source_id("outfit",record.id),
            "Reviewed reusable link",
        )

    def analyze(
        self,annotation,values: Iterable[str],
    ) -> CharacterImageKnowledgeView:
        links=self._reviewed_links()
        facts=[]
        seen_ids=set()
        plan=(ConfirmedTagIdentityService.shared(self.tags).plan(
            values,stored_ids=annotation.tag_identities,confirmation=False,
        ) if self.tags is not None else None)
        for entry in plan.entries if plan is not None else ():
            tag=self.tags.tags.get(entry.stable_id) if entry.resolved else None
            if tag is None or tag.id in seen_ids:
                continue
            role=self._role(tag)
            if not role:
                continue
            seen_ids.add(tag.id)
            facts.append(self._fact(tag,role,links))
        facts.sort(key=lambda item:(
            self.ROLE_ORDER.get(item.role,99),item.display.casefold(),item.tag_id,
        ))
        present_ids={item.tag_id for item in facts}
        suggested=[]

        def suggest(tag):
            if tag is not None and tag.id not in present_ids and tag.id not in suggested:
                suggested.append(tag.id)

        for fact in facts:
            if fact.linked_kind!="character":
                continue
            character=self.characters.characters.get(fact.linked_id)
            source_id=(
                self.characters.character_source_id("character",character.id)
                if character is not None else ""
            )
            source=(
                self.characters.series.get(source_id)
                if source_id else None
            )
            suggest(
                self._reviewed_tag(source,"character_source",links)
                if source else None
            )

        contexts=[]
        for assignment in sorted(annotation.characters,key=lambda item:item.slot):
            character=self.characters.characters.get(assignment.character_id)
            source_id=(
                self.characters.character_source_id("character",character.id)
                if character is not None else ""
            )
            source=(
                self.characters.series.get(source_id)
                if source_id else None
            )
            outfits=tuple(
                self.characters.outfits[outfit_id]
                for outfit_id in assignment.outfit_ids
                if outfit_id in self.characters.outfits
            )
            assignment_suggestions=[]

            def assignment_suggest(tag):
                if tag is None or tag.id in present_ids:
                    return
                if tag.id not in suggested:
                    suggested.append(tag.id)
                if tag.id not in assignment_suggestions:
                    assignment_suggestions.append(tag.id)

            assignment_suggest(
                self._reviewed_tag(character,"character_identity",links)
                if character is not None else None
            )
            assignment_suggest(
                self._reviewed_tag(source,"character_source",links)
                if source is not None else None
            )
            for outfit in outfits:
                assignment_suggest(
                    self._reviewed_tag(outfit,"character_outfit",links)
                )
            if assignment_suggestions:
                state="Reviewed Character Tags available"
            elif assignment.character_id and character is None:
                state="Missing reusable Character record"
            elif assignment.character_id or assignment.outfit_ids or assignment.all_tags():
                state="Retained ownership context"
            elif assignment.role:
                state="Retained compatibility role"
            else:
                state="Empty legacy assignment"
            contexts.append(LegacyCharacterContext(
                assignment_id=assignment.id,
                slot=assignment.slot,
                identity=(character.name if character is not None else
                          str(assignment.character_id or "Not identified")),
                source=source.name if source is not None else "",
                outfits=tuple(outfit.name for outfit in outfits),
                role=str(assignment.role or ""),
                owned_tags=tuple(assignment.all_tags()),
                suggested_tag_ids=tuple(assignment_suggestions),
                state=state,
            ))
        suggested_tags=(
            [self.tags.tags[tag_id] for tag_id in suggested]
            if self.tags is not None else []
        )
        suggested_tags.sort(key=lambda tag:(
            self.ROLE_ORDER.get(self._role(tag),99),tag.display.casefold(),tag.id,
        ))
        identity_count=sum(item.role=="character_identity" for item in facts)
        ownership_state=(
            f"{len(contexts):,} retained ownership context"
            f"{'s' if len(contexts)!=1 else ''}"
            if contexts else
            "Ownership not specified" if identity_count>1 else
            "No per-image ownership needed"
        )
        return CharacterImageKnowledgeView(
            facts=tuple(facts),legacy_contexts=tuple(contexts),
            suggested_tag_ids=tuple(tag.id for tag in suggested_tags),
            suggested_values=tuple(tag.display for tag in suggested_tags),
            identity_count=identity_count,ownership_state=ownership_state,
        )

    def identity_clear_plan(self,annotation,values,text,*,prior_values=()):
        """Plan an explicit per-image correction using canonical roles and names."""
        current=tuple(values)
        resolver=ConfirmedTagIdentityService.shared(self.tags)
        entries=resolver.plan((*current,*prior_values),stored_ids=annotation.tag_identities,
                              confirmation=False).entries
        affected={}; removed=[]
        for entry in entries:
            tag=self.tags.tags.get(entry.stable_id) if entry.resolved else None
            if tag is not None and self._role(tag) in {"character_identity","character_source"}:
                affected[tag.id]=tag
                if entry.raw_value in current:removed.append(entry.raw_value)
        # A description may still name a known identity after its chip was removed.
        # Resolve each exact name before using it; collisions are never guessed.
        for tag in self.tags.tags.values():
            if self._role(tag) not in {"character_identity","character_source"}:
                continue
            pattern=_identity_name_pattern((tag.display,*_description_aliases(tag.aliases)))
            if pattern and pattern.search(text):
                matches=resolver.plan((match.group() for match in pattern.finditer(text)),
                                      confirmation=False).entries
                if any(entry.resolved and entry.stable_id==tag.id for entry in matches):
                    affected[tag.id]=tag
        names=set(); sources=set(); rejected=set(removed)
        links=self._reviewed_links()
        record_owners={}
        for kind,records in (("character",self.characters.characters.values()),("series",self.characters.series.values())):
            for record in records:
                for name in (record.name,*record.aliases):
                    record_owners.setdefault((kind,name.casefold().replace("_"," ")),set()).add(record.id)
        def record_names(kind,record):
            return (record.name,*(alias for alias in _description_aliases(record.aliases)
                if record_owners.get((kind,alias.casefold().replace("_"," ")))=={record.id}))
        for tag in affected.values():
            target=names if self._role(tag)=="character_identity" else sources
            target.add(tag.display); rejected.add(tag.display)
            for alias in _description_aliases(tag.aliases):
                entry=resolver.plan((alias,),confirmation=False).entries[0]
                if entry.resolved and entry.stable_id==tag.id:target.add(alias)
            for record in links[self._role(tag)].get(tag.id,()):
                kind="character" if self._role(tag)=="character_identity" else "series"
                target.update(record_names(kind,record))
                if self._role(tag)=="character_identity":
                    source_id=self.characters.character_source_id("character",record.id)
                    source=self.characters.series.get(source_id)
                    if source is not None:sources.update(record_names("series",source))
        for assignment in annotation.characters:
            record=self.characters.characters.get(assignment.character_id)
            if record is not None:
                names.update(record_names("character",record))
                source=self.characters.series.get(self.characters.character_source_id("character",record.id))
                if source is not None:sources.update(record_names("series",source))
                tag=self._reviewed_tag(record,"character_identity",links)
                if tag is not None:rejected.add(tag.display)
        description,changed,discarded=clear_character_description(text,names,sources)
        return CharacterIdentityClearPlan(
            tuple(dict.fromkeys(removed)),tuple(sorted(rejected)),description,changed,discarded,
            sum(bool(item.character_id) for item in annotation.characters),
        )

    def automatic_source_tags(self,annotation,values,*,consumer_policies,automation_engine,conflict_check):
        """Plan only reviewed, permitted sources of confirmed character identities.

        This is the narrow source-association default, not general fact expansion.
        No writes, lexical inference, compatibility-only source, or owner creation.
        The caller applies image overrides and stages the result with normal Undo.
        """
        values=tuple(values)
        view=self.analyze(annotation,values)
        links=self._reviewed_links()
        targets={}
        for fact in view.facts:
            if fact.linked_kind!="character":
                continue
            if tag_addition_decision_issues(annotation,(fact.display,),knowledge=self.tags):
                continue
            relation=self.characters.character_relationship(
                CHARACTER_SOURCE,"character",fact.linked_id,
            )
            if (not relation.represented or relation.conflicting
                    or len(relation.active_relationships)!=1
                    or not relation.target_id):
                continue
            source=self.characters.series.get(relation.target_id)
            target=self._reviewed_tag(source,"character_source",links)
            if target is None:
                continue
            target_plan=ConfirmedTagIdentityService.shared(self.tags).plan(
                (target.display,),confirmation=False,
            )
            if (not target_plan.resolved
                    or target_plan.entries[0].stable_id!=target.id):
                continue
            decision=consumer_policies.resolve(
                IMAGE_IDENTIFICATION,relationship_kind=CHARACTER_SOURCE,
                relationship_id=relation.active_relationships[0].id,
                tag_id=target.id,context={"scope":"image","owner_kind":"image"},
            )
            if decision.failed_closed or not decision.permits_fact_expansion:
                continue
            if tag_addition_decision_issues(
                annotation,(target.display,),knowledge=self.tags,
                automation_engine=automation_engine,
            ):
                continue
            targets[target.id]=target
        # Check the complete proposed set so sources cannot conflict with each other.
        if targets and conflict_check(
            values,(*values,*(tag.display for tag in targets.values())),
        ):
            return ()
        return tuple(sorted(targets.values(),key=lambda tag:(tag.display.casefold(),tag.id)))

    def description_identity(self,annotation,values):
        """Use one confirmed linked identity and its separately confirmed source."""
        view=self.analyze(annotation,values)
        identities=[fact for fact in view.facts if fact.role=="character_identity"]
        if len(identities)!=1 or identities[0].linked_kind!="character":
            return None
        fact=identities[0]
        if tag_addition_decision_issues(annotation,(fact.display,),knowledge=self.tags):
            return None
        character=self.characters.characters[fact.linked_id]
        relation=self.characters.character_relationship(CHARACTER_SOURCE,"character",character.id)
        source=None
        if relation.represented and not relation.conflicting and len(relation.active_relationships)==1:
            source=next((self.characters.series[item.linked_id] for item in view.facts
                         if item.linked_kind=="series" and item.linked_id==relation.target_id),None)
        return (
            character.name,(character.name,*character.aliases),
            source.name if source else "",(source.name,*source.aliases) if source else (),
        )
