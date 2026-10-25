"""Read-only, explicit-location resolution of recorded training package claims.

Hashes in manual dataset/training_manifest references have no declared hash
domain and are deliberately not locators. No discovery or registry writes occur.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re

from .training_package import (
    MANIFEST_NAME, TrainingPackageService, TrainingPackageVerification,
    TrainingPackageVerificationLimits, _inspection_signature,
)
from .training_source_io import _assert_unredirected


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _sha(value):
    return type(value) is str and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _identity(value):
    return type(value) is str and value.startswith("sha256:") and _sha(value[7:])


@dataclass(frozen=True)
class ModelTrainingPackageReference:
    model_id: str
    registry_revision: str
    claim_digest: str
    package_fingerprint: str
    configuration_json: str
    base_id: str
    candidate_fingerprint: str = ""


def training_package_reference(record, model_id, revision):
    """Project one known producer's claim; malformed/ambiguous data is held."""
    try:
        claims = record.get("lineage")
        if not _identity(model_id) or type(claims) is not list or len(claims) != 1:
            return None
        claim = claims[0]
        if (type(claim) is not dict or type(claim.get("schema_version")) is not int
                or claim["schema_version"] != 1 or claim.get("operation") != "training"
                or claim.get("output_id") != model_id or not _sha(claim.get("package_fingerprint"))):
            return None
        configuration = claim["training_configuration"]
        base = configuration["target"]["base_model_id"]
        parents = claim["parent_ids"]
        if (not _identity(base) or type(parents) is not list or not 1 <= len(parents) <= 64
                or any(not _identity(parent) for parent in parents)
                or len(set(parents)) != len(parents) or model_id in parents or base not in parents):
            return None
        authority = claim.get("authority")
        if authority == "user_attributed" and claim.get("execution_receipt") == "unavailable":
            # register_training_result records the CANDIDATE plan here.
            candidate = claim.get("plan_fingerprint")
            if not _sha(candidate): return None
        elif authority == "collectigen_verified" and _identity(claim.get("execution_receipt")):
            # TrainingResultService records the PROCESS plan here. Never compare
            # that digest against the package's embedded candidate plan.
            if not _sha(claim.get("plan_fingerprint")): return None
            candidate = ""
        else:
            return None
        encoded = _json(claim)
        if len(encoded.encode("utf-8")) > 256 * 1024: return None
        return ModelTrainingPackageReference(model_id, revision,
            hashlib.sha256(encoded.encode("utf-8")).hexdigest(), claim["package_fingerprint"],
            _json(configuration), base, candidate)
    except (KeyError, TypeError, ValueError, RecursionError):
        return None


@dataclass(frozen=True)
class TrainingPackageReferenceResolution:
    status: str
    reason: str
    reference: ModelTrainingPackageReference
    verification: TrainingPackageVerification | None = None
    root_signature: tuple = ()


def _candidate_path(value, protected_roots):
    if not isinstance(value, (str, Path)) or not Path(value).is_absolute():
        raise ValueError("Choose an explicit absolute package folder.")
    path = Path(os.path.abspath(value))
    # Reject overlap BEFORE stat/read, including ancestors of protected zones.
    for value in protected_roots:
        root = Path(os.path.abspath(value))
        if path == root or root in path.parents or path in root.parents:
            raise ValueError("This package location is protected. Choose a separate public package.")
    _assert_unredirected(path)
    return path


def _root_signature(path):
    _assert_unredirected(path)
    value = path.stat(follow_symlinks=False)
    return value.st_dev, value.st_ino, value.st_mtime_ns, value.st_ctime_ns


def _current_claim(registry, reference):
    snapshot, revision = registry.snapshot()
    current = training_package_reference(snapshot["resources"].get(reference.model_id, {}),
                                         reference.model_id, revision)
    if current != reference:
        raise ValueError("The model registry or recorded claim changed. Reload Models and choose the reference again.")


def reference_sources_current(resolution, *, should_cancel=None):
    """Metadata-only freshness of the exact verified source list; no discovery."""
    verification = resolution.verification
    if resolution.status != "resolved" or verification is None: return False
    try:
        if _root_signature(verification.destination) != resolution.root_signature: return False
        for path, signature in verification.inspection_sources:
            if should_cancel and should_cancel():
                raise InterruptedError("Reference inspection was cancelled.")
            if _inspection_signature(Path(path)) != signature: return False
        return True
    except InterruptedError:
        raise
    except (OSError, ValueError, RuntimeError):
        return False


def reference_anchor_current(resolution):
    """Cheap presentation guard after the worker checked every artifact."""
    if resolution.verification is None: return False
    try:
        root = resolution.verification.destination
        sources = resolution.verification.inspection_sources
        return (bool(sources) and _root_signature(root) == resolution.root_signature
                and sources[0][0] == str(root / MANIFEST_NAME)
                and _inspection_signature(root / MANIFEST_NAME) == sources[0][1])
    except (OSError, ValueError, RuntimeError):
        return False


def resolve_training_package_reference(registry, reference, candidate, *, protected_roots=(),
                                       limits=None, should_cancel=None):
    """Verify a user-selected public package against one exact recorded claim."""
    cancel = should_cancel or (lambda: False)
    try:
        if cancel(): raise InterruptedError("Reference inspection was cancelled.")
        _current_claim(registry, reference)
        path = _candidate_path(candidate, protected_roots)
        root_signature = _root_signature(path)
        verification = TrainingPackageService().verify(path, should_cancel=cancel,
            limits=limits or TrainingPackageVerificationLimits(),
            expected_package_fingerprint=reference.package_fingerprint)
        if not verification.valid:
            return TrainingPackageReferenceResolution("unavailable", " ".join(verification.issues), reference)
        manifest = verification.manifest
        if (verification.package_fingerprint != reference.package_fingerprint
                or _json(manifest["training_configuration"]) != reference.configuration_json
                or manifest["lineage"]["base_model_id"] != reference.base_id
                or reference.candidate_fingerprint and
                    manifest["candidate_plan"]["plan_fingerprint"] != reference.candidate_fingerprint):
            raise ValueError("The chosen package does not match the recorded package, configuration, base or candidate identity.")
        result = TrainingPackageReferenceResolution("resolved", "Exact recorded package verified; frozen data only.",
            reference, verification, root_signature)
        _current_claim(registry, reference)
        if not reference_sources_current(result, should_cancel=cancel):
            raise ValueError("The chosen package changed during inspection. Choose it again to retry.")
        if cancel(): raise InterruptedError("Reference inspection was cancelled.")
        return result
    except InterruptedError:
        raise
    except (OSError, ValueError, RuntimeError, TypeError, KeyError) as exc:
        return TrainingPackageReferenceResolution("unavailable", str(exc), reference)
