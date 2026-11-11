"""Existing Manager result proof enters the existing Organizer copy planner."""
import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from PIL import Image
from core.generation_execution import GenerationExecutionService, GenerationIngestHandoff, digest
from core.ingest_import import IngestImportPlanningService
from core.edit_service import MoveHistoryService
from core.library_profiles import LibraryProfile, LibraryProfileRegistry, LibraryRole
from core.image_record import ImageRecord


class ImmediateJobs:
    def submit(self, name, worker, **kwargs):
        job = SimpleNamespace(cancel_requested=False, update=lambda *a: None)
        job.result = worker(job)
        return job


class GeneratedAdapter:
    def __init__(self): self.transport = self
    def revalidate(self, prepared):
        return {"session_id": "generated-session"}
    def events(self, payload, **kwargs):
        out = io.BytesIO()
        Image.new("RGB", (payload["width"], payload["height"]), "blue").save(out, format="PNG")
        params = {key: value for key, value in payload.items() if key not in {"session_id", "extra_metadata"}}
        yield {"image": "data:image/png;base64," + base64.b64encode(out.getvalue()).decode(),
            "metadata": json.dumps({"sui_image_params": params,
                "sui_extra_data": payload["extra_metadata"],
                "sui_models": [{"param": "model", "name": params["model"], "hash": "0x" + "b" * 64}]}),
            "batch_index": "0"}
        yield {"discard_indices": []}


def generated_preparation():
    value = {"plan": {"goal": "generated fixture"}, "request": {},
        "payload": {"prompt": "synthetic blue cube", "negativeprompt": "", "model": "fixture.safetensors",
            "width": 64, "height": 64, "seed": 12, "steps": 2, "cfgscale": 1.0,
            "sampler": "euler", "scheduler": "normal"},
        "binding": {"resource_id": "sha256:" + "a" * 64, "tensor_sha256": "b" * 64},
        "capabilities": {"version": "generated-v1"}}
    value["fingerprint"] = digest(value)
    return value


def generated_receipt(root):
    service = GenerationExecutionService(root / "Manager/generation", ImmediateJobs())
    result = service.submit(GeneratedAdapter(), generated_preparation(), confirmed=True).result
    assert result["state"] == "verified"
    return Path(result["outputs"][0]["path"]).with_name("receipt.json"), result


def planner(root, handoff, *, preserve_source=True):
    incoming = root / "Incoming Images"
    incoming.mkdir(exist_ok=True)
    profile = LibraryProfile.create("Generated Library", root, LibraryRole.CLOUD_SYNCED)
    return IngestImportPlanningService(incoming,
        library_registry=LibraryProfileRegistry([profile]),
        transfers=MoveHistoryService(root / "history.json", recover=False),
        preserve_source=preserve_source, verified_versions=(handoff,))


def test_exact_manager_result_is_a_copy_only_pinned_ingest_preview(tmp_path):
    selected, receipt = generated_receipt(tmp_path)
    handoff = GenerationIngestHandoff.from_selected_manager_receipt(selected)
    before = {p: p.read_bytes() for p in [selected, handoff.output, handoff.output.with_suffix(".tags.json")]}
    plan = planner(tmp_path, handoff).preview_files([handoff.output])
    assert plan.ready("copy") and not plan.ready("move")
    assert all(p.read_bytes() == raw for p, raw in before.items())
    record = ImageRecord(handoff.output)
    assert record.annotation.review_status == "needs_review"
    assert not record.annotation.sdxl_tags and not record.annotation.plain_language
    assert record.annotation.source["generation_job"] == receipt["job_id"]


@pytest.mark.parametrize("changed", ["receipt", "image", "sidecar"])
def test_changed_manager_result_cannot_reuse_preview_or_retry(tmp_path, changed):
    selected, receipt = generated_receipt(tmp_path)
    handoff = GenerationIngestHandoff.from_selected_manager_receipt(selected)
    service = planner(tmp_path, handoff)
    assert service.preview_files([handoff.output]).ready("copy")
    target = {"receipt": selected, "image": handoff.output,
        "sidecar": handoff.output.with_suffix(".tags.json")}[changed]
    if changed == "receipt":
        value = json.loads(target.read_bytes())
        value["state"] = "failed"
        target.write_text(json.dumps(value), encoding="utf-8")
    else:
        target.write_bytes(target.read_bytes() + b" ")
    plan = service.preview_files([handoff.output])
    assert plan.errors and not plan.ready("copy")
    assert not list((tmp_path / "Incoming Images").iterdir())


def test_receipt_selection_and_source_preservation_are_explicit(tmp_path):
    selected, receipt = generated_receipt(tmp_path)
    handoff = GenerationIngestHandoff.from_selected_manager_receipt(selected)
    with pytest.raises(ValueError):
        planner(tmp_path, handoff, preserve_source=False)
    with pytest.raises(ValueError):
        GenerationIngestHandoff.from_selected_manager_receipt(selected.with_name("other.json"))
    receipt["state"] = "failed"
    selected.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError):
        GenerationIngestHandoff.from_selected_manager_receipt(selected)


def test_cancelled_preview_keeps_original_result(tmp_path):
    selected, receipt = generated_receipt(tmp_path)
    handoff = GenerationIngestHandoff.from_selected_manager_receipt(selected)
    before = handoff.output.read_bytes()
    with pytest.raises(InterruptedError, match="cancelled before any files changed"):
        planner(tmp_path, handoff).preview_files([handoff.output], should_cancel=lambda: True)
    assert handoff.output.read_bytes() == before
    assert not list((tmp_path / "Incoming Images").iterdir())
