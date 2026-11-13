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


# GEN-06A: current production planner/executor refusal and failure-summary component.
@pytest.fixture
def failed_copy_summary(monkeypatch, tmp_path):
    from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QProgressBar, QWidget, QVBoxLayout
    from PySide6.QtGui import QFont, QFontDatabase
    from qt_app import ingest_page as ui
    app = QApplication.instance() or QApplication([])
    for font in ('segoeui.ttf', 'segoeuib.ttf'):
        assert QFontDatabase.addApplicationFont('C:/Windows/Fonts/' + font) >= 0
    app.setFont(QFont('Segoe UI', 10)); app.setStyle('Fusion')
    window = QWidget(); window.setWindowTitle('Changed Manager result - source component')
    layout = QVBoxLayout(window)
    summary = QLabel(); summary.setWordWrap(True); layout.addWidget(summary)
    calls = []
    owner = SimpleNamespace(_batch_completion_context='add_images_saved_apply',
        _batch_task=object(), _batch_paths=['synthetic'],
        state=SimpleNamespace(finish_long_operation=lambda scope:calls.append(scope)),
        batch_actions=QPushButton(), refresh_button=QPushButton(),
        batch_progress=QProgressBar(), cancel_batch_button=QPushButton(),
        batch_summary=summary, _finish_preparation_editor=lambda:None,
        update_selection=lambda:calls.append('selection'),
        _notify_background_work_finished=lambda:calls.append('finished'))
    monkeypatch.setattr(ui.IngestPage, '_refresh_saved_add_batch_state', lambda self:None)
    monkeypatch.setattr(ui.QMessageBox, 'critical', lambda *args,**kwargs:calls.append('error_dialog'))
    def render(message, name='stale'):
        ui.IngestPage._batch_failed(owner, message)
        window.resize(1200, 200); window.show(); app.processEvents()
        assert summary.isVisible() and owner._batch_task is None
        assert calls == ['ingest', 'selection', 'error_dialog', 'finished']
        assert summary.toolTip() == message
        import os
        output = os.environ.get('CG_GEN06A_CAPTURES')
        if output:
            target=Path(output);target.mkdir(parents=True,exist_ok=True)
            assert window.grab().save(str(target/(name+'.png')))
        return summary.text()
    yield render
    window.close(); app.processEvents()


@pytest.mark.parametrize('changed', ['image', 'sidecar'])
def test_changed_after_preview_shows_current_manager_receipt_recovery(tmp_path, failed_copy_summary, changed):
    from core.ingest_add_execution import AddImagesBatchExecutor
    selected, receipt = generated_receipt(tmp_path)
    handoff = GenerationIngestHandoff.from_selected_manager_receipt(selected)
    service = planner(tmp_path, handoff)
    plan = service.preview_files([handoff.output])
    assert plan.ready('copy') and not plan.ready('move')
    family = (selected, handoff.output, handoff.output.with_suffix('.tags.json'))
    before = {p:p.read_bytes() for p in family}
    target = handoff.output if changed == 'image' else handoff.output.with_suffix('.tags.json')
    target.write_bytes(before[target] + b' ')
    with pytest.raises(ValueError) as failure:
        AddImagesBatchExecutor(service.transfers).run_initial(plan, 'copy',
            origin='ordinary_files', boundary_confirmed=True,
            progress=lambda *args:None, should_pause=lambda:False)
    message = failed_copy_summary(str(failure.value), changed)
    assert 'image or sidecar changed after preview' in message
    assert 'Review Manager Result and select the current receipt' in message
    assert not list((tmp_path/'Incoming Images').iterdir())
    assert all(p.read_bytes() == before[p] + (b' ' if p == target else b'') for p in family)


def test_current_manager_result_copies_through_saved_add_executor(tmp_path):
    from core.ingest_add_execution import AddImagesBatchExecutor
    selected, receipt = generated_receipt(tmp_path)
    handoff = GenerationIngestHandoff.from_selected_manager_receipt(selected)
    service = planner(tmp_path, handoff)
    family = (selected, handoff.output, handoff.output.with_suffix('.tags.json'))
    before = {p:p.read_bytes() for p in family}
    plan = service.preview_files([handoff.output])
    result = AddImagesBatchExecutor(service.transfers).run_initial(plan, 'copy',
        origin='ordinary_files', boundary_confirmed=True,
        progress=lambda *args:None, should_pause=lambda:False)
    assert result.status == 'complete' and result.completed == 1
    assert all(p.read_bytes() == before[p] for p in family)
    assert (tmp_path/'Incoming Images'/handoff.output.name).read_bytes() == before[handoff.output]


def test_unrelated_copy_failure_keeps_original_summary(failed_copy_summary):
    assert failed_copy_summary('Destination is unavailable.', 'unrelated') == 'Batch stopped before completion'
