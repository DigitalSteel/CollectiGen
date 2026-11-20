from types import SimpleNamespace

import pytest

from core.manager_launch_owner import LaunchHold
from core.manager_stock_hook_provider import StockExitPending, wait_for_stock_exit
from suite_core.manager.launcher_main import PublicLaunch


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_pending_updater_can_finish_on_the_same_retained_process():
    results = iter((None, 0))
    member = SimpleNamespace(process=SimpleNamespace(poll=lambda: next(results)))
    with pytest.raises(StockExitPending, match="updater"):
        wait_for_stock_exit(member, {}, timeout=0)
    wait_for_stock_exit(member, {}, timeout=0)


def test_nonzero_callback_exit_is_an_error_even_when_updater_is_still_running():
    member = SimpleNamespace(process=SimpleNamespace(poll=lambda: None))
    callback = SimpleNamespace(process=SimpleNamespace(poll=lambda: 2))
    with pytest.raises(LaunchHold, match="updated.*exit 2"):
        wait_for_stock_exit(member, {"--veloapp-updated": callback}, timeout=0)


def test_launcher_observes_the_same_stock_continuation(qapp, tmp_path):
    calls = []

    def finish():
        calls.append("finish")
        launch.update_handoff_status = None

    def install(owner):
        calls.append("install")
        owner.pending_stock_continuation = finish
        raise StockExitPending("Still waiting for updater")

    launch = PublicLaunch(tmp_path, update_after_close=install)
    launch._run_update_handoff()
    assert calls == ["install", "finish"]
    assert launch.pending_stock_continuation is None
    assert launch.update_worker.closed
    assert len(list((tmp_path / "Data/Manager/PublicLaunchReports").glob("*exit-pending.json"))) == 1


def test_launcher_delivers_install_error(qapp, tmp_path):
    def install(_owner):
        raise LaunchHold("failure during apply")

    launch = PublicLaunch(tmp_path, update_after_close=install)
    with pytest.raises(LaunchHold, match="failure during apply"):
        launch._run_update_handoff()
    assert launch.update_worker.closed
