"""Supplied candidate metadata only; no task archive location or inspection provider."""
from dataclasses import dataclass
from datetime import datetime
LAUNCH_HOLD = "Launch held: approved deployment root, runtime and native GUI qualification are not supplied."

@dataclass(frozen=True)
class CandidatePackageObservation:
    component_id: str
    software_version: str
    archive_path: str
    archive_sha256: str
    source_plan_sha256: str
    observed_at: datetime
    size_bytes: int
    source_files: int
    snapshot_interface_version: int
    supported_snapshot_interfaces: tuple
    declared_dependencies: tuple
    notes: str
    release_dates: tuple
    installable: bool = False
    authenticated: bool = False
    self_contained: bool = False
    execution_qualified: bool = False
    installation_state: str = "unknown"
    running_state: str = "unknown"
