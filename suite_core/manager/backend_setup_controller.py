"""Explicit setup actions; injected adapter/host retain persistence and worker authority."""
from dataclasses import dataclass


MESSAGES = {
    "invalid_ollama_endpoint": "Enter an HTTP loopback address with an explicit port. Credentials, paths and remote hosts are unsupported.",
    "settings_changed_reload_setup": "Settings changed elsewhere. Reload to review saved values; your draft is retained.",
    "settings_recovery_required": "Save needs recovery review. Your draft is retained. Review settings recovery before Reload; restoration is not confirmed.",
    "settings_publication_failed": "Save failed. Your draft is retained. Reload and review before trying again; restoration is not confirmed.",
    "settings_size_limit": "Settings exceed the supported size. Your draft is retained. Review settings before Reload.",
}


@dataclass(frozen=True)
class Draft:
    endpoint: str = ""
    allowed: bool = False


class SetupController:
    def __init__(self, setup, host):
        self.setup, self.host = setup, host
        self.draft = self.saved = Draft()
        self.opened = self.closed = self.hold = self.activation_pending = False
        self.state, self.message = "blank", "No connection saved. Choose Connect existing Ollama."
        self._refresh_bridge = self._refresh_previous_report = None

    @property
    def dirty(self):
        return self.draft != self.saved

    @property
    def can_save(self):
        return self.opened and not self.closed and not self.hold and not self.activation_pending and self.dirty and bool(self.draft.endpoint)

    @property
    def can_refresh(self):
        return self.opened and not self.closed and not self.hold and not self.dirty and not self.activation_pending and self.saved.allowed and self.host.can_refresh

    def _read_selection(self):
        # Frozen r2 exposes status but not the selection. This single, pinned read
        # consumes its already-observed snapshot: never call settings.observe here.
        value = self.setup._observed.selection
        return Draft() if value is None else Draft(value.endpoint, value.endpoint_network_allowed)

    def _error(self, error):
        code = getattr(error, "code", "")
        if code == "explicit_ollama_port_required": code = "invalid_ollama_endpoint"
        self.state = "invalid" if code == "invalid_ollama_endpoint" else "recovery" if code == "settings_recovery_required" else "held"
        self.hold = self.state != "invalid"
        self.message = MESSAGES.get(code, "Settings unavailable or unsupported. Your draft is retained. Review settings and Reload.")
        return False

    def open(self):
        if self.closed: return False
        if self.opened: return True  # reopening a dialog must not discard a draft
        if not self.reload(): return False
        self.use_saved()
        return True

    def reload(self):
        if self.closed: return False
        try:
            self.setup.open_setup()
            self.saved = self._read_selection()
        except Exception as error:
            return self._error(error)
        self.opened, self.hold = True, False
        self.state, self.message = "reloaded", "Saved values reloaded. Draft retained; choose Use saved values or Save connection."
        self.activation_pending = True
        self.activate()
        return True

    def use_saved(self):
        if not self.opened or self.closed or self.hold: return False
        self.draft = self.saved
        if not self.activation_pending:
            self._saved_message()
        return True

    def edit(self, endpoint, allowed):
        if self.closed: return
        self.draft = Draft(endpoint, allowed)
        if not self.hold and not self.activation_pending and not self.dirty:
            self._saved_message()
        elif not self.hold:
            self.state, self.message = "draft", "Draft only. Save connection to use these choices; no metadata is requested by Save."

    def _saved_message(self):
        self.state = "saved" if self.saved.endpoint else "blank"
        self.message = ("Connection saved. Choose Refresh metadata to check availability." if self.saved.allowed else
            "Connection saved. Metadata access is off.") if self.saved.endpoint else "No connection saved. Enter a local Ollama endpoint and Save connection."

    def save(self):
        if not self.can_save: return False
        try:
            self.setup.save_connection(self.draft.endpoint, endpoint_network_allowed=self.draft.allowed)
            self.saved = self._read_selection()
        except Exception as error:
            return self._error(error)
        self.draft = self.saved  # canonical normalized endpoint after verified Save
        self.activation_pending = True
        self.activate()
        return True  # persistence success is separate from bridge readiness

    def activate(self):
        if self.closed or not self.activation_pending: return False
        try:
            if not self.host.replace_ready():
                self.state, self.message = "waiting", getattr(self.host, "replacement_hold_reason", "Saved selection retained. Waiting for the previous metadata worker to finish; Refresh is unavailable.")
                return False
            self.host.install(self.setup.selected_binding())
        except Exception:
            self.state, self.message = "binding_held", "Saved selection retained. Connection activation failed; choose Retry connection."
            return False
        self.activation_pending = False
        if self.dirty:
            self.state, self.message = "reloaded", "Saved values reloaded. Draft retained; choose Use saved values or Save connection."
        else:
            self._saved_message()
        return True

    def refresh(self):
        if not self.can_refresh: return False
        # This bridge serializes metadata requests. Snapshot before submission,
        # including fast completions, to distinguish new admission from retained
        # dated inventory when the current receipt is delayed or refused.
        bridge = self.host.get_bridge()
        previous = None if bridge is None else bridge.ollama_report
        try:
            accepted = self.host.refresh()
        except Exception:
            accepted = False
        self._refresh_bridge = bridge if accepted else None
        self._refresh_previous_report = previous if accepted else None
        self.state, self.message = ("refreshing", "Refreshing saved connection metadata; Cancel refresh discards this attempt.") if accepted else ("refresh_held", "Refresh unavailable. Review connection status and retry explicitly.")
        return bool(accepted)

    def cancel_refresh(self):
        if self.closed: return False
        try:
            self.host.cancel_refresh()
        except Exception:
            self.state, self.message = "refresh_held", "Cancellation could not be confirmed; worker ownership is retained."
            return False
        self.state, self.message = "cancelled", "Refresh cancellation requested. Waiting for its worker acknowledgment; saved choices are retained."
        return True

    def observe_refresh(self):
        if self.closed or self.state not in ("refreshing", "cancelled"): return
        bridge = self.host.get_bridge()
        if bridge is None or bridge.ollama_pending or bridge.worker_pending: return
        if self.state == "cancelled":
            self.message = "Refresh cancelled. Saved choices are retained; choose Refresh metadata to try again."
        elif bridge.ollama_error is not None:
            self.state, self.message = "refresh_failed", "Metadata refresh failed or became stale. Review Ollama status in Applications and retry explicitly."
        elif (bridge is self._refresh_bridge and bridge.ollama_report is not None
                and bridge.ollama_report is not self._refresh_previous_report):
            self.state, self.message = "metadata_ready", "Metadata check completed. Review dated results and any unavailable routes in Applications."
        else:
            self.state, self.message = "refresh_failed", "No current metadata result was admitted. Any older dated inventory is retained. Choose Refresh metadata to retry."

    def cancel(self):
        if self.closed: return
        self.setup.cancel_setup()
        self.draft = self.saved
        # Cancel draft neither cancels a saved selection nor grants recovery.
        if not self.hold and not self.activation_pending: self._saved_message()

    def begin_close(self):
        # Existing Manager close policy owns the final decision and teardown.
        self.closed = True

    def finish_close(self):
        self.cancel_before_close()

    def cancel_before_close(self):
        self.setup.cancel_setup()
        self.draft = self.saved


class MetadataBridgeHost:
    """Reuse the accepted bridge; no second thread, transport or admission owner."""
    def __init__(self, get_bridge, install_bridge, bridge_factory, now, close_pending):
        self.get_bridge, self.install_bridge = get_bridge, install_bridge
        self.bridge_factory, self.now, self.close_pending = bridge_factory, now, close_pending

    @property
    def can_refresh(self):
        bridge = self.get_bridge()
        return not self.close_pending() and bridge is not None and not getattr(bridge, "module_enabled", False) and bridge.can_refresh_ollama

    @property
    def replacement_hold_reason(self):
        if getattr(self.get_bridge(), "module_enabled", False):
            return "Saved selection retained. Close the current module session and reopen Manager before activating Ollama metadata. Your draft is retained."
        return "Saved selection retained. Waiting for the previous metadata worker to finish; Refresh is unavailable."

    def replace_ready(self):
        if self.close_pending(): return False
        bridge = self.get_bridge()
        if bridge is None: return True
        # A module launch keeps its exact bridge for the entire launch, including idle.
        if getattr(bridge, "module_enabled", False): return False
        # Never retire a bridge which also owns a running application/session.
        if bridge.occupied or bridge.cleanup_held or bridge.session_key is not None: return False
        bridge.cancel_ollama_refresh()
        bridge.request_shutdown()
        bridge.drain(self.now())
        return bridge.quiescent and bridge.finish_ui_closed()

    def install(self, binding):
        if self.close_pending(): raise RuntimeError("close_pending")
        current = self.get_bridge()
        if getattr(current, "module_enabled", False): raise RuntimeError("module_owner_replacement_refused")
        if current is not None and not (current.quiescent and current.closed):
            raise RuntimeError("previous_bridge_teardown_unacknowledged")
        bridge = self.bridge_factory(binding)
        # Callback installs lifetime ownership before constructing Qt views.
        self.install_bridge(bridge)

    def refresh(self):
        return self.can_refresh and self.get_bridge().refresh_ollama()

    def cancel_refresh(self):
        bridge = self.get_bridge()
        if bridge is not None: bridge.cancel_ollama_refresh()
