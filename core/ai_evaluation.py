from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Callable, Iterable


AI_BASELINE_EVALUATION_SCHEMA_VERSION = 1
AI_BASELINE_EVALUATION_SAMPLE_LIMIT = 4

AI_EVALUATION_QUALITY_LABELS = {
    1: "Poor",
    2: "Fair",
    3: "Good",
    4: "Excellent",
}
AI_EVALUATION_HALLUCINATION_LABELS = {
    "none": "None observed",
    "identity": "Subject or identity",
    "attributes": "Attributes or anatomy",
    "action": "Action or relationship",
    "setting": "Setting, object, or text",
    "sensitivity": "Content sensitivity",
}
AI_EVALUATION_SAFETY_LABELS = {
    "appropriate": "Appropriate",
    "understated": "Missed or understated",
    "unsupported": "Unsupported sensitive claim",
    "unclear": "Not applicable or unclear",
}
AI_EVALUATION_DECISION_LABELS = {
    "keep_baseline": "Keep the current installed set",
    "narrow_follow_up": "Open a narrower configuration follow-up",
    "evaluate_adapter": "Evaluate one evidence-backed adapter candidate",
}
_AI_EVALUATION_CONTENT_RATINGS = {
    "unknown","general","sensitive","questionable","explicit",
}
_PRIVACY_SAFE_DEVICE_FIELDS = {
    "kind","provider","index","name","precision","quantization",
}


@dataclass
class AIEvaluationAdapter:
    model_id: str
    display_name: str
    kind: str
    backend: object
    required_device_kind: str = ""


def _process_rss_bytes() -> int | None:
    try:
        import psutil

        return int(psutil.Process(os.getpid()).memory_info().rss)
    except Exception:
        return None


def _gpu_process_bytes() -> int | None:
    executable=shutil.which("nvidia-smi")
    if not executable:
        return None
    flags=getattr(subprocess,"CREATE_NO_WINDOW",0)
    try:
        result=subprocess.run(
            [
                executable,
                "--query-compute-apps=pid,used_gpu_memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,text=True,check=False,timeout=5,
            creationflags=flags,
        )
    except (OSError,subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    total=0
    found=False
    for line in result.stdout.splitlines():
        parts=[part.strip() for part in line.split(",")]
        if len(parts)!=2:
            continue
        try:
            pid=int(parts[0])
            used_mib=float(parts[1])
        except ValueError:
            continue
        if pid==os.getpid() and used_mib>=0:
            total+=int(round(used_mib*1024*1024))
            found=True
    return total if found else 0


def local_ai_resource_snapshot() -> dict[str,int | None]:
    """Return machine-local process resources without exposing paths or content."""
    return {
        "process_rss_bytes":_process_rss_bytes(),
        "gpu_process_bytes":_gpu_process_bytes(),
    }


class PeakResourceSampler:
    """Poll peak process resources while one local adapter is active."""

    def __init__(
        self,probe: Callable[[],dict[str,int | None]] | None=None,*,
        interval_seconds: float=.5,
    ) -> None:
        self.probe=probe or local_ai_resource_snapshot
        self.interval_seconds=max(.05,float(interval_seconds))
        self._stop=threading.Event()
        self._thread: threading.Thread | None=None
        self._baseline: dict[str,int | None]={}
        self._peak: dict[str,int | None]={}
        self._samples=0

    def _sample(self) -> None:
        try:
            snapshot=dict(self.probe() or {})
        except Exception:
            snapshot={}
        self._samples+=1
        for key in ("process_rss_bytes","gpu_process_bytes"):
            value=snapshot.get(key)
            if value is None:
                self._peak.setdefault(key,None)
                continue
            numeric=max(0,int(value))
            current=self._peak.get(key)
            self._peak[key]=numeric if current is None else max(int(current),numeric)

    def start(self) -> None:
        if self._thread is not None:
            return
        try:
            self._baseline=dict(self.probe() or {})
        except Exception:
            self._baseline={}
        self._peak={
            key:(None if self._baseline.get(key) is None else max(0,int(self._baseline[key])))
            for key in ("process_rss_bytes","gpu_process_bytes")
        }
        self._samples=1
        self._stop.clear()

        def poll() -> None:
            while not self._stop.wait(self.interval_seconds):
                self._sample()

        self._thread=threading.Thread(
            target=poll,name="collectigen-ai-resource-sampler",daemon=True,
        )
        self._thread.start()

    def stop(self) -> dict[str,object]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0,self.interval_seconds*3))
        self._thread=None
        self._sample()
        peak_delta={}
        for key in ("process_rss_bytes","gpu_process_bytes"):
            baseline=self._baseline.get(key)
            peak=self._peak.get(key)
            peak_delta[key]=(
                None if baseline is None or peak is None
                else max(0,int(peak)-max(0,int(baseline)))
            )
        return {
            "baseline":{
                key:self._baseline.get(key)
                for key in ("process_rss_bytes","gpu_process_bytes")
            },
            "peak":dict(self._peak),
            "peak_delta":peak_delta,
            "sample_count":self._samples,
        }


def _configuration_fingerprint(result) -> str:
    prompt=str(getattr(result,"prompt","") or "")
    payload={
        "model_id":str(getattr(result,"model_id","") or ""),
        "model_revision":str(getattr(result,"model_revision","") or ""),
        "kind":str(getattr(result,"kind","") or ""),
        "settings":getattr(result,"settings",{}) or {},
        "prompt_sha256":hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    }
    return hashlib.sha256(
        json.dumps(
            payload,sort_keys=True,separators=(",",":"),ensure_ascii=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _device_record(backend,first_result) -> dict[str,object]:
    device={}
    probe=getattr(backend,"_benchmark_device",None)
    if callable(probe):
        try:
            value=probe()
            if isinstance(value,dict):
                device=dict(value)
        except Exception:
            device={}
    if not device:
        settings=getattr(first_result,"settings",{}) or {}
        provider=str(settings.get("execution_provider") or "")
        if provider:
            device={
                "kind":"cuda" if "CUDA" in provider.upper() else "cpu",
                "provider":provider,
            }
    return device


class AIBaselineEvaluator:
    """Run a bounded, no-write evaluation and retain raw results only in memory."""

    def __init__(
        self,adapters: Iterable[AIEvaluationAdapter],*,
        resource_probe: Callable[[],dict[str,int | None]] | None=None,
        sampler_interval_seconds: float=.5,
    ) -> None:
        self.adapters=tuple(adapters)
        self.resource_probe=resource_probe
        self.sampler_interval_seconds=sampler_interval_seconds
        identities=[adapter.model_id for adapter in self.adapters]
        if not identities or len(set(identities))!=len(identities):
            raise ValueError("Choose one or more distinct installed model identities.")

    @staticmethod
    def _paths(images: Iterable[Path]) -> tuple[Path,...]:
        unique=[]
        for raw in images:
            path=Path(raw)
            if path not in unique:
                unique.append(path)
        paths=tuple(unique[:AI_BASELINE_EVALUATION_SAMPLE_LIMIT])
        if not paths:
            raise ValueError("Choose at least one representative image.")
        return paths

    @staticmethod
    def _analyze(backend,path: Path,should_cancel) -> object:
        cancellable=getattr(backend,"analyze_with_cancellation",None)
        if callable(cancellable):
            return cancellable(path,should_cancel)
        if should_cancel and should_cancel():
            raise InterruptedError("Baseline evaluation cancelled safely")
        return backend.analyze(path)

    def run(
        self,images: Iterable[Path],*,
        should_cancel: Callable[[],bool] | None=None,
        progress: Callable[[int,int,str],None] | None=None,
    ) -> dict[str,object]:
        paths=self._paths(images)
        cancel=should_cancel or (lambda:False)
        report=progress or (lambda *_args:None)
        total=len(paths)*len(self.adapters)
        completed=0
        models=[]
        for model_index,adapter in enumerate(self.adapters,1):
            if cancel():
                raise InterruptedError("Baseline evaluation cancelled safely")
            sampler=PeakResourceSampler(
                self.resource_probe,interval_seconds=self.sampler_interval_seconds,
            )
            samples=[]
            model_revision=""
            configuration_fingerprint=""
            device={}
            sampler.start()
            try:
                for sample_index,path in enumerate(paths,1):
                    if cancel():
                        raise InterruptedError("Baseline evaluation cancelled safely")
                    report(
                        completed,total,
                        f"Running {adapter.display_name} • image {sample_index:,} "
                        f"of {len(paths):,}",
                    )
                    started=time.perf_counter()
                    result=self._analyze(adapter.backend,path,cancel)
                    elapsed=max(time.perf_counter()-started,1e-9)
                    if not configuration_fingerprint:
                        model_revision=str(
                            getattr(result,"model_revision","") or ""
                        )
                        configuration_fingerprint=_configuration_fingerprint(
                            result
                        )
                        device=_device_record(adapter.backend,result)
                        required=str(adapter.required_device_kind or "")
                        actual=str(device.get("kind") or "unknown")
                        if required and actual!=required:
                            raise RuntimeError(
                                f"{adapter.display_name} did not run on the "
                                f"required {required.upper()} device (actual: "
                                f"{actual.upper()}). Repair the reviewed local "
                                "AI runtime and run the installed-model evaluation again; this "
                                "result was not kept."
                            )
                    kind=str(getattr(result,"kind",adapter.kind) or adapter.kind)
                    if kind!=adapter.kind:
                        raise RuntimeError(
                            f"{adapter.display_name} returned {kind!r} instead of "
                            f"the expected {adapter.kind!r} output."
                        )
                    if kind=="sdxl_tags":
                        output={
                            "tags":[
                                {
                                    "tag":str(getattr(item,"tag","") or ""),
                                    "confidence":float(getattr(item,"confidence",0) or 0),
                                    "category":str(getattr(item,"category","") or "general"),
                                }
                                for item in (getattr(result,"tags",()) or ())
                                if str(getattr(item,"tag","") or "")
                            ]
                        }
                    else:
                        output={
                            "plain_language":str(
                                getattr(result,"plain_language","") or ""
                            )
                        }
                    samples.append({
                        "sample_number":sample_index,
                        "seconds":elapsed,
                        "output":output,
                    })
                    completed+=1
                    report(
                        completed,total,
                        f"Completed {completed:,} of {total:,} model results",
                    )
                    del result
            finally:
                resources=sampler.stop()
                release=getattr(adapter.backend,"release",None)
                if callable(release):
                    release()
            if not configuration_fingerprint:
                raise RuntimeError(f"{adapter.display_name} produced no evaluation result.")
            models.append({
                "model_id":adapter.model_id,
                "display_name":adapter.display_name,
                "kind":adapter.kind,
                "model_revision":model_revision,
                "configuration_fingerprint":configuration_fingerprint,
                "device":device,
                "resources":resources,
                "samples":samples,
            })
        return {
            "schema_version":AI_BASELINE_EVALUATION_SCHEMA_VERSION,
            "measured_at":datetime.now(timezone.utc).isoformat(),
            "sample_count":len(paths),
            "model_count":len(models),
            "offline_only":True,
            "writes_performed":False,
            "models":models,
        }


def _counts(values) -> dict[str,int]:
    return dict(sorted(Counter(str(value) for value in values).items()))


def _memory_mib(value) -> float | None:
    return None if value is None else round(max(0,int(value))/(1024*1024),1)


def _privacy_safe_device(device) -> dict[str,object]:
    if not isinstance(device,dict):
        return {}
    return {
        key:value for key,value in device.items()
        if key in _PRIVACY_SAFE_DEVICE_FIELDS
        and isinstance(value,(str,int,float,bool))
    }


def privacy_safe_ai_evaluation_summary(
    session: dict, reviews: dict, sample_ratings: dict,
    decision: str,
) -> dict[str,object]:
    """Reduce an in-memory session to aggregate evidence with no paths or outputs."""
    if session.get("schema_version")!=AI_BASELINE_EVALUATION_SCHEMA_VERSION:
        raise ValueError("The installed AI evaluation session uses an unsupported format.")
    if decision not in AI_EVALUATION_DECISION_LABELS:
        raise ValueError("Choose one installed AI evaluation decision.")
    sample_count=max(0,int(session.get("sample_count",0) or 0))
    models=session.get("models",())
    if not isinstance(models,(list,tuple)) or not models:
        raise ValueError("The installed AI evaluation has no model results.")
    model_summaries=[]
    for model in models:
        model_id=str(model.get("model_id") or "")
        samples=model.get("samples",())
        model_reviews=[]
        for sample in samples:
            sample_number=max(1,int(sample.get("sample_number",0) or 0))
            review=reviews.get((model_id,sample_number))
            if not isinstance(review,dict):
                raise ValueError("Every model result needs a completed review.")
            quality=int(review.get("quality",0) or 0)
            safety=str(review.get("safety") or "")
            hallucinations=tuple(sorted(set(
                str(value) for value in review.get("hallucinations",())
            )))
            if quality not in AI_EVALUATION_QUALITY_LABELS:
                raise ValueError("Every model result needs a quality rating.")
            if safety not in AI_EVALUATION_SAFETY_LABELS:
                raise ValueError("Every model result needs a safety assessment.")
            if (
                not hallucinations
                or any(value not in AI_EVALUATION_HALLUCINATION_LABELS for value in hallucinations)
                or ("none" in hallucinations and len(hallucinations)>1)
            ):
                raise ValueError("Every model result needs a valid hallucination assessment.")
            model_reviews.append((quality,safety,hallucinations))
        timings=[max(0,float(sample.get("seconds",0) or 0)) for sample in samples]
        warm=timings[1:] or timings
        hallucination_values=[
            value for _quality,_safety,values in model_reviews
            for value in values if value!="none"
        ]
        resources=model.get("resources",{}) if isinstance(model.get("resources"),dict) else {}
        peak=resources.get("peak",{}) if isinstance(resources.get("peak"),dict) else {}
        model_summaries.append({
            "model_id":model_id,
            "display_name":str(model.get("display_name") or model_id),
            "kind":str(model.get("kind") or ""),
            "model_revision":str(model.get("model_revision") or ""),
            "configuration_fingerprint":str(
                model.get("configuration_fingerprint") or ""
            ),
            "device":_privacy_safe_device(model.get("device")),
            "sample_count":len(samples),
            "quality_mean":round(mean(value[0] for value in model_reviews),2),
            "quality_distribution":_counts(
                AI_EVALUATION_QUALITY_LABELS[value[0]] for value in model_reviews
            ),
            "material_hallucination_results":sum(
                bool(values and values!=("none",))
                for _quality,_safety,values in model_reviews
            ),
            "hallucination_categories":_counts(hallucination_values),
            "safety_distribution":_counts(value[1] for value in model_reviews),
            "cold_first_seconds":round(timings[0],3) if timings else None,
            "warm_median_seconds":round(median(warm),3) if warm else None,
            "warm_range_seconds":[
                round(min(warm),3),round(max(warm),3)
            ] if warm else [],
            "peak_process_rss_mib":_memory_mib(peak.get("process_rss_bytes")),
            "peak_gpu_process_mib":_memory_mib(peak.get("gpu_process_bytes")),
        })
    expected_ratings=[
        str(sample_ratings.get(index,"unknown") or "unknown")
        for index in range(1,sample_count+1)
    ]
    if any(value not in _AI_EVALUATION_CONTENT_RATINGS for value in expected_ratings):
        raise ValueError("Every sample needs a valid expected content rating.")
    return {
        "schema_version":AI_BASELINE_EVALUATION_SCHEMA_VERSION,
        "evaluation":"AI-01 installed local-model evaluation",
        "measured_at":str(session.get("measured_at") or ""),
        "offline_only":bool(session.get("offline_only")),
        "writes_performed":bool(session.get("writes_performed")),
        "privacy":{
            "sample_paths_saved":False,
            "pixels_saved":False,
            "raw_outputs_saved":False,
            "sample_identifiers":"session-only ordinal numbers",
        },
        "sample_count":sample_count,
        "sample_content_rating_distribution":_counts(expected_ratings),
        "models":model_summaries,
        "decision":decision,
        "decision_label":AI_EVALUATION_DECISION_LABELS[decision],
    }
