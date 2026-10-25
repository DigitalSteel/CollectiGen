"""Explicit post-hook binding; no discovery, migration, setting reads or writes."""
from dataclasses import dataclass
from pathlib import Path
import argparse
import sys

HOST_OPTIONS = ("--suite-mode", "--suite-code-parent", "--suite-data-parent", "--manager-settings-path")


@dataclass(frozen=True)
class FrozenEntryBinding:
    layout: object
    settings_path: Path
    diagnostics_root: Path
    manager_arguments: tuple[str, ...]
    restart_arguments: tuple[str, ...]


def binding(argv, *, executable, frozen):
    """Validate supplied normal-launch authority before importing Manager main.

    Point-in-time alias checks do not qualify hostile races or SDK housekeeping.
    The host must authorize this executable/root before it dispatches SDK hooks.
    """
    from suite_core.installation import InstallMode, SuiteLayout, _path
    if frozen is not True:
        raise ValueError("approved_frozen_manager_required")
    argv = tuple(argv)
    if any(type(value) is not str for value in argv):
        raise ValueError("explicit_host_arguments_required")
    host = []
    remaining = []
    seen = set()
    index = 0
    while index < len(argv):
        token = argv[index]
        key, separator, value = token.partition("=")
        if key in HOST_OPTIONS:
            if key in seen:
                raise ValueError("duplicate_host_binding_argument")
            seen.add(key)
            if not separator:
                index += 1
                if index >= len(argv):
                    raise ValueError("missing_host_binding_value")
                value = argv[index]
            if not value or value.startswith("-"):
                raise ValueError("empty_or_missing_host_binding_value")
            host.extend((key, value))
        elif key.startswith(("--suite", "--manager-settings")):
            raise ValueError("unknown_or_abbreviated_host_argument")
        else:
            remaining.append(token)
        index += 1
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--suite-mode", required=True, choices=("installed", "portable"))
    parser.add_argument("--suite-code-parent", required=True)
    parser.add_argument("--suite-data-parent", required=True)
    parser.add_argument("--manager-settings-path", required=True)
    args = parser.parse_args(host)
    code = _path(Path(args.suite_code_parent))
    data = _path(Path(args.suite_data_parent))
    layout = SuiteLayout(code, data, InstallMode(args.suite_mode))
    exe = _path(Path(executable))
    expected = code / "Apps" / "Manager" / "current" / "CollectiGenManager.exe"
    if exe != expected:
        raise ValueError("exact_manager_payload_identity_required")
    settings = _path(Path(args.manager_settings_path))
    if settings == data or not settings.is_relative_to(data):
        raise ValueError("manager_settings_outside_approved_data")
    if not settings.is_file():
        raise ValueError("existing_manager_settings_file_required")
    diagnostics = _path(data / "Manager")
    return FrozenEntryBinding(layout, settings, diagnostics, tuple(remaining), argv)


def validate_normal_launch(argv, *, executable):
    """Candidate full pre-spawn argv/settings admission; no Qt/SDK/process work.

    This does not authenticate a handoff or qualify an owned runtime package.
    Native owner/channel admission and existing exact package selection remain
    separate. The original argv is retained for the launch/handshake digest.
    """
    from dataclasses import replace
    from suite_core.installation import _path
    import re
    admitted = binding(argv, executable=executable, frozen=True)
    if admitted.layout.mode != 'portable' or admitted.layout.data_root != admitted.layout.code_root / 'Data':
        raise ValueError('portable_deployment_data_root_required')
    keys = {'--owned-ollama-selection', '--owned-ollama-selection-sha256',
            '--owned-ollama-system-root', '--allow-reviewed-owned-ollama'}
    direct_keys={'--direct-runtime-reference','--direct-runtime-reference-sha256'}
    seen = set()
    for token in admitted.manager_arguments:
        key = token.partition('=')[0]
        if key in keys|direct_keys:
            if key in seen:
                raise ValueError('duplicate_owned_selection_argument')
            seen.add(key)
    selector = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    selector.add_argument('--owned-ollama-selection')
    selector.add_argument('--owned-ollama-selection-sha256')
    selector.add_argument('--owned-ollama-system-root')
    selector.add_argument('--allow-reviewed-owned-ollama', action='store_true')
    selector.add_argument('--direct-runtime-reference')
    selector.add_argument('--direct-runtime-reference-sha256')
    manager = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    manager.add_argument('--demo', action='store_true')
    manager.add_argument('--theme', choices=('system','dark','light','midnight'), default='system')
    manager.add_argument('--color-blind-mode', action='store_true')
    try:
        selected, normal = selector.parse_known_args(admitted.manager_arguments)
        args = manager.parse_args(normal)
    except SystemExit as error:
        raise ValueError('invalid_normal_launch_arguments') from error
    if seen & direct_keys:
        if seen!=direct_keys or args.demo or not re.fullmatch('[0-9a-f]{64}',selected.direct_runtime_reference_sha256):
            raise ValueError('exact_direct_reference_selection_required')
        if _path(Path(selected.direct_runtime_reference))!=admitted.layout.code_root/'_bootstrap/direct-runtime.json':
            raise ValueError('package_local_direct_reference_required')
    elif seen:
        if seen != keys or not selected.allow_reviewed_owned_ollama:
            raise ValueError('complete_explicit_owned_package_selection_required')
        if args.demo:
            raise ValueError('owned_runtime_not_demo_authority')
        if not re.fullmatch('[0-9a-f]{64}', selected.owned_ollama_selection_sha256):
            raise ValueError('exact_selection_hash_required')
        path = _path(Path(selected.owned_ollama_selection))
        _path(Path(selected.owned_ollama_system_root))
        if not path.is_file():
            raise ValueError('existing_owned_selection_file_required')
    return replace(admitted, manager_arguments=tuple(normal))


def selected_direct_runtime(admitted):
    selector=argparse.ArgumentParser(add_help=False,allow_abbrev=False)
    selector.add_argument('--direct-runtime-reference');selector.add_argument('--direct-runtime-reference-sha256')
    selected,_=selector.parse_known_args(admitted.restart_arguments)
    if selected.direct_runtime_reference is None:return None
    from core.manager_direct_admission import load_direct_reference
    return load_direct_reference(admitted.layout.code_root,{'schema':1,'relative':'_bootstrap/direct-runtime.json',
        'sha256':selected.direct_runtime_reference_sha256},observe_model=False)


def selected_owned_package(admitted):
    """Reuse exact host selection and pin; never choose a package by default."""
    selector=argparse.ArgumentParser(add_help=False,allow_abbrev=False)
    selector.add_argument('--owned-ollama-selection')
    selector.add_argument('--owned-ollama-selection-sha256')
    selector.add_argument('--owned-ollama-system-root')
    selector.add_argument('--allow-reviewed-owned-ollama',action='store_true')
    selected,_=selector.parse_known_args(admitted.restart_arguments)
    if not selected.allow_reviewed_owned_ollama:return None
    from .launcher_main import OWNED_HELPER_PRODUCTION_QUALIFIED
    if not OWNED_HELPER_PRODUCTION_QUALIFIED:
        raise ValueError('owned_helper_production_qualification_pending')
    from core.manager_ollama_selection import select_owned_package
    return select_owned_package(selected.owned_ollama_selection,selected.owned_ollama_selection_sha256,
        layout=admitted.layout,system_root=selected.owned_ollama_system_root)


def launch_manager(*, normal_entry_session=None, argv=None, executable=None, manager_update_service=None, tag_database_controller=None, tag_encyclopedia=None):
    """Enter managed normal UI only with the already native-qualified session."""
    from .bootstrap_entry import EntryHold, NormalEntrySession
    if type(normal_entry_session) is not NormalEntrySession or normal_entry_session.state != 'offered':
        raise EntryHold('qualified_normal_entry_session_required')
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    admitted = validate_normal_launch(arguments, executable=sys.executable if executable is None else executable)
    if admitted != normal_entry_session.admitted:
        raise EntryHold('normal_entry_complete_binding_changed')
    if manager_update_service is not None:
        from core.manager_update_service import ManagerUpdateService
        if type(manager_update_service) is not ManagerUpdateService:
            raise EntryHold('explicit_manager_update_service_required')
        manager_update_service.bind_normal_entry(admitted)
    selection=selected_owned_package(admitted)
    if selection is not None:
        if manager_update_service is not None:
            raise EntryHold('owned_helper_update_binding_unqualified')
        from .owned_package_entry import launch_selected_package,HeldManagerEntry
        result=launch_selected_package(selection,allowed=True,manager_arguments=admitted.manager_arguments,
            settings_path=admitted.settings_path,diagnostics_root=admitted.diagnostics_root,
            normal_entry_session=normal_entry_session,
            tag_database_controller=tag_database_controller,tag_encyclopedia=tag_encyclopedia)
        return result.supervise() if type(result) is HeldManagerEntry else result
    from suite_core.manager.__main__ import main
    options = dict(settings_path=admitted.settings_path,
                   diagnostics_root=admitted.diagnostics_root, normal_entry_session=normal_entry_session)
    if manager_update_service is not None:
        options['manager_update_service'] = manager_update_service
    if tag_database_controller is not None:
        options.update(tag_database_controller=tag_database_controller,tag_encyclopedia=tag_encyclopedia)
    direct=selected_direct_runtime(admitted)
    if direct is not None:options['direct_runtime_selection']=direct
    from .encyclopedia_acquisition_binding import factory_for_session
    acquisition_factory = factory_for_session(normal_entry_session)
    if acquisition_factory is not None and '--demo' not in admitted.manager_arguments:
        options['encyclopedia_acquisition_factory'] = acquisition_factory
    return main(list(admitted.manager_arguments), **options)
