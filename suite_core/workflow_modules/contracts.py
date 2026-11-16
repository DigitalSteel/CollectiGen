"""Finite workspace offer data; an offer alone never grants application access.

The original native owner must bind this data to the signed selected component,
its actual child/channel and retained exclusive workspace lifetime. Parsing a
mapping or supplying paths on a command line is not that admission.
"""
from dataclasses import dataclass, fields
from pathlib import Path
import re
from types import MappingProxyType

from suite_core.discovery.component_release_signature import _version


WORKSPACE_CONTRACT_VERSION = 1
WORKFLOW_EXECUTABLES = MappingProxyType({
    "collectigen.generate": "Generate.exe",
    "collectigen.train": "Train.exe",
})
_PATH_FIELDS = frozenset({"code_root", "data_root", "package_root", "workspace_root"})


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


@dataclass(frozen=True)
class WorkflowWorkspaceOffer:
    schema_version: int
    workspace_contract_version: int
    component_id: str
    software_version: str
    code_root: Path
    data_root: Path
    package_root: Path
    workspace_root: Path
    inventory_sha256: str
    manifest_sha256: str
    owner_generation: str
    channel_epoch: str
    attempt_id: str
    grant_id: str
    native_identity_sha256: str
    access_mode: str

    def __post_init__(self):
        _require(type(self.schema_version) is int and self.schema_version == 1,
                 "workflow_offer_schema")
        _require(type(self.workspace_contract_version) is int
                 and self.workspace_contract_version == WORKSPACE_CONTRACT_VERSION,
                 "workflow_workspace_contract")
        _require(type(self.component_id) is str and self.component_id in WORKFLOW_EXECUTABLES,
                 "finite_workflow_component")
        _version(self.software_version)
        for name in _PATH_FIELDS:
            value = getattr(self, name)
            _require(isinstance(value, Path) and value.is_absolute()
                     and ".." not in value.parts and len(str(value)) <= 4096
                     and "\0" not in str(value), "absolute_workflow_roots")
        _require(self.package_root == self.code_root / "Apps" / self.component_id / self.software_version,
                 "exact_workflow_package_root")
        # Existing immutable receipts contain these physical paths. Extraction
        # changes the UI owner, not the canonical workspace or its records.
        _require(self.workspace_root == self.data_root / "Manager",
                 "canonical_legacy_workflow_workspace")
        for name in ("inventory_sha256", "manifest_sha256", "native_identity_sha256"):
            value = getattr(self, name)
            _require(type(value) is str and re.fullmatch("[0-9a-f]{64}", value) is not None,
                     "exact_workflow_digest")
        for name in ("owner_generation", "channel_epoch", "attempt_id", "grant_id"):
            value = getattr(self, name)
            _require(type(value) is str and re.fullmatch("[0-9a-f]{32}", value) is not None,
                     "original_workflow_lifetime_identity")
        _require(type(self.access_mode) is str and self.access_mode == "exclusive-writer",
                 "serialized_workflow_workspace_required")

    @property
    def program(self):
        return self.package_root / WORKFLOW_EXECUTABLES[self.component_id]

    @property
    def helper_role(self):
        return "owned-" + self.component_id.removeprefix("collectigen.")

    def mapping(self):
        return {field.name: str(getattr(self, field.name)) if field.name in _PATH_FIELDS
                else getattr(self, field.name) for field in fields(self)}

    @classmethod
    def from_mapping(cls, value):
        _require(type(value) is dict and set(value) == {field.name for field in fields(cls)},
                 "exact_workflow_offer_fields")
        for name in _PATH_FIELDS:
            _require(type(value[name]) is str and 0 < len(value[name]) <= 4096
                     and "\0" not in value[name], "bounded_workflow_root_locator")
        return cls(**{key: Path(item) if key in _PATH_FIELDS else item for key, item in value.items()})
