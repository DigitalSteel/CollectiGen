from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image,ImageDraw

from .ai_interrogation import (
    JOYCAPTION_MODEL,WD_TAGGER_MODELS,WDEva02Backend,
    configured_plain_language_backend,recommend_wd_pipeline_settings,
)
from .dependencies import DependencyService


@dataclass(frozen=True)
class QuickAiBenchmarkResult:
    wd_results: dict[str,dict]
    joy_result: dict | None
    wd_execution_provider: str
    wd_batch_size: int


def _synthetic_benchmark_images(root: Path) -> tuple[Path,...]:
    """Create disposable, non-user benchmark inputs with varied simple structure."""
    root.mkdir(parents=True,exist_ok=True)
    first=Image.new("RGB",(512,512),(32,48,72))
    draw=ImageDraw.Draw(first)
    for offset in range(0,512,32):
        top=offset//2
        draw.rectangle(
            (offset,top,min(511,offset+112),min(511,top+128)),
            outline=(80+offset//4,170,220),width=5,
        )
    second=Image.new("RGB",(640,448),(230,220,196))
    draw=ImageDraw.Draw(second)
    draw.ellipse((52,44,310,302),fill=(88,132,176),outline=(22,42,72),width=8)
    draw.polygon(((350,360),(470,62),(592,360)),fill=(196,92,76))
    paths=(root/"benchmark-one.png",root/"benchmark-two.png")
    first.save(paths[0]); second.save(paths[1])
    return paths


def run_quick_ai_benchmark(
    settings,*,progress: Callable[[int,int,str],None] | None=None,
    should_cancel: Callable[[],bool] | None=None,
) -> QuickAiBenchmarkResult:
    """Measure installed recommended AI with disposable images and no saved pixels."""
    report=progress or (lambda *_args:None)
    cancelled=should_cancel or (lambda:False)
    service=DependencyService(settings)
    model_ids=tuple(
        value for value in settings.enabled_wd_taggers
        if value in WD_TAGGER_MODELS
        and service.component(service.wd_component_name(value)).installed
    )
    joy_ready=(
        str(getattr(settings,"plain_language_model",JOYCAPTION_MODEL))==JOYCAPTION_MODEL
        and service.capability_ready("joy")
    )
    total=len(model_ids)+(1 if joy_ready else 0)
    if not total:
        raise RuntimeError("No installed recommended AI capability is ready to benchmark.")
    completed=0; wd_results={}; joy_result=None
    with tempfile.TemporaryDirectory(prefix="collectigen-benchmark-") as folder:
        samples=_synthetic_benchmark_images(Path(folder))
        thresholds=(
            settings.wd_general_threshold,settings.wd_character_threshold,
            settings.wd_rating_threshold,
        )
        provider=str(getattr(settings,"wd_execution_provider","cpu"))
        provider=provider if provider in {"cuda","cpu"} else "cpu"
        planned_batch=max(1,min(16,int(getattr(settings,"wd_batch_size",1))))
        batch_sizes=tuple(sorted({1,planned_batch}))
        for model_id in model_ids:
            if cancelled():
                raise InterruptedError("Quick benchmark cancelled safely.")
            display=WD_TAGGER_MODELS[model_id][0]
            report(completed,total,f"Benchmarking {display} on {provider.upper()}…")
            backend=WDEva02Backend(
                service.wd_model_dir_for(model_id),*thresholds,model_id=model_id,
            )
            wd_results[model_id]=backend.benchmark(
                samples,batch_sizes=batch_sizes,repeats=1,
                should_cancel=cancelled,providers=(provider,),
            )
            completed+=1; report(completed,total,f"Finished {display}")
        recommendation=(
            recommend_wd_pipeline_settings(wd_results) if wd_results else {
                "provider":provider,"batch_size":planned_batch,
            }
        )
        if joy_ready:
            if cancelled():
                raise InterruptedError("Quick benchmark cancelled safely.")
            report(completed,total,"Benchmarking JoyCaption with the selected memory profile…")
            joy_result=configured_plain_language_backend(settings).benchmark(
                samples[:1],repeats=1,should_cancel=cancelled,
                progress=lambda current,inner_total,label:report(
                    completed,total,f"JoyCaption • {label}"
                ),
            )
            completed+=1; report(completed,total,"Finished JoyCaption")
    return QuickAiBenchmarkResult(
        wd_results=wd_results,joy_result=joy_result,
        wd_execution_provider=str(recommendation["provider"]),
        wd_batch_size=int(recommendation["batch_size"]),
    )
