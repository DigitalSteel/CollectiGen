"""Disconnected Help commands; host retains the canonical executor/window.

No database admission, launcher, installation action, startup check or polling
feed. Presenters belong to the host's existing dialog/status surface. Wire
begin_close/close_held into the original window lifetime before connecting.
"""
from threading import Event

from PySide6.QtCore import QObject, QTimer

from core.encyclopedia_help_content import EncyclopediaHelpContent
from core.encyclopedia_update_check import EncyclopediaUpdateCheck, EncyclopediaUpdateNotice
from core.encyclopedia_entry_adapter import EncyclopediaEntryUpdateCheck, EncyclopediaEntryNotice
from qt_app.command_bar import CommandBar, UiCommand
from suite_core.manager.resource_executor import ResourceExecutorOwner


def notice_text(notice):
    if notice.status == 'available':
        ownership = ('Use Suite Manager to review this installation update.'
            if notice.route == 'manager' else 'Review the manual package update separately.')
        version = notice.version if type(notice) is EncyclopediaEntryNotice else notice.offer.release.software_version
        return f'Tag Encyclopedia {version} is available. {ownership} No package has been downloaded.'
    return {
        'not_found': 'No newer release found on this channel.',
        'unavailable': 'Update checking is unavailable. Continue browsing locally and retry when ready.',
        'held': 'Update checking is held until release and installation ownership can be verified. Local browsing remains available.',
    }[notice.status]


class EncyclopediaHelpCommands(QObject):
    def __init__(self, parent, content, *, present_notes, present_about,
                 present_update, update_check=None, executor=None):
        super().__init__(parent)
        if type(content) is not EncyclopediaHelpContent:
            raise ValueError('exact_offline_help_content_required')
        if update_check is not None and (type(update_check) not in (EncyclopediaUpdateCheck, EncyclopediaEntryUpdateCheck)
                or type(executor) is not ResourceExecutorOwner):
            raise ValueError('existing_check_and_retained_executor_required')
        self.content, self.checker, self.executor = content, update_check, executor
        self.present_notes, self.present_about, self.present_update = present_notes, present_about, present_update
        self.future = None
        self.cancel = Event()
        self.closing = False
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.poll)
        self.commands = (
            UiCommand('encyclopedia:whats-new', "What's New", 'Help', callback=self.whats_new,
                enabled=lambda: not self.closing, description='Read the packaged release history offline.'),
            UiCommand('encyclopedia:check-updates', 'Check for Updates', 'Help', callback=self.check_updates,
                enabled=lambda: not self.closing and not self.close_held,
                description='Check signed releases explicitly; no package download or installation.'),
            UiCommand('encyclopedia:about', 'About / Version', 'Help', callback=self.about,
                enabled=lambda: not self.closing),
        )

    def create_bar(self, parent, *, on_search, existing_commands=(), groups=None):
        """Optional shared bar; host inserts it once, preserving left navigation."""
        bar = CommandBar(parent)
        bar.set_commands('Tag Encyclopedia', [*existing_commands, *self.commands], groups=groups)
        bar.searchRequested.connect(on_search)
        return bar

    def whats_new(self):
        if not self.closing:
            self.present_notes(self.content)

    def about(self):
        if not self.closing:
            self.present_about(self.content.about)

    @property
    def close_held(self):
        return (self.future is not None or (self.executor is not None and self.executor.close_held)
            or type(self.checker) is EncyclopediaEntryUpdateCheck and self.checker.close_held)

    def check_updates(self):
        if self.closing or self.close_held:
            return False
        if self.checker is None:
            self.present_update('Update checking is unavailable in this package. Local browsing remains available.', None)
            return False
        self.cancel.clear()
        self.present_update('Checking signed releases. Local browsing remains available.', None)
        self.future = self.executor.submit(lambda: self.checker.check(should_cancel=self.cancel.is_set))
        self.timer.start()  # polls owned work only, never starts another check
        return True

    def request_cancel(self):
        self.cancel.set()
        if self.future is not None and not self.closing:
            self.present_update('Cancellation requested. Waiting for the existing update-check worker to finish; local browsing remains available.', None)

    def begin_close(self):
        self.closing = True
        self.request_cancel()  # executor shutdown remains the original host's job

    def poll(self):
        if self.future is None:
            if (type(self.checker) is EncyclopediaEntryUpdateCheck
                    and self.checker.can_resume('check') and not self.executor.closed
                    and not self.executor.close_held):
                # Consume only the original late reply; never send another check.
                self.future = self.executor.submit(lambda: self.checker.check(should_cancel=self.cancel.is_set))
                self.timer.start()
                return
            self.timer.stop()
            return
        if not self.future.done() or self.executor.close_held:
            return
        future, self.future = self.future, None
        self.timer.stop()
        if self.closing or self.cancel.is_set():
            return
        try:
            notice = future.result()
            if type(notice) not in (EncyclopediaUpdateNotice, EncyclopediaEntryNotice):
                raise ValueError('exact_update_notice_required')
            text = notice_text(notice)
        except Exception:
            notice = None
            text = 'Update checking could not finish. Local browsing remains available; retry when ready.'
        self.present_update(text, notice)
