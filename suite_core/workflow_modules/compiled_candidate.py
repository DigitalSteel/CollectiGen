"""Build-bound pending candidate policy; never completed native evidence.

The ordinary source and release build leave PROFILE unset. A private isolated
qualification build may render the exact pending document and public trust here.
It still needs signed software and the original native parent/session grant.
This policy does not grant Codex or a user permission to execute a trial.
"""
from dataclasses import dataclass

from core.workflow_candidate_admission import CandidateWorkflowAdmission
from suite_core.workflow_modules.contracts import WORKFLOW_EXECUTABLES


@dataclass(frozen=True)
class CompiledWorkflowCandidateAdmission:
    component_id: str
    admission: CandidateWorkflowAdmission
    workspace_contract_version: int = 1

    def validate(self, component_id):
        if (self.component_id != component_id or component_id not in WORKFLOW_EXECUTABLES
                or type(self.admission) is not CandidateWorkflowAdmission
                or type(self.workspace_contract_version) is not int
                or self.workspace_contract_version != 1):
            raise ValueError('compiled_pending_workflow_candidate_required')
        self.admission.validate().version_for(component_id)
        return self

    @property
    def software_version(self):
        return self.admission.version_for(self.component_id)

    @property
    def trust_bytes(self):
        return self.admission.trust_bytes

    @property
    def trust_sha256(self):
        return self.admission.trust_sha256


PROFILE = None
