"""Shared workspace admission and exact-selection history launchers."""
from __future__ import annotations

from PySide6.QtCore import Qt

from core.history_configuration import HistoryConfigurationService
from core.history_workflow import configured_capture


def history_configuration(page):
    configured = getattr(page.state, "history_configuration", None)
    return configured if configured is not None else HistoryConfigurationService(page.settings)


def capture_operation(page, images, *, operation_kind, reason="", cancelled=lambda: False):
    """Freeze optional capture; callers retain their draft when admission fails."""
    return configured_capture(history_configuration(page), images,
        operation_kind=operation_kind, reason=reason, cancelled=cancelled)


def structured_history_configuration(page):
    from core.structured_history_configuration import StructuredHistoryConfigurationService
    image_configuration = history_configuration(page)
    return StructuredHistoryConfigurationService(page.settings,
        settings_path=image_configuration.settings_path)


def history_dialogs_busy(page):
    return any(dialog._task is not None for dialog in getattr(page, "_history_dialogs", ()))


def close_history_dialogs(page):
    """Request safe closure; the page remains alive until retained jobs drain."""
    for dialog in tuple(getattr(page, "_history_dialogs", ())):
        dialog.reject()
    return history_dialogs_busy(page)


def open_history(page, images, *, operation_target=None, on_complete=None, recovery_handler=None, on_organization_outcomes=None):
    """Open a retained nonmodal browser for exact images and an optional exact ID."""
    from core.history_browser import HistoryBrowser
    from .history_workspace_dialog import HistoryWorkspaceDialog

    images = tuple(images)
    browser = HistoryBrowser(history_configuration(page), images,
        operation_target=operation_target, knowledge=page.state.knowledge,
        characters=getattr(page, "characters", None))
    domains = ()
    if not operation_target and images and getattr(page.state, "moves", None) is not None:
        domains = (_organization_domain(page, paths=images),)
    dialog = HistoryWorkspaceDialog(page.state.jobs, page, image_browser=browser,
        recovery_handler=recovery_handler, domains=domains)
    if on_organization_outcomes is not None:
        dialog.restorationFinished.connect(lambda outcomes: on_organization_outcomes(tuple(
            value for value in outcomes if hasattr(value, "inverse_operation_id"))))
    return _retain_dialog(page, dialog, on_complete, image_only=on_organization_outcomes is not None)


def _organization_domain(page, **scope):
    from core.organization_history import OrganizationHistoryService
    from core.organization_history_browser import OrganizationHistoryBrowser
    from .history_domain_panel import OrganizationHistoryPanel
    service = getattr(page.state, "organization_history", None)
    if service is None:
        service = OrganizationHistoryService(page.state.moves)
    browser = OrganizationHistoryBrowser(service, **scope)
    return (browser.domain_key, browser.label, lambda jobs, parent: OrganizationHistoryPanel(browser, jobs, parent))


def open_organization_history(page, *, paths=(), entity_ids=(), operation_targets=(), group_id="", on_complete=None, on_outcomes=None):
    from .history_workspace_dialog import HistoryWorkspaceDialog
    domain = _organization_domain(page, paths=paths, entity_ids=entity_ids,
        operation_targets=operation_targets, group_id=group_id)
    dialog = HistoryWorkspaceDialog(page.state.jobs, page, domains=(domain,), initial_domain="organization")
    if on_outcomes is not None:
        dialog.restorationFinished.connect(on_outcomes)
    return _retain_dialog(page, dialog, on_complete)


def open_collection_history(page, configuration, canonical, group_id, dataset, *, scope_current, on_complete, observation_only=False):
    from core.collection_history_browser import CollectionHistoryBrowser
    from .collection_history_panel import CollectionHistoryPanel
    from .history_workspace_dialog import HistoryWorkspaceDialog
    browser = CollectionHistoryBrowser(configuration, canonical, group_id, dataset,
        scope_current=scope_current, observation_only=observation_only)
    domain = (browser.domain_key, browser.label,
        lambda jobs, parent: CollectionHistoryPanel(browser, jobs, parent))
    dialog = HistoryWorkspaceDialog(page.state.jobs, page, domains=(domain,), initial_domain="collections")
    dialog.restorationFinished.connect(on_complete)
    return _retain_dialog(page, dialog, None)


def open_recycle_history(page, record_ids, *, action="restore_origin", destination=None, destination_profile=None, initial_previews=None, on_outcomes=None):
    from core.recycle_history_browser import RecycleHistoryBrowser
    from .history_domain_panel import OrganizationHistoryPanel
    from .history_workspace_dialog import HistoryWorkspaceDialog

    browser = RecycleHistoryBrowser(page.state.recycle, record_ids, action=action,
        destination=destination, destination_profile=destination_profile, initial_previews=initial_previews)
    domain = (browser.domain_key, browser.label,
        lambda jobs, parent: OrganizationHistoryPanel(browser, jobs, parent))
    dialog = HistoryWorkspaceDialog(page.state.jobs, page, domains=(domain,), initial_domain="recycle")
    if on_outcomes is not None:
        dialog.restorationFinished.connect(on_outcomes)
    return _retain_dialog(page, dialog, None)


def _retain_dialog(page, dialog, on_complete, *, image_only=False):
    dialog.setAttribute(Qt.WA_DeleteOnClose, True)
    dialogs = getattr(page, "_history_dialogs", None)
    if dialogs is None:
        dialogs = page._history_dialogs = []
    dialogs.append(dialog)
    def finished(_result):
        if dialog in dialogs:
            dialogs.remove(dialog)
        notify = getattr(page, "_notify_background_work_finished", None)
        if callable(notify):
            notify()
    dialog.finished.connect(finished)
    if on_complete is not None:
        def restored(outcomes):
            images = tuple(dict.fromkeys(getattr(item, "image", getattr(item, "current_path", None))
                for item in outcomes if item.status == "complete" and (not image_only or hasattr(item, "image"))))
            images = tuple(image for image in images if image is not None)
            if images:
                on_complete(images)
        dialog.restorationFinished.connect(restored)
    dialog.show()
    return dialog
