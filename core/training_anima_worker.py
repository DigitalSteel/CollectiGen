"""Fixed Anima trainer entry, owned by TrainingProcessService's native job tree.

No independent process is started here. The process owner verifies all selected
inputs before and after this entry point; this entry also checks its exact
contract and package bytes before training and retains a bounded result claim.
"""
from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import runpy
import stat
import sys


MAX_CONTRACT = 64 * 1024
MAX_MANIFEST = 128 * 1024 * 1024
MAX_ARTIFACT = 512 * 1024 * 1024
MAX_DATASET = 1024 * 1024 * 1024
MAX_OUTPUT = 512 * 1024 * 1024
SCRIPT_SHA = "0886548c19cd9fc80410cd0c625c99f7e63250dfb4624c2b353c7646f704e45d"
OUTPUT_NAME = "anima-lora.safetensors"


class WorkerError(ValueError):
    pass


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
        separators=(",", ":")).encode("utf-8")


def _unredirected(path: Path):
    current = path.absolute()
    while True:
        try:
            state = current.stat(follow_symlinks=False)
        except FileNotFoundError:
            state = None
        if state is not None and (stat.S_ISLNK(state.st_mode)
                or int(getattr(state, "st_file_attributes", 0)) & (0x400 | 0x1000 | 0x40000 | 0x400000)):
            raise WorkerError("anima_worker_redirected_or_offline_path")
        if current.parent == current:
            return
        current = current.parent


def _regular(path: Path, limit: int):
    if not path.is_absolute():
        raise WorkerError("anima_worker_redirected_or_relative_input")
    _unredirected(path)
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or not 0 <= before.st_size <= limit:
        raise WorkerError("anima_worker_input_bound")
    return before


def _read(path: Path, limit: int):
    before = _regular(path, limit)
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
            raise WorkerError("anima_worker_input_changed")
        raw = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
    current = path.stat(follow_symlinks=False)
    if (len(raw) != before.st_size or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
            != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)):
        raise WorkerError("anima_worker_input_changed")
    return raw


def _relative(value):
    if (type(value) is not str or not value or len(value) > 240 or "\\" in value
            or ":" in value or PurePosixPath(value).is_absolute()
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise WorkerError("anima_worker_artifact_path_invalid")
    return value


def _copy_exact(source: Path, destination: Path, expected: dict, remaining: list[int]):
    before = _regular(source, MAX_ARTIFACT)
    _unredirected(destination)
    if before.st_size != expected["size"] or before.st_size > remaining[0]:
        raise WorkerError("anima_worker_artifact_size_changed")
    remaining[0] -= before.st_size
    digest = sha256()
    with source.open("rb") as reader, destination.open("xb") as writer:
        while True:
            chunk = reader.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            writer.write(chunk)
        writer.flush(); os.fsync(writer.fileno())
    readback = sha256()
    with destination.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            readback.update(chunk)
    if (digest.hexdigest() != expected["sha256"] or
            readback.hexdigest() != expected["sha256"]):
        raise WorkerError("anima_worker_artifact_changed")


def _stage_dataset(contract: dict, work: Path):
    package = contract["package"]
    root = Path(package["path"])
    _unredirected(root)
    raw = _read(root / "collectigen-training-manifest.json", MAX_MANIFEST)
    if sha256(raw).hexdigest() != package["manifest_sha256"]:
        raise WorkerError("anima_worker_package_changed")
    manifest = json.loads(raw)
    if manifest.get("package_fingerprint") != package["fingerprint"]:
        raise WorkerError("anima_worker_package_changed")
    items = manifest["package_plan"]["items"]
    artifacts = {row["path"]: row for row in manifest["artifacts"]}
    selected = [row for row in items if row.get("role") == "training"]
    if not 1 <= len(selected) <= 1000:
        raise WorkerError("anima_worker_training_scope_invalid")
    images = work / "dataset" / "images"
    images.mkdir(parents=True)
    remaining = [MAX_DATASET]
    for row in selected:
        image_name, caption_name = _relative(row["image_path"]), _relative(row["caption_path"])
        image, caption = artifacts[image_name], artifacts[caption_name]
        if image["kind"] != "image" or caption["kind"] != "caption":
            raise WorkerError("anima_worker_artifact_role_invalid")
        suffix = PurePosixPath(image_name).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise WorkerError("anima_worker_image_type_invalid")
        basename = row["artifact_id"]
        if type(basename) is not str or not __import__("re").fullmatch(r"[0-9a-f]{24}", basename):
            raise WorkerError("anima_worker_artifact_identity_invalid")
        _copy_exact(root / image_name, images / (basename + suffix), image, remaining)
        _copy_exact(root / caption_name, images / (basename + ".txt"), caption, remaining)
    settings = contract["settings"]
    dataset_toml = ("[[datasets]]\n"
        f"resolution = {settings['resolution']}\n"
        f"batch_size = {settings['batch_size']}\n"
        "[[datasets.subsets]]\n"
        f"image_dir = {json.dumps(str(images))}\n"
        f"num_repeats = {settings['repeats']}\n"
        "caption_extension = \".txt\"\n")
    config = work / "dataset.toml"
    with config.open("xb") as handle:
        handle.write(dataset_toml.encode("utf-8")); handle.flush(); os.fsync(handle.fileno())
    return config


def _arguments(contract: dict, work: Path, dataset: Path):
    resources, settings = contract["resources"], contract["settings"]
    output = work / "output"
    output.mkdir()
    return [
        "--pretrained_model_name_or_path", resources["base"]["path"],
        "--qwen3", resources["qwen"]["path"],
        "--vae", resources["vae"]["path"],
        "--dataset_config", str(dataset),
        "--output_dir", str(output), "--output_name", "anima-lora",
        "--save_model_as", "safetensors", "--network_module", "networks.lora_anima",
        "--network_dim", str(settings["rank"]), "--network_alpha", str(settings["rank"]),
        "--learning_rate", str(settings["learning_rate"]),
        "--optimizer_type", "AdamW", "--lr_scheduler", "constant",
        "--max_train_steps", str(settings["max_steps"]),
        "--train_batch_size", str(settings["batch_size"]),
        "--gradient_accumulation_steps", str(settings["gradient_accumulation"]),
        "--mixed_precision", "bf16", "--seed", str(settings["seed"]),
        "--network_train_unet_only", "--cache_text_encoder_outputs",
        "--gradient_checkpointing", "--timestep_sampling", "sigmoid",
        "--discrete_flow_shift", "1.0", "--qwen_image_vae_2d",
    ]


def execute(contract_path, *, work=None, trainer_runner=None):
    path = Path(contract_path).absolute()
    raw = _read(path, MAX_CONTRACT)
    try:
        contract = json.loads(raw)
    except (ValueError, UnicodeError):
        raise WorkerError("anima_worker_contract_invalid") from None
    if (type(contract) is not dict or _canonical(contract) != raw
            or contract.get("schema_version") != 1
            or contract.get("kind") not in {"collectigen_anima_dit_lora_trial", "collectigen_anima_dit_lora"}
            or contract.get("source_revision") != "6721028c79ee85a78b3a06dfd8954dae310a1cce"
            or contract.get("output_name") != OUTPUT_NAME):
        raise WorkerError("anima_worker_contract_invalid")
    work = Path.cwd() if work is None else Path(work).absolute()
    _unredirected(work)
    if not work.is_dir() or work.name != "work":
        raise WorkerError("anima_worker_workspace_invalid")
    source = Path(contract["source"])
    script = source / "anima_train_network.py"
    if sha256(_read(script, 8 * 1024 * 1024)).hexdigest() != SCRIPT_SHA:
        raise WorkerError("anima_worker_source_changed")
    dataset = _stage_dataset(contract, work)
    args = _arguments(contract, work, dataset)
    if trainer_runner is None:
        site = Path(contract["site_packages"])
        _unredirected(site)
        if not site.is_absolute() or not site.is_dir():
            raise WorkerError("anima_worker_environment_unavailable")
        sys.path[:0] = [str(source), str(site)]
        sys.argv = [str(script), *args]
        runpy.run_path(str(script), run_name="__main__")
    else:
        trainer_runner(script, tuple(args))
    output = work / "output" / OUTPUT_NAME
    before = _regular(output, MAX_OUTPUT)
    if before.st_size < 16:
        raise WorkerError("anima_worker_output_unavailable")
    digest = sha256()
    with output.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    if output.stat(follow_symlinks=False).st_size != before.st_size:
        raise WorkerError("anima_worker_output_changed")
    first_hash = digest.hexdigest()
    reread = sha256()
    with output.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            reread.update(chunk)
    if reread.hexdigest() != first_hash or output.stat(follow_symlinks=False).st_size != before.st_size:
        raise WorkerError("anima_worker_output_readback_changed")
    receipt = {"schema_version": 1, "kind": "anima_worker_result",
        "contract_sha256": sha256(raw).hexdigest(), "output_name": OUTPUT_NAME,
        "output_id": "sha256:" + first_hash, "output_bytes": before.st_size,
        "max_steps": contract["settings"]["max_steps"]}
    target = work / "anima-worker-result.json"
    with target.open("xb") as handle:
        handle.write(_canonical(receipt)); handle.flush(); os.fsync(handle.fileno())
    if _read(target, 16 * 1024) != _canonical(receipt):
        raise WorkerError("anima_worker_result_readback_failed")
    return receipt


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(2)
    try:
        execute(sys.argv[1])
    except (OSError, ValueError, KeyError, TypeError):
        # Only fixed codes reach the bounded parent process log.
        raise SystemExit(1) from None
