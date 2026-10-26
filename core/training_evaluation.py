"""Fixed comparison specifications carried by canonical generation plans.

This module owns no job queue, model catalog, output gallery or image facts.
Saved specifications use GenerationPlan's revision/recovery transaction. Every
cell remains a separately reviewed ordinary generation request.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import re
from uuid import uuid4

from .annotation import GenerationRecord, GenerationResource
from .generation_planning import GenerationPlan

KEY = "training_evaluation"
CELL_KEY = "training_evaluation_cell"
MAX_CELLS = 64


def template_summary(plan):
    """Human-readable fixed settings, validated before the user freezes them."""
    record = _template(plan)
    resources = "; ".join(f"{r.role}: {r.name or r.id}" for r in record.resources)
    return (f"{resources}\n{record.width} × {record.height} · {record.steps} steps · "
            f"{record.sampler} / {record.scheduler} · Guidance {record.cfg_scale:g}")


def prepare_missing_batch(session, service, storage_root, *, batch=0, should_cancel=None):
    """Retain/reopen one deterministic, reviewed Generation group; never submit.

    Stable batch paths let a save whose acknowledgement was lost reopen safely.
    Existing pending/uncertain candidates are never silently replaced or retried.
    """
    from pathlib import Path
    from .generation_experiments import MAX_CANDIDATES, load_experiment, save_experiment, validate_experiment
    cells = evaluation_cells(session)
    if type(batch) is not int or not 0 <= batch < (len(cells) + MAX_CANDIDATES - 1) // MAX_CANDIDATES:
        raise ValueError("Choose an available comparison batch.")
    selected = cells[batch * MAX_CANDIDATES:(batch + 1) * MAX_CANDIDATES]
    spec = session.target_context[KEY]
    path = Path(storage_root) / "experiments" / f"training-evaluation-{spec['id']}-{batch + 1}.json"
    if should_cancel and should_cancel():
        raise InterruptedError("Comparison queue preparation cancelled.")
    if path.exists():
        group, digest = load_experiment(path)
        expected = {cell.key: cell for cell in selected}
        seen = set()
        for candidate in group['candidates']:
            plan = GenerationPlan.from_dict(candidate['plan'])
            binding = plan.target_context.get(CELL_KEY, {})
            key = binding.get('cell')
            if (key not in expected or key in seen or binding.get('session_id') != spec['id']
                    or binding.get('run_id', '') != spec.get('run_id', '')
                    or comparison_identity(plan) != expected[key].comparison_sha256):
                raise ValueError("The retained comparison queue does not match this evaluation. It was preserved.")
            seen.add(key)
        return group, path, digest
    results = matched_results(session, service, should_cancel=should_cancel)
    # A prior ordinary submission, including an uncertain or failed one, needs
    # explicit review in Generate before another job is created for that cell.
    missing = [cell for cell in selected if not results.get(cell.key)]
    if not missing:
        raise ValueError("No unsubmitted samples in this batch. Refresh results or review existing jobs in Generate.")
    group_id = uuid4().hex
    candidates = []
    for index, cell in enumerate(missing):
        label = f"{cell.label} · Seed {cell.seed} · {cell.key}"[:120]
        context = cell.plan.target_context
        context['experiment'] = {'id': group_id, 'candidate': index + 1, 'label': label}
        plan = cell.plan.revise({'target_context': context}, source='training_evaluation',
            consequence='Prepare a fixed comparison sample for explicit Generation queue review')
        candidates.append({'id': uuid4().hex, 'label': label, 'plan': plan.to_dict(),
            'state': 'pending', 'job_id': '', 'favorite': False, 'error': ''})
    group = validate_experiment({'schema_version': 1, 'id': group_id,
        'name': f"Training comparison · Batch {batch + 1}", 'draft_first': False, 'candidates': candidates})
    if should_cancel and should_cancel():
        raise InterruptedError("Comparison queue preparation cancelled.")
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = save_experiment(path, group)
    return group, path, digest


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def _resource(resource, role):
    if not isinstance(resource, GenerationResource) or resource.role != role:
        raise ValueError(f"Choose an exact registered {role} resource.")
    sha = resource.hashes.get("sha256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", sha) or resource.id != "sha256:" + sha:
        raise ValueError("Evaluation requires an exact full-file model identity.")
    return resource.to_dict()


def _template(plan):
    if not isinstance(plan, GenerationPlan) or plan.source_identity:
        raise ValueError("Use an image generation plan without source-image conditioning.")
    record = plan.request
    if len(record.stages) > 1 or any(s.inputs or s.kind != "first_pass" for s in record.stages):
        raise ValueError("Evaluation currently uses one unconditioned image generation pass.")
    if any(r.role not in {"checkpoint", "vae", "lora"} for r in record.resources):
        raise ValueError("Evaluation currently supports checkpoint, VAE and LoRA resources.")
    bases = [r for r in record.resources if r.role == "checkpoint"]
    if len(bases) != 1:
        raise ValueError("Choose one exact base model in Generate before creating an evaluation.")
    for resource in record.resources:
        _resource(resource, resource.role)
    for name in ("width", "height", "steps"):
        value = getattr(record, name)
        if type(value) is not int or value <= 0:
            raise ValueError("Set image size and generation steps in the template first.")
    if not record.sampler or not record.scheduler or not isinstance(record.cfg_scale, (int, float)) or not math.isfinite(record.cfg_scale):
        raise ValueError("Choose explicit sampler, scheduler and guidance settings in Generate first.")
    if plan.target_context.get("output_goal") or plan.target_context.get("prompt_templates"):
        raise ValueError("Use fixed prompts and the native output size for a comparable evaluation.")
    return record


def create_evaluation(template, *, prompts, seeds, loras=(), strengths=(1.0,), run_id=""):
    """Freeze a baseline and bounded LoRA variants; do not submit any work."""
    _template(template)
    if not isinstance(prompts, (tuple, list)) or not 1 <= len(prompts) <= 8:
        raise ValueError("Choose between one and eight evaluation prompts.")
    if any(type(p) is not str or not p.strip() or len(p) > 4000 for p in prompts) or len(set(prompts)) != len(prompts):
        raise ValueError("Evaluation prompts must be distinct, nonempty, bounded text.")
    if not isinstance(seeds, (tuple, list)) or not 1 <= len(seeds) <= 8 or any(type(s) is not int or not 0 <= s < 2**63 for s in seeds) or len(set(seeds)) != len(seeds):
        raise ValueError("Choose one to eight distinct nonnegative 63-bit seeds.")
    if not isinstance(loras, (tuple, list)) or len(loras) > 8:
        raise ValueError("Choose at most eight registered LoRAs.")
    if not isinstance(strengths, (tuple, list)) or not 1 <= len(strengths) <= 8 or any(type(s) not in (int, float) or not math.isfinite(s) or not 0 < s <= 2 for s in strengths) or len(set(strengths)) != len(strengths):
        raise ValueError("Choose distinct LoRA strengths greater than zero and at most two.")
    resources = [_resource(r, "lora") for r in loras]
    if len({r["id"] for r in resources}) != len(resources):
        raise ValueError("Choose each LoRA only once.")
    variants = [{"label": "Base model", "resource": None, "strength": 0.0}]
    variants += [{"label": f"{r['name'] or r['id']} · {s:g}", "resource": r,
                  "strength": float(s)} for r in resources for s in strengths]
    if len(prompts) * len(seeds) * len(variants) > MAX_CELLS:
        raise ValueError("This comparison exceeds 64 images. Reduce prompts, seeds or variants.")
    if run_id and not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("The source training run identity is invalid.")
    spec = {"schema_version": 1, "id": uuid4().hex, "run_id": run_id,
            "prompts": list(prompts), "seeds": list(seeds), "variants": variants,
            "reviews": {}}
    context = template.target_context
    context.pop(KEY, None); context.pop(CELL_KEY, None)
    context[KEY] = spec
    return template.fork().revise({"target_context": context}, source="training_evaluation",
        consequence="Freeze explicit comparison prompts, seeds and model variants; no jobs started")


@dataclass(frozen=True)
class EvaluationCell:
    key: str
    label: str
    prompt: str
    seed: int
    plan: GenerationPlan
    comparison_sha256: str


def comparison_identity(plan):
    """Compare settings, including exact resources; ignore request record IDs."""
    record = deepcopy(plan.request.to_dict())
    record.pop("id", None)
    record.pop("adapter_report", None)
    for stage in record["stages"]:
        stage.pop("id", None)
    return _hash(record)


def evaluation_cells(session):
    record = _template(session)
    spec = session.target_context.get(KEY)
    if not isinstance(spec, dict) or spec.get("schema_version") != 1:
        raise ValueError("This saved generation plan is not a supported training evaluation.")
    if not re.fullmatch(r"[0-9a-f]{32}", str(spec.get("id", ""))):
        raise ValueError("The evaluation identity is invalid.")
    # Revalidate saved values through the creation contract, including bounds.
    variants = spec.get("variants")
    if not isinstance(variants, list) or not variants or variants[0] != {"label": "Base model", "resource": None, "strength": 0.0}:
        raise ValueError("The evaluation baseline is invalid.")
    loras, strengths = {}, []
    for row in variants[1:]:
        if not isinstance(row, dict) or set(row) != {"label", "resource", "strength"}:
            raise ValueError("The evaluation variant is invalid.")
        resource = GenerationResource.from_dict(row["resource"])
        _resource(resource, "lora")
        loras[resource.id] = resource
        if row["strength"] not in strengths: strengths.append(row["strength"])
    expected = create_evaluation(session, prompts=spec.get("prompts"), seeds=spec.get("seeds"),
        loras=list(loras.values()), strengths=strengths or [1.0], run_id=spec.get("run_id", ""))
    if expected.target_context[KEY]["variants"] != variants:
        raise ValueError("The saved evaluation combinations changed.")
    if not isinstance(spec.get("reviews"), dict) or len(spec["reviews"]) > MAX_CELLS:
        raise ValueError("The saved evaluation reviews are invalid.")
    cells = []
    for pi, prompt in enumerate(spec["prompts"]):
        for seed in spec["seeds"]:
            for vi, variant in enumerate(variants):
                request = deepcopy(record.to_dict())
                resources = [r for r in request["resources"] if r["role"] != "lora"]
                if variant["resource"] is not None:
                    resource = deepcopy(variant["resource"])
                    resource["weights"] = {"model": variant["strength"], "text_encoder": variant["strength"]}
                    resources.append(resource)
                key = f"p{pi}-s{seed}-v{vi}"
                request.update(positive_prompt=prompt, seed=seed, resources=resources, loras=[])
                for stage in request["stages"]:
                    stage.update(positive_prompt=prompt, seeds=[seed], batch_size=1,
                                 resource_ids=[r["id"] for r in resources])
                plan = session.fork(seed=seed)
                context = plan.target_context
                context.pop(KEY, None)
                context[CELL_KEY] = {"session_id": spec["id"], "cell": key,
                                     "run_id": spec.get("run_id", "")}
                for field in ("raw", "source", "source_field"):
                    request.pop(field, None)
                plan = plan.revise({"request": request, "target_context": context},
                    source="training_evaluation", consequence="Review one fixed evaluation image")
                cells.append(EvaluationCell(key, variant["label"], prompt, seed, plan, comparison_identity(plan)))
    if set(spec["reviews"]) - {c.key for c in cells}:
        raise ValueError("A saved review refers to an unknown evaluation cell.")
    for review in spec["reviews"].values():
        if (not isinstance(review, dict) or set(review) != {"job_id", "output_sha256", "favorite", "notes"}
                or not re.fullmatch(r"gen-[0-9a-f]{32}", str(review["job_id"]))
                or not re.fullmatch(r"[0-9a-f]{64}", str(review["output_sha256"]))
                or type(review["favorite"]) is not bool or type(review["notes"]) is not str
                or len(review["notes"]) > 4000):
            raise ValueError("A saved evaluation observation is invalid.")
    return tuple(cells)


def review_cell(session, key, *, job_id, output_sha256, favorite=False, notes=""):
    """Record a personal comparison note; this never rates image facts/quality."""
    if key not in {c.key for c in evaluation_cells(session)}:
        raise ValueError("Choose an existing evaluation cell.")
    if not re.fullmatch(r"gen-[0-9a-f]{32}", job_id) or not re.fullmatch(r"[0-9a-f]{64}", output_sha256):
        raise ValueError("Bind the review to an exact verified generation result.")
    if type(favorite) is not bool or type(notes) is not str or len(notes) > 4000:
        raise ValueError("Use a bounded personal note and explicit favorite choice.")
    context = session.target_context
    context[KEY]["reviews"][key] = {"job_id": job_id, "output_sha256": output_sha256,
                                     "favorite": favorite, "notes": notes}
    return session.revise({"target_context": context}, source="user_evaluation",
        consequence="Save a personal result observation; model quality remains unmeasured")


def matched_results(session, service, *, should_cancel=None):
    """Resolve canonical verified generation receipts; never adopt loose files."""
    cells = {c.key: c for c in evaluation_cells(session)}
    identity = session.target_context[KEY]["id"]
    matches = {key: [] for key in cells}
    for receipt in service.history():
        if should_cancel and should_cancel(): raise InterruptedError("Evaluation result check cancelled.")
        raw = receipt.get("prepared", {}).get("plan")
        if not isinstance(raw, dict): continue
        marker = raw.get("target_context", {}).get(CELL_KEY, {})
        if marker.get("session_id") != identity or marker.get("cell") not in cells: continue
        cell = cells[marker["cell"]]
        actual = GenerationPlan.from_dict(raw)
        comparable = comparison_identity(actual) == cell.comparison_sha256
        row = {"job_id": receipt["job_id"], "state": receipt["state"],
               "comparable": comparable, "path": "", "sha256": ""}
        if comparable and receipt["state"] == "verified":
            paths = service.verified_output_paths(receipt)
            if len(paths) != 1: raise ValueError("Evaluation expects one verified image per cell.")
            row.update(path=str(paths[0]), sha256=receipt["outputs"][0]["sha256"])
        matches[cell.key].append(row)
    return matches
