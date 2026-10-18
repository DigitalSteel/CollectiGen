"""Pure package-to-reference image policy assessment; never runtime authority.

Call only after the shared package verifier and stale guards. The finite
generated reference cases predict risks; they do not observe selected pixels.
No filesystem, trainer, tokenizer or model access occurs in this module.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import training_image_effects_contract as reference

PROFILE = "package-image-policy-reference-v1"
MAX_ITEMS = 50_000
HOLD_LABELS = {
    "crop_conflict": "Crop conflicts with no-crop choice",
    "upscale_conflict": "Enlargement conflicts with no-upscale choice",
    "alpha_loss": "Transparency needs a supported treatment",
    "crop_policy_unverified": "Centered crop mapping needs verification",
    "geometry_unmeasured": "Image size or settings need measurement",
    "pixels_unmeasured": "Image format or color handling needs measurement",
    "caption_change": "Caption text would change",
}
HOLD_SUMMARIES = {
    "crop_conflict": "Crop conflict", "upscale_conflict": "Upscale conflict",
    "alpha_loss": "Transparency loss", "crop_policy_unverified": "Centered crop unverified",
    "geometry_unmeasured": "Geometry unmeasured", "pixels_unmeasured": "Color or format unmeasured",
    "caption_change": "Caption would change",
}


def profile_sha256():
    return reference.sha(reference.canonical({"profile": PROFILE,
        "reference": reference.expected_report(), "subset": reference.SUBSET,
        "dataset": reference.DATASET, "holds": HOLD_LABELS, "summaries": HOLD_SUMMARIES}))


def policy_holds(*, input_wh, resized_wh, removed_ltrb, crop_policy, allow_upscale, has_alpha):
    """Keep the user's policy separate from measured or predicted geometry."""
    holds = []
    if crop_policy == "none" and any(removed_ltrb):
        holds.append("crop_conflict")
    if not allow_upscale and any(after > before for before, after in zip(input_wh, resized_wh)):
        holds.append("upscale_conflict")
    if has_alpha:
        # The qualified reference explicitly drops alpha. No approved treatment
        # is represented by this proposal; packaging permission is not consent.
        holds.append("alpha_loss")
    return tuple(holds)


@dataclass(frozen=True)
class ImageEffectAssessment:
    status: str
    reference_case: str = ""
    input_wh: tuple[int, int] = (0, 0)
    resized_wh: tuple[int, int] | None = None
    output_wh: tuple[int, int] | None = None
    removed_ltrb: tuple[int, int, int, int] | None = None
    conditioning_crop_hw: tuple[int, int] | None = None
    holds: tuple[str, ...] = ()

    @property
    def summary(self):
        if self.status == "not_training":
            return "Outside training"
        if self.holds:
            return "; ".join(HOLD_SUMMARIES[code] for code in self.holds[:2]) + (f" (+{len(self.holds)-2})" if len(self.holds) > 2 else "")
        return "Reference match only"

    @property
    def search_text(self):
        return " ".join((self.summary, *(HOLD_LABELS[code] for code in self.holds)))

    def detail(self):
        if self.status == "not_training":
            return "Outside training. This role is retained in the package and omitted from the training subset."
        lines = [self.summary, *(HOLD_LABELS[code] for code in self.holds),
            "Reference prediction only. These package images have not been run through a qualified trainer."]
        if self.reference_case:
            lines += [f"Generated reference case: {self.reference_case}",
                f"Input → resized → output (width × height): {self.input_wh} → {self.resized_wh} → {self.output_wh}",
                f"Expected removed pixels (left, top, right, bottom): {self.removed_ltrb}",
                f"Separate SDXL conditioning crop (height, width): {self.conditioning_crop_hw}"]
            if self.resized_wh != self.input_wh:
                lines.append("Resizing changes pixels; no pixel-preservation or output-quality claim is made.")
        lines.append("Token encoding remains unverified.")
        lines.append("Review package choices in Training Setup if needed, then build a new package. No source or package file is changed here.")
        return "\n".join(lines)


def assess_image(image, caption, role, config):
    """Project one freshly verified artifact pair using only finite references.

    Unknown dimensions/options stay held; do not extrapolate the bucketing
    algorithm or confuse reference conditioning with pixels actually removed.
    """
    transform = image["transform"]
    size = tuple(transform["output_dimensions"])
    if role != "training":
        return ImageEffectAssessment("not_training", input_wh=size)
    operations = transform["operations"]
    alpha = "A" in transform["output_mode"] or any("alpha" in op for op in operations)
    holds = []
    if config.crop_policy != "none":
        # A finite bucket reference can show expected removed pixels without
        # establishing a translation of the neutral centered-crop request.
        holds.append("crop_policy_unverified")
    color_profile = any("color_profile" in op for op in operations)
    if transform["output_format"] != "PNG" or transform["output_mode"] != "RGB" or color_profile:
        holds.append("pixels_unmeasured")
    # Package verification has already proved the UTF-8 caption + one LF bytes.
    # Keep a defensive check for future profiles that might admit raw sidecars.
    text = caption["package_caption"]
    if not text or text.split("\n", 1)[0].strip() != text or "\r" in text:
        holds.append("caption_change")
    row = None
    if config.target_resolution == 1024 and config.crop_policy in {"none", "center"}:
        for candidate in reference.GEOMETRY:
            name, dimensions, *_ = candidate
            if dimensions != size:
                continue
            if config.bucket_policy == "square":
                matches = name == "square"
            elif config.bucket_policy == "aspect_ratio":
                matches = name == "upscale" if config.allow_upscale else name not in {"upscale", "square"}
            else:
                matches = False
            if matches:
                row = candidate
                break
    if row is None:
        holds.append("geometry_unmeasured")
        if alpha:
            holds.insert(0, "alpha_loss")
        return ImageEffectAssessment("held", input_wh=size, holds=tuple(holds))
    name, dimensions, resized, output, removed, conditioning = row
    holds = list(policy_holds(input_wh=size, resized_wh=resized, removed_ltrb=removed,
        crop_policy=config.crop_policy, allow_upscale=config.allow_upscale, has_alpha=alpha)) + holds
    return ImageEffectAssessment("held" if holds else "reference_match", name, size,
        resized, output, removed, conditioning, tuple(holds))


def dataset_proposal(directory, config):
    """Propose explicit effect controls; installed schema acceptance is unverified.

    None-valued constructor defaults cannot be emitted in the dataset schema.
    Their installed parser/default binding remains a separate runtime hold.
    """
    subset = {key: value for key, value in reference.SUBSET.items()
        if value is not None and key not in {"validation_seed", "validation_split"}}
    subset.update(image_dir=str(directory), num_repeats=config.repeats)
    dataset = {key: reference.DATASET[key] for key in ("min_bucket_reso", "max_bucket_reso",
        "bucket_reso_steps", "network_multiplier", "validation_seed", "validation_split")}
    dataset.update(resolution=config.target_resolution, batch_size=config.batch_size,
        enable_bucket=config.bucket_policy == "aspect_ratio", bucket_no_upscale=not config.allow_upscale,
        subsets=[subset])
    return {"datasets": [dataset]}
