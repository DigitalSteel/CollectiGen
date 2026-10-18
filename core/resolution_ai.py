"""Bounded, offline ESRGAN RRDBNet 4x inference for reviewed Safetensors.

The RRDB/RRDBNet structure is adapted from Xintao Wang's ESRGAN reference
implementation, Copyright 2018 Xintao Wang et al., Apache-2.0:
https://github.com/xinntao/ESRGAN/blob/master/RRDBNet_arch.py
No upstream model loader or executable model code is used.
"""
from __future__ import annotations

import hashlib
import importlib
from importlib import metadata
import json
import math
from pathlib import Path
import re
from typing import Callable

from PIL import Image

from .training_source_io import _assert_unredirected, read_stable_training_source

ADAPTER_ID = "collectigen.esrgan.rrdbnet4x.safetensors.v1"
RRDBNET_REVISION = "xinntao-esrgan-rrdbnet4x-compatible-v1"
MAX_MODEL_BYTES = 512 * 1024 * 1024
MAX_HEADER_BYTES = 16 * 1024 * 1024
MAX_TENSORS = 2048
MAX_PIXELS = 40_000_000
TILE_SIZE = 128
TILE_OVERLAP = 32
_DTYPE_BYTES = {"F16": 2, "BF16": 2, "F32": 4}


def runtime_status() -> dict:
    """Return actionable native-runtime readiness without loading a model."""
    try:
        from .dependencies import _activate_optional_component_site
        _activate_optional_component_site()
        torch = importlib.import_module("torch")
        importlib.import_module("safetensors.torch")
        importlib.import_module("numpy")
        versions = {name: metadata.version(name) for name in ("torch", "safetensors", "numpy")}
        cuda = bool(torch.cuda.is_available())
        return {"ready": True, "versions": versions, "cuda_available": cuda,
                "device": "cuda" if cuda else "cpu", "error": ""}
    except Exception as exc:
        return {"ready": False, "versions": {}, "cuda_available": False,
                "device": "unavailable",
                "error": f"Native ESRGAN runtime unavailable. Use the supported application launcher and review optional AI components in Setup. {type(exc).__name__}: {exc}"}


def _read_model(path: str | Path) -> tuple[Path, bytes, dict]:
    source = Path(path).absolute()
    if source.suffix.casefold() != ".safetensors":
        raise ValueError("Native ESRGAN accepts Safetensors models only")
    _assert_unredirected(source)
    if source.stat(follow_symlinks=False).st_size > MAX_MODEL_BYTES:
        raise ValueError("The model exceeds the bounded 512 MiB adapter limit")
    raw, identity = read_stable_training_source(source)
    if len(raw) > MAX_MODEL_BYTES:
        raise ValueError("The model exceeds the bounded 512 MiB adapter limit")
    return source, raw, identity


def _parse_header(raw: bytes) -> dict[str, dict]:
    if len(raw) < 10:
        raise ValueError("Safetensors file is truncated")
    header_size = int.from_bytes(raw[:8], "little")
    if header_size < 2 or header_size > MAX_HEADER_BYTES or 8 + header_size > len(raw):
        raise ValueError("Safetensors header is outside the bounded valid range")
    try:
        header = json.loads(raw[8:8 + header_size].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Safetensors header is invalid") from exc
    if not isinstance(header, dict):
        raise ValueError("Safetensors header must be an object")
    tensors = {key: value for key, value in header.items() if key != "__metadata__"}
    if not tensors or len(tensors) > MAX_TENSORS or any(not isinstance(key, str) for key in tensors):
        raise ValueError("Safetensors tensor count is unsupported")
    data_size = len(raw) - 8 - header_size
    ranges = []
    for key, value in tensors.items():
        if not isinstance(value, dict) or set(value) != {"dtype", "shape", "data_offsets"}:
            raise ValueError(f"Malformed tensor header: {key}")
        dtype, shape, offsets = value["dtype"], value["shape"], value["data_offsets"]
        if dtype not in _DTYPE_BYTES or not isinstance(shape, list) or not shape:
            raise ValueError(f"Unsupported tensor dtype or shape: {key}")
        if any(type(item) is not int or item <= 0 for item in shape):
            raise ValueError(f"Invalid tensor shape: {key}")
        if (not isinstance(offsets, list) or len(offsets) != 2
                or any(type(item) is not int for item in offsets)):
            raise ValueError(f"Invalid tensor offsets: {key}")
        start, end = offsets
        if start < 0 or end <= start or end > data_size:
            raise ValueError(f"Tensor lies outside model data: {key}")
        if math.prod(shape) * _DTYPE_BYTES[dtype] != end - start:
            raise ValueError(f"Tensor byte size does not match its shape: {key}")
        ranges.append((start, end))
    ranges.sort()
    cursor = 0
    for start, end in ranges:
        if start != cursor:
            raise ValueError("Safetensors data contains a gap or overlap")
        cursor = end
    if cursor != data_size:
        raise ValueError("Safetensors data is not fully described by its header")
    return tensors


def _canonical_key(key: str, key_format: str, nb: int) -> str:
    if key_format == "modern":
        return key
    if key_format == "original":
        mapped = key.replace("RRDB_trunk.", "body.").replace(".RDB", ".rdb")
        mapped = mapped.replace("trunk_conv", "conv_body").replace("upconv1", "conv_up1")
        return mapped.replace("upconv2", "conv_up2").replace("HRconv", "conv_hr")
    mapped = re.sub(r"\.conv([1-5])\.0\.(weight|bias)$", r".conv\1.\2", key)
    prefixes = {"model.0.": "conv_first.", f"model.1.sub.{nb}.": "conv_body.",
                "model.3.": "conv_up1.", "model.6.": "conv_up2.",
                "model.8.": "conv_hr.", "model.10.": "conv_last."}
    for prefix, replacement in prefixes.items():
        if mapped.startswith(prefix):
            return replacement + mapped[len(prefix):]
    match = re.match(r"model\.1\.sub\.(\d+)\.RDB([123])\.(.+)", mapped)
    if match:
        return f"body.{match.group(1)}.rdb{match.group(2)}.{match.group(3)}"
    return mapped


def _model_config(tensors: dict[str, dict]) -> tuple[dict, dict[str, str]]:
    keys = set(tensors)
    if "model.0.weight" in keys:
        key_format = "sequential"
        blocks = {int(match.group(1)) for key in keys
                  if (match := re.match(r"model\.1\.sub\.(\d+)\.RDB1\.", key))}
        nb = max(blocks) + 1 if blocks else 0
    elif any(key.startswith("RRDB_trunk.") for key in keys):
        key_format = "original"
        blocks = {int(match.group(1)) for key in keys if (match := re.match(r"RRDB_trunk\.(\d+)\.", key))}
        nb = max(blocks) + 1 if blocks else 0
    else:
        key_format = "modern"
        blocks = {int(match.group(1)) for key in keys if (match := re.match(r"body\.(\d+)\.", key))}
        nb = max(blocks) + 1 if blocks else 0
    if not 1 <= nb <= 32 or blocks != set(range(nb)):
        raise ValueError("RRDB block indexes must be contiguous and bounded")
    mapping = {key: _canonical_key(key, key_format, nb) for key in keys}
    if len(set(mapping.values())) != len(mapping):
        raise ValueError("Model keys map ambiguously")
    canonical = {mapping[key]: value for key, value in tensors.items()}
    try:
        first = canonical["conv_first.weight"]["shape"]
        nf, in_nc = first[0], first[1]
        gc = canonical["body.0.rdb1.conv1.weight"]["shape"][0]
    except (KeyError, IndexError) as exc:
        raise ValueError("Required RRDBNet tensors are missing") from exc
    if in_nc != 3 or not 1 <= nf <= 256 or not 1 <= gc <= 128:
        raise ValueError("Only bounded three-channel RRDBNet models are supported")
    expected = _expected_shapes(nf, nb, gc)
    if set(canonical) != set(expected):
        unknown = sorted(set(canonical) - set(expected))[:3]
        missing = sorted(set(expected) - set(canonical))[:3]
        raise ValueError(f"RRDBNet keys mismatch; unknown={unknown}, missing={missing}")
    dtypes = set()
    count = 0
    for key, shape in expected.items():
        actual = canonical[key]
        if actual["shape"] != list(shape):
            raise ValueError(f"RRDBNet tensor shape mismatch: {key}")
        dtypes.add(actual["dtype"])
        count += math.prod(shape)
    if len(dtypes) != 1:
        raise ValueError("RRDBNet tensors must use one supported floating dtype")
    return ({"architecture": "RRDBNet", "scale": 4, "in_channels": 3,
             "out_channels": 3, "features": nf, "blocks": nb, "growth_channels": gc,
             "dtype": next(iter(dtypes)), "parameter_count": count,
             "key_format": key_format, "tile_size": TILE_SIZE,
             "tile_overlap": TILE_OVERLAP,
             "seam_limitation": "Fixed overlap bounds memory; unusually strong long-range effects may show tile seams."}, mapping)


def _expected_shapes(nf: int, nb: int, gc: int) -> dict[str, tuple[int, ...]]:
    result = {"conv_first.weight": (nf, 3, 3, 3), "conv_first.bias": (nf,)}
    for block in range(nb):
        for dense in range(1, 4):
            for conv in range(1, 6):
                output = nf if conv == 5 else gc
                inputs = nf + (conv - 1) * gc
                prefix = f"body.{block}.rdb{dense}.conv{conv}"
                result[prefix + ".weight"] = (output, inputs, 3, 3)
                result[prefix + ".bias"] = (output,)
    for name in ("conv_body", "conv_up1", "conv_up2", "conv_hr"):
        result[name + ".weight"] = (nf, nf, 3, 3)
        result[name + ".bias"] = (nf,)
    result["conv_last.weight"] = (3, nf, 3, 3)
    result["conv_last.bias"] = (3,)
    return result


def inspect_ai_model(path: str | Path, *, device: str | None = None) -> dict:
    source, raw, identity = _read_model(path)
    tensors = _parse_header(raw)
    config, _ = _model_config(tensors)
    status = runtime_status()
    selected = device or status["device"]
    if selected not in {"cpu", "cuda", "unavailable"}:
        raise ValueError("Execution device must be cpu or cuda")
    if selected == "cuda" and not status["cuda_available"]:
        raise RuntimeError("CUDA was selected but is not ready in the managed runtime")
    if selected in {"cpu", "cuda"} and not status["ready"]:
        selected = "unavailable"
    core = {"sha256": identity["sha256"], "revision": RRDBNET_REVISION,
            "config": config, "device": selected,
            "runtime_versions": status["versions"]}
    fingerprint = hashlib.sha256(json.dumps(core, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"adapter": ADAPTER_ID, "path": str(source), **core,
            "fingerprint": fingerprint, "runtime": status}


def _build_model(torch, config: dict):
    nn, functional = torch.nn, torch.nn.functional
    nf, nb, gc = config["features"], config["blocks"], config["growth_channels"]
    class RDB(nn.Module):
        def __init__(self):
            super().__init__()
            self.conv1=nn.Conv2d(nf,gc,3,1,1); self.conv2=nn.Conv2d(nf+gc,gc,3,1,1)
            self.conv3=nn.Conv2d(nf+2*gc,gc,3,1,1); self.conv4=nn.Conv2d(nf+3*gc,gc,3,1,1)
            self.conv5=nn.Conv2d(nf+4*gc,nf,3,1,1)
        def forward(self,x):
            xs=[x]
            for layer in (self.conv1,self.conv2,self.conv3,self.conv4): xs.append(functional.leaky_relu(layer(torch.cat(xs,1)),.2))
            return x+self.conv5(torch.cat(xs,1))*.2
    class RRDB(nn.Module):
        def __init__(self): super().__init__(); self.rdb1=RDB(); self.rdb2=RDB(); self.rdb3=RDB()
        def forward(self,x): return x+self.rdb3(self.rdb2(self.rdb1(x)))*.2
    class Net(nn.Module):
        def __init__(self):
            super().__init__(); self.conv_first=nn.Conv2d(3,nf,3,1,1)
            self.body=nn.Sequential(*(RRDB() for _ in range(nb))); self.conv_body=nn.Conv2d(nf,nf,3,1,1)
            self.conv_up1=nn.Conv2d(nf,nf,3,1,1); self.conv_up2=nn.Conv2d(nf,nf,3,1,1)
            self.conv_hr=nn.Conv2d(nf,nf,3,1,1); self.conv_last=nn.Conv2d(nf,3,3,1,1)
        def forward(self,x):
            first=self.conv_first(x); value=first+self.conv_body(self.body(first))
            value=functional.leaky_relu(self.conv_up1(functional.interpolate(value,scale_factor=2,mode="nearest")),.2)
            value=functional.leaky_relu(self.conv_up2(functional.interpolate(value,scale_factor=2,mode="nearest")),.2)
            return self.conv_last(functional.leaky_relu(self.conv_hr(value),.2))
    return Net()


def resize_ai(image: Image.Image, target_size, model_spec: dict, *,
              cancelled: Callable[[], bool] = lambda: False,
              progress: Callable[[int, int, str], None] = lambda *_: None) -> Image.Image:
    if image.mode not in {"RGB", "RGBA"}:
        raise ValueError("Native ESRGAN accepts RGB or RGBA images only")
    size = tuple(target_size)
    if len(size) != 2 or any(type(v) is not int or v <= 0 for v in size):
        raise ValueError("Target dimensions must be positive integers")
    source_pixels, target_pixels = image.width*image.height, size[0]*size[1]
    if source_pixels > MAX_PIXELS or target_pixels > MAX_PIXELS or source_pixels*16 > MAX_PIXELS:
        raise ValueError("Source, native 4x intermediate, and output are bounded to 40 megapixels")
    if target_pixels <= source_pixels:
        raise ValueError("Native ESRGAN requires an upscale target larger than the source")
    if abs(size[0]*image.height-size[1]*image.width) > max(image.size):
        raise ValueError("Native ESRGAN target dimensions must preserve source aspect ratio")
    if model_spec.get("adapter") != ADAPTER_ID or model_spec.get("device") not in {"cpu", "cuda"}:
        raise RuntimeError("Select a ready native ESRGAN runtime before resizing")
    source, raw, identity = _read_model(model_spec["path"])
    current = inspect_ai_model(source, device=model_spec["device"])
    for key in ("sha256", "revision", "config", "device", "fingerprint"):
        if current.get(key) != model_spec.get(key):
            raise RuntimeError("The reviewed ESRGAN model or runtime changed; inspect it again")
    if identity["sha256"] != current["sha256"]:
        raise RuntimeError("The ESRGAN model changed while preparing inference")
    if cancelled(): raise InterruptedError("Native ESRGAN resize cancelled safely")
    status = runtime_status()
    if not status["ready"]: raise RuntimeError(status["error"])
    torch=importlib.import_module("torch"); np=importlib.import_module("numpy")
    safe=importlib.import_module("safetensors.torch")
    tensors=safe.load(raw); header=_parse_header(raw); _,mapping=_model_config(header)
    state={mapping[key]:value for key,value in tensors.items()}
    if any(not torch.isfinite(value).all().item() for value in state.values()):
        raise ValueError("ESRGAN weights contain non-finite values")
    device=torch.device(model_spec["device"]); model=None
    try:
        model=_build_model(torch,model_spec["config"])
        model.load_state_dict(state,strict=True); model.eval().to(device)
        rgb=image.convert("RGB"); array=np.asarray(rgb,dtype=np.float32)/255.0
        tensor=torch.from_numpy(array.copy()).permute(2,0,1).unsqueeze(0)
        output=torch.empty((3,image.height*4,image.width*4),dtype=torch.float32)
        tiles=math.ceil(image.width/TILE_SIZE)*math.ceil(image.height/TILE_SIZE); done=0
        with torch.inference_mode():
            for top in range(0,image.height,TILE_SIZE):
                for left in range(0,image.width,TILE_SIZE):
                    if cancelled(): raise InterruptedError("Native ESRGAN resize cancelled safely")
                    bottom=min(image.height,top+TILE_SIZE); right=min(image.width,left+TILE_SIZE)
                    y0=max(0,top-TILE_OVERLAP); x0=max(0,left-TILE_OVERLAP)
                    y1=min(image.height,bottom+TILE_OVERLAP); x1=min(image.width,right+TILE_OVERLAP)
                    patch=model(tensor[:,:,y0:y1,x0:x1].to(device))[0].float().cpu()
                    if not torch.isfinite(patch).all().item():
                        raise ValueError("ESRGAN produced non-finite pixels; result withheld")
                    output[:,top*4:bottom*4,left*4:right*4]=patch[:,(top-y0)*4:(bottom-y0)*4,(left-x0)*4:(right-x0)*4]
                    done+=1; progress(done,tiles,"Upscaling with native ESRGAN")
        pixels=output.clamp(0,1).mul(255).round().byte().permute(1,2,0).numpy()
        result=Image.fromarray(pixels,"RGB")
        if result.size != size: result=result.resize(size,Image.Resampling.LANCZOS)
        if image.mode=="RGBA": result.putalpha(image.getchannel("A").resize(size,Image.Resampling.LANCZOS))
        result.info.update(image.info)
        _,_,after= _read_model(source)
        if after["sha256"] != identity["sha256"]: raise RuntimeError("ESRGAN model changed during inference; result withheld")
        return result
    finally:
        if model is not None: del model
        if device.type=="cuda": torch.cuda.empty_cache()
