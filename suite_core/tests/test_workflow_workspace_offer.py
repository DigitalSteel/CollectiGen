"""Workspace offer consistency is separate from original native authority."""
from dataclasses import replace

import pytest

from suite_core.workflow_modules.contracts import WorkflowWorkspaceOffer


@pytest.fixture(params=["generate", "train"])
def offer(tmp_path, request):
    component = "collectigen." + request.param
    return WorkflowWorkspaceOffer(
        schema_version=1, workspace_contract_version=1, component_id=component,
        software_version="0.1.0-alpha.1", code_root=tmp_path / "Code", data_root=tmp_path / "Data",
        package_root=tmp_path / "Code" / "Apps" / component / "0.1.0-alpha.1",
        workspace_root=tmp_path / "Data" / "Manager", inventory_sha256="1" * 64,
        manifest_sha256="2" * 64, owner_generation="3" * 32, channel_epoch="4" * 32,
        attempt_id="5" * 32, grant_id="6" * 32, native_identity_sha256="7" * 64,
        access_mode="exclusive-writer")


def test_independent_component_uses_canonical_legacy_workspace(offer):
    restored = WorkflowWorkspaceOffer.from_mapping(offer.mapping())
    assert restored == offer
    assert restored.workspace_root == offer.data_root / "Manager"
    assert restored.program.name in {"Generate.exe", "Train.exe"}
    assert restored.helper_role in {"owned-generate", "owned-train"}
    # This DTO has no API to admit a session, open storage or release a lease.
    assert not hasattr(restored, "production_authorized")


@pytest.mark.parametrize("field,value,reason", [
    ("component_id", "collectigen.unknown", "finite_workflow_component"),
    ("workspace_contract_version", True, "workflow_workspace_contract"),
    ("workspace_contract_version", 2, "workflow_workspace_contract"),
    ("inventory_sha256", "short", "exact_workflow_digest"),
    ("owner_generation", "", "original_workflow_lifetime_identity"),
    ("access_mode", "concurrent-writer", "serialized_workflow_workspace_required"),
])
def test_offer_refuses_unenrolled_or_unbound_context(offer, field, value, reason):
    with pytest.raises(ValueError, match=reason):
        replace(offer, **{field: value})


def test_offer_cannot_select_a_new_or_moved_workspace(offer):
    with pytest.raises(ValueError, match="canonical_legacy_workflow_workspace"):
        replace(offer, workspace_root=offer.data_root / "AnotherWorkspace")
    with pytest.raises(ValueError, match="exact_workflow_package_root"):
        replace(offer, package_root=offer.code_root / "Apps" / "Manager" / "current")


def test_offer_rejects_unknown_fields_and_nonstring_locators(offer):
    payload = offer.mapping()
    payload["trusted"] = True
    with pytest.raises(ValueError, match="exact_workflow_offer_fields"):
        WorkflowWorkspaceOffer.from_mapping(payload)
    payload = offer.mapping()
    payload["workspace_root"] = offer.workspace_root
    with pytest.raises(ValueError, match="bounded_workflow_root_locator"):
        WorkflowWorkspaceOffer.from_mapping(payload)
