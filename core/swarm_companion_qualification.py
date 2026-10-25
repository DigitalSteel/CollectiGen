"""Read-only consumer of one reviewed, already executed companion qualification.

Pins are application policy, never imported from a selected manifest. The retained
developer candidate is not a shipped component. No build, host read, or execution
is performed; an absent or changed candidate makes preparation unavailable.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from . import swarm_configuration_backup as safe
from .swarm_companion_installation import FILES, PackageError, QualificationEvidence

_ROOT = Path(__file__).resolve().parents[1]
_PACKET = ".tmp/swarm-companion-real-host/b58cba03ce3140ef9c4122356f123b1e"
_PROOFS = (
    (f"{_PACKET}/preparation.json", "8366cb7df4e41fea659a1986052d0df9355db786b5e4e7e1833472e121e8623d"),
    (f"{_PACKET}/approved-run-outcome.json", "fa2e42252621c78e81402e59f70baa821c5f2eac56cbb431344a9fb6504ffc01"),
    (f"{_PACKET}/run-73981c09392f426f97546e424272f22a/execution-result.json",
     "409b35a14ac742a5b1353c1ed6fd67cc84398759d60718e4b841f828bd1c45a9"),
)
_SOURCE = "integrations/swarmui/CollectiGenReadOnlyInventory"
_DLL = ".tmp/swarm-companion-host/eed96508903d472094b1f5803102cbcd/CollectiGenReadOnlyInventory.dll"
_HASHES = (
    "75730adc133258226ac78256311c8897adde48a9716f17fecfe973e1ef8536b1",
    "1fa0c796d7c15ff0cdeacb545e340cbd1e322c9db6d8b7c46c0a8c682ffa76c7",
    "892d481dbb038588305d3e1bc639fa3aed7b3d3a3fffaf37b4900afca9f2a3fa",
    "a58d6b1bf367e6e37609a879c5bc9931ad56b6fe202ca0e3843d24db0886bd2a",
)
_HOST = "1a7908c4edd3424a50881d56ecba0a910840e7a2af1a452514de812cb8d2ad03"
_PROOF_LIMIT = 128 * 1024
UNAVAILABLE = "The reviewed developer companion candidate is unavailable or changed. Reopening existing packages remains available."


def _proof(path: Path, expected: str):
    path = safe._destination_path(path)
    before = safe._regular(path)
    if not 0 < before.st_size <= _PROOF_LIMIT:
        raise PackageError(UNAVAILABLE)
    with path.open("rb") as stream:
        if not safe._same_open_stat(before, os.fstat(stream.fileno())):
            raise PackageError(UNAVAILABLE)
        raw = stream.read(_PROOF_LIMIT + 1)
        if not safe._same_open_stat(before, os.fstat(stream.fileno())):
            raise PackageError(UNAVAILABLE)
    if (len(raw) != before.st_size or not safe._same_stat(before, safe._regular(path))
            or hashlib.sha256(raw).hexdigest() != expected):
        raise PackageError(UNAVAILABLE)
    return json.loads(raw.decode("utf-8-sig"))


class PinnedCompanionQualification:
    """Production pins for exact retained evidence; no caller-supplied trust keys."""

    def __init__(self):
        self.root = _ROOT

    def artifacts(self) -> dict[str, Path]:
        return {name: self.root / (_DLL if name.endswith(".dll") else f"{_SOURCE}/{name}") for name in FILES}

    def verify(self, files) -> QualificationEvidence:
        try:
            paths = self.artifacts()
            if (tuple(f.role for f in files) != FILES
                    or tuple(f.sha256 for f in files) != _HASHES
                    or any(Path(f.path) != paths[f.role] for f in files)):
                raise PackageError(UNAVAILABLE)
            preparation, outcome, result = [_proof(self.root / path, pin) for path, pin in _PROOFS]
            if (preparation["schema"] != 1 or preparation["status"] != "compiled-not-executed"
                    or preparation["companion"]["sha256"] != _HASHES[3]
                    or next(row["sha256"] for row in preparation["host"] if row["name"] == "SwarmUI.dll") != _HOST
                    or {row["name"]: row["sha256"] for row in preparation["sources"] if row["name"] in FILES}
                       != dict(zip(FILES[:2], _HASHES[:2]))
                    or outcome["status"] != "passed" or outcome["approved_runs"] != 1
                    or outcome["execution_mode"] != "ExecuteReviewed"
                    or outcome["owned_process_exit_code"] != 0 or outcome["owned_process_exit_observed"] is not True
                    or outcome["source_hashes_unchanged"] is not True
                    or outcome["existing_runtime_contacted"] is not False
                    or outcome["preparation_sha256"] != _PROOFS[0][1] or outcome["result_sha256"] != _PROOFS[2][1]
                    or outcome["owned_process_id"] != result["processId"] or result["status"] != "passed"
                    or result["realDispatch"] is not True
                    or (outcome["requests"], outcome["checks"], result["requests"], result["checks"]) != (12, 45, 12, 45)
                    or any(value is not False for value in (outcome["normal_loader_qualified"], outcome["installed"],
                                                           result["normalLoaderQualified"], result["installed"]))):
                raise PackageError(UNAVAILABLE)
            hashes = tuple(zip(FILES, _HASHES))
            return QualificationEvidence(hashes, _HOST, _PROOFS[1][1],
                qualified_artifact_hashes=tuple(row for row in hashes if row[0] != FILES[2]),
                reviewed_support_hashes=(hashes[2],))
        except (OSError, ValueError, TypeError, KeyError, StopIteration, RecursionError, safe.BackupError) as exc:
            raise PackageError(UNAVAILABLE) from exc
