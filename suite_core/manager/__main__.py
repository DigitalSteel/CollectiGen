"""Suite Manager; runtime and endpoint authority are supplied explicitly by an owning host."""
import argparse
import sys
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field
from .version import VERSION

def create_local_resource_route(*, executor=None, sampler=None):
    """Construct an inert local binding; only Refresh can start its worker."""
    from suite_core.backend_contracts import ApplicationId, HostId
    from suite_core.local_resources import sample_local_resources
    from .resource_sample_route_candidate import ResourceSampleRoute
    from .resource_executor import ResourceExecutorOwner
    owner, host = ApplicationId("suite-manager"), HostId("local")
    executor = executor if executor is not None else ResourceExecutorOwner()
    sampler = sampler if sampler is not None else lambda: sample_local_resources(owner, host)
    return ResourceSampleRoute(owner=owner, host=host, sampler=sampler, executor=executor)


def create_default_diagnostics_provider(*, root=None, executor=None, writer=None):
    """Inert existing-Manager-log review; only explicit Help actions perform I/O."""
    from .diagnostic_help_provider import create_diagnostics_help_provider
    from suite_core.diagnostics import export_preview
    # Same application-root convention as core.paths, without importing core.
    external_root = root is not None
    root = Path(__file__).resolve().parents[2] if root is None else Path(root)
    return create_diagnostics_help_provider(root, VERSION, executor=executor,
        writer=export_preview if writer is None else writer, external_root=external_root)


def tag_content_public_trust_path():
    """Select the exact packaged public resource; source retains its canonical file."""
    entry = Path(__file__).resolve()
    if getattr(sys, 'frozen', False):
        return entry.with_name('public_release_trust.json')
    return entry.parents[2] / 'data/defaults/release-trust.json'


def create_metadata_bridge(binding=None):
    """Inert unless explicit Refresh; consume an existing owner-supplied binding."""
    from .metadata_binding import ManagerMetadataBinding, MissingMetadataConfiguration
    if binding is None or type(binding) is MissingMetadataConfiguration:
        return None
    if type(binding) is not ManagerMetadataBinding:
        raise ValueError('manager_metadata_binding_required')
    from .async_lifecycle_bridge import AsyncLifecycleBridge
    return AsyncLifecycleBridge(metadata_binding=binding)




@dataclass(frozen=True)
class OwnedRuntimeAdmission:
    binding: object = field(repr=False)
    descriptor: object = field(repr=False)
    plan: object = field(repr=False)
    environment: tuple = field(repr=False)
    publication_pin: str
    descriptor_pin: str
    allowed: bool


def admit_owned_runtime(descriptor, plan, environment, binding, *, allowed):
    """Inert host admission; no default descriptor, acquisition, registry read or start."""
    from core.manager_ollama_runtime import ManagerOllamaRuntimeBinding
    from core.ollama_publication_plan import OllamaPublicationPlan
    from .owned_runtime_ui import qualification
    if (type(binding) is not ManagerOllamaRuntimeBinding or type(plan) is not OllamaPublicationPlan
            or plan is not binding.plan or type(environment) is not dict
            or not all(type(k) is str and type(v) is str for k,v in environment.items())
            or environment != binding.environment or not binding.owned_by_current_thread
            or qualification(binding, descriptor, allowed)):
        raise ValueError('exact_owned_runtime_host_admission_required')
    return OwnedRuntimeAdmission(binding,descriptor,plan,tuple(sorted(environment.items())),
        binding.publication_identity,descriptor.descriptor_pin,True)


def validate_owned_runtime(admission):
    if type(admission) is not OwnedRuntimeAdmission:
        raise ValueError('typed_owned_runtime_host_admission_required')
    fresh=admit_owned_runtime(admission.descriptor,admission.plan,dict(admission.environment),
        admission.binding,allowed=admission.allowed)
    if fresh != admission:
        raise ValueError('owned_runtime_host_admission_changed')
    return admission


_HELD_ENTRY_OWNERS = []


class EntryLifetimeOwner:
    """Retain the exact module facade across failed or partial UI entry.

    Error unwinding gets at most one second of checked drain, never a GUI join.
    Unacknowledged cleanup remains reachable on the original exception and in
    the entry registry; a host can poll_cleanup after the exact child drains.
    """
    def __init__(self, normal_entry_session=None):
        self.bridge = None
        self.owned_runtime = None
        self.update_panel = None
        self.window = None
        self.app = None
        self.state = "unacquired"
        self.closed = False
        self.cleanup_error = None
        self.normal_entry_session = normal_entry_session
        self.tag_controller = None
        self.chat_host = None
        self.direct_model_host = None
        self.reader_owner = None
        self.acquisition_executor = self.acquisition_panel = None
        self.organizer_connection_host = None
        self.organizer_acquisition_executor = self.organizer_acquisition_panel = None
        self.models_workspace_host = None
        self.tag_content_host = None

    def _drain_acquisition(self):
        if self.acquisition_panel is not None and not self.acquisition_panel.close_ready(): return False
        executor = self.acquisition_executor
        if executor is not None:
            # Retain the original worker until public thread teardown, then shut
            # down the host owner. Future completion alone cannot release entry.
            if executor.close_held: return False
            executor.begin_close()
            if executor.close_held: return False
        self.acquisition_executor = self.acquisition_panel = None
        return True

    def _drain_organizer_acquisition(self):
        panel=self.organizer_acquisition_panel
        if panel is not None and not panel.close_ready():return False
        executor=self.organizer_acquisition_executor
        if executor is not None:
            if executor.close_held:return False
            executor.begin_close()
            if executor.close_held:return False
        self.organizer_acquisition_executor=self.organizer_acquisition_panel=None
        return True

    def acquire_direct_model(self, host):
        from .direct_model_ui import DirectModelHost
        if type(host) is not DirectModelHost: raise ValueError('typed_direct_model_host_required')
        self.direct_model_host = host

    def _drain_direct_model(self):
        host = self.direct_model_host
        if host is None:return True
        host.begin_close()
        if not host.finish_close():return False
        self.direct_model_host=None
        return True

    def acquire_chat(self, host):
        from .model_chat_binding import ManagerChatHost
        if type(host) is not ManagerChatHost: raise ValueError('typed_manager_chat_host_required')
        self.chat_host = host

    def _drain_chat(self):
        host = self.chat_host
        if host is None: return True
        if not host.closed: host.begin_close()
        if not host.finish_close(): return False
        self.chat_host = None
        return True

    def acquire_tag(self, controller):
        from .tag_database_controller import TagDatabaseController
        if type(controller) is not TagDatabaseController:
            raise ValueError('explicit_host_owned_tag_controller_required')
        controller._check_owner()
        self.tag_controller = controller

    def _drain_tag_content(self):
        host = self.tag_content_host
        if host is None: return True
        if not host.close_ready(): return False
        if not host.window.close(): return False
        self.tag_content_host = None
        return True

    def _drain_tag(self):
        controller = self.tag_controller
        if controller is None: return True
        if not controller.closed: controller.begin_close()
        controller.poll()
        if controller.close_held: return False
        if controller.owns_executor:
            controller.executor.begin_close()
            if controller.executor.close_held: return False
        self.tag_controller = None
        return True

    def acquire(self, bridge):
        self.bridge = bridge
        self.state = "owned" if bridge is not None or self.owned_runtime is not None else "unacquired"
        return bridge

    def acquire_owned(self, binding):
        from core.manager_ollama_runtime import ManagerOllamaRuntimeBinding
        if type(binding) is not ManagerOllamaRuntimeBinding:
            raise ValueError('typed_owned_runtime_binding_required')
        self.owned_runtime = binding
        self.state = "owned"
        return binding

    def _drain_owned(self):
        binding = self.owned_runtime
        if binding is None: return True
        if not binding.closed:
            binding.cancel()  # Never implicitly Stop a child, even during UI failure.
            binding.poll()
            if binding.close_held or not binding.begin_close(): return False
        if binding.close_held: return False
        self.owned_runtime = None
        return True

    def poll_cleanup(self, *, budget=0):
        import time
        if self.closed:
            return True
        try:
            if not self._drain_tag_content():
                self.state = "held"
                return False
            host=self.organizer_connection_host
            if host is not None:
                host.begin_close();host.poll()
                if host.close_held:
                    self.state='held'
                    return False
                self.organizer_connection_host=None
            if not self._drain_organizer_acquisition():
                self.state='held'
                return False
            if not self._drain_acquisition():
                self.state = "held"
                return False
            if self.reader_owner is not None:
                self.reader_owner.poll()
                if self.reader_owner.close_held:
                    self.state = 'held'
                    return False
            if not self._drain_direct_model():
                self.state='held'
                return False
            if self.models_workspace_host is not None:
                self.models_workspace_host.begin_close()
                if self.models_workspace_host.close_held:
                    self.state = 'held'
                    return False
                self.models_workspace_host = None
            if not self._drain_chat():
                self.state = 'held'
                return False
            if not self._drain_tag():
                self.state = 'held'
                return False
        except BaseException as error:
            self.cleanup_error, self.state = error, 'held'
            return False
        if self.update_panel is not None:
            try:
                if not self.update_panel.permit_close():
                    self.state = 'held'
                    return False
                self.update_panel = None
            except BaseException as error:
                self.cleanup_error, self.state = error, 'held'
                return False
        session = self.normal_entry_session
        if session is not None:
            try:
                if not session.begin_cleanup():
                    self.state = 'held'
                    return False
            except BaseException as error:
                self.cleanup_error, self.state = error, 'held'
                return False
        if self.bridge is None and self.owned_runtime is None:
            if session is not None:
                try:
                    if not session.local_drained():
                        self.state = 'held'
                        return False
                except BaseException as error:
                    self.cleanup_error, self.state = error, 'held'
                    return False
            self.state, self.closed = "closed", True
            self.window = None
            self.app = None
            if self in _HELD_ENTRY_OWNERS:
                _HELD_ENTRY_OWNERS.remove(self)
            return True
        self.state = "closing"
        deadline = time.monotonic() + max(0, min(budget, 1))
        try:
            if self.bridge is not None: self.bridge.request_shutdown()
            while True:
                if self.bridge is not None:
                    self.bridge.drain(datetime.now(timezone.utc))
                    if (self.bridge.quiescent and self.bridge.finish_ui_closed() and self.bridge.closed):
                        self.bridge = None
                owned_done = self._drain_owned()
                if self.bridge is None and owned_done:
                    if session is not None and not session.local_drained():
                        self.state = 'held'
                        return False
                    self.state, self.closed = "closed", True
                    self.window = None
                    self.app = None
                    if self in _HELD_ENTRY_OWNERS:
                        _HELD_ENTRY_OWNERS.remove(self)
                    return True
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.005)
        except BaseException as cleanup_error:
            self.cleanup_error = cleanup_error
        self.state = "held"
        return False

    def tag_recovery_required(self):
        """A never-used idle default controller is not retained Tag work."""
        controller = self.tag_controller
        if self.tag_content_host is not None and self.tag_content_host.busy: return True
        return controller is not None and bool(
            controller.close_held or controller.choices
            or controller.connection_review is not None or controller.review is not None
            or controller.result is not None or controller.last_operation
            or controller.error or controller.faulted)

    def preserve_failure(self, error):
        # Cleanup errors must never replace the original construction/show/loop
        # exception, nor erase a physical or unacknowledged worker owner.
        if (self.acquisition_executor is not None and self.acquisition_executor.close_held or self.reader_owner is not None and self.reader_owner.close_held or self.tag_recovery_required() or self.chat_host is not None and self.chat_host.close_held
                or self.direct_model_host is not None and self.direct_model_host.close_held):
            # Failed entry needs one visible recovery surface even when its
            # completion arrives during unwinding. Never discard its controller.
            self.state = 'held'
        elif self.state not in ("held", "closed"):
            self.poll_cleanup(budget=1)
        try:
            error.manager_entry_owner = self
        except BaseException:
            pass
        if not self.closed:
            if self not in _HELD_ENTRY_OWNERS:
                _HELD_ENTRY_OWNERS.append(self)
            try:
                error.add_note("Manager entry cleanup is held; exact ownership is retained for checked recovery.")
            except BaseException:
                pass


def organizer_factory_for_entry(explicit_factory, normal_entry_session):
    """Join only an original root's authenticated metadata opt-in; no defaults."""
    if explicit_factory is not None or normal_entry_session is None:
        return explicit_factory
    profile=getattr(normal_entry_session,'update_profile',None)
    if type(profile) is not dict or 'component_metadata' not in profile:
        return None
    # No qualified metadata composition exists for this opt-in. Preserve the
    # original intent/session and hold rather than invent a binding or import
    # an absent module. Explicitly supplied reviewed factories remain usable.
    from .bootstrap_entry import EntryHold
    raise EntryHold('organizer_metadata_factory_not_supplied')


def main(argv=None, *, disposable_module_binding=None, settings_path=None, diagnostics_root=None, owned_runtime_admission=None, entry_probe=None, before_window_probe=None, normal_entry_session=None, manager_update_service=None, tag_database_controller=None, tag_encyclopedia=None,direct_runtime_selection=None,encyclopedia_acquisition_factory=None,organizer_connection_factory=None, shared_settings_controller=None, shared_settings_submit=None, shared_settings_close_choice=None, model_workspace_factory=None, tag_content_factory=None, organizer_acquisition_factory=None):
    if normal_entry_session is not None:
        from .bootstrap_entry import EntryHold, NormalEntrySession
        if type(normal_entry_session) is not NormalEntrySession:
            raise EntryHold('typed_native_normal_session_required')
        if owned_runtime_admission is not None:
            binding=getattr(owned_runtime_admission,'binding',None)
            if binding is None or binding.helper_capability is not None:
                raise EntryHold('single_worker_native_helper_factory_required')
    if manager_update_service is not None:
        from core.manager_update_service import ManagerUpdateService
        if type(manager_update_service) is not ManagerUpdateService or manager_update_service.current_version != VERSION:
            raise ValueError('exact_current_manager_update_service_required')
    entry_owner = EntryLifetimeOwner(normal_entry_session)
    try:
        if tag_database_controller is not None: entry_owner.acquire_tag(tag_database_controller)
        admission = None
        if owned_runtime_admission is not None:
            if type(owned_runtime_admission) is not OwnedRuntimeAdmission:
                raise ValueError("typed_owned_runtime_host_admission_required")
            entry_owner.acquire_owned(owned_runtime_admission.binding)
            admission = validate_owned_runtime(owned_runtime_admission)
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--demo", action="store_true")
        parser.add_argument("--theme", choices=("system", "dark", "light", "midnight"), default="system")
        parser.add_argument("--color-blind-mode", action="store_true")
        args = parser.parse_args(argv)
        if args.demo and manager_update_service is not None:
            raise ValueError("update_service_not_demo_authority")
        if args.demo and disposable_module_binding is not None:
            raise ValueError("disposable_provider_not_demo_authority")
        from .window import create_window, synthetic_demo, unconfigured
        from .async_lifecycle_bridge import AsyncLifecycleBridge
        from PySide6.QtWidgets import QApplication
        from qt_app.theme import apply_theme
        app = QApplication.instance() or QApplication(sys.argv[:1])
        entry_owner.app = app
        apply_theme(app, args.theme, args.color_blind_mode)
        now = datetime.now(timezone.utc)
        from suite_core.manager.current_build import load_current_build
        build_status = load_current_build(Path(__file__).resolve().parent, VERSION)
        resource_route = None if args.demo else create_local_resource_route()
        diagnostics_provider = None if args.demo else create_default_diagnostics_provider(root=diagnostics_root)
        # The portable launcher owns INI UI preferences. Native AppSettings
        # consumers share the adjacent JSON document; never reinterpret or
        # replace the admitted INI/restart binding. Explicit JSON stays exact.
        app_settings_path = settings_path
        if settings_path is not None and Path(settings_path).suffix.casefold() == ".ini":
            app_settings_path = Path(settings_path).with_suffix(".json")
        connection_settings = app_settings_path
        if connection_settings is None and not args.demo:
            from core.paths import SETTINGS_PATH
            connection_settings = SETTINGS_PATH
        if model_workspace_factory is not None:
            from .model_workspace_host import ModelsWorkspaceHost
            if type(model_workspace_factory) is ModelsWorkspaceHost:
                entry_owner.models_workspace_host = model_workspace_factory
        from .metadata_setup import create_collectigen_metadata_setup
        metadata_setup = None if args.demo or disposable_module_binding is not None else create_collectigen_metadata_setup(settings_path=app_settings_path)
        if tag_content_factory is not None:
            if (args.demo or disposable_module_binding is not None or not callable(tag_content_factory)
                    or tag_database_controller is not None or tag_encyclopedia is not None):
                raise ValueError('one_original_tag_content_host_required')
            # Reuse the complete original catalog host: one borrowed controller,
            # retained executor, monotonic generation, data review and recovery.
            from suite_core.encyclopedia.__main__ import create_window as create_tag_workspace
            tag_encyclopedia = create_tag_workspace(settings_path=connection_settings,
                default_content_factory=tag_content_factory)
            entry_owner.tag_content_host = tag_encyclopedia.catalog_host
            tag_database_controller = entry_owner.tag_content_host.controller
            entry_owner.acquire_tag(tag_database_controller)
        if tag_database_controller is None and not args.demo:
            if tag_encyclopedia is None and disposable_module_binding is None:
                from suite_core.encyclopedia.__main__ import create_window as create_tag_workspace
                from suite_core.encyclopedia.tag_data_feed_entry import standalone_tag_data_factory
                optional_factory = standalone_tag_data_factory(settings_path=Path(connection_settings),
                    trust_path=tag_content_public_trust_path())
                tag_encyclopedia = create_tag_workspace(settings_path=connection_settings,
                    optional_content_factory=optional_factory)
                entry_owner.tag_content_host = tag_encyclopedia.catalog_host
                tag_database_controller = entry_owner.tag_content_host.controller
            else:
                from .tag_database_controller import TagDatabaseController
                from .resource_executor import ResourceExecutorOwner
                tag_database_controller = TagDatabaseController(executor=ResourceExecutorOwner(),
                    apply_authorized=True, settings_path=connection_settings, owns_executor=True)
                if tag_encyclopedia is None:
                    from suite_core.encyclopedia.browser import EncyclopediaWindow
                    tag_encyclopedia = EncyclopediaWindow()
            entry_owner.acquire_tag(tag_database_controller)
        from .model_chat_binding import ManagerChatHost
        chat_host = None if metadata_setup is None else ManagerChatHost()
        if chat_host is not None: entry_owner.acquire_chat(chat_host)
        from .direct_model_ui import DirectModelHost
        if direct_runtime_selection is not None:
            if args.demo or disposable_module_binding is not None or owned_runtime_admission is not None:
                raise ValueError('single_direct_root_runtime_required')
            direct_model_host=DirectModelHost(direct_runtime_selection=direct_runtime_selection,
                normal_entry_session=normal_entry_session)
        else:
            direct_model_host = None if args.demo or normal_entry_session is not None or disposable_module_binding is not None else DirectModelHost()
        if direct_model_host is not None:entry_owner.acquire_direct_model(direct_model_host)
        if admission is not None and args.demo:
            raise ValueError("owned_runtime_not_demo_authority")
        lifecycle_bridge = entry_owner.acquire(None if disposable_module_binding is None else
            AsyncLifecycleBridge(disposable_module_binding=disposable_module_binding))
        from core.encyclopedia_connection import OwnedEncyclopedia, manager_reader_settings_path
        reader_settings = connection_settings
        if normal_entry_session is not None and not args.demo:
            reader_settings = manager_reader_settings_path(normal_entry_session.admitted.layout.code_root,
                                                          normal_entry_session.admitted.settings_path)
        reader_owner = None if args.demo else OwnedEncyclopedia(
            launch_allowed=normal_entry_session is None or getattr(normal_entry_session,'reader_selection',None) is not None,
            normal_entry_session=normal_entry_session)
        entry_owner.reader_owner = reader_owner
        encyclopedia_acquisition = encyclopedia_executor = None
        if encyclopedia_acquisition_factory is not None:
            if args.demo or not callable(encyclopedia_acquisition_factory):
                raise ValueError('explicit_non_demo_acquisition_factory_required')
            encyclopedia_acquisition, encyclopedia_executor = encyclopedia_acquisition_factory(reader_owner, reader_settings)
            from core.encyclopedia_installation import EncyclopediaInstallation
            from .resource_executor import ResourceExecutorOwner
            if (type(encyclopedia_acquisition) is not EncyclopediaInstallation
                    or type(encyclopedia_executor) is not ResourceExecutorOwner):
                raise ValueError('typed_acquisition_service_and_worker_required')
            encyclopedia_acquisition.native_health = reader_owner.native_health
            entry_owner.acquisition_executor = encyclopedia_executor
        organizer_acquisition = organizer_executor = None
        if organizer_acquisition_factory is None and normal_entry_session is not None and not args.demo:
            from .organizer_acquisition_binding import factory_for_session
            organizer_acquisition_factory=factory_for_session(normal_entry_session)
        if organizer_acquisition_factory is not None:
            if args.demo or not callable(organizer_acquisition_factory):
                raise ValueError('explicit_non_demo_organizer_acquisition_required')
            organizer_acquisition,organizer_executor=organizer_acquisition_factory(reader_owner,connection_settings)
            from core.organizer_installation import OrganizerInstallation
            from .resource_executor import ResourceExecutorOwner
            if type(organizer_acquisition) is not OrganizerInstallation or type(organizer_executor) is not ResourceExecutorOwner:
                raise ValueError('typed_organizer_acquisition_and_worker_required')
            entry_owner.organizer_acquisition_executor=organizer_executor
        organizer_connection_factory=organizer_factory_for_entry(organizer_connection_factory,normal_entry_session)
        organizer_connection_host = None
        if organizer_connection_factory is not None:
            if args.demo or not callable(organizer_connection_factory):
                raise ValueError('explicit_non_demo_organizer_connection_factory_required')
            from .organizer_connection_ui import OrganizerConnectionHost
            organizer_connection_host = organizer_connection_factory(connection_settings)
            if type(organizer_connection_host) is not OrganizerConnectionHost:
                raise ValueError('typed_organizer_connection_host_required')
            entry_owner.organizer_connection_host = organizer_connection_host
        if before_window_probe is not None: before_window_probe(entry_owner)
        window = create_window(synthetic_demo(now) if args.demo else unconfigured(now), theme=args.theme,
            color_blind_mode=args.color_blind_mode, current_build=build_status, resource_route=resource_route, diagnostics_provider=diagnostics_provider, metadata_setup=metadata_setup, lifecycle_bridge=lifecycle_bridge,
            owned_runtime=None if admission is None else admission.binding,
            owned_descriptor=None if admission is None else admission.descriptor,
            owned_runtime_allowed=admission is not None and admission.allowed,
            manager_update_service=manager_update_service, normal_entry_session=normal_entry_session,
            tag_database_controller=tag_database_controller, tag_encyclopedia=tag_encyclopedia,
            chat_host=chat_host,direct_model_host=direct_model_host,
            reader_settings_path=None if args.demo else reader_settings, reader_owner=reader_owner,
            encyclopedia_acquisition=encyclopedia_acquisition, encyclopedia_executor=encyclopedia_executor,
            organizer_connection_host=organizer_connection_host,
            organizer_acquisition=organizer_acquisition, organizer_executor=organizer_executor,
            shared_settings_controller=shared_settings_controller,
            shared_settings_submit=shared_settings_submit,
            shared_settings_close_choice=shared_settings_close_choice,
            model_workspace_factory=model_workspace_factory, tag_content_host=entry_owner.tag_content_host)
        entry_owner.window = window
        if not args.demo and settings_path is not None and Path(settings_path).suffix.casefold() == '.ini':
            from .ui_preferences import ManagerUiPreferences
            window.ui_preferences = ManagerUiPreferences(settings_path)
            window.ui_preferences.restore(window)
        if normal_entry_session is not None and getattr(normal_entry_session,'legacy_reader_profile_sha256',None) is not None:
            from .legacy_reader_handoff_ui import LegacyReaderHandoffFlow
            reader_owner.legacy_handoff=LegacyReaderHandoffFlow(window,normal_entry_session)
        entry_owner.acquisition_panel = getattr(window, "acquisition_panel", None)
        entry_owner.organizer_acquisition_panel = getattr(window, "organizer_acquisition_panel", None)
        entry_owner.update_panel = getattr(window, "manager_update_panel", None)
        window.show()
        if normal_entry_session is not None: normal_entry_session.ready()
        if entry_probe is not None: entry_probe(window, app)
        result = app.exec()
        if not entry_owner.poll_cleanup(budget=1):
            raise RuntimeError("manager_entry_cleanup_held")
        return result
    except BaseException as error:
        entry_owner.preserve_failure(error)
        if (entry_owner.organizer_acquisition_executor is not None and entry_owner.organizer_acquisition_executor.close_held
                or entry_owner.organizer_acquisition_panel is not None and entry_owner.organizer_acquisition_panel.close_held
                or entry_owner.acquisition_executor is not None and entry_owner.acquisition_executor.close_held or entry_owner.acquisition_panel is not None and entry_owner.acquisition_panel.close_held or entry_owner.reader_owner is not None and entry_owner.reader_owner.close_held or entry_owner.tag_recovery_required() or entry_owner.chat_host is not None
                and entry_owner.chat_host.close_held or entry_owner.direct_model_host is not None
                and entry_owner.direct_model_host.close_held or entry_owner.organizer_connection_host is not None
                and entry_owner.organizer_connection_host.close_held or entry_owner.models_workspace_host is not None
                and entry_owner.models_workspace_host.close_held) and not entry_owner.closed:
            from .owned_package_entry import HeldManagerEntry
            return HeldManagerEntry(error, admission).supervise()
        raise


if __name__ == "__main__":
    raise SystemExit(main())
