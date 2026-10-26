"""Bounded generation alternatives; execution remains in GenerationExecutionService."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
from uuid import uuid4

from .external_write_transaction import atomic_verified_bytes
from .generation_planning import GenerationPlan
from .training_source_io import _assert_unredirected

MAX_CANDIDATES = 32
MAX_BYTES = 24 * 1024 * 1024
AXES = {"seed", "steps", "cfg_scale", "denoising_strength", "positive_prompt", "negative_prompt", "lora_weight"}
STATES = {"pending", "submitting", "verified", "failed", "cancelled", "outcome_unknown"}


def _bytes(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                     separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise ValueError("This experiment exceeds the saved size limit.")
    return raw


def _axis_value(axis, value):
    if axis not in AXES:
        raise ValueError("Choose a supported comparison setting.")
    if axis in {"positive_prompt", "negative_prompt"}:
        if not isinstance(value, str) or len(value) > 16384:
            raise ValueError("Each prompt must be bounded text.")
    elif axis in {"steps", "seed"}:
        if type(value) is not int or not (1 if axis == "steps" else 0) <= value <= (150 if axis == "steps" else 2**63 - 1):
            raise ValueError("Steps must be 1 to 150; seeds must be 0 to 2^63-1 for this local generator.")
    elif axis == "lora_weight":
        if type(value) not in {int, float} or not math.isfinite(value) or not -2 <= value <= 2:
            raise ValueError("LoRA weights must be finite values from -2 to 2.")
    elif type(value) not in {int, float} or not math.isfinite(value) or not (0 < value <= (1 if axis == "denoising_strength" else 30)):
        raise ValueError("Guidance and Remix strength must be finite; guidance is within 0 to 30 and strength within 0 to 1 for this local generator.")
    return value


def grid_shape(axis, values, column_axis=None, column_values=()):
    """Validate a bounded Cartesian preview without creating plans or jobs."""
    if not isinstance(values, (tuple, list, range)) or not isinstance(column_values, (tuple, list, range)):
        raise ValueError("Comparison values must be a bounded list.")
    if len(values) > MAX_CANDIDATES or len(column_values) > MAX_CANDIDATES:
        raise ValueError("A comparison axis may contain at most 32 values.")
    if column_axis and len(values) * len(column_values) > MAX_CANDIDATES:
        raise ValueError("A comparison grid may contain at most 32 candidates.")
    if not axis:
        if column_axis or column_values:
            raise ValueError("Choose the first comparison setting before adding a second.")
        return (), (), 0
    rows = tuple(_axis_value(axis, value) for value in values)
    if not 1 <= len(rows) <= MAX_CANDIDATES or len(set(rows)) != len(rows):
        raise ValueError("Enter between 1 and 32 distinct values for the first setting.")
    if column_axis:
        if column_axis == axis:
            raise ValueError("Choose two different comparison settings for a grid.")
        columns = tuple(_axis_value(column_axis, value) for value in column_values)
        if not 1 <= len(columns) <= MAX_CANDIDATES or len(set(columns)) != len(columns):
            raise ValueError("Enter between 1 and 32 distinct values for the second setting.")
        if len(rows) * len(columns) > MAX_CANDIDATES:
            raise ValueError("A comparison grid may contain at most 32 candidates.")
    else:
        if column_values:
            raise ValueError("Choose a second comparison setting for those values.")
        columns = ()
    return rows, columns, len(rows) * (len(columns) or 1)


def _axis_name(axis):
    return {"cfg_scale": "Guidance", "denoising_strength": "Remix strength",
            "lora_weight": "LoRA weight"}.get(axis, axis.replace("_", " ").title())


def _apply_axis(candidate, base, axis, value, lora_id, label):
    if axis == "seed":
        return candidate
    changes = {axis: value}
    stages = [s.to_dict() for s in candidate.request.stages]
    if axis != "lora_weight":
        for stage in stages:
            if stage.get(axis) not in (None, "") and stage.get(axis) == getattr(base.request, axis):
                stage[axis] = value
        changes["stages"] = stages
    else:
        resources = [r.to_dict() for r in candidate.request.resources]
        for resource in resources:
            if resource["id"] == lora_id and resource["role"] == "lora":
                resource["weights"] = {"model": value, "text_encoder": value}
        changes = {"resources": resources}
    if axis == "denoising_strength":
        for stage in stages:
            for item in stage.get("inputs", []):
                if item.get("kind") == "init_image":
                    item["strength"] = value
        changes["stages"] = stages
    return candidate.revise(changes, source="comparison", consequence=label)


def build_experiment(plan, *, count=4, axis=None, values=(), column_axis=None,
                     column_values=(), draft_first=True, lora_id=None, name=None):
    """Freeze each candidate and seed; never change source facts or authorize work."""
    plan = GenerationPlan.from_dict(plan.to_dict())
    if type(count) is not int or not 1 <= count <= MAX_CANDIDATES:
        raise ValueError("Choose between 1 and 32 alternatives.")
    values, column_values, grid_count = grid_shape(axis, values, column_axis, column_values)
    if axis:
        axes = (axis, column_axis) if column_axis else (axis,)
        if plan.request.seed is None and "seed" not in axes:
            raise ValueError("Choose an exact seed to compare settings fairly.")
        if "denoising_strength" in axes and not any(s.inputs for s in plan.request.stages):
            raise ValueError("Remix strength comparisons need a source image.")
        if "lora_weight" in axes and len([r for r in plan.request.resources if r.role == "lora" and r.id == lora_id]) != 1:
            raise ValueError("Select one exact LoRA to compare its model and text weights together.")
        count = grid_count
    identifier = uuid4().hex
    candidates = []
    used = set()
    for index in range(count):
        row_index = index // len(column_values) if column_values else index
        column_index = index % len(column_values) if column_values else None
        choices = [(axis, values[row_index])] if axis else []
        if column_axis:
            choices.append((column_axis, column_values[column_index]))
        seed = (next((value for key, value in choices if key == "seed"), plan.request.seed)
                if axis else secrets.randbelow(2**63))
        if not axis:
            while seed in used:
                seed = secrets.randbelow(2**63)
        used.add(seed)
        candidate = plan.fork(seed=seed)
        label = (f"Row {row_index + 1}, column {column_index + 1} · " if column_axis else "") + (
            " · ".join(f"{_axis_name(key)}: {value}" for key, value in choices)
            if choices else f"Alternative {index + 1} · seed {seed}")
        for key, value in choices:
            candidate = _apply_axis(candidate, plan, key, value, lora_id, label)
        context = candidate.target_context
        if draft_first:
            finish = {key: context.pop(key) for key in ("output_goal", "output_plan") if key in context}
            context["deferred_finishing"] = finish
        membership = {"id": identifier, "candidate": index + 1, "label": label}
        if column_axis:
            membership.update(row=row_index + 1, column=column_index + 1)
        context["experiment"] = membership
        candidate = candidate.revise({"target_context": context}, source="experiment")
        row = {"id": uuid4().hex, "label": label, "plan": candidate.to_dict(),
               "state": "pending", "job_id": "", "favorite": False, "error": ""}
        if column_axis:
            row["grid_position"] = {"row": row_index + 1, "column": column_index + 1}
        candidates.append(row)
    default_name = ((f"{_axis_name(axis)} × {_axis_name(column_axis)} grid" if column_axis else
                     str(axis).replace("_", " ").title() + " comparison") if axis else "Image alternatives")
    value = {"schema_version": 1, "id": identifier, "name": name.strip() if isinstance(name, str) and name.strip() else default_name,
             "draft_first": bool(draft_first),
             "candidates": candidates}
    if column_axis:
        value["grid"] = {"rows": {"axis": axis, "values": list(values)},
                         "columns": {"axis": column_axis, "values": list(column_values)}}
        if "lora_weight" in (axis, column_axis):
            value["grid"]["lora_id"] = lora_id
    return validate_experiment(value)


def validate_experiment(value):
    _bytes(value)
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("Unsupported experiment version.")
    if not isinstance(value.get("id"), str) or not re.fullmatch(r"[0-9a-f]{32}", value["id"]):
        raise ValueError("Invalid experiment identity.")
    group_name = value.get("name", "Saved alternatives")
    if (not isinstance(group_name, str) or not group_name.strip() or len(group_name) > 120
            or any(ord(char) < 32 for char in group_name)):
        raise ValueError("Give this group a name of 1 to 120 characters.")
    rows = value.get("candidates")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_CANDIDATES:
        raise ValueError("Invalid candidate count.")
    ids = set()
    plan_ids = set()
    grid = value.get("grid")
    positions = set()
    if grid is not None:
        if not isinstance(grid, dict) or not isinstance(grid.get("rows"), dict) or not isinstance(grid.get("columns"), dict):
            raise ValueError("Invalid saved comparison grid.")
        row_axis = grid["rows"].get("axis"); column_axis = grid["columns"].get("axis")
        row_values, column_values, expected_count = grid_shape(
            row_axis, grid["rows"].get("values", ()), column_axis, grid["columns"].get("values", ()))
        if not column_axis or expected_count != len(rows):
            raise ValueError("Saved comparison grid does not match its candidates.")
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or row["id"] in ids:
            raise ValueError("Candidate identities must be unique.")
        ids.add(row["id"])
        plan = GenerationPlan.from_dict(row["plan"])
        membership = plan.target_context.get("experiment", {})
        if (plan.plan_id in plan_ids or membership.get("id") != value["id"]
                or membership.get("label") != row.get("label")):
            raise ValueError("A candidate has duplicate identity or belongs to another saved group.")
        plan_ids.add(plan.plan_id)
        if grid is not None:
            position = row.get("grid_position")
            if not isinstance(position, dict):
                raise ValueError("A grid candidate has no exact position.")
            r, c = position.get("row"), position.get("column")
            if (type(r) is not int or type(c) is not int or not 1 <= r <= len(row_values)
                    or not 1 <= c <= len(column_values) or (r, c) in positions
                    or membership.get("row") != r or membership.get("column") != c):
                raise ValueError("A grid candidate has an invalid or duplicate position.")
            positions.add((r, c))
            expected_label = (f"Row {r}, column {c} · {_axis_name(row_axis)}: {row_values[r - 1]}"
                              f" · {_axis_name(column_axis)}: {column_values[c - 1]}")
            if row["label"] != expected_label:
                raise ValueError("A grid candidate label does not match its saved position.")
            for key, expected in ((row_axis, row_values[r - 1]),
                                  (column_axis, column_values[c - 1])):
                if key == "lora_weight":
                    if not any(resource.role == "lora" and resource.id == grid.get("lora_id")
                               and resource.weights.get("model") == expected
                               and resource.weights.get("text_encoder") == expected
                               for resource in plan.request.resources):
                        raise ValueError("A grid candidate does not match its saved setting values.")
                elif getattr(plan.request, key) != expected:
                    raise ValueError("A grid candidate does not match its saved setting values.")
        if row.get("state") not in STATES or type(row.get("favorite")) is not bool:
            raise ValueError("Invalid candidate state.")
        if any(not isinstance(row.get(key), str) for key in ("label", "job_id", "error")):
            raise ValueError("Invalid candidate details.")
    checked = deepcopy(value)
    checked["name"] = group_name.strip()
    return checked


def save_experiment(path, experiment, *, expected_digest=None):
    path = Path(path).absolute()
    _assert_unredirected(path)
    raw = _bytes(validate_experiment(experiment))
    existing = path.read_bytes() if path.exists() and path.stat().st_size <= MAX_BYTES else None
    if path.exists() and existing is None:
        raise ValueError("Existing experiment is too large.")
    digest = hashlib.sha256(existing).hexdigest() if existing is not None else ""
    if expected_digest is None and existing is not None or expected_digest is not None and digest != expected_digest:
        raise ValueError("The saved experiment changed. Reopen it before saving.")
    if existing is not None:
        atomic_verified_bytes(path.with_suffix(path.suffix + ".previous"), existing, label="Experiment recovery copy")
    atomic_verified_bytes(path, raw, label="Generation experiment")
    return hashlib.sha256(raw).hexdigest()


def load_experiment(path):
    path = Path(path).absolute(); _assert_unredirected(path)
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("Saved experiment is too large.")
    raw = path.read_bytes()
    value = validate_experiment(json.loads(raw))
    # No process restart is permission to resubmit an uncertain GPU job.
    for row in value["candidates"]:
        if row["state"] == "submitting":
            row["state"] = "outcome_unknown"
            row["error"] = "Reconcile retained jobs before resuming. This candidate will not be resubmitted automatically."
    return value, hashlib.sha256(raw).hexdigest()


def reconcile_experiment(experiment, receipts):
    result = validate_experiment(experiment)
    by_plan = {}
    for receipt in receipts:
        plan = receipt.get("prepared", {}).get("plan", {})
        by_plan.setdefault((plan.get("plan_id"), plan.get("digest")), []).append(receipt)
    for row in result["candidates"]:
        matches = by_plan.get((row["plan"]["plan_id"], row["plan"]["digest"]), [])
        if not matches:
            continue
        if len(matches) != 1:
            row.update(state="outcome_unknown", job_id="",
                       error="Multiple retained jobs match this candidate. Review Generation History; automatic retry is unavailable.")
            continue
        receipt = matches[0]
        row["job_id"] = receipt["job_id"]
        state = receipt.get("state")
        row["state"] = ("verified" if state == "verified" else state
            if state in {"failed", "cancelled"} and (receipt.get("proven_unsubmitted") or receipt.get("server_stopped"))
            else "outcome_unknown")
        row["error"] = "" if row["state"] == "verified" else "Review the retained job outcome before retrying."
    return result


def retry_failed(experiment, candidate_ids, *, receipts=()):
    result = validate_experiment(experiment)
    selected = set(candidate_ids)
    proofs = {receipt.get("job_id"): receipt for receipt in receipts}
    for row in result["candidates"]:
        if row["id"] not in selected:
            continue
        if row["state"] not in {"failed", "cancelled"}:
            raise ValueError("Only confirmed failed or cancelled candidates can be retried.")
        proof = proofs.get(row["job_id"], {})
        if (proof.get("state") not in {"failed", "cancelled"}
                or not (proof.get("proven_unsubmitted") or proof.get("server_stopped"))
                or proof.get("prepared", {}).get("plan", {}).get("digest") != row["plan"]["digest"]):
            raise ValueError("Retry requires a matching confirmed failed or cancelled receipt.")
        row["plan"] = GenerationPlan.from_dict(row["plan"]).fork().to_dict()
        row.update(state="pending", job_id="", error="")
    return result
