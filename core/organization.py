from __future__ import annotations

import re
from dataclasses import dataclass,field
from datetime import datetime
from pathlib import Path
from typing import Any

from .character_database import CharacterDatabase
from .evidence_resolver import EvidenceResolver
from .image_record import ImageRecord
from .tag_collection import normalize_tag


LEVEL_LABELS={
    "rating":"Rating",
    "series":"Character Source",
    "character":"Character",
    "character_type":"Character type",
    "outfit":"Outfit",
    "style":"Visual style",
    "style_relationship":"Style relationship",
    "species":"Species",
    "project":"Project or custom group",
    "source_folder":"Original source folder",
    "ingest_date":"Date ingested",
}

DEFAULT_LEVEL_CONTAINERS={
    "series":"Specific Character Source",
    "character":"Specific Character",
    "outfit":"Specific Outfit",
    "species":"Specific Species",
}


def default_organization_profile() -> dict[str,Any]:
    return {
        "name":"Default",
        "levels":["rating","style","species"],
        "use_known_character_route":True,
        "known_character_levels":["rating","style","series","character"],
        "fallback_enabled":False,
        "fallback_folder":"Unsorted",
        "level_containers":{
            "series":DEFAULT_LEVEL_CONTAINERS["series"],
            "species":DEFAULT_LEVEL_CONTAINERS["species"],
        },
    }


INVALID_COMPONENT=re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED={"CON","PRN","AUX","NUL",*(f"COM{x}" for x in range(1,10)),*(f"LPT{x}" for x in range(1,10))}


def safe_folder_name(value: str,fallback: str="Unsorted") -> str:
    cleaned=INVALID_COMPONENT.sub("-",str(value)).strip(" .")
    if not cleaned or cleaned.upper() in RESERVED: cleaned=fallback
    return cleaned[:120].rstrip(" .") or fallback


@dataclass
class OrganizationProfile:
    name: str="Default"
    levels: list[str]=field(default_factory=lambda:["rating","style","species"])
    use_known_character_route: bool=True
    known_character_levels: list[str]=field(default_factory=lambda:["rating","style","series","character"])
    fallback_enabled: bool=False
    fallback_folder: str="Unsorted"
    level_containers: dict[str,str]=field(default_factory=dict)

    @property
    def depends_on_tag_order(self) -> bool:
        # Every currently supported route consumes explicit Image Knowledge
        # fields. Unknown dictionary flags cannot opt into unsupported routing.
        return False

    def __post_init__(self):
        self.levels=list(dict.fromkeys(value for value in self.levels if value in LEVEL_LABELS))
        self.known_character_levels=list(dict.fromkeys(value for value in self.known_character_levels if value in LEVEL_LABELS))
        self.fallback_folder=safe_folder_name(self.fallback_folder or "Unsorted")
        self.level_containers={
            str(level):safe_folder_name(str(label))
            for level,label in self.level_containers.items()
            if str(level) in LEVEL_LABELS and str(label).strip()
        }

    @classmethod
    def from_dict(cls,value: Any):
        data=value if isinstance(value,dict) else {}
        valid=lambda items:[str(item) for item in items if str(item) in LEVEL_LABELS] if isinstance(items,(list,tuple)) else []
        defaults=default_organization_profile()
        raw_containers=data.get("level_containers")
        # Existing saved profiles predate category containers. Preserve their
        # exact routing until the user previews and saves an explicit change.
        containers=(
            {str(level):str(label) for level,label in raw_containers.items()}
            if isinstance(raw_containers,dict) else
            (dict(defaults["level_containers"]) if not data else {})
        )
        fallback_folder=safe_folder_name(
            str(data.get("fallback_folder") or "Unsorted")
        )
        fallback_enabled=(
            bool(data.get("fallback_enabled"))
            if "fallback_enabled" in data else
            fallback_folder.casefold()!="unsorted"
        )
        return cls(
            name=str(data.get("name") or "Default"),
            levels=valid(data.get("levels",defaults["levels"])),
            use_known_character_route=bool(data.get("use_known_character_route",True)),
            known_character_levels=valid(data.get("known_character_levels",defaults["known_character_levels"])),
            fallback_enabled=fallback_enabled,
            fallback_folder=fallback_folder,
            level_containers=containers,
        )

    def to_dict(self):
        return {
            "name":self.name,"levels":list(self.levels),
            "use_known_character_route":self.use_known_character_route,
            "known_character_levels":list(self.known_character_levels),
            "fallback_enabled":self.fallback_enabled,
            "fallback_folder":self.fallback_folder,
            "level_containers":dict(self.level_containers),
        }


@dataclass
class OrganizationSuggestion:
    destination: Path
    components: list[str]
    confidence: float
    reasons: list[str]=field(default_factory=list)
    missing: list[str]=field(default_factory=list)
    skipped: list[str]=field(default_factory=list)

    @property
    def ready(self): return not self.missing


class OrganizationRouter:
    """Preview a configurable destination without creating or moving anything."""

    def __init__(self,working_root: Path,characters: CharacterDatabase,profile: OrganizationProfile|dict|None=None):
        self.root=Path(working_root); self.characters=characters
        self.profile=profile if isinstance(profile,OrganizationProfile) else OrganizationProfile.from_dict(profile)

    def suggest(self,image: str|Path|ImageRecord) -> OrganizationSuggestion:
        record=image if isinstance(image,ImageRecord) else ImageRecord(image)
        path=record.image_path; annotation=record.annotation
        assigned=[self.characters.characters.get(slot.character_id) for slot in annotation.characters if slot.character_id]
        assigned=[item for item in assigned if item]
        levels=(self.profile.known_character_levels if assigned and self.profile.use_known_character_route else self.profile.levels)
        assertions=EvidenceResolver().analyze(path,self.root)
        rating=assertions["rating"]; style=assertions["visual_style"]
        values: dict[str,list[str]]={}
        if rating.value and not rating.conflict: values["rating"]=[rating.value]
        confirmed_styles={
            normalize_tag(value)
            for value in annotation.visual_style.style_tags
            if normalize_tag(value)
        }
        if confirmed_styles:
            values["style"]=sorted(confirmed_styles)
        elif style.value and not style.conflict:
            values["style"]=[style.value]
        series=[]; characters=[]; outfits=[]
        for item in assigned:
            characters.append(item.name)
            source_id=self.characters.character_source_id("character",item.id)
            if source_id in self.characters.series: series.append(self.characters.series[source_id].name)
        for slot in annotation.characters:
            for outfit_id in slot.outfit_ids:
                if outfit_id in self.characters.outfits: outfits.append(self.characters.outfits[outfit_id].name)
        if series: values["series"]=series
        if characters: values["character"]=characters
        if outfits: values["outfit"]=outfits
        character_type=str(annotation.source.get("character_type") or "").strip()
        if not character_type: character_type="Known characters" if assigned else ("Unidentified characters" if annotation.characters else "No assigned character")
        values["character_type"]=[character_type]
        if annotation.visual_style.relationship!="unknown": values["style_relationship"]=[annotation.visual_style.relationship.replace("_"," ").title()]
        species=annotation.custom_groups.get("species",[])
        if species: values["species"]=species
        project=str(annotation.source.get("project") or annotation.source.get("custom_group") or "").strip()
        if project: values["project"]=[project]
        values["source_folder"]=[path.parent.name]
        try: values["ingest_date"]=[datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m")]
        except OSError: pass

        components=[]; missing=[]; skipped=[]; reasons=[]
        def append_component(component: str) -> None:
            if component.casefold() not in {
                item.casefold() for item in components
            }:
                components.append(component)
        for level in levels:
            options=[]
            for value in values.get(level,[]):
                clean=safe_folder_name(value,"")
                if clean and clean.casefold() not in {
                    item.casefold() for item in options
                }:
                    options.append(clean)
            options.sort(key=str.casefold)
            if options:
                container=self.profile.level_containers.get(level,"")
                if container:
                    append_component(container)
                component=" and ".join(options)
                append_component(component)
                reasons.append(f"{LEVEL_LABELS[level]}: {component}")
            elif self.profile.fallback_enabled:
                container=self.profile.level_containers.get(level,"")
                if container:
                    append_component(container)
                append_component(self.profile.fallback_folder)
                missing.append(LEVEL_LABELS[level])
                reasons.append(
                    f"{LEVEL_LABELS[level]}: missing; using optional fallback "
                    f"{self.profile.fallback_folder}"
                )
            else:
                skipped.append(LEVEL_LABELS[level])
                reasons.append(
                    f"{LEVEL_LABELS[level]}: missing; sorting continues at the "
                    "nearest resolved parent"
                )
        destination=self.root.joinpath(*components)
        unresolved=len(missing)+len(skipped)
        confidence=max(.25,1-(unresolved/max(len(levels),1))*.65)
        return OrganizationSuggestion(
            destination,components,confidence,reasons,missing,skipped
        )
