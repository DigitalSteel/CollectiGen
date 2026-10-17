"""Real frozen entry composition; environment/DTOs never provide native proof."""
from dataclasses import dataclass
from pathlib import Path
import argparse
import sys
from hashlib import sha256

from core.encyclopedia_connection import COMPONENT
from core.encyclopedia_update_binding import StandaloneEncyclopediaBinding, admit_standalone_binding
from core.component_acquisition import ComponentAcquisition
from core.manager_launch_owner import DeploymentOwner
from release_contract import sha256_file
from suite_core.installation import SuiteLayout, InstallMode, _path
from suite_core.discovery.component_staging import require, canonical

HOST = ('--encyclopedia-code-root', '--encyclopedia-data-root', '--encyclopedia-settings',
        '--encyclopedia-context-sha256')


@dataclass(frozen=True)
class EncyclopediaEntryBinding:
    layout: SuiteLayout
    settings_path: Path
    selected: object
    context_sha256: str
    restart_arguments: tuple
    renderer_arguments: tuple = ()

    @property
    def command(self):
        return (str(self.selected.program), *self.restart_arguments)


def reader_binding(binding):
    require(type(binding) is StandaloneEncyclopediaBinding, 'original_standalone_binding_required')
    current = binding.current()
    context = sha256(canonical({'root': str(binding.root), 'data': str(binding.data_root),
        'settings': str(binding.settings_path), 'bootstrap_sha256': binding.bootstrap_sha256,
        'trust_sha256': binding.trust_sha256, 'selected': current.selected.mapping()})).hexdigest()
    args = ('--encyclopedia-code-root', str(binding.root), '--encyclopedia-data-root', str(binding.data_root),
        '--encyclopedia-settings', str(binding.settings_path), '--encyclopedia-context-sha256', context)
    return EncyclopediaEntryBinding(SuiteLayout(binding.root, binding.data_root, InstallMode.PORTABLE),
        binding.settings_path, current.selected, context, args)


def child_binding(argv, *, trust_bytes, trust_sha256):
    """Before Qt: admit actual running immutable reader and signed root context.

    Public trust comes from the compiled child contract. Root/data/settings argv
    are locators constrained by actual executable layout and signed active state.
    Bootstrap file identity uses the existing trusted-installed-deployment model;
    it does not claim publisher authentication for mutable bootstrap bytes.
    """
    require(getattr(sys, 'frozen', False), 'frozen_original_reader_required')
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    for key in HOST:
        parser.add_argument(key, required=True)
    parsed, renderer = parser.parse_known_args(list(argv))
    root = _path(Path(parsed.encyclopedia_code_root)); data = _path(Path(parsed.encyclopedia_data_root))
    executable = _path(Path(sys.executable))
    require(executable.is_relative_to(root / 'Apps' / COMPONENT.value), 'actual_immutable_reader_layout_required')
    bootstrap = root / 'TagEncyclopedia.exe'
    binding = admit_standalone_binding(root=root, data_root=data, settings_path=parsed.encyclopedia_settings,
        bootstrap_sha256=sha256_file(bootstrap), trust_bytes=trust_bytes, trust_sha256=trust_sha256)
    admitted = reader_binding(binding)
    require(executable == admitted.selected.program and parsed.encyclopedia_context_sha256 == admitted.context_sha256,
            'actual_signed_reader_context_changed')
    # Retain exact original argv, including renderer args, for native qualification.
    return EncyclopediaEntryBinding(admitted.layout, admitted.settings_path, admitted.selected,
        admitted.context_sha256, tuple(argv), tuple(renderer)), binding


@dataclass(frozen=True)
class StandaloneEntryProfile:
    root: Path
    data_root: Path
    settings_path: Path
    bootstrap_sha256: str
    trust_bytes: bytes
    trust_sha256: str
    channel: str
    acquisition: ComponentAcquisition
    local_reader_inputs: object = None


def create_root_entry(profile):
    """Compose one lease-backed entry and admit its root custodian before work."""
    require(type(profile) is StandaloneEntryProfile and getattr(sys, 'frozen', False),
            'typed_frozen_standalone_profile_required')
    require(profile.channel in ('stable', 'beta'), 'explicit_standalone_channel_required')
    require(profile.local_reader_inputs is None, 'local_reader_first_install_only')
    root = _path(profile.root)
    require(_path(Path(sys.executable)) == root / 'TagEncyclopedia.exe', 'actual_stable_bootstrap_required')
    binding = admit_standalone_binding(root=root, data_root=profile.data_root, settings_path=profile.settings_path,
        bootstrap_sha256=profile.bootstrap_sha256, trust_bytes=profile.trust_bytes, trust_sha256=profile.trust_sha256)
    require(type(profile.acquisition) is ComponentAcquisition, 'existing_component_acquisition_required')
    zone = binding.data_root / 'Encyclopedia/UpdateCache'
    require(profile.acquisition.root != zone and profile.acquisition.root.is_relative_to(zone), 'original_component_cache_required')
    require(profile.acquisition.trust == binding.trust_bytes and profile.acquisition.trust_pin == binding.trust_sha256,
            'original_component_trust_required')
    from .bootstrap_entry import StandaloneEncyclopediaBootstrap
    owner = DeploymentOwner(root, data_root=binding.data_root).acquire()
    try:
        backend = StandaloneEncyclopediaBootstrap(binding, profile.acquisition, owner)
        backend.update_check.channel = profile.channel
        owner.enable_standalone_control(backend.subject)
        owner.begin_standalone_custody(backend.subject)
        return backend
    except BaseException as error:
        # Partial root custody retains its original owner, native ledger and lease.
        _HELD_COMPILED_ENTRIES.append(owner)
        error.encyclopedia_root_owner = owner
        raise


def create_enrollment_entry(profile):
    """Admit first-install root custody before exposing the stock installer."""
    require(type(profile) is StandaloneEntryProfile and getattr(sys, 'frozen', False),
            'typed_frozen_standalone_profile_required')
    require(profile.channel in ('stable', 'beta'), 'explicit_standalone_channel_required')
    from core.encyclopedia_enrollment import EncyclopediaEnrollmentContext, StandaloneEncyclopediaEnrollment
    context = EncyclopediaEnrollmentContext(_path(profile.root), _path(profile.data_root),
        _path(profile.settings_path), profile.bootstrap_sha256, profile.trust_bytes, profile.trust_sha256)
    context.validate_static()
    owner = DeploymentOwner(context.root, data_root=context.data_root).acquire()
    try:
        adapter = StandaloneEncyclopediaEnrollment(context, profile.acquisition, owner, channel=profile.channel)
        owner.begin_standalone_custody(adapter.subject)
        if profile.local_reader_inputs is not None:
            from core.encyclopedia_local_reader import SignedLocalReader
            adapter.installation.select_local_reader(SignedLocalReader(profile.local_reader_inputs, profile.acquisition))
        return adapter
    except BaseException as error:
        _HELD_COMPILED_ENTRIES.append(owner)
        error.encyclopedia_root_owner = owner
        raise


_HELD_COMPILED_ENTRIES = []


def start_compiled_control():
    """Retain the one stock root and ready control session; no Qt composition.

    The caller services handle_next on its retained executor and preserves this
    tuple until normal reader closure, publication/health and owner settlement.
    PROFILE=None is the existing legacy/offline path, not profile activation.
    """
    from .bootstrap_entry import from_compiled_profile
    backend = from_compiled_profile()
    if backend is None:
        return None
    from core.encyclopedia_enrollment import StandaloneEncyclopediaEnrollment
    if type(backend) is StandaloneEncyclopediaEnrollment:
        backend.owner._standalone_enrollment_owned(backend.subject)
        return backend  # Primary entry presents the existing installation service.
    return start_original_reader(backend)


def start_original_reader(backend):
    """Same retained bootstrap after installation handoff; no new owner/custodian."""
    from .bootstrap_entry import StandaloneEncyclopediaBootstrap
    require(type(backend) is StandaloneEncyclopediaBootstrap, 'original_reader_bootstrap_required')
    control = None
    try:
        if backend.owner._standalone_control_factory is None:
            backend.owner.enable_standalone_control(backend.subject)
        else:
            require(backend.subject.reader.factory is backend.owner._standalone_control_factory,
                'original_root_control_factory_changed')
        backend.launch_current()
        from .control_entry import EncyclopediaRootControl
        control = EncyclopediaRootControl(backend)
        control.accept_ready()
        return backend, control
    except BaseException as error:
        # No fresh owner, pipe replacement or ordinary Close on startup failure.
        _HELD_COMPILED_ENTRIES.append(backend)
        if control is not None:
            _HELD_COMPILED_ENTRIES.append(control)
            error.encyclopedia_root_control = control
        error.encyclopedia_root_owner = backend
        error.add_note("The original Encyclopedia root is retained for checked recovery.")
        raise


def render_compiled_child(renderer, argv):
    """Admit the inherited child before calling the existing renderer.

    The renderer owns entry_control on the same retained executor as its catalog
    and Help work. It must preserve outstanding reply IDs and finish Close only
    after visible saved completion; this wrapper provides no health boolean.
    """
    require(callable(renderer), 'existing_renderer_callable_required')
    arguments = tuple(argv)
    from . import bootstrap_entry
    profile = bootstrap_entry.PROFILE
    hosted = any(argument.split('=', 1)[0] in HOST for argument in arguments)
    if profile is None:
        require(not hosted, 'compiled_standalone_profile_unavailable')
        return renderer(argv=arguments)
    require(type(profile) is StandaloneEntryProfile and hosted,
            'compiled_original_child_profile_required')
    from .control_entry import EncyclopediaChildControl
    control = EncyclopediaChildControl.from_inherited_entry(arguments,
        trust_bytes=profile.trust_bytes, trust_sha256=profile.trust_sha256)
    try:
        from .tag_data_feed_entry import standalone_tag_data_factory
        optional = standalone_tag_data_factory(settings_path=control.admitted.settings_path,
            trust_bytes=profile.trust_bytes)
        return renderer(argv=control.admitted.renderer_arguments,
            settings_path=control.admitted.settings_path, entry_control=control,
            optional_content_factory=optional)
    except BaseException as error:
        # Keep the original control and any late reply; never reopen or silently
        # discard it because a renderer could not finish startup/close.
        _HELD_COMPILED_ENTRIES.append(control)
        error.encyclopedia_child_control = control
        error.add_note("The original Encyclopedia child control is retained for checked cleanup.")
        raise
