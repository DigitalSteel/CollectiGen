from __future__ import annotations

import copy
from contextlib import contextmanager
import csv
import gc
import hashlib
from importlib import metadata as importlib_metadata
import importlib.util
from itertools import islice
import json
import os
import platform
import re
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from statistics import median
from types import SimpleNamespace
from typing import Callable,Iterable,Protocol

from .annotation import AIEvidence
from .ai_runtime_residency import (
    LocalAIRuntimeKey,LocalAIRuntimeManager,LocalAIRuntimeSpec,RuntimeLease,RuntimeMemoryAdmissionError,
)
from .ai_output import generated_completion_text
from .confirmed_tag_identity import ConfirmedTagIdentityService
from .image_fact_decisions import (
    ImageFindingDecisionService,
    image_tag_identity,
    unavailable_confirmed_tag_identities,
)
from .image_record import ImageRecord
from .history_configuration import HistoryConfigurationError
from .history_versions import HistoryError
from .image_knowledge_history import HistoryWriteInterrupted
from .tag_collection import TagCollection, normalize_tag
from .settings import (
    DEFAULT_JOYCAPTION_PROMPT,FLORENCE2_MODEL_ID,JOYCAPTION_MODEL_ID,
    normalize_plain_language_exclusion,normalized_plain_language_max_new_tokens,
)


_LIBRARY_OUTPUT_HANDLES = []


def _ensure_library_output_streams() -> None:
    """Give progress-oriented libraries a safe sink under ``pythonw.exe``."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is not None:
            continue
        handle = open(os.devnull, "w", encoding="utf-8")
        _LIBRARY_OUTPUT_HANDLES.append(handle)
        setattr(sys, name, handle)


WD_EVA02_MODEL = "SmilingWolf/wd-eva02-large-tagger-v3"
JOYCAPTION_MODEL = JOYCAPTION_MODEL_ID
FLORENCE2_MODEL = FLORENCE2_MODEL_ID
MODEL_INSTALLATION_MANIFEST = "tag-manager-model.json"
WD_BENCHMARK_SCHEMA_VERSION = 2
WD_BENCHMARK_SAMPLE_LIMIT = 8
JOY_BENCHMARK_SCHEMA_VERSION = 3
JOY_BENCHMARK_SAMPLE_LIMIT = 4
JOY_QUALITY_COMPARISON_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ModelAcquisition:
    model_id: str
    revision: str
    license_name: str
    license_url: str
    source_url: str
    terms_notice: str = ""


MODEL_ACQUISITIONS = {
    WD_EVA02_MODEL: ModelAcquisition(
        WD_EVA02_MODEL,
        "b25b82a03f7282e41aa2f257a52c7583b710bd1c",
        "Apache-2.0",
        "https://huggingface.co/SmilingWolf/wd-eva02-large-tagger-v3/blob/b25b82a03f7282e41aa2f257a52c7583b710bd1c/LICENSE",
        "https://huggingface.co/SmilingWolf/wd-eva02-large-tagger-v3/tree/b25b82a03f7282e41aa2f257a52c7583b710bd1c",
    ),
    "SmilingWolf/wd-vit-large-tagger-v3": ModelAcquisition(
        "SmilingWolf/wd-vit-large-tagger-v3",
        "ae469aa2e4706a3af08d3673cf73a11d1add314c",
        "Apache-2.0",
        "https://huggingface.co/SmilingWolf/wd-vit-large-tagger-v3/blob/ae469aa2e4706a3af08d3673cf73a11d1add314c/LICENSE",
        "https://huggingface.co/SmilingWolf/wd-vit-large-tagger-v3/tree/ae469aa2e4706a3af08d3673cf73a11d1add314c",
    ),
    "SmilingWolf/wd-swinv2-tagger-v3": ModelAcquisition(
        "SmilingWolf/wd-swinv2-tagger-v3",
        "627aef95638667ddcaa3ac8ae625e88ea5b02f51",
        "Apache-2.0",
        "https://huggingface.co/SmilingWolf/wd-swinv2-tagger-v3/blob/627aef95638667ddcaa3ac8ae625e88ea5b02f51/LICENSE",
        "https://huggingface.co/SmilingWolf/wd-swinv2-tagger-v3/tree/627aef95638667ddcaa3ac8ae625e88ea5b02f51",
    ),
    "SmilingWolf/wd-convnext-tagger-v3": ModelAcquisition(
        "SmilingWolf/wd-convnext-tagger-v3",
        "d39e46de298d27340111b64965e20b8185c407e6",
        "Apache-2.0",
        "https://huggingface.co/SmilingWolf/wd-convnext-tagger-v3/blob/d39e46de298d27340111b64965e20b8185c407e6/LICENSE",
        "https://huggingface.co/SmilingWolf/wd-convnext-tagger-v3/tree/d39e46de298d27340111b64965e20b8185c407e6",
    ),
    "SmilingWolf/wd-vit-tagger-v3": ModelAcquisition(
        "SmilingWolf/wd-vit-tagger-v3",
        "7f6b584d0bd3f55c4531f14ba3d4761b2bccdc0f",
        "Apache-2.0",
        "https://huggingface.co/SmilingWolf/wd-vit-tagger-v3/blob/7f6b584d0bd3f55c4531f14ba3d4761b2bccdc0f/LICENSE",
        "https://huggingface.co/SmilingWolf/wd-vit-tagger-v3/tree/7f6b584d0bd3f55c4531f14ba3d4761b2bccdc0f",
    ),
    JOYCAPTION_MODEL: ModelAcquisition(
        JOYCAPTION_MODEL,
        "ebf414ea497a020da0f82df3913e5b6cb8e9663a",
        "Llama 3.1 Community License",
        "https://huggingface.co/fancyfeast/llama-joycaption-beta-one-hf-llava/blob/ebf414ea497a020da0f82df3913e5b6cb8e9663a/LLAMA_LICENSE",
        "https://huggingface.co/fancyfeast/llama-joycaption-beta-one-hf-llava/tree/ebf414ea497a020da0f82df3913e5b6cb8e9663a",
        "Built with Llama. Use is subject to the Llama 3.1 Community License and Acceptable Use Policy: https://llama.meta.com/llama3_1/use-policy",
    ),
    FLORENCE2_MODEL: ModelAcquisition(
        FLORENCE2_MODEL,
        "0b03b6f15a4a211370fb204aee4e7dd48887ea37",
        "MIT",
        "https://huggingface.co/microsoft/Florence-2-base-ft/blob/494b983c36978a56fdd3b1082f5995cc9fed7c09/LICENSE",
        "https://huggingface.co/florence-community/Florence-2-base-ft/tree/0b03b6f15a4a211370fb204aee4e7dd48887ea37",
        "Native Transformers conversion of Microsoft's Florence-2-base-ft checkpoint; model-supplied Python code is not executed.",
    ),
}


def installed_model_revision(model_dir: Path, model_id: str) -> str:
    """Return a verified download revision, or blank for a legacy/unmarked folder."""
    try:
        payload=json.loads((Path(model_dir)/MODEL_INSTALLATION_MANIFEST).read_text(encoding="utf-8"))
    except (OSError,ValueError,TypeError):
        return ""
    if not isinstance(payload,dict) or payload.get("model_id")!=model_id:
        return ""
    revision=str(payload.get("revision") or "")
    return revision if revision==MODEL_ACQUISITIONS[model_id].revision else ""


def _runtime_model_fingerprint(
    model_dir: Path,model_id: str,required: Iterable[str],
) -> str:
    """Bound resident identity to the small load contract and model-file stats."""
    root=Path(model_dir).resolve(strict=False)
    rows=[]
    names={MODEL_INSTALLATION_MANIFEST,"config.json","selected_tags.csv",*required}
    explicit={root/name for name in names}
    try:
        # Model weights are too large to hash on every one-image run. Their
        # canonical top-level names, size, timestamp and file identity still
        # retire a resident instance after replacement/removal. Small load
        # metadata is content-hashed to catch edits with preserved timestamps.
        sampled=set(islice(root.iterdir(),512))
    except OSError:
        sampled=set()
    children=sorted(explicit|sampled,key=lambda path:path.name.casefold())
    for path in children:
        suffix=path.suffix.casefold()
        if path.name not in names and suffix not in {
            ".onnx",".safetensors",".bin",".json",".csv",".model",
        }:
            continue
        try:
            stat=path.stat()
            row={
                "name":path.name,"file":path.is_file(),"size":stat.st_size,
                "mtime_ns":stat.st_mtime_ns,"device":stat.st_dev,"inode":stat.st_ino,
            }
            if path.is_file() and stat.st_size<=16*1024*1024 and suffix in {".json",".csv"}:
                row["sha256"]=hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            row={"name":path.name,"error":type(exc).__name__}
        rows.append(row)
    payload={
        "model_id":model_id,"root":os.path.normcase(str(root)),
        "required":sorted(str(value) for value in required),"files":rows,
    }
    return hashlib.sha256(json.dumps(
        payload,sort_keys=True,separators=(",",":"),
    ).encode("utf-8")).hexdigest()


def _runtime_settings_fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(
        payload,sort_keys=True,separators=(",",":"),
    ).encode("utf-8")).hexdigest()


def _record_model_installation(model_dir: Path, acquisition: ModelAcquisition) -> None:
    target=Path(model_dir)/MODEL_INSTALLATION_MANIFEST
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_name(target.name+".tmp")
    payload={
        "schema_version":1,
        "model_id":acquisition.model_id,
        "revision":acquisition.revision,
        "license":acquisition.license_name,
        "license_url":acquisition.license_url,
        "source_url":acquisition.source_url,
    }
    temporary.write_text(json.dumps(payload,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    temporary.replace(target)


def split_plain_language(value: str) -> list[str]:
    """Split imported prose into editable sentence-level Image Knowledge facts."""
    sentences=[]
    for paragraph in re.split(r"\n+",str(value or "")):
        sentences.extend(
            item.strip() for item in re.split(r'(?<=[.!?])\s+(?=[A-Z0-9"“‘(])',paragraph.strip())
            if item.strip()
        )
    return TagCollection(sentences).to_list()


def merge_plain_language_in_source_order(existing, incoming, source_sequences=()) -> list[str]:
    """Add prose without letting review timing replace its source sentence order."""
    result=[]; known=set()
    for raw in existing:
        value=str(raw or "").strip(); key=value.casefold()
        if value and key not in known: result.append(value); known.add(key)
    sequences=[]
    for raw_sequence in source_sequences:
        sequence=[]; sequence_keys=set()
        for raw in raw_sequence:
            for value in split_plain_language(str(raw or "")):
                key=value.casefold()
                if key not in sequence_keys: sequence.append(value); sequence_keys.add(key)
        if sequence: sequences.append(sequence)
    for raw in incoming:
        for value in split_plain_language(str(raw or "")):
            key=value.casefold()
            if key in known: continue
            sequence=next((item for item in sequences if key in {entry.casefold() for entry in item}),None)
            if sequence is None:
                result.append(value); known.add(key); continue
            order={entry.casefold():index for index,entry in enumerate(sequence)}; target=order[key]
            following=[
                (order.get(entry.casefold()),index) for index,entry in enumerate(result)
                if order.get(entry.casefold()) is not None and order[entry.casefold()]>target
            ]
            if following:
                _source_index,insert_at=min(following,key=lambda item:(item[0],item[1]))
            else:
                preceding=[
                    (order.get(entry.casefold()),index) for index,entry in enumerate(result)
                    if order.get(entry.casefold()) is not None and order[entry.casefold()]<target
                ]
                insert_at=(max(preceding,key=lambda item:(item[0],item[1]))[1]+1) if preceding else len(result)
            result.insert(insert_at,value); known.add(key)
    return result


WD_TAGGER_MODELS = {
    WD_EVA02_MODEL:("WD EVA02 Large v3","wd-eva02-large-tagger-v3"),
    "SmilingWolf/wd-vit-large-tagger-v3":("WD ViT Large v3","wd-vit-large-tagger-v3"),
    "SmilingWolf/wd-swinv2-tagger-v3":("WD SwinV2 v3","wd-swinv2-tagger-v3"),
    "SmilingWolf/wd-convnext-tagger-v3":("WD ConvNeXt v3","wd-convnext-tagger-v3"),
    "SmilingWolf/wd-vit-tagger-v3":("WD ViT v3","wd-vit-tagger-v3"),
}
WD_TAGGER_DESCRIPTIONS = {
    WD_EVA02_MODEL:"Published validation F1 0.4772 · 0.3B parameters · highest published F1 in this included set.",
    "SmilingWolf/wd-vit-large-tagger-v3":"Published validation F1 0.4674 · 0.3B parameters · large ViT consensus alternative.",
    "SmilingWolf/wd-swinv2-tagger-v3":"Published validation F1 0.4541 · 98M parameters · smaller architecture-diverse option.",
    "SmilingWolf/wd-convnext-tagger-v3":"Published validation F1 0.4419 · 98.7M parameters · smaller ConvNeXt option.",
    "SmilingWolf/wd-vit-tagger-v3":"Published validation F1 0.4402 · 94.6M parameters · smallest included ViT.",
}
WD_TAGGER_BEST_FOR = {
    WD_EVA02_MODEL:"the strongest single default when published tag quality matters most",
    "SmilingWolf/wd-vit-large-tagger-v3":"a close-quality second opinion in a multi-tagger consensus",
    "SmilingWolf/wd-swinv2-tagger-v3":"a smaller, architecture-diverse second opinion that balances quality and model size",
    "SmilingWolf/wd-convnext-tagger-v3":"adding a convolutional second opinion alongside transformer taggers",
    "SmilingWolf/wd-vit-tagger-v3":"a lighter transformer baseline when model size matters more than top published F1",
}
WD_TAGGER_MODEL_CARDS = {
    model_id: {
        "summary": summary,
        "best_for": WD_TAGGER_BEST_FOR[model_id],
        "source_label": "Official Model Card",
        "source_url": f"https://huggingface.co/{model_id}",
    }
    for model_id,summary in WD_TAGGER_DESCRIPTIONS.items()
}

PLAIN_LANGUAGE_MODELS = {
    JOYCAPTION_MODEL:(
        "JoyCaption",
        "joycaption",
        "Best for detailed, instruction-guided captions; heavier and NVIDIA-only in the reviewed profile.",
    ),
    FLORENCE2_MODEL:(
        "Florence-2 Base FT",
        "florence-2-base-ft",
        "Best for fast, compact visual captions with a smaller local model.",
    ),
}


@dataclass
class TagPrediction:
    tag: str
    confidence: float
    category: str = "general"


@dataclass
class InterrogationResult:
    model_id: str
    kind: str
    tags: list[TagPrediction] = field(default_factory=list)
    plain_language: str = ""
    prompt: str = ""
    model_revision: str = ""
    settings: dict = field(default_factory=dict)
    raw_output: object = None


class InterrogationBackend(Protocol):
    def analyze(self, image: Path) -> InterrogationResult: ...


def hash_image(path: Path) -> str:
    digest = hashlib.blake2b(digest_size=16)
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sidecar_fingerprint(path: Path) -> tuple[int,int,str] | None:
    try:
        before=path.stat(); raw=path.read_bytes(); after=path.stat()
    except OSError:
        return None
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
        raise RuntimeError(f"{path.name} changed while it was being read.")
    return after.st_size,after.st_mtime_ns,hashlib.sha256(raw).hexdigest()


_EXPECTED_SIDECAR_UNSET=object()


def validate_interrogation_result(result: InterrogationResult) -> None:
    if result.kind not in AIEvidence.VALID_KINDS:
        raise ValueError(f"Unsupported AI evidence kind: {result.kind!r}")
    if result.kind == "sdxl_tags" and result.plain_language.strip():
        raise ValueError("SDXL Tag analysis returned Plain Language content.")
    if result.kind == "plain_language" and result.tags:
        raise ValueError("Plain Language analysis returned SDXL Tag content.")


def _serialized_predictions(result: InterrogationResult) -> list[dict]:
    return [
        {"tag": item.tag, "confidence": round(float(item.confidence), 6), "category": item.category}
        for item in result.tags
    ]


def result_to_evidence(
    image: Path,
    result: InterrogationResult,
    *,
    observed_result: InterrogationResult | None = None,
) -> AIEvidence:
    validate_interrogation_result(result)
    observed_result=observed_result or result
    validate_interrogation_result(observed_result)
    if observed_result.kind!=result.kind:
        raise ValueError("Observed and reviewable AI evidence kinds do not match.")
    return AIEvidence(
        model_id=result.model_id,
        model_revision=result.model_revision,
        kind=result.kind,
        prompt=result.prompt,
        created_at=datetime.now(timezone.utc).isoformat(),
        image_hash=hash_image(image),
        settings=dict(result.settings),
        observed_tags=_serialized_predictions(observed_result),
        tags=_serialized_predictions(result),
        plain_language=result.plain_language,
        raw_output=result.raw_output,
    )


@dataclass
class Reconciliation:
    suggested_tags: list[TagPrediction]
    already_present: list[TagPrediction]
    previously_rejected: list[TagPrediction]
    proposed_plain_language: str


@dataclass
class AIImageRunResult:
    processed: int = 0
    skipped_existing: int = 0
    errors: list[str] = field(default_factory=list)
    auto_merged: int = 0
    conflicts_held: int = 0
    review_required: bool = False

    def absorb(self, other: "AIImageRunResult") -> None:
        self.processed += int(other.processed)
        self.skipped_existing += int(other.skipped_existing)
        self.errors.extend(other.errors)
        self.auto_merged += int(other.auto_merged)
        self.conflicts_held += int(other.conflicts_held)
        self.review_required = self.review_required or bool(other.review_required)


@dataclass
class BatchInterrogationResult:
    processed: int = 0
    skipped_existing: int = 0
    review_paths: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    auto_merged: int = 0
    rules_applied: int = 0
    conflicts_held: int = 0
    cancelled: bool = False
    observations: dict[Path,InterrogationResult] = field(default_factory=dict,repr=False)
    image_results: dict[Path,AIImageRunResult] = field(default_factory=dict,repr=False)
    history_interrupted: bool = False

    def image_result(self, image: str | Path) -> AIImageRunResult:
        return self.image_results.setdefault(Path(image),AIImageRunResult())


@dataclass(frozen=True)
class AIProcessingEstimate:
    warm_seconds: float
    first_run_seconds: float
    benchmarked_models: tuple[str,...]
    missing_models: tuple[str,...]
    stale_models: tuple[str,...] = ()


@lru_cache(maxsize=1)
def wd_benchmark_environment() -> dict[str,object]:
    """Return a machine-local runtime identity without storing private paths."""
    try:
        import onnxruntime as ort
        runtime_version=str(getattr(ort,"__version__","") or "")
        providers=sorted(str(value) for value in ort.get_available_providers())
    except Exception:
        runtime_version=""; providers=[]
    return {
        "platform":sys.platform,
        "machine":platform.machine(),
        "processor":platform.processor(),
        "cpu_count":int(os.cpu_count() or 0),
        "onnxruntime":runtime_version,
        "providers":providers,
    }


def wd_benchmark_environment_fingerprint() -> str:
    payload=json.dumps(
        wd_benchmark_environment(),sort_keys=True,separators=(",",":"),ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def wd_benchmark_is_current(settings,model_id: str,benchmark: object) -> bool:
    """Reject estimates measured for another model/configuration/runtime."""
    if not isinstance(benchmark,dict):
        return False
    try:
        schema_version=int(benchmark.get("schema_version",0) or 0)
    except (TypeError,ValueError):
        return False
    if schema_version!=WD_BENCHMARK_SCHEMA_VERSION:
        return False
    configuration=benchmark.get("configuration",{})
    if not isinstance(configuration,dict) or configuration.get("model_id")!=model_id:
        return False
    thresholds=configuration.get("thresholds",{})
    expected={
        "general":float(getattr(settings,"wd_general_threshold",.35)),
        "character":float(getattr(settings,"wd_character_threshold",.85)),
        "rating":float(getattr(settings,"wd_rating_threshold",.35)),
    }
    if thresholds!=expected:
        return False
    model_info=WD_TAGGER_MODELS.get(model_id)
    if not model_info:
        return False
    model_dir=Path(getattr(settings,"ai_models_directory",""))/model_info[1]
    if configuration.get("model_revision","")!=installed_model_revision(model_dir,model_id):
        return False
    return benchmark.get("environment_fingerprint","")==wd_benchmark_environment_fingerprint()


@lru_cache(maxsize=1)
def joy_benchmark_environment() -> dict[str,object]:
    """Return a lightweight JoyCaption runtime identity without importing the model."""
    versions={}
    for distribution in ("torch","transformers","bitsandbytes","pillow"):
        try:
            versions[distribution]=importlib_metadata.version(distribution)
        except importlib_metadata.PackageNotFoundError:
            versions[distribution]=""
    return {
        "platform":sys.platform,
        "machine":platform.machine(),
        "processor":platform.processor(),
        "cpu_count":int(os.cpu_count() or 0),
        "packages":versions,
    }


def joy_benchmark_environment_fingerprint() -> str:
    payload=json.dumps(
        joy_benchmark_environment(),sort_keys=True,separators=(",",":"),ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def joy_benchmark_configuration(settings) -> dict[str,object]:
    """Describe the saved JoyCaption settings that materially affect measured speed."""
    do_sample=bool(getattr(settings,"joycaption_do_sample",False))
    generation={
        "max_new_tokens":normalized_plain_language_max_new_tokens(
            getattr(settings,"joycaption_max_new_tokens",256)
        ),
        "min_new_tokens":int(getattr(settings,"joycaption_min_new_tokens",30)),
        "num_beams":int(getattr(settings,"joycaption_num_beams",4)),
        "length_penalty":float(getattr(settings,"joycaption_length_penalty",1.0)),
        "do_sample":do_sample,
        "repetition_penalty":float(getattr(settings,"joycaption_repetition_penalty",1.0)),
        "no_repeat_ngram_size":int(getattr(settings,"joycaption_no_repeat_ngram_size",3)),
        "gpu_index":int(getattr(settings,"joycaption_gpu_index",0)),
        "quantization":(
            str(getattr(settings,"joycaption_quantization","bf16"))
            if str(getattr(settings,"joycaption_quantization","bf16")) in {"bf16","nf4"}
            else "bf16"
        ),
    }
    if do_sample:
        generation.update(
            temperature=float(getattr(settings,"joycaption_temperature",0.5)),
            top_k=int(getattr(settings,"joycaption_top_k",50)),
            top_p=float(getattr(settings,"joycaption_top_p",0.9)),
        )
    prompt=str(getattr(settings,"joycaption_prompt","") or DEFAULT_JOYCAPTION_PROMPT).strip()
    model_dir=Path(getattr(settings,"ai_models_directory",""))/"joycaption"
    return {
        "model_id":JOYCAPTION_MODEL,
        "model_revision":installed_model_revision(model_dir,JOYCAPTION_MODEL),
        "prompt_fingerprint":hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "generation":generation,
    }


def joy_benchmark_is_current(settings,benchmark: object) -> bool:
    """Reject JoyCaption timing measured for another model, prompt, or runtime."""
    if not isinstance(benchmark,dict):
        return False
    try:
        schema_version=int(benchmark.get("schema_version",0) or 0)
    except (TypeError,ValueError):
        return False
    return (
        schema_version==JOY_BENCHMARK_SCHEMA_VERSION
        and benchmark.get("configuration")==joy_benchmark_configuration(settings)
        and benchmark.get("environment_fingerprint","")==joy_benchmark_environment_fingerprint()
    )


def florence_benchmark_configuration(settings) -> dict[str,object]:
    """Describe the saved controls that materially affect Florence-2 speed."""
    do_sample=bool(getattr(settings,"joycaption_do_sample",False))
    generation={
        "max_new_tokens":normalized_plain_language_max_new_tokens(
            getattr(settings,"joycaption_max_new_tokens",256)
        ),
        "min_new_tokens":int(getattr(settings,"joycaption_min_new_tokens",30)),
        "num_beams":int(getattr(settings,"joycaption_num_beams",4)),
        "length_penalty":float(getattr(settings,"joycaption_length_penalty",1.0)),
        "do_sample":do_sample,
        "repetition_penalty":float(getattr(settings,"joycaption_repetition_penalty",1.0)),
        "no_repeat_ngram_size":int(getattr(settings,"joycaption_no_repeat_ngram_size",3)),
        "gpu_index":int(getattr(settings,"joycaption_gpu_index",0)),
        "quantization":"bf16",
    }
    if do_sample:
        generation.update(
            temperature=float(getattr(settings,"joycaption_temperature",0.5)),
            top_k=int(getattr(settings,"joycaption_top_k",50)),
            top_p=float(getattr(settings,"joycaption_top_p",0.9)),
        )
    model_dir=Path(getattr(settings,"ai_models_directory",""))/PLAIN_LANGUAGE_MODELS[FLORENCE2_MODEL][1]
    return {
        "model_id":FLORENCE2_MODEL,
        "model_revision":installed_model_revision(model_dir,FLORENCE2_MODEL),
        "prompt_fingerprint":hashlib.sha256(
            Florence2Backend.TASK_PROMPT.encode("utf-8")
        ).hexdigest(),
        "generation":generation,
    }


def florence_benchmark_is_current(settings,benchmark: object) -> bool:
    """Reject Florence timing measured for another task/configuration/runtime."""
    if not isinstance(benchmark,dict):
        return False
    try:
        schema_version=int(benchmark.get("schema_version",0) or 0)
    except (TypeError,ValueError):
        return False
    return (
        schema_version==JOY_BENCHMARK_SCHEMA_VERSION
        and benchmark.get("configuration")==florence_benchmark_configuration(settings)
        and benchmark.get("environment_fingerprint","")==joy_benchmark_environment_fingerprint()
    )


def plain_language_benchmark_is_current(
    settings,model_id: str,benchmark: object,
) -> bool:
    if model_id==JOYCAPTION_MODEL:
        return joy_benchmark_is_current(settings,benchmark)
    if model_id==FLORENCE2_MODEL:
        return florence_benchmark_is_current(settings,benchmark)
    return False


def recommend_wd_pipeline_settings(results: dict[str,dict]) -> dict[str,object]:
    """Choose one measured provider/batch pair for the complete enabled pipeline."""
    values=[value for value in results.values() if isinstance(value,dict)]
    if not values:
        return {"provider":"auto","batch_size":1,"seconds_per_image":0.0}
    candidates=[]
    for provider in ("cuda","cpu"):
        common_sizes=None
        for value in values:
            measurement=(value.get("measurements",{}) or {}).get(provider,{})
            if measurement.get("error"):
                common_sizes=set(); break
            if provider=="cuda" and measurement.get("provider")!="CUDAExecutionProvider":
                common_sizes=set(); break
            sizes={
                int(size) for size,batch in (measurement.get("batches",{}) or {}).items()
                if isinstance(batch,dict) and float(batch.get("seconds_per_image",0) or 0)>0
            }
            common_sizes=sizes if common_sizes is None else common_sizes&sizes
        for size in sorted(common_sizes or ()):
            seconds=sum(
                float(value["measurements"][provider]["batches"][str(size)]["seconds_per_image"])
                for value in values
            )
            candidates.append((seconds,provider,size))
    if not candidates:
        sizes=[max(1,int(value.get("recommended_batch_size",1) or 1)) for value in values]
        return {
            "provider":"auto","batch_size":min(sizes) if sizes else 1,
            "seconds_per_image":sum(float(value.get("seconds_per_image",0) or 0) for value in values),
        }
    fastest=min(value[0] for value in candidates)
    near_fastest=[value for value in candidates if value[0]<=fastest*1.05]
    seconds,provider,size=min(near_fastest,key=lambda value:(value[2],value[0],value[1]))
    return {
        "provider":provider,"batch_size":size,"seconds_per_image":seconds,
        "output_agreement":all(bool(value.get("output_agreement",True)) for value in values),
    }


def estimate_ai_duration(settings,count: int,model_ids: Iterable[str]) -> AIProcessingEstimate:
    """Estimate the configured local pipeline from this computer's stored benchmark data."""
    count=max(0,int(count)); warm=0.0; startup=0.0; measured=[]; missing=[]
    benchmarks=getattr(settings,"ai_model_benchmarks",{}) or {}; provider=str(getattr(settings,"wd_execution_provider","auto")); batch_size=max(1,int(getattr(settings,"wd_batch_size",1)))
    stale=[]
    for model_id in model_ids:
        benchmark=benchmarks.get(model_id,{}) if isinstance(benchmarks,dict) else {}
        if model_id in PLAIN_LANGUAGE_MODELS:
            if benchmark and not plain_language_benchmark_is_current(
                settings,model_id,benchmark,
            ):
                stale.append(str(model_id)); continue
            seconds=benchmark.get("seconds_per_image") if isinstance(benchmark,dict) else None
            if not seconds:
                missing.append(str(model_id)); continue
            per_image=max(0.0,float(seconds)); warm+=per_image*count; measured.append(str(model_id))
            cold=float(benchmark.get("cold_seconds",0) or 0)
            startup+=max(0.0,cold-per_image)
            continue
        if model_id in WD_TAGGER_MODELS and benchmark and not wd_benchmark_is_current(settings,model_id,benchmark):
            stale.append(str(model_id)); continue
        selected_provider=provider if provider in {"cpu","cuda"} else str(benchmark.get("recommended_provider",provider))
        measurement=(benchmark.get("measurements",{}) or {}).get(selected_provider,{}) if isinstance(benchmark,dict) else {}
        batch=(measurement.get("batches",{}) or {}).get(str(batch_size),{}) if isinstance(measurement,dict) else {}
        seconds=batch.get("seconds_per_image") or benchmark.get("seconds_per_image") if isinstance(benchmark,dict) else None
        if not seconds:
            missing.append(str(model_id)); continue
        per_image=max(0.0,float(seconds)); warm+=per_image*count; measured.append(str(model_id))
        cold=float(measurement.get("cold_seconds",0) or 0) if isinstance(measurement,dict) else 0.0
        startup+=max(0.0,cold-per_image)
    return AIProcessingEstimate(warm,warm+startup,tuple(measured),tuple(missing),tuple(stale))


@contextmanager
def _preparation_write_guard(context, image):
    """Do not let a fresh producer write implicitly reconcile pending history."""
    if context is None:
        yield
        return
    with context.locked(image):
        if context.store.has_pending(context.entity):
            raise HistoryWriteInterrupted("Saved history needs explicit scoped recovery before preparation can continue.")
        yield


def _preparation_record(image, history_operation=None):
    if history_operation is None:
        return ImageRecord(image)
    context = history_operation.context_for(image)
    if context is None or context.operation_id != history_operation.operation_id:
        raise HistoryError("Participating preparation requires its exact non-empty history context.")
    with _preparation_write_guard(context, image):
        return ImageRecord(image, history_context=context)


class AIInterrogationService:
    def __init__(self,runtime_manager: LocalAIRuntimeManager|None=None,*,timing_store=None) -> None:
        self.runtime_manager=runtime_manager
        self.timing_store=timing_store

    def _bind_runtime(self,backend,should_cancel=None) -> None:
        manager=getattr(self,"runtime_manager",None)
        bind=getattr(backend,"bind_runtime_manager",None)
        if manager is not None and callable(bind):
            bind(manager)
        bind_timing=getattr(backend,"bind_timing_store",None)
        if callable(bind_timing):bind_timing(getattr(self,"timing_store",None))
        begin_timing=getattr(backend,'begin_timing_request',None)
        if callable(begin_timing):begin_timing()
        set_cancel=getattr(backend,"set_runtime_cancellation",None)
        if callable(set_cancel):
            set_cancel(should_cancel)

    @classmethod
    def _analysis_error_text(cls,exc: Exception) -> str:
        detail=str(exc).strip() or type(exc).__name__
        lowered=detail.casefold()
        if (
            "cudnn_" in lowered
            and (
                "winerror 127" in lowered
                or "specified procedure could not be found" in lowered
            )
        ):
            return (
                "The NVIDIA AI runtimes loaded incompatible CUDA libraries. "
                "Restart CollectiGen, then retry. If the problem continues, "
                "update both AI components together in Setup."
            )
        return detail

    @classmethod
    def _is_systemic_backend_error(cls,exc: Exception) -> bool:
        detail=str(exc).casefold()
        return (
            isinstance(exc,RuntimeMemoryAdmissionError)
            or "ai tag runtime could not load" in detail
            or "onnxruntime_pybind11_state" in detail
            or (
                "cudnn_" in detail
                and (
                    "winerror 127" in detail
                    or "specified procedure could not be found" in detail
                )
            )
        )

    @staticmethod
    def work_units(image_count: int,backends) -> int:
        count=max(0,int(image_count))
        return sum(count+(2 if callable(getattr(backend,"release",None)) else 0) for backend in backends)

    @staticmethod
    def _backend_label(backend) -> str:
        value=str(getattr(backend,"model_id","") or type(backend).__name__)
        return value.rsplit("/",1)[-1]

    @staticmethod
    def _matching_settings(settings) -> dict:
        """Exclude hardware routing that does not change the model's semantics."""
        values=dict(settings or {})
        values.pop("execution_provider",None)
        return values

    @staticmethod
    def matching_evidence(
        record: ImageRecord,backend,*,check_image: bool=True,image_digest: str="",
    ) -> AIEvidence | None:
        identity=backend.evidence_identity() if hasattr(backend,"evidence_identity") else None
        if not identity: return None
        image_digest=(image_digest or hash_image(record.image_path)) if check_image else None
        return next((evidence for evidence in reversed(record.annotation.ai_evidence) if
            (not check_image or evidence.image_hash==image_digest)
            and evidence.model_id==identity["model_id"]
            and evidence.model_revision==identity.get("model_revision","")
            and evidence.kind==identity["kind"]
            and evidence.prompt==identity.get("prompt","")
            and AIInterrogationService._matching_settings(evidence.settings)
                ==AIInterrogationService._matching_settings(identity.get("settings",{}))
        ),None)

    @staticmethod
    def has_matching_evidence(
        record: ImageRecord,backend,*,check_image: bool=True,image_digest: str="",
    ) -> bool:
        return AIInterrogationService.matching_evidence(
            record,backend,check_image=check_image,image_digest=image_digest,
        ) is not None

    @staticmethod
    def evidence_is_fresh(
        evidence: AIEvidence | None,
        maximum_age_seconds: float | None,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Return whether exact matching evidence may satisfy an age-based skip.

        ``None`` preserves the historical behavior where exact current evidence
        remains reusable indefinitely. Once a maximum age is supplied, missing
        or malformed timestamps are conservatively treated as stale.
        """
        if evidence is None:
            return False
        if maximum_age_seconds is None:
            return True
        try:
            created_text=str(evidence.created_at or "").strip()
            if created_text.endswith("Z"):
                created_text=created_text[:-1]+"+00:00"
            created=datetime.fromisoformat(created_text)
            if created.tzinfo is None:
                created=created.replace(tzinfo=timezone.utc)
            current=now or datetime.now(timezone.utc)
            if current.tzinfo is None:
                current=current.replace(tzinfo=timezone.utc)
            age=(
                current.astimezone(timezone.utc)-created.astimezone(timezone.utc)
            ).total_seconds()
        except (TypeError,ValueError,OverflowError):
            return False
        return age<=max(0.0,float(maximum_age_seconds))

    def analyze_batch(
        self,images: Iterable[str | Path],backend: InterrogationBackend,tag_database=None,
        progress: Callable[[int,int,str],None] | None=None,
        force: bool = False,
        auto_merge: bool = False,
        run_mode: str = "manual",
        maximum_age_seconds: float | None = None,
        _manage_lifecycle: bool = True,
        should_cancel: Callable[[],bool] | None = None,
        mutation_callback: Callable[[Path,dict,dict,bool,str],None] | None = None,
        return_partial_on_cancel: bool = False,
        history_operation=None,
    ) -> BatchInterrogationResult:
        paths=[Path(value) for value in images]; result=BatchInterrogationResult()
        self._bind_runtime(backend,should_cancel)
        set_cost_scope=getattr(backend,'set_cost_scope',None)
        if callable(set_cost_scope):set_cost_scope(paths)
        for path in paths:
            result.image_result(path)
        managed=_manage_lifecycle and callable(getattr(backend,"release",None)); total=len(paths)+(2 if managed else 0); label=self._backend_label(backend)
        completed_image=0
        def provider_label():
            lease=getattr(backend,"_runtime_lease",None)
            value=getattr(backend,"_provider","") or getattr(getattr(lease,"key",None),"provider","")
            if not value:
                pipeline=getattr(backend,"_pipeline",None)
                device=getattr(getattr(pipeline,"model",None),"device",None)
                value=getattr(device,"type","")
            return str(value or "not yet resolved")
        def timing_detail():
            describe=getattr(backend,"timing_progress_detail",None)
            return (" • "+describe(max(0,len(paths)-completed_image))) if callable(describe) else ""
        def observe_remaining(phase):
            observer=getattr(progress,'observe_inference',None)
            if callable(observer):observer(backend,max(0,len(paths)-completed_image),phase)
        def report(index,_total,name):
            nonlocal completed_image
            completed_image=max(0,min(len(paths),int(index)))
            observe_remaining('completed-unit')
            if progress:
                progress(completed_image+(1 if managed else 0),total,
                    f"Analyzing {label} • provider {provider_label()}; {completed_image}/{len(paths)} images finished"+timing_detail())
        def runtime_progress(phase):
            observe_remaining('inference' if phase.startswith('Running inference') else 'loading')
            if progress:
                progress(completed_image+(1 if managed else 0),total,
                    f"{phase} {label} • provider {provider_label()}; {completed_image}/{len(paths)} images finished"+timing_detail())
        report.observe_inference=getattr(progress,'observe_inference',None)
        backend._runtime_progress=runtime_progress
        runtime_progress("Loading")
        try:
            self._raise_if_cancelled(should_cancel)
            if getattr(backend,"batch_size",1)>1 and hasattr(backend,"analyze_many"):
                return self._analyze_many_batch(
                    paths,backend,tag_database,report,force,auto_merge,run_mode,
                    maximum_age_seconds=maximum_age_seconds,
                    should_cancel=should_cancel,
                    mutation_callback=mutation_callback,
                    result=result,
                    **({"history_operation": history_operation} if history_operation is not None else {}),
                )
            for index,path in enumerate(paths,start=1):
                self._raise_if_cancelled(should_cancel)
                try:
                    record=_preparation_record(path, history_operation)
                    expected_sidecar=_sidecar_fingerprint(record.sidecar_path)
                    expected_image_hash=hash_image(path)
                    if hasattr(backend,"known_facts"):
                        backend.known_facts=record.annotation.all_structured_tags()
                    matching=self.matching_evidence(
                        record,backend,image_digest=expected_image_hash,
                    )
                    if not force and self.evidence_is_fresh(
                        matching,maximum_age_seconds,
                    ):
                        result.skipped_existing+=1
                        result.image_result(path).skipped_existing+=1
                        if report: report(index,len(paths),path.name)
                        continue
                    analyze_cancelled=getattr(backend,"analyze_with_cancellation",None)
                    analysis=(
                        analyze_cancelled(path,should_cancel)
                        if should_cancel is not None and callable(analyze_cancelled)
                        else backend.analyze(path)
                    )
                    self._raise_if_cancelled(should_cancel)
                    self._consume_with_cost(
                        record,analysis,result,tag_database,auto_merge,run_mode,
                        expected_sidecar,expected_image_hash,
                        refresh_matching=force or matching is not None,
                        mutation_callback=mutation_callback,
                        backend=backend,should_cancel=should_cancel,
                    )
                    finish_timing=getattr(backend,"finish_timing_sample",None)
                    if callable(finish_timing):finish_timing(not bool(should_cancel and should_cancel()))
                except InterruptedError:
                    raise
                except Exception as exc:
                    if isinstance(exc,RuntimeMemoryAdmissionError):observe_remaining('held')
                    finish_timing=getattr(backend,"finish_timing_sample",None)
                    if callable(finish_timing):finish_timing(False)
                    if history_operation is not None and isinstance(exc, (HistoryError, HistoryConfigurationError)):
                        result.history_interrupted = True
                        result.errors.append(f"{path.name}: {exc}")
                        result.image_result(path).errors.append(str(exc))
                        return result
                    detail=self._analysis_error_text(exc)
                    result.errors.append(f"{path.name}: {detail}")
                    result.image_result(path).errors.append(detail)
                    if self._is_systemic_backend_error(exc):
                        if report:
                            report(index,len(paths),path.name)
                        for remaining_index,remaining_path in enumerate(
                            paths[index:],start=index+1,
                        ):
                            result.errors.append(f"{remaining_path.name}: {detail}")
                            result.image_result(remaining_path).errors.append(detail)
                            if report:
                                report(remaining_index,len(paths),remaining_path.name)
                        break
                if report: report(index,len(paths),path.name)
            self._raise_if_cancelled(should_cancel)
            return result
        except InterruptedError:
            result.cancelled=True
            if return_partial_on_cancel:
                return result
            raise
        finally:
            finish_timing=getattr(backend,"finish_timing_sample",None)
            if callable(finish_timing):finish_timing(False)
            backend._runtime_progress=None
            set_cancel=getattr(backend,"set_runtime_cancellation",None)
            if callable(set_cancel):
                set_cancel(None)
            if managed:
                try:
                    if progress:
                        try: progress(total-1,total,f"Releasing {label}…")
                        except InterruptedError: pass
                finally:
                    try: backend.release()
                    except Exception: pass
                if progress:
                    try: progress(total,total,f"Finished {label}")
                    except InterruptedError: pass

    @staticmethod
    def _raise_if_cancelled(should_cancel: Callable[[],bool] | None) -> None:
        if should_cancel is not None and should_cancel():
            raise InterruptedError("AI analysis cancelled safely")

    def _consume_with_cost(self,*args,backend,should_cancel=None,**kwargs):
        identity=None;make=getattr(backend,'saving_identity',None)
        if callable(make):
            try:identity=make(args[0],args[1],args[4],kwargs.get('refresh_matching',False))
            except Exception:pass
        clock=getattr(self,'_cost_clock',time.perf_counter);started=clock()
        self._consume_analysis(*args,**kwargs)
        seconds=max(0,clock()-started)
        if not bool(should_cancel and should_cancel()):
            from .task_costs import observe_cost
            observe_cost(identity,seconds)

    def _consume_analysis(
        self,record,analysis,result,tag_database,auto_merge,run_mode="manual",
        expected_sidecar=_EXPECTED_SIDECAR_UNSET,expected_image_hash: str="",
        *,refresh_matching: bool=False,
        mutation_callback: Callable[[Path,dict,dict,bool,str],None] | None=None,
    ):
        result.observations[record.image_path]=analysis
        matching=next((item for item in reversed(record.annotation.ai_evidence) if
            item.image_hash==expected_image_hash
            and item.model_id==analysis.model_id
            and item.model_revision==analysis.model_revision
            and item.kind==analysis.kind
            and item.prompt==analysis.prompt
            and self._matching_settings(item.settings)==self._matching_settings(analysis.settings)
        ),None)
        original_evidence=record.annotation.ai_evidence
        if refresh_matching and matching is not None:
            record.annotation.ai_evidence=[item for item in original_evidence if item is not matching]
        try:
            cacheable=self.filter_cacheable_result(record,analysis,tag_database)
        finally:
            record.annotation.ai_evidence=original_evidence
        comparison=self.reconcile(record,cacheable,tag_database)
        known_prose={value.casefold() for value in record.annotation.plain_language}
        new_description=(comparison.proposed_plain_language.strip()
                         and comparison.proposed_plain_language.casefold() not in known_prose)
        evidence=self.save_evidence(
            record,cacheable,tag_database,already_filtered=True,run_mode=run_mode,
            expected_sidecar=expected_sidecar,expected_image_hash=expected_image_hash,
            replace_matching=refresh_matching,observed_result=analysis,
            mutation_callback=mutation_callback,
        ); result.processed+=1
        image_result=result.image_result(record.image_path)
        image_result.processed+=1
        if auto_merge and comparison.suggested_tags:
            merged,rules,held=self._merge_suggested_tags(
                record,[evidence],comparison.suggested_tags,tag_database,
                mutation_callback=mutation_callback,
            )
            result.auto_merged+=merged; result.rules_applied+=rules; result.conflicts_held+=held
            image_result.auto_merged+=merged
            image_result.conflicts_held+=held
        if comparison.suggested_tags or new_description:
            result.review_paths.append(record.image_path)
            image_result.review_required=True

    def _analyze_many_batch(
        self,paths,backend,tag_database,progress,force,auto_merge,run_mode="manual",
        *,maximum_age_seconds: float | None=None,
        should_cancel: Callable[[],bool] | None=None,
        mutation_callback: Callable[[Path,dict,dict,bool,str],None] | None=None,
        result: BatchInterrogationResult | None=None,
        history_operation=None,
    ):
        result=result or BatchInterrogationResult(); size=max(1,int(backend.batch_size))
        for path in paths:
            result.image_result(path)
        for start in range(0,len(paths),size):
            self._raise_if_cancelled(should_cancel)
            chunk=paths[start:start+size]; pending=[]
            for path in chunk:
                self._raise_if_cancelled(should_cancel)
                try:
                    record=_preparation_record(path, history_operation)
                    expected_sidecar=_sidecar_fingerprint(record.sidecar_path)
                    expected_image_hash=hash_image(path)
                    matching=self.matching_evidence(
                        record,backend,image_digest=expected_image_hash,
                    )
                    if not force and self.evidence_is_fresh(
                        matching,maximum_age_seconds,
                    ):
                        result.skipped_existing+=1
                        result.image_result(path).skipped_existing+=1
                    else: pending.append((
                        record,expected_sidecar,expected_image_hash,
                        force or matching is not None,
                    ))
                except Exception as exc:
                    if history_operation is not None and isinstance(exc, InterruptedError):
                        raise
                    if history_operation is not None and isinstance(exc, (HistoryError, HistoryConfigurationError)):
                        result.history_interrupted = True
                        result.errors.append(f"{path.name}: {exc}")
                        result.image_result(path).errors.append(str(exc))
                        return result
                    detail=self._analysis_error_text(exc)
                    result.errors.append(f"{path.name}: {detail}")
                    result.image_result(path).errors.append(detail)
            if pending:
                try:
                    analyses=backend.analyze_many([
                        record.image_path for record,_,_,_ in pending
                    ])
                    if len(analyses)!=len(pending): raise RuntimeError("The tagger returned an unexpected batch size.")
                except Exception as exc:
                    if isinstance(exc,RuntimeMemoryAdmissionError):
                        observer=getattr(progress,'observe_inference',None)
                        if callable(observer):observer(backend,len(paths)-start,'held')
                    if history_operation is not None and isinstance(exc, InterruptedError):
                        raise
                    detail=self._analysis_error_text(exc)
                    result.errors.extend(
                        f"{record.image_path.name}: {detail}" for record,_,_,_ in pending
                    )
                    for record,_,_,_ in pending:
                        result.image_result(record.image_path).errors.append(detail)
                    if self._is_systemic_backend_error(exc):
                        remaining=paths[start+len(chunk):]
                        result.errors.extend(f"{path.name}: {detail}" for path in remaining)
                        for path in remaining:
                            result.image_result(path).errors.append(detail)
                        if progress:
                            for offset,path in enumerate(chunk,start=1):
                                progress(start+offset,len(paths),path.name)
                            for offset,path in enumerate(
                                remaining,start=start+len(chunk)+1,
                            ):
                                progress(offset,len(paths),path.name)
                        return result
                else:
                    successful_consumption=0
                    for (
                        record,expected_sidecar,expected_image_hash,refresh_matching
                    ),analysis in zip(pending,analyses):
                        self._raise_if_cancelled(should_cancel)
                        try:
                            self._consume_with_cost(
                                record,analysis,result,tag_database,auto_merge,run_mode,
                                expected_sidecar,expected_image_hash,
                                refresh_matching=refresh_matching,
                                mutation_callback=mutation_callback,
                                backend=backend,should_cancel=should_cancel,
                            )
                            successful_consumption+=1
                        except Exception as exc:
                            if history_operation is not None and isinstance(exc, InterruptedError):
                                raise
                            if history_operation is not None and isinstance(exc, (HistoryError, HistoryConfigurationError)):
                                result.history_interrupted = True
                                result.errors.append(f"{record.image_path.name}: {exc}")
                                result.image_result(record.image_path).errors.append(str(exc))
                                return result
                            detail=self._analysis_error_text(exc)
                            result.errors.append(f"{record.image_path.name}: {detail}")
                            result.image_result(record.image_path).errors.append(detail)
                    finish_timing=getattr(backend,"finish_timing_sample",None)
                    if callable(finish_timing):
                        finish_timing(successful_consumption==len(pending) and not bool(should_cancel and should_cancel()))
            if progress:
                for offset,path in enumerate(chunk,start=1): progress(start+offset,len(paths),path.name)
        return result

    def analyze_taggers(
        self,images,backends,tag_database=None,progress=None,*,force: bool=False,
        auto_merge: bool=False,minimum_agreement: int=1,run_mode: str="manual",
        maximum_age_seconds: float | None=None,
        _manage_lifecycle: bool=True,
        should_cancel: Callable[[],bool] | None=None,
        allow_single_model_promotion: bool=False,
        count_external_evidence_vote: bool=True,
        mutation_callback: Callable[[Path,dict,dict,bool,str],None] | None=None,
        history_operation=None,
    ) -> BatchInterrogationResult:
        from .automation_repository import active_automation_engine

        paths=[Path(value) for value in images]; backends=list(backends); combined=BatchInterrogationResult()
        def native_stop(path, exc):
            combined.cancelled = isinstance(exc, InterruptedError)
            combined.history_interrupted = not combined.cancelled
            combined.errors.append(f"{path.name}: {exc}")
            combined.image_result(path).errors.append(str(exc))
            return combined
        for path in paths:
            combined.image_result(path)
        automation_engine=active_automation_engine(tag_database)
        backend_units=sum(
            len(paths)+(2 if _manage_lifecycle and callable(getattr(backend,"release",None)) else 0)
            for backend in backends
        )
        application_units=len(paths) if auto_merge else 0
        review_units=len(paths)
        total=max(1,backend_units+application_units+review_units)
        offset=0; run_observations=[]
        for backend in backends:
            if should_cancel is not None and should_cancel():
                combined.cancelled=True
                return combined
            managed=_manage_lifecycle and callable(getattr(backend,"release",None)); span=len(paths)+(2 if managed else 0); label=self._backend_label(backend)
            if managed and progress: progress(offset+1,total,f"Loading {label}…")
            def backend_progress(index,_total,name,base=offset+(1 if managed else 0)):
                if progress:progress(base+index,total,name)
            backend_progress.observe_inference=getattr(progress,'observe_inference',None)
            try:
                result=self.analyze_batch(paths,backend,tag_database,
                    backend_progress if progress else None,
                    force=force,auto_merge=False,run_mode=run_mode,
                    maximum_age_seconds=maximum_age_seconds,_manage_lifecycle=False,
                    should_cancel=should_cancel,
                    mutation_callback=mutation_callback,
                    return_partial_on_cancel=True,
                    **({"history_operation": history_operation} if history_operation is not None else {}),
                )
                combined.processed+=result.processed; combined.skipped_existing+=result.skipped_existing
                combined.review_paths.extend(result.review_paths); combined.errors.extend(result.errors)
                for path,item in result.image_results.items():
                    combined.image_result(path).absorb(item)
                run_observations.append((backend,dict(result.observations)))
            finally:
                # Automatic analysis owns its outer lifecycle across chunks,
                # but a resident lease must end after each backend pass so
                # incompatible taggers stay sequenced within one memory budget.
                resident_lease=getattr(backend,"_runtime_lease",None) is not None
                if managed or resident_lease:
                    try:
                        if managed and progress:
                            try: progress(offset+span-1,total,f"Releasing {label}…")
                            except InterruptedError: pass
                    finally:
                        try: backend.release()
                        except Exception: pass
                    if managed and progress:
                        try: progress(offset+span,total,f"Finished {label}")
                        except InterruptedError: pass
            observer=getattr(progress,'observe_inference',None)
            if callable(observer):observer(backend,0,'finished')
            offset+=span
            if history_operation is not None and result.history_interrupted:
                combined.history_interrupted = True
                return combined
            if result.cancelled:
                combined.cancelled=True
                return combined
        distinct_models={
            str(getattr(backend,"model_id","") or type(backend).__name__)
            for backend in backends
        }
        if auto_merge:
            minimum=max(
                1 if allow_single_model_promotion else 2,
                int(minimum_agreement),
            )
            for index,path in enumerate(paths,1):
                if should_cancel is not None and should_cancel():
                    combined.cancelled=True
                    return combined
                if distinct_models and tag_database is not None:
                    try:
                        record=_preparation_record(path, history_operation)
                    except (HistoryError, HistoryConfigurationError, InterruptedError) as exc:
                        if history_operation is None:
                            raise
                        return native_stop(path, exc)
                    evidences=[self.matching_evidence(record,backend) for backend in backends]
                    evidences=[item for item in evidences if item is not None]
                    votes={}; strongest={}
                    confirmed=TagCollection(record.annotation.all_structured_tags()).normalized_set()
                    rejected={normalize_tag(tag) for tag in record.annotation.source.get("rejected_tags",[])}
                    rejected.update(
                        normalize_tag(tag) for tag in record.annotation.confirmed_absent
                    )
                    rejected.update(
                        normalize_tag(tag) for evidence in record.annotation.ai_evidence
                        for tag,decision in evidence.decisions.items()
                        if evidence.kind=="sdxl_tags" and decision=="rejected"
                    )
                    counted_models={}
                    for backend,observations in run_observations:
                        model_key=str(getattr(backend,"model_id","") or type(backend).__name__)
                        if model_key in counted_models:
                            continue
                        counted_models[model_key]=True
                        analysis=observations.get(path)
                        if analysis is not None:
                            predictions=list(analysis.tags)
                        else:
                            cached=self.matching_evidence(record,backend)
                            predictions=[
                                TagPrediction(str(item.get("tag") or ""),float(item.get("confidence",0)),str(item.get("category") or "general"))
                                for item in (cached.typed_observed_tags() if cached is not None else [])
                            ]
                        seen=set()
                        for candidate in predictions:
                            tag=str(candidate.tag).strip()
                            if automation_engine.is_evidence_rejected(tag):
                                continue
                            known=tag_database.find(tag)
                            tag=known.display if known is not None else tag
                            key=normalize_tag(tag)
                            if not key or key in seen or key in rejected or key in confirmed: continue
                            seen.add(key); votes[key]=votes.get(key,0)+1
                            normalized_candidate=TagPrediction(tag,candidate.confidence,candidate.category)
                            if key not in strongest or candidate.confidence>strongest[key].confidence:
                                strongest[key]=normalized_candidate
                    if count_external_evidence_vote:
                        for key in self._pending_source_tag_keys(record,tag_database):
                            if key in votes and key not in rejected:
                                votes[key]+=1
                    predictions=[strongest[key] for key,count in votes.items() if count>=minimum]
                    if predictions:
                        try:
                            merged,rules,held=self._merge_suggested_tags(
                                record,evidences,predictions,tag_database,
                                mutation_callback=mutation_callback,
                            )
                        except (HistoryError, HistoryConfigurationError, InterruptedError) as exc:
                            if history_operation is None:
                                raise
                            return native_stop(path, exc)
                        combined.auto_merged+=merged; combined.rules_applied+=rules; combined.conflicts_held+=held
                        item=combined.image_result(path)
                        item.auto_merged+=merged
                        item.conflicts_held+=held
                if progress:
                    progress(offset+index,total,f"Applying agreed Tags: {path.name}")
            offset+=application_units
        combined.review_paths=[]
        for index,path in enumerate(paths,1):
            if should_cancel is not None and should_cancel():
                combined.cancelled=True
                return combined
            try:
                unresolved=self._has_unresolved_findings(_preparation_record(path, history_operation))
            except (HistoryError, HistoryConfigurationError, InterruptedError) as exc:
                if history_operation is None:
                    raise
                return native_stop(path, exc)
            combined.image_result(path).review_required=unresolved
            if unresolved:
                combined.review_paths.append(path)
            if progress:
                progress(offset+index,total,f"Preparing review findings: {path.name}")
        return combined

    @staticmethod
    def _pending_source_tag_keys(record: ImageRecord,tag_database=None) -> set[str]:
        """Return one-vote imported findings; TXT plus Metadata never count twice."""
        confirmed=TagCollection(record.annotation.all_structured_tags()).normalized_set()
        rejected={normalize_tag(value) for value in record.annotation.source.get("rejected_tags",[])}
        rejected.update(
            normalize_tag(value) for value in record.annotation.confirmed_absent
        )
        keys=set()
        for item in record.annotation.source.get("candidate_findings",[]):
            if not isinstance(item,dict) or item.get("kind")!="tag":
                continue
            sources={str(value).strip().casefold() for value in item.get("sources",[]) if str(value).strip()}
            if not sources&{"txt caption","image metadata"}:
                continue
            value=str(item.get("value") or "").strip()
            known=tag_database.find(value) if tag_database is not None else None
            key=normalize_tag(known.display if known is not None else value)
            if key and key not in confirmed|rejected:
                keys.add(key)
        return keys

    @staticmethod
    def _has_unresolved_findings(record: ImageRecord) -> bool:
        confirmed=TagCollection(record.annotation.all_structured_tags()).normalized_set()
        decisions=ImageFindingDecisionService(record.annotation)
        rejected={normalize_tag(value) for value in record.annotation.source.get("rejected_tags",[])}
        rejected.update(
            normalize_tag(value) for value in record.annotation.confirmed_absent
        )
        rejected_prose=decisions.ignored_description_keys()
        for evidence in record.annotation.ai_evidence:
            if evidence.kind=="sdxl_tags":
                for item in evidence.typed_tags():
                    key=normalize_tag(str(item.get("tag") or ""))
                    if (key and key not in confirmed|rejected
                            and evidence.decisions.get(key) not in {
                                "accepted","rejected",*decisions.DEFERRED_VALUES,
                            }
                            and not decisions.source_is_deferred(
                                "tag",key,evidence.model_id or "Unknown model",
                                evidence_id=evidence.id,
                            )):
                        return True
            elif (
                evidence.kind=="plain_language"
                and evidence.typed_plain_language().strip()
                and evidence.decisions.get("plain_language") not in {
                    "accepted","rejected",*decisions.DEFERRED_VALUES,
                }
            ):
                for sentence in split_plain_language(
                    evidence.typed_plain_language()
                ):
                    key=normalize_plain_language_exclusion(sentence)
                    if (
                        key and key not in rejected_prose
                        and not decisions.source_is_deferred(
                            "description",sentence,
                            evidence.model_id or "Unknown model",
                            evidence_id=evidence.id,
                        )
                    ):
                        return True
        return False

    @staticmethod
    def _conflict_key(match) -> tuple:
        return match.rule.id,match.character_slot,match.ambiguous,tuple(sorted(normalize_tag(value) for value in match.found_tags))

    def _merge_suggested_tags(
        self,record: ImageRecord,evidences,predictions,tag_database=None,*,
        mutation_callback: Callable[[Path,dict,dict,bool,str],None] | None=None,
    ) -> tuple[int,int,int]:
        """Confirm explicitly authorized facts while holding ambiguity for review."""
        from .conflict_rules import ConflictRuleEngine
        from .automation_repository import active_automation_engine
        from .paths import DEFAULTS_DIR
        conflicts=ConflictRuleEngine.load(
            DEFAULTS_DIR/"conflicts.json",tags=tag_database,
        )
        automation_engine=active_automation_engine(tag_database)
        initial_snapshot=copy.deepcopy(record.annotation.to_dict())
        sidecar_existed=record.sidecar_path.exists()
        baseline={self._conflict_key(item) for item in conflicts.evaluate(record)}
        unavailable=unavailable_confirmed_tag_identities(
            record.annotation,tag_database,
        )
        accepted=[]; held=0
        for prediction in predictions:
            candidate=str(prediction.tag).strip()
            if automation_engine.is_evidence_rejected(candidate):
                continue
            if image_tag_identity(candidate,tag_database) in unavailable:
                held += 1
                continue
            if tag_database is not None and tag_database.find(candidate) is None:
                continue
            trial=ImageRecord(record.image_path); trial.annotation=copy.deepcopy(record.annotation)
            trial.add_tags([candidate])
            new_conflicts={self._conflict_key(item) for item in conflicts.evaluate(trial)}-baseline
            if new_conflicts: held+=1; continue
            accepted.append(candidate)
        if not accepted: return 0,0,held
        confirmed_before={
            normalize_tag(value) for value in record.annotation.all_structured_tags()
        }
        for value in accepted:
            if normalize_tag(value) not in confirmed_before:
                record.confirm_tags([value], knowledge=tag_database)
        identity_service=ConfirmedTagIdentityService(tag_database)
        identity_service.synchronize_annotation(record.annotation)
        ConfirmedTagIdentityService.prune_annotation(record.annotation)
        if mutation_callback is not None and record.history_context is None:
            mutation_callback(
                record.image_path,initial_snapshot,
                copy.deepcopy(record.annotation.to_dict()),
                sidecar_existed,"Confirm safely agreed Tags",
            )
        record.modified=True
        self._save_analysis_record(record, _EXPECTED_SIDECAR_UNSET, knowledge=tag_database)
        return len(accepted),0,held

    def reconcile(self, record: ImageRecord, result: InterrogationResult, tag_database=None) -> Reconciliation:
        validate_interrogation_result(result)
        confirmed = {normalize_tag(value) for value in record.annotation.all_structured_tags()}
        cached = {
            normalize_tag(str(item.get("tag") or ""))
            for evidence in record.annotation.ai_evidence for item in evidence.typed_tags()
            if normalize_tag(str(item.get("tag") or ""))
        }
        rejected = unavailable_confirmed_tag_identities(
            record.annotation,tag_database,
        )
        new, existing, refused = [], [], []
        for source_item in result.tags:
            known = tag_database.find(source_item.tag) if tag_database is not None else None
            item = TagPrediction(known.display, source_item.confidence, source_item.category) if known else source_item
            key = normalize_tag(item.tag)
            if image_tag_identity(item.tag,tag_database) in rejected:
                refused.append(item)
            elif key in confirmed or key in cached:
                existing.append(item)
            else:
                new.append(item)
        return Reconciliation(new, existing, refused, result.plain_language.strip())

    def filter_cacheable_result(self,record,result,tag_database=None,automation_engine=None):
        """Keep every novel model observation as reviewable Image Knowledge evidence."""
        validate_interrogation_result(result)
        confirmed=TagCollection(record.annotation.all_structured_tags())
        known_keys=confirmed.normalized_set()
        rejected=unavailable_confirmed_tag_identities(
            record.annotation,tag_database,
        )
        novel=[]; seen=set()
        for source_item in result.tags:
            known=tag_database.find(source_item.tag) if tag_database is not None else None
            item=TagPrediction(known.display,source_item.confidence,source_item.category) if known else source_item
            key=normalize_tag(item.tag)
            if (
                not key or key in known_keys or key in seen
                or image_tag_identity(item.tag,tag_database) in rejected
            ):
                continue
            seen.add(key); novel.append(item)

        descriptions={value.casefold() for value in record.annotation.plain_language}
        descriptions.update(
            evidence.typed_plain_language().casefold() for evidence in record.annotation.ai_evidence
            if evidence.typed_plain_language()
        )
        descriptions.update(
            str(value).strip().casefold() for value in record.annotation.source.get("rejected_descriptions",[])
            if str(value).strip()
        )
        descriptions.update(
            str(item.get("value") or "").strip().casefold()
            for item in record.annotation.source.get("candidate_findings",[])
            if isinstance(item,dict) and item.get("kind")=="description" and str(item.get("value") or "").strip()
        )
        prose=result.plain_language.strip()
        if not prose or prose.casefold() in descriptions:
            prose=""
        filtered=copy.copy(result); filtered.tags=novel; filtered.plain_language=prose
        return filtered

    def save_evidence(
        self,record: ImageRecord,result: InterrogationResult,tag_database=None,*,
        already_filtered=False,automation_engine=None,run_mode: str="manual",
        expected_sidecar=_EXPECTED_SIDECAR_UNSET,expected_image_hash: str="",
        replace_matching: bool=False,observed_result: InterrogationResult|None=None,
        mutation_callback: Callable[[Path,dict,dict,bool,str],None] | None=None,
    ) -> AIEvidence:
        from .automation_repository import active_automation_engine

        engine=automation_engine or active_automation_engine(tag_database)
        before=copy.deepcopy(record.annotation.to_dict())
        sidecar_existed=record.sidecar_path.exists()

        def save_record() -> None:
            ConfirmedTagIdentityService.prune_annotation(record.annotation)
            if mutation_callback is not None and record.history_context is None:
                mutation_callback(
                    record.image_path,before,
                    copy.deepcopy(record.annotation.to_dict()),sidecar_existed,
                    "Save current AI evidence",
                )
            self._save_analysis_record(record,expected_sidecar)
        source_result=observed_result or result
        if not already_filtered:
            result=self.filter_cacheable_result(record,result,tag_database,automation_engine)
        evidence = result_to_evidence(record.image_path,result,observed_result=source_result)
        if expected_image_hash and evidence.image_hash!=expected_image_hash:
            raise RuntimeError("The image changed while AI analysis was running; the result was not saved.")
        if (
            expected_sidecar is not _EXPECTED_SIDECAR_UNSET
            and _sidecar_fingerprint(record.sidecar_path)!=expected_sidecar
        ):
            raise RuntimeError("The image record changed while AI analysis was running; the result was not saved.")
        current=record.annotation.all_structured_tags()
        for item in evidence.tags:
            tag=str(item.get("tag") or "").strip()
            if not tag: continue
            after=TagCollection([*current,tag]).to_list()
            warnings=engine.conflict_warnings(record.image_path,current,after)
            if warnings:
                item["review_required"]=True
                item["conflicts"]=list(dict.fromkeys(warnings))
            current=after
        matching=next((item for item in reversed(record.annotation.ai_evidence) if
            item.image_hash==evidence.image_hash
            and item.model_id==evidence.model_id
            and item.model_revision==evidence.model_revision
            and item.kind==evidence.kind
            and item.prompt==evidence.prompt
            and self._matching_settings(item.settings)==self._matching_settings(evidence.settings)
        ),None)
        if matching is not None and replace_matching:
            matching.tags=[dict(item) for item in evidence.tags]
            matching.observed_tags=[dict(item) for item in evidence.observed_tags]
            matching.plain_language=evidence.plain_language
            matching.created_at=evidence.created_at; matching.raw_output=evidence.raw_output
            self._record_ai_run(record,matching,run_mode)
            record.modified=True; save_record()
            return matching
        if matching is not None and evidence.tags:
            predictions={normalize_tag(str(item.get("tag") or "")):dict(item) for item in matching.tags if normalize_tag(str(item.get("tag") or ""))}
            for item in evidence.tags:
                key=normalize_tag(str(item.get("tag") or ""))
                if key: predictions[key]=dict(item)
            matching.tags=list(predictions.values())
            observations={
                normalize_tag(str(item.get("tag") or "")):dict(item)
                for item in matching.typed_observed_tags()
                if normalize_tag(str(item.get("tag") or ""))
            }
            for item in evidence.observed_tags:
                key=normalize_tag(str(item.get("tag") or ""))
                if key: observations[key]=dict(item)
            matching.observed_tags=list(observations.values())
            matching.created_at=evidence.created_at; matching.raw_output=evidence.raw_output
            self._record_ai_run(record,matching,run_mode)
            record.modified=True; save_record()
            return matching
        if matching is not None and not evidence.tags and not evidence.plain_language:
            matching.observed_tags=[dict(item) for item in evidence.observed_tags]
            matching.created_at=evidence.created_at; matching.raw_output=evidence.raw_output
            self._record_ai_run(record,matching,run_mode)
            record.modified=True; save_record()
            return matching
        if matching is not None and evidence.plain_language==matching.plain_language:
            matching.created_at=evidence.created_at; matching.raw_output=evidence.raw_output
            self._record_ai_run(record,matching,run_mode)
            record.modified=True; save_record()
            return matching
        record.annotation.ai_evidence.append(evidence)
        self._record_ai_run(record,evidence,run_mode)
        record.modified = True
        save_record()
        return evidence

    @staticmethod
    def _save_analysis_record(record: ImageRecord,expected_sidecar, *, knowledge=None) -> None:
        with _preparation_write_guard(record.history_context, record.image_path):
            AIInterrogationService._save_admitted_analysis_record(record, expected_sidecar, knowledge=knowledge)

    @staticmethod
    def _save_admitted_analysis_record(record: ImageRecord,expected_sidecar, *, knowledge=None) -> None:
        if expected_sidecar is None:
            if not record.create_sidecar():
                raise RuntimeError(
                    "An image record appeared while AI analysis was running; the result was not saved."
                )
            return
        if (
            expected_sidecar is not _EXPECTED_SIDECAR_UNSET
            and _sidecar_fingerprint(record.sidecar_path)!=expected_sidecar
        ):
            raise RuntimeError(
                "The image record changed while AI analysis was being saved; the result was not saved."
            )
        record.save_sidecar(force=True, **({"knowledge": knowledge} if knowledge is not None else {}))

    @staticmethod
    def _record_ai_run(record: ImageRecord,evidence: AIEvidence,run_mode: str) -> None:
        identity={
            "model_id":evidence.model_id,"model_revision":evidence.model_revision,
            "kind":evidence.kind,"prompt":evidence.prompt,
            "settings":AIInterrogationService._matching_settings(evidence.settings),
        }
        key=hashlib.sha256(json.dumps(
            identity,sort_keys=True,separators=(",",":"),ensure_ascii=False,default=str,
        ).encode("utf-8")).hexdigest()
        runs=record.annotation.source.get("ai_analysis_runs",{})
        if not isinstance(runs,dict): runs={}
        runs[key]={
            **identity,"mode":str(run_mode or "manual"),"status":"complete",
            "completed_at":evidence.created_at,"image_hash":evidence.image_hash,
            "evidence_id":evidence.id,
        }
        record.annotation.source["ai_analysis_runs"]=runs

    def accept_tags(
        self, record: ImageRecord, evidence: AIEvidence, tags: list[str],
        *, tag_database,
    ) -> None:
        record.confirm_sdxl_tags(tags,knowledge=tag_database)
        ImageFindingDecisionService(
            record.annotation,knowledge=tag_database,
        ).accept_confirmed_tags(tags)
        record.modified = True
        record.save_sidecar(force=True,knowledge=tag_database)

    def reject_tags(self, record: ImageRecord, evidence: AIEvidence, tags: list[str]) -> None:
        ImageFindingDecisionService(record.annotation).ignore_for_image(
            tags=tags,
        )
        record.modified = True
        record.save_sidecar(force=True)

    def accept_plain_language(self,record:ImageRecord,evidence:AIEvidence,text:str) -> None:
        values=split_plain_language(text)
        record.annotation.plain_language=merge_plain_language_in_source_order(
            record.annotation.plain_language,values,(values,),
        )
        ImageFindingDecisionService(
            record.annotation,
        ).accept_confirmed_descriptions(record.annotation.plain_language)
        record.modified=True; record.save_sidecar(force=True)


@dataclass
class _WDRuntimeResource:
    session: object
    rows: list[dict]
    provider: str


from .task_costs import RuntimeCostLearning


class WDEva02Backend(RuntimeCostLearning):
    """Optional local ONNX backend compatible with the WD v3 model files."""

    def __init__(
        self, model_dir: Path, general_threshold: float = 0.35, character_threshold: float = 0.85,
        rating_threshold: float = 0.35, *, model_id: str=WD_EVA02_MODEL,
        execution_provider: str="auto", batch_size: int=1,
        runtime_manager: LocalAIRuntimeManager|None=None,
    ):
        self.model_dir = Path(model_dir)
        self.model_id=model_id
        self.general_threshold = general_threshold
        self.character_threshold = character_threshold
        self.rating_threshold = rating_threshold
        self.execution_provider=execution_provider if execution_provider in {"auto","cuda","cpu"} else "auto"
        self.batch_size=max(1,min(16,int(batch_size)))
        self._session = None
        self._rows = None
        self._provider=""
        self.runtime_manager=runtime_manager
        self._runtime_lease: RuntimeLease|None=None
        self._owned_runtime: _WDRuntimeResource|None=None
        self._runtime_should_cancel: Callable[[],bool]|None=None
        self._timing_store=None;self._timing_key=None;self._pending_timing_sample=None
        self._timing_clock=time.perf_counter;self._timing_load_seconds=0.0;self._timing_wait_seconds=0.0
        self._timing_failed=False;self._timing_failed_attempt_seconds=0.0

    def begin_timing_request(self):
        # Resolve this request's current configuration after successful runtime
        # use. Previous keys are not evidence for changed settings or placement.
        self._timing_key=None;self._pending_timing_sample=None;self._timing_failed=False
        self.begin_cost_request()

    def bind_timing_store(self,store):
        if self._timing_store is not store:
            self._timing_key=None;self._pending_timing_sample=None
            self._ephemeral_timing_store=None;self._ephemeral_cost_store=None
        self._timing_store=store

    def finish_timing_sample(self,success=True):
        self.finish_loading_cost(success)
        pending=self._pending_timing_sample;self._pending_timing_sample=None
        if pending is None:return
        if not success:
            self._timing_failed=True;return
        store,key,seconds,count=pending
        store.observe(key,seconds,count)

    def _timed_runtime_spec(self,spec):
        if self._timing_store is None:return spec
        loader=spec.loader;identity=self.prepare_loading_cost(spec)
        callback=getattr(self,'_runtime_progress',None)
        if callback:callback('Loading model files')
        def load():
            started=self._timing_clock();succeeded=False
            try:
                resource=loader();succeeded=True;return resource
            finally:
                elapsed=max(0,self._timing_clock()-started);self._timing_load_seconds+=elapsed
                if succeeded:self.note_loading_cost(identity,elapsed,spec.key.provider if hasattr(spec,'key') else '')
        return replace(spec,loader=load)

    def _inference_timing_identity(self,count):
        provider=self._provider or "CPUExecutionProvider"
        environment=wd_benchmark_environment();hardware=None
        if "cuda" in provider.casefold():
            probe=getattr(self.runtime_manager,"hardware_probe",None)
            if probe is None:
                from .dependencies import nvidia_gpu_profile
                probe=nvidia_gpu_profile
            try:
                profile=probe(0)
                if profile is not None:hardware={'name':profile.name,'memory_mib':profile.total_memory_mib,'device_index':0}
            except (OSError,ValueError,TypeError,AttributeError):pass
        elif environment.get("processor"):
            hardware={key:environment.get(key) for key in ('processor','cpu_count','machine')}
        from .inference_timing import InferenceTimingStore
        if hardware is None:
            if not getattr(self,"_ephemeral_timing_store",None):self._ephemeral_timing_store=InferenceTimingStore()
            store=self._ephemeral_timing_store
        else:store=self._timing_store
        key=InferenceTimingStore.fingerprint({'model_id':self.model_id,'revision':installed_model_revision(self.model_dir,self.model_id),
            'files':_runtime_model_fingerprint(self.model_dir,self.model_id,('model.onnx','selected_tags.csv')),
            'settings':self.evidence_identity()['settings'],'provider':provider,'batch_images':count,
            'configured_batch_size':self.batch_size,'environment':environment,'hardware':hardware,
            'dtype':'float32','threads':max(1,min(2,(os.cpu_count() or 2)//2))})
        return store,key,hardware is None

    def remaining_inference_estimate(self,remaining):
        if remaining<=0:return (0.0,0.0)
        if self._timing_failed or self._timing_key is None:return (None,None)
        store,key,_session_only=self._timing_key
        estimate=store.estimate(key,remaining)
        return estimate.low_seconds,estimate.high_seconds

    def timing_progress_detail(self,remaining):
        if self._timing_failed:return "WD inference ETA: unavailable (last inference failed or was cancelled)"
        if self._timing_key is None:
            detail="WD inference ETA: collecting successful inference timings"
        else:
            store,key,session_only=self._timing_key
            detail=store.estimate(key,remaining).text(remaining)
            if session_only:detail+="; session-only, hardware unverified"
            elif not store.persistence_available:detail+="; session samples only"
        return detail+f"; model loading: {self._timing_load_seconds:.0f}s; runtime wait/checks: {self._timing_wait_seconds:.0f}s; failed attempts: {self._timing_failed_attempt_seconds:.0f}s (all excluded)"

    def bind_runtime_manager(self,manager: LocalAIRuntimeManager) -> None:
        if self.runtime_manager is None:
            self.runtime_manager=manager

    def set_runtime_memory_override(self,allowed: bool) -> None:
        self._allow_memory_pressure=bool(allowed)

    def set_runtime_cancellation(self,callback: Callable[[],bool]|None) -> None:
        self._runtime_should_cancel=callback

    @staticmethod
    def preferred_providers(available,preference: str="auto") -> list[str]:
        values=set(available)
        if preference!="cpu" and "CUDAExecutionProvider" in values: return ["CUDAExecutionProvider","CPUExecutionProvider"]
        return ["CPUExecutionProvider"]

    @classmethod
    def device_label(cls) -> str:
        try:
            import onnxruntime as ort
            if cls.preferred_providers(ort.get_available_providers())[0]!="CUDAExecutionProvider": return "CPU"
            packaged=importlib.util.find_spec("nvidia.cublas") and importlib.util.find_spec("nvidia.cudnn")
            try:
                import torch
                torch_ready=bool(torch.cuda.is_available())
            except Exception: torch_ready=False
            return "NVIDIA GPU (CUDA)" if packaged or torch_ready else "CPU fallback (CUDA libraries missing)"
        except Exception: return "runtime not installed"

    def evidence_identity(self) -> dict:
        # Matching deliberately excludes execution_provider: an existing result
        # must remain reusable even when the optional native runtime cannot load.
        return {
            "model_id":self.model_id,
            "model_revision":installed_model_revision(self.model_dir,self.model_id),
            "kind":"sdxl_tags","prompt":"",
            "settings":{"general_threshold":self.general_threshold,"character_threshold":self.character_threshold,"rating_threshold":self.rating_threshold,"execution_provider":self.execution_provider},
        }

    @staticmethod
    def _session_options(ort):
        options=ort.SessionOptions(); options.intra_op_num_threads=max(1,min(2,(os.cpu_count() or 2)//2)); options.inter_op_num_threads=1
        if hasattr(ort,"ExecutionMode"): options.execution_mode=ort.ExecutionMode.ORT_SEQUENTIAL
        return options

    def release(self) -> None:
        lease=self._runtime_lease; owned=self._owned_runtime
        self._runtime_lease=None; self._owned_runtime=None
        self._session=None; self._rows=None; self._provider=""
        if lease is not None:
            lease.close()
        elif owned is not None:
            self._release_runtime(owned)
        else:
            gc.collect()

    def _prepare_cuda_runtime(self,ort) -> None:
        if hasattr(ort,"preload_dlls"):
            # ONNX Runtime's default order deliberately prefers the CUDA/cuDNN
            # DLLs bundled with PyTorch, then falls back to NVIDIA site packages.
            # Forcing the latter can mix incompatible cuDNN companions in one
            # process when WD and JoyCaption run in the same job.
            try: ort.preload_dlls()
            except Exception: pass

    @staticmethod
    def _release_runtime(resource: _WDRuntimeResource) -> None:
        resource.session=None; resource.rows.clear(); gc.collect()

    @classmethod
    def _load_runtime(
        cls,model_dir: Path,execution_provider: str,
    ) -> _WDRuntimeResource:
        _ensure_library_output_streams()
        try:
            import onnxruntime as ort
        except (ImportError,OSError) as exc:
            raise RuntimeError(
                "The AI Tag runtime could not load. In Setup, open AI Tools & "
                "Models > Manage AI Tags and repair the shared AI Tag runtime, "
                "then restart CollectiGen and retry. Existing image results "
                "remain saved."
            ) from exc
        model=Path(model_dir)/"model.onnx"
        labels=Path(model_dir)/"selected_tags.csv"
        if not model.is_file() or not labels.is_file():
            raise FileNotFoundError("WD model files are not downloaded yet.")
        providers=(
            ["CUDAExecutionProvider","CPUExecutionProvider"]
            if execution_provider=="CUDAExecutionProvider" else
            ["CPUExecutionProvider"]
        )
        if providers[0]=="CUDAExecutionProvider":
            # Use the same compatible PyTorch-first preload order as analysis.
            if hasattr(ort,"preload_dlls"):
                try: ort.preload_dlls()
                except Exception: pass
        options=cls._session_options(ort)
        session=ort.InferenceSession(
            str(model),sess_options=options,providers=providers,
        )
        with labels.open("r",encoding="utf-8") as handle:
            rows=list(csv.DictReader(handle))
        return _WDRuntimeResource(session,rows,session.get_providers()[0])

    def runtime_spec(
        self,selected_provider: str|None=None,
    ) -> LocalAIRuntimeSpec:
        model_dir=self.model_dir
        provider=self.execution_provider
        loader=type(self)._load_runtime
        releaser=type(self)._release_runtime
        try:
            import onnxruntime as ort
            selected=(selected_provider or self.preferred_providers(
                ort.get_available_providers(),provider,
            )[0])
        except Exception:
            selected=selected_provider or (
                "CUDAExecutionProvider" if provider=="cuda" else
                "CPUExecutionProvider" if provider=="cpu" else "auto"
            )
        revision=installed_model_revision(model_dir,self.model_id)
        load_settings={"execution_provider":provider,"selected_provider":selected}
        key=LocalAIRuntimeKey(
            self.model_id,revision,
            _runtime_model_fingerprint(
                model_dir,self.model_id,("model.onnx","selected_tags.csv"),
            ),
            wd_benchmark_environment_fingerprint(),selected,0,"float32","none",
            _runtime_settings_fingerprint(load_settings),
        )
        return LocalAIRuntimeSpec(
            key,self.model_id.rsplit("/",1)[-1],"wd",1536,
            loader=lambda root=model_dir,choice=selected,load=loader:load(root,choice),
            releaser=releaser,
            health_check=lambda resource:bool(
                getattr(resource,"session",None) is not None
                and getattr(resource,"rows",None)
            ),
        )

    def _resident_runtime(self) -> _WDRuntimeResource:
        if self._runtime_lease is not None:
            return self._runtime_lease.resource
        if self._owned_runtime is not None:
            return self._owned_runtime
        acquisition_started=self._timing_clock();load_before=self._timing_load_seconds
        spec=self._timed_runtime_spec(self.runtime_spec())
        try:
            if self.runtime_manager is not None:
                self._runtime_lease=self.runtime_manager.acquire(
                    spec,should_cancel=self._runtime_should_cancel,
                    allow_memory_pressure=bool(getattr(self,"_allow_memory_pressure",False)),
                )
                resource=self._runtime_lease.resource
            else:
                if self._runtime_should_cancel and self._runtime_should_cancel():
                    raise InterruptedError("AI runtime loading cancelled safely")
                resource=spec.loader(); self._owned_runtime=resource
        except (InterruptedError,RuntimeMemoryAdmissionError):
            raise
        except Exception:
            if "cuda" not in spec.key.provider.casefold():
                raise
            spec=self._timed_runtime_spec(self.runtime_spec("CPUExecutionProvider"))
            if self.runtime_manager is not None:
                self._runtime_lease=self.runtime_manager.acquire(
                    spec,should_cancel=self._runtime_should_cancel,
                    allow_memory_pressure=bool(getattr(self,"_allow_memory_pressure",False)),
                )
                resource=self._runtime_lease.resource
            else:
                resource=spec.loader(); self._owned_runtime=resource
        if self._runtime_should_cancel and self._runtime_should_cancel():
            self.release()
            raise InterruptedError("AI runtime loading cancelled safely")
        self._timing_wait_seconds+=max(0,self._timing_clock()-acquisition_started-(self._timing_load_seconds-load_before))
        self._session=resource.session; self._rows=resource.rows
        self._provider=resource.provider
        callback=getattr(self,"_runtime_progress",None)
        if callback:callback("Running inference with")
        return resource

    @staticmethod
    def download(model_dir: Path,model_id: str=WD_EVA02_MODEL) -> Path:
        _ensure_library_output_streams()
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise RuntimeError("Install huggingface-hub before downloading AI models.") from exc
        acquisition=MODEL_ACQUISITIONS[model_id]
        downloaded=Path(snapshot_download(
            repo_id=model_id,
            revision=acquisition.revision,
            local_dir=Path(model_dir),
            allow_patterns=("model.onnx", "selected_tags.csv", "config.json"),
        ))
        _record_model_installation(Path(model_dir),acquisition)
        return downloaded

    def analyze_many(self, images: Iterable[Path]) -> list[InterrogationResult]:
        self._timing_failed=False;self._pending_timing_sample=None
        try:return self._analyze_many_with_timing(images)
        except BaseException:
            self._timing_failed=True
            raise

    def _analyze_many_with_timing(self, images: Iterable[Path]) -> list[InterrogationResult]:
        _ensure_library_output_streams()
        try:
            import numpy as np
            import onnxruntime as ort
            from PIL import Image
        except ImportError as exc:
            raise RuntimeError("WD analysis requires Pillow, NumPy, and ONNX Runtime.") from exc
        model = self.model_dir / "model.onnx"
        resource=self._resident_runtime()
        session = resource.session
        input_meta = session.get_inputs()[0]
        height = int(input_meta.shape[1])
        arrays=[]
        for image in images:
            source = Image.open(image).convert("RGBA")
            background = Image.new("RGBA", source.size, "WHITE")
            background.alpha_composite(source)
            rgb = background.convert("RGB")
            size = max(rgb.size)
            square = Image.new("RGB", (size, size), "WHITE")
            square.paste(rgb, ((size - rgb.width) // 2, (size - rgb.height) // 2))
            square = square.resize((height, height), Image.Resampling.BICUBIC)
            arrays.append(np.asarray(square, dtype=np.float32)[:, :, ::-1][None, ...])
        if not arrays: return []
        array=np.concatenate(arrays,axis=0)
        inference_started=self._timing_clock()
        try:
            probability_batches = session.run(None, {input_meta.name: array})[0]
        except Exception:
            if self._provider!="CUDAExecutionProvider": raise
            self._timing_failed_attempt_seconds+=max(0,self._timing_clock()-inference_started)
            self._loading_cost_identity=None;self._pending_load_costs=[]
            callback=getattr(self,"_runtime_progress",None)
            if callback:callback("Loading CPU fallback after CUDA inference failed")
            fallback_started=self._timing_clock()
            try:resource.session=ort.InferenceSession(str(model),sess_options=self._session_options(ort),providers=["CPUExecutionProvider"])
            finally:self._timing_load_seconds+=max(0,self._timing_clock()-fallback_started)
            resource.provider="CPUExecutionProvider"
            self._session=resource.session; self._provider=resource.provider
            if self.runtime_manager is not None and self._runtime_lease is not None:
                key=self._runtime_lease.key
                self.runtime_manager.invalidate(
                    lambda candidate,target=key:candidate==target,
                    reason="CUDA provider fallback",
                )
            session=self._session; input_meta=session.get_inputs()[0]
            if callback:callback("Running inference with")
            inference_started=self._timing_clock()
            probability_batches=session.run(None,{input_meta.name:array})[0]
        inference_seconds=self._timing_clock()-inference_started
        rows = resource.rows
        results=[]
        for probabilities in probability_batches:
            predictions=[]
            for row, probability in zip(rows, probabilities):
                category_code = str(row.get("category", "0"))
                category = "character" if category_code == "4" else "rating" if category_code == "9" else "general"
                threshold = self.character_threshold if category == "character" else self.rating_threshold if category == "rating" else self.general_threshold
                if float(probability) >= threshold:
                    predictions.append(TagPrediction(row.get("name", "").replace("_", " "), float(probability), category))
            predictions.sort(key=lambda item: item.confidence, reverse=True)
            results.append(InterrogationResult(
                model_id=self.model_id,kind="sdxl_tags",tags=predictions,
                model_revision=installed_model_revision(self.model_dir,self.model_id),
                settings={"general_threshold":self.general_threshold,"character_threshold":self.character_threshold,
                          "rating_threshold":self.rating_threshold,"execution_provider":self._provider or "CPUExecutionProvider"},
            ))
        cancelled=bool(self._runtime_should_cancel and self._runtime_should_cancel())
        self._timing_failed=cancelled or len(results)!=len(arrays)
        if len(results)==len(arrays) and not cancelled:
            self._timing_failed=False
            if self._timing_store is not None:
                self._timing_key=self._inference_timing_identity(len(arrays))
                store,key,_session_only=self._timing_key
                self._pending_timing_sample=(store,key,inference_seconds,len(arrays))
        return results

    def analyze(self, image: Path) -> InterrogationResult:
        return self.analyze_many([image])[0]

    def benchmark(
        self,images: Path|Iterable[Path],batch_sizes=(1,2,4,8,16),*,repeats: int=2,
        should_cancel: Callable[[],bool] | None=None,
        providers: Iterable[str]=("cuda","cpu"),
    ) -> dict:
        """Measure representative cold/warm throughput without changing settings."""
        if isinstance(images,(str,Path)):
            paths=[Path(images)]
        else:
            paths=[Path(value) for value in images]
        unique=[]
        for path in paths:
            if path not in unique:
                unique.append(path)
        paths=unique[:WD_BENCHMARK_SAMPLE_LIMIT]
        if not paths:
            raise ValueError("Choose at least one representative image.")
        sizes=tuple(sorted({
            max(1,min(16,int(value))) for value in batch_sizes
        }))
        repeat_count=max(1,min(5,int(repeats)))
        measurements={}
        selected_providers=tuple(dict.fromkeys(
            value for value in (str(item).casefold() for item in providers)
            if value in {"cuda","cpu"}
        ))
        if not selected_providers:
            raise ValueError("Choose at least one benchmark execution provider.")
        for preference in selected_providers:
            AIInterrogationService._raise_if_cancelled(should_cancel)
            backend=type(self)(
                self.model_dir,self.general_threshold,self.character_threshold,self.rating_threshold,
                model_id=self.model_id,execution_provider=preference,
            )
            try:
                started=time.perf_counter(); first=backend.analyze(paths[0]); cold=time.perf_counter()-started
                batches={}
                output_fingerprint=""
                for size in sizes:
                    AIInterrogationService._raise_if_cancelled(should_cancel)
                    elapsed_values=[]
                    try:
                        for repeat in range(repeat_count):
                            AIInterrogationService._raise_if_cancelled(should_cancel)
                            batch=[
                                paths[(repeat*size+offset)%len(paths)]
                                for offset in range(size)
                            ]
                            started=time.perf_counter()
                            results=backend.analyze_many(batch)
                            AIInterrogationService._raise_if_cancelled(should_cancel)
                            if len(results)!=len(batch):
                                raise RuntimeError("The tagger returned an unexpected batch size.")
                            expected_provider=(
                                "CUDAExecutionProvider" if preference=="cuda"
                                else "CPUExecutionProvider"
                            )
                            actual_providers={
                                str(result.settings.get("execution_provider",""))
                                for result in results
                            }
                            if actual_providers!={expected_provider}:
                                actual=", ".join(sorted(value or "unknown" for value in actual_providers))
                                raise RuntimeError(
                                    f"{preference.upper()} batch used {actual} instead of "
                                    f"{expected_provider}."
                                )
                            elapsed_values.append(time.perf_counter()-started)
                            if not output_fingerprint and size>=len(paths):
                                tag_sets=[
                                    sorted(normalize_tag(value.tag) for value in result.tags)
                                    for result in results[:len(paths)]
                                ]
                                output_fingerprint=hashlib.sha256(json.dumps(
                                    tag_sets,separators=(",",":"),ensure_ascii=True,
                                ).encode("utf-8")).hexdigest()
                    except InterruptedError:
                        raise
                    except Exception as exc:
                        batches[str(size)]={"error":str(exc)}
                        continue
                    elapsed=max(float(median(elapsed_values)),1e-9)
                    batches[str(size)]={
                        "seconds":elapsed,
                        "seconds_per_image":elapsed/size,
                        "images_per_second":size/elapsed,
                        "repetitions":repeat_count,
                        "seconds_samples":elapsed_values,
                    }
                if not output_fingerprint:
                    tags=sorted(normalize_tag(value.tag) for value in first.tags)
                    output_fingerprint=hashlib.sha256(json.dumps(
                        [tags],separators=(",",":"),ensure_ascii=True,
                    ).encode("utf-8")).hexdigest()
                measurements[preference]={
                    "cold_seconds":cold,"provider":first.settings.get("execution_provider",""),
                    "batches":batches,"output_fingerprint":output_fingerprint,
                }
            except InterruptedError:
                raise
            except Exception as exc:
                measurements[preference]={"error":str(exc)}
            finally:
                backend.release()
        candidates=[]
        for preference,measurement in measurements.items():
            if measurement.get("error") or (preference=="cuda" and measurement.get("provider")!="CUDAExecutionProvider"): continue
            for size,value in measurement["batches"].items():
                seconds_per_image=float(value.get("seconds_per_image",0) or 0)
                if seconds_per_image>0:
                    candidates.append((seconds_per_image,preference,int(size)))
        if not candidates: raise RuntimeError("No benchmark configuration completed successfully.")
        fastest=min(value[0] for value in candidates)
        near_fastest=[value for value in candidates if value[0]<=fastest*1.05]
        seconds,preference,size=min(near_fastest,key=lambda value:(value[2],value[0]))
        cpu_output=measurements.get("cpu",{}).get("output_fingerprint","")
        gpu_output=measurements.get("cuda",{}).get("output_fingerprint","")
        output_agreement=not cpu_output or not gpu_output or cpu_output==gpu_output
        for measurement in measurements.values():
            if isinstance(measurement,dict):
                measurement.pop("output_fingerprint",None)
        configuration={
            "model_id":self.model_id,
            "model_revision":installed_model_revision(self.model_dir,self.model_id),
            "thresholds":{
                "general":float(self.general_threshold),
                "character":float(self.character_threshold),
                "rating":float(self.rating_threshold),
            },
        }
        return {
            "schema_version":WD_BENCHMARK_SCHEMA_VERSION,
            "measured_at":datetime.now(timezone.utc).isoformat(),
            "model_id":self.model_id,"measurements":measurements,
            "recommended_provider":preference,"recommended_batch_size":size,
            "seconds_per_image":seconds,
            "output_agreement":output_agreement,
            "sample_count":len(paths),"repetitions":repeat_count,
            "batch_sizes":list(sizes),"configuration":configuration,
            "environment":wd_benchmark_environment(),
            "environment_fingerprint":wd_benchmark_environment_fingerprint(),
        }


def configured_wd_backends(
    settings,*,runtime_manager: LocalAIRuntimeManager|None=None,
) -> list[WDEva02Backend]:
    enabled = [
        value for value in settings.enabled_wd_taggers if value in WD_TAGGER_MODELS
    ] or [WD_EVA02_MODEL]
    root = Path(settings.ai_models_directory)
    return [
        WDEva02Backend(
            root / WD_TAGGER_MODELS[model_id][1],
            settings.wd_general_threshold,
            settings.wd_character_threshold,
            settings.wd_rating_threshold,
            model_id=model_id,
            execution_provider=settings.wd_execution_provider,
            batch_size=settings.wd_batch_size,
            runtime_manager=runtime_manager,
        )
        for model_id in enabled
    ]


class JoyCaptionBackend(RuntimeCostLearning):
    """Lazy local plain-language captioning through the official HF model API."""

    DEFAULT_PROMPT = DEFAULT_JOYCAPTION_PROMPT
    supports_purpose_prompts = True

    def __init__(
        self,model_dir:Path,prompt:str="",max_new_tokens:int=256,
        known_facts:list[str]|None=None,runtime_manager: LocalAIRuntimeManager|None=None,
        **generation,
    ) -> None:
        self.model_dir=Path(model_dir)
        self.model_id=JOYCAPTION_MODEL
        self.prompt=prompt.strip() or self.DEFAULT_PROMPT
        self.max_new_tokens=max_new_tokens
        self.known_facts=[str(value).strip() for value in (known_facts or []) if str(value).strip()]
        self.generation=dict(generation)
        self._pipeline=None
        self.runtime_manager=runtime_manager
        self._runtime_lease: RuntimeLease|None=None
        self._owned_runtime=None
        self._runtime_should_cancel: Callable[[],bool]|None=None
        self._timing_store=None;self._timing_key=None;self._pending_timing_sample=None
        self._timing_clock=time.perf_counter;self._timing_load_seconds=0.0;self._timing_wait_seconds=0.0
        self._timing_failed=False

    def begin_timing_request(self):
        # Resolve this request's current configuration after successful runtime
        # use. Previous keys are not evidence for changed settings or placement.
        self._timing_key=None;self._pending_timing_sample=None;self._timing_failed=False
        self.begin_cost_request()

    def bind_timing_store(self,store):
        if self._timing_store is not store:
            self._timing_key=None;self._pending_timing_sample=None;self._ephemeral_timing_store=None;self._ephemeral_cost_store=None
        self._timing_store=store if self.model_id==JOYCAPTION_MODEL else None

    def finish_timing_sample(self,success=True):
        self.finish_loading_cost(success)
        pending=self._pending_timing_sample;self._pending_timing_sample=None
        if pending is None:return
        if not success:self._timing_failed=True;return
        store,key,seconds,tokens=pending
        try:store.observe_caption(key,seconds,tokens)
        except Exception:pass  # optional telemetry must not invalidate saved results

    def _caption_timing_identity(self,prompt):
        from .inference_timing import InferenceTimingStore
        device=self._benchmark_device();environment=joy_benchmark_environment();hardware=None
        if device.get('kind')=='cuda':
            probe=getattr(self.runtime_manager,'hardware_probe',None)
            if probe is None:
                from .dependencies import nvidia_gpu_profile
                probe=nvidia_gpu_profile
            try:
                index=int(device.get('index',self.generation.get('gpu_index',0)));profile=probe(index)
                if profile is not None:hardware={'index':index,'name':profile.name,'memory_mib':profile.total_memory_mib}
            except Exception:pass
        elif device.get('kind')=='cpu' and environment.get('processor'):
            hardware={key:environment.get(key) for key in ('processor','cpu_count','machine')}
        if hardware is None:
            if getattr(self,'_ephemeral_timing_store',None) is None:self._ephemeral_timing_store=InferenceTimingStore()
            store=self._ephemeral_timing_store
        else:store=self._timing_store
        # Context-length bucket separates substantially different inputs without
        # retaining user facts or creating a unique baseline for every image.
        config={'model':self.model_id,'revision':installed_model_revision(self.model_dir,self.model_id),
                'files':_runtime_model_fingerprint(self.model_dir,self.model_id,('config.json',)),
                'generation':self._generation_settings(),'quantization':self._quantization_mode(),
                'device':device,'environment':environment,'hardware':hardware,
                'instruction':hashlib.sha256(getattr(self,'_timing_instruction',self.prompt).encode('utf-8')).hexdigest(),
                'context_length_bucket':len(prompt)//256}
        return store,InferenceTimingStore.fingerprint(config),hardware is None

    def _completion_tokens(self,text):
        processor=getattr(self._pipeline,'processor',None)
        tokenizer=getattr(processor,'tokenizer',None) or getattr(self._pipeline,'tokenizer',None)
        try:
            if tokenizer is not None and callable(getattr(tokenizer,'encode',None)):
                count=len(tokenizer.encode(text,add_special_tokens=False))
                return count if count>0 else None
        except Exception:pass
        return None

    def remaining_inference_estimate(self,remaining):
        if remaining<=0:return (0.0,0.0)
        if self.model_id!=JOYCAPTION_MODEL or self._timing_failed or self._timing_key is None:return (None,None)
        store,key,_session_only=self._timing_key
        estimate=store.caption_estimate(key,remaining,self.max_new_tokens)
        return estimate.low_seconds,estimate.high_seconds

    def timing_progress_detail(self,remaining):
        if self.model_id!=JOYCAPTION_MODEL:return ''
        if self._timing_failed:detail='JoyCaption inference ETA: unavailable (last request failed or was cancelled)'
        elif self._timing_key is None:detail='JoyCaption inference ETA: collecting successful inference timings'
        else:
            store,key,session_only=self._timing_key
            detail=store.caption_detail(key,remaining,self.max_new_tokens)
            if session_only:detail+='; session-only, hardware/placement unverified'
            elif not store.persistence_available:detail+='; session samples only'
        return detail+f'; model loading: {self._timing_load_seconds:.0f}s; runtime wait/checks: {self._timing_wait_seconds:.0f}s (excluded); inference includes pipeline preprocessing; no whole-task ETA'

    def bind_runtime_manager(self,manager: LocalAIRuntimeManager) -> None:
        if self.runtime_manager is None:
            self.runtime_manager=manager

    def set_runtime_memory_override(self,allowed: bool) -> None:
        self._allow_memory_pressure=bool(allowed)

    def set_runtime_cancellation(self,callback: Callable[[],bool]|None) -> None:
        self._runtime_should_cancel=callback

    def _grounded_prompt(self) -> str:
        value=self.prompt
        if self.known_facts:
            value += (
                "\n\nUse these already-confirmed facts as the factual basis of the description: "
                + ", ".join(self.known_facts)
                + ". Express them naturally and consistently. Do not contradict them. "
                "You may add clearly visible context, but do not invent uncertain identity or details."
            )
        return value

    def _generation_settings(self) -> dict:
        settings={
            "max_new_tokens":self.max_new_tokens,
            "min_new_tokens":int(self.generation.get("min_new_tokens",30)),
            "num_beams":int(self.generation.get("num_beams",4)),
            "length_penalty":float(self.generation.get("length_penalty",1.0)),
            "do_sample":bool(self.generation.get("do_sample",False)),
            "repetition_penalty":float(self.generation.get("repetition_penalty",1.0)),
            "no_repeat_ngram_size":int(self.generation.get("no_repeat_ngram_size",3)),
        }
        if settings["do_sample"]:
            settings.update(temperature=float(self.generation.get("temperature",0.5)),top_k=int(self.generation.get("top_k",50)),top_p=float(self.generation.get("top_p",0.9)))
        return settings

    def _benchmark_configuration(self) -> dict[str,object]:
        return {
            "model_id":JOYCAPTION_MODEL,
            "model_revision":installed_model_revision(self.model_dir,JOYCAPTION_MODEL),
            "prompt_fingerprint":hashlib.sha256(self.prompt.encode("utf-8")).hexdigest(),
            "generation":{
                **self._generation_settings(),
                "gpu_index":int(self.generation.get("gpu_index",0)),
                "quantization":self._quantization_mode(),
            },
        }

    def _quantization_mode(self) -> str:
        mode=str(self.generation.get("quantization","bf16") or "bf16")
        return mode if mode in {"bf16","nf4"} else "bf16"

    def _benchmark_device(self) -> dict[str,object]:
        pipeline=self._pipeline
        model=getattr(pipeline,"model",None)
        mapped=getattr(model,"hf_device_map",None)
        values=list(mapped.values()) if isinstance(mapped,dict) and mapped else []
        if not values:
            values=[
                value for value in (
                    getattr(pipeline,"device",None),
                    getattr(model,"device",None),
                )
                if value is not None
            ]
        normalized=[]
        indexes=[]
        for value in values:
            text=str(value).casefold()
            if isinstance(value,int) or text.isdigit() or text.startswith("cuda"):
                normalized.append("cuda")
                try:
                    indexes.append(int(value if isinstance(value,int) else text.rsplit(":",1)[-1]))
                except (TypeError,ValueError):
                    pass
            elif text=="disk":
                normalized.append("disk")
            elif text.startswith("cpu"):
                normalized.append("cpu")
            else:
                normalized.append(text or "unknown")
        kinds=set(normalized)
        kind=(
            "cuda" if kinds=={"cuda"} else
            "cpu" if kinds=={"cpu"} else
            next(iter(kinds),"unknown") if len(kinds)==1 else
            "mixed"
        )
        gpu_index=indexes[0] if indexes else int(self.generation.get("gpu_index",0))
        dtype=str(getattr(model,"dtype","") or "").removeprefix("torch.")
        result={"kind":kind}
        if dtype:
            result["precision"]=dtype
        result["quantization"]=(
            "nf4" if bool(getattr(model,"is_loaded_in_4bit",False)) else "bf16"
        )
        try:
            import torch
            if kind=="cuda" and torch.cuda.is_available():
                result.update(
                    index=gpu_index,name=str(torch.cuda.get_device_name(gpu_index)),
                )
        except Exception:
            pass
        return result

    def _benchmark_token_count(self,text: str) -> int:
        """Count generated tokens when available, with a nonsemantic word fallback."""
        processor=getattr(self._pipeline,"processor",None)
        tokenizer=getattr(processor,"tokenizer",None) or getattr(self._pipeline,"tokenizer",None)
        try:
            if tokenizer is not None and hasattr(tokenizer,"encode"):
                return max(0,len(tokenizer.encode(text,add_special_tokens=False)))
        except Exception:
            pass
        return len(str(text or "").split())

    def evidence_identity(self) -> dict:
        settings=self._generation_settings()
        return {
            "model_id":JOYCAPTION_MODEL,
            "model_revision":installed_model_revision(self.model_dir,JOYCAPTION_MODEL),
            "kind":"plain_language","prompt":self._grounded_prompt(),
            "settings":{
                **settings,
                "gpu_index":int(self.generation.get("gpu_index",0)),
                "quantization":self._quantization_mode(),
                "grounded_facts":list(self.known_facts),
            },
        }

    @staticmethod
    def download(model_dir:Path) -> Path:
        _ensure_library_output_streams()
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise RuntimeError("Install huggingface-hub before downloading JoyCaption.") from exc
        acquisition=MODEL_ACQUISITIONS[JOYCAPTION_MODEL]
        downloaded=Path(snapshot_download(
            repo_id=JOYCAPTION_MODEL,revision=acquisition.revision,local_dir=Path(model_dir),
        ))
        _record_model_installation(Path(model_dir),acquisition)
        return downloaded

    @classmethod
    def _load_pipeline_resource(cls,model_dir: Path,gpu_index: int,mode: str):
        _ensure_library_output_streams()
        try:
            from transformers import pipeline
        except ImportError as exc:
            raise RuntimeError("JoyCaption requires Transformers and PyTorch.") from exc
        if not (Path(model_dir)/"config.json").is_file():
            raise FileNotFoundError("JoyCaption model files are not downloaded yet.")
        try:
            import torch
        except ImportError as exc:
            raise RuntimeError("JoyCaption requires the reviewed PyTorch CUDA runtime.") from exc
        if torch.cuda.is_available():
            if gpu_index<0 or gpu_index>=int(torch.cuda.device_count()):
                raise RuntimeError(
                    f"Configured GPU {gpu_index} is unavailable. Choose an installed "
                    "NVIDIA GPU in Advanced Generation."
                )
            load_options={
                "device_map":gpu_index,"torch_dtype":torch.bfloat16,
                "model_kwargs":{"local_files_only":True},
            }
            if mode=="nf4":
                try:
                    import bitsandbytes  # noqa: F401
                    from transformers import BitsAndBytesConfig
                except (ImportError,OSError,RuntimeError) as exc:
                    raise RuntimeError(
                        "The selected 4-bit JoyCaption mode requires the reviewed "
                        "BitsAndBytes component. Update Plain Language components "
                        "in Setup, then try again."
                    ) from exc
                load_options["model_kwargs"].update({
                    "quantization_config":BitsAndBytesConfig(
                        load_in_4bit=True,
                        bnb_4bit_quant_type="nf4",
                        bnb_4bit_compute_dtype=torch.bfloat16,
                        bnb_4bit_quant_storage=torch.bfloat16,
                        bnb_4bit_use_double_quant=True,
                    ),
                })
        else:
            if mode=="nf4":
                raise RuntimeError(
                    "The selected 4-bit JoyCaption mode requires an available "
                    "NVIDIA GPU. Choose Full BF16 for the recoverable CPU path."
                )
            # Retain the existing recoverable analysis path. The speed benchmark
            # diagnoses and rejects this placement rather than mislabelling it.
            load_options={
                "device_map":{"":"cpu"},"torch_dtype":"auto",
                "model_kwargs":{"local_files_only":True},
            }
        loaded=pipeline(
            "image-text-to-text",model=str(model_dir),
            trust_remote_code=False,**load_options,
        )
        if mode=="nf4":
            cls._patch_nf4_vision_attention(
                getattr(loaded,"model",None),torch,bitsandbytes,
            )
        return loaded

    @staticmethod
    def _release_pipeline_resource(resource) -> None:
        # Break the heavyweight object graph while the releaser still receives
        # the pipeline wrapper; allocator cleanup must not run with model
        # tensors retained by that wrapper.
        for name in ("model","processor","tokenizer","image_processor"):
            try:
                if hasattr(resource,name): setattr(resource,name,None)
            except Exception:
                pass
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available(): torch.cuda.empty_cache()
        except ImportError: pass

    def runtime_spec(self) -> LocalAIRuntimeSpec:
        model_dir=self.model_dir; model_id=self.model_id
        gpu_index=int(self.generation.get("gpu_index",0))
        mode=self._quantization_mode()
        try:
            import torch
            cuda=bool(torch.cuda.is_available())
        except Exception:
            cuda=False
        provider="cuda" if cuda else "cpu"
        dtype="bfloat16" if cuda else "auto"
        load_settings={
            "model_id":model_id,"gpu_index":gpu_index,
            "quantization":mode,"provider":provider,"dtype":dtype,
        }
        loader=type(self)._load_pipeline_resource
        releaser=type(self)._release_pipeline_resource
        key=LocalAIRuntimeKey(
            model_id,installed_model_revision(model_dir,model_id),
            _runtime_model_fingerprint(model_dir,model_id,("config.json",)),
            joy_benchmark_environment_fingerprint(),provider,gpu_index,dtype,mode,
            _runtime_settings_fingerprint(load_settings),
        )
        # Conservative task peak, not a model-weight measurement. The local
        # four-beam NF4 characterization reserved ~8 GiB with only 100 output
        # tokens. Budget full requested decode length and vision/workspace
        # overhead; do not reduce quality settings to fit available memory.
        beams=max(1,int(self.generation.get("num_beams",4)))
        tokens=max(1,int(self.max_new_tokens))
        decode_mib=(beams*tokens+1)//2
        estimate=(9216 if mode=="nf4" else 18*1024)+decode_mib
        if model_id==FLORENCE2_MODEL:
            estimate=4096+decode_mib
        return LocalAIRuntimeSpec(
            key,PLAIN_LANGUAGE_MODELS.get(model_id,(model_id,"",""))[0],"joy",
            estimate,
            loader=lambda root=model_dir,index=gpu_index,memory=mode,load=loader:
                load(root,index,memory),
            releaser=releaser,
            inference_headroom_mib=3072+decode_mib,
            health_check=lambda resource:getattr(resource,"model",None) is not None,
        )

    def _load(self,should_cancel: Callable[[],bool]|None=None):
        if self._pipeline is not None:
            return self._pipeline
        callback=should_cancel or self._runtime_should_cancel
        spec=self.runtime_spec()
        acquisition_started=self._timing_clock();load_before=self._timing_load_seconds
        if self._timing_store is not None:
            loader=spec.loader;identity=self.prepare_loading_cost(spec)
            report=getattr(self,'_runtime_progress',None)
            if report:report('Loading model files')
            def timed_load():
                started=self._timing_clock();succeeded=False
                try:
                    resource=loader();succeeded=True;return resource
                finally:
                    elapsed=max(0,self._timing_clock()-started);self._timing_load_seconds+=elapsed
                    if succeeded:self.note_loading_cost(identity,elapsed,spec.key.provider if hasattr(spec,'key') else '')
            spec=replace(spec,loader=timed_load)
        if self.runtime_manager is not None:
            self._runtime_lease=self.runtime_manager.acquire(
                spec,should_cancel=callback,
                allow_memory_pressure=bool(getattr(self,"_allow_memory_pressure",False)),
            )
            self._pipeline=self._runtime_lease.resource
        else:
            if callback is not None and callback():
                raise InterruptedError("AI runtime loading cancelled safely")
            self._owned_runtime=spec.loader()
            self._pipeline=self._owned_runtime
            if callback is not None and callback():
                self.release()
                raise InterruptedError("AI runtime loading cancelled safely")
        if self._timing_store is not None:
            self._timing_wait_seconds+=max(0,self._timing_clock()-acquisition_started-(self._timing_load_seconds-load_before))
        callback=getattr(self,"_runtime_progress",None)
        if callback:callback("Running inference with")
        return self._pipeline

    @staticmethod
    def _patch_nf4_vision_attention(model,torch,bitsandbytes) -> None:
        """Keep JoyCaption's unsupported vision attention projection in BF16."""
        try:
            attention=model.vision_tower.vision_model.head.attention
            linear4bit=bitsandbytes.nn.Linear4bit
        except AttributeError as exc:
            raise RuntimeError(
                "The installed JoyCaption model or BitsAndBytes runtime does "
                "not expose the reviewed NF4 vision-attention structure."
            ) from exc
        if not isinstance(attention.out_proj,linear4bit):
            return
        # MultiheadAttention consumes out_proj.weight through its functional
        # path instead of calling Linear4bit.forward(), so the packed weight is
        # interpreted as a 1 x N matrix. TagGUI and JoyCaption's documented NF4
        # path replace this unused vision-head projection after model loading.
        attention.out_proj=torch.nn.Linear(
            in_features=attention.embed_dim,
            out_features=attention.embed_dim,
            device=model.device,
            dtype=torch.bfloat16,
        )
        attention.out_proj.eval()

    def release(self) -> None:
        lease=self._runtime_lease; owned=self._owned_runtime
        self._runtime_lease=None; self._owned_runtime=None; self._pipeline=None
        if lease is not None:
            lease.close()
        elif owned is not None:
            self._release_pipeline_resource(owned)
        else:
            gc.collect()

    @staticmethod
    def _text(output:object,formatted_prompt: str="",user_prompt: str="") -> str:
        return generated_completion_text(
            output,formatted_prompt=formatted_prompt,user_prompt=user_prompt,
        )

    def analyze(self,image:Path) -> InterrogationResult:
        return self.analyze_with_cancellation(image,None)

    def analyze_with_cancellation(
        self,image: Path,should_cancel: Callable[[],bool] | None,
        *,prompt_override: str | None = None,
    ) -> InterrogationResult:
        self._pending_timing_sample=None
        try:return self._analyze_caption_with_timing(image,should_cancel,prompt_override=prompt_override)
        except BaseException:
            self._timing_failed=True;self._pending_timing_sample=None
            raise

    def _analyze_caption_with_timing(
        self,image: Path,should_cancel: Callable[[],bool] | None,
        *,prompt_override: str | None = None,
    ) -> InterrogationResult:
        from PIL import Image
        AIInterrogationService._raise_if_cancelled(should_cancel)
        with Image.open(image) as opened:
            source=opened.convert("RGB")
        if prompt_override is not None and (not isinstance(prompt_override,str)
                or not prompt_override.strip() or len(prompt_override)>2048):
            raise ValueError("Choose a bounded, nonempty image suggestion instruction.")
        grounded_prompt=prompt_override.strip() if prompt_override is not None else self._grounded_prompt()
        self._timing_instruction=prompt_override.strip() if prompt_override is not None else self.prompt
        pipeline=self._load(should_cancel)
        AIInterrogationService._raise_if_cancelled(should_cancel)
        messages=[
            {"role":"system","content":"You are a helpful image captioner."},
            {"role":"user","content":grounded_prompt},
        ]
        processor=getattr(pipeline,"processor",None)
        if processor is None or not hasattr(processor,"apply_chat_template"):
            raise RuntimeError("The installed JoyCaption processor does not support its required chat template.")
        formatted_prompt=processor.apply_chat_template(messages,tokenize=False,add_generation_prompt=True)
        persistent_settings=self._generation_settings()
        kwargs=dict(persistent_settings)
        if should_cancel is not None:
            from transformers import StoppingCriteria,StoppingCriteriaList
            class CancellationStoppingCriteria(StoppingCriteria):
                def __call__(self,*_args,**_kwargs):
                    return bool(should_cancel())
            kwargs["stopping_criteria"]=StoppingCriteriaList([CancellationStoppingCriteria()])
        max_new_tokens=kwargs.pop("max_new_tokens")
        inference_started=self._timing_clock()
        output=pipeline(
            images=source,text=formatted_prompt,max_new_tokens=max_new_tokens,
            generate_kwargs=kwargs,
        )
        inference_seconds=max(0,self._timing_clock()-inference_started)
        AIInterrogationService._raise_if_cancelled(should_cancel)
        completion=self._text(output,formatted_prompt=formatted_prompt,user_prompt=grounded_prompt)
        if self._timing_store is not None:
            try:
                self._timing_key=self._caption_timing_identity(grounded_prompt)
                store,key,_session_only=self._timing_key
                self._pending_timing_sample=(store,key,inference_seconds,self._completion_tokens(completion))
                self._timing_failed=False
            except Exception:self._timing_key=None;self._pending_timing_sample=None
        return InterrogationResult(
            model_id=JOYCAPTION_MODEL,kind="plain_language",
            model_revision=installed_model_revision(self.model_dir,JOYCAPTION_MODEL),
            plain_language=completion,prompt=grounded_prompt,
            settings={
                **persistent_settings,
                "gpu_index":int(self.generation.get("gpu_index",0)),
                "quantization":self._quantization_mode(),
                "grounded_facts":list(self.known_facts),
            },
            raw_output=output,
        )

    def benchmark(
        self,images: Path|Iterable[Path],*,repeats: int=2,
        should_cancel: Callable[[],bool] | None=None,
        progress: Callable[[int,int,str],None] | None=None,
    ) -> dict:
        """Measure the current caption configuration without retaining its captions."""
        if isinstance(images,(str,Path)):
            paths=[Path(images)]
        else:
            paths=[Path(value) for value in images]
        unique=[]
        for path in paths:
            if path not in unique:
                unique.append(path)
        paths=unique[:JOY_BENCHMARK_SAMPLE_LIMIT]
        if not paths:
            raise ValueError("Choose at least one representative image.")
        repeat_count=max(1,min(3,int(repeats)))
        total=1+len(paths)*repeat_count
        completed=0
        warm_seconds=[]
        token_counts=[]
        report=progress or (lambda *_args:None)
        try:
            AIInterrogationService._raise_if_cancelled(should_cancel)
            report(0,total,"Loading Plain Language model and measuring the first image")
            started=time.perf_counter()
            first=self.analyze_with_cancellation(paths[0],should_cancel)
            cold=max(time.perf_counter()-started,1e-9)
            del first
            device=self._benchmark_device()
            if device.get("kind")!="cuda":
                actual=str(device.get("kind") or "unknown").upper()
                raise RuntimeError(
                    "The Plain Language model did not execute entirely on the "
                    f"configured NVIDIA GPU (actual placement: {actual}). The "
                    "slow result was not saved; repair the local AI runtime or "
                    "choose the correct GPU and try again."
                )
            expected_quantization=self._quantization_mode()
            if device.get("quantization")!=expected_quantization:
                actual=str(device.get("quantization") or "unknown").upper()
                raise RuntimeError(
                    "The Plain Language model did not use the selected memory "
                    f"mode (expected {expected_quantization.upper()}, actual "
                    f"{actual}). The result was not saved; repair the Plain "
                    "Language components and try again."
                )
            completed=1
            report(completed,total,"First image complete; measuring warm generation speed")
            for repeat in range(repeat_count):
                for index,path in enumerate(paths,1):
                    AIInterrogationService._raise_if_cancelled(should_cancel)
                    report(
                        completed,total,
                        f"Warm pass {repeat+1:,} of {repeat_count:,} "
                        f"• image {index:,} of {len(paths):,}",
                    )
                    started=time.perf_counter()
                    result=self.analyze_with_cancellation(path,should_cancel)
                    elapsed=max(time.perf_counter()-started,1e-9)
                    warm_seconds.append(elapsed)
                    token_counts.append(self._benchmark_token_count(result.plain_language))
                    completed+=1
                    report(completed,total,f"Completed {completed:,} of {total:,} benchmark steps")
            seconds_per_image=max(float(median(warm_seconds)),1e-9)
            elapsed_total=sum(warm_seconds)
            generated_tokens=sum(token_counts)
            return {
                "schema_version":JOY_BENCHMARK_SCHEMA_VERSION,
                "measured_at":datetime.now(timezone.utc).isoformat(),
                "model_id":self.model_id,
                "seconds_per_image":seconds_per_image,
                "cold_seconds":cold,
                "tokens_per_second":generated_tokens/elapsed_total if elapsed_total else 0.0,
                "median_generated_tokens":float(median(token_counts)) if token_counts else 0.0,
                "sample_count":len(paths),
                "repetitions":repeat_count,
                "warm_measurement_count":len(warm_seconds),
                "warm_seconds_min":min(warm_seconds),
                "warm_seconds_max":max(warm_seconds),
                "device":device,
                "configuration":self._benchmark_configuration(),
                "environment":joy_benchmark_environment(),
                "environment_fingerprint":joy_benchmark_environment_fingerprint(),
            }
        finally:
            self.release()

    def compare_memory_modes(
        self,images: Path|Iterable[Path],*,
        should_cancel: Callable[[],bool] | None=None,
        progress: Callable[[int,int,str],None] | None=None,
    ) -> dict:
        """Generate a bounded, non-persistent BF16/NF4 caption comparison."""
        if self._generation_settings()["do_sample"]:
            raise ValueError(
                "Turn off varied/random wording and save the deterministic "
                "Plain Language settings before comparing BF16 and NF4."
            )
        if isinstance(images,(str,Path)):
            paths=[Path(images)]
        else:
            paths=[Path(value) for value in images]
        unique=[]
        for path in paths:
            if path not in unique:
                unique.append(path)
        paths=unique[:JOY_BENCHMARK_SAMPLE_LIMIT]
        if not paths:
            raise ValueError("Choose at least one representative image.")

        modes=("bf16","nf4")
        total=len(paths)*len(modes)
        completed=0
        report=progress or (lambda *_args:None)
        captions={mode:[] for mode in modes}
        devices={}
        for mode in modes:
            generation={**self.generation,"quantization":mode}
            backend=type(self)(
                self.model_dir,prompt=self.prompt,
                max_new_tokens=self.max_new_tokens,
                known_facts=self.known_facts,**generation,
            )
            try:
                for index,path in enumerate(paths,1):
                    AIInterrogationService._raise_if_cancelled(should_cancel)
                    label="Full BF16" if mode=="bf16" else "4-bit NF4"
                    report(
                        completed,total,
                        f"Generating {label} caption "
                        f"{index:,} of {len(paths):,}",
                    )
                    result=backend.analyze_with_cancellation(
                        path,should_cancel,
                    )
                    if index==1:
                        device=backend._benchmark_device()
                        if device.get("kind")!="cuda":
                            actual=str(
                                device.get("kind") or "unknown"
                            ).upper()
                            raise RuntimeError(
                                "The Plain Language model did not execute "
                                "entirely on the configured NVIDIA GPU while "
                                f"comparing {label} (actual placement: "
                                f"{actual}). No comparison was kept."
                            )
                        if device.get("quantization")!=mode:
                            actual=str(
                                device.get("quantization") or "unknown"
                            ).upper()
                            raise RuntimeError(
                                "The Plain Language model did not use the "
                                f"requested {label} mode (actual mode: "
                                f"{actual}). No comparison was kept."
                            )
                        devices[mode]=device
                    captions[mode].append(result.plain_language)
                    completed+=1
                    report(
                        completed,total,
                        f"Completed {completed:,} of {total:,} captions",
                    )
            finally:
                backend.release()

        configuration=copy.deepcopy(self._benchmark_configuration())
        generation=configuration.get("generation",{})
        if isinstance(generation,dict):
            generation.pop("quantization",None)
        configuration["compared_modes"]=list(modes)
        return {
            "schema_version":JOY_QUALITY_COMPARISON_SCHEMA_VERSION,
            "model_id":JOYCAPTION_MODEL,
            "comparison_kind":"memory_mode",
            "variants":list(modes),
            "variant_labels":{
                "bf16":"Full BF16",
                "nf4":"4-bit NF4",
            },
            "configuration":configuration,
            "devices":devices,
            "sample_count":len(paths),
            "samples":[
                {
                    "sample_number":index+1,
                    "captions":{
                        mode:captions[mode][index] for mode in modes
                    },
                }
                for index in range(len(paths))
            ],
        }


    def compare_token_limits(
        self,images: Path|Iterable[Path],*,
        reference_limit: int=100,
        should_cancel: Callable[[],bool] | None=None,
        progress: Callable[[int,int,str],None] | None=None,
    ) -> dict:
        """Generate a bounded, non-persistent NF4 output-limit comparison."""
        settings=self._generation_settings()
        if settings["do_sample"]:
            raise ValueError(
                "Turn off varied/random wording and save the deterministic "
                "Plain Language settings before comparing output lengths."
            )
        saved_limit=max(1,int(self.max_new_tokens))
        short_limit=max(1,int(reference_limit))
        if saved_limit<=short_limit:
            raise ValueError(
                "Set and save the maximum Plain Language Description length "
                f"above {short_limit:,} tokens before comparing it with the "
                f"{short_limit:,}-token TagGUI reference."
            )
        if int(settings["min_new_tokens"])>short_limit:
            raise ValueError(
                "Set and save the minimum Plain Language Description length "
                f"to {short_limit:,} tokens or fewer before comparing output "
                "length limits."
            )
        if isinstance(images,(str,Path)):
            paths=[Path(images)]
        else:
            paths=[Path(value) for value in images]
        unique=[]
        for path in paths:
            if path not in unique:
                unique.append(path)
        paths=unique[:JOY_BENCHMARK_SAMPLE_LIMIT]
        if not paths:
            raise ValueError("Choose at least one representative image.")

        limits=(saved_limit,short_limit)
        variants=tuple(f"tokens_{limit}" for limit in limits)
        total=len(paths)*len(limits)
        completed=0
        report=progress or (lambda *_args:None)
        captions={variant:[] for variant in variants}
        generation={**self.generation,"quantization":"nf4"}
        backend=type(self)(
            self.model_dir,prompt=self.prompt,
            max_new_tokens=saved_limit,
            known_facts=self.known_facts,**generation,
        )
        device={}
        try:
            for variant,limit in zip(variants,limits):
                backend.max_new_tokens=limit
                for index,path in enumerate(paths,1):
                    AIInterrogationService._raise_if_cancelled(should_cancel)
                    report(
                        completed,total,
                        f"Generating up to {limit:,}-token caption "
                        f"{index:,} of {len(paths):,}",
                    )
                    result=backend.analyze_with_cancellation(
                        path,should_cancel,
                    )
                    if index==1:
                        actual_device=backend._benchmark_device()
                        if actual_device.get("kind")!="cuda":
                            actual=str(
                                actual_device.get("kind") or "unknown"
                            ).upper()
                            raise RuntimeError(
                                "The Plain Language model did not execute "
                                "entirely on the configured NVIDIA GPU while "
                                "comparing output lengths (actual placement: "
                                f"{actual}). No comparison was kept."
                            )
                        if actual_device.get("quantization")!="nf4":
                            actual=str(
                                actual_device.get("quantization") or "unknown"
                            ).upper()
                            raise RuntimeError(
                                "The Plain Language model did not use the "
                                "required 4-bit NF4 mode while comparing output "
                                f"lengths (actual mode: {actual}). No comparison "
                                "was kept."
                            )
                        device=actual_device
                    captions[variant].append(result.plain_language)
                    completed+=1
                    report(
                        completed,total,
                        f"Completed {completed:,} of {total:,} captions",
                    )
        finally:
            backend.release()

        configuration=copy.deepcopy(self._benchmark_configuration())
        generation=configuration.get("generation",{})
        if isinstance(generation,dict):
            generation["quantization"]="nf4"
            generation.pop("max_new_tokens",None)
        configuration["compared_token_limits"]=list(limits)
        return {
            "schema_version":JOY_QUALITY_COMPARISON_SCHEMA_VERSION,
            "model_id":JOYCAPTION_MODEL,
            "comparison_kind":"token_limit",
            "variants":list(variants),
            "variant_labels":{
                variant:f"Up to {limit:,} tokens"
                for variant,limit in zip(variants,limits)
            },
            "configuration":configuration,
            "device":device,
            "sample_count":len(paths),
            "samples":[
                {
                    "sample_number":index+1,
                    "captions":{
                        variant:captions[variant][index]
                        for variant in variants
                    },
                }
                for index in range(len(paths))
            ],
        }


class Florence2Backend(JoyCaptionBackend):
    """Native, offline-only Florence-2 detailed-caption adapter."""

    TASK_PROMPT = "<MORE_DETAILED_CAPTION>"
    supports_purpose_prompts = False

    def __init__(
        self,model_dir: Path,max_new_tokens: int=256,
        known_facts: list[str]|None=None,**generation,
    ) -> None:
        super().__init__(
            model_dir,prompt=self.TASK_PROMPT,
            max_new_tokens=max_new_tokens,known_facts=known_facts,**generation,
        )
        self.model_id=FLORENCE2_MODEL

    def _grounded_prompt(self) -> str:
        return self.TASK_PROMPT

    def _quantization_mode(self) -> str:
        return "bf16"

    def compare_memory_modes(self,*_args,**_kwargs):
        raise ValueError(
            "The BF16/NF4 comparison applies only to JoyCaption."
        )

    def compare_token_limits(self,*_args,**_kwargs):
        raise ValueError(
            "The controlled TagGUI token-limit comparison applies only to "
            "JoyCaption."
        )

    def _benchmark_configuration(self) -> dict[str,object]:
        return {
            "model_id":FLORENCE2_MODEL,
            "model_revision":installed_model_revision(
                self.model_dir,FLORENCE2_MODEL,
            ),
            "prompt_fingerprint":hashlib.sha256(
                self.TASK_PROMPT.encode("utf-8")
            ).hexdigest(),
            "generation":{
                **self._generation_settings(),
                "gpu_index":int(self.generation.get("gpu_index",0)),
                "quantization":"bf16",
            },
        }

    def evidence_identity(self) -> dict:
        return {
            "model_id":FLORENCE2_MODEL,
            "model_revision":installed_model_revision(
                self.model_dir,FLORENCE2_MODEL,
            ),
            "kind":"plain_language","prompt":self.TASK_PROMPT,
            "settings":{
                **self._generation_settings(),
                "gpu_index":int(self.generation.get("gpu_index",0)),
                "quantization":"bf16",
                "task":self.TASK_PROMPT,
                "grounding_supported":False,
            },
        }

    @staticmethod
    def download(model_dir: Path) -> Path:
        _ensure_library_output_streams()
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise RuntimeError(
                "Install the reviewed Plain Language runtime before downloading Florence-2."
            ) from exc
        acquisition=MODEL_ACQUISITIONS[FLORENCE2_MODEL]
        downloaded=Path(snapshot_download(
            repo_id=FLORENCE2_MODEL,revision=acquisition.revision,
            local_dir=Path(model_dir),
        ))
        _record_model_installation(Path(model_dir),acquisition)
        return downloaded

    @classmethod
    def _load_pipeline_resource(cls,model_dir: Path,gpu_index: int,_mode: str):
        _ensure_library_output_streams()
        try:
            import torch
            from transformers import AutoProcessor,Florence2ForConditionalGeneration
        except ImportError as exc:
            raise RuntimeError(
                "Florence-2 requires the reviewed Transformers and PyTorch runtime."
            ) from exc
        if not (Path(model_dir)/"config.json").is_file():
            raise FileNotFoundError("Florence-2 model files are not downloaded yet.")
        if not torch.cuda.is_available():
            raise RuntimeError(
                "The reviewed Florence-2 adapter requires a supported NVIDIA GPU."
            )
        if gpu_index<0 or gpu_index>=int(torch.cuda.device_count()):
            raise RuntimeError(
                f"Configured GPU {gpu_index} is unavailable. Choose an installed "
                "NVIDIA GPU in Advanced Generation."
            )
        processor=AutoProcessor.from_pretrained(
            str(model_dir),local_files_only=True,use_fast=False,
            trust_remote_code=False,
        )
        model=Florence2ForConditionalGeneration.from_pretrained(
            str(model_dir),local_files_only=True,
            trust_remote_code=False,
            device_map=gpu_index,dtype=torch.bfloat16,
        )
        return SimpleNamespace(
            processor=processor,model=model,device=model.device,
        )

    def analyze_with_cancellation(
        self,image: Path,should_cancel: Callable[[],bool] | None,
    ) -> InterrogationResult:
        from PIL import Image
        AIInterrogationService._raise_if_cancelled(should_cancel)
        with Image.open(image) as opened:
            source=opened.convert("RGB")
        runtime=self._load(should_cancel)
        AIInterrogationService._raise_if_cancelled(should_cancel)
        try:
            import torch
        except ImportError as exc:
            raise RuntimeError(
                "Florence-2 requires the reviewed PyTorch runtime."
            ) from exc
        inputs=runtime.processor(
            text=self.TASK_PROMPT,images=source,return_tensors="pt",
        ).to(runtime.model.device,torch.bfloat16)
        generation=self._generation_settings()
        if should_cancel is not None:
            from transformers import StoppingCriteria,StoppingCriteriaList
            class CancellationStoppingCriteria(StoppingCriteria):
                def __call__(self,*_args,**_kwargs):
                    return bool(should_cancel())
            generation["stopping_criteria"]=StoppingCriteriaList([
                CancellationStoppingCriteria(),
            ])
        output=runtime.model.generate(**inputs,**generation)
        AIInterrogationService._raise_if_cancelled(should_cancel)
        raw=runtime.processor.batch_decode(
            output,skip_special_tokens=False,
        )[0]
        parsed=runtime.processor.post_process_generation(
            raw,task=self.TASK_PROMPT,image_size=source.size,
        )
        caption=str(parsed.get(self.TASK_PROMPT) or "").strip()
        if not caption:
            raise RuntimeError("Florence-2 returned no usable detailed caption.")
        persistent_settings=self._generation_settings()
        return InterrogationResult(
            model_id=FLORENCE2_MODEL,kind="plain_language",
            model_revision=installed_model_revision(
                self.model_dir,FLORENCE2_MODEL,
            ),
            plain_language=caption,prompt=self.TASK_PROMPT,
            settings={
                **persistent_settings,
                "gpu_index":int(self.generation.get("gpu_index",0)),
                "quantization":"bf16",
                "task":self.TASK_PROMPT,
                "grounding_supported":False,
            },
            raw_output={"generated_text":raw,"parsed":parsed},
        )


def configured_plain_language_backend(
    settings,known_facts=(),*,runtime_manager: LocalAIRuntimeManager|None=None,
):
    """Build the explicitly selected reviewed Plain Language adapter."""
    model_id=str(
        getattr(settings,"plain_language_model",JOYCAPTION_MODEL)
        or JOYCAPTION_MODEL
    )
    common={
        "max_new_tokens":normalized_plain_language_max_new_tokens(
            settings.joycaption_max_new_tokens
        ),
        "known_facts":list(known_facts),
        "min_new_tokens":settings.joycaption_min_new_tokens,
        "num_beams":settings.joycaption_num_beams,
        "length_penalty":settings.joycaption_length_penalty,
        "do_sample":settings.joycaption_do_sample,
        "temperature":settings.joycaption_temperature,
        "top_k":settings.joycaption_top_k,
        "top_p":settings.joycaption_top_p,
        "repetition_penalty":settings.joycaption_repetition_penalty,
        "no_repeat_ngram_size":settings.joycaption_no_repeat_ngram_size,
        "gpu_index":settings.joycaption_gpu_index,
    }
    if model_id==FLORENCE2_MODEL:
        return Florence2Backend(
            Path(settings.ai_models_directory)
            /PLAIN_LANGUAGE_MODELS[FLORENCE2_MODEL][1],
            runtime_manager=runtime_manager,
            **common,
        )
    return JoyCaptionBackend(
        Path(settings.ai_models_directory)
        /PLAIN_LANGUAGE_MODELS[JOYCAPTION_MODEL][1],
        prompt=settings.joycaption_prompt,
        quantization=getattr(settings,"joycaption_quantization","bf16"),
        runtime_manager=runtime_manager,
        **common,
    )


def configured_runtime_specs(
    settings,*,capabilities: Iterable[str]=("wd","joy"),
    readiness: Callable[[str,str,Path],bool]|None=None,
) -> tuple[LocalAIRuntimeSpec,...]:
    """Build preload-safe specs from saved settings without semantic run state."""
    selected={str(value).casefold() for value in capabilities}
    unknown=selected-{"wd","joy"}
    if unknown:
        raise ValueError("Unknown local AI capabilities: "+", ".join(sorted(unknown)))
    backends=[]
    if "wd" in selected:
        backends.extend(configured_wd_backends(settings))
    if "joy" in selected:
        backends.append(configured_plain_language_backend(settings))
    specs=[]
    for backend in backends:
        capability="wd" if isinstance(backend,WDEva02Backend) else "joy"
        model_dir=Path(backend.model_dir)
        model_id=str(backend.model_id)
        required=("model.onnx","selected_tags.csv") if capability=="wd" else ("config.json",)
        ready=(
            all((model_dir/name).is_file() for name in required)
            if readiness is None else
            bool(readiness(capability,model_id,model_dir))
        )
        if ready:
            specs.append(backend.runtime_spec())
    return tuple(specs)
