"""Evaluate rule-based collection membership from the bounded Dataset cache."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Callable

from .dataset_search_projection import DatasetSearchProjectionStore,DatasetSearchSpec
from .tag_collection import normalize_tag


RULE_SCHEMA_VERSION=1
MAX_TERMS_PER_LIST=64
MAX_TERM_LENGTH=120
MAX_RULE_IMAGES=50_000
CONFIRMED_SOURCE="Confirmed Image Knowledge"
_PROJECTION_STORE=DatasetSearchProjectionStore()


@dataclass(frozen=True)
class RuleUnavailable:
    identity: str
    reason: str


@dataclass(frozen=True)
class RulePreview:
    rules: dict
    paths: tuple[Path,...]
    unavailable: tuple[RuleUnavailable,...]
    source_token: tuple[object,...]
    memory_token: tuple[object,...]

    @property
    def unavailable_count(self) -> int:
        return len(self.unavailable)


def _terms(value,*,tags: bool) -> list[str]:
    if not isinstance(value,list) or len(value)>MAX_TERMS_PER_LIST:
        raise ValueError("Collection rules need a bounded list of terms.")
    result=[]
    for raw in value:
        if not isinstance(raw,str):
            raise ValueError("Collection rule terms must be text.")
        text=(normalize_tag(raw) if tags else " ".join(raw.split()).casefold())
        if not text or len(text)>MAX_TERM_LENGTH:
            raise ValueError("Collection rule terms must be nonempty and bounded.")
        result.append(text)
    return sorted(set(result))


def normalize_collection_rules(value) -> dict:
    """Validate and canonicalize one portable rule definition without matching it."""
    if not isinstance(value,dict) or value.get("schema_version")!=RULE_SCHEMA_VERSION:
        raise ValueError("Collection rules must use schema version 1.")
    raw_libraries=value.get("library_ids")
    if not isinstance(raw_libraries,list) or not raw_libraries:
        raise ValueError("Collection rules need at least one named library.")
    libraries=[]
    for raw in raw_libraries:
        if not isinstance(raw,str):
            raise ValueError("Collection rule library IDs must be text.")
        library_id=raw.strip()
        if not library_id or len(library_id)>200:
            raise ValueError("Collection rule library IDs must be bounded nonempty text.")
        libraries.append(library_id)
    mode=str(value.get("match_mode") or "")
    if mode not in {"all","any"}:
        raise ValueError("Collection rules must match all or any positive criterion.")
    rules={
        "schema_version":RULE_SCHEMA_VERSION,
        "library_ids":sorted(set(libraries)),
        "match_mode":mode,
        "include_tags":_terms(value.get("include_tags",[]),tags=True),
        "exclude_tags":_terms(value.get("exclude_tags",[]),tags=True),
        "include_words":_terms(value.get("include_words",[]),tags=False),
        "exclude_words":_terms(value.get("exclude_words",[]),tags=False),
    }
    if not rules["include_tags"] and not rules["include_words"]:
        raise ValueError("Collection rules need at least one include tag or description term.")
    return rules


def _source_is_usable(token) -> bool:
    if isinstance(token,(tuple,list)):
        return all(_source_is_usable(value) for value in token)
    return token != -1


def _library_status(dataset,library_id: str) -> str:
    profile=getattr(dataset,"profile",None)
    if not callable(profile):
        return "not_loaded"
    if profile(library_id) is None:
        return "missing"
    scoped=getattr(dataset,"scope_library_ids",None)
    if scoped is not None:
        try:
            active={str(value) for value in scoped}
        except TypeError:
            active=None
        if active is not None and library_id not in active:
            return "outside_active_scope"
    availability=getattr(dataset,"library_availability",None)
    if not callable(availability):
        return "not_loaded"
    return str(availability(library_id) or "not_loaded")


def _path_is_readable(dataset,path: Path) -> tuple[bool,str]:
    availability=getattr(dataset,"availability_for_path",None)
    if callable(availability):
        state=str(availability(path) or "not_loaded")
        if state!="current":
            return False,state
    storage=getattr(dataset,"storage_availability",None)
    if callable(storage):
        status=storage(path)
        if not bool(getattr(status,"readable",True)):
            return False,str(getattr(status,"detail","") or "unreadable")
    return True,""


def _identity_for(dataset,path: Path):
    provider=getattr(dataset,"identity_for_path",None)
    return provider(path) if callable(provider) else None


def _confirmed_tags(record) -> set[str]:
    sources=getattr(record,"tag_sources",{}) or {}
    result=set()
    for raw in getattr(record,"tags",()) or ():
        tag=normalize_tag(raw)
        values=sources.get(raw,sources.get(tag,())) if isinstance(sources,dict) else ()
        if isinstance(values,list) and CONFIRMED_SOURCE in values:
            result.add(tag)
    return result


def _description_matches(descriptions,terms) -> bool:
    if not terms:return False
    values=tuple(str(value).casefold() for value in (descriptions or ()))
    return any(
        re.search(r"(?<!\w)"+re.escape(term)+r"(?!\w)",description)
        for term in terms for description in values
    )


def _matches(rules,record) -> bool:
    tags=_confirmed_tags(record)
    positives=(
        [tag in tags for tag in rules["include_tags"]]
        + [_description_matches(record.plain_language,(term,))
           for term in rules["include_words"]]
    )
    positive=all(positives) if rules["match_mode"]=="all" else any(positives)
    if not positive:return False
    return not (
        bool(set(rules["exclude_tags"]).intersection(tags))
        or _description_matches(record.plain_language,rules["exclude_words"])
    )


def _current_tokens(dataset) -> tuple[tuple[object,...],tuple[object,...]]:
    store=DatasetSearchProjectionStore()
    index=getattr(dataset,"index",None)
    return (
        store._source_token(dataset,index,getattr(dataset,"images",())),
        store.memory_token(dataset),
    )


def rule_preview_is_current(preview: RulePreview,dataset) -> bool:
    """Refuse an auto-membership write after cached evidence or availability changes."""
    if not isinstance(preview,RulePreview) or not _source_is_usable(preview.source_token):
        return False
    source,memory=_current_tokens(dataset)
    return source==preview.source_token and memory==preview.memory_token


def preview_collection_rules(
    rules,dataset,*,progress: Callable[[int,int,str],None]|None=None,
    should_cancel: Callable[[],bool]|None=None,
) -> RulePreview:
    """Match confirmed cached Image Knowledge without opening source image files."""
    normalized=normalize_collection_rules(rules)
    cancel=should_cancel or (lambda:False)
    if cancel(): raise InterruptedError("Collection rule preview cancelled")
    if len(tuple(getattr(dataset,"images",()) or ()))>MAX_RULE_IMAGES:
        raise ValueError("Collection rule previews support at most 50,000 cached images.")
    projection=_PROJECTION_STORE.project(
        dataset,DatasetSearchSpec(),readiness_cache={},should_continue=lambda:not cancel(),
    )
    if len(projection.paths)>MAX_RULE_IMAGES:
        raise ValueError("Collection rule previews support at most 50,000 cached images.")
    unavailable=[]
    if not _source_is_usable(projection.source_token):
        unavailable.append(RuleUnavailable("dataset","Saved evidence snapshot is unavailable."))
    requested=set(normalized["library_ids"])
    for library_id in normalized["library_ids"]:
        status=_library_status(dataset,library_id)
        if status!="current":
            unavailable.append(RuleUnavailable(
                f"library:{library_id}",
                (
                    "Required library is missing."
                    if status=="missing" else
                    "Required library is outside the active Dataset scope."
                    if status=="outside_active_scope" else
                    f"Required library is {status}."
                ),
            ))
    matched=[]
    total=len(projection.paths)
    for position,raw_path in enumerate(projection.paths,1):
        if position%128==0 and cancel():
            raise InterruptedError("Collection rule preview cancelled")
        path=Path(raw_path)
        identity=_identity_for(dataset,path)
        library_id=str(getattr(identity,"library_id","") or "")
        if identity is None or not library_id:
            unavailable.append(RuleUnavailable(
                str(path),"The source library for this cached row is unavailable."
            ))
            continue
        if library_id not in requested:
            continue
        readable,reason=_path_is_readable(dataset,path)
        record=projection.records.get(raw_path)
        if not readable:
            unavailable.append(RuleUnavailable(str(path),reason or "Source is unavailable."))
        elif record is None or not bool(getattr(record,"sidecar_exists",False)):
            unavailable.append(RuleUnavailable(str(path),"Confirmed Image Knowledge is unavailable."))
        elif str(getattr(record,"error","") or ""):
            unavailable.append(RuleUnavailable(str(path),"Cached Image Knowledge has an error."))
        elif _matches(normalized,record):
            matched.append(path)
        if progress is not None and (position%128==0 or position==total):
            progress(position,total,"Matching cached Collection rules")
    preview=RulePreview(
        normalized,tuple(matched),tuple(unavailable),projection.source_token,
        projection.memory_token,
    )
    if cancel(): raise InterruptedError("Collection rule preview cancelled")
    if not rule_preview_is_current(preview,dataset):
        raise InterruptedError("Collection rule source changed")
    return preview
