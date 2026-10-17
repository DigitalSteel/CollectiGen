"""Inactive candidate glue; no executor/timer creation or resource probing."""
from dataclasses import replace


class ResourceDashboardBinding:
    """Reuse the Manager's completion timer and close context, not another poller.

    Called on the same UI owner as the route. Supplied callbacks update a panel;
    no sampler is invoked here. The host owns executor supervision/shutdown.
    """
    def __init__(self, route, *, dashboard_visible, render):
        self.route = route
        self.dashboard_visible = dashboard_visible
        self.render = render
        route.bind(self)

    def navigation_changed(self, *_):
        self.route.set_visible(bool(self.dashboard_visible()))
        self.render()

    def tick(self):
        self.route.poll()
        if self.dashboard_visible():
            self.render()

    def close_context(self, existing):
        return replace(existing, worker_pending=existing.worker_pending or self.route.close_held)

    def begin_close(self):
        self.route.begin_close()
        self.render()
