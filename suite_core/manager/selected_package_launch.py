"""Inactive exact-package verification adapter; no process or filesystem work.

An owning host supplies reviewed plans and fresh observations. This adapter
preserves the receipt for the existing runner, granting no execution approval.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PureWindowsPath, PurePosixPath
import re
import json
from hashlib import sha256

from suite_core.installation import (InstalledComponent, InstallationRegistry,
    InstallationId, ComponentKind, LocalInventory, ComponentId)
from suite_core.manager.host_contracts import (DeploymentBinding, LaunchPlan,
    COMPONENT, OWNER, ARGV, aware, require)
from suite_core.discovery.component_payload_policy import (
    ComponentPayloadPolicy, ORGANIZER_INTERPRETER_PAYLOAD_POLICY, select_payload_policy,
    payload_relative)

VERSION = '0.1.0-alpha.1'
ENTRYPOINT = 'suite_core/encyclopedia/__main__.py'


def module_launch_contract(component, *, interpreter_admission=None, package_root=None, qualification=None):
    """Finite existing Python-module entries; no artifact or execution authority."""
    require(type(component) is ComponentId, 'typed_component_required')
    if component == COMPONENT:
        arguments, entrypoint = ARGV, ENTRYPOINT
    elif component == ComponentId('collectigen.media-organizer'):
        arguments, entrypoint = ('-B', '-E', '-s', '-m', 'suite_core.media_organizer'), 'suite_core/media_organizer/__main__.py'
    else:
        raise ValueError('application_module_launch_unavailable')
    if interpreter_admission is not None:
        require(type(interpreter_admission) is InterpreterPackageAdmission
            and component == interpreter_admission.component_id, 'interpreter_admission_binding_required')
        require(type(package_root) is str, 'canonical_package_root_required')
        root = PureWindowsPath(package_root)
        require(root.is_absolute() and re.fullmatch('[A-Za-z]:', root.drive)
            and '..' not in root.parts and str(root) == package_root, 'canonical_package_root_required')
        arguments += ('--runtime-profile', interpreter_admission.capability_profile,
            '--runtime-root', str(root / 'Runtime'))
    if qualification is not None:
        require(interpreter_admission is not None and type(qualification) is DisposableQualification,
            'explicit_interpreter_qualification_required')
        qualification.check_package_root(package_root)
        arguments += ('--profile-root', qualification.profile_root)
    return arguments, entrypoint


def pin(value):
    require(type(value) is str and re.fullmatch('[0-9a-f]{64}', value), 'exact_hash_required')


def _windows_root(value):
    require(type(value) is str, 'canonical_qualification_root_required')
    path = PureWindowsPath(value)
    require(path.is_absolute() and re.fullmatch('[A-Za-z]:', path.drive)
        and '..' not in path.parts and str(path) == value, 'canonical_qualification_root_required')
    return path


@dataclass(frozen=True)
class DisposableQualification:
    """Explicit host-owned scratch scope, never a normal-user profile selection."""
    workspace_root: str
    system_root: str
    marker_sha256: str
    normal_close_policy: str = 'legacy-all-top-level'
    expected_window_title: str = ''
    expected_window_class: str = ''

    def __post_init__(self):
        workspace, system = _windows_root(self.workspace_root), _windows_root(self.system_root)
        require(workspace != system and workspace not in system.parents and system not in workspace.parents,
            'separate_qualification_workspace_required')
        pin(self.marker_sha256)
        require(self.normal_close_policy in ('legacy-all-top-level', 'unique-visible-primary-v1', 'unique-identified-primary-v2'),
            'qualification_close_policy_unknown')
        names = (self.expected_window_title, self.expected_window_class)
        require(all(type(v) is str and len(v) <= 128 and not any(ord(c) < 32 for c in v) for v in names),
            'qualification_window_identity_invalid')
        require(all(names) if self.normal_close_policy == 'unique-identified-primary-v2' else names == ('', ''),
            'qualification_window_identity_required')

    @property
    def marker_path(self):
        return str(PureWindowsPath(self.workspace_root) / '.synthetic-component-root.json')

    @property
    def profile_root(self):
        return str(PureWindowsPath(self.workspace_root) / 'profile')

    def check_package_root(self, package_root):
        package, workspace = _windows_root(package_root), PureWindowsPath(self.workspace_root)
        require(package != workspace and package not in workspace.parents and workspace not in package.parents,
            'qualification_workspace_overlaps_package')

    def environment(self, package_root):
        self.check_package_root(package_root)
        package = PureWindowsPath(package_root)
        workspace, system = PureWindowsPath(self.workspace_root), PureWindowsPath(self.system_root)
        python = package / 'Runtime/python'
        qt = python / 'Lib/site-packages/PySide6'
        home = workspace / 'home'
        return tuple(sorted({
            'PATH': ';'.join(map(str, (python, package / 'Runtime/exiftool', qt,
                python / 'Lib/site-packages/shiboken6', system / 'System32'))),
            'SYSTEMROOT': str(system), 'WINDIR': str(system), 'SystemDrive': system.drive,
            'TEMP': str(workspace / 'temp'), 'TMP': str(workspace / 'temp'),
            'USERPROFILE': str(home), 'APPDATA': str(home / 'AppData/Roaming'),
            'LOCALAPPDATA': str(home / 'AppData/Local'),
            'QT_QPA_PLATFORM': 'windows', 'QT_PLUGIN_PATH': str(qt / 'plugins'),
            'QT_QPA_PLATFORM_PLUGIN_PATH': str(qt / 'plugins/platforms'),
        }.items()))


def qualification_fingerprint(plan, deployment, qualification, pins, links):
    """Bind the exact profile/environment and evidence before permit creation."""
    require(type(qualification) is DisposableQualification, 'typed_qualification_required')
    value = {'component': plan.component_id.value, 'owner': plan.owner.value,
        'root': plan.root, 'program': plan.program, 'arguments': plan.arguments,
        'archive': plan.archive_pin, 'entrypoint': plan.entrypoint_pin,
        'version': deployment.version, 'source_plan': deployment.source_plan_pin,
        'payload': deployment.payload_pins, 'pins': pins, 'links': links,
        'profile': qualification.profile_root, 'marker': qualification.marker_sha256,
        'environment': qualification.environment(plan.root), 'policy': 'disposable-organizer-qualification-v1'}
    if qualification.normal_close_policy != 'legacy-all-top-level':
        value['normal_close_policy'] = qualification.normal_close_policy
    if qualification.normal_close_policy == 'unique-identified-primary-v2':
        value['application_window_identity'] = (qualification.expected_window_title, qualification.expected_window_class)
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True)
class InterpreterPackageAdmission:
    """Trusted caller expectations, never profile or command claims from a manifest."""
    component_id: ComponentId
    os_name: str
    architecture: str
    runtime_kind: str
    capability_profile: str
    policy: ComponentPayloadPolicy

    def __post_init__(self):
        require(type(self.component_id) is ComponentId
            and self.runtime_kind == 'interpreter-portable'
            and self.capability_profile == 'organizer-image', 'interpreter_profile_required')
        selected = select_payload_policy(self.component_id, self.os_name,
            self.architecture, self.runtime_kind)
        require(self.policy is selected is ORGANIZER_INTERPRETER_PAYLOAD_POLICY,
            'finite_interpreter_policy_required')


def payload(rows, *, interpreter_admission=None, component_id=None):
    maximum = 4096
    if interpreter_admission is not None:
        require(type(interpreter_admission) is InterpreterPackageAdmission
            and component_id == interpreter_admission.component_id,
            'interpreter_admission_binding_required')
        # The bootstrap inventory excludes itself from its member count.
        maximum = interpreter_admission.policy.max_inventory_members + 1
    require(type(rows) is tuple and 1 <= len(rows) <= maximum, 'bounded_payload_pins_required')
    names = set()
    for row in rows:
        require(type(row) is tuple and len(row) == 2, 'invalid_payload_pin')
        name, digest = row
        require(type(name) is str and 0 < len(name) <= 512, 'invalid_payload_path')
        p = PurePosixPath(name)
        require(not p.is_absolute() and str(p) == name and '..' not in p.parts
            and '\\' not in name and ':' not in name and not any(ord(c) < 32 for c in name)
            and name.casefold() not in names, 'unsafe_or_duplicate_payload_path')
        if interpreter_admission is not None:
            payload_relative(name, payload_policy=interpreter_admission.policy)
        names.add(name.casefold()); pin(digest)


def validate_package_payload(deployment, interpreter_admission=None):
    """Pure immutable launch evidence; parsing/admission of bytes belongs to the host."""
    payload(deployment.payload_pins, interpreter_admission=interpreter_admission,
        component_id=deployment.component_id)
    fixed_program = str(PureWindowsPath(deployment.root) / 'Runtime' / 'python' / 'python.exe')
    if interpreter_admission is None:
        require(PureWindowsPath(deployment.program) != PureWindowsPath(fixed_program),
            'interpreter_admission_required')
        return
    require(deployment.program == fixed_program, 'fixed_interpreter_program_required')
    required = ('BOOTSTRAP_INVENTORY.json', 'RUNTIME_MANIFEST.json',
        'Runtime/python/python.exe', 'Runtime/python/python313._pth',
        'Runtime/python/collectigen-runtime.json', 'Runtime/exiftool/exiftool.exe',
        'requirements-organizer-image-runtime.lock')
    require(all(name in dict(deployment.payload_pins) for name in required),
        'interpreter_payload_evidence_required')


@dataclass(frozen=True)
class ReviewedPackageLaunch:
    """Supplied identity evidence, never an install/launch or approval claim."""
    installation: InstalledComponent
    suite_code_root: str
    deployment: DeploymentBinding
    plan: LaunchPlan
    archive_path: str
    source_plan_path: str
    verification_pins: tuple
    verification_links: tuple
    reviewed_at: datetime
    review_reference: str
    interpreter_admission: InterpreterPackageAdmission | None = None
    qualification: DisposableQualification | None = None

    def __post_init__(self):
        item, dep, plan = self.installation, self.deployment, self.plan
        require(type(item) is InstalledComponent
            and item.kind is ComponentKind.APP and type(item.location) is LocalInventory,
            'exact_local_application_required')
        require(type(dep) is DeploymentBinding and type(plan) is LaunchPlan, 'typed_reviewed_binding_required')
        root = PureWindowsPath(self.suite_code_root)
        require(type(self.suite_code_root) is str and root.is_absolute()
            and re.fullmatch('[A-Za-z]:', root.drive) and '..' not in root.parts
            and str(root) == self.suite_code_root, 'canonical_local_windows_root_required')
        expected_root = str(root.joinpath(*PurePosixPath(item.location.relative_directory).parts))
        require(dep.root == expected_root, 'selected_installation_path_changed')
        arguments, entrypoint = module_launch_contract(item.component_id,
            interpreter_admission=self.interpreter_admission, package_root=expected_root,
            qualification=self.qualification)
        validate_package_payload(dep, self.interpreter_admission)
        require(entrypoint in dict(dep.payload_pins), 'source_entrypoint_required')
        entrypoint_pin = dict(dep.payload_pins)[entrypoint]
        pin(entrypoint_pin); pin(plan.entrypoint_pin)
        require(dict(dep.payload_pins).get(item.location.manifest) == item.location.manifest_sha256,
            'selected_manifest_hash_changed')
        pin(dep.archive_pin); pin(dep.source_plan_pin); pin(plan.fingerprint)
        require(dep.component_id == item.component_id and dep.version == item.software_version
            and dep.arguments == arguments and plan.component_id == item.component_id and plan.owner == OWNER
            and plan.root == dep.root and plan.program == dep.program and plan.arguments == dep.arguments
            and plan.archive_pin == dep.archive_pin and plan.entrypoint_pin == entrypoint_pin
            and plan.snapshot is None and plan.shell is False and plan.open_supported is False,
            'reviewed_package_plan_changed')
        program = PureWindowsPath(dep.program)
        require(program.is_absolute() and re.fullmatch('[A-Za-z]:', program.drive)
            and '..' not in program.parts and str(program) == dep.program, 'canonical_program_required')
        require(type(self.verification_pins) is tuple and 1 <= len(self.verification_pins) <= 8192,
            'complete_verification_pins_required')
        seen = set()
        for row in self.verification_pins:
            require(type(row) is tuple and len(row) == 2 and type(row[0]) is str
                and 0 < len(row[0]) <= 2048 and row[0].casefold() not in seen, 'invalid_verification_pin')
            seen.add(row[0].casefold()); pin(row[1])
        measured = dict(self.verification_pins)
        for name, digest in ((self.archive_path, dep.archive_pin), (self.source_plan_path, dep.source_plan_pin)):
            require(type(name) is str, 'canonical_package_evidence_path_required')
            path = PureWindowsPath(name)
            require(path.is_absolute() and re.fullmatch('[A-Za-z]:', path.drive)
                and '..' not in path.parts and str(path) == name
                and measured.get(name) == digest, 'package_evidence_path_hash_changed')
        for name, digest in dep.payload_pins:
            require(measured.get(str(PureWindowsPath(dep.root).joinpath(*PurePosixPath(name).parts))) == digest,
                'payload_verification_closure_missing')
        require(dep.program in measured, 'program_verification_missing')
        require(type(self.verification_links) is tuple and len(self.verification_links) <= 128
            and all(type(row) is tuple and len(row) == 2 and all(type(v) is str and 0 < len(v) <= 2048 for v in row)
                for row in self.verification_links), 'invalid_reviewed_link_closure')
        aware(self.reviewed_at)
        require(type(self.review_reference) is str and 0 < len(self.review_reference) <= 512,
            'independent_identity_review_reference_required')
        if self.qualification is not None:
            require(measured.get(self.qualification.marker_path) == self.qualification.marker_sha256,
                'qualification_marker_pin_required')
            require(plan.fingerprint == qualification_fingerprint(plan, dep, self.qualification,
                self.verification_pins, self.verification_links), 'qualification_fingerprint_changed')


class SelectedPackageVerifier:
    """Bound verification seam for an existing owner/runner; disabled by default.

    observe must be a bound method returning an unchanged owning-host receipt
    with plan/observed_at/pins/links fields. No runner, gate or permit is created.
    The registry and reviewed selection are compared before every observation.
    Mutation or staleness never silently chooses a different package.
    """
    def __init__(self, *, enabled=False, selection=None, registry_reader=None, observer=None):
        self.enabled = enabled is True
        self.selection = selection
        self.registry_reader = registry_reader
        self.observer = observer
        self._registry = getattr(registry_reader, 'read', None)
        self._observe = getattr(observer, 'verify', None)
        self._providers = ((registry_reader, 'read', self._registry), (observer, 'verify', self._observe))
        self._selection = selection
        self._last_time = None

    def verify(self, now):
        require(self.enabled is True, 'selected_package_provider_disabled')
        aware(now)
        require(self._last_time is None or now >= self._last_time, 'package_clock_regression')
        require(type(self.selection) is ReviewedPackageLaunch and self.selection is self._selection,
            'reviewed_selection_changed')
        selected = self._selection
        require(timedelta(0) <= now - selected.reviewed_at <= timedelta(seconds=30), 'reviewed_selection_stale_or_future')
        providers = self._providers
        def providers_match():
            require(self._providers is providers and self.registry_reader is providers[0][0]
                and self.observer is providers[1][0] and self._registry is providers[0][2]
                and self._observe is providers[1][2], 'package_provider_binding_changed')
            for owner, name, method in providers:
                current = getattr(owner, name, None)
                require(callable(method) and getattr(method, '__self__', None) is owner
                    and getattr(method, '__func__', None) is not None
                    and getattr(current, '__self__', None) is owner
                    and getattr(current, '__func__', None) is method.__func__, 'package_provider_binding_changed')
        providers_match()
        def registry_matches():
            registry = self._registry()
            require(type(registry) is InstallationRegistry, 'typed_current_registry_required')
            matches = tuple(row for row in registry.entries if row.installation_id == selected.installation.installation_id)
            require(matches == (selected.installation,), 'selected_registry_entry_changed')
        registry_matches()
        providers_match()
        receipt = self._observe(now)
        providers_match()
        aware(receipt.observed_at)
        require(timedelta(0) <= now - receipt.observed_at <= timedelta(seconds=30), 'package_observation_stale_or_future')
        require(type(receipt.plan) is LaunchPlan and receipt.plan == selected.plan
            and type(receipt.pins) is tuple and receipt.pins == selected.verification_pins
            and type(receipt.links) is tuple and receipt.links == selected.verification_links,
            'fresh_package_identity_changed')
        registry_matches()
        providers_match()
        require(self.enabled is True and self.selection is selected, 'reviewed_selection_changed')
        self._last_time = now
        return receipt
