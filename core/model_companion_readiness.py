"""Explicit companion review over existing guidance, registry and adapter owners.

Rendering is pure. Only verify() and the issued cache resolver inspect the
user-selected registered paths. Nothing downloads, changes recency or launches.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
from pathlib import Path
import re
from uuid import uuid4

from .model_cache import CacheResource, ModelRequirementSet
from .model_resource_registry import ModelResourceError, RESOURCE_ROLES, _cancel
from .model_storage_policy import retained_fast_roots
from .model_use_leases import digest
from .model_workflow_guidance import CompanionRequirement, ResourceBinding
from .model_workspace import _path, _pinned_model_file
from .model_organization import normalize_model_family
from . import training_anima as anima

_RESOURCE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_CHOICES = 64


@dataclass(frozen=True)
class CompanionChoice:
    slot: str
    resource_id: str
    path: str
    role: str
    family: str


@dataclass(frozen=True)
class CompanionReadinessRequest:
    main_resource_id: str
    workflow_kind: str = "generation"
    target_id: str = ""
    adapter_id: str = ""
    adapter_version: str = ""
    choices: tuple[CompanionChoice, ...] = ()


@dataclass(frozen=True)
class CompanionProfile:
    workflow_kind: str
    target_id: str
    adapter_id: str
    adapter_version: str
    label: str
    applicability: str


@dataclass(frozen=True)
class CompanionReadinessRow:
    slot: str
    role: str
    family: str
    necessity: str
    expected_resource_ids: tuple[str, ...]
    resource_id: str
    path: str
    availability: str
    verification: str
    compatibility: str
    summary: str


@dataclass(frozen=True)
class CompanionReadinessReport:
    request: CompanionReadinessRequest
    registry_token: str
    catalog_token: str
    profile_status: str
    authority: str
    rows: tuple[CompanionReadinessRow, ...]
    held: tuple[str, ...]
    qualification: str = "unknown"
    review_id: str = ""
    sources: tuple[str, ...] = ()

    @property
    def ready_to_run(self):
        return False

    @property
    def can_prepare(self):
        return bool(self.review_id and self.request.choices and not self.held
            and all(row.verification == "verified" for row in self.rows if row.resource_id))


@dataclass(frozen=True)
class CompanionAcquisitionHandoff:
    request: CompanionReadinessRequest
    slot: str
    role: str
    family: str
    expected_resource_ids: tuple[str, ...]
    catalog_token: str
    authority: str


def _validate(request):
    if (not isinstance(request, CompanionReadinessRequest)
            or not isinstance(request.main_resource_id, str) or not _RESOURCE.fullmatch(request.main_resource_id)
            or request.workflow_kind not in ("training", "generation")
            or any(not isinstance(value, str) or len(value) > 300 or any(ord(c) < 32 for c in value)
                for value in (request.target_id, request.adapter_id, request.adapter_version))
            or not isinstance(request.choices, tuple) or len(request.choices) > _MAX_CHOICES):
        raise ModelResourceError("Choose an exact standalone model and bounded companion profile.")
    slots = set()
    for choice in request.choices:
        if (not isinstance(choice, CompanionChoice) or not isinstance(choice.slot, str)
                or not choice.slot.strip() or len(choice.slot) > 100 or choice.slot in slots
                or not isinstance(choice.resource_id, str) or not _RESOURCE.fullmatch(choice.resource_id)
                or choice.role not in RESOURCE_ROLES or not isinstance(choice.family, str)
                or not choice.family.strip() or len(choice.family) > 160
                or not isinstance(choice.path, str) or not Path(choice.path).is_absolute()
                or len(choice.path) > 4096
                or any(ord(c) < 32 for c in choice.slot + choice.family + choice.path)):
            raise ModelResourceError("Choose one explicit registered path and declaration per companion slot.")
        if choice.slot == "main" and choice.resource_id != request.main_resource_id:
            raise ModelResourceError("The selected main resource differs from this review.")
        slots.add(choice.slot)


def validate_companion_acquisition(handoff, resource_id, *, role="", family=""):
    """Pure identity/declaration guard before existing acquisition/registration.

    An unpinned catalog hint permits an explicitly reviewed full-file identity;
    this does not make that identity compatible with the selected main model.
    """
    if not isinstance(handoff, CompanionAcquisitionHandoff):
        raise ModelResourceError("Keep the exact companion acquisition handoff with this file.")
    _validate(handoff.request)
    if (not isinstance(resource_id, str) or not _RESOURCE.fullmatch(resource_id)
            or not isinstance(handoff.slot, str) or not handoff.slot.strip() or len(handoff.slot) > 100
            or not isinstance(handoff.expected_resource_ids, tuple) or len(handoff.expected_resource_ids) > 64
            or any(not isinstance(value, str) or not _RESOURCE.fullmatch(value) for value in handoff.expected_resource_ids)):
        raise ModelResourceError("Companion acquisition requires a complete standalone SHA-256 identity.")
    if handoff.expected_resource_ids and resource_id not in handoff.expected_resource_ids:
        raise ModelResourceError("The file does not match this companion slot's expected full identity.")
    if handoff.slot == "main" and resource_id != handoff.request.main_resource_id:
        raise ModelResourceError("The acquired main file differs from the selected main identity.")
    if (not isinstance(role, str) or not isinstance(family, str)
            or (role and (role not in RESOURCE_ROLES or handoff.role and role != handoff.role))):
        raise ModelResourceError("The file's declared role differs from its companion slot.")
    if (family and handoff.family and
            normalize_model_family(family).casefold() != normalize_model_family(handoff.family).casefold()):
        raise ModelResourceError("The file's declared family differs from its companion slot.")
    if handoff.authority == "anima_training_resource_contract":
        owner = CompanionReadinessService._owner(handoff.request)
        expected = next((row for row in owner.requirements if row.slot == handoff.slot), None) if owner else None
        if expected is None or (expected.resource_id, expected.role, expected.family) != (
                resource_id, handoff.role, handoff.family):
            raise ModelResourceError("The exact training companion contract changed; review acquisition again.")
    return resource_id


class CompanionReadinessService:
    def __init__(self, registry, *, guidance=None, protected_roots=(), managed_roots=()):
        self.registry, self.guidance = registry, guidance
        self.protected_roots, self.managed_roots = tuple(protected_roots), tuple(managed_roots)
        self._issued = {}
        self._requirements = {}

    def available_profiles(self, snapshot, main_resource_id, *, allow_planned=False):
        """Unverified suggestions; planned identities never grant file readiness."""
        if not isinstance(main_resource_id, str) or not _RESOURCE.fullmatch(main_resource_id):
            raise ModelResourceError("Choose an exact registered main model first.")
        result = [CompanionProfile("generation", "", "", "", "Manual selection — requirements Unknown", "unknown")]
        if main_resource_id == anima.BASE_ID and (allow_planned or main_resource_id in snapshot.get("resources", {})):
            result.append(CompanionProfile("training", anima.RESOURCE_PROFILE_ID, anima.RESOURCE_ADAPTER_ID,
                anima.SOURCE_REVISION, "Anima Base v1.0 training — exact resource profile; files need verification", "exact_profile_unverified"))
        if self.guidance is not None:
            records = self.guidance.targets()
            if len(records) > 256:
                raise ModelResourceError("Companion profiles exceed the bounded catalog limit.")
            for record in records:
                for workflow in ("training", "generation"):
                    result.append(CompanionProfile(workflow, record["id"], "", "",
                        f"{record['family']} {record['version']} · {workflow} catalog guidance — applicability Unknown", "unknown"))
        return tuple(result)

    @staticmethod
    def _owner(request):
        if not request.adapter_id and not request.adapter_version:
            return None
        try:
            return anima.anima_resource_contract(workflow_kind=request.workflow_kind, profile_id=request.target_id,
                adapter_id=request.adapter_id, adapter_version=request.adapter_version, main_resource_id=request.main_resource_id)
        except anima.AnimaError:
            return None

    def _profile(self, request):
        owner = self._owner(request)
        guidance = self.guidance.companion_projection(request.target_id, request.workflow_kind) if self.guidance else None
        if owner:
            requirements = tuple(CompanionRequirement(row.slot, row.role, row.family, "", owner.adapter_version,
                "Exact pinned Anima TRAINING resource contract; run admission remains separate.", ("training",),
                "required", "tested", (row.resource_id,)) for row in owner.requirements)
            return requirements, owner, guidance
        main = next((choice for choice in request.choices if choice.slot == "main"), None)
        requirements = (CompanionRequirement("main", main.role if main else "", main.family if main else "",
            "", "", "Selected main identity; companion completeness Unknown.", (request.workflow_kind,), "required",
            "not_assessed", (request.main_resource_id,)), *(guidance.requirements if guidance else ()))
        return requirements, None, guidance

    def project(self, request, *, snapshot, registry_token):
        """Pure stored projection, never stat/resolve/network/registry writes."""
        _validate(request)
        if not isinstance(registry_token, str) or not registry_token or not isinstance(snapshot, dict):
            raise ModelResourceError("Review an exact registry snapshot.")
        requirements, owner, guidance = self._profile(request)
        choices = {choice.slot: choice for choice in request.choices}
        known = {row.slot for row in requirements}
        catalog_assessments = {}
        if owner is None and guidance and guidance.known:
            assessed = self.guidance.companion_projection(request.target_id, request.workflow_kind,
                bindings=tuple(ResourceBinding(choice.slot, choice.resource_id) for choice in request.choices
                    if choice.slot != "main" and choice.slot in known), registry_snapshot=snapshot)
            catalog_assessments = {row.slot: row for row in assessed.assessments}
        held = []
        if set(choices) - known:
            held.append("A selected companion slot is not described by this profile. Review the exact set.")
        if (request.adapter_id or request.adapter_version) and owner is None:
            held.append("The requested adapter/profile/version has no exact resource authority for this workflow.")
        rows = []
        for requirement in requirements:
            choice = choices.get(requirement.slot)
            record = snapshot.get("resources", {}).get(choice.resource_id) if choice else None
            declaration = bool(record and any(row.get("role") == choice.role and row.get("family") == choice.family
                for row in record.get("declarations", ())))
            location = bool(record and any(row.get("path") == choice.path for row in record.get("locations", ())))
            available = "missing" if not choice else "registered" if declaration and location else "unregistered_or_mismatched"
            compatibility = "unknown"
            if choice and choice.role != requirement.role:
                compatibility = "mismatch"
                held.append("A selected companion role differs from its reviewed slot.")
            if (choice and requirement.accepted_sha256 and choice.resource_id not in requirement.accepted_sha256
                    and (owner or requirement.compatibility_status != "not_assessed")):
                compatibility = "mismatch"
                held.append("A selected identity differs from the exact profile requirement.")
            assessment = catalog_assessments.get(requirement.slot)
            if choice and assessment and assessment.availability == "declaration_mismatch":
                compatibility = "mismatch"
                held.append("The selected declaration does not match this catalog slot; choose or review its profile.")
            if choice and available != "registered":
                held.append("A selected path or declaration is not registered for this exact resource.")
            summary = ("Select an exact registered file for this slot." if not choice else
                "Stored selection; recheck this exact path. Adapter compatibility and run readiness are separate.")
            rows.append(CompanionReadinessRow(requirement.slot, requirement.role, requirement.family,
                requirement.necessity, requirement.accepted_sha256, choice.resource_id if choice else "",
                choice.path if choice else "", available, "not_checked", compatibility, summary))
        status = "exact_profile_unverified" if owner else "catalog_guidance" if guidance and guidance.known else "unknown"
        authority = "anima_training_resource_contract" if owner else "catalog_claims" if guidance and guidance.known else "unknown"
        return CompanionReadinessReport(request, registry_token, guidance.catalog_fingerprint if guidance else "",
            status, authority, tuple(rows), tuple(dict.fromkeys(held)),
            sources=(f"{owner.adapter_id} @ {owner.adapter_version}",) if owner else guidance.sources if guidance else ())

    def _verify(self, request, token, should_cancel, progress):
        _cancel(should_cancel)
        snapshot, current = self.registry.snapshot()
        if current != token:
            raise ModelResourceError("The registry changed; review companion selections again.")
        if self.guidance is not None:
            self.guidance.require_current_catalog()
        report = self.project(request, snapshot=snapshot, registry_token=token)
        if report.held:
            return report, (), ""
        if not any(choice.slot == "main" for choice in request.choices):
            return replace(report, held=("Choose the main model's exact registered permanent path.",)), (), ""
        # Preflight ALL selected boundaries before the first model read. Never
        # resolve an unselected alternate location or fall through to one.
        fast = retained_fast_roots(snapshot)
        paths = {}
        for choice in request.choices:
            paths[choice.slot] = _path(choice.path, (*self.protected_roots, *self.managed_roots, *fast))
            for candidate in (paths[choice.slot], *paths[choice.slot].parents):
                try:
                    info = candidate.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if int(getattr(info, "st_file_attributes", 0)) & (0x1000 | 0x40000 | 0x400000):
                    raise ModelResourceError("Selected companion files must be locally available, not cloud placeholders.")
            locations = [row for row in snapshot["resources"][choice.resource_id]["locations"] if row["path"] == choice.path]
            if len(locations) != 1 or locations[0].get("cache_entry_id"):
                raise ModelResourceError("Choose an exact permanent original, not an owned cache copy.")
        files, inspections, failures = [], {}, []
        with ExitStack() as pins:
            for choice in request.choices:
                _cancel(should_cancel)
                try:
                    pins.enter_context(_pinned_model_file(paths[choice.slot], self.protected_roots))
                    resolution = self.registry.resolve(choice.resource_id, role=choice.role, family=choice.family,
                        location_path=paths[choice.slot], should_cancel=should_cancel, progress=progress)
                    if resolution.status != "identity_verified" or resolution.inspection is None:
                        raise ModelResourceError("This exact selected file is missing, changed or unsupported.")
                    inspections[choice.slot] = resolution.inspection
                    files.append((choice.slot, tuple(resolution.inspection.file_state)))
                except InterruptedError:
                    raise
                except (OSError, ValueError, RuntimeError):
                    failures.append(choice.slot)
            owner = self._owner(request)
            complete = bool(owner and not failures and {row.slot for row in owner.requirements} == set(inspections))
            if complete:
                from .training_anima import verify_anima_resource_structure
                verify_anima_resource_structure(owner, inspections["main"], protected_roots=self.protected_roots, should_cancel=should_cancel)
            _cancel(should_cancel)
            if self.registry.snapshot()[1] != token:
                raise ModelResourceError("The registry changed during companion verification.")
            fresh = self.project(request, snapshot=snapshot, registry_token=token)
            if fresh != report:
                raise ModelResourceError("The companion profile changed during verification.")
        catalog_matches = set()
        if owner is None and self.guidance is not None:
            assessed = self.guidance.companion_projection(request.target_id, request.workflow_kind,
                bindings=tuple(ResourceBinding(choice.slot, choice.resource_id) for choice in request.choices if choice.slot != "main"),
                registry_snapshot=snapshot, verified_resource_ids=tuple(inspection.resource_id for inspection in inspections.values()))
            catalog_matches = {row.slot for row in assessed.assessments if row.ready}
        rows = tuple(replace(row, verification="verified", availability="available",
            compatibility="matched" if complete else "catalog_match" if row.slot in catalog_matches else row.compatibility,
            summary="Exact selected bytes verified; run readiness remains separate.") if row.slot in inspections else
            replace(row, verification="failed", availability="unavailable", summary="Selected file is unavailable or changed; locate an exact copy.") if row.slot in failures else row
            for row in report.rows)
        return replace(report, rows=rows, held=tuple("A selected companion file needs recovery or a fresh review." for _ in failures[:1]),
            qualification="adapter_qualified" if complete else "unknown"), tuple(files), digest(asdict(owner)) if owner else ""

    def verify(self, request, *, expected_registry, should_cancel=None, progress=None):
        report, files, owner_token = self._verify(request, expected_registry, should_cancel, progress)
        result = replace(report, review_id=uuid4().hex)
        if len(self._issued) >= 32:
            self._issued.pop(next(iter(self._issued)))
        self._issued[result.review_id] = (digest(asdict(result)), files, owner_token)
        return result

    def requirements(self, report):
        evidence = self._issued.get(getattr(report, "review_id", ""))
        if not isinstance(report, CompanionReadinessReport) or not evidence or evidence[0] != digest(asdict(report)) or not report.can_prepare:
            raise ModelResourceError("Verify an unchanged selected companion set before preparing it.")
        snapshot, token = self.registry.snapshot()
        if token != report.registry_token:
            raise ModelResourceError("The registry changed; verify companions again.")
        if self.guidance is not None:
            self.guidance.require_current_catalog()
        current = self.project(report.request, snapshot=snapshot, registry_token=token)
        owner = self._owner(report.request)
        if current.catalog_token != report.catalog_token or (digest(asdict(owner)) if owner else "") != evidence[2]:
            raise ModelResourceError("The companion profile changed; verify the selected set again.")
        rows = tuple(CacheResource(choice.resource_id, Path(choice.path), choice.role, choice.family)
            for choice in sorted(report.request.choices, key=lambda choice: choice.slot))
        if report.qualification != "adapter_qualified":
            return ModelRequirementSet(rows)
        key = digest({"review": asdict(report), "files": evidence[1], "owner": evidence[2]})
        if len(self._requirements) >= 32:
            self._requirements.pop(next(iter(self._requirements)))
        self._requirements[key] = (report, evidence, rows)
        return ModelRequirementSet(rows, "adapter_qualified", report.request.adapter_id, report.request.adapter_version, key)

    def resolver(self, *, should_cancel=None, progress=None):
        """Return the owner-backed callback consumed by ModelCacheService."""
        def resolve(adapter_id, adapter_version, request_fingerprint):
            selected = self._requirements.get(request_fingerprint)
            if not selected:
                raise ModelResourceError("Review and verify the exact adapter companion request in this session.")
            report, evidence, resources = selected
            if (adapter_id, adapter_version) != (report.request.adapter_id, report.request.adapter_version):
                raise ModelResourceError("The companion adapter version differs from the reviewed contract.")
            fresh, files, owner = self._verify(report.request, report.registry_token, should_cancel, progress)
            if (fresh != replace(report, review_id="") or files != evidence[1] or owner != evidence[2]
                    or fresh.qualification != "adapter_qualified"):
                raise ModelResourceError("The exact companion request or files changed; verify again.")
            return resources
        return resolve

    def acquisition_handoff(self, report, slot):
        if not isinstance(report, CompanionReadinessReport):
            raise ModelResourceError("Review the companion requirement before opening acquisition.")
        _validate(report.request)
        requirements, owner, guidance = self._profile(report.request)
        row = next((row for row in requirements if row.slot == slot), None)
        if row is None or report.catalog_token != (guidance.catalog_fingerprint if guidance else ""):
            raise ModelResourceError("The companion slot/profile changed; review it again.")
        return CompanionAcquisitionHandoff(report.request, slot, row.role, row.family,
            row.accepted_sha256, report.catalog_token, "anima_training_resource_contract" if owner else "catalog_claims" if guidance else "unknown")
