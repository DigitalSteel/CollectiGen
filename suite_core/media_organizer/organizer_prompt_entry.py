"""Organizer-only image action and close binding; no Generate workspace imports."""
from copy import deepcopy
from pathlib import Path
import weakref


def _window_live(window):
    from shiboken6 import isValid
    return window is not None and isValid(window) and not getattr(window, '_organizer_prompt_closing', False)


def install_prompt_action(window, *, open_dialog=None):
    page = getattr(window, 'dataset_page', None)
    button = getattr(page, 'inspector_actions', None)
    if button is None or getattr(page, '_organizer_prompt_action', None) is not None:return
    menu = button.menu()
    if menu is None:return
    action = menu.addAction('Suggest Prompt from Image…')
    action.setToolTip('Review local image text for a target model, then copy or export an editable prompt.')
    page._organizer_prompt_action = action
    action.triggered.connect(lambda: (open_dialog or open_prompt_dialog)(window, page))


def confirm_prompt_close(window):
    dialog = getattr(window, '_organizer_prompt_dialog', None)
    from shiboken6 import isValid
    return dialog is None or not isValid(dialog) or dialog.confirm_draft_loss()


def request_prompt_shutdown(window):
    window._organizer_prompt_closing = True
    task = getattr(window, '_organizer_prompt_prepare', None)
    if task is not None:
        window._organizer_prompt_prepare = None  # Reject queued delivery before cancellation/unobserve.
        task.cancel()
        window._organizer_prompt_draining_prepare = task
        # The existing parent shutdown_background_jobs owns terminal worker drain.
    dialog = getattr(window, '_organizer_prompt_dialog', None)
    from shiboken6 import isValid
    if dialog is not None and isValid(dialog):dialog.request_background_shutdown()


def finish_prompt_close(window, accepted):
    dialog = getattr(window, '_organizer_prompt_dialog', None)
    from shiboken6 import isValid
    if accepted:
        if dialog is not None and isValid(dialog):dialog.finish_parent_close()
    else:
        # Another owner may have declined close, or the shared coordinator is still draining.
        window._organizer_prompt_closing = False
        if dialog is not None and isValid(dialog):dialog.resume_after_cancelled_shutdown()


def _source_identity(path):
    from core.training_source_io import read_stable_training_source
    if Path(path).stat().st_size > 64 * 1024 * 1024:
        raise ValueError('This image exceeds the 64 MiB prompt-source limit. Continue with a manual draft.')
    _, image = read_stable_training_source(path)
    result = {'image_sha256': image['sha256']}
    for suffix in ('.tags.json', '.txt'):
        sidecar = Path(path).with_suffix(suffix)
        if sidecar.is_file() and sidecar.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("This image's saved text exceeds the bounded prompt-source limit.")
        result[suffix] = read_stable_training_source(sidecar)[1]['sha256'] if sidecar.is_file() else None
    return result


def _prepare_source(path, registry):
    from core.image_record import ImageRecord
    identity = _source_identity(path)
    record = ImageRecord(path)
    parts = []
    for text in list(record.annotation.sdxl_tags) + list(record.annotation.plain_language):
        if isinstance(text, str) and text.strip() and len(parts) < 32:
            parts.append({'id': f'saved-{len(parts)}', 'kind': 'other', 'text': text,
                          'origin': 'reviewed_image_knowledge', 'reviewed': True, 'selected': False})
    if _source_identity(path) != identity:raise ValueError('Source changed while preparing the prompt. Reopen this image.')
    checkpoints = []
    registry_issue = ''
    try:
        value, revision = registry.snapshot()
    except (OSError, ValueError, RuntimeError) as exc:
        value, revision = {'resources': {}}, 'unavailable'
        registry_issue = str(exc)
    for rid, row in value['resources'].items():
        if not rid.startswith('sha256:') or len(rid) != 71:continue
        declarations = row.get('declarations', [])
        if not any(d.get('role') in {'checkpoint', 'unet'} for d in declarations):continue
        checkpoints.append({'label': str(row.get('user', {}).get('display_name') or rid),
            'resource_id': rid, 'checkpoint_sha256': rid[7:], 'registry_revision': revision,
            'declarations': deepcopy(declarations), 'lineage': deepcopy(row.get('lineage', []))})
        if len(checkpoints) >= 256:break
    return {'path': str(path), 'identity': identity,
        'analysis': {'schema_version': 1, 'image_sha256': identity['image_sha256'],
                     'analysis_model_id': 'saved-image-knowledge/manual-review', 'parts': parts,
                     'source_identity': identity},
        'registry_issue': registry_issue,
        'recorded_metadata': [r.to_dict() for r in record.annotation.generation_records]}, checkpoints


def open_prompt_dialog(window, page, *, task_factory=None, dialog_factory=None, registry_factory=None, prepare_source=None):
    from PySide6.QtWidgets import QMessageBox
    if not _window_live(window):return
    if task_factory is None:
        from qt_app.workers import CoordinatedCallTask
        task_factory = CoordinatedCallTask
    if dialog_factory is None:
        from qt_app.organizer_prompt_dialog import OrganizerPromptDialog
        dialog_factory = OrganizerPromptDialog
    if registry_factory is None:
        from core.model_resource_registry import ModelResourceRegistry
        from core.paths import MODEL_RESOURCES_PATH
        registry_factory = lambda: ModelResourceRegistry(MODEL_RESOURCES_PATH)
    prepare_source = prepare_source or _prepare_source
    draining = getattr(window, '_organizer_prompt_draining_prepare', None)
    if draining is not None:
        job = getattr(getattr(draining, 'manager', None), 'jobs', {}).get(getattr(draining, 'job_id', ''))
        if job is None or job.state not in {'complete', 'failed', 'cancelled'}:return
        window._organizer_prompt_draining_prepare = None
    path = getattr(page, 'current_path', None)
    if path is None:
        QMessageBox.information(page, 'Choose an image', 'Inspect one Dataset image, then choose Suggest Prompt from Image.');return
    existing = getattr(window, '_organizer_prompt_dialog', None)
    if existing is not None:
        existing.show(); existing.raise_(); existing.activateWindow();return
    if getattr(window, '_organizer_prompt_prepare', None) is not None:return
    path = Path(path)
    state = window.state
    window_ref = weakref.ref(window)
    def current_owner(task):
        owner = window_ref()
        return owner if _window_live(owner) and getattr(owner, '_organizer_prompt_prepare', None) is task else None
    def show(result, task):
        owner = current_owner(task)
        if owner is None:return
        owner._organizer_prompt_prepare = None
        source, checkpoints = result
        dialog = dialog_factory(source=source, checkpoints=checkpoints,
            settings=state.settings, jobs=state.jobs, source_current_provider=lambda: _source_identity(path), parent=owner)
        owner._organizer_prompt_dialog = dialog
        def released(_):
            parent = window_ref()
            if _window_live(parent) and getattr(parent, '_organizer_prompt_dialog', None) is dialog:
                parent._organizer_prompt_dialog = None
        dialog.finished.connect(released)
        dialog.show()
    def failed(error, task):
        owner = current_owner(task)
        if owner is None:return
        owner._organizer_prompt_prepare = None
        QMessageBox.information(owner, 'Prompt source unavailable', str(error) + '\nYour image and metadata were not changed.')
    def work(job):
        if job.cancel_requested:raise InterruptedError('Source preparation cancelled.')
        result = prepare_source(path, registry_factory())
        if job.cancel_requested:raise InterruptedError('Source preparation cancelled.')
        return result
    task = task_factory(state.jobs, 'Prepare one prompt source', work, resource='io', key=f'organizer-prompt-source:{id(window)}')
    window._organizer_prompt_prepare = task
    window.destroyed.connect(lambda *_: task.cancel())
    task.signals.complete.connect(lambda result, active=task: show(result, active))
    task.signals.failed.connect(lambda error, active=task: failed(error, active))
    task.signals.cancelled.connect(lambda active=task: failed('Source preparation cancelled.', active))
    try:task.start()
    except Exception as exc:failed(str(exc), task)
