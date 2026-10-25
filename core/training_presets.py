"""Pure, non-authoritative previews of reviewed Training configuration choices.

These previews never change a package or admit a native trainer. Native
preparation still verifies the exact package, model, runtime and resources.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from math import ceil
from typing import Mapping

from .training_configuration import TrainingConfiguration
from .training_tokenizer_contract import LIMITS


@dataclass(frozen=True)
class TrainingWorkload:
    training_images: int | None
    exposures_per_epoch: int | None
    total_exposures: int | None
    effective_batch: int
    optimizer_steps: int | None
    step_basis: str

    @property
    def summary(self) -> str:
        if self.training_images is None:
            return "Training image count is unavailable; workload steps need the reviewed training roles."
        count = (f"{self.training_images:,} training images · {self.exposures_per_epoch:,} exposures per epoch · "
                 f"{self.total_exposures:,} total exposures · effective batch {self.effective_batch:,}")
        return count + (f" · {self.optimizer_steps:,} optimizer steps" if self.optimizer_steps is not None
                        else " · optimizer steps require exact trainer batching")


@dataclass(frozen=True)
class TrainingPresetChange:
    field: str
    before: object
    after: object
    reason: str


@dataclass(frozen=True)
class TrainingPresetPreview:
    preset_id: str
    configuration: TrainingConfiguration
    changes: tuple[TrainingPresetChange, ...]
    native_status: str
    native_reasons: tuple[str, ...]
    workload: TrainingWorkload
    cautions: tuple[str, ...]


def training_workload(
    configuration: TrainingConfiguration, training_image_count: int | None, *,
    bucket_counts: Mapping[tuple[int, int], int] | None = None,
) -> TrainingWorkload:
    """Count actual optimizer steps only when the adapter's grouping is known.

    SDXL rounds each image-size bucket to batches, then flushes gradient
    accumulation at each epoch. Anima accepts batch one and also flushes at
    each epoch. A neutral/manual package has no executable step contract.
    """
    if ((training_image_count is not None and
         (type(training_image_count) is not int or not 0 <= training_image_count <= 50_000))
            or any(type(value) is not int or value < 1 for value in
                   (configuration.repeats, configuration.epochs,
                    configuration.batch_size, configuration.gradient_accumulation))):
        raise ValueError("A bounded training image count and positive settings are required.")
    if bucket_counts is not None:
        if training_image_count is None:
            raise ValueError("Exact bucket counts require a known training image count.")
        if (not isinstance(bucket_counts, Mapping) or len(bucket_counts) > 50_000
                or any(type(key) is not tuple or len(key) != 2
                       or any(type(dimension) is not int or dimension < 1 for dimension in key)
                       or type(count) is not int or count < 1
                       for key, count in bucket_counts.items())
                or sum(bucket_counts.values()) != training_image_count):
            raise ValueError("Bucket counts must exactly cover the selected training images.")
    per_epoch = (training_image_count * configuration.repeats
                 if training_image_count is not None else None)
    family = configuration.target_model_family.casefold()
    steps = None
    basis = "Unavailable until a trainer and exact dataset batching are reviewed."
    if family == "anima" and configuration.batch_size == 1 and per_epoch is not None:
        steps = configuration.epochs * ceil(per_epoch / configuration.gradient_accumulation)
        basis = "Anima batch-one accumulation flushed at each epoch."
    elif family == "sdxl" and bucket_counts is not None:
        epoch_batches = sum(ceil(count * configuration.repeats / configuration.batch_size)
                            for count in bucket_counts.values())
        steps = configuration.epochs * ceil(epoch_batches / configuration.gradient_accumulation)
        basis = "SDXL image-size buckets rounded to batches, then accumulation flushed per epoch."
    return TrainingWorkload(training_image_count, per_epoch,
                            per_epoch * configuration.epochs if per_epoch is not None else None,
                            configuration.batch_size * configuration.gradient_accumulation,
                            steps, basis)


def _native_assessment(configuration: TrainingConfiguration,
                       workload: TrainingWorkload) -> tuple[str, tuple[str, ...]]:
    family = configuration.target_model_family.casefold()
    reasons: list[str] = []
    if family == "anima":
        if configuration.target_model_version.casefold() not in {
                "base v1.0", "base-v1.0", "base-1.0", "1.0"}:
            reasons.append("Native Anima requires the exact Base v1.0 target.")
        if configuration.output_kind != "lora":
            reasons.append("Native Anima supports a LoRA output only.")
        if configuration.target_resolution not in {256, 384, 512}:
            reasons.append("Native Anima accepts only 256, 384 or 512 px.")
        if configuration.batch_size != 1:
            reasons.append("Native Anima accepts batch size one only.")
        if not 1 <= configuration.gradient_accumulation <= 8:
            reasons.append("Native Anima accumulation must be 1 through 8.")
        if not 1 <= configuration.repeats <= 16:
            reasons.append("Native Anima repeats must be 1 through 16.")
        if not 0 < configuration.learning_rate <= 1e-4:
            reasons.append("Native Anima learning rate must be above zero and at most 1e-4.")
        if workload.training_images is not None and (
                workload.optimizer_steps is None or not 1 <= workload.optimizer_steps <= 2_000):
            reasons.append("Native Anima requires 1 through 2,000 optimizer steps from this training scope.")
    elif family == "sdxl":
        if configuration.target_model_version.casefold() not in {"", "1.0", "base-1.0"}:
            reasons.append("Native SDXL accepts the 1.0 base target only.")
        if configuration.output_kind != "lora":
            reasons.append("Native SDXL supports a LoRA output only.")
        if configuration.max_caption_tokens not in LIMITS:
            reasons.append("Native SDXL needs an admitted tokenizer caption limit.")
        if configuration.precision not in {"bf16", "fp16"}:
            reasons.append("Native SDXL requires BF16 or FP16 mixed precision.")
        if configuration.target_resolution != 1024:
            reasons.append("Native SDXL preparation requires the 1024 px target; lower resolution is an external/manual choice.")
        if configuration.bucket_policy != "aspect_ratio" or configuration.allow_upscale or configuration.crop_policy != "none":
            reasons.append("Native SDXL preparation requires aspect-ratio buckets, no upscaling and no cropping.")
        if not 1 <= configuration.batch_size <= 4:
            reasons.append("Native SDXL preparation accepts batch sizes one through four.")
        if workload.training_images is not None and not 1 <= workload.exposures_per_epoch <= 50_000:
            reasons.append("Native SDXL preparation requires 1 through 50,000 image exposures per epoch.")
    else:
        return "external_manual_only", (
            "No qualified native adapter is identified for this model family; keep the manual handoff explicit.",)
    return ("native_incompatible" if reasons else "native_candidate", tuple(reasons))


def preview_training_preset(
    configuration: TrainingConfiguration, *, preset_id: str,
    training_image_count: int | None, bucket_counts: Mapping[tuple[int, int], int] | None = None,
    goal_kind: str = "concept",
) -> TrainingPresetPreview:
    """Preview a deliberate change; callers must explicitly apply the candidate.

    `external_manual` preserves the existing expert settings and avoids any
    native compatibility claim. A native candidate is still subject to all
    package, runtime, model, GPU and per-image admission checks.
    """
    if preset_id not in {"current", "native_default", "lower_memory", "external_manual"}:
        raise ValueError("Choose a supported Training preset preview.")
    candidate = configuration
    changes: list[TrainingPresetChange] = []
    if preset_id in {"native_default", "lower_memory"}:
        family = configuration.target_model_family.casefold()
        if preset_id == "lower_memory":
            proposals = {
                "batch_size": (1, "Batch one reduces simultaneous image load; it may take longer."),
            }
            if family == "anima":
                proposals["target_resolution"] = (
                    min(configuration.target_resolution, 256),
                    "256 px is the lower admitted Anima resolution; detail and outcome may change.")
                proposals["gradient_accumulation"] = (
                    min(configuration.gradient_accumulation, 8),
                    "Keep accumulation inside the qualified Anima range; it does not reduce batch memory.")
        elif family == "anima":
            native_accumulation = (min(8, max(1, ceil(training_image_count / 2_000)))
                                   if training_image_count else 1)
            proposals = {
                "output_kind": ("lora", "The admitted Anima path produces a DiT-only LoRA."),
                "target_resolution": (256, "256 px is the conservative admitted Anima starting resolution."),
                "batch_size": (1, "The admitted Anima trainer is batch-one only."),
                "gradient_accumulation": (native_accumulation,
                    "Use the planned training-role count to stay inside the native 2,000-step limit; accumulation does not reduce batch memory."),
                "learning_rate": (2e-5, "2e-5 is the native starting rate, not a quality guarantee."),
                "repeats": (1, "Start with one exposure per image per epoch."),
            }
            if training_image_count is not None and 0 < training_image_count <= 2_000:
                maximum_epochs = 2_000 // ceil(training_image_count / native_accumulation)
                if configuration.epochs > maximum_epochs:
                    proposals["epochs"] = (maximum_epochs,
                        "Cap this selected scope at the native 2,000-step boundary; fewer epochs are not a quality recommendation.")
            elif training_image_count is not None and 2_000 < training_image_count <= 16_000:
                maximum_epochs = 2_000 // ceil(training_image_count / native_accumulation)
                if configuration.epochs > maximum_epochs:
                    proposals["epochs"] = (maximum_epochs,
                        "Cap this selected scope at the native 2,000-step boundary; fewer epochs are not a quality recommendation.")
        elif family == "sdxl":
            proposals = {
                "output_kind": ("lora", "The admitted SDXL path produces a LoRA."),
                "target_resolution": (1024, "The native SDXL preparation contract requires exactly 1024 px; lower targets need an external trainer."),
                "bucket_policy": ("aspect_ratio", "The native SDXL dataset uses reviewed aspect-ratio buckets."),
                "allow_upscale": (False, "The native SDXL dataset does not admit invented upscaled pixels."),
                "crop_policy": ("none", "The native SDXL dataset preserves the reviewed framing."),
                "batch_size": (1, "Batch one limits simultaneous image load; actual fit remains unmeasured."),
                "gradient_accumulation": (4, "Start with four accumulation steps, subject to exact dataset review."),
                "precision": ("bf16", "The admitted SDXL path requires BF16 or FP16; BF16 is a starting request."),
                "max_caption_tokens": (225, "Use an admitted SDXL tokenizer caption limit."),
            }
        else:
            proposals = {}
        for field, (after, reason) in proposals.items():
            before = getattr(configuration, field)
            if after != before:
                changes.append(TrainingPresetChange(field, before, after, reason))
        if changes:
            candidate = replace(configuration, **{item.field: item.after for item in changes},
                override_reason=(configuration.override_reason + " | " if configuration.override_reason else "")
                    + f"Explicit {preset_id.replace('_', ' ')} preview applied; settings do not prove GPU fit or quality.")
    workload = training_workload(candidate, training_image_count, bucket_counts=bucket_counts)
    status, reasons = _native_assessment(candidate, workload)
    if preset_id == "external_manual":
        status = "external_manual_only"
        reasons = ("Existing expert settings are preserved for an external/manual trainer; no native compatibility is claimed.",)
    cautions = [
        "Batch size is simultaneous images; accumulation combines successive batches for one optimizer update. Epochs are passes through training examples, and exposures count how often examples are seen. More collection images mean more work, not loading all images into GPU memory at once.",
        "Learning rate and epoch defaults are starting points, not evidence of convergence or output quality.",
        "A native candidate still needs exact package, runtime, model, current free-memory and execution preflight; GPU fit is unmeasured here.",
    ]
    if training_image_count is not None and training_image_count < 10:
        cautions.append("This small selected training scope may repeat examples heavily; review goal coverage and held-out roles.")
    if goal_kind.casefold() in {"character", "outfit", "action"}:
        cautions.append("Review varied poses, views and backgrounds for this goal; workload counts do not measure concept coverage.")
    if changes:
        cautions.append("Preset changes can alter detail and learning behavior; compare a measured short trial before a full run.")
    return TrainingPresetPreview(preset_id, candidate, tuple(changes), status, reasons,
                                 workload, tuple(cautions))
