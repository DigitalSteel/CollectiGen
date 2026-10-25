from __future__ import annotations

import hashlib
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING,Callable

import PIL
from PIL import Image,ImageOps

from .training_source_io import read_stable_training_source

if TYPE_CHECKING:
    from .training_candidate_selection import TrainingCandidate


CancelCallback=Callable[[],bool]
COPYABLE_FORMATS={"JPEG":"jpg","PNG":"png","WEBP":"webp"}
COPYABLE_MODES={"RGB","RGBA","L"}


def _stable_source_bytes(path: Path) -> tuple[bytes,dict[str,object]]:
    raw,state=read_stable_training_source(path)
    return raw,{"exists":True,**state}


@dataclass(frozen=True)
class TrainingImageTransformPlan:
    stable_id: str
    source_state: dict[str,object]
    output_extension: str
    output_format: str
    byte_copy: bool
    operations: tuple[str,...]
    source_dimensions: tuple[int,int]
    expected_dimensions: tuple[int,int]
    expected_mode: str
    engine: str
    engine_version: str

    def to_dict(self) -> dict[str,object]:
        return {
            "stable_id":self.stable_id,"source_state":dict(self.source_state),
            "output_extension":self.output_extension,
            "output_format":self.output_format,"byte_copy":self.byte_copy,
            "operations":list(self.operations),
            "source_dimensions":list(self.source_dimensions),
            "expected_dimensions":list(self.expected_dimensions),
            "expected_mode":self.expected_mode,"engine":self.engine,
            "engine_version":self.engine_version,
            "resize_applied":False,"crop_applied":False,
        }


@dataclass(frozen=True)
class TrainingImageTransformResult:
    payload: bytes
    output_sha256: str
    output_size: int
    output_dimensions: tuple[int,int]
    output_mode: str
    plan: TrainingImageTransformPlan

    def provenance(self) -> dict[str,object]:
        return {
            **self.plan.to_dict(),"output_sha256":self.output_sha256,
            "output_size":self.output_size,
            "output_dimensions":list(self.output_dimensions),
            "output_mode":self.output_mode,
        }


def plan_training_image_transform(
    candidate: "TrainingCandidate",
) -> TrainingImageTransformPlan:
    evidence=candidate.evidence
    if evidence is None:
        raise ValueError("An unavailable source cannot be added to a training package.")
    source_state={
        "exists":True,"size":evidence.size,"mtime_ns":evidence.mtime_ns,
        "sha256":evidence.sha256,
    }
    image_format=evidence.format.upper()
    byte_copy=(
        image_format in COPYABLE_FORMATS and evidence.mode in COPYABLE_MODES
        and evidence.frame_count==1 and evidence.exif_orientation==1
    )
    if byte_copy:
        operations=["copy_exact_source_bytes","preserve_source_framing","no_resize_or_crop"]
        if evidence.has_alpha:
            operations.append("preserve_alpha_in_source_bytes")
        if evidence.has_color_profile:
            operations.append("preserve_color_profile_in_source_bytes")
        return TrainingImageTransformPlan(
            candidate.stable_id,source_state,COPYABLE_FORMATS[image_format],
            image_format,True,tuple(operations),(evidence.width,evidence.height),
            (evidence.width,evidence.height),evidence.mode,"byte-copy","1",
        )
    operations=[]
    width,height=evidence.width,evidence.height
    if evidence.frame_count>1:
        operations.append("select_first_frame")
    if evidence.exif_orientation!=1:
        operations.append("apply_exif_orientation")
        if evidence.exif_orientation in {5,6,7,8}:
            width,height=height,width
    mode=evidence.mode
    if mode not in COPYABLE_MODES:
        if evidence.has_alpha or mode in {"LA","PA"}:
            mode="RGBA"; operations.append("convert_color_mode_to_rgba")
        elif mode=="L":
            mode="L"
        else:
            mode="RGB"; operations.append("convert_color_mode_to_rgb")
    if evidence.has_alpha:
        operations.append("preserve_alpha_in_lossless_png")
    if evidence.has_color_profile:
        operations.append(
            "preserve_compatible_color_profile"
            if mode==evidence.mode else "omit_incompatible_source_color_profile"
        )
    operations.extend(("encode_lossless_png","preserve_source_framing","no_resize_or_crop"))
    return TrainingImageTransformPlan(
        candidate.stable_id,source_state,"png","PNG",False,tuple(operations),
        (evidence.width,evidence.height),(width,height),mode,
        "Pillow",str(PIL.__version__),
    )


def render_training_image(
    candidate: "TrainingCandidate",plan: TrainingImageTransformPlan,* ,
    should_cancel: CancelCallback|None=None,
) -> TrainingImageTransformResult:
    cancel=should_cancel or (lambda:False)
    if cancel():
        raise InterruptedError("Training package image preparation was cancelled.")
    raw,current=_stable_source_bytes(Path(candidate.image))
    if current!=plan.source_state:
        raise RuntimeError(f"{Path(candidate.image).name} changed after package preview.")
    if plan.byte_copy:
        payload=raw
    else:
        with Image.open(BytesIO(raw)) as opened:
            opened.seek(0)
            frame=opened.copy()
            icc_profile=opened.info.get("icc_profile")
        if cancel():
            raise InterruptedError("Training package image preparation was cancelled.")
        if "apply_exif_orientation" in plan.operations:
            frame=ImageOps.exif_transpose(frame)
        if frame.mode!=plan.expected_mode:
            frame=frame.convert(plan.expected_mode)
        options={"format":"PNG","optimize":False,"compress_level":6}
        if (
            icc_profile and
            "preserve_compatible_color_profile" in plan.operations
        ):
            options["icc_profile"]=icc_profile
        output=BytesIO(); frame.save(output,**options); payload=output.getvalue()
    if cancel():
        raise InterruptedError("Training package image preparation was cancelled.")
    with Image.open(BytesIO(payload)) as verified:
        verified.load(); dimensions=tuple(int(value) for value in verified.size)
        mode=str(verified.mode)
        output_format=str(verified.format or "")
        frames=int(getattr(verified,"n_frames",1) or 1)
    if (
        dimensions!=plan.expected_dimensions or mode!=plan.expected_mode
        or output_format.upper()!=plan.output_format or frames!=1
    ):
        raise RuntimeError("A prepared training image failed decoded read-back verification.")
    _raw_after,after=_stable_source_bytes(Path(candidate.image))
    if after!=plan.source_state:
        raise RuntimeError(f"{Path(candidate.image).name} changed during package preparation.")
    return TrainingImageTransformResult(
        payload,hashlib.sha256(payload).hexdigest(),len(payload),
        dimensions,mode,plan,
    )
