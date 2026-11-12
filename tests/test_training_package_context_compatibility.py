"""Current/legacy Training package context round trips with synthetic images."""
from copy import deepcopy
import json
import pytest
from PIL import Image
from core import (ImageRecord, LibraryImageIdentity, TagDatabase,
    TrainingCandidateSelectionService, TrainingGoal, TrainingPermissionDisposition)
from core.training_package import TrainingPackageService, MANIFEST_NAME, _stable_hash, _json_bytes


@pytest.mark.parametrize("context", ["current", "legacy", "invalid"])
def test_package_context_round_trip_and_legacy_compatibility(tmp_path, monkeypatch, context):
    if context == "legacy":
        # Replay the historical serializer shape through the whole stock producer,
        # so control files and lineage carry the same original plan fingerprint.
        from core.training_candidate_selection import TrainingCandidatePlan
        current_manifest = TrainingCandidatePlan.to_manifest_dict
        def legacy_manifest(self):
            payload = current_manifest(self)
            payload.pop("assessment_context_fingerprint", None)
            payload.pop("plan_fingerprint")
            return {**payload, "plan_fingerprint": _stable_hash(payload)}
        monkeypatch.setattr(TrainingCandidatePlan, "to_manifest_dict", legacy_manifest)
    image = tmp_path / "synthetic-library" / "hero.png"
    image.parent.mkdir()
    Image.new("RGB", (768, 896), (30, 60, 90)).save(image)
    record = ImageRecord(image)
    record.annotation.sdxl_tags = ["hero", "solo"]
    record.modified = True
    record.save_sidecar(force=True)
    original = image.read_bytes()
    knowledge = TagDatabase(tmp_path / "tags.json")
    selection = TrainingCandidateSelectionService(knowledge)
    plan = selection.plan((image,), TrainingGoal("Teach hero", kind="character",
        base_model_id="sha256:" + "a" * 64),
        identities={image: LibraryImageIdentity("synthetic", image.name)})
    plan = selection.apply_permission_disposition(plan,
        TrainingPermissionDisposition("proceed", "Reviewed synthetic fixture.", True))
    packages = TrainingPackageService(knowledge)
    parent = tmp_path / "packages"
    parent.mkdir()
    destination = parent / "frozen"
    preview = packages.preview(plan, destination)
    assert preview.ready, preview.issues
    created = packages.apply(preview, preview.decision.execution())
    assert created.status == "created", created.issues
    manifest_path = destination / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_bytes())
    if context == "legacy":
        assert "assessment_context_fingerprint" not in manifest["candidate_plan"]
    else:
        assert manifest["candidate_plan"]["assessment_context_fingerprint"] == plan.assessment_context_fingerprint
    if context == "invalid":
        manifest = deepcopy(manifest)
        candidate = manifest["candidate_plan"]
        candidate["assessment_context_fingerprint"] = "not-a-sha256"
        candidate.pop("plan_fingerprint")
        candidate["plan_fingerprint"] = _stable_hash(candidate)
        manifest["package_plan"]["candidate_plan_fingerprint"] = candidate["plan_fingerprint"]
        manifest["intent_fingerprint"] = _stable_hash(manifest["package_plan"])
        manifest.pop("package_fingerprint")
        manifest["package_fingerprint"] = _stable_hash(manifest)
        manifest_path.write_bytes(_json_bytes(manifest))
    verified = packages.verify(destination)
    if context == "invalid":
        assert not verified.valid
        assert any("assessment context" in issue for issue in verified.issues)
    else:
        assert verified.valid, verified.issues
    assert image.read_bytes() == original
