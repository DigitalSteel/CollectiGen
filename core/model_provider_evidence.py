"""Opt-in, read-only provider evidence for explicitly supplied model queries.

Provider claims remain dated external evidence.  This module never scans local
resources, downloads model weights, or mutates Model/System/Image Knowledge.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import socket
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime,timezone
from pathlib import Path
from typing import Callable,Mapping
from urllib.error import HTTPError,URLError
from urllib.parse import parse_qs,parse_qsl,quote,unquote,urlencode,urljoin,urlparse,urlunparse
from urllib.request import HTTPRedirectHandler,Request,build_opener

from .external_write_transaction import atomic_verified_bytes
from .training_source_io import _assert_unredirected,_identity


SCHEMA_VERSION=1
CACHE_MAX_BYTES=4*1024*1024
CACHE_MAX_ENTRIES=512
MAX_RESPONSE_BYTES=2*1024*1024
MAX_QUERY_CHARS=256
MAX_SEARCH_RESULTS=20
MAX_REDIRECTS=3
REQUEST_TIMEOUT_SECONDS=10.0
DEFAULT_CACHE_MAX_AGE_SECONDS=7*24*60*60
ADAPTER_VERSIONS={"civitai":"civitai-api-v1@1","hugging_face":"hf-hub-api@1"}
_SHA256=re.compile(r"[0-9a-fA-F]{64}\Z")
_INTEGER=re.compile(r"[0-9]+\Z")
_HF_SEGMENT=re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z")
_HOSTS={
    "civitai":frozenset(("civitai.com","www.civitai.com")),
    "hugging_face":frozenset(("huggingface.co","www.huggingface.co")),
}


class ProviderEvidenceError(ValueError):
    pass


class ProviderConfirmationRequired(ProviderEvidenceError):
    pass


@dataclass(frozen=True,slots=True)
class ProviderQuery:
    provider: str
    kind: str
    value: str


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        return None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _cancelled(value) -> bool:
    if value is None:
        return False
    if callable(value):
        return bool(value())
    checker=getattr(value,"is_set",None)
    return bool(checker()) if callable(checker) else bool(value)


def _check_cancelled(value) -> None:
    if _cancelled(value):
        raise InterruptedError("Provider evidence lookup was cancelled.")


def _clean_text(value,limit: int=4096) -> str|None:
    if value is None:
        return None
    result=str(value).strip()
    return result[:limit] if result else None


def _string_list(value,limit: int=256) -> list[str]:
    if isinstance(value,str):
        value=[value]
    if not isinstance(value,list):
        return []
    result=[]
    for item in value[:limit]:
        text=_clean_text(item,512)
        if text is not None and text not in result:
            result.append(text)
    return result


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")


def _safe_url(url: str,provider: str) -> str:
    parsed=urlparse(url)
    try:
        port=parsed.port
    except ValueError as exc:
        raise ProviderEvidenceError("The provider URL has an invalid port.") from exc
    if parsed.scheme!="https" or parsed.username or parsed.password or port not in (None,443):
        raise ProviderEvidenceError("Provider URLs must use ordinary HTTPS without credentials or a custom port.")
    if (parsed.hostname or "").lower() not in _HOSTS[provider]:
        raise ProviderEvidenceError("The URL is outside the selected provider host.")
    if len(url)>2048:
        raise ProviderEvidenceError("The provider URL is too long.")
    return url


def _strict_json(raw: bytes):
    def pairs(values):
        result={}
        for key,value in values:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key]=value
        return result
    return json.loads(raw.decode("utf-8"),object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def _assert_cache_path(path: Path) -> None:
    try:
        _assert_unredirected(path)
    except RuntimeError as exc:
        raise ProviderEvidenceError("The provider evidence cache cannot use a redirected path.") from exc


def _bounded_cache_read(path: Path) -> bytes|None:
    target=path.absolute(); _assert_cache_path(target)
    try:
        before_path=target.stat(follow_symlinks=False)
        descriptor=os.open(target,os.O_RDONLY|getattr(os,"O_BINARY",0)|getattr(os,"O_NOFOLLOW",0))
    except FileNotFoundError:
        return None
    try:
        before=os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or _identity(before)!=_identity(before_path):
            raise ProviderEvidenceError("The provider evidence cache is not a stable regular file.")
        chunks=[]; size=0
        while size<=CACHE_MAX_BYTES:
            chunk=os.read(descriptor,min(65536,CACHE_MAX_BYTES+1-size))
            if not chunk:
                break
            chunks.append(chunk); size+=len(chunk)
        after=os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after_path=target.stat(follow_symlinks=False); _assert_cache_path(target)
    if _identity(before)!=_identity(after) or _identity(after)!=_identity(after_path):
        raise ProviderEvidenceError("The provider evidence cache changed while it was read.")
    raw=b"".join(chunks)
    if len(raw)>CACHE_MAX_BYTES:
        raise ProviderEvidenceError("The provider evidence cache exceeds its supported size limit.")
    return raw


def _metadata_url(value,provider: str) -> str|None:
    text=_clean_text(value,2048)
    if not text:
        return None
    try:
        return _safe_url(text,provider)
    except ProviderEvidenceError:
        return None


def _repo_id(value: str) -> str:
    pieces=value.strip().strip("/").split("/")
    if len(pieces)!=2 or not all(_HF_SEGMENT.fullmatch(part) for part in pieces):
        raise ProviderEvidenceError("A Hugging Face repository must be an explicit owner/name identity.")
    return "/".join(pieces)


def _normalize(query: ProviderQuery) -> ProviderQuery:
    if not isinstance(query,ProviderQuery):
        raise ProviderEvidenceError("A ProviderQuery is required.")
    provider=query.provider.strip().lower().replace("-","_")
    if provider in ("huggingface","hf"):
        provider="hugging_face"
    if provider not in ADAPTER_VERSIONS:
        raise ProviderEvidenceError("The provider must be Civitai or Hugging Face.")
    kind=query.kind.strip().lower()
    value=query.value.strip()
    if not value or len(value)>MAX_QUERY_CHARS:
        raise ProviderEvidenceError("The explicit provider query is empty or too long.")
    allowed={"civitai":{"model_id","version_id","url","sha256","search"},
             "hugging_face":{"repo_id","url","search"}}[provider]
    if kind not in allowed:
        raise ProviderEvidenceError(f"{kind!r} is not a supported {provider} query kind.")
    if kind in ("model_id","version_id"):
        if not _INTEGER.fullmatch(value) or int(value)<1:
            raise ProviderEvidenceError("Provider IDs must be positive decimal integers.")
        value=str(int(value))
    elif kind=="sha256":
        if provider!="civitai" or not _SHA256.fullmatch(value):
            raise ProviderEvidenceError("Civitai hash lookup requires an exact 64-character SHA-256.")
        value=value.lower()
    elif kind=="repo_id":
        value=_repo_id(value)
    elif kind=="url":
        value=_safe_url(value,provider)
        parsed=urlparse(value)
        if parsed.fragment:
            raise ProviderEvidenceError("Provider URL fragments are not part of an exact model query.")
        parameters=parse_qsl(parsed.query,keep_blank_values=True)
        if provider=="hugging_face" and parameters:
            raise ProviderEvidenceError("Hugging Face model URLs cannot include query parameters.")
        if provider=="civitai" and any(key!="modelVersionId" for key,_item in parameters):
            raise ProviderEvidenceError("Civitai model URLs can include only modelVersionId; token and tracking parameters are rejected.")
        if sum(key=="modelVersionId" for key,_item in parameters)>1:
            raise ProviderEvidenceError("The Civitai URL has an ambiguous model version query.")
        clean_query=urlencode(parameters)
        value=urlunparse(("https",(parsed.hostname or "").lower(),parsed.path,"",clean_query,""))
    return ProviderQuery(provider,kind,value)


def _query_target(query: ProviderQuery) -> tuple[str,dict[str,str|None]]:
    """Return the API URL and explicit page/revision/file identity."""
    if query.provider=="civitai":
        if query.kind=="model_id":
            return f"https://civitai.com/api/v1/models/{query.value}",{"model_id":query.value}
        if query.kind=="version_id":
            return f"https://civitai.com/api/v1/model-versions/{query.value}",{"version_id":query.value}
        if query.kind=="sha256":
            return f"https://civitai.com/api/v1/model-versions/by-hash/{query.value}",{"sha256":query.value}
        if query.kind=="search":
            raise ProviderEvidenceError("Use search() for provider searches.")
        parsed=urlparse(query.value); parts=[part for part in parsed.path.split("/") if part]
        args=parse_qs(parsed.query)
        if len(parts)>=2 and parts[0]=="models" and _INTEGER.fullmatch(parts[1]) and int(parts[1])>0:
            version=(args.get("modelVersionId") or [None])[0]
            if version is not None:
                if not _INTEGER.fullmatch(version) or int(version)<1:
                    raise ProviderEvidenceError("The Civitai URL has an invalid model version ID.")
                return f"https://civitai.com/api/v1/model-versions/{int(version)}",{"model_id":str(int(parts[1])),"version_id":str(int(version))}
            return f"https://civitai.com/api/v1/models/{int(parts[1])}",{"model_id":str(int(parts[1]))}
        if len(parts)>=4 and parts[:3]==["api","v1","model-versions"] and _INTEGER.fullmatch(parts[3]) and int(parts[3])>0:
            return f"https://civitai.com/api/v1/model-versions/{int(parts[3])}",{"version_id":str(int(parts[3]))}
        if len(parts)>=4 and parts[:3]==["api","v1","models"] and _INTEGER.fullmatch(parts[3]) and int(parts[3])>0:
            return f"https://civitai.com/api/v1/models/{int(parts[3])}",{"model_id":str(int(parts[3]))}
        raise ProviderEvidenceError("The Civitai URL must identify one model or model version.")
    if query.kind=="search":
        raise ProviderEvidenceError("Use search() for provider searches.")
    revision=None; filename=None
    if query.kind=="repo_id":
        repo=query.value
    else:
        parsed=urlparse(query.value); parts=[part for part in parsed.path.split("/") if part]
        if len(parts)<2:
            raise ProviderEvidenceError("The Hugging Face URL must identify one model repository.")
        repo=_repo_id("/".join(parts[:2]))
        if len(parts)>2:
            if parts[2] not in ("tree","blob","resolve") or len(parts)<4:
                raise ProviderEvidenceError("The Hugging Face URL has an unsupported model path.")
            revision=unquote(parts[3])
            if not revision or len(revision)>128:
                raise ProviderEvidenceError("The Hugging Face revision is invalid.")
            filename=unquote("/".join(parts[4:])) or None
    api=f"https://huggingface.co/api/models/{quote(repo,safe='/')}"
    if revision:
        api+=f"/revision/{quote(revision,safe='')}"
    # Hugging Face's model_info(files_metadata=True) maps this documented
    # option to blobs=true so siblings include exact size and LFS metadata.
    api+="?blobs=true"
    return api,{"repo_id":repo,"revision":revision,"filename":filename}


def _credentials_headers(provider: str,credentials) -> dict[str,str]:
    if credentials is None:
        return {}
    if isinstance(credentials,str):
        token=credentials.strip()
    elif isinstance(credentials,Mapping):
        token=str(credentials.get(provider) or credentials.get("token") or "").strip()
    else:
        raise ProviderEvidenceError("Credentials must be an in-memory token or mapping.")
    return {"Authorization":f"Bearer {token}"} if token else {}


def _default_fetch(url: str,headers: Mapping[str,str],timeout: float,cancelled):
    opener=build_opener(_NoRedirect())
    current=url
    for redirect in range(MAX_REDIRECTS+1):
        _check_cancelled(cancelled)
        request=Request(current,headers={"Accept":"application/json","User-Agent":"CollectiGen-provider-evidence/1",**dict(headers)})
        try:
            response=opener.open(request,timeout=timeout)
        except HTTPError as exc:
            if exc.code in (301,302,303,307,308):
                location=exc.headers.get("Location")
                if not location or redirect==MAX_REDIRECTS:
                    raise ProviderEvidenceError("The provider redirect limit was exceeded.")
                candidate=urljoin(current,location)
                provider="civitai" if (urlparse(url).hostname or "").lower() in _HOSTS["civitai"] else "hugging_face"
                current=_safe_url(candidate,provider)
                continue
            body=exc.read(MAX_RESPONSE_BYTES+1)
            return exc.code,dict(exc.headers.items()),body,current
        with response:
            body=bytearray()
            while True:
                _check_cancelled(cancelled)
                chunk=response.read(min(65536,MAX_RESPONSE_BYTES+1-len(body)))
                if not chunk:
                    break
                body.extend(chunk)
                if len(body)>MAX_RESPONSE_BYTES:
                    raise ProviderEvidenceError("The provider response exceeds the supported size limit.")
            return response.status,dict(response.headers.items()),bytes(body),current
    raise ProviderEvidenceError("The provider redirect limit was exceeded.")


def _hashes(files) -> list[dict]:
    result=[]
    if not isinstance(files,list):
        return result
    for item in files[:256]:
        if not isinstance(item,dict):
            continue
        hashes=item.get("hashes") if isinstance(item.get("hashes"),dict) else {}
        for algorithm,value in hashes.items():
            text=_clean_text(value,256)
            if text:
                canonical=str(algorithm).upper()
                result.append({"algorithm":canonical,"value":text.lower(),"scope":"full_file" if canonical=="SHA256" and bool(_SHA256.fullmatch(text)) else "provider_hash","provider_reported":True})
    return result


def _civitai_version(version: dict,model: dict|None=None) -> dict:
    model=model if isinstance(model,dict) else (version.get("model") if isinstance(version.get("model"),dict) else {})
    files=[]
    for item in version.get("files",[])[:256] if isinstance(version.get("files"),list) else []:
        if not isinstance(item,dict):
            continue
        reported_hashes=_hashes([item])
        exact_sha=next((value["value"] for value in reported_hashes if value["algorithm"]=="SHA256" and value["scope"]=="full_file"),None)
        filename=_clean_text(item.get("name"),512)
        files.append({
            "id":item.get("id"),"name":filename,"filename":filename,
            "role":_clean_text(item.get("type"),128),
            "size_bytes":int(float(item["sizeKB"])*1024) if type(item.get("sizeKB")) in (int,float) and math.isfinite(item["sizeKB"]) and item["sizeKB"]>=0 else None,
            "download_url":_metadata_url(item.get("downloadUrl") or version.get("downloadUrl"),"civitai"),
            "sha256":exact_sha,"hash_scope":"full_file" if exact_sha else "unknown",
            "hashes":reported_hashes,"format":_clean_text((item.get("metadata") or {}).get("format"),128) if isinstance(item.get("metadata"),dict) else None,
        })
    model_id=version.get("modelId") or model.get("id")
    return {
        "provider_model_id":model_id,"provider_version_id":version.get("id"),"version_id":version.get("id"),
        "model_name":_clean_text(model.get("name"),512),"version_name":_clean_text(version.get("name"),512),
        "role":_clean_text(model.get("type"),128),"base_model":_clean_text(version.get("baseModel"),256),
        "trigger_words":_string_list(version.get("trainedWords")),
        "content_rating":{"nsfw":model.get("nsfw") if type(model.get("nsfw")) is bool else None,"poi":model.get("poi") if type(model.get("poi")) is bool else None,
                          "nsfw_level":version.get("nsfwLevel",model.get("nsfwLevel")),"minor":model.get("minor") if type(model.get("minor")) is bool else None,"sfw_only":model.get("sfwOnly") if type(model.get("sfwOnly")) is bool else None},
        "creator":_clean_text((model.get("creator") or {}).get("username"),256) if isinstance(model.get("creator"),dict) else None,
        "availability":_clean_text(model.get("mode") or version.get("availability") or version.get("status"),128) or "published",
        "terms":{key:model.get(key) for key in ("allowNoCredit","allowCommercialUse","allowDerivatives","allowDifferentLicense") if key in model},
        "license":_clean_text(model.get("license"),256),"usage_control":_clean_text(version.get("usageControl"),128),"description":_clean_text(version.get("description"),8192),
        "page_url":f"https://civitai.com/models/{model_id}?modelVersionId={version.get('id')}" if model_id and version.get("id") else None,
        "created_at":_clean_text(version.get("createdAt"),128),"updated_at":_clean_text(version.get("updatedAt"),128),"files":files,
    }


def _parse_civitai(payload,identity: dict,search: bool=False) -> list[dict]:
    if not isinstance(payload,dict):
        raise ProviderEvidenceError("Civitai returned a malformed response.")
    values=payload.get("items") if search else [payload]
    if not isinstance(values,list):
        raise ProviderEvidenceError("Civitai returned a malformed model list.")
    results=[]
    for model in values[:MAX_SEARCH_RESULTS]:
        if not isinstance(model,dict):
            raise ProviderEvidenceError("Civitai returned a malformed model item.")
        versions=model.get("modelVersions")
        if isinstance(versions,list):
            for version in versions[:64]:
                if isinstance(version,dict):
                    results.append(_civitai_version(version,model))
        elif model.get("id") is not None and model.get("modelId") is not None and isinstance(model.get("model"),dict):
            results.append(_civitai_version(model))
    if not results:
        raise ProviderEvidenceError("Civitai returned no identifiable model versions.")
    return results


def _parse_hf(payload,identity: dict,search: bool=False) -> list[dict]:
    values=payload if search else [payload]
    if not isinstance(values,list):
        raise ProviderEvidenceError("Hugging Face returned a malformed response.")
    results=[]
    for model in values[:MAX_SEARCH_RESULTS]:
        if not isinstance(model,dict):
            raise ProviderEvidenceError("Hugging Face returned a malformed model item.")
        repo=_clean_text(model.get("id") or model.get("modelId"),256)
        if not repo:
            raise ProviderEvidenceError("Hugging Face omitted the repository identity.")
        card=model.get("cardData") if isinstance(model.get("cardData"),dict) else {}
        files=[]
        for sibling in model.get("siblings",[])[:512] if isinstance(model.get("siblings"),list) else []:
            if not isinstance(sibling,dict):
                continue
            lfs=sibling.get("lfs") if isinstance(sibling.get("lfs"),dict) else {}
            sha=_clean_text(lfs.get("sha256") or lfs.get("oid"),256)
            hashes=[]
            if sha:
                if sha.startswith("sha256:"):
                    sha=sha[7:]
                hashes=[{"algorithm":"SHA256" if _SHA256.fullmatch(sha) else "LFS_OID","value":sha.lower(),"scope":"full_file" if _SHA256.fullmatch(sha) else "provider_hash","provider_reported":True}]
            filename=_clean_text(sibling.get("rfilename"),1024)
            if identity.get("filename") and filename!=identity["filename"]:
                continue
            revision=_clean_text(model.get("sha"),128) or identity.get("revision") or "main"
            exact_sha=next((value["value"] for value in hashes if value["algorithm"]=="SHA256" and value["scope"]=="full_file"),None)
            files.append({"name":filename,"filename":filename,"size_bytes":lfs.get("size") if type(lfs.get("size")) is int else sibling.get("size") if type(sibling.get("size")) is int else None,
                          "sha256":exact_sha,"hash_scope":"full_file" if exact_sha else "unknown","hashes":hashes,
                          "download_url":_metadata_url(f"https://huggingface.co/{repo}/resolve/{quote(str(revision),safe='')}/{quote(filename,safe='/')}","hugging_face") if filename else None})
        tags=_string_list(model.get("tags"))
        license_value=_clean_text(card.get("license"),256)
        if not license_value:
            license_value=next((tag[8:] for tag in tags if tag.startswith("license:")),None)
        results.append({
            "repo_id":repo,"revision":_clean_text(model.get("sha"),128) or identity.get("revision"),
            "model_name":repo.rsplit("/",1)[-1],"creator":_clean_text(model.get("author"),256) or repo.split("/",1)[0],
            "role":_clean_text(model.get("pipeline_tag"),128),"base_model":card.get("base_model"),
            "trigger_words":_string_list(card.get("trigger_words") or card.get("instance_prompt")),
            "content_rating":{"tags":[tag for tag in tags if tag in ("nsfw","not-for-all-audiences")]},
            "license":license_value,"terms":{"gated":model.get("gated",False),"private":model.get("private",False)},
            "availability":"disabled" if model.get("disabled") else "gated" if model.get("gated") else "published",
            "page_url":f"https://huggingface.co/{repo}","last_modified":_clean_text(model.get("lastModified"),128),
            "description":_clean_text(card.get("model_description") or card.get("description"),8192),
            "description_status":"available" if card.get("model_description") or card.get("description") else "unavailable_from_model_info",
            "tags":tags,"files":files,
        })
    return results


def _validate_identity(query: ProviderQuery,identity: dict,items: list[dict]) -> str:
    """Reject provider drift and label a specifically requested missing file."""
    if query.provider=="civitai":
        expected_version=identity.get("version_id")
        expected_model=identity.get("model_id")
        if expected_version and (
            len(items)!=1 or str(items[0].get("provider_version_id"))!=expected_version
        ):
            raise ProviderEvidenceError("Civitai returned a different or ambiguous model version.")
        if expected_model and any(str(item.get("provider_model_id"))!=expected_model for item in items):
            raise ProviderEvidenceError("Civitai returned a different model identity.")
        expected_hash=identity.get("sha256")
        if expected_hash:
            matching=[]
            for item in items:
                for file in item.get("files",[]):
                    if any(value.get("algorithm")=="SHA256" and value.get("value")==expected_hash for value in file.get("hashes",[])):
                        matching.append(item)
                        break
            if len(matching)!=1:
                raise ProviderEvidenceError("Civitai returned a changed or ambiguous SHA-256 match.")
    else:
        expected_repo=identity.get("repo_id")
        if expected_repo and (
            len(items)!=1 or str(items[0].get("repo_id","")).casefold()!=str(expected_repo).casefold()
        ):
            raise ProviderEvidenceError("Hugging Face returned a different repository identity.")
        if identity.get("filename") and items and not items[0].get("files"):
            if items[0].get("availability")!="gated":
                return "changed"
    return "available"


class ProviderEvidenceService:
    def __init__(self,cache_path: str|Path,*,fetcher: Callable|None=None,clock: Callable[[],datetime]|None=None,
                 cache_max_age_seconds: int=DEFAULT_CACHE_MAX_AGE_SECONDS) -> None:
        self.cache_path=Path(cache_path).absolute()
        self._fetcher=fetcher or _default_fetch
        self._clock=clock or _now_utc
        self.cache_max_age_seconds=max(0,int(cache_max_age_seconds))

    def _cache(self) -> dict:
        raw=_bounded_cache_read(self.cache_path)
        if raw is None:
            return {"schema_version":SCHEMA_VERSION,"entries":{}}
        try:
            value=_strict_json(raw)
        except (ValueError,UnicodeError,RecursionError) as exc:
            raise ProviderEvidenceError("The provider evidence cache is malformed.") from exc
        if not isinstance(value,dict) or value.get("schema_version")!=SCHEMA_VERSION or not isinstance(value.get("entries"),dict):
            raise ProviderEvidenceError("The provider evidence cache schema is unsupported.")
        if len(value["entries"])>CACHE_MAX_ENTRIES or any(
            not _SHA256.fullmatch(str(key)) or not isinstance(entry,dict)
            or not isinstance(entry.get("stored_at"),str)
            or not isinstance(entry.get("result"),dict)
            for key,entry in value["entries"].items()
        ):
            raise ProviderEvidenceError("The provider evidence cache contains invalid or excessive entries.")
        return value

    @contextmanager
    def _writer(self):
        parent=self.cache_path.absolute().parent; _assert_cache_path(parent)
        parent.mkdir(parents=True,exist_ok=True)
        lock=self.cache_path.with_name(self.cache_path.name+".lock").absolute()
        _assert_cache_path(lock)
        descriptor=os.open(lock,os.O_RDWR|os.O_CREAT|getattr(os,"O_BINARY",0)|getattr(os,"O_NOFOLLOW",0),0o600)
        with os.fdopen(descriptor,"r+b") as handle:
            state=os.fstat(handle.fileno())
            if not stat.S_ISREG(state.st_mode) or _identity(state)!=_identity(lock.stat(follow_symlinks=False)):
                raise ProviderEvidenceError("The provider evidence cache lock is not a stable regular file.")
            if state.st_size==0:
                handle.write(b"0"); handle.flush(); os.fsync(handle.fileno())
            handle.seek(0)
            try:
                if os.name=="nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError as exc:
                raise ProviderEvidenceError("Another provider evidence cache update is active; retry shortly.") from exc
            try:
                yield
            finally:
                handle.seek(0)
                if os.name=="nt":
                    msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
                else:
                    fcntl.flock(handle.fileno(),fcntl.LOCK_UN)

    @staticmethod
    def _key(query: ProviderQuery,operation: str,limit: int|None=None) -> str:
        raw=json.dumps({"provider":query.provider,"kind":query.kind,"value":query.value,"operation":operation,"limit":limit},sort_keys=True,separators=(",",":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _cached(self,key: str):
        entry=self._cache()["entries"].get(key)
        if not isinstance(entry,dict) or not isinstance(entry.get("result"),dict):
            return None
        result=json.loads(json.dumps(entry["result"]))
        try:
            fetched=datetime.fromisoformat(str(result["source"]["retrieved_at"]).replace("Z","+00:00"))
            age=max(0,(self._clock().astimezone(timezone.utc)-fetched.astimezone(timezone.utc)).total_seconds())
        except (KeyError,TypeError,ValueError):
            raise ProviderEvidenceError("The cached provider evidence has invalid provenance.")
        result["currentness"]="current" if age<=self.cache_max_age_seconds else "stale"
        result["cache"]={"used":True,"age_seconds":int(age)}
        return result

    def _store(self,key: str,result: dict) -> None:
        with self._writer():
            cache=self._cache(); entries=cache["entries"]
            entries[key]={"stored_at":_iso(self._clock()),"result":result}
            if len(entries)>CACHE_MAX_ENTRIES:
                ordered=sorted(entries.items(),key=lambda pair:str(pair[1].get("stored_at","")))
                for old,_value in ordered[:len(entries)-CACHE_MAX_ENTRIES]:
                    entries.pop(old,None)
            payload=(json.dumps(cache,sort_keys=True,ensure_ascii=False,allow_nan=False)+"\n").encode("utf-8")
            if len(payload)>CACHE_MAX_BYTES:
                raise ProviderEvidenceError("The provider evidence cache would exceed its supported size limit.")
            atomic_verified_bytes(self.cache_path,payload,label="Provider evidence cache")

    def _enrich_civitai(self,item: dict,*,cancelled,credentials) -> tuple[dict|None,str|None]:
        """Fetch the parent model when version evidence omits creator/terms."""
        model_id=item.get("provider_model_id")
        if not model_id or (item.get("creator") and item.get("terms")):
            return None,None
        url=_safe_url(f"https://civitai.com/api/v1/models/{model_id}","civitai")
        try:
            status,_headers,body,final_url=self._fetcher(
                url,_credentials_headers("civitai",credentials),REQUEST_TIMEOUT_SECONDS,cancelled,
            )
            _check_cancelled(cancelled); _safe_url(final_url,"civitai")
            if status!=200 or len(body)>MAX_RESPONSE_BYTES:
                return None,f"Parent model metadata returned HTTP {status}."
            model=_strict_json(body)
            if not isinstance(model,dict) or str(model.get("id"))!=str(model_id) or not isinstance(model.get("modelVersions"),list):
                return None,"Parent model metadata did not match the requested model identity."
        except InterruptedError:
            raise
        except (ValueError,UnicodeError,RecursionError,ProviderEvidenceError,URLError,socket.timeout,TimeoutError,OSError) as exc:
            return None,str(exc)[:512]
        item["creator"]=_clean_text((model.get("creator") or {}).get("username"),256) if isinstance(model.get("creator"),dict) else item.get("creator")
        item["terms"]={key:model.get(key) for key in ("allowNoCredit","allowCommercialUse","allowDerivatives","allowDifferentLicense") if key in model}
        item["license"]=_clean_text(model.get("license"),256)
        rating=item["content_rating"]
        for key in ("nsfw","poi","minor","sfwOnly","nsfwLevel"):
            if key in model:
                rating[{"sfwOnly":"sfw_only","nsfwLevel":"nsfw_level"}.get(key,key)]=model[key]
        item["role"]=_clean_text(model.get("type"),128) or item.get("role")
        item["model_name"]=_clean_text(model.get("name"),512) or item.get("model_name")
        item["availability"]=_clean_text(model.get("mode") or model.get("availability"),128) or item.get("availability")
        return {"api_url":final_url,"state":"available"},None

    def lookup(self,query: ProviderQuery,*,allow_network: bool=False,confirmed: bool=False,cancelled=None,credentials=None) -> dict:
        return self._request(_normalize(query),"lookup",None,allow_network,confirmed,cancelled,credentials)

    def search(self,query: ProviderQuery,*,limit: int=20,allow_network: bool=False,confirmed: bool=False,cancelled=None,credentials=None) -> dict:
        normalized=_normalize(query)
        if normalized.kind!="search":
            raise ProviderEvidenceError("search() requires an explicit search query kind.")
        if type(limit) is not int or not 1<=limit<=MAX_SEARCH_RESULTS:
            raise ProviderEvidenceError(f"Search limits must be from 1 to {MAX_SEARCH_RESULTS}.")
        return self._request(normalized,"search",limit,allow_network,confirmed,cancelled,credentials)

    def _request(self,query,operation,limit,allow_network,confirmed,cancelled,credentials):
        _check_cancelled(cancelled)
        key=self._key(query,operation,limit); cached=self._cached(key)
        if not allow_network:
            if cached is not None:
                return cached
            return self._unavailable(query,operation,"offline","Network access was not enabled for this explicit query.")
        if not confirmed:
            raise ProviderConfirmationRequired("Confirm the displayed provider and exact query before network access.")
        if operation=="search":
            if query.provider=="civitai":
                url="https://civitai.com/api/v1/models?"+urlencode({"query":query.value,"limit":limit})
            else:
                url="https://huggingface.co/api/models?"+urlencode({"search":query.value,"limit":limit,"full":"true"})
            identity={"search_text":query.value}
        else:
            url,identity=_query_target(query)
        url=_safe_url(url,query.provider)
        try:
            status,_headers,body,final_url=self._fetcher(url,_credentials_headers(query.provider,credentials),REQUEST_TIMEOUT_SECONDS,cancelled)
            _check_cancelled(cancelled); _safe_url(final_url,query.provider)
        except InterruptedError:
            raise
        except (ProviderEvidenceError,URLError,socket.timeout,TimeoutError,OSError) as exc:
            if cached is not None:
                cached["refresh"]={"state":"unavailable","detail":str(exc)[:512]}
                return cached
            return self._unavailable(query,operation,"unavailable",str(exc))
        if status==429:
            return self._fallback_or_unavailable(cached,query,operation,"rate_limited","The provider rate limited this request.")
        if status in (401,403):
            return self._fallback_or_unavailable(cached,query,operation,"gated","The provider requires access or valid credentials.")
        if status==404:
            return self._fallback_or_unavailable(cached,query,operation,"deleted_or_unpublished","The explicit provider identity was not found or is no longer published.")
        if not 200<=int(status)<300:
            return self._fallback_or_unavailable(cached,query,operation,"unavailable",f"The provider returned HTTP {status}.")
        if len(body)>MAX_RESPONSE_BYTES:
            return self._fallback_or_unavailable(cached,query,operation,"malformed","The provider response exceeded the supported size limit.")
        try:
            payload=_strict_json(body)
            items=_parse_civitai(payload,identity,operation=="search") if query.provider=="civitai" else _parse_hf(payload,identity,operation=="search")
            response_state="available" if operation=="search" else _validate_identity(query,identity,items)
        except (ValueError,UnicodeError,RecursionError,ProviderEvidenceError) as exc:
            return self._fallback_or_unavailable(cached,query,operation,"malformed",str(exc))
        if operation=="search":
            items=items[:limit]
        enrichment=None
        if query.provider=="civitai" and operation=="lookup" and len(items)==1:
            enrichment,error=self._enrich_civitai(items[0],cancelled=cancelled,credentials=credentials)
            if error:
                enrichment={"state":"unavailable","detail":error}
        result={
            "schema_version":SCHEMA_VERSION,"provider":query.provider,"adapter":{"id":query.provider,"version":ADAPTER_VERSIONS[query.provider]},
            "operation":operation,"query":{"kind":query.kind,"value":query.value,"disclosure":"Only this explicit provider query was sent."},
            "state":response_state,"currentness":"changed" if response_state=="changed" else "current","source":{"api_url":final_url,"retrieved_at":_iso(self._clock()),"attribution":query.provider},
            "items":items,"cache":{"used":False,"age_seconds":0},
            "limitations":["Provider evidence is not tested compatibility, execution safety, or confirmed Image Knowledge.","No model weights or provider images were downloaded."],
        }
        if enrichment is not None:
            result["enrichment"]=enrichment
        self._store(key,result)
        return result

    @staticmethod
    def _unavailable(query,operation,state,detail):
        return {"schema_version":SCHEMA_VERSION,"provider":query.provider,"adapter":{"id":query.provider,"version":ADAPTER_VERSIONS[query.provider]},
                "operation":operation,"query":{"kind":query.kind,"value":query.value,"disclosure":"Only this explicit provider query would be sent."},
                "state":state,"currentness":"unavailable","detail":str(detail)[:512],"items":[],"cache":{"used":False,"age_seconds":None},
                "limitations":["No provider evidence is available for this query."]}

    @classmethod
    def _fallback_or_unavailable(cls,cached,query,operation,state,detail):
        if cached is not None:
            cached["refresh"]={"state":state,"detail":detail}
            if state=="deleted_or_unpublished":
                cached["currentness"]="changed"
            return cached
        return cls._unavailable(query,operation,state,detail)
