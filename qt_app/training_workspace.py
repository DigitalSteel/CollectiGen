"""Connect the persistent Training page to existing Dataset and Setup owners."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt

from core.concept_groups import group_fingerprint

def create_training_page(shell):
    from core.paths import USER_DATA_DIR, TRAINING_PLAN_DRAFTS_PATH
    from core.training_workspace import TrainingWorkspace
    from .training_page import TrainingPage
    from .training_workflow_controller import shared_training_workflow
    page = TrainingPage(shell.state.jobs, TrainingWorkspace(
        concept_store_path=USER_DATA_DIR / "concept_groups.json",
        draft_store_path=TRAINING_PLAN_DRAFTS_PATH,
    ), shared_training_workflow(shell.state, shell), shell)
    page.collection_requested.connect(lambda group: open_training_workspace(shell, group=group))
    page.draft_requested.connect(lambda draft: open_training_workspace(shell, draft=draft))
    page.dataset_requested.connect(lambda: _collections(shell))
    page.images_requested.connect(lambda: _images(shell))
    page.setup_requested.connect(lambda: _setup(shell))
    page.models_requested.connect(lambda: _models(shell))
    page.package_requested.connect(lambda destination: open_training_package(shell, destination))
    page.package_recovery_requested.connect(lambda receipt: open_training_package(shell, receipt, recovery=True))
    page.retry_package_requested.connect(lambda: retry_training_package(shell))
    page.cancel_training_requested.connect(lambda: _cancel_training(shell))
    return page


def _cancel_training(shell):
    page = shell.training_page
    page._training_request_token = getattr(page, "_training_request_token", 0) + 1
    task = getattr(page, "_training_recheck_task", None)
    if task is not None and page._task is task:
        page.cancel_check()
    page._training_recheck_task = None
    dataset = getattr(shell, "dataset_page", None)
    if dataset is not None and (dataset._training_candidate_task is not None
                                or dataset._training_package_task is not None):
        task = dataset._training_candidate_task or dataset._training_package_task
        if hasattr(task, "cancel"):
            dataset.cancel_data_task()
            page.set_planning(True, "Cancelling Training preparation safely…")
        else:
            page.set_planning(True, "This Training recovery step must finish safely before it can close.")
    else:
        page.set_planning(False, "Training preparation cancelled before it started.")


def _collections(shell, group_id=""):
    shell.navigation.setCurrentRow(2)
    if shell.stack.currentIndex() != 2:
        return
    shell.training_page.refresh_collections_on_return()
    shell._queue_dataset_page_action(lambda: shell.dataset_page.open_concept_groups(selected_group_id=group_id))


def _images(shell):
    """Return to the Dataset gallery so the user can choose exact members."""
    shell.training_page.refresh_collections_on_return()
    shell.navigation.setCurrentRow(2)


def _setup(shell):
    # Use the canonical nonmodal Setup controller. Training stays visible and its
    # exact draft remains in the page while the existing Setup queue does work.
    shell._ensure_page(6)
    shell.setup_page.open_training_setup()


def _models(shell):
    shell._ensure_page(6)
    shell.setup_page.open_models_workspace()


def open_training_workspace(shell, *, group=None, draft=None, collection_reviewed=False):
    shell.navigation.setCurrentRow(8)
    if shell.stack.currentIndex() != 8:
        return False
    shell._ensure_page(8)
    page = shell.training_page
    if page._review is not None or page._planning:
        page.show_review()
        page.status.setText("Continue or close the current training review before opening another plan.")
        return False
    if group is None and draft is None:
        page.show_overview()
        return True
    page.clear_package_retry()
    if group is not None and group.get("rules") is not None and not collection_reviewed:
        # Dataset alone owns the saved automatic-membership refresh and its
        # dirty/recovery guards. Its Build Training Data action returns here.
        page.status.setText("Refresh the selected collection in Dataset, then choose Build Training Data.")
        _collections(shell, group["id"])
        return False

    page._training_request_token = getattr(page, "_training_request_token", 0) + 1
    page._training_cancellable = True
    request_token = page._training_request_token

    def current():
        return request_token == page._training_request_token

    def clear_if_current():
        if current():
            page._training_recheck_task = None
            page.set_planning(False)

    def begin():
        if not current():
            return False
        dataset = shell.dataset_page
        if page._shutdown or page._review is not None:
            clear_if_current()
            return False

        def plan(selected):
            if not current():
                return False
            page._training_recheck_task = None
            if page._shutdown or page._review is not None:
                clear_if_current()
                return False
            if draft is not None:
                dataset.resume_training_dataset_draft(draft=selected)
            else:
                dataset.plan_concept_training(selected)
            active = dataset._training_candidate_task is not None
            if active:
                page._training_retry_action = "Resume Draft" if draft is not None else "Prepare Training"
            page.set_planning(active, "Inspecting the frozen training scope…" if active else "Training preparation was not started. Choose the collection or draft to retry.")
            return active

        if collection_reviewed:
            return plan(group)

        # The catalog checked the displayed revision before this route, but
        # Dataset can be initialized later. Recheck the exact saved identity at
        # delivery before asking Dataset to freeze any training input.
        if draft is not None:
            resolve = lambda: page.catalog.resolve_draft(draft.draft_id, draft.fingerprint)
        else:
            resolve = lambda: page.catalog.resolve_collection(group["id"], group_fingerprint(group))
        if page._task is not None:
            clear_if_current()
            page.status.setText("Finish the current saved-work check, then choose the training item again.")
            return False
        retry = "Resume Draft" if draft is not None else "Prepare Training"
        page._start(
            "Rechecking the selected saved training work…", resolve, plan,
            cancel_message=f"Training preparation cancelled during the saved-work check. Choose {retry} again to retry; no plan was opened.",
        )
        task = page._task
        if task is None:
            clear_if_current()
            return False
        page._training_recheck_task = task
        task.signals.complete.connect(
            lambda *_: clear_if_current() if page._cancelled or page._shutdown else None
        )
        task.signals.failed.connect(lambda *_: clear_if_current())
        task.signals.cancelled.connect(clear_if_current)
        return True

    page.set_planning(True,"Opening the Dataset context for the selected training scope…")
    queued = bool(shell._queue_dataset_page_action(begin))
    if not queued and page._planning:
        clear_if_current()
        page.status.setText("Training preparation was not started. Choose the collection or draft to retry.")
    return queued


def retain_training_review(shell, dialog, finished):
    shell.navigation.setCurrentRow(8)
    shell._ensure_page(8)
    page = shell.training_page
    page._dataset_training_active = False
    page._training_retry_action = None
    page.set_planning(False, "Review the frozen data and settings. Save Draft keeps this plan for reopening.")
    return page.retain_review(dialog, finished)


def open_training_package(shell, destination, *, recovery=False):
    """Reopen a user-selected package through the canonical verifier and review."""
    if not open_training_workspace(shell):
        return False
    page = shell.training_page
    page.clear_package_retry()
    if page._task is not None or page._shutdown:
        page.status.setText("Finish the saved-work check before opening a training package.")
        return False
    page._training_request_token = getattr(page, "_training_request_token", 0) + 1
    page._training_cancellable = True
    request_token = page._training_request_token

    def begin():
        if request_token != page._training_request_token or page._shutdown:
            return False
        dataset = shell.dataset_page
        # A receipt selected after restart has no authority to reuse an unrelated
        # prior candidate plan. The canonical recovery owner will require its exact
        # plan again before publishing a completed staging directory.
        dataset._training_package_plan = None
        dataset._training_package_recovery_return = None
        if recovery:
            dataset._start_training_package_recovery_preview(Path(destination))
        else:
            dataset._start_training_package_verify(Path(destination))
        active = dataset._training_package_task is not None
        page.set_planning(active, "Verifying the selected training package…" if active else
                          "Training package verification was not started. Try again when Dataset is ready.")
        return active

    page.set_planning(True, "Opening the existing training package for verification…")
    queued = bool(shell._queue_dataset_page_action(begin))
    if not queued and request_token == page._training_request_token:
        page.set_planning(False)
    return queued


def remember_training_package_preview(shell, candidate_plan, destination):
    """Retain a retry only for the exact saved draft currently in Training."""
    page = getattr(shell, "training_page", None)
    dataset = getattr(shell, "dataset_page", None)
    if page is None:
        return
    if dataset is None or shell.stack.currentIndex() != 8:
        page.clear_package_retry()
        return
    if dataset._training_candidate_plan is not candidate_plan:
        page.clear_package_retry()
        return
    page.remember_package_preview(candidate_plan, destination)


def retry_training_package(shell):
    """Recheck the saved draft before a fresh canonical package preview."""
    page = shell.training_page
    intent = page._package_retry_intent
    if (intent is None or page._task is not None or page._planning or page._shutdown
            or page._review is not None):
        return
    key, fingerprint, candidate_plan, destination = intent
    item = page.items.currentItem()
    row = item.data(0, Qt.UserRole) if item else None
    dataset = getattr(shell, "dataset_page", None)
    if dataset is not None and (getattr(dataset, "_training_candidate_task", None) is not None
                                or getattr(dataset, "_training_package_task", None) is not None):
        page.status.setText("Finish the current Dataset work before retrying this package preview.")
        return
    if (page.kind.currentData() != "drafts" or row is None
            or (row.key, row.fingerprint) != (key, fingerprint)
            or dataset is None or dataset._training_candidate_plan is not candidate_plan):
        page.clear_package_retry()
        page.status.setText("The saved draft or package context changed. Choose the current draft and Resume Draft again.")
        return

    def recheck():
        try:
            page.catalog.resolve_draft(key, fingerprint)
        except Exception as exc:
            return str(exc)
        return ""

    def ready(error):
        if error:
            page.clear_package_retry()
            page.status.setText(f"{error} Choose Refresh, then Resume Draft to review the current saved plan.")
            return
        current = page.items.currentItem()
        current_row = current.data(0, Qt.UserRole) if current else None
        if (page._package_retry_intent is not intent or page._shutdown
                or current_row is None or (current_row.key, current_row.fingerprint) != (key, fingerprint)
                or dataset._training_candidate_plan is not candidate_plan):
            page.clear_package_retry()
            page.status.setText("The package context changed. Resume the current draft before previewing again.")
            return
        dataset._start_training_package_preview(candidate_plan, destination)
        if dataset._training_package_task is None:
            page.status.setText("Package preview could not start. Finish other Dataset work, then retry.")

    page._start("Rechecking the saved draft for a fresh package preview…", recheck, ready)


def sync_training_progress(shell):
    """Mirror only an already-running Dataset operation; perform no data reads."""
    page = getattr(shell, "training_page", None)
    dataset = getattr(shell, "dataset_page", None)
    if page is None or dataset is None:
        return
    busy = dataset._training_candidate_task is not None or dataset._training_package_task is not None
    if busy:
        page._dataset_training_active = True
        page._training_cancellable = hasattr(dataset._training_candidate_task or dataset._training_package_task, "cancel")
        page.set_planning(True, dataset.header.status.text())
    elif getattr(page, "_dataset_training_active", False):
        page._dataset_training_active = False
        message = dataset.header.status.text() if page._review is None else ""
        retry = getattr(page, "_training_retry_action", None)
        page._training_retry_action = None
        if retry and message.startswith("Training dataset planning cancelled safely."):
            message += f" Choose {retry} again to retry."
        elif getattr(page, "_package_retry_intent", None) is not None:
            if message.startswith("Training package preview cancelled safely."):
                message += " Choose Retry Package Preview to recheck the saved draft, sources and destination."
            elif message == "Training package work stopped":
                message = ("Package preview stopped. Correct the issue shown, then choose Retry Package Preview; "
                           "Resume Draft if the plan needs changes.")
        elif page.items.currentItem() is not None and message.startswith(
                "Training package preview cancelled safely."):
            retry = "Resume Draft" if page.kind.currentData() == "drafts" else "Prepare Training"
            message += f" Choose {retry} again to recheck the source and choose a destination."
        page.set_planning(False, message)
