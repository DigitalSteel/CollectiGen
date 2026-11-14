"""Finite caption protocol tests with synthetic roots and fake stock services.

These tests never install packages, download models or invoke an AI backend.
"""
from dataclasses import dataclass
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import manager_caption_worker as worker


@pytest.fixture
def envelope(tmp_path):
    source = tmp_path / "source"
    runtime = tmp_path / "worker"
    source.mkdir()
    runtime.mkdir()
    executable = runtime / "python.exe"
    executable.write_bytes(b"synthetic interpreter")
    payload = {"schema_version": 1, "operation": "status", "source_root": str(source),
               "worker_root": str(runtime), "profile_root": str(tmp_path / "Descriptions"),
               "interpreter_sha256": sha256(executable.read_bytes()).hexdigest(),
               "python_version": "3.13.15", "settings": {}}
    return payload, dict(source_root=source, executable=executable, python_version="3.13.15")


def test_envelope_is_admitted_without_storage_imports(envelope):
    payload, binding = envelope
    source, runtime, profile = worker.validate_request(payload, **binding)
    assert source == binding["source_root"]
    assert runtime == binding["executable"].parent
    assert not profile.exists()


@pytest.mark.parametrize("change", [{"schema_version": True}, {"schema_version": 2},
                                   {"operation": "remove"}, {"operation": []},
                                   {"extra": "no"}, {"settings": []},
                                   {"interpreter_sha256": "0" * 64},
                                   {"python_version": "3.12.0"},
                                   {"image_path": "unused"}])
def test_reject_invalid_envelopes(envelope, change):
    payload, binding = envelope
    with pytest.raises(ValueError):
        worker.validate_request({**payload, **change}, **binding)


def test_profile_and_interpreter_cannot_escape_binding(envelope):
    payload, binding = envelope
    for profile in (payload["source_root"], payload["worker_root"],
                    str(Path(payload["source_root"]) / "Descriptions")):
        with pytest.raises(ValueError, match="separate"):
            worker.validate_request({**payload, "profile_root": profile}, **binding)
    with pytest.raises(ValueError, match="interpreter"):
        worker.validate_request(payload, **{**binding, "executable": binding["source_root"]})


def test_settings_exact_allowlist_uses_canonical_validator():
    seen = []
    def validator(values):
        seen.append(values.copy())
        if values["plain_language_model"] != "reviewed":
            raise ValueError("canonical validation refused")
    settings = worker.validate_settings({"plain_language_model": "reviewed"},
                                        {"plain_language_model"}, validator, SimpleNamespace)
    assert settings.plain_language_model == "reviewed" and len(seen) == 1
    for values in ({}, {"plain_language_model": "reviewed", "exiftool_path": "bad"}):
        with pytest.raises(ValueError, match="approved"):
            worker.validate_settings(values, {"plain_language_model"}, validator, SimpleNamespace)
    with pytest.raises(ValueError, match="canonical"):
        worker.validate_settings({"plain_language_model": "bad"}, {"plain_language_model"}, validator, SimpleNamespace)


def test_reviewed_wheel_is_bound_to_worker_and_pin(envelope):
    payload, binding = envelope
    source = binding["source_root"]
    runtime = binding["executable"].parent
    (source / "tools").mkdir()
    wheel = runtime / "pip-reviewed.whl"
    wheel.write_bytes(b"synthetic reviewed wheel")
    pins = {"pip_wheel": {"filename": wheel.name, "sha256": sha256(wheel.read_bytes()).hexdigest()}}
    pin_path = source / "tools/runtime-pins.json"
    pin_path.write_text(json.dumps(pins))
    command = worker.reviewed_pip_command(source, runtime, binding["executable"], "3.13.15")
    assert command[:4] == [str(binding["executable"]), "-I", "-B", "-c"]
    assert "runpy.run_module('pip'" in command[4]
    assert command[-2:] == ["3.13.15", str(wheel)]
    wheel.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="changed"):
        worker.reviewed_pip_command(source, runtime, binding["executable"], "3.13.15")
    pins["pip_wheel"]["filename"] = "../outside.whl"
    pin_path.write_text(json.dumps(pins))
    with pytest.raises(ValueError, match="invalid"):
        worker.reviewed_pip_command(source, runtime, binding["executable"], "3.13.15")


def test_bootstrap_subclass_delegates_exact_stock_packages_and_command():
    calls = []
    class Stock:
        @classmethod
        def _acquire_optional_packages(cls, packages, site, stage, *, pip_command=None):
            calls.append((packages, site, stage, pip_command))
    command = ["reviewed-python", "-I", "stock-bootstrap"]
    bound = worker.bound_dependency_service(Stock, command, lambda: False)
    packages = [{"name": "original", "sha256": "original-pin"}]
    bound._acquire_optional_packages(packages, "site", "stage")
    assert calls == [(packages, "site", "stage", command)]
    with pytest.raises(ValueError):
        bound._acquire_optional_packages(packages, "site", "stage", pip_command=["other"])
    with pytest.raises(InterruptedError):
        worker.bound_dependency_service(Stock, command, lambda: True)._acquire_optional_packages(packages, "site", "stage")
    assert len(calls) == 1


@dataclass
class Component:
    name: str
    installed: bool


class StockService:
    JOY_BASE_PACKAGES = {"PyTorch", "Transformers"}
    JOY_HARDWARE_COMPONENT = "NVIDIA GPU"
    def __init__(self):
        self.calls = []
    def capability_ready(self, capability):
        self.calls.append(("ready", capability))
        return False
    def plain_language_component_name(self, model):
        return "Selected model"
    def plain_language_model_dir_for(self, model):
        return Path("synthetic-models") / "selected"
    def components(self, *, include_sizes):
        assert include_sizes is False
        return [Component("NVIDIA GPU", True), Component("PyTorch", False), Component("Selected model", False),
                Component("Pillow", True), Component("Unrelated Video", False)]
    def component(self,name):
        return next((row for row in self.components(include_sizes=False) if row.name==name),None)
    def optional_profile_runtime_issue(self):
        return ""
    def install_profile(self, capability, *, progress):
        self.calls.append(("install", capability, progress))
        return "stock installed"
    def setup_joycaption(self, **kwargs):
        self.calls.append(("download", kwargs))
        return "stock downloaded"


def call_dispatch(operation, service=None, suggest=None, cancelled=lambda: False):
    service = service or StockService()
    settings = SimpleNamespace(plain_language_model="reviewed", joycaption_quantization="bf16")
    payload = {"operation": operation, "image_path": "synthetic.png", "selected_kinds": ["character", "style"]}
    value = worker.dispatch(payload, settings, service, suggest,
                            model_catalog={"reviewed": ("Reviewed model",)},
                            cancelled=cancelled, progress="progress")
    return value, service


def test_status_uses_stock_capability_and_only_caption_rows():
    value, service = call_dispatch("status")
    assert service.calls == [("ready", "joy")]
    assert value["ready"] is False
    assert {row["name"] for row in value["components"]} == {"NVIDIA GPU", "PyTorch", "Selected model", "Pillow"}
    assert value["missing"] == ["PyTorch", "Selected model"]
    assert value["model_display"] == "Reviewed model"


def test_mutation_dispatch_only_original_service_operations():
    value, service = call_dispatch("install-runtime")
    assert value["message"] == "stock installed"
    assert service.calls == [("install", "joy", "progress")]
    value, service = call_dispatch("download-model")
    assert service.calls == [("download", {"download_model": True, "model_id": "reviewed", "progress": "progress"})]
    with pytest.raises(ValueError, match="Unsupported"):
        call_dispatch("remove")


def test_suggest_invokes_whole_service_preserving_owned_purpose_prompts():
    @dataclass
    class Result:
        proposals: tuple
        notes: tuple
    seen = []
    def whole_service(image, settings, *, selected_kinds, cancelled):
        # No backend keyword: canonical service owns selection and purpose prompts.
        seen.append((image, selected_kinds, settings.plain_language_model, cancelled()))
        return Result(("canonical proposal",), ("canonical uncertainty",))
    value, service = call_dispatch("suggest", suggest=whole_service)
    assert seen == [("synthetic.png", ["character", "style"], "reviewed", False)]
    assert value == {"proposals": ("canonical proposal",), "notes": ("canonical uncertainty",)}
    assert service.calls == []


def test_cancellation_prevents_and_refuses_completed_publication():
    with pytest.raises(InterruptedError):
        call_dispatch("install-runtime", cancelled=lambda: True)
    state = [False]
    service = StockService()
    def install(capability, *, progress):
        state[0] = True
        return "completed just before cancellation"
    service.install_profile = install
    with pytest.raises(InterruptedError):
        call_dispatch("install-runtime", service=service, cancelled=lambda: state[0])


def test_bounded_result_and_atomic_no_replace(tmp_path):
    path = tmp_path / "result.json"
    response = {"schema_version": 1, "ok": True, "value": {"ready": False}}
    worker.publish_result(path, response)
    assert json.loads(path.read_bytes()) == response
    with pytest.raises(FileExistsError):
        worker.publish_result(path, {"ok": False})
    assert json.loads(path.read_bytes()) == response
    with pytest.raises(ValueError, match="size limit"):
        worker.encode_result({"value": "x" * worker.MAX_RESULT_BYTES})
    with pytest.raises(ValueError):
        worker._json(b'{"operation":"status","operation":"suggest"}')


def test_progress_is_prefixed_bounded_and_cancellable():
    stream = StringIO()
    worker.progress_writer(lambda: False, stream)(1, 3, "\u2603" * 10000)
    raw = stream.getvalue()
    assert raw.startswith(worker.PROGRESS_PREFIX) and len(raw.encode("utf-8")) <= 4096
    assert json.loads(raw[len(worker.PROGRESS_PREFIX):])["total"] == 3
    with pytest.raises(InterruptedError):
        worker.progress_writer(lambda: True, stream)(0, 0, "stock message")


@pytest.mark.parametrize("operation", ["install-runtime", "download-model"])
def test_original_hardware_guard_precedes_caption_mutation(operation):
    service=StockService();service.component=lambda _name:Component("NVIDIA GPU",False)
    with pytest.raises(RuntimeError,match="Nothing was installed"):
        call_dispatch(operation,service=service)
    assert service.calls==[]
