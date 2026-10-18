"""Reference-only collection versions over the canonical Training plan.

No source discovery, persistence, permission approval or image copying occurs here.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from contextlib import contextmanager, nullcontext
import hashlib
import json
import re

from .concept_groups import ConceptGroupStore, ConceptGroupError, verify_plan_membership
from .concept_groups import _validate as validate_collection_snapshot
from .model_resource_registry import _json as strict_json, ModelResourceError
from .training_candidate_selection import TrainingCandidatePlan
from .training_package import (
    TrainingPackageService, TrainingPackagePlan, TrainingPackageResult, TrainingCollectionBinding,
)
from .training_plan_drafts import (
    validate_training_plan_manifest, TrainingPlanDraftError,
    _validate_source_record, _validate_permission_evidence, _assert_source_neutral,
)
from .training_configuration import TrainingConfiguration
from .export_profile_store import validate_export_profile
from .export_profile import ExportProfile

MAX_VERSION_BYTES = 64 * 1024 * 1024
MAX_VERSION_DEPTH = 32
MAX_VERSION_NODES = 2_000_000


def _structural_bounds(value):
    """Bound decoded JSON iteratively before serialization or recursive validators."""
    pending = [(value, 0)]
    nodes = 1
    while pending:
        item, depth = pending.pop()
        if depth > MAX_VERSION_DEPTH or nodes > MAX_VERSION_NODES:
            raise ValueError("Collection version exceeds supported depth or node limits.")
        if isinstance(item, dict):
            # Count keys too: wide objects must not evade the node bound.
            nodes += 2 * len(item)
            if nodes > MAX_VERSION_NODES:
                raise ValueError("Collection version exceeds supported depth or node limits.")
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            nodes += len(item)
            if nodes > MAX_VERSION_NODES:
                raise ValueError("Collection version exceeds supported depth or node limits.")
            pending.extend((child, depth + 1) for child in item)


def _validate_held_manifest(manifest):
    """Validate review evidence without asserting complete/unique draft semantics.

    Partial/duplicate/cancelled captures may deliberately retain inconsistent
    analysis/count snapshots. Preserve those observations; they confer no export
    readiness and do not enter this path without a recomputed membership hold.
    """
    required = {"schema_version", "kind", "goal", "selection_scope", "profile",
        "profile_provenance", "counts", "candidates", "issues", "dataset_analysis",
        "permission_review", "training_configuration", "training_configuration_issues",
        "package_ready", "limitations", "cancelled", "plan_fingerprint"}
    if (not isinstance(manifest, dict) or set(manifest) != required
            or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 6
            or manifest["kind"] != "collectigen_training_candidate_plan"
            or type(manifest["package_ready"]) is not bool
            or type(manifest["cancelled"]) is not bool):
        raise ValueError("Unsupported held candidate evidence.")
    for field in ("candidates", "issues", "training_configuration_issues", "limitations"):
        if not isinstance(manifest[field], list):
            raise ValueError("Malformed held candidate evidence lists.")
    for field in ("goal", "selection_scope", "profile_provenance", "counts", "permission_review"):
        if not isinstance(manifest[field], dict):
            raise ValueError("Malformed held candidate evidence objects.")
    if manifest["dataset_analysis"] is not None and not isinstance(manifest["dataset_analysis"], dict):
        raise ValueError("Malformed held analysis evidence.")
    body = {key: value for key, value in manifest.items() if key != "plan_fingerprint"}
    if hashlib.sha256(_bytes(body)).hexdigest() != manifest["plan_fingerprint"]:
        raise ValueError("Held candidate evidence fingerprint does not match.")
    profile = ExportProfile.from_dict(manifest["profile"])
    validate_export_profile(profile)
    if profile.to_dict() != manifest["profile"]:
        raise ValueError("Held profile evidence is not canonical.")
    if manifest["training_configuration"] is not None:
        configuration = TrainingConfiguration.from_dict(manifest["training_configuration"])
        if configuration.to_dict() != manifest["training_configuration"]:
            raise ValueError("Held configuration evidence is not canonical.")
    scope = manifest["selection_scope"]
    for field in ("frozen_selection", "not_processed"):
        if not isinstance(scope.get(field), list):
            raise ValueError("Malformed held selection evidence.")
        for source in scope[field]:
            if _validate_source_record(source, frozen=True) != source:
                raise ValueError("Held source identity is not canonical.")
    for candidate in manifest["candidates"]:
        if (not isinstance(candidate, dict)
                or not isinstance(candidate.get("stable_id"), str)
                or not isinstance(candidate.get("caption"), str)
                or candidate.get("decision") not in {"include", "exclude"}
                or candidate.get("role") not in {"training", "validation", "holdout"}
                or not isinstance(candidate.get("issues"), list)):
            raise ValueError("Malformed held candidate record.")
        source = candidate["source"]
        if _validate_source_record(source, frozen=False) != source:
            raise ValueError("Held candidate identity is not canonical.")
        _validate_permission_evidence(candidate["permission_evidence"])
        state = candidate["source_state"]
        if state is not None and (not isinstance(state, dict)
                or not isinstance(state.get("file"), dict)
                or not isinstance(state["file"].get("sha256"), str)
                or not re.fullmatch("[0-9a-f]{64}", state["file"]["sha256"])):
            raise ValueError("Malformed held candidate source evidence.")
        caption_hash = candidate.get("caption_sha256")
        if caption_hash not in {"", hashlib.sha256(candidate["caption"].encode("utf-8")).hexdigest()}:
            raise ValueError("Held caption hash does not match.")
    _assert_source_neutral(manifest)
    return manifest


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _key(library, relative):
    return (library.casefold(), relative.casefold())


@dataclass(frozen=True)
class CollectionTrainingVersion:
    """Immutable UTF-8 evidence; callers receive detached dictionaries."""

    payload: bytes
    issues: tuple[str, ...]

    @property
    def version_id(self):
        return hashlib.sha256(self.payload).hexdigest()

    @property
    def ready(self):
        return not self.issues

    def to_dict(self):
        return json.loads(self.payload)

    def to_bytes(self):
        """Portable durable envelope; storage remains the caller's guarded owner."""
        return _bytes({"schema_version": 1, "version_id": self.version_id,
                       "version": self.to_dict()})

    @classmethod
    def from_bytes(cls, raw: bytes):
        try:
            return cls._decode_bytes(raw)
        except (TypeError, KeyError, AttributeError, RecursionError, OverflowError,
                TrainingPlanDraftError, ModelResourceError) as exc:
            raise ValueError("Collection version contains malformed evidence.") from exc

    @classmethod
    def _decode_bytes(cls, raw: bytes):
        if not isinstance(raw, bytes) or len(raw) > MAX_VERSION_BYTES:
            raise ValueError("Collection version exceeds the supported byte limit.")
        try:
            envelope = strict_json(raw)
        except ModelResourceError as exc:
            raise ValueError("Collection version JSON is invalid.") from exc
        _structural_bounds(envelope)
        if (not isinstance(envelope, dict) or set(envelope) != {
                "schema_version", "version_id", "version"}
                or type(envelope["schema_version"]) is not int
                or envelope["schema_version"] != 1):
            raise ValueError("Unsupported collection version envelope.")
        value = envelope["version"]
        if (not isinstance(value, dict) or set(value) != {
                "kind", "schema_version", "collection_store_revision", "collection",
                "candidate_plan", "member_outcomes", "issues"}
                or value["kind"] != "collectigen_collection_training_version"
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or not isinstance(value["collection_store_revision"], str)
                or not re.fullmatch("[0-9a-f]{64}", value["collection_store_revision"])):
            raise ValueError("Unsupported collection version payload.")
        payload = _bytes(value)
        if hashlib.sha256(payload).hexdigest() != envelope["version_id"]:
            raise ValueError("Collection version digest does not match its evidence.")
        group = value["collection"]
        if not isinstance(group, dict) or not isinstance(group.get("id"), str):
            raise ValueError("Collection version has no saved collection identity.")
        validate_collection_snapshot({"schema_version": 2, "groups": {group["id"]: group}})
        raw_manifest = value["candidate_plan"]
        # Only named held snapshot forms can use the evidence-only decoder.
        # Digest/outcome checks below remain mandatory; this never falls back
        # after a failed executable-draft validation.
        if not isinstance(raw_manifest, dict) or type(raw_manifest.get("cancelled")) is not bool:
            raise ValueError("Malformed candidate cancellation evidence.")
        preliminary_outcomes, preliminary_issues = _membership_evidence(group, raw_manifest)
        review_only = raw_manifest["cancelled"] or bool(set(preliminary_issues) & {
            "unresolved_collection_members", "duplicate_candidate_identity"})
        if review_only:
            manifest = _validate_held_manifest(raw_manifest)
        else:
            try:
                manifest = validate_training_plan_manifest(raw_manifest)
            except TrainingPlanDraftError as exc:
                raise ValueError("Collection version candidate evidence is invalid.") from exc
        if manifest != value["candidate_plan"]:
            raise ValueError("Collection version candidate evidence is not canonical.")
        outcomes, issues = preliminary_outcomes, preliminary_issues
        if outcomes != value["member_outcomes"] or issues != value["issues"]:
            raise ValueError("Collection version outcomes do not match its evidence.")
        return cls(payload, tuple(issues))


def capture_collection_training_version(
    store: ConceptGroupStore, collection_id: str, plan: TrainingCandidatePlan,
) -> CollectionTrainingVersion:
    """Bind one validated saved collection to an explicitly inspected full plan.

    Missing/unreadable candidates and stale member hashes remain named holds.
    Excluded candidates remain evidence: they are never dropped from the version.
    """
    snapshot, revision = store.snapshot()
    if collection_id not in snapshot["groups"]:
        raise ValueError("The selected saved collection no longer exists.")
    collection = snapshot["groups"][collection_id]
    manifest = plan.to_manifest_dict()
    try:
        verify_plan_membership(collection, plan)
    except ConceptGroupError:
        # The canonical admission check is reused; the durable representation
        # below retains each named outcome instead of flattening its exception.
        pass
    outcomes, issues = _membership_evidence(collection, manifest)
    payload = {
        "kind": "collectigen_collection_training_version", "schema_version": 1,
        "collection_store_revision": revision, "collection": collection,
        "candidate_plan": manifest, "member_outcomes": outcomes, "issues": issues,
    }
    return CollectionTrainingVersion(_bytes(payload), tuple(issues))


def _membership_evidence(collection, manifest):
    members = {_key(m["library_id"], m["relative_path"]): m
               for m in collection["members"]}
    issues = []
    seen = set()
    outcomes = []
    for candidate in manifest["candidates"]:
        source = candidate["source"]
        if source.get("kind") != "library_image":
            issues.append("non_library_identity")
            continue
        key = _key(source["library_id"], source["relative_image_path"])
        if key in seen:
            issues.append("duplicate_candidate_identity")
        seen.add(key)
        member = members.get(key)
        state = candidate["source_state"]
        if member is None:
            status = "outside_collection"
        elif state is None:
            status = "missing_or_unreadable"
        elif state["file"]["sha256"] != member["image_sha256"]:
            status = "stale_content"
        else:
            status = "pinned"
        outcomes.append({"source": source, "status": status})
        if status != "pinned":
            issues.append(status)
    if set(members) - seen:
        issues.append("unresolved_collection_members")
    if not members:
        issues.append("empty_collection")
    if issues and not manifest["cancelled"]:
        issues.append("collection_membership_not_exact")
    if manifest["cancelled"] or not manifest["package_ready"]:
        issues.append("candidate_plan_not_package_ready")
    return outcomes, sorted(set(issues))


def preview_collection_training_export(
    version: CollectionTrainingVersion, store: ConceptGroupStore,
    collection_id: str, current_plan: TrainingCandidatePlan, destination,
    *, package_service: TrainingPackageService | None = None, **preview_options,
):
    """Hold on any changed pin, then delegate all live guards to package preview.

    The caller must explicitly obtain a current candidate plan. A matching version
    does not bypass package preview's source recheck or write-decision authority.
    """
    if not version.ready:
        raise ValueError("Collection version is held: " + ", ".join(version.issues))
    current = capture_collection_training_version(store, collection_id, current_plan)
    if not current.ready or current.payload != version.payload:
        raise ValueError("Collection dataset version is stale; rebuild and review it.")
    service = package_service or TrainingPackageService()
    binding = _package_binding(version)
    return service.preview(current_plan, destination, collection_version=binding,
        collection_guard=lambda: _collection_lease(version, store), **preview_options)


def _package_binding(version):
    value = version.to_dict()
    return TrainingCollectionBinding(version.version_id, value["collection"]["id"],
        value["collection_store_revision"], value["candidate_plan"]["plan_fingerprint"])


@contextmanager
def _collection_lease(version, store):
    expected = version.to_dict()
    with store.locked_snapshot() as (snapshot, revision):
        group = snapshot["groups"].get(expected["collection"]["id"])
        if revision != expected["collection_store_revision"] or group != expected["collection"]:
            raise ValueError("Collection dataset version is stale at apply; rebuild and review it.")
        yield


@dataclass(frozen=True)
class CollectionTrainingExportPlan:
    version: CollectionTrainingVersion
    package_plan: TrainingPackagePlan


@dataclass(frozen=True)
class CollectionTrainingExportResult:
    package_result: TrainingPackageResult
    evidence: bytes


def prepare_collection_training_export(version, store, collection_id, current_plan,
                                       destination, **preview_options):
    """Bound successor to the legacy raw-package preview API."""
    validated = CollectionTrainingVersion.from_bytes(version.to_bytes())
    package = preview_collection_training_export(validated, store, collection_id,
        current_plan, destination, **preview_options)
    return CollectionTrainingExportPlan(validated, package)


def apply_collection_training_export(prepared, store, execution, *,
                                     package_service=None, **apply_options):
    """Recheck the exact collection under its canonical writer lease, then apply.

    The lease prevents canonical Collection edits during package publication.
    It cannot grant authority over external writers that ignore that protocol.
    """
    version = CollectionTrainingVersion.from_bytes(prepared.version.to_bytes())
    package = prepared.package_plan
    expected = version.to_dict()
    if package.candidate_manifest != expected["candidate_plan"]:
        raise ValueError("Package candidate evidence differs from its collection version.")
    if not version.ready:
        raise ValueError("Collection version is held.")
    cancel = apply_options.get("should_cancel")
    if cancel and cancel():
        raise InterruptedError("Collection export was cancelled before admission.")
    service = package_service or TrainingPackageService()
    if package.collection_version != _package_binding(version):
        raise ValueError("Package Collection binding differs from its version.")
    with _collection_lease(version, store):
        current = capture_collection_training_version(store, expected["collection"]["id"],
                                                       package.candidate_plan)
        if current.payload != version.payload or not current.ready:
            raise ValueError("Collection dataset version is stale at apply; rebuild and review it.")
        # This helper already owns the public lease; avoid taking a second
        # nonreentrant OS lock. Direct canonical apply uses the prepared guard.
        result = service.apply(replace(package, collection_guard=nullcontext), execution, **apply_options)
    return _bound_result(version, package, result)


def _bound_result(version, package, result):
    expected = version.to_dict()
    verified = result.verification
    binding_valid = bool(result.status in {"created", "current", "recovered"} and verified and verified.valid
        and verified.intent_fingerprint == package.intent_fingerprint
        and verified.manifest.get("candidate_plan") == expected["candidate_plan"]
        and verified.manifest.get("collection_version") == package.collection_version.to_dict())
    evidence = _bytes({"kind": "collectigen_collection_training_export_evidence",
        "schema_version": 1, "collection_version_id": version.version_id,
        "collection_id": expected["collection"]["id"],
        "collection_store_revision": expected["collection_store_revision"],
        "candidate_plan_fingerprint": expected["candidate_plan"]["plan_fingerprint"],
        "package_intent_fingerprint": package.intent_fingerprint,
        "package_fingerprint": verified.package_fingerprint if binding_valid else "",
        "package_status": result.status, "verified_binding": binding_valid})
    return CollectionTrainingExportResult(result, evidence)


def recover_collection_training_export(prepared, store, receipt, *, package_service=None, **options):
    """Recover only this exact bound intent under the current Collection lease."""
    version = CollectionTrainingVersion.from_bytes(prepared.version.to_bytes())
    package = prepared.package_plan
    if not version.ready or package.collection_version != _package_binding(version):
        raise ValueError("The Collection version is held or differs from the recovery binding.")
    service = package_service or TrainingPackageService()
    with _collection_lease(version, store):
        result = service.recover(receipt, candidate_plan=package.candidate_plan,
            expected_intent=package.intent_fingerprint,
            collection_version=package.collection_version,collection_guard=nullcontext, **options)
    return _bound_result(version, package, result)
