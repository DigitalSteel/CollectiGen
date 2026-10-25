"""Disconnected offscreen commands and deterministic original executor drain."""
import json
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QWidget

from core.encyclopedia_help_content import load_encyclopedia_help
from qt_app.encyclopedia_help_commands import EncyclopediaHelpCommands
from suite_core.manager.resource_executor import ResourceExecutorOwner
from tests.test_encyclopedia_update_check import make_check
from tests.test_component_acquisition import no_executors


class SyntheticThread:
    def __init__(self, *, target, **kwargs):
        self.target, self.live = target, False

    def start(self):
        self.live = True

    def is_alive(self):
        return self.live

    def complete(self):
        self.target()  # Future acknowledgement deliberately precedes drain


@pytest.fixture
def help_scope(tmp_path):
    application = QApplication.instance() or QApplication([])
    root = tmp_path / 'packaged-docs'
    root.mkdir()
    (root / 'RELEASE_HISTORY.json').write_text(json.dumps({'releases': [
        {'software_version': '0.1.0-alpha.8', 'date': '2026-10-08', 'scope': 'PRIVATE TEST NOTES',
         'status': 'INTERNAL QUALIFICATION'},
    ]}), encoding='utf-8')
    content = load_encyclopedia_help(root, public_notes='Reviewed canonical product changes.')
    parent = QWidget()
    notes, about, notices = [], [], []
    check, feed, _ = make_check(tmp_path)
    executor = ResourceExecutorOwner(thread_factory=SyntheticThread)
    host = EncyclopediaHelpCommands(parent, content, present_notes=notes.append,
        present_about=about.append, present_update=lambda text, notice: notices.append((text, notice)),
        update_check=check, executor=executor)
    yield host, executor, feed, notes, about, notices
    host.begin_close()
    if executor._thread is not None:
        if not executor._future.done():
            executor._thread.complete()
        executor._thread.live = False
    host.poll()
    executor.begin_close()
    host.timer.stop()
    parent.deleteLater()
    application.processEvents()


def test_offline_commands_no_startup_feed_or_internal_history(help_scope):
    host, _, feed, notes, about, _ = help_scope
    assert not host.timer.isActive() and feed.calls == []
    assert host.commands[0].invoke() and host.commands[2].invoke()
    assert notes == [host.content] and 'Tag Encyclopedia' in about[0]
    assert all(entry.notes == '' for entry in notes[0].history)
    assert 'PRIVATE' not in repr(notes[0]) and 'QUALIFICATION' not in repr(notes[0])
    assert feed.calls == []


def test_explicit_check_waits_for_physical_drain(help_scope):
    host, executor, feed, _, _, notices = help_scope
    assert host.commands[1].invoke()
    notices.clear()  # initial checking status is separate from completion
    assert host.close_held and not host.commands[1].available()
    executor._thread.complete()
    assert executor._future.done()
    host.poll()
    assert host.close_held and notices == []
    executor._thread.live = False
    host.poll()
    assert not host.close_held and not host.timer.isActive()
    assert len(notices) == 1 and notices[0][1].status == 'available'
    assert not any(url.endswith('.zip') for url, *_ in feed.calls)


@pytest.mark.parametrize('close', [False, True])
def test_cancel_or_close_suppresses_late_notice_and_retains_drain(help_scope, close):
    host, executor, _, _, _, notices = help_scope
    host.check_updates()
    executor._thread.complete()
    host.begin_close() if close else host.request_cancel()
    notices.clear()  # cancellation status is separate from a late completion
    host.poll()
    assert host.close_held and notices == []
    executor._thread.live = False
    host.poll()
    assert not host.close_held and notices == []
    assert not host.timer.isActive()


def test_missing_checker_keeps_offline_help_available(help_scope):
    host, _, feed, notes, about, notices = help_scope
    host.checker = None
    assert host.check_updates() is False
    assert 'unavailable' in notices[0][0] and notices[0][1] is None
    host.whats_new(); host.about()
    assert notes and about and feed.calls == []


def test_shared_bar_exposes_one_help_home_without_dispatch(help_scope):
    host, _, feed, _, _, _ = help_scope
    searches = []
    bar = host.create_bar(host.parent(), on_search=lambda: searches.append(True))
    assert [c.label for c in bar._commands] == ["What's New", 'Check for Updates', 'About / Version']
    assert all(c.group == 'Help' for c in bar._commands)
    assert feed.calls == []
    bar.searchRequested.emit()
    assert searches == [True] and feed.calls == []
    bar._state_timer.stop()
    bar.deleteLater()


def test_real_packaged_history_reuses_version_without_internal_fields():
    root = Path(__file__).resolve().parents[1] / 'suite_core/encyclopedia'
    content = load_encyclopedia_help(root)
    assert content.history and content.notes_reference.name == 'WHATS_NEW.md'
    assert content.public_notes == ''  # public notes projection is an integration gate
    assert 'source candidate' not in repr(content.history)


def test_unavailable_network_reports_failure_without_blocking_offline_commands(help_scope):
    host, executor, feed, notes, about, notices = help_scope
    feed.failure = lambda _: True
    host.check_updates()
    executor._thread.complete(); executor._thread.live = False
    host.poll()
    assert notices[-1][1].status == 'unavailable'
    host.whats_new(); host.about()
    assert notes and about and not host.close_held


def test_unknown_ownership_hold_leaves_offline_commands_available(help_scope):
    from dataclasses import replace
    host, executor, feed, notes, about, notices = help_scope
    original = host.checker.observe()
    host.checker.observe = lambda: replace(original, owner='unknown')
    host.check_updates()
    executor._thread.complete(); executor._thread.live = False
    host.poll()
    assert notices[-1][1].status == 'held' and feed.calls == []
    host.whats_new(); host.about()
    assert notes and about
