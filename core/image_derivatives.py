"""Original-preserving resize versions shared by AI-02 and future EDIT-01 tools.

Each version is a complete image family in its own atomically published folder.
An immutable receipt binds all family bytes; interrupted staging is never inventory.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import base64
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

from PIL import Image, ImageOps, PngImagePlugin, __version__ as PILLOW_VERSION

from .external_write_transaction import atomic_verified_bytes
from .training_source_io import _assert_unredirected, read_stable_training_source
from .verified_transfer import VerifiedTransferEngine

ADAPTER_ID = "collectigen.pillow.lanczos.v1"
MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_PIXELS = 40_000_000


def _encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _result_name(fingerprint):
    return "resized-" + fingerprint + ".png"


def _read(path):
    _assert_unredirected(path)
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("This file exceeds the bounded resize input limit (256 MiB).")
    return read_stable_training_source(path)


def _family(source):
    result = {}
    for suffix in (source.suffix, ".tags.json", ".txt", ".xmp"):
        path = source if suffix == source.suffix else source.with_suffix(suffix)
        if path.exists():
            raw, identity = _read(path)
            result[suffix] = {"raw": raw, "identity": identity}
    return result


def _identities(family):
    return {key: value["identity"] for key, value in family.items()}


def _decode(raw):
    image = Image.open(io.BytesIO(raw))
    if image.width * image.height > MAX_PIXELS:
        raise ValueError("The manual adapter supports up to 40 megapixels per image.")
    if getattr(image, "n_frames", 1) != 1:
        raise ValueError("Animated and multi-frame sources require a separate adapter.")
    if image.format not in {"PNG", "JPEG", "WEBP"} or image.mode not in {"RGB", "RGBA", "L", "LA"}:
        raise ValueError("This adapter supports single-frame PNG, JPEG and WebP in RGB, RGBA, L or LA mode.")
    image.load()
    return image


def preview_resize(source, target_size, policy, *, force=False, allow_derivative=False, model_spec=None):
    """Freeze source/family bytes and a portable ordered recipe; no writes."""
    source = Path(source).absolute()
    family = _family(source)
    if source.suffix not in family:
        raise ValueError("Source image is unavailable.")
    raw = family[source.suffix]["raw"]
    image = _decode(raw)
    annotation_raw = family.get(".tags.json", {}).get("raw")
    annotation = json.loads(annotation_raw) if annotation_raw else {}
    if (image.info.get("collectigen_derivative") or annotation.get("derivative_lineage")) and not allow_derivative:
        raise ValueError("This is already a derivative. Explicitly allow a new reviewed version to resize it again.")
    size = tuple(target_size)
    if len(size) != 2 or any(type(v) is not int or v <= 0 for v in size) or size[0] * size[1] > MAX_PIXELS:
        raise ValueError("Choose positive output dimensions up to 40 megapixels.")
    display = ImageOps.exif_transpose(image)
    # Integer rounding may change either dimension by at most one pixel.
    if abs(size[0] * display.height - size[1] * display.width) > max(display.size):
        raise ValueError("Resize must preserve the source aspect ratio.")
    if size == display.size:
        raise ValueError("The target has the same dimensions as the current image.")
    # External XMP may assert stale dimensions and crop regions. Retain exact
    # source evidence in the recovery version; do not publish it as current.
    receipt = {
        "schema_version": 1, "source": str(source), "source_family": _identities(family),
        "source_size": list(display.size), "target_size": list(size),
        "source_format": image.format, "source_mode": image.mode,
        "recipe": {"operation": "resize", "adapter": ADAPTER_ID,
                   "revision": PILLOW_VERSION, "model": None, "resampling": "LANCZOS",
                   "target_size": list(size), "policy": deepcopy(policy), "output_format": "PNG",
                   "review_policy": "new-image-unreviewed-v1"},
        "scale": size[0] / display.width, "allow_derivative": bool(allow_derivative),
        "force_token": uuid4().hex if force else "",
        "effects": "Lossless PNG output; aspect ratio retained within pixel rounding. "
                   "Resampling can soften detail or introduce ringing; upscaling does not recover true detail. "
                   "Alpha and ICC are retained; orientation is applied. Exact original bytes and sidecars remain protected. "
                   "Confirmed Image Knowledge and TXT are linked to the new version; projection checks become stale. "
                   "External XMP remains source evidence in the retained original family.",
        "gpu": "Not used (local CPU)", "time": "Not benchmarked; depends on source and target pixels.",
        "estimated_bytes": sum(len(v["raw"]) for v in family.values()) + size[0] * size[1] * 8 + 65536,
    }
    if model_spec is not None:
        if not model_spec.get("runtime", {}).get("ready"):
            raise ValueError(model_spec.get("runtime", {}).get("error") or "The native AI runtime is unavailable; review Setup.")
        if display.width * display.height * 16 > MAX_PIXELS:
            raise ValueError("This AI adapter supports source images up to 2.5 MP (40 MP native 4x intermediate).")
        if size[0] <= display.width or size[1] <= display.height:
            raise ValueError("The AI upscale adapter is only for increasing resolution.")
        if image.mode not in {"RGB", "RGBA"}:
            raise ValueError("The AI adapter supports RGB and RGBA sources only.")
        from .resolution_ai import ADAPTER_ID as AI_ADAPTER_ID
        receipt["model_spec"] = deepcopy(model_spec)
        receipt["recipe"].update(adapter=AI_ADAPTER_ID, revision=model_spec["revision"],
            model={k: deepcopy(v) for k, v in model_spec.items() if k != "path"}, resampling="RRDB 4x then Lanczos to exact target")
        receipt["gpu"] = f"Local AI execution: {model_spec['device']}"
        receipt["effects"] += " AI may invent or alter detail; review output content and quality before using it. Tiled boundaries can differ from whole-image inference."
        receipt["estimated_bytes"] += display.width * display.height * 16 * 4
    receipt["fingerprint"] = _digest(receipt)
    return receipt


class DerivativeStore:
    """Contained immutable versions, exact read-back and reversible visibility."""

    def __init__(self, root):
        self.root = Path(root).absolute()

    def _validate_root(self):
        _assert_unredirected(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        _assert_unredirected(self.root)

    @staticmethod
    def _key(fingerprint):
        if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
            raise ValueError("Invalid derivative identity.")
        return fingerprint

    def _version(self, fingerprint):
        return self.root / self._key(fingerprint)

    def destination(self, plan):
        return self._version(plan["fingerprint"]) / _result_name(plan["fingerprint"])

    def _verify(self, folder, *, expected=None):
        _assert_unredirected(folder)
        raw, _ = _read(folder / "receipt.json")
        receipt = json.loads(raw)
        if receipt.get("schema_version") != 1 or receipt.get("fingerprint") != folder.name.removesuffix(".undone"):
            raise ValueError("Derivative receipt identity is invalid.")
        if expected is not None and receipt["fingerprint"] != expected:
            raise ValueError("The derivative differs from the reviewed result.")
        plan = receipt.get("plan", {})
        if _digest({k: v for k, v in plan.items() if k != "fingerprint"}) != receipt["fingerprint"]:
            raise ValueError("Retained resize plan fingerprint is invalid.")
        result_name = receipt.get("result_name", "result.png")
        if result_name not in {"result.png", _result_name(receipt["fingerprint"])}:
            raise ValueError("Invalid derivative result name.")
        result_stem = Path(result_name).stem
        expected_names = {result_name}
        for suffix, identity in plan["source_family"].items():
            expected_names.add("original" + suffix.lower())
            if suffix in {".tags.json", ".txt"}:
                expected_names.add(result_stem + suffix)
            if receipt["files"].get("original" + suffix.lower()) != identity["sha256"]:
                raise ValueError("Protected source family is incomplete or differs from the reviewed source.")
        if set(receipt["files"]) != expected_names:
            raise ValueError("Derivative receipt has missing or unsafe family members.")
        if {p.name for p in folder.iterdir()} != expected_names | {"receipt.json"}:
            raise ValueError("Retained version contains missing or unexpected files; recovery requires review.")
        for name, sha in receipt["files"].items():
            if name not in {result_name, result_stem + ".tags.json", result_stem + ".txt", "original.png", "original.jpg", "original.jpeg", "original.webp", "original.tags.json", "original.txt", "original.xmp"}:
                raise ValueError("Derivative receipt contains an unsafe family member.")
            _, identity = _read(folder / name)
            if identity["sha256"] != sha:
                raise ValueError("A retained derivative or original changed; recovery requires review.")
        with Image.open(folder / result_name) as image:
            image.load()
            if list(image.size) != receipt["plan"]["target_size"]:
                raise ValueError("Derivative dimensions failed read-back.")
        return receipt

    def recover(self, fingerprint):
        """Read-only restart verification; never replay an uncertain mutation."""
        folder = self._version(fingerprint)
        if not folder.exists():
            return {"state": "unpublished", "fingerprint": fingerprint,
                    "message": "No published version. Original is unchanged; preview again to retry. Staged work is not inventory."}
        result = self._verify(folder, expected=fingerprint)
        return {**result, "state": "verified", "output": str(folder / result.get("result_name", "result.png")), "store_root": str(self.root)}


    def history(self, limit=100):
        """Bounded read-only recovery inventory of published versions only."""
        _assert_unredirected(self.root)
        if not self.root.is_dir():
            return []
        results = []
        for index, entry in enumerate(self.root.iterdir()):
            if index >= 1000 or len(results) >= limit:
                break
            if len(entry.name) != 64:
                continue
            try:
                results.append(self.recover(self._key(entry.name)))
            except (ValueError, OSError, RuntimeError) as exc:
                results.append({"state": "unavailable", "fingerprint": entry.name, "error": str(exc), "store_root": str(self.root)})
        return results

    def execute(self, plan, *, cancelled=lambda: False, guard=lambda: None):
        body = {k: v for k, v in plan.items() if k != "fingerprint"}
        if _digest(body) != plan.get("fingerprint"):
            raise ValueError("Resize preview changed; rebuild it before execution.")
        if "model_spec" in plan:
            from .resolution_ai import inspect_ai_model
            if inspect_ai_model(plan["model_spec"]["path"]) != plan["model_spec"]:
                raise ValueError("The AI model or runtime changed; preview again.")
        elif plan["recipe"]["adapter"] != ADAPTER_ID or plan["recipe"]["revision"] != PILLOW_VERSION:
            raise ValueError("The resize adapter changed; preview again.")
        guard()
        source = Path(plan["source"])
        family = _family(source)
        if _identities(family) != plan["source_family"]:
            raise ValueError("Source image or sidecars changed after preview.")
        self._validate_root()
        destination = self._version(plan["fingerprint"])
        if destination.exists():
            return {**self.recover(plan["fingerprint"]), "reused": True}
        if cancelled():
            return {"state": "cancelled", "fingerprint": plan["fingerprint"]}
        if shutil.disk_usage(self.root).free < plan["estimated_bytes"]:
            raise ValueError("Insufficient space for the protected original and derivative.")
        stage = self.root / (".resize-" + uuid4().hex)
        stage.mkdir()
        try:
            image = _decode(family[source.suffix]["raw"])
            info = dict(image.info)
            transformed = ImageOps.exif_transpose(image)
            if "model_spec" in plan:
                from .resolution_ai import resize_ai
                resized = resize_ai(transformed, tuple(plan["target_size"]), plan["model_spec"], cancelled=cancelled)
            else:
                resized = transformed.resize(tuple(plan["target_size"]), Image.Resampling.LANCZOS)
            lineage = {"schema_version": 1, "parent_sha256": plan["source_family"][source.suffix]["sha256"],
                       "fingerprint": plan["fingerprint"], "recipe": plan["recipe"],
                       "created_at": datetime.now(timezone.utc).isoformat()}
            if "model_spec" in plan:
                lineage["review_required"] = "AI-generated detail and technical quality require review."
            png = PngImagePlugin.PngInfo()
            binary_metadata = {}
            for key, value in info.items():
                if isinstance(value, str) and key != "collectigen_derivative":
                    png.add_itxt(key, value)
                elif isinstance(value, bytes) and key not in {"icc_profile", "exif"}:
                    binary_metadata[key] = {"encoding": "base64", "bytes": base64.b64encode(value).decode("ascii")}
            if binary_metadata:
                png.add_itxt("collectigen_source_metadata_bytes", json.dumps(binary_metadata, sort_keys=True))
            png.add_itxt("collectigen_derivative", json.dumps(lineage, ensure_ascii=False))
            options = {"pnginfo": png}
            if info.get("icc_profile"):
                options["icc_profile"] = info["icc_profile"]
            exif = transformed.getexif()
            if exif:
                exif[256], exif[257] = resized.size
                exif[40962], exif[40963] = resized.size
                exif[274] = 1
                options["exif"] = exif.tobytes()
            payload = io.BytesIO()
            resized.save(payload, format="PNG", **options)
            result_name = _result_name(plan["fingerprint"])
            result_stem = Path(result_name).stem
            files = {result_name: payload.getvalue()}
            for suffix, member in family.items():
                files["original" + suffix.lower()] = member["raw"]
                if suffix == ".txt":
                    files[result_stem + ".txt"] = member["raw"]
                elif suffix == ".tags.json":
                    annotation = json.loads(member["raw"])
                    if not isinstance(annotation, dict) or annotation.get("schema_version", 1) > 6:
                        raise ValueError("Unsupported Image Knowledge; preserve source and update the application first.")
                    annotation = deepcopy(annotation)
                    annotation["image"] = result_name
                    annotation["derivative_lineage"] = lineage
                    annotation["file_identity"] = {}
                    annotation["projection_state"] = {}
                    annotation["standard_metadata"] = {}
                    annotation["review_status"] = "unreviewed"
                    files[result_stem + ".tags.json"] = _encoded(annotation)
            receipt = {"schema_version": 1, "fingerprint": plan["fingerprint"], "plan": plan,
                       "result_name": result_name,
                       "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}}
            for name, raw in files.items():
                if cancelled():
                    return {"state": "cancelled", "fingerprint": plan["fingerprint"]}
                atomic_verified_bytes(stage / name, raw, label="Resize version")
            atomic_verified_bytes(stage / "receipt.json", _encoded(receipt), label="Resize receipt")
            with Image.open(stage / result_name) as check:
                check.load()
                if check.size != resized.size or check.mode != resized.mode or check.tobytes() != resized.tobytes():
                    raise ValueError("Resize pixel read-back failed.")
                if check.info.get("icc_profile") != info.get("icc_profile"):
                    raise ValueError("Resize color-profile read-back failed.")
                for key, value in info.items():
                    if isinstance(value, str) and key != "collectigen_derivative" and check.info.get(key) != value:
                        raise ValueError("Raw Metadata did not survive resizing.")
                if binary_metadata and json.loads(check.info.get("collectigen_source_metadata_bytes", "{}")) != binary_metadata:
                    raise ValueError("Binary source Metadata did not survive resizing.")
            guard()
            if _identities(_family(source)) != plan["source_family"]:
                raise ValueError("Source image or sidecars changed while resizing; result withheld.")
            if "model_spec" in plan:
                from .resolution_ai import inspect_ai_model
                if inspect_ai_model(plan["model_spec"]["path"]) != plan["model_spec"]:
                    raise ValueError("The AI model changed before publication; result withheld.")
            if cancelled():
                return {"state": "cancelled", "fingerprint": plan["fingerprint"]}
            _assert_unredirected(self.root)
            VerifiedTransferEngine._rename_no_replace(stage, destination)
            return {**self.recover(plan["fingerprint"]), "reused": False}
        finally:
            # This exact private staging directory was created by this call.
            # Published and externally redirected paths are never removed.
            if stage.exists():
                _assert_unredirected(stage)
                shutil.rmtree(stage)

    def undo(self, fingerprint):
        """Hide a verified version reversibly; imported copies use normal Ingest Undo."""
        folder = self._version(fingerprint)
        self._verify(folder, expected=fingerprint)
        target = self.root / (fingerprint + ".undone")
        VerifiedTransferEngine._rename_no_replace(folder, target)
        return {"state": "undone", "fingerprint": fingerprint, "store_root": str(self.root)}

    def restore(self, fingerprint):
        folder = self.root / (self._key(fingerprint) + ".undone")
        self._verify(folder, expected=fingerprint)
        VerifiedTransferEngine._rename_no_replace(folder, self._version(fingerprint))
        return self.recover(fingerprint)


@dataclass(frozen=True)
class DerivativeIngestHandoff:
    """Pin one verified saved version across Ingest preview and retry."""

    store_root: Path
    fingerprint: str
    output: Path
    receipt_files: tuple[tuple[str, str], ...]
    receipt_sha256: str

    @classmethod
    def from_verified_result(cls, result):
        if result.get("state") != "verified":
            raise ValueError("Choose a verified saved version before opening Ingest.")
        store = DerivativeStore(result["store_root"])
        fingerprint = store._key(result["fingerprint"])
        result_name = result.get("result_name", "result.png")
        expected = store._version(fingerprint) / result_name
        if Path(result["output"]).absolute() != expected:
            raise ValueError("Saved version output differs from its reviewed receipt.")
        files = result.get("files")
        if not isinstance(files, dict) or not files:
            raise ValueError("Saved version has no reviewed file-family hashes.")
        _, receipt_identity = _read(expected.parent / "receipt.json")
        return cls(store.root, fingerprint, expected, tuple(sorted(files.items())),
                   receipt_identity["sha256"])

    def check_review_status(self):
        """Cheap UI preflight; full receipt and hashes are rechecked in Ingest."""
        sidecar = self.output.with_suffix(".tags.json")
        if dict(self.receipt_files).get(sidecar.name) is not None:
            raw, _ = _read(sidecar)
            if json.loads(raw).get("review_status") != "unreviewed":
                raise ValueError(
                    "This saved version has an earlier review status and cannot return as a new "
                    "unreviewed result. Its protected files are unchanged. Create a new resized "
                    "version from the original."
                )

    def verify(self):
        """Recheck the same receipt and every export member; never adopt new bytes."""
        try:
            current = DerivativeStore(self.store_root).recover(self.fingerprint)
        except (ValueError, OSError, RuntimeError) as exc:
            raise ValueError(
                "This saved result changed after verification. Nothing was imported. "
                "Reload Saved Versions to inspect it."
            ) from exc
        if (Path(current["output"]) != self.output or
                tuple(sorted(current["files"].items())) != self.receipt_files):
            raise ValueError(
                "This saved result changed after verification. Nothing was imported. "
                "Reload Saved Versions to inspect it."
            )
        _, receipt_identity = _read(self.output.parent / "receipt.json")
        if receipt_identity["sha256"] != self.receipt_sha256:
            raise ValueError(
                "This saved result changed after verification. Nothing was imported. "
                "Reload Saved Versions to inspect it."
            )
        self.check_review_status()
        exported = {self.output.name, self.output.with_suffix(".tags.json").name,
                    self.output.with_suffix(".txt").name}
        return tuple((str(self.output.parent / name), digest)
                     for name, digest in self.receipt_files if name in exported)
