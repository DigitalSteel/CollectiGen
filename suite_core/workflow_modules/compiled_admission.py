"""Build-bound public admission policy; source checkout deliberately cannot launch.

A reviewed module build may replace PROFILE with this finite value. Public trust
bytes and their pin are compiled inputs, never environment/CLI/peer enrollment.
The qualification pin names retained native-route evidence; it grants no process
or workspace authority by itself. No private signing material belongs here.
"""
from dataclasses import dataclass
from hashlib import sha256
import re

from suite_core.discovery.component_release_signature import _trust, _version
from suite_core.workflow_modules.contracts import WORKFLOW_EXECUTABLES


@dataclass(frozen=True)
class CompiledWorkflowAdmission:
    component_id: str
    software_version: str
    trust_bytes: bytes
    trust_sha256: str
    minimum_manager_version: str
    maximum_manager_version_exclusive: str
    native_entry_qualification_sha256: str
    workspace_contract_version: int = 1

    def validate(self, component_id):
        if (self.component_id != component_id or component_id not in WORKFLOW_EXECUTABLES
                or type(self.workspace_contract_version) is not int or self.workspace_contract_version != 1):
            raise ValueError('compiled_finite_workflow_admission_required')
        _version(self.software_version)
        minimum, maximum = _version(self.minimum_manager_version), _version(self.maximum_manager_version_exclusive)
        if minimum.compare(maximum) >= 0:
            raise ValueError('compiled_manager_compatibility_range_required')
        if (type(self.trust_bytes) is not bytes or not 0 < len(self.trust_bytes) <= 65536
                or type(self.trust_sha256) is not str or not re.fullmatch('[0-9a-f]{64}', self.trust_sha256)
                or sha256(self.trust_bytes).hexdigest() != self.trust_sha256
                or type(self.native_entry_qualification_sha256) is not str
                or not re.fullmatch('[0-9a-f]{64}', self.native_entry_qualification_sha256)):
            raise ValueError('compiled_workflow_trust_and_qualification_required')
        _trust(self.trust_bytes)
        return self


PROFILE = None
