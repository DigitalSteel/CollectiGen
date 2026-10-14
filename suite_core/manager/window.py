"""Developer Manager with explicit detached observations and qualified runtime authority."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .version import VERSION, DISPLAY_VERSION, DATE
from core.component_release_identity import display_component_version
from .resource_executor import begin_resource_close, resource_close_held
TITLE = "CollectiGen Suite Manager"
ICON_PATH = Path(__file__).resolve().parent / "assets" / "collectigen.ico"
from core.application_categories import (APP_CATEGORIES, CATEGORY_DESCRIPTIONS, APPLICATION_GROUPS,
    ApplicationFilterState, select_category, matches_category, matching_catalog, application_roles, related_application_entries)
NOTES = "# What's New - a.0.4.1 (embedded 0.4.1-alpha.1) - 2026-10-12\n\n- Fixed a startup error in the previous alpha candidate that could prevent Manager from opening.\n\nThis unsigned correction has not established the full reader workflow or clean-machine compatibility. The default package continues to connect existing approved reader packages. Automatic Encyclopedia acquisition and native updates remain unavailable. Preserve earlier copies and recovery records.\n\n# What's New - a.0.4.0 (embedded 0.4.0-alpha.1) - 2026-10-12\n\n- Applications search and status filters now include Tag Encyclopedia. Its card shows the next supported action, with connection and release tools under Options.\n- Dependencies shows supplied status and blockers, with links to the existing setup or recovery workspace. Returning from that workspace lets you review its status again.\n- Fixed first-party release discovery rejecting valid GitHub download links.\n\nThe default launch connects existing approved reader packages. Automatic acquisition requires a separately admitted original-root profile and remains unavailable by default. Saved reader changes may require a Manager restart before Launch. Native installation, ordinary launch/close, updates and clean-machine compatibility remain separately qualified.\n\n# What's New - a.0.3.2 (embedded 0.3.2-alpha.1) - 2026-10-11\n\n- Fixed a reader setup settings mismatch that could prevent saving an Encyclopedia connection in portable Manager. Saved connections use the companion settings file while preserving the original startup configuration.\n\nConnect an already installed Encyclopedia from Applications, save it, then restart Manager before Launch. Automatic reader download/install and native application updates remain unavailable. This unsigned local candidate has not established clean-machine compatibility.\n\n# What's New - a.0.3.1 (embedded 0.3.1-alpha.1) - 2026-10-11\n\n- After saving a new Encyclopedia selection, Manager clearly asks you to restart before launching it. Launch stays unavailable until that exact reader is admitted at startup.\n- Switching to another reader, including a changed package in the same folder, keeps the restart guidance visible. Reopening the saved connection after restart restores Launch for the admitted reader.\n\nThis unsigned local candidate connects already installed readers. Automatic Encyclopedia download/install remains unavailable; native application updates, public release and clean-machine compatibility remain separately qualified.\n\n# What's New - a.0.3.0 (embedded 0.3.0-alpha.1) - 2026-10-11\n\n- Applications > Encyclopedia lets you connect an already installed standalone reader. Review its folder, save the connection, then restart Manager to launch that selected reader.\n- Open saved connection restores your selection. Manager keeps reader work supervised while closing; Cancel Close returns to the same window.\n- Databases continues to offer separate connection review, content preview, Apply, recovery and Reload for an enrolled Tag catalog.\n\nThis unsigned local candidate does not download or install Encyclopedia automatically. Native application updates, public release and clean-machine compatibility remain separately qualified.\n\n# What's New - a.0.2.16 (embedded 0.2.1-alpha.16) - 2026-10-11\n\n- Databases lets you select an approved local Tag source and create a new catalog or open an enrolled catalog. Review and confirm the connection, then preview and apply the content separately.\n- Reopen the saved catalog, recover an interrupted import, or reload saved changes after a snapshot refresh fails. Closing retains admitted work until it finishes.\n- Capability summaries describe the selected catalog and keep unavailable behavior and its reasons visible. Connection settings remain accessible beside the selected catalog.\n\nThis is an unreleased source checkpoint. Portable startup and clean-machine behavior remain separately qualified. Native application updates and public release remain unavailable.\n\n# What's New - a.0.2.15 (embedded 0.2.1-alpha.15) - 2026-10-10\n\n- Direct GGUF chat now allows replies up to 512 tokens. The shorter 32-token limit belongs to explicit proof runs.\n- This unsigned manual candidate uses ordinary entries and starts with empty conversations. Review the fixed installed files, Load model, then Open Chat to send your own message.\n\nThe admitted Qwen/runtime profile remains CPU only: 2 threads, 4096 context and one slot. Reply limits are not adjustable here. Synthetic streaming checks passed at 512 tokens; real-model 512-token behavior, ordinary manual startup/navigation/Close, GPU operation and clean-machine compatibility remain unverified. The earlier exact-profile native lifecycle proof used 32 tokens. Native updates and public release remain unavailable.\n\n# What's New - a.0.2.14 (embedded 0.2.1-alpha.14) - 2026-10-10\n\n- Standalone Manager entry offers an existing GGUF file directly, with a separately selected installed llama-server. No model copy, Ollama import or download.\n- Review checks the selected files and server version. Load uses a bounded CPU-only plan; Chat and Stop keep the original process owned through completion.\n\nThe selected Qwen GGUF completed a bounded CPU-only frozen Manager load, reply, reload and owned shutdown through the normal root with the retained llama-server runtime. An admitted direct-runtime profile fixes the reviewed files. The alpha14 manual artifact uses 2 CPU threads, 4096 context, one slot and a fixed maximum of 32 reply tokens; these limits are not adjustable there. Ordinary manual startup, navigation and Close were not accepted; exact development actors were cleaned up separately and its failed ledger was preserved. GPU operation and other models/runtimes are not qualified. Existing-backend chat source integration remains separate; native updates and public release remain unavailable.\n\n# What's New - a.0.2.13 (embedded 0.2.1-alpha.13) - 2026-10-10\n\n- Open Chat from Applications > Ollama after refreshing a configured backend and choosing an already loaded model.\n- Replies appear as they arrive. Stop cancels this request; New Chat clears the conversation. Messages stay in memory.\n- Closing Manager waits for chat work to finish safely. After an interrupted request, refresh the backend before sending again.\n\nSource integration is under verification. Direct GGUF loading, packaged inference, native updates and public distribution remain separately qualified.\n\n# What's New - a.0.2.12 (embedded 0.2.1-alpha.12) - 2026-10-10\n\n- If Manager encounters a problem while approved Tag work is pending, the same window keeps its status, cancellation and recovery controls available.\n- Close waits for the original Tag operation to settle; Cancel Close keeps its saved outcome and original failure available for review.\n\nAvailable only with an explicitly supplied approved Tag host. The default entry has no catalog connected. Native updates and public distribution remain separately qualified.\n\n# What's New - a.0.2.11 (embedded 0.2.1-alpha.11) - 2026-10-10\n\n- With an explicitly connected approved Tag catalog, Databases offers preview, reviewed Apply, cancellation, recovery and saved-result reload.\n- Closing waits for Tag work to finish. Cancel Close keeps its progress and recovery controls available.\n- Media Organizer details show its current source version and date.\n\nThe default entry has no approved Tag catalog connected. Combining this Tag host with owned failed-entry recovery requires separate qualification. Automatic updates and public distribution remain separate.\n\n# What's New - a.0.2.10 (embedded 0.2.1-alpha.10) - 2026-10-10\n\n- When a local runner exits during startup, Manager keeps recovery available so Stop can finish its cleanup.\n- Runner status distinguishes an exited runner awaiting cleanup from a running or fully stopped runner.\n- Startup diagnostics retain the original failure to help explain an unsuccessful start.\n- Repeated reviewed starts use the same retained deployment owner and separate attempt identities.\n- Failed-entry recovery keeps one visible Manager surface, original controls and cleanup evidence; codes are available in Details.\n\nUnreleased fixes for explicitly qualified local runner packages. The exact private existing-profile lifecycle acceptance passed.\nNew installations, model/Hermes actions, automatic updates and distribution remain separately qualified.\n\n# What's New - a.0.2.9 (embedded 0.2.1-alpha.9) - 2026-10-09\n\n- Starting a reviewed local Ollama runner no longer fails while Manager is finishing its startup checks.\n- A running runner is shown correctly. Stop remains available, and a stopped runner can be started again.\n- Closing Manager explains when the runner still needs to be stopped. Once Stop finishes, Manager closes normally without leaving its runner behind.\n\nAvailable only with an explicitly configured, qualified local runner package.\nNew installations, model installation and Hermes actions remain unavailable until\nseparately qualified. Automatic updates and public distribution remain separate.\n\n# What's New - a.0.2.8 (embedded 0.2.1-alpha.8) - 2026-10-09\n\n- Owned Ollama starts can reuse an explicitly reviewed, verified local model cache. Unknown, incomplete or changed cache contents remain blocked.\n- Cancelled or expired starts stop before launching a process.\n- Stop closes the runner Manager started together with its inference processes, preserving recovery ownership and cached model files.\n\nThis is an unreleased source checkpoint. These runtime changes do not enable cached-model controls in a packaged application. Model installation and Hermes actions remain unavailable; automatic updates and distribution remain separately qualified.\n\n# What's New - a.0.2.7 (embedded 0.2.1-alpha.7) - 2026-10-09\n\n- With an explicitly supplied qualified Ollama package and installation permission, Applications > Model runners offers installation review, progress, Cancel and recovery.\n- Review local key creation before starting the owned runner. Stop targets only the runner Manager started; external connections remain independent.\n- Failed status delivery retains the runner for explicit Stop and reconciliation instead of leaving controls permanently busy. Closing waits for retained work and process ownership.\n\nThis is an unreleased source checkpoint. The default entry has no qualified package and keeps installation unavailable. Production host/package selection, packaged and real GUI qualification remain pending. Model installation and Hermes agent actions remain unavailable until separately qualified.\n\n# What's New - a.0.2.6 (embedded 0.2.1-alpha.6) - 2026-10-09\n\n- Find Ollama connection tools and model observations in Applications > Model runners. Servers opens the same workspace.\n- The connection editor uses clear Save and Cancel actions. Saving a connection keeps metadata access separate; saved choices can be reloaded and reconciled explicitly.\n- Model summaries and inventory remain readable in narrower or shorter windows, with scrolling to reach the full workspace.\n\nThis is an unreleased source checkpoint. Install new remains unavailable; packaged and real-provider qualification remain pending.\n\n# What's New - a.0.2.5 (embedded 0.2.1-alpha.5) - 2026-10-08\n\n- Dashboard separates applications, connections, this computer and jobs into concise summaries, with further information under Details.\n- Light and dark themes keep application symbols, status badges and controls readable. Cards use more consistent spacing and action placement.\n- Application descriptions wrap in narrower windows. Expanded Details stay inside their cards and scroll to the end; collapsing restores the compact layout.\n- Everday is described as a dating and simulation game with AI characters in Games.\n- Normal navigation keeps demo and debug controls out of the way.\n- Servers opens correctly. Unavailable measurements and connections remain clearly marked.\n\n# What's New - a.0.2.4 (embedded 0.2.1-alpha.4) - 2026-10-08\n\n- Applications groups the existing catalog by task with a category list, while retaining search and installation-status filters.\n- Application cards show declared roles. Agent and model-runner groups stay empty unless the supplied catalog identifies matching applications.\n- Details links to declared related components when matching catalog information is available; compatibility and installation remain unknown.\n\n# What's New - a.0.2.3 (embedded 0.2.1-alpha.3) - 2026-10-08\n\n- Servers brings local computer measurements and existing backend connections into a separate section with compact cards and expandable Details.\n- Dashboard summarizes observations and links to Servers. Expired or unavailable measurements remain unknown; held jobs stay visible without an invented server assignment.\n- The sidebar selects sections. The command bar offers supported actions for the current section, with View, Help and command search still available.\n- Connect existing Ollama opens the metadata connection editor. Saving a connection and refreshing metadata remain separate actions; this does not install Ollama.\n\n# What's New - a.0.2.2 (embedded 0.2.1-alpha.2) - 2026-10-08\n\n- Applications offers compact cards with readable requirements and expandable Details.\n- Catalog review fits narrower windows, and Settings groups connection, model, appearance and build information.\n- Local backend setup keeps deliberate Save separate from Refresh; appearance remains session-only.\n\nThis is an unreleased source candidate. Normal component installation and managed execution remain unavailable by default. Native saved-workflow, recovery and portable qualification remain pending.\n\n# What's New - a.0.2.1 (embedded 0.2.1-alpha.1) - 2026-10-08\n\n- Application cards are smaller and easier to scan, with category icons and Details for requirements and availability.\n- Catalog review controls fit narrower windows; disconnected catalogs keep review details out of the default view.\n- Settings groups backend connections, model observations, appearance and build details consistently.\n- Dependencies separates build information from supplied component requirements without implying that setup actions are available.\n\n# What's New - a.0.2.0 (embedded 0.2.0-alpha.1) - 2026-10-07\n\n- Local Ollama setup saves connection settings explicitly and refreshes metadata only when requested. A delayed or refused refresh keeps previous dated results visible and offers retry.\n\nNo new user-visible component lifecycle capability is enabled by this source proposal. Normal component installation and managed sessions remain unavailable; their saved-workflow and safe-close recovery still need native acceptance.\n\n# What's New - a.0.1.22 (embedded 0.1.0-alpha.22) - 2026-10-08\n\n- No new user-visible capability in this development checkpoint. Media Organizer remains In development; application launches remain unavailable through the default Manager entry.\n\n# What's New - a.0.1.21 (embedded 0.1.0-alpha.21) - 2026-10-07\n\n- Expand or collapse the sidebar with one click; the View menu uses the same control.\n- Applications shows Media Organizer and Everday as In development. Databases shows the default Tag Catalog. These cards stay separate from installed and downloadable components; Install explains why it is unavailable.\n- Page headings and spacing are consistent, empty pages use clearer wording, and the color-blind palette shows On or Off.\n- Portable build details display the packaged runtime information when valid build metadata is included.\n\n# What's New - a.0.1.20 (embedded 0.1.0-alpha.20) - 2026-10-06\n\n\n- Help offers Review diagnostics before saving an explicitly selected ZIP. No information is uploaded.\n- Preview existing safe Manager diagnostics and runtime versions, choose what to include, cancel an unwanted preview, and save to a new filename. Unavailable records and save failures stay visible.\n- No diagnostics are read until Preview is chosen. Startup capture stays disabled; raw application logs are excluded.\n\n# What's New - a.0.1.19 (embedded 0.1.0-alpha.19) - 2026-10-06\n\n\n- Dashboard offers Refresh local resources to read this computer's CPU, RAM and NVIDIA GPU usage. Nothing is sampled until you click Refresh.\n- Measurements show their source and time; expired or unavailable values stay unknown. Cancel discards the result, and close waits for the measurement worker to finish.\n- Demo remains synthetic. Existing Databases preview controls remain available.\n\n# What's New - a.0.1.18 (embedded 0.1.0-alpha.18) - 2026-10-06\n\n\n- Databases uses the same prominent heading and content spacing as Dashboard.\n- With a configured preview provider, request a catalog preview, review content versions and protected edits, or discard the preview. Apply remains unavailable.\n\n# What's New — a.0.1.17 (embedded 0.1.0-alpha.17) — 2026-10-06\n\n- Databases uses the same prominent heading and content spacing as Dashboard.\n- Request and discard background catalog previews when an approved host route is supplied. Review content versions and protected edits; Apply remains unavailable.\n- Canceled Ollama refreshes no longer reappear as retained models after a later failed refresh.\n- A timed-out inventory refresh can be retried explicitly. When refresh is unavailable, Settings explains how to recover.\n- Settings shows installed and loaded Ollama models separately, with metadata health, observation dates and freshness.\n- Current build details remain separate from release history.\n"




@dataclass(frozen=True)
class DetachedView:
    report: object
    synthetic: bool = False
    release_notes: tuple = ()
    jobs: tuple = ()
    installation_registry: object = None
    supported_apps: tuple = ()


@dataclass(frozen=True)
class SupportedAppPreview:
    """Presentation-only supplied catalog assertion; no installed-state authority."""
    component_id: object
    title: str
    source_ref: str
    observed_at: datetime
    optional_addon_instances: tuple = ()
    categories: tuple = ()
    description: str = ""
    capabilities: tuple = ()
    record_kind: str = "supported catalog"
    candidate_package: object = None


@dataclass(frozen=True)
class DevelopmentComponent:
    key: str
    title: str
    description: str
    categories: tuple
    requirement: str
    source_version: str = ""
    release_date: str = ""


from suite_core.media_organizer.version import SOFTWARE_VERSION as ORGANIZER_SOURCE_VERSION, RELEASE_DATE as ORGANIZER_SOURCE_DATE

DEVELOPMENT_APPLICATIONS = (
    DevelopmentComponent("media-organizer", "Media Organizer", "Organize images, search your collection and prepare training data.", ("Productivity",), "Requires a compatible application package and image collection setup.", ORGANIZER_SOURCE_VERSION, ORGANIZER_SOURCE_DATE),
    DevelopmentComponent("everday", "Everday", "A dating and simulation game with AI characters.", ("Games",), "Requires a compatible application package, a configured backend and a supported model."),
)
DEFAULT_TAG_CATALOG = DevelopmentComponent("default-tag-catalog", "Default Tag Catalog", "Shared Tags and categories for organizing and describing images.", (), "Requires a compatible content package and a reviewed catalog installation route.")


def matching_development(status, category, query):
    if status not in ("All entries", "In development"): return ()
    query = query.strip().casefold()
    return tuple(item for item in DEVELOPMENT_APPLICATIONS
        if (category == "All categories" or category in item.categories)
        and (not query or query in " ".join((item.title, item.description)).casefold()))


def unconfigured(now):
    from suite_core.backend_contracts import ApplicationId
    from suite_core.inventory_view import compose_inventory
    return DetachedView(compose_inventory(ApplicationId("suite-manager"), now=now))


def synthetic_demo(now):
    from suite_core.backend_contracts import ApplicationId, BackendId, CapabilityId, ConnectionId, HostId, StableId, ResourceGroup, ResourceGroupId, ResourceTelemetry, JobState
    from suite_core.installation import (ComponentId, InstallationId, InstalledComponent, ComponentKind,
        InterfaceBinding, LocalInventory, InstallationRegistry, Resolution, ResolveState)
    from suite_core.inventory_view import ComponentObservation, compose_inventory
    from suite_core.registry.status_view import ConfiguredConnection, MetadataSnapshot, SnapshotStore, ResourceSnapshot
    from suite_core.discovery.swarm_readonly import DiscoveryState
    owner = ApplicationId("suite-manager")
    runtime = InstalledComponent(InstallationId("synthetic-python-runtime"), ComponentId("synthetic.python-runtime"),
        "3.13.15", ComponentKind.RUNTIME, frozenset({CapabilityId("runtime.python")}),
        (InterfaceBinding("runtime_interface_version", 1),),
        LocalInventory("Runtimes/synthetic-python", "MODULE_MANIFEST.json", "b" * 64))
    entry = InstalledComponent(InstallationId("synthetic-encyclopedia"), ComponentId("collectigen.tag-encyclopedia"),
        "0.1.0-alpha.4", ComponentKind.MODULE, frozenset({CapabilityId("knowledge.read")}),
        (InterfaceBinding("snapshot_interface_version", 1),),
        LocalInventory("Modules/synthetic-encyclopedia", "MODULE_MANIFEST.json", "a" * 64),
        dependencies=(runtime.installation_id,))
    registry = InstallationRegistry((entry, runtime))
    component = ComponentObservation(registry.entries[0], Resolution(owner, ResolveState.MISSING_LOCAL,
        entry.installation_id, reason="synthetic-no-lookup"), StableId("synthetic-locator"), now, timedelta(seconds=30))
    group = ResourceGroup(ResourceGroupId("synthetic-gpu"), HostId("synthetic-host"), StableId("synthetic-device"), True)
    connection = ConfiguredConnection(owner, BackendId("suite-manager-image"), ConnectionId("suite-manager-connection"),
        group.host_id, (group,), frozenset({CapabilityId("image.generate")}))
    second = ConfiguredConnection(owner, BackendId("synthetic-second-channel"), ConnectionId("synthetic-second-connection"),
        group.host_id, (group,), frozenset({CapabilityId("image.generate")}))
    store = SnapshotStore((connection, second))
    store.observe_metadata(MetadataSnapshot(owner, connection.connection_ref, DiscoveryState.READY,
        now, timedelta(seconds=30), 2, 1, 8, source_ref=StableId("synthetic-metadata")), now)
    store.observe_resource(ResourceSnapshot(ResourceTelemetry(group.id, now, timedelta(seconds=30),
        free_vram_bytes=6 * 1024**3, total_vram_bytes=12 * 1024**3,
        free_ram_bytes=20 * 1024**3, total_ram_bytes=32 * 1024**3, active_jobs=1, queued_jobs=1),
        StableId("synthetic-complete-host-telemetry")), now)
    runtime_observation = ComponentObservation(runtime, Resolution(owner, ResolveState.MISSING_LOCAL,
        runtime.installation_id, reason="synthetic-no-lookup"), StableId("synthetic-runtime-locator"), now, timedelta(seconds=30))
    report = compose_inventory(owner, status_rows=store.rows(owner, now), components=(component, runtime_observation), now=now)
    from dataclasses import replace
    from suite_core.examples.fake_job_view import inputs
    from suite_core.registry.job_view import project_queue
    sources = []
    for name, state in (("demo-running", JobState.RUNNING), ("demo-queued", JobState.QUEUED)):
        _, source = inputs("suite-manager", name, state)
        status = replace(source.status, observed_at=now)
        sources.append(replace(source, submitted=status, status=status, control=None))
    jobs = project_queue(tuple(sources), frozenset({owner}), now=now)
    return DetachedView(report, True, ((entry.component_id.value, entry.software_version, DATE,
        "Synthetic demonstration only", "# Synthetic Encyclopedia note\nInjected demo prose; no module notes were read."),), jobs, registry,
        (SupportedAppPreview(ComponentId("synthetic.main-app"), "Synthetic main application",
            "synthetic-supported-catalog", now, categories=("AI Generation",),
            description="Create images from a prompt.", capabilities=("image.generate",)),
         SupportedAppPreview(ComponentId("synthetic.analysis"), "Synthetic Analysis", "synthetic-supported-catalog", now,
            categories=("AI Analysis",), description="Tag, caption and understand existing images.", capabilities=("image.analyze",)),
         SupportedAppPreview(ComponentId("synthetic.organizer"), "Synthetic Organizer", "synthetic-supported-catalog", now,
            categories=("Productivity",), description="Organize files and review your collection.", capabilities=("collection.browse",)),
         SupportedAppPreview(ComponentId("synthetic.chat"), "Synthetic Chat", "synthetic-supported-catalog", now,
            categories=("LLM",), description="Chat with a language model.", capabilities=("text.chat",))))


def view_for_candidate(observation):
    from .candidate_inventory import CandidatePackageObservation
    if type(observation) is not CandidatePackageObservation:
        raise ValueError("readonly_candidate_observation_required")
    from suite_core.installation import ComponentId
    from dataclasses import replace
    return replace(unconfigured(observation.observed_at), supported_apps=(SupportedAppPreview(
        ComponentId(observation.component_id), "Tag Encyclopedia", "accepted-developer-archive-inspection",
        observation.observed_at, description="Browse approved Tag publications, search and inspect identity decisions.",
        record_kind="developer candidate", candidate_package=observation),),
        release_notes=((observation.component_id, observation.software_version,
            dict(observation.release_dates).get(observation.software_version), "Unsigned developer candidate", observation.notes),))


def create_window(view, *, theme="system", color_blind_mode=False, candidate_inspector=None, offline_previews=(), lifecycle_controller=None, close_policy=None, close_clock=None, lifecycle_bridge=None, current_build=None, database_catalogs=(), database_review=None, database_preview_route=None, resource_route=None, diagnostics_provider=None, metadata_setup=None, owned_runtime=None, owned_descriptor=None, owned_runtime_allowed=False, manager_update_service=None, normal_entry_session=None, tag_database_controller=None, tag_encyclopedia=None, chat_host=None, direct_model_host=None, reader_settings_path=None, reader_owner=None, encyclopedia_acquisition=None, encyclopedia_executor=None):
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSplitter,
        QStackedWidget, QListWidgetItem, QTableView, QTextBrowser, QComboBox, QDialog, QDialogButtonBox,
        QPushButton, QScrollArea, QFrame, QLineEdit, QToolButton, QGroupBox, QSizePolicy)
    from qt_app.navigation_icons import PageNavigationList, NavigationItemDelegate, navigation_icon
    from suite_core.manager.command_bar import CommandBar, UiCommand, CommandSearchDialog, bind_source
    from qt_app.browse_components import IncrementalResultModel, IncrementalResultRow
    from qt_app.settings_navigation import SettingsNavigator
    from suite_core.manager.ui_cards import ResponsiveCardGrid, card_style, workspace_layout, workspace_heading, development_card, unavailable_action_style, compact_action, disclosure_panel

    from suite_core.manager.database_presentation import (
        DatabaseCatalogChoice, project_database_review, PREVIEW_DISABLED,
    )
    if type(database_catalogs) is not tuple or len(database_catalogs)>32 or any(type(choice) is not DatabaseCatalogChoice for choice in database_catalogs):
        raise ValueError("Supply at most 32 explicit approved catalog choices")
    if database_review is not None and not database_catalogs:
        raise ValueError("A supplied database review requires its selected controller")
    from suite_core.manager.database_preview_route import DatabasePreviewRoute
    from suite_core.database_actions import DatabaseAction
    if database_preview_route is not None and (type(database_preview_route) is not DatabasePreviewRoute
            or tuple(item.controller for item in database_preview_route.inputs) != tuple(choice.controller for choice in database_catalogs)):
        raise ValueError("Host preview route must own exactly the supplied catalog controllers")
    from suite_core.manager.tag_database_controller import TagDatabaseController
    if tag_database_controller is not None:
        if type(tag_database_controller) is not TagDatabaseController:
            raise TypeError("Explicit host-owned Tag controller required")
        if database_catalogs or database_review is not None or database_preview_route is not None:
            raise ValueError("Native Tag pane and legacy preview route cannot own Databases together")
    from suite_core.manager.current_build import project_status
    build_status = project_status(current_build, VERSION)

    class ManagerWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self.tag_database_pane = None
            self.reader_panel = self.reader_dialog = None
            if reader_settings_path is not None and not view.synthetic:
                from .encyclopedia_connection_ui import EncyclopediaConnectionPanel
                self.reader_dialog = QDialog(self)
                self.reader_dialog.setWindowTitle("Tag Encyclopedia connection")
                self.reader_dialog.resize(1000, 440)
                reader_layout = QVBoxLayout(self.reader_dialog)
                self.reader_panel = EncyclopediaConnectionPanel(reader_settings_path,
                    normal_entry_session=normal_entry_session, parent=self.reader_dialog, owner=reader_owner)
                reader_layout.addWidget(self.reader_panel)
                self.reader_panel.timer.timeout.connect(self.render_library)
                self.reader_panel.timer.timeout.connect(self.render_dependency_status)
            self.acquisition_panel = self.acquisition_dialog = None
            if encyclopedia_acquisition is not None:
                from core.encyclopedia_installation import EncyclopediaInstallation
                from .resource_executor import ResourceExecutorOwner
                from .encyclopedia_acquisition_ui import EncyclopediaAcquisitionPanel
                if (type(encyclopedia_acquisition) is not EncyclopediaInstallation
                        or type(encyclopedia_executor) is not ResourceExecutorOwner
                        or self.reader_panel is None or view.synthetic):
                    raise ValueError('explicit_encyclopedia_acquisition_host_required')
                self.acquisition_dialog = QDialog(self)
                self.acquisition_dialog.setWindowTitle('Get or update Tag Encyclopedia')
                self.acquisition_dialog.resize(1000, 650)
                acquisition_layout = QVBoxLayout(self.acquisition_dialog)
                self.acquisition_panel = EncyclopediaAcquisitionPanel(encyclopedia_acquisition,
                    encyclopedia_executor, parent=self.acquisition_dialog,
                    connection_changed=self.reader_panel.reload_saved)
                acquisition_layout.addWidget(self.acquisition_panel)
                self.acquisition_panel.timer.timeout.connect(self.render_library)
                self.acquisition_panel.timer.timeout.connect(self.render_dependency_status)
                self.reader_panel.reader_settled = self.acquisition_panel.reconcile_launch
            from .model_chat_binding import ManagerChatHost
            if chat_host is not None and type(chat_host) is not ManagerChatHost:
                raise ValueError('typed_manager_chat_host_required')
            self.chat_host = chat_host or (ManagerChatHost() if metadata_setup is not None and not view.synthetic else None)
            from .direct_model_ui import DirectModelHost
            if direct_model_host is not None and (type(direct_model_host) is not DirectModelHost
                    or view.synthetic or normal_entry_session is not None and (
                        direct_model_host.normal_entry_session is not normal_entry_session
                        or direct_model_host.direct_runtime_selection is None)):
                raise ValueError('direct_model_native_root_helper_not_qualified')
            self.direct_model_host=direct_model_host
            unavailable_action_style(self)
            from .resource_sample_route_candidate import ResourceSampleRoute
            from .resource_executor import ResourceExecutorOwner
            if resource_route is not None and (type(resource_route) is not ResourceSampleRoute
                    or type(resource_route.executor) is not ResourceExecutorOwner):
                raise ValueError("manager_owned_resource_route_required")
            self.resource_route = resource_route
            self.resource_panel = None
            self.resource_dashboard_binding = None
            from .diagnostic_help_candidate import DiagnosticsHelpFlow
            self.diagnostic_help_flow = DiagnosticsHelpFlow(diagnostics_provider)
            self.diagnostics_dialog = None
            self.current_build = build_status
            from suite_core.manager.lifecycle_controller import LifecycleController
            from suite_core.manager.async_lifecycle_bridge import AsyncLifecycleBridge
            if lifecycle_bridge is not None and (type(lifecycle_bridge) is not AsyncLifecycleBridge or lifecycle_controller is not None):
                raise ValueError("exclusive_typed_async_bridge_required")
            if lifecycle_controller is not None and type(lifecycle_controller) is not LifecycleController:
                raise ValueError("typed_detached_lifecycle_controller_required")
            self.lifecycle_bridge = lifecycle_bridge
            self.lifecycle_controller = lifecycle_bridge if lifecycle_bridge is not None else (
                lifecycle_controller if lifecycle_controller is not None else LifecycleController())
            from suite_core.manager.close_policy import CloseCoordinator
            from time import monotonic
            if close_policy is not None and type(close_policy) is not CloseCoordinator:
                raise ValueError("typed_close_policy_required")
            if close_clock is not None and not callable(close_clock):
                raise ValueError("injected_monotonic_clock_required")
            self.close_policy = close_policy if close_policy is not None else CloseCoordinator()
            self.close_clock = close_clock if close_clock is not None else monotonic
            self.close_retry_queued = False
            self.close_timer = QTimer(self); self.close_timer.setInterval(250)
            self.close_timer.timeout.connect(self.reconsider_close)
            self.cancel_close_button = QPushButton("Cancel pending close", self)
            self.cancel_close_button.clicked.connect(self.cancel_pending_close)
            self.statusBar().addPermanentWidget(self.cancel_close_button)
            self.cancel_close_button.hide()
            self.lifecycle_timer = QTimer(self)
            self.lifecycle_timer.setInterval(250)
            self.lifecycle_timer.timeout.connect(self.poll_lifecycle)
            self.lifecycle_timer.timeout.connect(self.poll_database_preview)
            self.lifecycle_timer.timeout.connect(self.poll_local_resources)
            self.lifecycle_timer.timeout.connect(self.poll_diagnostics_help)
            if diagnostics_provider is not None or resource_route is not None or database_preview_route is not None or self.lifecycle_controller.fixture_enabled or (self.lifecycle_bridge is not None and self.lifecycle_bridge.enabled):
                self.lifecycle_timer.start()
            self.setWindowTitle(TITLE)
            self.setWindowIcon(QIcon(str(ICON_PATH)))
            self.resize(1200, 780)
            root = QWidget(self); self.setCentralWidget(root)
            layout = QVBoxLayout(root); layout.setContentsMargins(0, 0, 0, 0)
            brand = QHBoxLayout(); brand.setContentsMargins(12, 6, 12, 2)
            mark = QLabel(); mark.setPixmap(self.windowIcon().pixmap(28, 28)); brand.addWidget(mark)
            heading = QLabel(TITLE); heading.setStyleSheet("font-size:18px;font-weight:600")
            brand.addWidget(heading); brand.addStretch()
            brand.addWidget(QLabel(DISPLAY_VERSION)); layout.addLayout(brand)
            self.build_status_label = QLabel(self.current_build.lines()[0])
            self.build_status_label.setObjectName("currentBuildStatus")
            self.build_status_label.setContentsMargins(16, 0, 16, 0)
            self.build_status_label.setWordWrap(True)
            self.build_status_label.setToolTip("\n".join(self.current_build.lines()))
            self.build_status_label.hide()  # Full build evidence remains in Settings / Help.
            self.bar = CommandBar(self); layout.addWidget(self.bar)
            self.banner = QLabel(); self.banner.setWordWrap(True)
            self.banner.setContentsMargins(12, 4, 12, 4); layout.addWidget(self.banner)
            splitter = QSplitter(); layout.addWidget(splitter, 1)
            self.navigation = PageNavigationList(); self.navigation.setObjectName("navigation")
            self.navigation.setItemDelegate(NavigationItemDelegate(self.navigation))
            self.navigation.setMinimumWidth(64)
            self.navigation.setAccessibleName("Manager sections")
            self.navigation_panel = QWidget(); navigation_layout = QVBoxLayout(self.navigation_panel)
            navigation_layout.setContentsMargins(4, 4, 4, 4)
            self.sidebar_toggle = QToolButton(); self.sidebar_toggle.setObjectName("navigationModeButton")
            self.sidebar_toggle.setCheckable(True); self.sidebar_toggle.setFocusPolicy(Qt.StrongFocus)
            self.sidebar_toggle.setText("\u2039"); self.sidebar_toggle.setAccessibleName("Collapse sidebar")
            self.sidebar_toggle.setToolTip("Collapse sidebar; section names remain available as tooltips.")
            self.sidebar_toggle.clicked.connect(self.toggle_compact)
            navigation_layout.addWidget(self.sidebar_toggle); navigation_layout.addWidget(self.navigation, 1)
            splitter.addWidget(self.navigation_panel)
            self.stack = QStackedWidget(); splitter.addWidget(self.stack); splitter.setSizes((220, 980))
            self.summary = QTextBrowser(); self.summary.setReadOnly(True)
            self.summary.setFrameShape(QFrame.NoFrame)
            self.applications = QTableView(); self.applications.setSelectionBehavior(QTableView.SelectRows)
            self.application_model = IncrementalResultModel(("Application", "Configured version", "Locator state", "Freshness"), parent=self)
            self.applications.setModel(self.application_model)
            applications_page = QWidget(); applications_layout = workspace_layout(applications_page)
            applications_layout.addWidget(workspace_heading("Applications")); applications_layout.addWidget(self.summary)
            self.summary.setMaximumHeight(48)
            self.disposable_panel = QFrame()
            self.disposable_panel.setObjectName('disposableModuleLifecycle')
            module_layout = QVBoxLayout(self.disposable_panel)
            module_layout.addWidget(QLabel('Disposable module â€” local test only'))
            self.disposable_status = QLabel('Checking disposable owner; installation is unavailable until ready.')
            self.disposable_status.setWordWrap(True); module_layout.addWidget(self.disposable_status)
            module_actions = QHBoxLayout()
            self.disposable_offer = QComboBox()
            if self.lifecycle_bridge is not None:
                for offer_id, version in self.lifecycle_bridge.module_versions:
                    self.disposable_offer.addItem(f'{offer_id} Â· {version}', offer_id)
            self.disposable_offer.setAccessibleName('Disposable target version')
            self.disposable_primary = QPushButton('Install selected version')
            self.disposable_primary.clicked.connect(self.request_disposable_primary)
            self.disposable_refresh = QPushButton('Refresh status')
            self.disposable_refresh.clicked.connect(lambda: self.request_disposable_action('poll'))
            self.disposable_cancel = QPushButton('Cancel operation')
            self.disposable_cancel.clicked.connect(self.cancel_disposable_operation)
            for control in (self.disposable_offer, self.disposable_primary, self.disposable_refresh, self.disposable_cancel):
                module_actions.addWidget(control)
            module_layout.addLayout(module_actions)
            self.disposable_panel.setVisible(self.lifecycle_bridge is not None and self.lifecycle_bridge.module_enabled)
            applications_layout.addWidget(self.disposable_panel)
            self.disposable_operation = 'install'
            self.render_disposable_module()
            library_controls = QHBoxLayout()
            self.library_filter = QComboBox(); self.library_filter.addItems(("All entries", "Installed", "Available", "Updates", "In development"))
            self.library_filter.setAccessibleName("Application status filter")
            self.application_filter_state = ApplicationFilterState()
            self.application_categories = SettingsNavigator(accessible_name="Application categories")
            category_side = self.application_categories.section_list.parentWidget()
            category_side.setMinimumWidth(145); category_side.setMaximumWidth(180)
            self.application_categories.layout().setSpacing(10)
            self.application_categories.search.hide()  # Keep the existing application search as the one query field.
            self.application_categories.empty.setText("No application categories available.")
            application_catalog_body = QWidget()
            catalog_layout = QVBoxLayout(application_catalog_body)
            catalog_layout.setContentsMargins(0, 0, 0, 0)
            catalog_layout.setSpacing(12)
            self.application_categories.set_content(application_catalog_body)
            for key, label, description in APPLICATION_GROUPS:
                self.application_categories.add_section(key, label,
                    lambda selected=key: self.select_application_category(selected), keywords=(description,))
            self.application_categories.sync_section("All categories")
            self.categories_toggle = QToolButton(); self.categories_toggle.setText("Categories")
            self.categories_toggle.setCheckable(True); self.categories_toggle.setChecked(True)
            self.categories_toggle.setAccessibleName("Show application categories")
            self.categories_toggle.toggled.connect(category_side.setVisible)
            applications_layout.addWidget(self.categories_toggle, 0, Qt.AlignLeft)
            applications_layout.addWidget(self.application_categories, 1)
            self.library_search = QLineEdit(); self.library_search.setPlaceholderText("Search name, description or capability")
            self.library_search.setAccessibleName("Search supported applications"); self.library_search.setClearButtonEnabled(True)
            clear = QPushButton("Clear filters"); clear.clicked.connect(self.clear_library_filters)
            for control in (self.library_filter, self.library_search, clear): library_controls.addWidget(control)
            catalog_layout.addLayout(library_controls)
            self.library_status = QLabel(); self.library_status.setWordWrap(True); catalog_layout.addWidget(self.library_status)
            self.category_description = QLabel(); self.category_description.setWordWrap(True)
            catalog_layout.addWidget(self.category_description)
            library_scroll = QScrollArea(); library_scroll.setWidgetResizable(True)
            self.library_body = ResponsiveCardGrid(); self.library_cards = self.library_body.grid
            library_scroll.setWidget(self.library_body); catalog_layout.addWidget(library_scroll, 1)
            self.library_filter.currentIndexChanged.connect(self.render_library)
            self.library_search.textChanged.connect(self.render_library)
            self.applications.setMaximumHeight(130)
            self.app_observation_heading = QLabel("Configured applications")
            applications_layout.addWidget(self.app_observation_heading)
            applications_layout.addWidget(self.applications)
            dashboard_page = QWidget(); dashboard_layout = QVBoxLayout(dashboard_page)
            dashboard_layout.setContentsMargins(0, 0, 0, 0)
            self.dashboard_slot = QVBoxLayout(); dashboard_layout.addLayout(self.dashboard_slot, 1)
            self.components = QTableView(); self.components.setSelectionBehavior(QTableView.SelectRows)
            self.component_model = IncrementalResultModel(("Component", "Configured version", "Locator state", "Freshness"), parent=self)
            self.components.setModel(self.component_model)
            modules_page = QWidget(); modules_layout = QVBoxLayout(modules_page)
            self.unclassified_modules_page = modules_page
            modules_layout.addWidget(QLabel("Modules")); self.module_status = QLabel(); self.module_status.setWordWrap(True)
            modules_layout.addWidget(self.module_status); modules_layout.addWidget(self.components, 1)
            self.backends = QTextBrowser(); self.backends.setReadOnly(True)
            self.databases = QTextBrowser(); self.databases.setReadOnly(True)
            self.dependencies = QTextBrowser(); self.dependencies.setReadOnly(True)
            databases_page = QWidget(); databases_page_layout = QVBoxLayout(databases_page)
            databases_page_layout.setContentsMargins(0, 0, 0, 0)
            database_body = QWidget(); database_layout = workspace_layout(database_body)
            self.database_heading = workspace_heading("Databases")
            database_layout.addWidget(self.database_heading)
            intro = QLabel("Browse Tag catalogs and review content changes. Installation is not available yet.")
            intro.setWordWrap(True); intro.setTextFormat(Qt.PlainText); database_layout.addWidget(intro)
            self.database_development_grid = ResponsiveCardGrid()
            self.database_development_grid.set_cards([development_card(DEFAULT_TAG_CATALOG)])
            database_layout.addWidget(self.database_development_grid)
            self.database_status = QLabel(); self.database_status.setWordWrap(True)
            self.database_status.setTextFormat(Qt.PlainText); database_layout.addWidget(self.database_status)
            review_group = QGroupBox("Catalog review")
            if database_catalogs:
                database_layout.addWidget(review_group)
            else:
                database_layout.addWidget(disclosure_panel("Catalog review details", review_group))
            database_layout.addStretch(1)
            database_layout = QVBoxLayout(review_group); database_layout.setSpacing(8)
            self.database_route_state = None
            self.database_preview_route = database_preview_route
            self.database_catalogs = database_catalogs
            self.database_controller = database_catalogs[0].controller if database_catalogs else None
            if self.database_preview_route is not None and self.database_controller is not None:
                self.database_preview_route.select(self.database_controller)
            self.database_review = database_review; self.database_discarded = False
            self.database_selector = QComboBox(); self.database_selector.setAccessibleName("Select catalog")
            for choice in database_catalogs:
                self.database_selector.addItem(choice.label + " [" + choice.controller.session.binding.catalog_id + "]")
            if not database_catalogs: self.database_selector.addItem("No catalog connected")
            self.database_selector.setEnabled(bool(database_catalogs))
            database_layout.addWidget(QLabel("Connected catalog")); database_layout.addWidget(self.database_selector)
            self.database_selection = QLabel(); self.database_versions = QLabel()
            for label in (self.database_selection, self.database_versions):
                label.setTextFormat(Qt.PlainText); label.setWordWrap(True); database_layout.addWidget(label)
            actions = ResponsiveCardGrid(minimum_card_width=220, maximum_columns=2)
            action_buttons = []
            self.database_preview_buttons = []
            for title, action in (("Preview install default catalog", DatabaseAction.INSTALL_DEFAULT), ("Preview merge/update existing", DatabaseAction.MERGE_UPDATE)):
                button = compact_action(QPushButton(title)); button.setEnabled(False); button.setToolTip(PREVIEW_DISABLED)
                button.clicked.connect(lambda checked=False, selected=action: self.request_database_preview(selected))
                self.database_preview_buttons.append(button); action_buttons.append(button)
            self.database_preview_reason = QLabel(PREVIEW_DISABLED); self.database_preview_reason.setTextFormat(Qt.PlainText)
            self.database_preview_reason.setWordWrap(True)
            self.database_refresh = compact_action(QPushButton("Refresh review"))
            self.database_refresh.setToolTip("Refresh the selected review. This does not search for or install catalogs.")
            self.database_refresh.clicked.connect(self.render_databases); action_buttons.append(self.database_refresh)
            self.database_discard = compact_action(QPushButton("Discard preview")); self.database_discard.clicked.connect(self.discard_database_review)
            action_buttons.append(self.database_discard); actions.set_cards(action_buttons); database_layout.addWidget(actions)
            database_layout.addWidget(self.database_preview_reason)
            self.database_section = QComboBox(); self.database_section.setAccessibleName("Catalog review detail section")
            database_layout.addWidget(self.database_section)
            self.databases.setFrameShape(QFrame.NoFrame); self.databases.document().setDocumentMargin(0)
            self.databases.setOpenExternalLinks(False); self.databases.setMaximumHeight(220); database_layout.addWidget(self.databases)
            self.database_apply = compact_action(QPushButton("Apply reviewed changes")); self.database_apply.setEnabled(False)
            database_layout.addWidget(self.database_apply, 0, Qt.AlignLeft)
            self.database_apply_reason = QLabel(); self.database_apply_reason.setWordWrap(True)
            self.database_apply_reason.setTextFormat(Qt.PlainText); database_layout.addWidget(self.database_apply_reason)
            database_scroll = QScrollArea(); database_scroll.setWidgetResizable(True); database_scroll.setFrameShape(QFrame.NoFrame)
            database_scroll.setWidget(database_body); databases_page_layout.addWidget(database_scroll, 1); self.databases_page = databases_page
            self.database_selector.currentIndexChanged.connect(self.select_database_catalog)
            self.database_section.currentIndexChanged.connect(self.render_database_details)
            self.render_databases()
            if tag_database_controller is not None:
                from suite_core.manager.tag_database_ui import TagDatabasePane
                self.tag_database_pane = TagDatabasePane(tag_database_controller,
                    encyclopedia=tag_encyclopedia, parent=databases_page)
                databases_page_layout.removeWidget(database_scroll)
                database_scroll.hide()
                databases_page_layout.addWidget(self.tag_database_pane, 1)
            settings_page = QWidget(); settings_outer = QVBoxLayout(settings_page); settings_outer.setContentsMargins(0, 0, 0, 0)
            settings_body = QWidget(); settings_layout = workspace_layout(settings_body)
            settings_layout.addWidget(workspace_heading("Settings"))
            connection_group = QGroupBox("Backend connection"); connection_layout = QVBoxLayout(connection_group)
            connection_hint = QLabel("Save your local Ollama endpoint deliberately; refresh metadata separately." if metadata_setup is not None else "Local backend setup is unavailable for this launch.")
            connection_hint.setWordWrap(True); connection_hint.setObjectName("mutedLabel"); connection_layout.addWidget(connection_hint)
            self.ollama_workspace = QScrollArea()
            self.ollama_workspace.setWidgetResizable(True)
            self.ollama_workspace.setFrameShape(QFrame.NoFrame)
            ollama_body = QWidget()
            ollama_layout = workspace_layout(ollama_body)
            self.ollama_workspace.setWidget(ollama_body)
            back = compact_action(QPushButton("Back to applications"))
            back.clicked.connect(self.close_ollama_workspace)
            ollama_layout.addWidget(back, 0, Qt.AlignLeft)
            ollama_layout.addWidget(workspace_heading("Ollama"))
            self.direct_model_button=compact_action(QPushButton('Use an existing GGUF model'))
            self.direct_model_button.setEnabled(self.direct_model_host is not None)
            self.direct_model_button.setToolTip('Reference existing files with a separately selected installed llama-server. '
                'This provider is available in standalone source entry; native root helper admission remains unqualified.')
            self.direct_model_button.clicked.connect(self.open_direct_model)
            applications_layout.addWidget(self.direct_model_button,0,Qt.AlignLeft)
            external = QLabel("Connected endpoints are external instances. Manager does not install or own their Ollama installation.")
            external.setWordWrap(True)
            external.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
            ollama_layout.addWidget(external)
            ollama_layout.addWidget(connection_group)
            self.ollama_connection_status = QLabel(); self.ollama_connection_status.setWordWrap(True)
            self.ollama_connection_status.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
            self.ollama_connection_status.setTextFormat(Qt.PlainText)
            connection_layout.addWidget(self.ollama_connection_status)
            applications_layout.addWidget(self.ollama_workspace, 1); self.ollama_workspace.hide()
            self.backend_setup_controller = None
            self.backend_setup_dialog = None
            if metadata_setup is not None:
                from .backend_setup_controller import SetupController, MetadataBridgeHost
                from .__main__ import create_metadata_bridge
                self._unconfigured_lifecycle_controller = self.lifecycle_controller
                host = MetadataBridgeHost(lambda: self.lifecycle_bridge, self.install_backend_bridge,
                    create_metadata_bridge, self.now, lambda: self.close_policy.pending)
                self.backend_setup_controller = SetupController(metadata_setup, host)
                setup_button = compact_action(QPushButton("Connect existing Ollama"))
                setup_button.setProperty("primary", True)
                setup_button.setAccessibleName("Connect existing Ollama metadata connection")
                setup_button.clicked.connect(self.open_backend_setup)
                connection_layout.addWidget(setup_button, 0, Qt.AlignLeft)
                self.lifecycle_timer.timeout.connect(self.poll_backend_setup)
                self.lifecycle_timer.start()
            from .owned_runtime_ui import OwnedRuntimePanel
            self.owned_runtime_panel = OwnedRuntimePanel(owned_runtime, owned_descriptor, owned_runtime_allowed,
                self, registry_admitted=self.admit_owned_registry)
            self.owned_runtime_panel.changed = self.owned_runtime_changed
            ollama_layout.addWidget(self.owned_runtime_panel)
            models_group = QGroupBox("Models and observations"); ollama_layout.addWidget(models_group)
            ollama_layout.addStretch()
            self.backend_settings_layout = QVBoxLayout(models_group)
            self.ollama_widget = self.create_ollama_view()
            self.backend_settings_layout.addWidget(self.ollama_widget)
            self.backends.setMaximumHeight(180)
            connection_layout.addWidget(disclosure_panel("Connection details", self.backends))
            appearance_group = QGroupBox("Appearance and accessibility"); appearance_layout = QVBoxLayout(appearance_group)
            appearance = QLabel(f"Theme: {theme}\nColor-blind status palette: {'On' if color_blind_mode else 'Off'}")
            appearance.setWordWrap(True); appearance_layout.addWidget(appearance)
            appearance_hint = QLabel("Launch preferences only; these values are not saved by Manager.")
            appearance_hint.setWordWrap(True); appearance_hint.setObjectName("mutedLabel"); appearance_layout.addWidget(appearance_hint)
            settings_layout.addWidget(appearance_group)
            build_group = QGroupBox("Current build"); build_layout = QVBoxLayout(build_group)
            build_label = QLabel("\n".join(self.current_build.lines())); build_label.setWordWrap(True); build_layout.addWidget(build_label)
            settings_layout.addWidget(build_group)
            self.manager_update_panel = None
            if manager_update_service is not None:
                from .update_review_ui import ManagerUpdateReviewPanel
                self.manager_update_panel = ManagerUpdateReviewPanel(manager_update_service, settings_body,
                    normal_entry_session=normal_entry_session)
                self.manager_update_panel.close_for_apply.connect(self.close)
                settings_layout.addWidget(self.manager_update_panel)
            settings_layout.addStretch(1)
            settings_scroll = QScrollArea(); settings_scroll.setWidgetResizable(True); settings_scroll.setFrameShape(QFrame.NoFrame)
            settings_scroll.setWidget(settings_body); settings_outer.addWidget(settings_scroll)
            dependencies_page = QWidget(); dependencies_layout = workspace_layout(dependencies_page)
            from .dependency_status_ui import DependencyStatusPanel
            self._dependency_route_epoch = 0
            self.dependency_status_panel = DependencyStatusPanel(parent=dependencies_page)
            self.dependency_status_panel.route_requested.connect(self.open_dependency_route)
            dependencies_layout.addWidget(self.dependency_status_panel, 1)
            runtime_label = QLabel("\n".join(self.current_build.lines())); runtime_label.setWordWrap(True)
            dependencies_layout.addWidget(disclosure_panel("Manager build runtime", runtime_label))
            self.dependencies.setFrameShape(QFrame.NoFrame)
            dependencies_layout.addWidget(disclosure_panel("Declared component inventory", self.dependencies))
            self.lifecycle_timer.timeout.connect(self.render_dependency_status)
            if self.reader_dialog is not None:
                self.reader_dialog.finished.connect(self.dependency_route_settled)
            self.applications_page = applications_page; self.dashboard_page = dashboard_page
            servers_page = QScrollArea(); servers_page.setWidgetResizable(True)
            self.servers_page = servers_page
            self.page_rows = {name: index for index, name in enumerate(("Dashboard", "Servers", "Applications", "Databases", "Dependencies", "Settings"))}
            for index, (label, widget) in enumerate((("Dashboard", dashboard_page), ("Servers", servers_page), ("Applications", applications_page),
                ("Databases", databases_page), ("Dependencies", dependencies_page), ("Settings", settings_page))):
                self.stack.addWidget(widget)
                item = QListWidgetItem(navigation_icon(("Dashboard", "Setup", "Generate", "Tag Encyclopedia", "Training", "Setup")[index]), label)
                item.setData(PageNavigationList.PAGE_INDEX_ROLE, index); self.navigation.addItem(item)
            self.navigation.pageChanged.connect(self.stack.setCurrentIndex)
            self.navigation_commands = [UiCommand("navigate-" + name.lower(), "Go to " + name, "Navigation",
                callback=lambda _name=name: self.navigation.setCurrentRow(self.page_rows[_name]))
                for name in self.page_rows]
            self.global_commands = [UiCommand("compact", "Expand/collapse sidebar", "View", source=self.sidebar_toggle),
                UiCommand("notes", "What's New", "Help", callback=self.open_notes),
                UiCommand("diagnostics", "Review diagnostics", "Help", callback=self.open_diagnostics)]
            self.inspection_command = UiCommand("inspect-candidate", "Inspect known Encyclopedia candidate", "Applications",
                callback=self.inspect_candidate, enabled=lambda: callable(candidate_inspector), reason="No package-inspection provider supplied.")
            if view.synthetic or self.lifecycle_controller.fixture_enabled:
                self.global_commands[:0] = [
                    UiCommand("demo", "Load synthetic demo", "View", callback=self.load_demo),
                    UiCommand("reset", "Clear detached observations", "View", callback=lambda: self.set_view(unconfigured(self.now())))]
            self.commands = self.navigation_commands + self.global_commands
            self.navigation.currentRowChanged.connect(self.refresh_section_commands)
            self.bar.searchRequested.connect(self.search_commands)
            if self.resource_route is not None:
                from .resource_host_binding_candidate import ResourceDashboardBinding
                self.resource_dashboard_binding = ResourceDashboardBinding(self.resource_route,
                    dashboard_visible=lambda: self.navigation.currentRow() == self.page_rows["Servers"] and not getattr(self, "view", view).synthetic,
                    render=self.render_local_resources)
                self.navigation.currentRowChanged.connect(self.resource_dashboard_binding.navigation_changed)
            self.navigation.setCurrentRow(0); self.set_view(view)
            if self.resource_dashboard_binding is not None:
                self.resource_dashboard_binding.navigation_changed()
            if self.tag_database_pane is not None:
                status = ("Use Databases to connect a catalog, review content and recover interrupted work."
                    if self.tag_database_pane.controller.settings_path is not None else
                    "Approved Tag catalog connected; review each change before Apply."
                    if self.tag_database_pane.controller.apply_authorized else
                    "Tag catalog review connected; Apply requires explicit host permission.")
            else:
                status = ("Encyclopedia installation tools supplied; review the package before confirming installation." if self.acquisition_panel is not None and self.acquisition_panel.service.install_available else
                    "Use Applications to connect an existing approved reader package." if self.reader_panel is not None else
                    "Qualified owned Ollama controls supplied; review each action." if self.owned_runtime_panel.ready else
                    "Preview only \u2022 Installation and application setup are not available yet")
            self.statusBar().showMessage(status)
            self.render_ollama()

        def select_database_catalog(self, index):
            if not 0 <= index < len(self.database_catalogs): return
            route = self.database_preview_route
            if route is not None: route.select(self.database_catalogs[index].controller)
            elif self.database_controller is not None: self.database_controller.cancel()
            self.database_controller = self.database_catalogs[index].controller
            self.database_review = None; self.database_discarded = False; self.render_databases()

        def set_database_review(self, controller, review):
            """UI-owner delivery; route-owned windows admit only that route's receipt."""
            if self.database_preview_route is not None and review is not self.database_preview_route.review: return False
            if controller is not self.database_controller: return False
            state = project_database_review(controller, review)
            if not state.review_accepted:
                self.render_databases(); return False
            self.database_review = review; self.database_discarded = False
            self.render_databases(); return True

        def discard_database_review(self):
            if self.tag_database_pane is not None:
                self.tag_database_pane.discard_review(); return
            if self.database_preview_route is not None: self.database_preview_route.invalidate()
            elif self.database_controller is not None: self.database_controller.cancel()
            self.database_review = None; self.database_discarded = True; self.render_databases()

        def request_database_preview(self, action):
            if self.tag_database_pane is not None:
                controller = self.tag_database_pane.controller
                expected = DatabaseAction.MERGE_UPDATE if controller.choice and controller.choice.installed else DatabaseAction.INSTALL_DEFAULT
                if action != expected:
                    controller.reason = "Select a blank catalog for install or the installed catalog for merge review."
                    self.tag_database_pane.render(); return False
                admitted = controller.request("preview")
                self.tag_database_pane.render(); return admitted
            route = self.database_preview_route
            if route is None or route.controller is not self.database_controller or not route.request(action):
                self.render_databases(); return False
            self.database_review = None; self.database_discarded = False
            self.render_databases(); return True

        def poll_database_preview(self):
            if self.tag_database_pane is not None:
                self.tag_database_pane.poll(); return
            route = self.database_preview_route
            if route is None: return
            accepted = route.poll()
            if accepted:
                self.set_database_review(route.controller, route.review)
            else:
                current = (id(route.review), id(route.controller), route.pending, route.reason, route.can_request, self.database_controller.session.binding if self.database_controller is not None else None)
                if current == self.database_route_state: return
                if self.database_review is not None and self.database_review is not route.review:
                    self.database_review = None
                self.render_databases()

        def render_databases(self):
            if self.tag_database_pane is not None:
                self.tag_database_pane.render(); return
            route = self.database_preview_route
            if route is not None:
                if route.review is not self.database_review: self.database_review = None
                same_selection = route.controller is self.database_controller
                reason = route.availability_reason if same_selection else "Preview route selection differs from this page. Select the catalog again to align it."
                for button in self.database_preview_buttons:
                    button.setEnabled(same_selection and route.can_request); button.setToolTip(reason)
                self.database_preview_reason.setText(reason + "\n" + route.reason)
                self.database_route_state = (id(route.review), id(route.controller), route.pending, route.reason, route.can_request, self.database_controller.session.binding if self.database_controller is not None else None)
            else:
                self.database_preview_reason.setText(PREVIEW_DISABLED)
            state = project_database_review(self.database_controller, self.database_review)
            self.database_state = state
            self.database_selector.blockSignals(True)
            for index, choice in enumerate(self.database_catalogs):
                self.database_selector.setItemText(index, choice.label + " [" + choice.controller.session.binding.catalog_id + "]")
            self.database_selector.blockSignals(False)
            self.database_selection.setText(state.selection)
            self.database_status.setText("Preview discarded. Catalog and original image assignments are unchanged." if self.database_discarded else state.status)
            self.database_versions.setText("\n".join(state.versions))
            self.database_refresh.setEnabled(self.database_controller is not None)
            self.database_discard.setEnabled(self.database_controller is not None and (self.database_review is not None or route is not None and route.pending))
            # Disabled unconditionally; there is intentionally no writer or Apply callback.
            self.database_apply.setEnabled(False); self.database_apply.setToolTip(state.apply_reason)
            self.database_apply_reason.setText(state.apply_reason)
            previous = self.database_section.currentIndex(); self.database_section.blockSignals(True)
            self.database_section.clear()
            for section in state.sections:
                count = "unknown" if section.total is None else str(section.total)
                self.database_section.addItem(section.title + " (" + count + ")")
            self.database_section.setCurrentIndex(max(0, min(previous, len(state.sections)-1)))
            self.database_section.blockSignals(False); self.render_database_details()

        def render_database_details(self):
            state = self.database_state; index = self.database_section.currentIndex()
            if not 0 <= index < len(state.sections): return
            self.databases.setPlainText(state.sections[index].text())

        def open_ollama_workspace(self):
            self.navigation.setCurrentRow(self.page_rows["Applications"])
            if not self.ollama_workspace.isVisible():
                self._ollama_return_visibility = {name: getattr(self, name).isVisible() for name in
                    ("application_categories", "categories_toggle", "app_observation_heading", "applications")}
            self.application_categories.hide(); self.categories_toggle.hide()
            self.app_observation_heading.hide(); self.applications.hide()
            self.ollama_workspace.show()
            self.render_ollama()

        def close_ollama_workspace(self):
            self.ollama_workspace.hide()
            self.dependency_route_settled()
            for name, visible in self._ollama_return_visibility.items():
                getattr(self, name).setVisible(visible)
            self.render_library()

        def create_ollama_tile(self):
            card = QFrame(); card.setObjectName("visualCard"); card_style(card, compact=True)
            card.setProperty("componentKey", "ollama-connection")
            body = QVBoxLayout(card); body.setContentsMargins(14, 14, 14, 14); body.setSpacing(8)
            title = QLabel("Ollama"); title.setObjectName("cardTitle"); body.addWidget(title)
            info = QLabel("Install a reviewed owned runner or connect an existing local instance. Model information stays unknown until explicitly refreshed." if self.owned_runtime_panel.ready else
                "Connect an existing local model runner. Model information stays unknown until you explicitly refresh.")
            info.setWordWrap(True); body.addWidget(info)
            panel = self.owned_runtime_panel
            state = QLabel("Manager-owned runner installed" if panel.installed else "Connection tools; owned installation status unknown")
            state.setWordWrap(True); body.addWidget(state)
            actions = QHBoxLayout()
            install = compact_action(QPushButton("Install new"))
            panel = self.owned_runtime_panel
            install.setEnabled(panel.install.isEnabled())
            install.setToolTip(panel.unavailable or "Review the supplied verified owned package; start remains separate.")
            install.clicked.connect(self.open_owned_install)
            connect = compact_action(QPushButton("Connect existing"))
            connect.setEnabled(self.backend_setup_controller is not None and not self.view.synthetic)
            connect.clicked.connect(self.open_backend_setup)
            manage = compact_action(QPushButton("Manage / Models")); manage.clicked.connect(self.open_ollama_workspace)
            for button in (install, connect, manage): actions.addWidget(button)
            body.addStretch(); body.addLayout(actions)
            return card

        def render_dependency_status(self):
            if not hasattr(self, "dependency_status_panel"): return
            from core.manager_dependency_status import DependencyObservation, DependencyStatusSnapshot, DependencyState as State, DependencyRoute as Route
            from hashlib import sha256
            rows = []
            closing = self.close_policy.pending
            runtime = self.owned_runtime_panel
            rows.append(DependencyObservation("owned-runtime", "Ollama owned runner",
                State.BUSY if runtime.busy or closing else State.UNKNOWN,
                detail="Review the original owned runner's status and recovery controls.",
                route=Route.OWNED_RUNTIME, route_available=bool(runtime.ready or runtime.binding is not None)))
            flow = self.backend_setup_controller
            if flow is not None:
                bridge = self.lifecycle_bridge
                busy = closing or flow.activation_pending or (self.chat_host is not None and self.chat_host.busy) or (bridge is not None and (bridge.ollama_pending or bridge.worker_pending))
                rows.append(DependencyObservation("backend-connection", "Ollama connection",
                    State.BUSY if busy else State.BLOCKED if flow.hold else State.UNKNOWN,
                    detail=flow.message[:2048], blocking_reason=flow.message[:1024] if flow.hold else "",
                    route=Route.BACKEND_CONNECTION,
                    route_available=not flow.closed))
            panel = self.reader_panel
            if panel is not None:
                busy = closing or panel.owner.close_held or (self.acquisition_panel is not None and self.acquisition_panel.close_held)
                restart = panel.restart_required()
                rows.append(DependencyObservation("reader-connection", "Tag Encyclopedia",
                    State.BUSY if busy else State.RESTART if restart else State.UNKNOWN,
                    detail=panel.status.text()[:2048],
                    blocking_reason="Restart Manager before launching the saved reader." if restart else "",
                    version=panel.selected.version if panel.selected is not None else "",
                    route=Route.READER_CONNECTION, route_available=True))
            rows = tuple(rows)
            revision = sha256(repr((rows, self._dependency_route_epoch)).encode("utf-8")).hexdigest()
            self.dependency_status_panel.set_snapshot(DependencyStatusSnapshot(revision, rows))

        def dependency_route_settled(self, *_):
            self._dependency_route_epoch += 1
            self.render_dependency_status()

        def open_dependency_route(self, key, route):
            self.render_dependency_status()
            from core.manager_dependency_status import project_dependency
            row = next((row for row in self.dependency_status_panel.snapshot.observations if row.key == key), None)
            if row is None or row.route is None or row.route.value != route or not project_dependency(row).action_enabled or self.close_policy.pending:
                self.dependency_route_settled()
                return
            if route == "reader-connection": self.open_reader_connection()
            elif route == "backend-connection": self.open_backend_setup()
            elif route == "owned-runtime": self.open_ollama_workspace()

        def reader_card_state(self):
            panel = self.reader_panel
            if panel is None: return None
            acquisition = self.acquisition_panel
            installed = (acquisition is not None and acquisition.installed is not None
                and acquisition.installed.selected is not None)
            available = acquisition is not None and acquisition.service.install_available
            update = (installed and acquisition.offer is not None
                and acquisition.offer.release.software_version != acquisition.installed.selected.version)
            busy = panel.owner.close_held or self.close_policy.pending or (
                acquisition is not None and (acquisition.close_held or acquisition.closing))
            launch = panel.launch.isEnabled()
            if busy:
                action, target = "View progress", "connection" if panel.owner.close_held else "acquisition"
            elif update and available:
                action, target = "Update", "acquisition"
            elif launch:
                action, target = "Launch", "launch"
            elif panel.selected is not None:
                action, target = "Manage connection", "connection"
            elif available:
                action, target = "Install", "acquisition"
            else:
                action, target = "Connect existing", "connection"
            status = panel.status.text() if panel.selected is not None or panel.owner.close_held else (
                acquisition.status.text() if acquisition is not None else "No reader connected.")
            return (installed, available, update, busy, action, target, status,
                panel.launch.isEnabled(), panel.selected)

        def create_reader_tile(self):
            state = self.reader_card_state()
            installed, available, update, busy, action, target, status, _, _ = state
            card = QFrame(); card.setObjectName("visualCard"); card_style(card, compact=True)
            card.setProperty("componentKey", "encyclopedia-connection")
            body = QVBoxLayout(card); body.setContentsMargins(14,14,14,14); body.setSpacing(8)
            title = QLabel("Tag Encyclopedia"); title.setObjectName("cardTitle"); body.addWidget(title)
            scope = QLabel("Browse tag knowledge in a separate reader. Review a first-party release to install, or connect an existing approved package." if available else
                "Browse tag knowledge in a separate reader. Connect an existing approved, extracted reader package.")
            scope.setWordWrap(True); body.addWidget(scope)
            label = QLabel(status); label.setWordWrap(True); body.addWidget(label)
            button = compact_action(QPushButton(action)); button.setObjectName("readerPrimaryAction")
            button.setEnabled(not self.close_policy.pending)
            button.clicked.connect(self.reader_primary_action)
            body.addStretch(); body.addWidget(button)
            options = QWidget(); options_layout = QVBoxLayout(options); options_layout.setContentsMargins(0,0,0,0)
            connect = compact_action(QPushButton("Connection settings")); connect.setObjectName("connectExistingEncyclopedia")
            connect.setEnabled(not busy); connect.clicked.connect(self.open_reader_connection); options_layout.addWidget(connect)
            if self.acquisition_panel is not None:
                acquire = compact_action(QPushButton("Releases and setup")); acquire.setObjectName("acquireEncyclopedia")
                acquire.setEnabled(not self.close_policy.pending); acquire.clicked.connect(self.open_reader_acquisition)
                options_layout.addWidget(acquire)
            body.addWidget(disclosure_panel("Options", options))
            return card

        def reader_primary_action(self):
            state = self.reader_card_state()
            if state is None or self.close_policy.pending: return
            target = state[5]
            if target == "launch": self.reader_panel.launch_reader()
            elif target == "acquisition": self.open_reader_acquisition()
            else: self.open_reader_connection()
            self.render_library()

        def open_reader_connection(self):
            self.reader_dialog.show(); self.reader_dialog.raise_(); self.reader_dialog.activateWindow()

        def open_reader_acquisition(self):
            if self.close_policy.pending or self.acquisition_dialog is None: return
            self.acquisition_dialog.show(); self.acquisition_dialog.raise_(); self.acquisition_dialog.activateWindow()
            self.acquisition_panel.reconcile_launch()

        def owned_runtime_changed(self):
            self.render_library()
            binding = self.owned_runtime_panel.binding
            if self.statusBar().currentMessage().startswith("Close held:") and binding is not None and not binding.close_held:
                self.statusBar().showMessage("Owned runtime idle; Manager can close after its other workers drain.")

        def admit_owned_registry(self, registry):
            from dataclasses import replace
            self.view = replace(self.view, installation_registry=registry)
            self.render_library()

        def open_owned_install(self):
            if self.close_policy.pending: return
            self.open_ollama_workspace()
            self.owned_runtime_panel.review_install()

        def open_backend_setup(self):
            if getattr(self, 'chat_host', None) is not None and self.chat_host.busy:
                self.statusBar().showMessage('Stop the chat request and wait for completion before changing its backend.')
                return
            if self.close_policy.pending or self.backend_setup_controller is None: return
            self.open_ollama_workspace()
            if self.backend_setup_dialog is not None:
                self.backend_setup_dialog.raise_(); self.backend_setup_dialog.activateWindow(); return
            from .backend_setup_ui import create_setup_dialog
            dialog = create_setup_dialog(self.backend_setup_controller, self)
            dialog.setWindowTitle("Connect existing Ollama")
            self.backend_setup_dialog = dialog
            dialog.finished.connect(lambda _: self.release_backend_setup_dialog(dialog))
            dialog.show()

        def release_backend_setup_dialog(self, dialog):
            self.dependency_route_settled()
            if self.backend_setup_dialog is dialog: self.backend_setup_dialog = None
            dialog.deleteLater()

        def poll_backend_setup(self):
            flow = self.backend_setup_controller
            if flow is not None and flow.state == "waiting" and not flow.closed:
                flow.activate()
            if flow is not None:
                flow.observe_refresh()
                self.ollama_connection_status.setText(flow.message)

        def install_backend_bridge(self, bridge):
            current = self.lifecycle_bridge
            if getattr(current, "module_enabled", False):
                raise RuntimeError("module_owner_replacement_refused")
            if current is not None and current is not bridge and not (current.quiescent and current.closed):
                raise RuntimeError("previous_bridge_teardown_unacknowledged")
            # Install lifetime ownership before constructing the replacement view.
            self.lifecycle_bridge = bridge
            self.lifecycle_controller = bridge if bridge is not None else self._unconfigured_lifecycle_controller
            replacement = self.create_ollama_view()
            self.backend_settings_layout.replaceWidget(self.ollama_widget, replacement)
            self.ollama_widget.hide(); self.ollama_widget.deleteLater()
            self.ollama_widget = replacement
            self.lifecycle_timer.start(); self.render_ollama()

        def create_ollama_view(self):
            from PySide6.QtWidgets import QTableWidget
            widget = QWidget(); body = QVBoxLayout(widget)
            self.ollama_summary = QLabel(); self.ollama_summary.setWordWrap(True)
            self.ollama_summary.setTextFormat(Qt.PlainText)
            self.ollama_summary.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
            body.addWidget(QLabel("Ollama models")); body.addWidget(self.ollama_summary)
            controls = QHBoxLayout(); self.ollama_refresh = compact_action(QPushButton("Refresh Ollama inventory"))
            self.ollama_cancel = compact_action(QPushButton("Cancel refresh"))
            self.ollama_refresh.clicked.connect(self.refresh_ollama)
            self.ollama_cancel.clicked.connect(self.cancel_ollama)
            controls.addWidget(self.ollama_refresh); controls.addWidget(self.ollama_cancel)
            self.open_chat = compact_action(QPushButton('Open Chat'))
            self.open_chat.setEnabled(self.chat_host is not None)
            self.open_chat.clicked.connect(self.open_model_chat)
            controls.addWidget(self.open_chat); controls.addStretch(1); body.addLayout(controls)
            self.ollama_recovery = QLabel(); self.ollama_recovery.setWordWrap(True)
            self.ollama_recovery.setTextFormat(Qt.PlainText)
            self.ollama_recovery.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum); body.addWidget(self.ollama_recovery)
            self.ollama_tables = {}; self.ollama_route_labels = {}; self._ollama_table_reports = {}
            from suite_core.manager.ollama_inventory import ReadRoute
            for route, title in ((ReadRoute.INSTALLED, "Installed models"), (ReadRoute.LOADED, "Loaded models")):
                label = QLabel(title); label.setWordWrap(True); body.addWidget(label)
                label.setTextFormat(Qt.PlainText)
                label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
                self.ollama_route_labels[route] = label
                if self.lifecycle_bridge is None or self.lifecycle_bridge.ollama_scope is None:
                    self.ollama_tables[route] = None
                    continue
                table = QTableWidget(0, 4); table.setHorizontalHeaderLabels(("Model", "Reported digest", "Bytes", "VRAM bytes"))
                table.setEditTriggers(QTableView.NoEditTriggers); table.setSelectionBehavior(QTableView.SelectRows)
                table.setMaximumHeight(155); table.horizontalHeader().setStretchLastSection(True)
                body.addWidget(table); self.ollama_tables[route] = table
            body.addWidget(QLabel("Read-only inventory. Loaded models do not establish inference readiness or idle capacity."))
            return widget

        def refresh_ollama(self):
            if getattr(self, 'chat_host', None) is not None and self.chat_host.busy:
                self.statusBar().showMessage('Stop this chat request and wait for completion before refreshing the backend.')
                return
            flow = self.backend_setup_controller
            accepted = flow.refresh() if flow is not None else (self.lifecycle_bridge is not None and self.lifecycle_bridge.refresh_ollama())
            if accepted:
                self.statusBar().showMessage("Refreshing read-only Ollama inventory")
            self.render_ollama()

        def cancel_ollama(self):
            if self.backend_setup_controller is not None: self.backend_setup_controller.cancel_refresh()
            elif self.lifecycle_bridge is not None: self.lifecycle_bridge.cancel_ollama_refresh()
            self.render_ollama()

        def render_ollama(self):
            from suite_core.manager.ollama_inventory import ReadRoute, summary, route_text
            from PySide6.QtWidgets import QTableWidgetItem
            bridge = self.lifecycle_bridge
            scope = bridge.ollama_scope if bridge is not None else None
            report = bridge.ollama_report if bridge is not None else None
            pending = bridge.ollama_pending if bridge is not None else False
            error = bridge.ollama_error if bridge is not None else None
            self.ollama_summary.setText(summary(scope, report, self.now(), pending=pending, error=error))
            if scope is not None:
                base = self.banner.text().split("\nOllama inventory:")[0]
                if report is not None:
                    base = base.replace("no source observations supplied", "no suite catalog observations supplied")
                    base = base.replace("default sources unconfigured", "suite catalog sources unconfigured")
                self.banner.setText(base + "\nOllama inventory: connection configured; "
                    + ("dated read-only observations available in Applications." if report is not None else "observations unknown."))
                if not bridge.fixture_enabled:
                    self.statusBar().showMessage("Ollama refresh pending" if pending else "Read-only Ollama metadata; see dated route status in Applications")
            flow = self.backend_setup_controller
            if flow is not None: flow.observe_refresh()
            self.ollama_refresh.setEnabled(flow.can_refresh if flow is not None else (bridge is not None and bridge.can_refresh_ollama))
            hint = bridge.ollama_recovery_hint if bridge is not None else ("Model information is unknown. Choose Connect existing Ollama, Save connection, then Refresh Ollama inventory." if self.backend_setup_controller is not None else "Model information is unknown. Local backend setup is unavailable for this launch. Reopen Manager without demo mode and with its backend setup provider to configure metadata.")
            if flow is not None and (flow.hold or flow.dirty or flow.activation_pending): hint = flow.message
            self.ollama_refresh.setToolTip(hint)
            self.ollama_recovery.setText(hint); self.ollama_recovery.setVisible(bool(hint))
            self.ollama_cancel.setEnabled(pending); self.ollama_cancel.setVisible(pending)
            if flow is not None: self.ollama_connection_status.setText(flow.message)
            for route, title in ((ReadRoute.INSTALLED, "Installed models"), (ReadRoute.LOADED, "Loaded models")):
                observation = report.observation(route) if report is not None else None
                records = observation.models if observation is not None else None
                displayed = () if records is None else records[:100]
                self.ollama_route_labels[route].setText(title + "\n" + (route_text(report, route, self.now())
                    if report is not None else "unknown; not observed") + (f"; showing 100 of {len(records)}" if records is not None and len(records)>100 else ""))
                if self.ollama_tables[route] is None or self._ollama_table_reports.get(route, False) is report: continue
                self._ollama_table_reports[route] = report
                table = self.ollama_tables[route]
                selected = table.selectedItems()[0].data(Qt.UserRole) if table.selectedItems() else None
                table.clearSelection(); table.setRowCount(len(displayed))
                for index, record in enumerate(displayed):
                    for column, value in enumerate((record.name, record.digest or "unknown",
                        str(record.size_bytes) if record.size_bytes is not None else "unknown",
                        str(record.size_vram_bytes) if record.size_vram_bytes is not None else "unknown")):
                        item = QTableWidgetItem(value); item.setData(Qt.UserRole, record.identity)
                        item.setToolTip(f"Provider identity: {record.connection_ref.value}; {record.name}; digest {record.digest or 'unknown'}.\n"
                            f"Context length: {record.context_length if record.context_length is not None else 'unknown'}; "
                            f"modified: {record.modified_at or 'unknown'}; expires: {record.expires_at or 'unknown'}.\nNot a verified weights hash or execution-ready profile.")
                        table.setItem(index, column, item)
                    if record.identity == selected: table.selectRow(index)
                table.resizeColumnsToContents()
                table.resizeRowsToContents()
                # Preserve a complete header and first row; additional rows scroll locally.
                row_height = table.rowHeight(0) if table.rowCount() else table.verticalHeader().defaultSectionSize()
                minimum = table.horizontalHeader().height() + row_height + 2 * table.frameWidth() + table.horizontalScrollBar().sizeHint().height()
                table.setMinimumHeight(min(155, minimum))
            self.render_local_resources()

            self.refresh_chat_admission()

        def refresh_chat_admission(self):
            host = self.chat_host
            if host is None: return
            bridge = self.lifecycle_bridge
            blocked = bool(bridge is not None and (bridge.ollama_pending or bridge.worker_pending)
                or self.owned_runtime_panel.binding is not None and self.owned_runtime_panel.binding.pending_ticket is not None
                or self.backend_setup_controller is not None and
                    (self.backend_setup_controller.dirty or self.backend_setup_controller.activation_pending))
            try:
                binding = None if metadata_setup is None else metadata_setup.selected_binding()
            except Exception:
                binding = None
            report = None if bridge is None else bridge.ollama_report
            host.update(binding, report, blocked=blocked)
            self.open_chat.setEnabled(not host.closed)
            self.owned_runtime_panel.setEnabled(not host.busy)
            if not host.busy:
                flow = self.backend_setup_controller
                self.ollama_refresh.setEnabled(flow.can_refresh if flow is not None else
                    bridge is not None and bridge.can_refresh_ollama)
            if host.busy:
                self.ollama_refresh.setEnabled(False)
                if self.backend_setup_dialog is not None: self.backend_setup_dialog.setEnabled(False)
            elif self.backend_setup_dialog is not None:
                self.backend_setup_dialog.setEnabled(True)

        def open_direct_model(self):
            host=self.direct_model_host
            if host is None or self.close_policy.pending:return
            from .direct_model_ui import create_direct_model_panel
            if host.closing and not host.close_held:host.closing=False
            if host.panel is None:host.panel=create_direct_model_panel(host,self)
            host.panel.show();host.panel.raise_();host.panel.activateWindow()
            if not hasattr(self,'direct_model_timer'):
                self.direct_model_timer=QTimer(self);self.direct_model_timer.setInterval(100)
                self.direct_model_timer.timeout.connect(self.poll_direct_model)
            host.panel.timer.start();self.direct_model_timer.start()

        def poll_direct_model(self):
            host=self.direct_model_host
            if host is None:return
            host.poll()
            if self.close_policy.pending and not host.close_held:self.reconsider_close()

        def open_model_chat(self):
            if self.chat_host is None: return
            self.refresh_chat_admission()
            self.chat_host.open_panel(self)
            if not hasattr(self, 'chat_timer'):
                self.chat_timer = QTimer(self); self.chat_timer.setInterval(100)
                self.chat_timer.timeout.connect(self.poll_model_chat)
            self.chat_timer.start()

        def poll_model_chat(self):
            if self.chat_host is None: return
            self.chat_host.poll()
            self.refresh_chat_admission()
            if not self.chat_host.busy and self.chat_host.panel is not None and not self.chat_host.panel.isVisible():
                self.chat_timer.stop()
            if not self.chat_host.busy and self.close_policy.pending:
                self.reconsider_close()

        @staticmethod
        def now():
            return datetime.now(timezone.utc)

        def set_view(self, detached):
            from suite_core.inventory_view import InventoryReport
            if type(detached.report) is not InventoryReport:
                raise ValueError("existing_inventory_report_required")
            self.view = detached; data = detached.report.to_mapping()
            if self.resource_route is not None:
                self.resource_route.set_visible(self.navigation.currentRow() == self.page_rows["Servers"] and not detached.synthetic)
            from suite_core.installation import ComponentId, InstallationId
            if type(detached.supported_apps) is not tuple or len(detached.supported_apps) > 128:
                raise ValueError("bounded_supported_catalog_required")
            seen = set()
            for entry in detached.supported_apps:
                if (type(entry) is not SupportedAppPreview or type(entry.component_id) is not ComponentId
                    or not entry.title.strip() or not entry.source_ref.strip()
                    or type(entry.observed_at) is not datetime or entry.observed_at.tzinfo is None
                    or type(entry.optional_addon_instances) is not tuple
                    or any(type(identity) is not InstallationId for identity in entry.optional_addon_instances)
                    or type(entry.categories) is not tuple or len(set(entry.categories)) != len(entry.categories)
                    or any(category not in APP_CATEGORIES for category in entry.categories)
                    or type(entry.description) is not str or type(entry.capabilities) is not tuple
                    or any(type(capability) is not str for capability in entry.capabilities)
                    or entry.record_kind not in {"supported catalog", "developer candidate"}
                    or entry.component_id in seen):
                    raise ValueError("explicit_supported_catalog_required")
                seen.add(entry.component_id)
                if entry.record_kind == "developer candidate":
                    from .candidate_inventory import CandidatePackageObservation
                    artifact = entry.candidate_package
                    if (type(artifact) is not CandidatePackageObservation or artifact.component_id != entry.component_id.value
                        or artifact.observed_at != entry.observed_at or artifact.installable is not False
                        or artifact.authenticated is not False or artifact.self_contained is not False
                        or artifact.execution_qualified is not False or artifact.installation_state != "unknown"
                        or artifact.running_state != "unknown"):
                        raise ValueError("readonly_candidate_observation_required")
                elif entry.candidate_package is not None:
                    raise ValueError("candidate_kind_required")
            from suite_core.manager.resource_dashboard import create_dashboard
            dashboard = create_dashboard(detached.report, detached.jobs, synthetic=detached.synthetic,
                supported_apps=detached.supported_apps, development_apps=DEVELOPMENT_APPLICATIONS)
            while self.dashboard_slot.count():
                old = self.dashboard_slot.takeAt(0).widget()
                if old is not None: old.hide(); old.deleteLater()
            self.dashboard = dashboard; self.dashboard_slot.addWidget(dashboard)
            from .servers_ui import create_servers_panel
            old_servers = self.servers_page.takeWidget()
            if old_servers is not None:
                old_servers.refresh.hide(); old_servers.refresh.deleteLater()
                old_servers.hide(); old_servers.deleteLater()
            self.resource_panel = create_servers_panel(detached.report, detached.jobs,
                route=self.resource_route, now=self.now, synthetic=detached.synthetic,
                configure=self.open_backend_setup if self.backend_setup_controller is not None else None,
                metadata=self.server_metadata,
                open_metadata=self.open_ollama_workspace)
            self.servers_page.setWidget(self.resource_panel)
            self.render_local_resources()
            from suite_core.installation import InstallationRegistry, removal_blockers
            registry = detached.installation_registry
            if registry is None:
                self.dependencies.setPlainText("No installation information connected.\n\nApplication requirements and compatibility are unknown.\nInstallation and removal are not available yet. Each application manages its own setup.")
            elif type(registry) is not InstallationRegistry:
                raise ValueError("existing_installation_registry_required")
            else:
                lines = ["Dependencies \u2014 supplied configured registry only", "Not installed-package proof. Compatibility/update qualification: unknown.",
                    "Impact is a configured-dependency preview only; it does not approve deletion, provide rollback or inspect running consumers.", ""]
                for entry in registry.entries:
                    lines.append(f"Installation instance: {entry.installation_id.value}")
                    lines.append(f"{entry.component_id.value} \u00b7 {display_component_version(entry.component_id.value, entry.software_version)} \u00b7 {entry.kind.value}")
                    lines.append("Declared dependency instances: " + (", ".join(item.value for item in entry.dependencies) or "none declared (not complete runtime requirements)"))
                    blockers = removal_blockers(registry, frozenset({entry.installation_id}))
                    lines.append("If this configured instance were removed, surviving declared dependents: " +
                        (", ".join(owner.value for owner, dependency in blockers) or "none declared; runtime/removal safety still unknown"))
                    lines.append("")
                self.dependencies.setPlainText("\n".join(lines))
            supplied = (any(data[key] for key in ("components", "hosts", "models", "hardware_observations"))
                or bool(detached.jobs) or bool(detached.supported_apps) or (registry is not None and bool(registry.entries)))
            self.banner.setText("SYNTHETIC DEMO - no installed-package or live-service proof" if detached.synthetic
                else "Showing recorded information. Current installation and running status have not been checked." if supplied
                else "No applications or catalogs connected yet. Browse components in development below; installation is not available yet.")
            self.banner.setVisible(detached.synthetic or self.lifecycle_controller.fixture_enabled
                or (self.lifecycle_bridge is not None and self.lifecycle_bridge.enabled))
            if any(entry.candidate_package for entry in detached.supported_apps):
                self.banner.setText("Candidate observations supplied")
            if self.lifecycle_controller.fixture_enabled:
                self.banner.setText("FAKE LIFECYCLE FIXTURE \u2014 no process launch, live readiness or installed proof. " + self.banner.text())
            if self.lifecycle_bridge is not None and self.lifecycle_bridge.module_enabled:
                self.banner.setText("Disposable module provider selected. Review readiness and the test version below. Production installation is unconfigured.")
            apps = [row for row in data["components"] if row["kind"] == "Apps"]
            modules = [row for row in data["components"] if row["kind"] == "Modules"]
            self.summary.setPlainText("Browse suite applications. Details shows requirements and availability.")
            self.summary.setVisible(bool(apps) or bool(detached.supported_apps))
            self.applications.setVisible(bool(apps)); self.app_observation_heading.setVisible(bool(apps))
            if any(entry.candidate_package for entry in detached.supported_apps):
                self.summary.setPlainText("Developer candidate package inspected. Supported product catalog and installed-app inventory are not supplied.\n"
                    "Manage shows version, provenance, external requirements and the next launch gate.")
            if self.lifecycle_bridge is not None and self.lifecycle_bridge.module_enabled:
                self.summary.setPlainText("Manage the disposable test component below. Production installation information is not connected.")
            def rows(records):
                return [IncrementalResultRow(row["installation_id"],
                (row["component_id"], row["configured_software_version"], row["locator_state"], row["freshness"]),
                tooltip="Configured version is not verified installed software. Package authenticity unknown.") for row in records]
            self.application_model.set_rows(rows(apps)); self.applications.resizeColumnsToContents()
            self.component_model.set_rows(rows(modules))
            self.module_status.setText(("Supplied module observations; package authenticity and execution readiness unknown." if modules
                else "Module source: unconfigured. No module observations supplied.")
                + "\nBackend metadata remains under Settings; no module-to-backend readiness is inferred.")
            self.components.resizeColumnsToContents()
            # Catalog refresh retains category, status, query and Details context.
            self.application_categories.sync_section(self.application_filter_state.category)
            self.lifecycle_controller.replace_catalog(detached.supported_apps)
            self.render_library()
            # Display the compositor's existing detached projection; no second status logic.
            import json
            self.backends.setPlainText("Supplied detached backend/resource observations; no live connection or preference changes.\n\n" + json.dumps(data["hosts"], indent=2) if data["hosts"] else
                "Backend metadata source: unconfigured. GPU activity, capacity and readiness: unknown.")
            self.refresh_section_commands()
            self.render_dependency_status()

        def render_library(self):
            if not hasattr(self, "view"): return
            selected = self.library_filter.currentText()
            query = self.library_search.text().strip().casefold()
            category = self.application_filter_state.category
            self.application_filter_state = ApplicationFilterState(category, selected, query)
            self.category_description.setText(CATEGORY_DESCRIPTIONS.get(category, "No category description available."))
            self.category_description.setVisible(category != "All categories")
            entries = matching_catalog(self.view.supported_apps, self.application_filter_state)
            self.visible_catalog_entries = entries
            development = matching_development(selected, category, query)
            self.visible_development_entries = development
            projections = []
            for entry in entries:
                binding = self.lifecycle_controller.binding_for(entry)
                projections.append((binding, self.lifecycle_controller.projection(binding, self.now())))
            signature = (selected, query, category, development, entries,
                tuple(tuple(sorted(state.items())) for _, state in projections),
                self.close_policy.pending, self.close_policy.role,
                self.owned_runtime_panel.installed, self.owned_runtime_panel.busy,
                self.owned_runtime_panel.dialog is not None, self.owned_runtime_panel.ready,
                self.owned_runtime_panel.install.isEnabled(), self.reader_card_state())
            if getattr(self, "_library_signature", None) == signature:
                return  # Idle polling keeps the current focus and Details disclosure.
            show_ollama = (selected == "All entries" or selected == "Installed" and self.owned_runtime_panel.installed) and category in ("All categories", "Model Runners") and (not query or query in "ollama local model runner")
            previous_details = getattr(self, "_library_details", {})
            for previous_card in self.library_body.cards:
                toggle = previous_card.findChild(QToolButton)
                if toggle is not None:
                    previous_details[previous_card.property("componentKey")] = toggle.isChecked()
            self._library_details = previous_details
            self.library_status.setText("Catalog entries supplied; installation status unknown."
                if entries else "No matching applications. " +
                ("No application catalog connected." if not self.view.supported_apps else
                 "No catalog entries match the combined filters." if selected == "All entries" else
                 f"No verified {selected.lower()} applications. Unknown entries are excluded from this filter."))
            if entries and any(entry.candidate_package for entry in entries):
                self.library_status.setText("Candidate artifact verified against accepted pins \u00b7 Installation and running state unknown")
            if development and not entries:
                self.library_status.setText("In development · View details for requirements.")
            cards = [development_card(item) for item in development]
            for item, card in zip(development, cards):
                role = QLabel(" · ".join(item.categories))
                role.setWordWrap(True); role.setTextFormat(Qt.PlainText)
                card.layout().insertWidget(2, role)
            for entry, (binding, lifecycle) in zip(entries, projections):
                card = QFrame(); card.setObjectName("visualCard"); card_style(card, compact=True)
                card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
                body = QVBoxLayout(card); body.setContentsMargins(14, 14, 14, 14); body.setSpacing(8)
                top = QHBoxLayout(); icon = QLabel()
                category = entry.categories[0] if entry.categories else None
                glyph = {"Productivity": "Dashboard", "AI Generation": "Generate", "AI Analysis": "Tag Encyclopedia",
                    "AI Training": "Training", "LLM": "Setup", "Games": "Dashboard"}.get(category, "Dashboard")
                glyph = {"media-organizer": "Dataset", "everday": "Rules & Automation",
                    "offline-encyclopedia": "Tag Encyclopedia"}.get(entry.component_id.value, glyph)
                icon.setObjectName("cardIcon"); icon.setPixmap(navigation_icon(glyph).pixmap(28, 28))
                icon.setAccessibleName(entry.title + " identity symbol")
                icon.setToolTip(entry.title + " · symbolic identity, not an official logo")
                top.addWidget(icon); top.addStretch()
                badge = QLabel("Verified candidate pins" if entry.candidate_package else "Unknown install state")
                badge.setObjectName("statusBadge"); top.addWidget(badge); body.addLayout(top)
                title = QLabel(entry.title); title.setObjectName("cardTitle"); title.setWordWrap(True); body.addWidget(title)
                if entry.candidate_package:
                    body.addWidget(QLabel(f"{display_component_version(entry.component_id.value, entry.candidate_package.software_version)} \u00b7 Unsigned developer candidate"))
                description = QLabel(entry.description or "Description not supplied.")
                description.setWordWrap(True); description.setToolTip(description.text())
                policy = description.sizePolicy(); policy.setHorizontalPolicy(QSizePolicy.Ignored)
                policy.setHeightForWidth(True); description.setSizePolicy(policy)
                description.setMinimumWidth(0); description.updateGeometry()
                body.addWidget(description)
                role_label = QLabel("Role: " + application_roles(entry.categories))
                role_label.setWordWrap(True); role_label.setTextFormat(Qt.PlainText); role_label.hide()
                category_label = QLabel(" \u00b7 ".join(entry.categories) or "Category not supplied")
                category_label.setWordWrap(True); body.addWidget(category_label); body.addStretch()
                if entry.candidate_package:
                    hold = QLabel("Installation unknown \u00b7 Launch not qualified"); hold.setWordWrap(True); body.addWidget(hold)
                card.setToolTip(f"Catalog: {entry.component_id.value}\nSource: {entry.source_ref}\nObserved: {entry.observed_at.isoformat()}")
                status = QLabel("Lifecycle: " + lifecycle["state"] + " \u2014 " + lifecycle["reason"] + " UI readiness unknown; Open disabled.")
                status.setObjectName("lifecycleStatus"); status.setWordWrap(True)
                body.addWidget(disclosure_panel("Status details", status))
                body.addStretch()
                actions = QHBoxLayout()
                for label in ("Launch", "Install", "Update"):
                    button = compact_action(QPushButton(label)); button.setEnabled(False)
                    if label == "Launch": button.setMinimumWidth(95)
                    button.setToolTip("Launch not wired. Future verified installed apps: open a trusted declared desktop/web UI, "
                        "or show required managed-backend startup progress; Open when running is confirmed."
                        if label == "Launch" else
                        "No compatible package is connected. Installation and updates are not available yet.")
                    if label == "Launch":
                        button.setObjectName("launch:" + entry.component_id.value)
                        from suite_core.manager.close_policy import CloseRole
                        button.setEnabled(lifecycle["launch_enabled"] and not self.close_policy.pending
                            and self.close_policy.role is CloseRole.MANAGER_OWNED)
                        button.setToolTip(lifecycle["reason"] + " Real Launch requires a fresh exact-scope approval and reviewed async host.")
                        if entry.candidate_package and not self.lifecycle_controller.fixture_enabled:
                            from .candidate_inventory import LAUNCH_HOLD
                            button.setToolTip(LAUNCH_HOLD)
                        button.clicked.connect(lambda checked=False, bound=binding: self.launch_bound_card(bound))
                    if not button.isEnabled(): button.hide()
                    actions.addWidget(button)
                manage = compact_action(QPushButton("Details")); manage.setToolTip("Read-only app details and declared dependency impact")
                manage.clicked.connect(lambda checked=False, item=entry: self.open_app_details(item)); actions.addWidget(manage)
                actions.addStretch(); body.addLayout(actions); card.updateGeometry(); cards.append(card)
            reader_state = self.reader_card_state()
            show_reader = reader_state is not None and self.application_filter_state.category in ("All categories", "AI Analysis") and (
                not query or query in "tag encyclopedia reader browse tag knowledge") and (
                selected == "All entries" or selected == "Installed" and reader_state[0]
                or selected == "Available" and reader_state[1] and not reader_state[0]
                or selected == "Updates" and reader_state[2])
            if show_reader:
                cards.append(self.create_reader_tile())
                if not entries and not development:
                    self.library_status.setText(reader_state[6])
            if show_ollama:
                cards.append(self.create_ollama_tile())
                if not entries and not development:
                    self.library_status.setText("Owned runner registered; use Manage / Models for explicit Start and Stop." if self.owned_runtime_panel.installed else
                    "Review Install new for the qualified supplied package; Connect existing remains separate." if self.owned_runtime_panel.ready else
                    "Existing Ollama connection tools. Owned installation unavailable without a qualified package and permission.")
            # Retire old action surfaces immediately; deferred Qt deletion must
            # not leave a previous enabled button visible during status refresh.
            for previous_card in self.library_body.cards:
                previous_card.setEnabled(False); previous_card.hide()
            self.library_body.set_cards(cards)
            for card in cards:
                toggle = card.findChild(QToolButton)
                if toggle is not None and previous_details.get(card.property("componentKey"), False):
                    toggle.setChecked(True)
            self._library_signature = signature

        def launch_bound_card(self, binding):
            from suite_core.manager.close_policy import CloseRole
            if self.close_policy.pending or self.close_policy.role is not CloseRole.MANAGER_OWNED:
                self.statusBar().showMessage("Launch held while closing or observing a shared session.")
                return
            self.lifecycle_controller.dispatch(binding, self.now())
            self.statusBar().showMessage(self.lifecycle_controller.reason)
            self.render_library()

        def request_disposable_action(self, action):
            if self.lifecycle_bridge is None or not self.lifecycle_bridge.module_enabled or self.close_policy.pending:
                return
            offer = self.disposable_offer.currentData() if action in {'install', 'reopen'} else None
            self.lifecycle_bridge.request_module(action, offer)
            self.render_disposable_module()

        def request_disposable_primary(self):
            self.request_disposable_action(self.disposable_operation)

        def cancel_disposable_operation(self):
            if self.lifecycle_bridge is not None: self.lifecycle_bridge.cancel_module_operation()
            self.render_disposable_module()

        def render_disposable_module(self):
            bridge = self.lifecycle_bridge
            if bridge is None or not bridge.module_enabled: return
            module = bridge.module_snapshot
            busy = bridge.worker_pending or bridge.stopping or self.close_policy.pending
            if bridge._module_held:
                status = bridge.reason
            elif bridge.stopping or self.close_policy.pending:
                status = 'Close pending; waiting for exact child, transaction and owner cleanup.'
            elif bridge.worker_pending:
                status = 'Module operation pending; waiting for current owned status.'
            else:
                status = bridge.reason
            if module is not None and (bridge._module_held or busy):
                status += '\nLast accepted observation: ' + module.reason
            self.disposable_status.setText(status + ' User data is outside replaced code. Production is unconfigured.')
            if module is None:
                self.disposable_primary.setEnabled(False)
                self.disposable_refresh.setEnabled(not busy)
                self.disposable_cancel.setEnabled(bridge.worker_pending and not bridge.stopping)
                return
            if module.ambiguous or bridge._module_held:
                action, label, ready = 'poll', 'Refresh exact owned status', not busy
            elif module.pending_transaction and module.child_active:
                action, label, ready = 'poll', 'Waiting for owned child', False
            elif module.pending_transaction and module.ready:
                action, label, ready = 'health', 'Confirm successful startup', not busy
            elif module.pending_transaction and module.exit_code is not None:
                action, label, ready = 'recover', 'Recover previous code', not busy
            elif module.pending_transaction:
                action, label, ready = 'start', 'Start installed version', not busy
            elif module.phase == 'recovered' and module.active:
                action, label, ready = 'reopen', 'Reopen recovered version', not busy
                version = dict(module.active).get('version')
                self.disposable_offer.setCurrentIndex(self.disposable_offer.findData(
                    {'0.1.0-alpha.1': 'A', '0.1.0-alpha.2': 'B'}.get(version, 'C')))
            else:
                action, label, ready = 'install', 'Install / update selected version', not busy and not module.child_active
            self.disposable_operation = action
            self.disposable_primary.setText(label); self.disposable_primary.setEnabled(ready)
            self.disposable_offer.setEnabled(action == 'install' and not busy)
            self.disposable_refresh.setEnabled(not busy)
            self.disposable_cancel.setEnabled(not bridge.stopping and (bridge.worker_pending or module.child_active or module.pending_transaction))

        def server_metadata(self):
            bridge = self.lifecycle_bridge
            return (bridge.ollama_scope, bridge.ollama_report) if bridge is not None else (None, None)

        def render_local_resources(self):
            if self.resource_panel is None:
                return
            self.resource_panel.update_values()
            route = self.resource_route
            if route is not None:
                self.resource_panel.refresh.setEnabled(route.can_request and not route.executor.close_held
                    and not self.view.synthetic)
                if not route.pending and route.executor.close_held:
                    self.resource_panel.status.setText("Worker completion is unconfirmed. Keep Manager open; retry after completion is confirmed.")
            from .resource_presentation_candidate import project_local_resources
            rows = () if route is None or self.view.synthetic else project_local_resources(route, self.now())
            self.dashboard.local_summary.setText("Local measurements disabled in Demo." if self.view.synthetic else
                ("Not measured" if not rows else
                 f"This computer: {sum(row.state == 'measured' for row in rows)} current readings / {len(rows)} observed resources. Details in Servers."))
            from core.manager_server_inventory import project_ollama_connection
            scope, report = self.server_metadata()
            backend = project_ollama_connection(scope.connection,
                report if scope.accepts(report) else None, self.now()) if scope is not None else None
            self.dashboard.backend_summary.setText("Live Ollama metadata disabled in Demo." if self.view.synthetic else
                ("Local Ollama: connection unknown" if backend is None else
                 "Ollama: " + backend["connection_state"] + "; readiness and activity unknown."))
            self.bar.refresh_state()

        def poll_local_resources(self):
            if self.resource_dashboard_binding is not None:
                self.resource_dashboard_binding.tick()
                if self.navigation.currentRow() == self.page_rows["Dashboard"]:
                    self.render_local_resources()  # expire summary values; never submit a sample

        def poll_lifecycle(self):
            if self.lifecycle_bridge is not None and self.lifecycle_bridge.module_enabled:
                self.lifecycle_bridge.poll(self.now())
                self.render_disposable_module()
                return
            if self.resource_route is not None and not self.lifecycle_controller.fixture_enabled and (
                    self.lifecycle_bridge is None or not self.lifecycle_bridge.enabled):
                return  # local completion timer must not rebuild unrelated inventories
            # Only quick in-memory fake providers exist in this candidate.
            # Real verification/supervision requires a separate async backend.
            self.lifecycle_controller.poll(self.now())
            self.statusBar().showMessage(self.lifecycle_controller.reason)
            self.render_library()
            self.render_ollama()

        def close_context(self):
            from suite_core.manager.close_policy import CloseContext
            from suite_core.manager.lifecycle_controller import State
            from datetime import timedelta
            controller = self.lifecycle_controller
            if self.lifecycle_bridge is not None and self.lifecycle_bridge.module_enabled:
                return self.lifecycle_bridge.module_close_context(self.now(), extra_worker_pending=(
                    self.close_policy.worker_pending or resource_close_held(self.resource_route)
                    or (self.chat_host is not None and self.chat_host.close_held)
                    or (self.direct_model_host is not None and self.direct_model_host.close_held)
                    or (self.tag_database_pane is not None and self.tag_database_pane.controller.close_held)
                    or self.diagnostic_help_flow.close_held or (self.database_preview_route is not None and self.database_preview_route.close_held)))
            offer = controller.session_offer
            key = self.lifecycle_bridge.session_key if self.lifecycle_bridge is not None else (
                (offer.permit.nonce.value, offer.plan.fingerprint) if offer is not None else None)
            fresh = (offer is None and not controller.occupied) or (
                controller.observed_at is not None and timedelta(0) <= self.now() - controller.observed_at <= timedelta(seconds=30))
            cleanup_held = controller.cleanup_held
            return CloseContext(key, controller.state, controller.occupied, controller.owned is not None,
                bool(fresh), self.close_policy.worker_pending or (self.acquisition_panel is not None and self.acquisition_panel.close_held) or (self.reader_panel is not None and self.reader_panel.owner.close_held) or (self.chat_host is not None and self.chat_host.close_held)
                or (self.direct_model_host is not None and self.direct_model_host.close_held) or (
                    self.tag_database_pane is not None and self.tag_database_pane.controller.close_held) or (
                    self.lifecycle_bridge.worker_pending if self.lifecycle_bridge is not None else False) or (
                    self.database_preview_route.close_held if self.database_preview_route is not None else False) or (
                    resource_close_held(self.resource_route) or self.diagnostic_help_flow.close_held), cleanup_held)

        def close_decision(self):
            decision = self.close_policy.request(self.close_context(), self.close_clock())
            if decision.allow_close and self.lifecycle_bridge is not None and not self.lifecycle_bridge.quiescent:
                self.lifecycle_bridge.request_shutdown()
                decision = self.close_policy.request(self.close_context(), self.close_clock())
            return decision

        def _show_close_decision(self, decision):
            self.statusBar().showMessage(decision.reason)
            self.cancel_close_button.setVisible(self.close_policy.pending)
            if self.close_policy.pending: self.close_timer.start()

        def reconsider_close(self):
            if not self.close_policy.pending or self.close_policy.closed: return
            try:
                decision = self.close_decision()
            except Exception:
                self.statusBar().showMessage("Close facts unavailable; ownership retained.")
                self.close_timer.stop(); return
            self._show_close_decision(decision)
            if decision.allow_close and not self.close_retry_queued:
                self.close_retry_queued = True
                request_id = self.close_policy.request_id
                QTimer.singleShot(0, lambda expected=request_id: self._retry_owned_close(expected))

        def _retry_owned_close(self, expected_request):
            if expected_request != self.close_policy.request_id: return
            self.close_retry_queued = False
            if self.close_policy.pending and not self.close_policy.closed:
                self.close() # recheck current identity/deadline; queued work is not proof

        def cancel_pending_close(self):
            if self.direct_model_host is not None:self.direct_model_host.closing=False
            if self.chat_host is not None:
                # Cancel only the shell close intent. The original request stays
                # cancelled and retained until drain; reopening starts new context.
                self.chat_host.closed = False
            if self.tag_database_pane is not None:
                # Keep cancelled native work and its completion handle; only
                # revoke the shell's close intent, never clear worker authority.
                self.tag_database_pane.controller.closed = False
                self.tag_database_pane.render()
            if self.backend_setup_controller is not None: self.backend_setup_controller.closed = False
            if self.backend_setup_dialog is not None: self.backend_setup_dialog.render()
            self.close_policy.cancel(); self.close_timer.stop()
            if self.acquisition_panel is not None:
                self.acquisition_panel.resume_after_cancelled_close()
            self.cancel_close_button.hide(); self.close_retry_queued = False
            self.statusBar().showMessage(self.close_policy.decision.reason)
            self.render_library()

        def closeEvent(self, event):
            if self.acquisition_panel is not None and not self.acquisition_panel.close_ready():
                self.statusBar().showMessage('Acquisition cancellation or transaction settlement is pending. Keep Manager open until its original worker drains.')
                self._show_close_decision(self.close_decision())
                event.ignore(); return
            if self.reader_panel is not None and self.reader_panel.owner.close_held:
                self.reader_panel.poll()
                if self.reader_panel.owner.close_held:
                    self.statusBar().showMessage("Close the owned Encyclopedia window first, or Stop retained reader in its connection workspace.")
                    self._show_close_decision(self.close_decision())
                    event.ignore(); return
            if self.direct_model_host is not None:
                self.direct_model_host.begin_close()
                if self.direct_model_host.close_held:
                    self.statusBar().showMessage('Owned model is stopping. Manager stays open until its process and worker are closed.')
                    self._show_close_decision(self.close_decision())
                    event.ignore();return
            if self.chat_host is not None:
                self.chat_host.begin_close()
                if self.chat_host.close_held:
                    self.statusBar().showMessage('Chat request is stopping. Manager stays open until its worker finishes.')
                    self._show_close_decision(self.close_decision())
                    event.ignore(); return
            if self.manager_update_panel is not None and not self.manager_update_panel.permit_close():
                self.navigation.setCurrentRow(self.page_rows['Settings'])
                self.statusBar().showMessage('Update worker is still completing. Manager stays open until it drains.')
                event.ignore(); return
            if not self.owned_runtime_panel.permit_close():
                self.open_ollama_workspace()
                self.statusBar().showMessage(self.owned_runtime_panel.message)
                event.ignore(); return
            if self.tag_database_pane is not None:
                if not self.tag_database_pane.controller.closed:
                    self.tag_database_pane.controller.begin_close()
                self.tag_database_pane.render()
            if self.backend_setup_controller is not None: self.backend_setup_controller.begin_close()
            if self.backend_setup_dialog is not None: self.backend_setup_dialog.render()
            if self.lifecycle_bridge is not None and self.lifecycle_bridge.module_enabled:
                self.lifecycle_bridge.begin_module_close()
            self.diagnostic_help_flow.begin_close()
            begin_resource_close(self.resource_route)
            self.render_local_resources()
            if self.database_preview_route is not None:
                self.database_preview_route.begin_close(); self.render_databases()
            try:
                decision = self.close_decision()
            except Exception:
                self.statusBar().showMessage("Close facts unavailable; ownership retained.")
                event.ignore(); return
            self._show_close_decision(decision)
            if not decision.allow_close:
                event.ignore(); self.render_library(); return
            if self.lifecycle_bridge is not None and not self.lifecycle_bridge.finish_ui_closed():
                self.statusBar().showMessage("Lifecycle worker teardown is unconfirmed; Manager stays open.")
                event.ignore(); return
            if self.database_preview_route is not None and not self.database_preview_route.quiescent:
                self.statusBar().showMessage("Preview completion is unconfirmed; Manager stays supervised.")
                event.ignore(); return
            if resource_close_held(self.resource_route):
                self.statusBar().showMessage("Local measurement worker teardown is unconfirmed; Manager stays open.")
                event.ignore(); return
            if not self.owned_runtime_panel.permit_close(final=True):
                self.statusBar().showMessage(self.owned_runtime_panel.message)
                event.ignore(); return
            if self.chat_host is not None and not self.chat_host.finish_close():
                event.ignore(); return
            if self.direct_model_host is not None and not self.direct_model_host.finish_close():
                event.ignore();return
            self.close_policy.finish()
            if self.reader_dialog is not None:self.reader_dialog.close()
            if hasattr(self, 'chat_timer'): self.chat_timer.stop()
            if hasattr(self,'direct_model_timer'):self.direct_model_timer.stop()
            if self.tag_database_pane is not None:
                self.tag_database_pane.timer.stop()
            if self.backend_setup_controller is not None: self.backend_setup_controller.finish_close()
            self.close_timer.stop(); self.lifecycle_timer.stop()
            self.cancel_close_button.hide()
            super().closeEvent(event)

        def select_application_category(self, category):
            self.application_filter_state = select_category(self.application_filter_state, category)
            self.render_library()

        def clear_library_filters(self):
            self.application_filter_state = ApplicationFilterState()
            self.application_categories.sync_section("All categories")
            self.library_filter.setCurrentIndex(0); self.library_search.clear(); self.render_library()

        def app_detail_text(self, entry):
            from suite_core.installation import ComponentKind, removal_blockers
            registry = self.view.installation_registry
            lines = [entry.title, f"Component: {entry.component_id.value}", "Install/available state: unknown.",
                f"Catalog source: {entry.source_ref} | observed: {entry.observed_at.isoformat()}", "",
                "Optional modules / add-ons"]
            if entry.candidate_package:
                from .candidate_inventory import LAUNCH_HOLD
                artifact = entry.candidate_package
                lines[4:4] = ["Developer candidate package (distinct from supported catalog and installed application)",
                    f"Software: {display_component_version(entry.component_id.value, artifact.software_version, include_legacy=True)}; maximum snapshot reader: {artifact.snapshot_interface_version}; supported: {artifact.supported_snapshot_interfaces}",
                    f"Archive: {artifact.archive_path}", f"Archive SHA256: {artifact.archive_sha256}",
                    f"Accepted source plan SHA256: {artifact.source_plan_sha256}",
                    f"{artifact.size_bytes} bytes; {artifact.source_files} source/support/notice files validated.",
                    "Unsigned; unauthenticated; non-installable; non-self-contained; execution unqualified.",
                    "Installation and running UI state: unknown. Hash validation is relative to accepted pins, not publisher authentication.",
                    "Declared external requirements: " + "; ".join(f"{key}: {value}" for key, value in artifact.declared_dependencies),
                    LAUNCH_HOLD, "Inspection is a single explicit read; no refresh/polling or installation registry was created.", ""]
            if not entry.optional_addon_instances:
                lines.append("Host/add-on relationships: not supplied. Generic Modules kinds and dependency edges remain unclassified.")
            for addon in entry.optional_addon_instances:
                matches = [item for item in registry.entries if item.installation_id == addon] if registry else []
                if len(matches) != 1 or matches[0].kind is not ComponentKind.MODULE:
                    lines.append(f"Held add-on assertion: {addon.value}; matching Module instance not supplied.")
                else:
                    lines.append(f"Declared optional add-on: {addon.value} ({matches[0].component_id.value}). "
                        "Configured registry only; compatibility and installed host relationship unknown.")
            lines.extend(("", "Requirements / dependency impact", "Generic dependencies are requirements, not add-on compatibility."))
            apps = [item for item in registry.entries if item.kind is ComponentKind.APP and item.component_id == entry.component_id] if registry else []
            if not apps: lines.append("Matching App installation instance: not supplied; removal impact unknown.")
            for app in apps:
                lines.append(f"Configured App instance: {app.installation_id.value}; not installed-package proof.")
                lines.append("Declared requirements: " + (", ".join(dep.value for dep in app.dependencies) or "none declared; completeness unknown"))
                blockers = removal_blockers(registry, frozenset({app.installation_id}))
                lines.append("Removal-impact preview: " + (", ".join(owner.value for owner, dependency in blockers) or "none declared; removal safety unknown"))
            lines.extend(("", "Settings", "App-owned settings: not supplied; no preferences read or written.", "", "What's New"))
            notes = [note for note in self.view.release_notes if note[0] == entry.component_id.value]
            for identity, version, recorded, status, prose in notes:
                lines.append(f"{identity} {display_component_version(identity, version, include_legacy=True)} - {recorded or 'Date not recorded'} - {status}\n{prose}")
            if not notes: lines.append("App release notes: not supplied.")
            if offline_previews:
                from suite_core.manager.offline_preview.manager_preview_adapter import preview_text_for_component
                lines.extend(("", preview_text_for_component(offline_previews, entry.component_id)))
            lifecycle = self.lifecycle_controller.projection(self.lifecycle_controller.binding_for(entry), self.now())
            lines.extend(("", "Lifecycle: " + lifecycle["state"], lifecycle["reason"],
                "UI readiness: unknown; Open: disabled. Installed status remains unknown."))
            return "\n".join(lines)

        def open_app_details(self, entry):
            dialog = QDialog(self); dialog.setWindowTitle("Details - " + entry.title)
            layout = QVBoxLayout(dialog); browser = QTextBrowser(); browser.setPlainText(self.app_detail_text(entry))
            layout.addWidget(browser)
            related = related_application_entries(entry, self.view.installation_registry, self.view.supported_apps)
            if related:
                layout.addWidget(QLabel("Declared related components - details only; compatibility unknown"))
                for target in related:
                    button = QPushButton("Details: " + target.title)
                    button.clicked.connect(lambda checked=False, item=target: self.open_app_details(item))
                    layout.addWidget(button)
            uninstall = QPushButton("Uninstall"); uninstall.setEnabled(False)
            uninstall.setToolTip("Declared dependency impact is a preview only; removal action is not wired.")
            layout.addWidget(uninstall)
            open_button = QPushButton("Open"); open_button.setEnabled(False)
            open_button.setToolTip("Exact-session UI readiness and activation are not qualified.")
            layout.addWidget(open_button)
            buttons = QDialogButtonBox(QDialogButtonBox.Close); buttons.rejected.connect(dialog.reject); layout.addWidget(buttons)
            dialog.resize(850, 620); dialog.exec()

        def load_demo(self):
            self.set_view(synthetic_demo(self.now()))

        def inspect_candidate(self):
            self.statusBar().showMessage("Inspecting known developer archive - read-only")
            try:
                if not callable(candidate_inspector): raise ValueError("No package-inspection provider supplied.")
                observation = candidate_inspector(self.now())
                self.set_view(view_for_candidate(observation)); self.navigation.setCurrentRow(self.page_rows["Applications"])
                self.statusBar().showMessage("Candidate pins verified; installation and running state unknown; Launch held")
            except (OSError, ValueError) as error:
                self.statusBar().showMessage("Candidate inspection held; previous view retained: " + str(error)[:240])

        def toggle_compact(self):
            compact = self.navigation_panel.maximumWidth() != 72
            self.navigation.setFixedWidth(64 if compact else 212)
            self.navigation_panel.setFixedWidth(72 if compact else 220)
            self.sidebar_toggle.setChecked(compact)
            text = "Expand sidebar" if compact else "Collapse sidebar"
            self.sidebar_toggle.setText("\u203a" if compact else "\u2039")
            self.sidebar_toggle.setAccessibleName(text); self.sidebar_toggle.setToolTip(text)
            for row in range(self.navigation.count()):
                item = self.navigation.item(row)
                if not item.toolTip(): item.setToolTip(item.text())
                item.setData(Qt.AccessibleTextRole, item.toolTip())
                item.setText("" if compact else item.toolTip())

        def refresh_section_commands(self, *_):
            if not hasattr(self, "view") or self.resource_panel is None:
                return
            from core.manager_server_inventory import section_command_keys
            section = next((name for name, row in self.page_rows.items() if row == self.navigation.currentRow()), "Dashboard")
            refresh = bind_source(self, "refresh-local", self.resource_panel.refresh, "Servers", move=True)
            available = {"refresh-local": refresh, "inspect-candidate": self.inspection_command}
            context = [available[key] for key in section_command_keys(section)
                if key != "inspect-candidate" or self.view.synthetic or callable(candidate_inspector)]
            self.commands = self.navigation_commands + [refresh] + self.global_commands
            if self.view.synthetic or callable(candidate_inspector):
                self.commands.append(self.inspection_command)
            self.bar.set_commands(section, context + self.global_commands,
                groups=(section, "View", "Help") if context else ("View", "Help"))

        def search_commands(self):
            dialog = CommandSearchDialog(self.commands, "Manager", self); dialog.exec()

        def poll_diagnostics_help(self):
            if self.diagnostic_help_flow.poll() and self.diagnostics_dialog is not None:
                self.diagnostics_dialog.render()

        def open_diagnostics(self):
            from .diagnostic_help_candidate import create_diagnostics_dialog
            if self.diagnostics_dialog is not None:
                self.diagnostics_dialog.raise_()
                return
            dialog = create_diagnostics_dialog(self, self.diagnostic_help_flow)
            self.diagnostics_dialog = dialog
            try:
                dialog.exec()
            finally:
                self.diagnostics_dialog = None

        def open_notes(self):
            dialog = QDialog(self); dialog.setWindowTitle("What's New \u2014 release history")
            layout = QVBoxLayout(dialog); chooser = QComboBox(); browser = QTextBrowser()
            status = QLabel("Current build\n" + "\n".join(self.current_build.lines()))
            status.setObjectName("currentBuildStatus"); status.setWordWrap(True)
            layout.addWidget(status); layout.addWidget(QLabel("Historical release notes"))
            entries = (("suite-manager", VERSION, DATE, "Developer preview release notes", NOTES),) + self.view.release_notes
            for identity, version, recorded, status, notes in entries:
                chooser.addItem(f"{identity} {display_component_version(identity, version)} \u2014 {recorded or 'Date not recorded'} \u2014 {status}")
            chooser.currentIndexChanged.connect(lambda index: browser.setMarkdown(entries[index][4]))
            browser.setMarkdown(entries[0][4]); layout.addWidget(chooser); layout.addWidget(browser)
            buttons = QDialogButtonBox(QDialogButtonBox.Close); buttons.rejected.connect(dialog.reject); layout.addWidget(buttons)
            dialog.resize(850, 580); dialog.exec()

    return ManagerWindow()
