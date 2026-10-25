"""Inactive facade over the canonical admitted Organizer shell."""
from types import MappingProxyType
from typing import Callable, Mapping

# Re-export original contracts: legacy imports retain canonical object identity.
from .contracts import (
    Availability, BoundaryError, ExistingContext, OptionalBackendStatus,
    OptionalBackendUnavailable, PAGE_INDEX, SERVICE_NAMES, Shell, Workspace,
)


class MediaOrganizer:
    """Focused command facade over the unchanged canonical application shell.

    Exposing this facade does not hide other pages or revoke their navigation:
    contextual review, Tag editing and dependency recovery remain native routes.
    """
    def __init__(self, context: ExistingContext, host: Shell,
                 optional_backends: Mapping[str, Callable[[], OptionalBackendStatus]] | None = None):
        context.validate()
        if getattr(host, "startup_cancelled", True):
            raise BoundaryError("Canonical host startup has not been admitted.")
        if host.state is not context.state or host.settings is not context.settings:
            raise BoundaryError("Host must retain the supplied state and settings identities.")
        for name in ("choose_library", "open_completed_dataset_image", "edit_image_data", "close"):
            if not callable(getattr(host, name, None)):
                raise BoundaryError("Canonical host route missing: " + name)
        if not callable(getattr(getattr(host, "navigation", None), "setCurrentRow", None)):
            raise BoundaryError("Canonical page navigation is required.")
        self.context = context
        self.host = host
        self._services = {name: getattr(context.state, name) for name in SERVICE_NAMES}
        adapters = dict(optional_backends or {})
        if any(not isinstance(name, str) or not name.strip() or not callable(adapter)
               for name, adapter in adapters.items()):
            raise BoundaryError("Optional backends require named callable adapters.")
        self._optional_backends = MappingProxyType(adapters)

    def _check_context(self):
        self.context.validate()
        if self.host.state is not self.context.state or self.host.settings is not self.context.settings:
            raise BoundaryError("Host authority changed; explicitly reattach the candidate.")
        if getattr(self.host, "startup_cancelled", True):
            raise BoundaryError("Canonical host is unavailable.")
        if any(getattr(self.context.state, name) is not service
               for name, service in self._services.items()):
            raise BoundaryError("Service authority changed; explicitly reattach the candidate.")

    def open_workspace(self, workspace: Workspace):
        if not isinstance(workspace, Workspace):
            raise BoundaryError("Choose a named Media Organizer workspace.")
        self._check_context()
        # Preserve native leave/draft/setup-return guards through the connected nav.
        return self.host.navigation.setCurrentRow(PAGE_INDEX[workspace])

    def choose_libraries(self):
        self._check_context()
        return self.host.choose_library()

    def open_completed_image(self, path: str):
        self._check_context()
        # Native host alone validates included scope, existence and pending refresh.
        return self.host.open_completed_dataset_image(path)

    def edit_image(self, path: str):
        self._check_context()
        return self.host.edit_image_data(path)

    def _enter_workspace(self, workspace: Workspace) -> bool:
        """Use native leave guards; do not deliver actions to a hidden workspace."""
        self._check_context()
        stack = getattr(self.host, "stack", None)
        current_index = getattr(stack, "currentIndex", None)
        if not callable(current_index):
            raise BoundaryError("Canonical current-workspace inspection is required.")
        self.open_workspace(workspace)
        # Navigation may run callbacks that change the borrowed context.
        self._check_context()
        if getattr(self.host, "stack", None) is not stack:
            raise BoundaryError("Canonical workspace authority changed during navigation.")
        return current_index() == PAGE_INDEX[workspace]

    def _ingest_command(self, name: str):
        if not self._enter_workspace(Workspace.INCOMING):
            return False
        page = getattr(self.host, "ingest_page", None)
        if (getattr(page, "state", None) is not self.context.state
                or getattr(page, "settings", None) is not self.context.settings):
            raise BoundaryError("The Ingest page must retain the admitted context.")
        command = getattr(page, name, None)
        if not callable(command):
            raise BoundaryError("Canonical Ingest command unavailable: " + name)
        return command()

    def add_images(self):
        """Open native selected-file Add Images review, without executing transfers.

        The native chooser/preview retains cancellation, busy holds and Copy/Move
        decisions. Its return value is not evidence of completed image intake.
        """
        return self._ingest_command("choose_ingest_image_files")

    def review_added_images(self):
        """Review the native latest intake result without replacing gallery scope."""
        return self._ingest_command("review_added_images")

    def view_library_image(self, path: str):
        """View one exact included image through native scope/loading checks.

        False includes a declined native page switch. A native True may mean a
        deferred handoff; it does not certify visible inspection or image writes.
        """
        if not self._enter_workspace(Workspace.LIBRARY):
            return False
        return self.open_completed_image(path)

    def optional_backend_status(self, name: str) -> OptionalBackendStatus:
        self._check_context()
        adapter = self._optional_backends.get(name)
        if adapter is None:
            return OptionalBackendStatus(Availability.UNKNOWN,
                "No optional backend status adapter is connected; local organization remains available.")
        try:
            status = adapter()
        except OptionalBackendUnavailable:
            # Do not echo exception text that could contain private paths or prompts.
            return OptionalBackendStatus(Availability.UNAVAILABLE,
                "Optional backend unavailable; use local organization or the existing Setup recovery workflow.")
        if not isinstance(status, OptionalBackendStatus):
            raise BoundaryError("Optional adapter returned an invalid status.")
        return status

    def close(self):
        # Always permit native close/recovery even if context identities drifted.
        return self.host.close()


def compose(context: ExistingContext, *, build_host: Callable[..., Shell],
            genuinely_new_profile: bool,
            optional_backends: Mapping[str, Callable[[], OptionalBackendStatus]] | None = None) -> MediaOrganizer:
    """Explicit future entry seam; caller owns admitted startup and Qt lifecycle.

    Integration supplies TagManagerWindow itself as build_host. No default loader,
    profile discovery, QApplication, event loop, show(), scan or process is added.
    Prefer MediaOrganizer(context, already_admitted_window) for the first milestone.
    A created host failing admission remains the caller's cleanup responsibility.
    """
    context.validate()
    if type(genuinely_new_profile) is not bool or not callable(build_host):
        raise BoundaryError("Explicit profile status and canonical host factory are required.")
    host = build_host(settings=context.settings, state=context.state,
                      genuinely_new_profile=genuinely_new_profile)
    return MediaOrganizer(context, host, optional_backends)
