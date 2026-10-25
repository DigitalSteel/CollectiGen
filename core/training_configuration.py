from __future__ import annotations

import re
import json
import math
from dataclasses import dataclass,replace
from typing import Any,Iterable


TRAINING_CONFIGURATION_SCHEMA_VERSION=1
MANUAL_PACKAGE_ADAPTER_ID="collectigen.manual-training-package"
MANUAL_PACKAGE_ADAPTER_VERSION=1
RECOMMENDATION_PROFILE_ID="collectigen.training-baseline-v1"

OUTPUT_KINDS=("lora","full_fine_tune","embedding")
BUCKET_POLICIES=("aspect_ratio","square")
CROP_POLICIES=("none","center")
CAPTION_PREFIX_POLICIES=("profile_only","trigger","trigger_and_class")
PRECISIONS=("bf16","fp16","float32")
RECOMMENDED_SETTING_FIELDS=(
    "output_kind","target_resolution","bucket_policy","crop_policy",
    "allow_upscale","caption_prefix_policy","max_caption_tokens","repeats",
    "epochs","batch_size","gradient_accumulation","learning_rate","precision",
    "regularization_policy","validation_policy",
)


def _one_line(value: object) -> str:
    return " ".join(str(value or "").split())


def _looks_machine_local(value: str) -> bool:
    raw=str(value or "")
    if (
        re.search(r"(?<![a-zA-Z0-9])[a-zA-Z]:[\\/]",raw)
        or re.search(r"\\\\[^\\\s]+[\\/]",raw)
    ):
        return True
    without_urls=re.sub(r"https?://\S+","",raw,flags=re.IGNORECASE)
    return bool(re.search(r"(?<![a-zA-Z0-9])/(?:[^/\s]+/)+",without_urls))


def model_guidance_evidence_json(value: object) -> str:
    """Retain bounded portable guidance in the existing immutable configuration."""
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1 or value.get("workflow_kind") != "training":
        raise ValueError("Training model guidance has an unsupported evidence schema.")
    bindings = value.get("bindings")
    if not isinstance(bindings, list) or len(bindings) > 64:
        raise ValueError("Training model guidance has an invalid companion set.")
    slots = set()
    for binding in bindings:
        if not isinstance(binding, dict) or not isinstance(binding.get("slot"), str) or not binding["slot"] or binding["slot"] in slots:
            raise ValueError("Training model guidance has invalid or duplicate companion slots.")
        slots.add(binding["slot"])
        identity = binding.get("resource_id", "")
        if not isinstance(identity, str) or (identity and not re.fullmatch(r"(?:sha256|pack-sha256):[0-9a-f]{64}", identity)):
            raise ValueError("Training companions require exact portable identities.")
    def portable(item, depth=0):
        if depth > 12:
            raise ValueError("Training model guidance is too deeply nested.")
        if isinstance(item, dict):
            for key, nested in item.items():
                portable(key, depth + 1); portable(nested, depth + 1)
        elif isinstance(item, list):
            for nested in item: portable(nested, depth + 1)
        elif isinstance(item, str) and _looks_machine_local(item):
            raise ValueError("Training model guidance cannot contain machine-local paths.")
    portable(value)
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))
    if len(raw.encode("utf-8")) > 64 * 1024:
        raise ValueError("Training model guidance exceeds the supported evidence size.")
    return raw


@dataclass(frozen=True)
class TrainingSettingRecommendation:
    field: str
    value: object
    reason: str
    scope: str

    def to_dict(self) -> dict[str,object]:
        return {
            "field":self.field,"value":self.value,
            "reason":self.reason,"scope":self.scope,
        }


@dataclass(frozen=True)
class TrainingConfigurationIssue:
    code: str
    level: str
    summary: str
    reason: str
    direct_fix: str=""

    def to_dict(self) -> dict[str,str]:
        return {
            "code":self.code,"level":self.level,"summary":self.summary,
            "reason":self.reason,"direct_fix":self.direct_fix,
        }


@dataclass(frozen=True)
class TrainingResourceEstimate:
    heuristic_id: str
    effective_training_exposures: int
    optimizer_steps: int|None
    package_bytes_lower_bound: int
    package_staging_bytes: int
    time_range: str
    vram_range: str
    training_workspace_range: str
    assumptions: tuple[str,...]

    def to_dict(self) -> dict[str,object]:
        return {
            "heuristic_id":self.heuristic_id,
            "effective_training_exposures":self.effective_training_exposures,
            "optimizer_steps":self.optimizer_steps,
            "package_bytes_lower_bound":self.package_bytes_lower_bound,
            "package_staging_bytes":self.package_staging_bytes,
            "time_range":self.time_range,"vram_range":self.vram_range,
            "training_workspace_range":self.training_workspace_range,
            "assumptions":list(self.assumptions),
        }


@dataclass(frozen=True)
class TrainingConfiguration:
    """Versioned trainer-neutral choices for one manual package handoff."""

    target_model_family: str="SDXL"
    target_model_version: str=""
    base_model_id: str=""
    output_name: str="training-output"
    output_kind: str="lora"
    target_resolution: int=1024
    bucket_policy: str="aspect_ratio"
    crop_policy: str="none"
    allow_upscale: bool=False
    caption_prefix_policy: str="profile_only"
    trigger_term: str=""
    class_term: str=""
    max_caption_tokens: int=225
    repeats: int=1
    epochs: int=10
    batch_size: int=1
    gradient_accumulation: int=4
    learning_rate: float=0.0001
    precision: str="bf16"
    seed: int=42
    regularization_policy: str="none"
    validation_policy: str="assigned_roles"
    image_transform_policy: str="lossless_when_required"
    recommendation_profile_id: str=RECOMMENDATION_PROFILE_ID
    recommendation_scope: str=""
    recommendations: tuple[TrainingSettingRecommendation,...]=()
    override_reason: str=""
    adapter_id: str=MANUAL_PACKAGE_ADAPTER_ID
    adapter_version: int=MANUAL_PACKAGE_ADAPTER_VERSION
    schema_version: int=TRAINING_CONFIGURATION_SCHEMA_VERSION
    model_guidance_json: str=""

    def require_single_checkpoint_adapter(self):
        if self.model_guidance_json and json.loads(self.model_guidance_json).get("bindings"):
            raise ValueError("This trainer adapter does not support companion resource sets. Keep the manual package or select a qualified adapter.")

    def value_for(self,field: str) -> object:
        return getattr(self,field)

    def restore_saved_recommendation(self) -> "TrainingConfiguration":
        """Restore the exact saved setting values without recalculating advice."""
        if (
            type(self.schema_version) is not int
            or self.schema_version!=TRAINING_CONFIGURATION_SCHEMA_VERSION
            or self.recommendation_profile_id!=RECOMMENDATION_PROFILE_ID
            or type(self.recommendation_scope) is not str
            or not self.recommendation_scope.strip()
            or type(self.recommendations) is not tuple
            or len(self.recommendations)!=len(RECOMMENDED_SETTING_FIELDS)
        ):
            raise ValueError("The saved training recommendation snapshot is unsupported or incomplete.")

        ranges={
            "target_resolution":(256,2048),
            "max_caption_tokens":(1,1_000_000),
            "repeats":(1,100),
            "epochs":(1,10_000),
            "batch_size":(1,64),
            "gradient_accumulation":(1,128),
        }
        policies={
            "output_kind":OUTPUT_KINDS,
            "bucket_policy":BUCKET_POLICIES,
            "crop_policy":CROP_POLICIES,
            "caption_prefix_policy":CAPTION_PREFIX_POLICIES,
            "precision":PRECISIONS,
            "regularization_policy":("none",),
            "validation_policy":("assigned_roles",),
        }
        restored={}
        for field,item in zip(RECOMMENDED_SETTING_FIELDS,self.recommendations):
            if (
                type(item) is not TrainingSettingRecommendation
                or item.field!=field
                or type(item.reason) is not str
                or not item.reason.strip()
                or item.scope!=self.recommendation_scope
            ):
                raise ValueError("The saved training recommendation snapshot is incomplete or reordered.")
            value=item.value
            if field in ranges:
                minimum,maximum=ranges[field]
                valid=type(value) is int and minimum<=value<=maximum
            elif field in policies:
                valid=type(value) is str and value in policies[field]
            elif field=="allow_upscale":
                valid=type(value) is bool
            elif field=="learning_rate":
                valid=type(value) is float and math.isfinite(value) and 0<value<=0.01
            else:
                valid=False
            if not valid:
                raise ValueError(f"The saved training recommendation for {field} is invalid.")
            restored[field]=value
        return replace(self,**restored,override_reason="")

    @property
    def overridden_fields(self) -> tuple[str,...]:
        return tuple(
            item.field for item in self.recommendations
            if self.value_for(item.field)!=item.value
        )

    def to_dict(self) -> dict[str,object]:
        return {
            **({"model_guidance":json.loads(model_guidance_evidence_json(json.loads(self.model_guidance_json)))} if self.model_guidance_json else {}),
            "schema_version":self.schema_version,
            "adapter":{
                "id":self.adapter_id,"version":self.adapter_version,
                "compatibility_status":"manual_handoff_unverified",
                "named_trainer_configuration_emitted":False,
            },
            "target":{
                "model_family":self.target_model_family,
                "model_version":self.target_model_version,
                "base_model_id":self.base_model_id,
            },
            "expected_output":{
                "name":self.output_name,"kind":self.output_kind,
                "lineage_policy":"link output to base model, plan, package, settings, and source manifest fingerprints",
            },
            "image_preparation":{
                "target_resolution":self.target_resolution,
                "bucket_policy":self.bucket_policy,"crop_policy":self.crop_policy,
                "allow_upscale":self.allow_upscale,
                "transform_policy":self.image_transform_policy,
            },
            "captions":{
                "prefix_policy":self.caption_prefix_policy,
                "trigger_term":self.trigger_term,"class_term":self.class_term,
                "max_tokens":self.max_caption_tokens,
                "tokenizer_status":"unavailable_for_manual_adapter",
                "variant_policy":"canonical_only",
            },
            "weighting":{
                "repeats":self.repeats,"epochs":self.epochs,
                "regularization_policy":self.regularization_policy,
                "validation_policy":self.validation_policy,
            },
            "optimization_request":{
                "batch_size":self.batch_size,
                "gradient_accumulation":self.gradient_accumulation,
                "learning_rate":self.learning_rate,"precision":self.precision,
                "seed":self.seed,
                "translation_status":"trainer_adapter_not_selected",
            },
            "recommendation":{
                "profile_id":self.recommendation_profile_id,
                "scope":self.recommendation_scope,
                "defaults":[item.to_dict() for item in self.recommendations],
                "overridden_fields":list(self.overridden_fields),
                "override_reason":self.override_reason,
            },
        }

    @classmethod
    def from_dict(cls,value: object) -> "TrainingConfiguration":
        if not isinstance(value,dict) or value.get("schema_version")!=1:
            raise ValueError("A training configuration must use schema version 1.")
        adapter=value.get("adapter"); target=value.get("target")
        output=value.get("expected_output"); image=value.get("image_preparation")
        captions=value.get("captions"); weighting=value.get("weighting")
        optimization=value.get("optimization_request"); recommendation=value.get("recommendation")
        if not all(isinstance(item,dict) for item in (
            adapter,target,output,image,captions,weighting,optimization,recommendation,
        )):
            raise ValueError("A training configuration section is missing or invalid.")
        raw_defaults=recommendation.get("defaults")
        if not isinstance(raw_defaults,list):
            raise ValueError("Training setting recommendations are invalid.")
        recommendations=[]; recommendation_fields=[]
        for item in raw_defaults:
            if not isinstance(item,dict) or set(item)!={"field","value","reason","scope"}:
                raise ValueError("A training setting recommendation is invalid.")
            field=str(item["field"])
            reason=str(item["reason"]); scope=str(item["scope"])
            if field not in RECOMMENDED_SETTING_FIELDS or field in recommendation_fields:
                raise ValueError("A training setting recommendation field is invalid or duplicated.")
            if not reason.strip() or not scope.strip():
                raise ValueError("A training setting recommendation has no reason or scope.")
            recommendation_fields.append(field)
            recommendations.append(TrainingSettingRecommendation(
                field,item["value"],reason,scope,
            ))
        if tuple(recommendation_fields)!=RECOMMENDED_SETTING_FIELDS:
            raise ValueError("The scoped training recommendation set is incomplete or reordered.")
        config=cls(
            target_model_family=str(target.get("model_family") or ""),
            target_model_version=str(target.get("model_version") or ""),
            base_model_id=str(target.get("base_model_id") or ""),
            output_name=str(output.get("name") or ""),
            output_kind=str(output.get("kind") or ""),
            target_resolution=int(image.get("target_resolution") or 0),
            bucket_policy=str(image.get("bucket_policy") or ""),
            crop_policy=str(image.get("crop_policy") or ""),
            allow_upscale=image.get("allow_upscale"),
            caption_prefix_policy=str(captions.get("prefix_policy") or ""),
            trigger_term=str(captions.get("trigger_term") or ""),
            class_term=str(captions.get("class_term") or ""),
            max_caption_tokens=int(captions.get("max_tokens") or 0),
            repeats=int(weighting.get("repeats") or 0),
            epochs=int(weighting.get("epochs") or 0),
            regularization_policy=str(weighting.get("regularization_policy") or ""),
            validation_policy=str(weighting.get("validation_policy") or ""),
            batch_size=int(optimization.get("batch_size") or 0),
            gradient_accumulation=int(optimization.get("gradient_accumulation") or 0),
            learning_rate=float(optimization.get("learning_rate") or 0),
            precision=str(optimization.get("precision") or ""),
            seed=int(optimization.get("seed") if optimization.get("seed") is not None else -1),
            image_transform_policy=str(image.get("transform_policy") or ""),
            recommendation_profile_id=str(recommendation.get("profile_id") or ""),
            recommendation_scope=str(recommendation.get("scope") or ""),
            recommendations=tuple(recommendations),
            override_reason=str(recommendation.get("override_reason") or ""),
            adapter_id=str(adapter.get("id") or ""),
            adapter_version=int(adapter.get("version") or 0),
            model_guidance_json=(model_guidance_evidence_json(value["model_guidance"]) if "model_guidance" in value else ""),
        )
        issues=validate_training_configuration(config)
        if any(issue.level=="blocking" for issue in issues):
            raise ValueError(issues[0].summary)
        if config.to_dict()!=value:
            raise ValueError("A training configuration contains unsupported or inconsistent fields.")
        return config


def recommended_training_configuration(
    *,target_model_family: str,target_model_version: str="",base_model_id: str="",
    goal_kind: str="concept",included_count: int=0,profile_token_budget: int|None=None,
) -> TrainingConfiguration:
    family=_one_line(target_model_family) or "SDXL"
    version=_one_line(target_model_version)
    base=_one_line(base_model_id)
    if type(included_count) is not int or not 0 <= included_count <= 50_000:
        raise ValueError("Use a bounded planned-image count for Training recommendations.")
    native_anima = family.casefold() == "anima"
    resolution=256 if native_anima else 1024 if family.casefold() == "sdxl" else 768
    accumulation=1 if native_anima else 4
    epochs=10
    if native_anima and included_count:
        # Included images of all roles conservatively bound later training roles.
        accumulation=min(8,max(1,math.ceil(included_count/2_000)))
        per_epoch=math.ceil(included_count/accumulation)
        epochs=min(10,max(1,2_000//per_epoch))
    learning_rate=2e-5 if native_anima else 0.0001
    max_tokens=max(1,min(1_000_000,int(profile_token_budget or 225)))
    scope=(
        f"{RECOMMENDATION_PROFILE_ID}; {family} target; {goal_kind or 'concept'} goal; "
        f"{included_count:,} currently included image(s); manual handoff without a validated trainer adapter"
    )
    defaults=(
        ("output_kind","lora","A LoRA is the least storage-intensive neutral starting intent; a trainer adapter must still confirm support."),
        ("target_resolution",resolution,f"{resolution}px is a starting planning resolution for the declared {family} family; source images remain uncropped in the package and native image limits need exact review."),
        ("bucket_policy","aspect_ratio","Aspect-ratio buckets reduce unnecessary square cropping when the eventual trainer supports them."),
        ("crop_policy","none","The manual package preserves source framing; an eventual trainer preview must own any crop."),
        ("allow_upscale",False,"Automatic upscaling can invent detail and remains off until deliberately enabled downstream."),
        ("caption_prefix_policy","profile_only","The reviewed projection remains unchanged unless the user deliberately supplies stable trigger/class terms."),
        ("max_caption_tokens",max_tokens,"The selected profile budget is carried as a limit request; no target tokenizer is bundled with this manual adapter."),
        ("repeats",1,"One exposure per image per epoch avoids silently reweighting this exact selected scope."),
        ("epochs",epochs,(f"Up to {included_count:,} planned images, including held-out roles, bound Anima to {epochs:,} epoch(s) under 2,000 steps when this scope permits; exact training roles are checked later. This is not a quality recommendation."
                          if native_anima and included_count else
                          "Ten epochs provides a visible baseline workload without claiming universal convergence.")),
        ("batch_size",1,"Batch size one limits simultaneous image load; actual GPU fit still needs native preflight."),
        ("gradient_accumulation",accumulation,(f"{accumulation} accumulated batch(es) uses the planned-image upper bound for Anima's 2,000-step limit; exact training roles are checked later. This does not reduce batch memory."
                                               if native_anima and included_count else
                                               f"{accumulation} accumulated batch(es) is a starting request, not a memory or quality measurement.")),
        ("learning_rate",learning_rate,f"{learning_rate:g} is a LoRA starting request, not evidence of convergence or quality."),
        ("precision","bf16","BF16 is requested for modern compatible hardware; unsupported hardware must select another value before execution."),
        ("regularization_policy","none","This baseline does not invent class images or regularization evidence."),
        ("validation_policy","assigned_roles","Use the deterministic roles already reviewed in this plan."),
    )
    recommendations=tuple(
        TrainingSettingRecommendation(field,value,reason,scope)
        for field,value,reason in defaults
    )
    return TrainingConfiguration(
        target_model_family=family,target_model_version=version,
        base_model_id=base,target_resolution=resolution,
        max_caption_tokens=max_tokens,epochs=epochs,gradient_accumulation=accumulation,
        learning_rate=learning_rate,recommendation_scope=scope,
        recommendations=recommendations,
    )


def recontextualize_training_configuration(
    configuration: TrainingConfiguration,*,included_count: int,goal_kind: str,
) -> TrainingConfiguration:
    scope=(
        f"{configuration.recommendation_profile_id}; "
        f"{configuration.target_model_family or 'unspecified'} target; "
        f"{goal_kind or 'concept'} goal; {included_count:,} currently included image(s); "
        "manual handoff without a validated trainer adapter"
    )
    recommendations=tuple(replace(item,scope=scope) for item in configuration.recommendations)
    return replace(configuration,recommendation_scope=scope,recommendations=recommendations)


def validate_training_configuration(
    configuration: TrainingConfiguration,*,profile_family: str="",
    profile_version: str="",require_base_model: bool=False,
) -> tuple[TrainingConfigurationIssue,...]:
    issues=[]
    if configuration.schema_version!=1:
        issues.append(TrainingConfigurationIssue(
            "configuration_schema","blocking","The training settings version is unsupported.",
            "This CollectiGen build can read only training configuration schema version 1.",
            "Rebuild the plan with current settings.",
        ))
    if (
        configuration.recommendation_profile_id!=RECOMMENDATION_PROFILE_ID
        or not configuration.recommendation_scope.strip()
        or tuple(item.field for item in configuration.recommendations)
        !=RECOMMENDED_SETTING_FIELDS
        or any(
            item.scope!=configuration.recommendation_scope
            or not item.reason.strip()
            for item in configuration.recommendations
        )
    ):
        issues.append(TrainingConfigurationIssue(
            "recommendation_snapshot","blocking","The scoped recommendation snapshot is invalid.",
            "Every baseline default must retain the versioned profile, exact scope, value, and reason in stable order.",
            "Rebuild the plan with current CollectiGen recommendations.",
        ))
    if (
        configuration.adapter_id!=MANUAL_PACKAGE_ADAPTER_ID
        or configuration.adapter_version!=MANUAL_PACKAGE_ADAPTER_VERSION
    ):
        issues.append(TrainingConfigurationIssue(
            "package_adapter","blocking","The package adapter is unsupported.",
            "TRN-01 currently emits only the versioned manual training package; named trainer adapters belong to TRN-03.",
            "Choose the CollectiGen manual package adapter.",
        ))
    family=_one_line(configuration.target_model_family)
    if not family:
        issues.append(TrainingConfigurationIssue(
            "target_family_required","blocking","Enter a target model family.",
            "Resolution, captions, and expected output cannot be interpreted without the intended family.",
            "Enter the family expected by the future base model and trainer.",
        ))
    if profile_family and family.casefold()!=_one_line(profile_family).casefold():
        issues.append(TrainingConfigurationIssue(
            "profile_family_mismatch","blocking","The caption profile and target model families differ.",
            f"The profile targets {profile_family!r}, while these settings declare {family!r}.",
            "Choose a matching profile or target family, then rebuild the exact captions.",
        ))
    if profile_version and configuration.target_model_version and (
        _one_line(profile_version).casefold()!=_one_line(configuration.target_model_version).casefold()
    ):
        issues.append(TrainingConfigurationIssue(
            "profile_version_mismatch","blocking","The caption profile and target versions differ.",
            f"The profile targets {profile_version!r}, while these settings declare {configuration.target_model_version!r}.",
            "Choose one compatible target version before packaging.",
        ))
    if not _one_line(configuration.base_model_id):
        issues.append(TrainingConfigurationIssue(
            "base_model_required","blocking" if require_base_model else "caution",
            "The base model identity is not recorded.",
            "The manual handoff cannot verify compatibility or preserve output lineage without a stable declared identifier.",
            "Enter a registry ID, model hash, or other portable base-model identifier; do not enter a machine-local path.",
        ))
    elif _looks_machine_local(configuration.base_model_id):
        issues.append(TrainingConfigurationIssue(
            "base_model_not_portable","blocking","The base model identity is machine-local.",
            "Source-neutral manifests cannot contain a drive, UNC, or absolute filesystem path.",
            "Use a model registry ID or exact model hash instead.",
        ))
    if (
        not _one_line(configuration.output_name)
        or len(configuration.output_name)>100
        or re.search(r"[\\/:*?\"<>|\x00-\x1f]",configuration.output_name)
    ):
        issues.append(TrainingConfigurationIssue(
            "output_name","blocking","The expected output name is unsafe.",
            "It must be a portable name, not a path, and must be 100 characters or fewer.",
            "Enter a short filename-safe model name.",
        ))
    ranges=(
        ("target_resolution",configuration.target_resolution,256,2048),
        ("max_caption_tokens",configuration.max_caption_tokens,1,1_000_000),
        ("repeats",configuration.repeats,1,100),
        ("epochs",configuration.epochs,1,10_000),
        ("batch_size",configuration.batch_size,1,64),
        ("gradient_accumulation",configuration.gradient_accumulation,1,128),
        ("seed",configuration.seed,0,4_294_967_295),
    )
    for field,value,minimum,maximum in ranges:
        if type(value) is not int or not minimum<=value<=maximum:
            issues.append(TrainingConfigurationIssue(
                f"{field}_range","blocking",f"{field.replace('_',' ').title()} is outside its safe planning range.",
                f"The supported neutral range is {minimum:,} through {maximum:,}.",
                "Choose a value inside the displayed range.",
            ))
    if type(configuration.allow_upscale) is not bool:
        issues.append(TrainingConfigurationIssue(
            "allow_upscale_type","blocking","The upscale choice is invalid.",
            "Upscaling must be deliberately on or off.","Choose On or Off.",
        ))
    if configuration.output_kind not in OUTPUT_KINDS:
        issues.append(TrainingConfigurationIssue("output_kind","blocking","The expected output kind is unsupported.","Choose a portable output intent.","Choose LoRA, full fine-tune, or embedding."))
    if configuration.bucket_policy not in BUCKET_POLICIES:
        issues.append(TrainingConfigurationIssue("bucket_policy","blocking","The bucket policy is unsupported.","Only the named neutral policies are reproducible.","Choose aspect-ratio or square buckets."))
    if configuration.crop_policy not in CROP_POLICIES:
        issues.append(TrainingConfigurationIssue("crop_policy","blocking","The crop policy is unsupported.","Only no crop or centered crop is represented by this adapter.","Choose a supported crop policy."))
    if configuration.caption_prefix_policy not in CAPTION_PREFIX_POLICIES:
        issues.append(TrainingConfigurationIssue("caption_prefix_policy","blocking","The caption prefix policy is unsupported.","Trigger and class behavior must be explicit.","Choose profile only, trigger, or trigger and class."))
    if configuration.caption_prefix_policy in {"trigger","trigger_and_class"} and not _one_line(configuration.trigger_term):
        issues.append(TrainingConfigurationIssue("trigger_required","blocking","Enter the stable trigger term.","The selected prefix policy requires the same explicit trigger in every package caption.","Enter a trigger term or use the unchanged profile caption."))
    if configuration.caption_prefix_policy=="trigger_and_class" and not _one_line(configuration.class_term):
        issues.append(TrainingConfigurationIssue("class_required","blocking","Enter the stable class term.","The selected prefix policy requires the same explicit class term in every package caption.","Enter a class term or choose another prefix policy."))
    if configuration.learning_rate<=0 or configuration.learning_rate>0.01:
        issues.append(TrainingConfigurationIssue("learning_rate_range","blocking","Learning rate is outside the safe planning range.","The neutral handoff accepts values above 0 through 0.01.","Choose a value in the displayed range."))
    if configuration.precision not in PRECISIONS:
        issues.append(TrainingConfigurationIssue("precision","blocking","The precision request is unsupported.","This neutral contract recognizes BF16, FP16, or float32.","Choose a supported precision request."))
    if configuration.regularization_policy!="none":
        issues.append(TrainingConfigurationIssue("regularization_unsupported","blocking","Regularization images are not supported by this manual package version.","CollectiGen will not invent class images or silently repurpose validation images.","Use None; TRN-03 may add a verified trainer-specific need and file mapping."))
    if configuration.validation_policy!="assigned_roles":
        issues.append(TrainingConfigurationIssue("validation_policy","blocking","The validation policy is unsupported.","This package must preserve the reviewed deterministic role assignments.","Use assigned roles."))
    if configuration.image_transform_policy!="lossless_when_required":
        issues.append(TrainingConfigurationIssue("transform_policy","blocking","The image transform policy is unsupported.","The manual package copies unchanged bytes and uses lossless PNG only for explicit technical normalization.","Use the supported loss-aware policy."))
    if configuration.overridden_fields and not _one_line(configuration.override_reason):
        issues.append(TrainingConfigurationIssue("settings_override_reason","blocking","Record why the recommended settings were changed.","The exact handoff must keep user changes explainable and reproducible.","Enter one reason covering the changed fields."))
    issues.append(TrainingConfigurationIssue(
        "trainer_adapter_unverified","caution","No named trainer compatibility has been claimed.",
        "This package is a manual, source-neutral handoff. Kohya_ss and OneTrainer translation is deliberately deferred to TRN-03.",
        "Use the package manually only after the target trainer confirms its image/caption/config requirements.",
    ))
    issues.append(TrainingConfigurationIssue(
        "tokenizer_unavailable","caution","The target tokenizer is unavailable in the manual adapter.",
        "CollectiGen can preserve the requested token limit and exact caption text, but it cannot claim an exact tokenizer count.",
        "Review captions in the eventual validated trainer adapter before execution.",
    ))
    return tuple(issues)


def package_caption(canonical_caption: str,configuration: TrainingConfiguration) -> str:
    canonical=_one_line(canonical_caption)
    prefix=[]
    if configuration.caption_prefix_policy in {"trigger","trigger_and_class"}:
        prefix.append(_one_line(configuration.trigger_term))
    if configuration.caption_prefix_policy=="trigger_and_class":
        prefix.append(_one_line(configuration.class_term))
    values=[value for value in (*prefix,canonical) if value]
    return ", ".join(values)


def estimate_training_resources(
    configuration: TrainingConfiguration,candidates: Iterable[object],
) -> TrainingResourceEstimate:
    included=tuple(
        item for item in candidates
        if getattr(item,"decision","")=="include"
    )
    training=tuple(item for item in included if getattr(item,"role","")=="training")
    source_bytes=sum(
        int(getattr(getattr(item,"evidence",None),"size",0) or 0)
        for item in included
    )
    transformed_reserve=sum(
        max(
            int(getattr(getattr(item,"evidence",None),"size",0) or 0),
            int(getattr(getattr(item,"evidence",None),"width",0) or 0)
            *int(getattr(getattr(item,"evidence",None),"height",0) or 0)*4
            +4_096,
        )
        for item in included
    )
    caption_bytes=sum(len(package_caption(
        str(getattr(item,"caption","") or ""),configuration,
    ).encode("utf-8")) for item in included)
    package_lower=source_bytes+caption_bytes+16_384+4_096*len(included)
    staging=2*(transformed_reserve+caption_bytes+16_384+4_096*len(included))
    exposures=len(training)*configuration.repeats*configuration.epochs
    steps=(configuration.epochs*math.ceil(
        len(training)*configuration.repeats/configuration.gradient_accumulation)
        if configuration.target_model_family.casefold()=="anima" and configuration.batch_size==1
        else None)
    return TrainingResourceEstimate(
        "collectigen.training-resource-scope-v2",exposures,steps,
        package_lower,staging,"Unmeasured; use a bounded native trial.",
        "Unmeasured; inspect current free GPU memory at native preflight.",
        "Unmeasured beyond the package; inspect native output and checkpoint budget.",(
            "Anima batch-one steps flush accumulation per epoch; SDXL steps await exact bucket counts from verified preparation.",
            "Package bytes are a conservative lower bound from current included source bytes, captions, and manifest/config overhead.",
            "Time and peak memory require an admitted native trial and do not establish output quality.",
        ),
    )
