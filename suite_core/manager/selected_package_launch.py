"""Inactive exact-package verification adapter; no process or filesystem work.

An owning host supplies reviewed plans and fresh observations. This adapter
preserves the receipt for the existing runner, granting no execution approval.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PureWindowsPath, PurePosixPath
import re

from suite_core.installation import (InstalledComponent, InstallationRegistry,
    InstallationId, ComponentKind, LocalInventory, ComponentId)
from suite_core.manager.host_contracts import (DeploymentBinding, LaunchPlan,
    COMPONENT, OWNER, ARGV, aware, require)

VERSION = '0.1.0-alpha.1'
ENTRYPOINT = 'suite_core/encyclopedia/__main__.py'


def module_launch_contract(component):
    """Finite existing Python-module entries; no artifact or execution authority."""
    require(type(component) is ComponentId, 'typed_component_required')
    if component == COMPONENT:
        return ARGV, ENTRYPOINT
    if component == ComponentId('collectigen.media-organizer'):
        return ('-B', '-E', '-s', '-m', 'suite_core.media_organizer'), 'suite_core/media_organizer/__main__.py'
    raise ValueError('application_module_launch_unavailable')


def pin(value):
    require(type(value) is str and re.fullmatch('[0-9a-f]{64}', value), 'exact_hash_required')


def payload(rows):
    require(type(rows) is tuple and 1 <= len(rows) <= 4096, 'bounded_payload_pins_required')
    names = set()
    for row in rows:
        require(type(row) is tuple and len(row) == 2, 'invalid_payload_pin')
        name, digest = row
        require(type(name) is str and 0 < len(name) <= 512, 'invalid_payload_path')
        p = PurePosixPath(name)
        require(not p.is_absolute() and str(p) == name and '..' not in p.parts
            and '\\' not in name and ':' not in name and not any(ord(c) < 32 for c in name)
            and name.casefold() not in names, 'unsafe_or_duplicate_payload_path')
        names.add(name.casefold()); pin(digest)


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

    def __post_init__(self):
        item, dep, plan = self.installation, self.deployment, self.plan
        require(type(item) is InstalledComponent
            and item.kind is ComponentKind.APP and type(item.location) is LocalInventory,
            'exact_local_application_required')
        arguments, entrypoint = module_launch_contract(item.component_id)
        require(type(dep) is DeploymentBinding and type(plan) is LaunchPlan, 'typed_reviewed_binding_required')
        root = PureWindowsPath(self.suite_code_root)
        require(type(self.suite_code_root) is str and root.is_absolute()
            and re.fullmatch('[A-Za-z]:', root.drive) and '..' not in root.parts
            and str(root) == self.suite_code_root, 'canonical_local_windows_root_required')
        expected_root = str(root.joinpath(*PurePosixPath(item.location.relative_directory).parts))
        require(dep.root == expected_root, 'selected_installation_path_changed')
        payload(dep.payload_pins)
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
