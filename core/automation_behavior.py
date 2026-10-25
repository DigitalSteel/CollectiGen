from __future__ import annotations

from dataclasses import dataclass,replace

from .preparation_workflow import CONFIDENCE_PRESETS


AUTOMATION_BEHAVIOR_SETTING_KEYS=(
    "suggest_wd_for_missing_tags",
    "suggest_joycaption_for_missing_descriptions",
    "auto_run_wd_on_managed_images",
    "auto_run_joy_on_managed_images",
    "wd_general_threshold",
    "wd_character_threshold",
    "wd_rating_threshold",
    "wd_consensus_minimum",
    "auto_merge_ai_tags",
    "prepare_confidence_preset",
    "prepare_tag_vote_minimum",
    "prepare_external_evidence_vote",
    "prepare_confirm_safe_tags",
    "prepare_preferred_descriptions",
    "prepare_read_external_evidence",
    "ingest_create_duplicate_fingerprints",
    "prepare_ai_skip_value",
    "prepare_ai_skip_unit",
)


def _bounded_float(value,minimum: float,maximum: float) -> float:
    return max(minimum,min(maximum,float(value)))


def _bounded_int(value,minimum: int,maximum: int) -> int:
    return max(minimum,min(maximum,int(value)))


@dataclass(frozen=True)
class AutomationBehaviorPolicy:
    """Validated reusable AI behavior configured by Rules & Automation."""

    suggest_wd_for_missing_tags: bool=True
    suggest_joycaption_for_missing_descriptions: bool=True
    auto_run_wd_on_managed_images: bool=False
    auto_run_joy_on_managed_images: bool=False
    wd_general_threshold: float=.35
    wd_character_threshold: float=.85
    wd_rating_threshold: float=.35
    wd_consensus_minimum: int=1
    auto_merge_ai_tags: bool=False
    prepare_confidence_preset: str="balanced"
    prepare_tag_vote_minimum: int=2
    prepare_external_evidence_vote: bool=True
    prepare_confirm_safe_tags: bool=True
    prepare_preferred_descriptions: bool=True
    prepare_read_external_evidence: bool=True
    ingest_create_duplicate_fingerprints: bool=True
    prepare_ai_skip_value: int=7
    prepare_ai_skip_unit: str="days"

    @classmethod
    def from_settings(cls,settings) -> "AutomationBehaviorPolicy":
        default=cls()
        values={
            key:getattr(settings,key,getattr(default,key))
            for key in AUTOMATION_BEHAVIOR_SETTING_KEYS
        }
        preset=str(values["prepare_confidence_preset"] or "balanced")
        if preset not in {*CONFIDENCE_PRESETS,"custom"}:
            preset="custom"
        return cls(
            suggest_wd_for_missing_tags=bool(values["suggest_wd_for_missing_tags"]),
            suggest_joycaption_for_missing_descriptions=bool(
                values["suggest_joycaption_for_missing_descriptions"]
            ),
            auto_run_wd_on_managed_images=bool(values["auto_run_wd_on_managed_images"]),
            auto_run_joy_on_managed_images=bool(values["auto_run_joy_on_managed_images"]),
            wd_general_threshold=_bounded_float(values["wd_general_threshold"],.05,.95),
            wd_character_threshold=_bounded_float(values["wd_character_threshold"],.05,.99),
            wd_rating_threshold=_bounded_float(values["wd_rating_threshold"],.05,.99),
            wd_consensus_minimum=_bounded_int(values["wd_consensus_minimum"],1,99),
            auto_merge_ai_tags=bool(values["auto_merge_ai_tags"]),
            prepare_confidence_preset=preset,
            prepare_tag_vote_minimum=_bounded_int(values["prepare_tag_vote_minimum"],1,99),
            prepare_external_evidence_vote=bool(values["prepare_external_evidence_vote"]),
            prepare_confirm_safe_tags=bool(values["prepare_confirm_safe_tags"]),
            prepare_preferred_descriptions=bool(values["prepare_preferred_descriptions"]),
            prepare_read_external_evidence=bool(values["prepare_read_external_evidence"]),
            ingest_create_duplicate_fingerprints=bool(values["ingest_create_duplicate_fingerprints"]),
            prepare_ai_skip_value=_bounded_int(values["prepare_ai_skip_value"],1,999),
            prepare_ai_skip_unit=(str(values["prepare_ai_skip_unit"])
                if values["prepare_ai_skip_unit"] in {"hours","days","months"} else "days"),
        )

    @classmethod
    def from_dict(cls,values: dict) -> "AutomationBehaviorPolicy":
        proxy=type("AutomationBehaviorValues",(),values)()
        return cls.from_settings(proxy)

    def to_dict(self) -> dict[str,object]:
        return {key:getattr(self,key) for key in AUTOMATION_BEHAVIOR_SETTING_KEYS}

    def apply_to(self,settings) -> None:
        for key,value in self.to_dict().items():
            setattr(settings,key,value)

    def preparation_preset(self,preset: str) -> "AutomationBehaviorPolicy":
        if preset not in CONFIDENCE_PRESETS:
            return replace(self,prepare_confidence_preset="custom")
        values=CONFIDENCE_PRESETS[preset]
        return replace(
            self,
            prepare_confidence_preset=preset,
            prepare_tag_vote_minimum=int(values["minimum_tag_votes"]),
            prepare_external_evidence_vote=bool(values["external_evidence_vote"]),
        )

    def workflow_preset(
        self,mode: str,*,tag_ready: bool,description_ready: bool,
        enabled_tag_models: int,
    ) -> "AutomationBehaviorPolicy":
        if mode not in {"review","analyze","maximum"}:
            raise ValueError(f"Unsupported automation preset: {mode}")
        policy=replace(
            self,
            auto_run_wd_on_managed_images=False,
            auto_run_joy_on_managed_images=False,
            auto_merge_ai_tags=False,
            prepare_confirm_safe_tags=False,
            prepare_preferred_descriptions=False,
        )
        if mode in {"analyze","maximum"}:
            policy=replace(
                policy,
                auto_run_wd_on_managed_images=tag_ready,
                auto_run_joy_on_managed_images=description_ready,
            )
        if mode=="maximum":
            policy=replace(
                policy,
                auto_merge_ai_tags=tag_ready,
                prepare_confirm_safe_tags=tag_ready,
                prepare_preferred_descriptions=description_ready,
                wd_consensus_minimum=(
                    max(2,enabled_tag_models//2+1) if tag_ready else 1
                ),
            ).preparation_preset("balanced")
        return policy

    def summary(self) -> str:
        after=[]
        if self.auto_run_wd_on_managed_images:
            after.append("AI Tags")
        if self.auto_run_joy_on_managed_images:
            after.append("AI Descriptions")
        prepare=[]
        if self.prepare_confirm_safe_tags:
            prepare.append("confirm safe Tags")
        if self.prepare_preferred_descriptions:
            prepare.append("fill or upgrade Descriptions")
        confirmation=(
            f"require {self.wd_consensus_minimum} independent votes"
            if self.auto_merge_ai_tags else "off"
        )
        return (
            "After library checks: "
            +(f"generate {' and '.join(after)}" if after else "do not run AI automatically")
            +". During Prepare: "
            +(f"{' and '.join(prepare)}" if prepare else "keep AI results for review")
            +f". Automatic Tag confirmation after checks: {confirmation}."
        )


@dataclass(frozen=True)
class ExternalEvidenceIntakePolicy:
    """Reusable inward-evidence default owned by Rules & Automation."""

    auto_import_source_evidence: bool=True

    @classmethod
    def from_settings(cls,settings) -> "ExternalEvidenceIntakePolicy":
        return cls(auto_import_source_evidence=bool(
            getattr(settings,"auto_import_source_evidence",True)
        ))

    def apply_to(self,settings) -> None:
        settings.auto_import_source_evidence=self.auto_import_source_evidence


@dataclass(frozen=True)
class LibraryCheckPolicy:
    """Reusable library-check trigger and folder-scope defaults."""

    validate_library_on_startup: bool=True
    include_subfolders: bool=True

    @classmethod
    def from_settings(cls,settings) -> "LibraryCheckPolicy":
        return cls(
            validate_library_on_startup=bool(
                getattr(settings,"validate_library_on_startup",True)
            ),
            include_subfolders=bool(getattr(settings,"include_subfolders",True)),
        )

    def apply_to(self,settings) -> None:
        settings.validate_library_on_startup=self.validate_library_on_startup
        settings.include_subfolders=self.include_subfolders


@dataclass(frozen=True)
class ExportContentPolicy:
    """Reusable maximum content rating for Dataset projections and exports."""

    maximum_content_rating: str="explicit"

    def __post_init__(self):
        if self.maximum_content_rating not in {
            "general","sensitive","questionable","explicit",
        }:
            object.__setattr__(self,"maximum_content_rating","explicit")

    @classmethod
    def from_settings(cls,settings) -> "ExportContentPolicy":
        return cls(str(getattr(settings,"export_content_level","explicit")))

    def apply_to(self,settings) -> None:
        settings.export_content_level=self.maximum_content_rating
