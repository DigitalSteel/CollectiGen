"""Help review UI candidate. No recorder, executor, startup capture or telemetry.

The approved host must supply nonblocking request/delivery and lifetime proof.
Opening/rendering this screen only inspects supplied immutable safe states.
"""
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from threading import get_ident
from typing import Callable

from suite_core.diagnostic_review_candidate import ReviewState, ReviewStatus, ReviewIssue, _immutable_preview
from suite_core.diagnostics import _decode, _validate, _valid_runtime, MAX_EVENTS, MAX_RECORD_BYTES

MEMBERS = ('diagnostics.json', 'runtime.json')


class HelpAction(str, Enum):
    PREVIEW = 'preview'
    EXPORT = 'export'


@dataclass(frozen=True)
class HelpRequest:
    sequence: int
    action: HelpAction
    review: ReviewState
    destination: Path | None = None


@dataclass(frozen=True)
class DiagnosticsHelpProvider:
    """Trusted host contract, not an executor implementation or privacy sandbox.

    submit(request)->bool: True admitted, False definitely not started.
    poll()->None or (same request, ReviewState): UI-owner completion only.
    close_held()->bool: retained background ownership, including teardown.
    begin_close()->None: seal requests; queued-only cancellation, no forced stop.
    All three are quick/nonblocking and cannot run review/export on the UI.
    """
    submit: Callable
    poll: Callable
    close_held: Callable
    begin_close: Callable = lambda: None


def safe_review(state):
    """Reuse exporter validators before displaying any supplied preview bytes."""
    try:
        if type(state) is not ReviewState or not isinstance(state.status, ReviewStatus) or not isinstance(state.issue, ReviewIssue):
            return False
        if state.preview is None:
            return state.status in {ReviewStatus.UNAVAILABLE, ReviewStatus.NOT_REVIEWED} and state.selected == () and state.hashes == ()
        if not _immutable_preview(state.preview):
            return False
        if (type(state.selected) is not tuple or any(type(name) is not str or name not in MEMBERS for name in state.selected)
            or len(set(state.selected)) != len(state.selected) or type(state.selection_confirmed) is not bool):
            return False
        for name, payload in state.preview.files:
            data = _decode(payload, limit=MAX_EVENTS * MAX_RECORD_BYTES + 1024)
            if name == 'diagnostics.json':
                if (type(data) is not dict or set(data) != {'schema', 'events'}
                    or type(data['schema']) is not int or data['schema'] != 1
                    or type(data['events']) is not list or len(data['events']) > MAX_EVENTS
                    or not all(_validate(row) for row in data['events'])):
                    return False
            elif not _valid_runtime(data):
                return False
        if state.status is ReviewStatus.EXPORTED:
            return (type(state.hashes) is tuple and len(state.hashes) == len(state.selected) + 1
                and set(name for name, digest in state.hashes) == set(state.selected) | {'manifest.json'}
                and all(type(digest) is str and len(digest) == 64 and all(c in '0123456789abcdef' for c in digest)
                        for name, digest in state.hashes))
        return state.hashes == ()
    except Exception:
        return False


class DiagnosticsHelpFlow:
    """UI-owner choices over immutable review; I/O belongs to the explicit host."""
    def __init__(self, provider=None):
        if provider is not None and (type(provider) is not DiagnosticsHelpProvider or
                not all(callable(value) for value in (provider.submit, provider.poll, provider.close_held, provider.begin_close))):
            raise ValueError('explicit_diagnostics_background_provider_required')
        self.provider = provider
        self.state = ReviewState()
        self.pending = None
        self.faulted = False
        self.closed = False
        self._render_marker = None
        self._sequence = 0
        self._discard_preview = False
        self._owner = get_ident()
        self.notice = ('No diagnostics provider is connected. Preview and export are unavailable.'
                       if provider is None else 'No preview requested. Choose Preview diagnostics to begin.')

    def _owner_check(self):
        if get_ident() != self._owner:
            raise RuntimeError('diagnostics_help_ui_owner_required')

    @property
    def close_held(self):
        if self.pending is not None:
            return True
        try:
            return self.provider is not None and self.provider.close_held() is not False
        except Exception:
            return True

    @property
    def can_request(self):
        return self.provider is not None and not self.closed and not self.faulted and not self.close_held

    @property
    def can_export(self):
        return self.can_request and self.state.preview is not None and self.state.selection_confirmed and bool(self.state.selected)

    def _request(self, action, destination=None):
        self._owner_check()
        if not self.can_request:
            return False
        self._sequence += 1
        request = HelpRequest(self._sequence, action, self.state, destination)
        self._discard_preview = False
        self.pending = request
        self.notice = 'Preparing a diagnostic preview…' if action is HelpAction.PREVIEW else 'Saving the reviewed selection…'
        try:
            accepted = self.provider.submit(request)
            if accepted is not True:
                if accepted is not False:
                    raise ValueError('unconfirmed_submission')
                self.pending = None
                self.notice = 'Request was not started. Your review is unchanged; try again when the provider is available.'
                return False
        except Exception:
            self.faulted = True
            self.notice = 'Background request completion is unknown. Export and close remain held for host supervision.'
            return False
        return True

    def preview(self):
        return self._request(HelpAction.PREVIEW)

    def cancel_preview(self):
        self._owner_check()
        if self.pending is None or self.pending.action is not HelpAction.PREVIEW:
            return False
        self._discard_preview = True
        self.notice = 'Preview cancelled. Its result will be discarded; waiting for completion and worker teardown.'
        return True

    def select(self, name, checked):
        self._owner_check()
        if self.pending is not None or self.state.preview is None or type(name) is not str or name not in MEMBERS or type(checked) is not bool:
            return False
        selected = set(self.state.selected)
        selected.add(name) if checked else selected.discard(name)
        self.state = replace(self.state, status=ReviewStatus.READY, issue=ReviewIssue.NONE,
            selected=tuple(member for member in MEMBERS if member in selected), selection_confirmed=True, hashes=())
        self.notice = 'Choose Save selected diagnostics to create a ZIP. Sharing it is a separate decision.' if selected else 'Select at least one category to save.'
        return True

    def export(self, destination):
        self._owner_check()
        if not self.can_export:
            return False
        if destination is None:
            self.state = replace(self.state, status=ReviewStatus.CANCELLED, issue=ReviewIssue.DESTINATION_CANCELLED, hashes=())
            self.notice = 'Save cancelled. Your reviewed preview and choices are unchanged.'
            return False
        if not isinstance(destination, (str, Path)):
            self.notice = 'Choose a valid ZIP destination. Your review is unchanged.'
            return False
        return self._request(HelpAction.EXPORT, Path(destination))

    def _poll_delivery(self):
        self._owner_check()
        if self.provider is None:
            return False
        try:
            completion = self.provider.poll()
        except Exception:
            self.faulted = True
            self.notice = 'Completion could not be checked. Pending work remains supervised; no success is assumed.'
            return False
        if completion is None:
            return False
        if (self.pending is None or type(completion) is not tuple or len(completion) != 2
            or completion[0] is not self.pending):
            return False
        request, state = completion
        if request.action is HelpAction.PREVIEW and self._discard_preview:
            self.pending = None
            self.faulted = False
            self.state = request.review
            self.notice = 'Preview discarded. Your earlier review and choices are retained; request another preview after the provider is ready.'
            return True
        valid = safe_review(state)
        if request.action is HelpAction.EXPORT:
            valid = (valid and state.status in {ReviewStatus.EXPORTED, ReviewStatus.FAILED, ReviewStatus.CANCELLED}
                and state.preview is request.review.preview and state.selected == request.review.selected and state.selection_confirmed)
        else:
            valid = (valid and state.status in {ReviewStatus.READY, ReviewStatus.UNAVAILABLE, ReviewStatus.NOT_REVIEWED}
                and state.selected == () and not state.selection_confirmed and state.hashes == ())
        if not valid:
            self.pending = None
            self.faulted = True
            self.notice = 'Diagnostic result was invalid. Nothing new is shown or certified as saved; contact the host owner.'
            return True
        self.pending = None
        self.faulted = False
        if (request.action is HelpAction.PREVIEW and state.preview is None
                and state.issue is ReviewIssue.PREVIEW_FAILED and request.review.preview is not None):
            self.state = request.review
            self.notice = "Preview unavailable; your earlier review and choices are retained. Retry Preview or save the reviewed selection."
            return True
        self.state = (replace(state, selected=(), selection_confirmed=False, hashes=())
                      if request.action is HelpAction.PREVIEW else state)
        messages = {
            ReviewStatus.READY: 'Preview ready. Review the details and select the categories you want to save.',
            ReviewStatus.UNAVAILABLE: 'Preview unavailable. No safe diagnostic preview could be supplied; try Preview diagnostics again.',
            ReviewStatus.NOT_REVIEWED: 'No preview supplied. Choose Preview diagnostics.',
            ReviewStatus.EXPORTED: 'Selected diagnostics saved and verified. Nothing was uploaded; choose whether to share the ZIP.',
            ReviewStatus.CANCELLED: 'Save cancelled. Your review and choices are retained.',
            ReviewStatus.FAILED: ('That file already exists. Choose a new filename; the existing file was not replaced.'
                if state.issue is ReviewIssue.DESTINATION_EXISTS else 'Save failed. Your review and choices are retained; choose a new destination and retry.'),
        }
        self.notice = messages[self.state.status]
        return True

    def poll(self):
        changed = self._poll_delivery()
        marker = (id(self.state), self.pending is not None, self.faulted,
                  self.closed, self.close_held, self.notice)
        transition = marker != self._render_marker
        self._render_marker = marker
        return changed or transition

    def begin_close(self):
        self._owner_check()
        self.closed = True
        self.notice = 'Diagnostics provider stopped for close. Reopen Manager after completion to request another preview.'
        if self.provider is not None:
            try:
                self.provider.begin_close()
            except Exception:
                self.faulted = True
                self.notice = 'Diagnostics shutdown is unconfirmed. Existing work remains supervised.'

    def detail(self, member):
        if self.state.preview is None or member not in MEMBERS:
            return 'No reviewed details available.'
        return dict(self.state.preview.files)[member].decode('utf-8')

    def summary(self, member):
        if self.state.preview is None or member not in MEMBERS:
            return 'No reviewed details available.'
        data = _decode(dict(self.state.preview.files)[member], limit=MAX_EVENTS * MAX_RECORD_BYTES + 1024)
        if member == 'runtime.json':
            return '\n'.join([f"OS: {data['os']} {data['os_release']}", f"Architecture: {data['architecture']}",
                f"Python: {data['python']}", 'Dependency versions:'] + [f'{name}: {version}' for name, version in data['dependencies'].items()])
        rows = data['events']
        if not rows:
            return 'No error groups are present in this supplied preview. This does not prove all startup or background failures were captured.'
        lines = [f'Error groups: {len(rows)}; total recorded occurrences: {sum(row["count"] for row in rows)}.']
        for row in rows[:8]:
            lines.extend([f"\n{row['component']} {row['build']} — {row['code']} ({row['count']} occurrences)",
                f"First: {row['first']}\nLast: {row['last']}",
                'Original failure types: ' + ', '.join(item['type'] for item in row['failure']),
                'Last recorded recovery: ' + (row['recoveries'][-1]['outcome'] if row['recoveries'] else 'none supplied')])
        if len(rows) > 8:
            lines.append('Showing the first 8 groups. Exact details include all groups in the selected category.')
        return '\n'.join(lines)


def create_diagnostics_dialog(parent, flow):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QCheckBox, QComboBox, QTextBrowser, QFileDialog
    from suite_core.manager.ui_cards import workspace_heading, workspace_layout, compact_action

    class DiagnosticsDialog(QDialog):
        def closeEvent(self, event):
            if flow.close_held:
                flow.notice = 'Diagnostic work is still supervised. Close is available after completion and worker teardown.'
                self.render(); event.ignore()
                return
            super().closeEvent(event)

        def reject(self):
            if flow.close_held:
                flow.notice = 'Diagnostics work or worker teardown is still pending. Keep this review open until the host confirms completion.'
                self.render()
                return
            super().reject()

    dialog = DiagnosticsDialog(parent)
    dialog.setWindowTitle('Review diagnostics')
    layout = workspace_layout(dialog)
    layout.addWidget(workspace_heading('Review diagnostics'))
    scope = QLabel('Preview existing safe Manager error/recovery records and runtime versions before saving a ZIP. '
        'Raw logs, prompts/chats, credentials, catalogs and media are excluded. Startup capture is not enabled by this screen.')
    scope.setWordWrap(True); scope.setTextFormat(Qt.PlainText); layout.addWidget(scope)
    status = QLabel(); status.setWordWrap(True); status.setTextFormat(Qt.PlainText); layout.addWidget(status)
    preview = compact_action(QPushButton('Preview diagnostics')); cancel = compact_action(QPushButton('Cancel preview'))
    preview_actions = QHBoxLayout(); preview_actions.addWidget(preview); preview_actions.addWidget(cancel)
    preview_actions.addStretch(); layout.addLayout(preview_actions)
    choices = QHBoxLayout(); boxes = {}
    for name, title in (('diagnostics.json', 'Errors and recovery'), ('runtime.json', 'Runtime and dependency versions')):
        box = QCheckBox(title); boxes[name] = box; choices.addWidget(box)
    layout.addLayout(choices)
    chooser = QComboBox(); chooser.setAccessibleName('Diagnostic preview details')
    chooser.addItem('Errors and recovery', 'diagnostics.json'); chooser.addItem('Runtime and dependency versions', 'runtime.json')
    layout.addWidget(chooser)
    exact = QCheckBox('Show exact diagnostic details'); layout.addWidget(exact)
    browser = QTextBrowser(); browser.setOpenExternalLinks(False); layout.addWidget(browser, 1)
    footer = QHBoxLayout(); save = compact_action(QPushButton('Save selected diagnostics…')); close = compact_action(QPushButton('Close'))
    footer.addStretch(); footer.addWidget(save); footer.addWidget(close); layout.addLayout(footer)

    def render():
        extra = ''
        if flow.pending is None and flow.close_held:
            extra += '\nThe provider still holds background work or teardown. Preview, save and close wait for confirmed completion.'
        if flow.state.preview is not None:
            p = flow.state.preview
            extra += ('\nDisk logging available.' if p.storage_available else '\nDisk logging unavailable to this review; available safe records are included.')
            extra += f' Unavailable files: {p.unavailable_files}; rejected records: {p.rejected_records}.'
        status.setText(flow.notice + extra)
        preview.setEnabled(flow.can_request); save.setEnabled(flow.can_export)
        cancel.setEnabled(flow.pending is not None and flow.pending.action is HelpAction.PREVIEW)
        cancel.setVisible(flow.pending is not None and flow.pending.action is HelpAction.PREVIEW)
        close.setEnabled(not flow.close_held)
        for name, box in boxes.items():
            box.blockSignals(True); box.setChecked(name in flow.state.selected); box.blockSignals(False)
            box.setEnabled(flow.state.preview is not None and flow.pending is None and not flow.faulted)
        browser.setPlainText(flow.detail(chooser.currentData()) if exact.isChecked() else flow.summary(chooser.currentData()))

    def request_preview():
        flow.preview(); render()

    def cancel_preview():
        flow.cancel_preview(); render()

    def choose_export():
        if not flow.can_export:
            return
        destination, _ = QFileDialog.getSaveFileName(dialog, 'Save reviewed diagnostics', 'collectigen-diagnostics.zip', 'ZIP files (*.zip)')
        flow.export(Path(destination) if destination else None); render()

    preview.clicked.connect(request_preview); cancel.clicked.connect(cancel_preview)
    save.clicked.connect(choose_export); close.clicked.connect(dialog.reject)
    def select_category(member, checked):
        flow.select(member, checked)
        if checked:
            chooser.setCurrentIndex(chooser.findData(member))
        elif chooser.currentData() not in flow.state.selected and flow.state.selected:
            chooser.setCurrentIndex(next(index for index in range(chooser.count())
                if chooser.itemData(index) in flow.state.selected))
        render()

    for name, box in boxes.items():
        box.toggled.connect(lambda checked, member=name: select_category(member, checked))
    chooser.currentIndexChanged.connect(lambda *_: render())
    exact.toggled.connect(lambda *_: render())
    dialog.render = render
    dialog.resize(850, 620); render()
    return dialog
