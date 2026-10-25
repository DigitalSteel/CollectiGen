"""Selected HF metadata inspection; no model loading, weight reads or registry writes."""
from copy import deepcopy
import hashlib
import re
from urllib.parse import quote
from datetime import datetime, timezone
from .model_provider_evidence import (ProviderQuery, ProviderEvidenceError,
    fetch_provider_metadata, parse_provider_metadata_json, provider_request_target)


def _check_cancelled(value):
    check=value if callable(value) else getattr(value,"is_set",lambda: bool(value))
    if check():
        raise InterruptedError("Repository inspection cancelled.")


DOCUMENTS = ("config.json", "adapter_config.json", "quantization_config.json", "model_index.json", "README.md")
MAX_DOCUMENT_BYTES = 256 * 1024
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def _bounded(value):
    todo=[(value,0)]; count=0
    while todo:
        item,depth=todo.pop(); count+=1
        if count>4096 or depth>32:
            raise ValueError("Metadata structure exceeds inspection bounds.")
        if isinstance(item,dict): todo.extend((v,depth+1) for v in item.values())
        elif isinstance(item,list): todo.extend((v,depth+1) for v in item)
    return value


def _claim(value, level, source, limitation=""):
    return {"value":deepcopy(value), "evidence_level":level,
            "source":deepcopy(source), "limitation":limitation}


class HuggingFaceRepositoryInspector:
    """Reuse explicit provider lookup; document transport carries no credentials.

    registry_snapshot is a caller-supplied metadata snapshot, never a path/store.
    No local location is resolved or returned; matches are identity suggestions.
    """
    def __init__(self, provider_service, *, document_fetcher=None):
        self.provider_service=provider_service
        self.document_fetcher=document_fetcher or fetch_provider_metadata

    @staticmethod
    def confirmation_scope(query: ProviderQuery):
        url,identity=provider_request_target(query)
        return {"operation":"selected_repository_metadata_inspection",
                "provider":"hugging_face", "metadata_url":url,
                "requested_identity":identity, "documents":list(DOCUMENTS),
                "document_max_bytes":MAX_DOCUMENT_BYTES,
                "pin_rule":"All documents use the exact returned repository commit; explicit commit drift is held.",
                "weights":False,"credentials":False,"execution":False}

    def inspect(self, query: ProviderQuery, *, allow_network=False, confirmed=False,
                cancelled=None, expected_commit=None, registry_snapshot=None):
        # confirmed means one review of confirmation_scope(query), including all
        # five pinned documents. Reuse existing provider consent; no per-file prompts.
        if query.provider!="hugging_face" or query.kind=="search":
            raise ProviderEvidenceError("Select one explicit Hugging Face repository first.")
        if expected_commit is not None and not _COMMIT.fullmatch(expected_commit):
            raise ProviderEvidenceError("Expected revision must be an exact commit SHA.")
        _url,requested=provider_request_target(query)
        requested_revision=requested.get("revision")
        requested_commit=requested_revision if isinstance(requested_revision,str) and _COMMIT.fullmatch(requested_revision) else None
        if expected_commit and requested_commit and expected_commit!=requested_commit:
            raise ProviderEvidenceError("Requested and reviewed commit pins disagree.")
        required_commit=expected_commit or requested_commit
        _check_cancelled(cancelled)
        evidence=self.provider_service.lookup(query,allow_network=allow_network,
            confirmed=confirmed,cancelled=cancelled)
        _check_cancelled(cancelled)
        result={"schema_version":1,"state":evidence.get("state","unavailable"),
                "repo_id":None,"commit":None,"claims":{},"documents":{},
                "files":[],"reuse":[],"holds":[],"runtime_status":"unknown",
                "model_code_executed":False,"weights_downloaded":False}
        items=evidence.get("items",[])
        if len(items)!=1 or evidence.get("state")!="available":
            result["holds"].append("repository_metadata_unavailable"); return result
        item=items[0]; repo=item.get("repo_id"); commit=item.get("revision")
        if not isinstance(repo,str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}",repo):
            raise ProviderEvidenceError("Malformed repository identity.")
        if not isinstance(commit,str) or not _COMMIT.fullmatch(commit):
            result["state"]="held";result["holds"].append("exact_commit_unavailable");return result
        if required_commit is not None and commit!=required_commit:
            result["state"]="changed";result["holds"].append("reviewed_commit_changed");return result
        result.update(repo_id=repo,commit=commit,state="partial")
        source={**evidence.get("source",{}),"revision":commit}
        for key in ("role","base_model","license","terms","description"):
            value=item.get(key)
            result["claims"][key]=_claim(value,"provider_declared" if value else "unknown",source,
                "Publisher evidence does not establish tested compatibility or legal permission.")
        files=item.get("files",[])
        if not isinstance(files,list) or len(files)>512:
            raise ProviderEvidenceError("File inventory exceeds inspection bounds.")
        for row in files:
            name=row.get("filename") or row.get("name")
            if not isinstance(name,str):continue
            fmt="safetensors" if name.endswith(".safetensors") else "gguf" if name.endswith(".gguf") else "other"
            sha=row.get("sha256"); exact=sha if isinstance(sha,str) and _SHA.fullmatch(sha) and row.get("hash_scope")=="full_file" else None
            result["files"].append({"filename":name,"format_hint":_claim(fmt,"provider_declared",source,"Filename only; bytes not inspected."),
                "sha256":_claim(exact,"provider_declared" if exact else "unknown",source),
                "size_bytes":_claim(row.get("size_bytes"),"provider_declared",source),
                "registry_format_status":"supported_metadata_only" if fmt=="safetensors" else "unsupported" if fmt=="gguf" or name.endswith((".bin",".pt",".ckpt")) else "unknown"})
            resources=(registry_snapshot or {}).get("resources",{})
            if exact and isinstance(resources,dict):
                for identity,record in resources.items():
                    stored=record.get("identity",{}) if isinstance(record,dict) else {}
                    if identity=="sha256:"+exact and stored.get("resource_id")==identity and type(stored.get("size")) is int and type(row.get("size_bytes")) is int and stored["size"]==row["size_bytes"]:
                        result["reuse"].append({"resource_id":identity,"filename":name,
                            "status":"exact_reported_hash_match", "requires_fresh_local_verification":True})
        result["claims"]["custom_code_requirement"]=_claim(None,"unknown",source,"No code is imported or executed.")
        if not allow_network or not confirmed or evidence.get("currentness")!="current" or evidence.get("refresh"):
            result["holds"].append("document_inspection_unavailable_offline_or_stale");return result
        if item.get("availability")=="gated":
            result["holds"].append("gated_access");return result
        configs={}
        for name in DOCUMENTS:
            _check_cancelled(cancelled)
            url=f"https://huggingface.co/{repo}/raw/{commit}/{quote(name)}"
            try:
                status,_headers,raw,final_url=self.document_fetcher(url,{},10.0,cancelled)
                _check_cancelled(cancelled)
                if final_url!=url:raise ValueError("Pinned metadata URL changed.")
                if status in (401,403):
                    result["documents"][name]={"state":"gated"};result["holds"].append("gated_access");break
                if status==404:
                    result["documents"][name]={"state":"missing"};continue
                if status!=200 or not isinstance(raw,bytes) or len(raw)>MAX_DOCUMENT_BYTES:
                    raise ValueError("Metadata response is unavailable or exceeds bounds.")
                text=raw.decode("utf-8"); docsource={"url":url,"revision":commit,
                    "retrieved_at":datetime.now(timezone.utc).isoformat(timespec="seconds"),"document_sha256":hashlib.sha256(raw).hexdigest()}
                if name.endswith(".json"):
                    value=_bounded(parse_provider_metadata_json(raw))
                    if not isinstance(value,dict):raise ValueError("Configuration must be an object.")
                    configs[name]=(value,docsource)
                    result["documents"][name]={"state":"inspected","validation":_claim({"utf8":True,"json_object":True},"parser_verified",docsource,"Syntax validation does not validate model claims."),"content":_claim(value,"provider_declared",docsource,"Strict JSON parsed; architecture claims remain untested.")}
                else:
                    result["documents"][name]={"state":"inspected","validation":_claim({"utf8":True},"parser_verified",docsource,"Text decoding does not validate publisher claims."),"content":_claim(text,"provider_declared",docsource,"Untrusted publisher prose; commands are not authority.")}
            except InterruptedError:raise
            except (ValueError,UnicodeError,RecursionError,OSError) as exc:
                result["documents"][name]={"state":"malformed_or_unavailable","detail":str(exc)[:256]}
                result["holds"].append("metadata_inspection_incomplete")
        config,config_source=configs.get("config.json",({},source))
        for key in ("model_type","architectures","torch_dtype","quantization_config","max_position_embeddings"):
            result["claims"][key]=_claim(config.get(key),"provider_declared" if key in config else "unknown",config_source)
        auto=config.get("auto_map")
        if isinstance(auto,dict) and auto:
            result["claims"]["custom_code_requirement"]=_claim(True,"provider_declared",config_source,"Custom Auto-class mappings declared; loading is outside inspection authority.")
        count=config.get("num_parameters");dtype=config.get("torch_dtype");width={"float32":4,"float16":2,"bfloat16":2}.get(dtype) if isinstance(dtype,str) else None
        standalone,standalone_source=configs.get("quantization_config.json",({},source))
        embedded_present="quantization_config" in config
        standalone_state=result["documents"].get("quantization_config.json",{}).get("state")
        quantization_uncertain=(embedded_present or standalone_state not in (None,"missing"))
        if standalone_state=="inspected":
            result["claims"]["standalone_quantization_config"]=_claim(standalone,"provider_declared",standalone_source,"Cannot apply plain dtype-width estimation to quantized evidence.")
        estimate=None
        if type(count) is int and 0<count<=10**13 and width and not quantization_uncertain:
            estimate=count*width
        result["claims"]["weight_memory_bytes"]=_claim(estimate,"estimated" if estimate else "unknown",config_source,
            "Declared parameter count times dtype width; excludes activations, KV cache, context/concurrency, companions, offload and runtime overhead. Quantization or incomplete quantization evidence holds the estimate unknown. Not a fit or speed claim.")
        result["claims"]["runtime_memory_bytes"]=_claim(None,"unknown",config_source,"Requires selected runtime, context, concurrency and workload evidence.")
        result["holds"]=sorted(set(result["holds"]))
        return result
