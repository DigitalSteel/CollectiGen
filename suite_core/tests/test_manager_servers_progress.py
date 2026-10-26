"""Source presentation doubles; no sampler or native measurement authority."""
from datetime import datetime, timezone
from types import SimpleNamespace

from PySide6.QtWidgets import QApplication

from suite_core.manager import servers_ui
from suite_core.manager.window import unconfigured


def test_refresh_feedback_matches_command_and_stays_visible_until_retirement(monkeypatch):
    app = QApplication.instance() or QApplication([])
    clock = [20.0]
    monkeypatch.setattr(servers_ui, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(servers_ui, 'project_local_resources', lambda *args: ())
    route = SimpleNamespace(state='unknown', reason='Refresh local resources to measure this host.',
        can_request=True, executor=SimpleNamespace(close_held=False), pending=False, closed=False)
    def request():
        route.pending=True; route.can_request=False; route.state='pending'; route.reason='Measuring this computer.'
    def cancel():
        route.state='canceled'; route.reason='Cancelled. Waiting for the original worker.'
    route.request=request; route.cancel=cancel
    panel=servers_ui.create_servers_panel(unconfigured(datetime.now(timezone.utc)).report,
        route=route,now=lambda: datetime.now(timezone.utc))
    panel.resize(1600,850); panel.show(); app.processEvents()
    try:
        assert 'Refresh this computer' in panel.status.text()
        assert not panel.cancel.isVisible() and not panel.activity.isVisible()
        panel.refresh.click(); app.processEvents()
        assert panel.activity.isVisible() and panel.cancel.isVisible()
        clock[0]+=3
        panel.update_values()
        assert '0m 03s elapsed' in panel.elapsed.text()
        panel.cancel.click(); app.processEvents()
        assert panel.activity.isVisible()  # Cancel is not a retirement receipt.
        assert not panel.cancel.isVisible() and not panel.cancel.isEnabled()
        assert 'original worker' in panel.status.text()
        route.pending=False; panel.update_values(); app.processEvents()
        assert not panel.activity.isVisible() and not panel.elapsed.isVisible()
        assert not panel.cancel.isVisible()
    finally:
        panel.close(); app.processEvents()
