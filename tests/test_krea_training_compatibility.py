"""Generated-only fixtures shared by the adapter's core and Qt tests."""
from dataclasses import replace
from pathlib import Path
import json

from PIL import Image

from core import CandidateOverride, ImageRecord, LibraryImageIdentity, TagDatabase, TrainingCandidateSelectionService, TrainingGoal, TrainingPermissionDisposition
from core.model_resource_registry import ModelResourceRegistry, inspect_model_resource
from core.training_package import TrainingPackageService
from core.export_profile import ExportProfile


def adapter_package(tmp_path, *, count=2, update_config=None, roles=(), image_specs=(), image_payloads=(), caption_tags=()):
    root=tmp_path/"synthetic-library"; root.mkdir()
    base=tmp_path/"synthetic-base.safetensors"
    header=json.dumps({"w":{"dtype":"F32","shape":[1],"data_offsets":[0,4]}}).encode()
    base.write_bytes(len(header).to_bytes(8,"little")+header+b"1234")
    registry=ModelResourceRegistry(tmp_path/"private"/"models.json")
    base_id=registry.register(inspect_model_resource(base),role="checkpoint",family="Krea 2",expected_registry=registry.snapshot()[1])
    images=[]; identities={}
    for index in range(count):
        mode, size, format_name = image_specs[index] if image_specs else ("RGB", (768+index,896), "PNG")
        path=root/f"image-{index}.{format_name.lower()}"
        if image_payloads:
            path.write_bytes(image_payloads[index])
        else:
            Image.new(mode,size,"navy" if mode in {"RGB", "RGBA"} else 128).save(path, format=format_name)
        record=ImageRecord(path); record.annotation.sdxl_tags=list(caption_tags[index]) if caption_tags else ["hero",f"pose_{index}"]
        record.modified=True; record.save_sidecar(force=True)
        images.append(path); identities[path]=LibraryImageIdentity("synthetic",path.name)
    selection=TrainingCandidateSelectionService(TagDatabase(tmp_path/"tags.json"))
    profile=ExportProfile(target_model_family=(update_config or {}).get("target_model_family", "Krea 2"))
    plan=selection.plan(images,TrainingGoal("Teach hero",kind="character",target_model_family=profile.target_model_family,base_model_id=base_id),identities=identities,profile=profile)
    if roles:
        overrides={candidate.stable_id:CandidateOverride("exclude" if role=="excluded" else "include", "Explicit synthetic role", "training" if role=="excluded" else role)
                   for candidate,role in zip(plan.candidates,roles)}
        plan=selection.apply_overrides(plan,overrides)
    if update_config:
        config=replace(plan.configuration,**update_config,override_reason="Explicit synthetic fixture request.")
        plan=selection.apply_configuration(plan,config)
    plan=selection.apply_permission_disposition(plan,TrainingPermissionDisposition("proceed","Synthetic fixture only.",True))
    service=TrainingPackageService()
    proposed=service.preview(plan,tmp_path/"package")
    assert proposed.ready, proposed.issues
    result=service.apply(proposed,proposed.decision.execution())
    assert result.status=="created", result.issues
    return result,registry,base


from copy import deepcopy
from dataclasses import FrozenInstanceError
import socket
import pytest

from core.krea_training_compatibility import (
    KreaTrainingCompatibilityService, KREA_SOURCE_REVISION, TRAINING_SOURCE_REVISION, _settings,
)
from core.training_configuration import TrainingConfiguration
from core.training_package import MANIFEST_NAME


def test_construction_is_inert(monkeypatch):
    def forbidden(*a, **k):
        pytest.fail("Construction must not read, write, verify or connect")
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(TrainingPackageService, "verify", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    KreaTrainingCompatibilityService()


def test_report_is_exact_deterministic_read_only_and_permanently_non_executable(tmp_path, monkeypatch):
    result, registry, base = adapter_package(tmp_path, count=4,
        roles=("training", "validation", "holdout", "excluded"),
        update_config={"repeats": 3, "caption_prefix_policy": "trigger", "trigger_term": "synthetic"})
    # Generated-only fixture inventory, never an application library.
    original = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("No network"))
    service = KreaTrainingCompatibilityService()
    report = service.report(result.verification, registry)
    assert service.report(result.verification, registry) == report
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == original
    assert not report.execution_ready
    assert report.base_status == "identity_verified"
    assert report.base_resource_id == result.verification.manifest["training_configuration"]["target"]["base_model_id"]
    assert json.loads(report.manifest_json) == result.verification.manifest
    assert report.krea_source_revision == KREA_SOURCE_REVISION
    assert report.training_source_revision == TRAINING_SOURCE_REVISION
    assert {i.role for i in report.images} == {"training", "validation", "holdout"}
    assert len(report.excluded_ids) == 1
    assert all(i.caption.count("synthetic") == 1 for i in report.images)
    for image in report.images:
        assert (result.destination / image.caption_path).read_text(encoding="utf8").strip() == image.caption
    required = {r.code: r for r in report.requirements}
    assert set(required) >= {"caption_staging", "roles_and_weights", "variant_and_companions",
                            "strict_resume", "output_custody", "external_effects", "runtime_and_fit"}
    assert "TXT sidecars" in required["caption_staging"].reason
    raw = result.verification.manifest["training_configuration"]
    expected = {f"{s}.{k}" if k else s for s, v in raw.items()
                for k in (v if isinstance(v, dict) else ("",))}
    assert {s.field for s in report.settings} == expected
    assert all(s.requested == json.dumps(raw[s.field.split(".")[0]][s.field.split(".")[1]]
        if "." in s.field else raw[s.field], ensure_ascii=False, sort_keys=True,
        allow_nan=False, separators=(",", ":")) for s in report.settings)
    assert not any(hasattr(report, key) for key in ("command", "configuration_toml", "dataset_json", "output_path"))
    with pytest.raises(FrozenInstanceError):
        report.base_status = "ready"
    with pytest.raises((AttributeError, TypeError)):
        report.execution_ready = True


@pytest.mark.parametrize("kind", ["caption", "image", "inventory", "fingerprint", "validity"])
def test_stale_and_fabricated_evidence_refused(tmp_path, kind):
    result, registry, _ = adapter_package(tmp_path)
    expected = result.verification
    item = expected.manifest["package_plan"]["items"][0]
    if kind in {"caption", "image"}:
        path = result.destination / item[kind + "_path"]
        path.write_bytes(path.read_bytes() + b"changed")
    elif kind == "inventory":
        manifest = deepcopy(expected.manifest)
        manifest["training_configuration"]["target"]["model_version"] = "forged"
        expected = replace(expected, manifest=manifest)
    elif kind == "fingerprint":
        expected = replace(expected, package_fingerprint="0" * 64)
    else:
        expected = replace(expected, valid=False)
    with pytest.raises(ValueError):
        KreaTrainingCompatibilityService().report(expected, registry)


def test_missing_exact_base_remains_unavailable_with_all_other_gates(tmp_path):
    result, registry, base = adapter_package(tmp_path)
    base.unlink()
    report = KreaTrainingCompatibilityService().report(result.verification, registry)
    assert report.base_status == "unavailable" and not report.execution_ready
    assert "variant_and_companions" in {r.code for r in report.requirements}


def test_wrong_family_refused_before_model_read(tmp_path, monkeypatch):
    result, registry, _ = adapter_package(tmp_path, update_config={"target_model_family": "SDXL"})
    monkeypatch.setattr(registry, "resolve", lambda *a, **k: pytest.fail("No model read for other family"))
    with pytest.raises(ValueError, match="Krea 2"):
        KreaTrainingCompatibilityService().report(result.verification, registry)


@pytest.mark.parametrize("moment", ["initial", "verifying", "after_resource"])
def test_cancellation_and_mid_report_changes_are_not_returned(tmp_path, monkeypatch, moment):
    result, registry, _ = adapter_package(tmp_path)
    cancel = [moment == "initial"]
    resolve = registry.resolve
    def changed(*a, **k):
        value = resolve(*a, **k)
        path = result.destination / result.verification.manifest["package_plan"]["items"][0]["caption_path"]
        path.write_bytes(path.read_bytes() + b"changed during report")
        return value
    if moment == "after_resource":
        monkeypatch.setattr(registry, "resolve", changed)
    if moment == "verifying":
        def unexpected_verify(*args, **kwargs):
            pytest.fail("Cancellation at the progress callback must skip verification")
        monkeypatch.setattr(TrainingPackageService, "verify", unexpected_verify)
    def progress(*args):
        if moment == "verifying":
            cancel[0] = True
    with pytest.raises(ValueError if moment == "after_resource" else InterruptedError):
        KreaTrainingCompatibilityService().report(result.verification, registry,
            should_cancel=lambda: cancel[0], progress=progress)


def test_future_setting_evidence_is_retained_without_interpretation(tmp_path):
    result, _, _ = adapter_package(tmp_path)
    raw = deepcopy(result.verification.manifest["training_configuration"])
    config = TrainingConfiguration.from_dict(raw)
    raw["future"] = {"opaque": {"unrecognized": [1, "exact"]}}
    raw["captions"]["future_policy"] = "leave unchanged"
    settings = {s.field: s for s in _settings(raw, config, None)}
    assert settings["future.opaque"].disposition == "unresolved"
    assert json.loads(settings["future.opaque"].requested) == raw["future"]["opaque"]
    assert settings["captions.future_policy"].disposition == "unresolved"


def test_current_package_verifier_remains_authority_for_unknown_schema(tmp_path):
    result, registry, _ = adapter_package(tmp_path)
    # The current canonical parser rejects unknown root fields. Do not loosen it
    # to manufacture a forward-compatibility claim for this report.
    path = result.destination / MANIFEST_NAME
    manifest = deepcopy(result.verification.manifest)
    manifest["future_extension"] = {"opaque": "preserve or refuse"}
    path.write_text(json.dumps(manifest), encoding="utf8")
    with pytest.raises(ValueError):
        KreaTrainingCompatibilityService().report(result.verification, registry)


@pytest.mark.parametrize("change", ["changed", "disappeared"])
def test_changed_base_during_report_is_refused(tmp_path, monkeypatch, change):
    result, registry, base = adapter_package(tmp_path)
    resolve = registry.resolve
    def changed(*a, **k):
        value = resolve(*a, **k)
        if change == "disappeared":
            base.unlink()
        else:
            base.write_bytes(base.read_bytes() + b"changed")
        return value
    monkeypatch.setattr(registry, "resolve", changed)
    with pytest.raises(ValueError, match=f"base model {change}"):
        KreaTrainingCompatibilityService().report(result.verification, registry)
