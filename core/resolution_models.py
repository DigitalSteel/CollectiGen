"""Reviewed optional upscalers using the shared acquisition and model registry."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .model_resource_registry import ModelResourceError, inspect_model_resource
from .model_workspace import (_path, _pinned_model_file, acquire_model,
                              preview_acquisition)
from .resolution_ai import inspect_ai_model


@dataclass(frozen=True)
class UpscalerModel:
    id: str
    name: str
    filename: str
    revision: str
    sha256: str
    size: int
    source: str
    description: str
    attribution: str = "Xintao Wang · Safetensors repack: Comfy-Org"
    license_name: str = "BSD-3-Clause"
    license_notice: str = ""

    @property
    def url(self):
        return f"{self.source}/resolve/{self.revision}/{self.filename}"

    @property
    def terms(self):
        return self.license_notice or MODEL_LICENSE


UPSCALERS = (UpscalerModel(
    "realesrgan-x4plus", "Real-ESRGAN x4plus · General images",
    "RealESRGAN_x4plus.safetensors", "ea19b4cd14f85a5b914eee8aa7ff77bc371039a0",
    "37f9a931c215f040aa6d50f711f2cb115f713c46df1d0d6469a8bd7bfe9a60bb", 66857836,
    "https://huggingface.co/Comfy-Org/Real-ESRGAN_repackaged",
    "Enlarges photos and illustrations with a local 4× ESRGAN model. "
    "May invent detail. CPU supported; a compatible GPU is faster.",
), UpscalerModel(
    "realesrgan-anime6b", "Real-ESRGAN Anime6B · Anime and illustration",
    "realesrgan_x4plus_anime_6B.safetensors", "d1679e8f2d87472deb367f6f2914afe114a4ccb2",
    "727b0c83e618a9d1b905e7ec32367d7c3e7554bb6506d84ecd85f8d82b5c14dc", 17888780,
    "https://huggingface.co/wkrettek/real-esrgan-models",
    "Suggested for anime and drawn illustrations. A smaller 6-block model optimized by its authors for anime images. "
    "Try general x4plus for photographs; compare edges and fine lines before keeping a result.",
    attribution="Xintao Wang · Safetensors conversion: wkrettek",
), UpscalerModel(
    "remacri", "Remacri · Detail-oriented alternative (noncommercial)",
    "4x_foolhardy_Remacri.safetensors", "491a1c74ca8f575e8c803fe2340a67170070ac98",
    "b3fc16df6d66e8d2cc5e5a1889b4844410744ba8f1a9d937bfa6d569f1942a64", 33467822,
    "https://huggingface.co/OzzyGT/4xRemacri",
    "A general-image alternative whose author aims for more detail and less smoothing. "
    "Compare texture and skin against x4plus; extra detail can be invented. "
    "Noncommercial license with attribution and share-alike terms; review before use.",
    attribution="FoolhardyVEVO · Safetensors conversion: OzzyGT",
    license_name="CC-BY-NC-SA-4.0",
    license_notice="Remacri by FoolhardyVEVO; Safetensors conversion by OzzyGT. "
    "CC BY-NC-SA 4.0: noncommercial use, attribution, indication of changes and share-alike conditions. "
    "Full terms: https://creativecommons.org/licenses/by-nc-sa/4.0/legalcode.en . "
    "Original model: https://openmodeldb.info/models/4x-Remacri . "
    "Conversion and attribution: https://huggingface.co/OzzyGT/4xRemacri . "
    "Model supplied without warranty. No upstream endorsement is implied.",
))

# Retained upstream notice also travels in the acquisition receipt and registry.
MODEL_LICENSE = """BSD 3-Clause License

Copyright (c) 2021, Xintao Wang
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.
3. Neither the name of the copyright holder nor the names of its
   contributors may be used to endorse or promote products derived from
   this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""


@dataclass(frozen=True)
class UpscalerInstallPlan:
    model: UpscalerModel | None
    destination: Path
    registry_revision: str
    acquisition: object | None


def preview_upscaler(registry, model, destination, *, protected_roots=()):
    """No network or writes; an existing exact file can be added offline."""
    if model is not None and model not in UPSCALERS:
        raise ModelResourceError("Choose an upscaler from the reviewed catalog.")
    target = _path(destination, protected_roots)
    _, revision = registry.snapshot()
    acquisition = None
    if not target.exists():
        if model is None:
            raise ModelResourceError("Choose an existing local Safetensors model. Custom models are not downloaded.")
        acquisition = preview_acquisition(
            provider="hugging_face", version=model.revision, url=model.url,
            sha256=model.sha256, size=model.size, destination=target,
            terms=model.terms, protected_roots=protected_roots)
    return UpscalerInstallPlan(model, target, revision, acquisition)


def verify_upscaler(model, path, *, protected_roots=(), should_cancel=None, progress=None):
    target = _path(path, protected_roots)
    with _pinned_model_file(target, protected_roots):
        inspection = inspect_model_resource(target, should_cancel=should_cancel, progress=progress)
        if model is not None and (inspection.resource_id != "sha256:" + model.sha256 or target.stat().st_size != model.size):
            raise ModelResourceError("This file does not match the selected catalog model. For another variant, choose Other local ESRGAN model. Existing files are never replaced.")
        spec = inspect_ai_model(target)
        if "sha256:" + spec["sha256"] != inspection.resource_id:
            raise ModelResourceError("The model changed during verification.")
        return inspection, spec


def install_upscaler(registry, plan, *, confirmed=False, protected_roots=(), should_cancel=None, progress=None):
    """Explicit download-and-register consent; preserved files support offline retry."""
    if not confirmed:
        raise ModelResourceError("Review and confirm Download & Add to Models first.")
    if plan.model is not None and plan.model not in UPSCALERS:
        raise ModelResourceError("Unknown catalog model.")
    if registry.snapshot()[1] != plan.registry_revision:
        raise ModelResourceError("Models changed after preview. Review the download again.")
    if plan.acquisition is not None:
        # Rebuild rather than trusting a caller-supplied acquisition object.
        expected = preview_upscaler(registry, plan.model, plan.destination, protected_roots=protected_roots)
        if expected != plan:
            raise ModelResourceError("The destination changed after preview. Review it again.")
        acquire_model(plan.acquisition, confirmed=True, protected_roots=protected_roots,
                      should_cancel=should_cancel, progress=progress)
    target = _path(plan.destination, protected_roots)
    with _pinned_model_file(target, protected_roots):
        inspection, spec = verify_upscaler(plan.model, target, protected_roots=protected_roots,
                                           should_cancel=should_cancel, progress=progress)
        registry.register(inspection, role="upscaler", family="ESRGAN",
                          expected_registry=plan.registry_revision,
                          source=plan.model.url if plan.model else "User-selected local upscaler",
                          license_note=plan.model.terms if plan.model else "Custom local model: source, license and intended use are not verified by the download catalog.",
                          should_cancel=should_cancel, progress=progress)
    return spec
