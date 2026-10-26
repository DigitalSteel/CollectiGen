"""Manager projection of explicitly bound core settings transactions.

The entry owner supplies canonical suite/approved-plan persistence. This module
does not discover profiles, write settings, acknowledge activation or launch apps.
"""
from copy import deepcopy
from collections import Counter

from core.suite_settings_policy import SuiteRevision, BatchPlan, MANAGED_KEY
from core.suite_settings_adoption import observe, effective_group, preview_batch, apply_batch


GROUP_LABELS = {
    "appearance.theme": "Theme",
    "appearance.accessibility": "Accessibility",
    "library.locations": "Library locations",
}
STATUS_LABELS = {
    "ready": "Ready to apply", "pending": "Pending — recipient unavailable",
    "conflict": "Conflict — refresh and review", "unsupported": "Unsupported",
    "override_retained": "Independent values retained", "failed": "Recovery required",
    "cancelled": "Cancelled", "applied": "Applied — activation acknowledged",
    "applied_activation_pending": "Saved — activation pending",
    "unknown": "Outcome unknown — recovery pending",
    "recovery_pending": "Outcome unknown — recovery pending",
    "unknown_recovery_pending": "Outcome unknown — recovery pending",
}


class SharedSettingsController:
    def __init__(self, *, read_suite, capabilities, receivers, save_approved_plan=None,
                 publish_suite=None, save_outcomes=None, contract_ready=False):
        self.read_suite = read_suite
        self.capabilities = tuple(capabilities)
        self.receivers = tuple(receivers)
        self.save_approved_plan = save_approved_plan
        self.publish_suite = publish_suite
        self.contract_ready = contract_ready
        self.save_outcomes = save_outcomes
        self.selected = set()  # Enrollment is supplied authority; selection is deliberate.
        self.observations = {}
        self.observation_holds = {}
        self.plan = None
        self.outcomes = ()
        self.busy = False
        self.closed = False
        self.future = None
        self.cancel_requested = False
        self.delivery_unknown = False
        self.message = "Select enrolled recipients, then preview the update."
        self.base = read_suite()
        self.values = self.base.values
        self.refresh()

    def invalidate(self):
        self.plan = None
        self.message = "Selection or draft changed. Preview again before Apply."

    def select(self, binding, enabled):
        if self.busy or self.closed:
            return
        if binding not in {c.binding for c in self.capabilities}:
            raise ValueError("Unknown enrolled recipient")
        (self.selected.add if enabled else self.selected.discard)(binding)
        self.invalidate()

    def edit_group(self, group, values):
        if self.busy or self.closed:
            return
        draft = deepcopy(self.values)
        draft[group] = deepcopy(values)
        SuiteRevision.create(self.base.suite_id, self.base.revision, draft)
        self.values = draft
        self.invalidate()

    def refresh(self):
        if self.busy or self.closed:
            return
        current = self.read_suite()
        if current.full_fingerprint != self.base.full_fingerprint:
            self.plan = None
            self.message = "Shared revision changed. Draft retained; use current shared values before reviewing."
        for cap in self.capabilities:
            try:
                self.observations[cap.binding] = observe(cap)
                self.observation_holds.pop(cap.binding, None)
            except (OSError, ValueError):
                self.observation_holds[cap.binding] = "Unavailable or held; cached observation only"
        # Preserve the exact reviewed plan for idempotent retry/catch-up. A new
        # preview always rebinds current observations and needs another Apply.

    def use_current(self):
        if not self.busy and not self.closed:
            self.base = self.read_suite()
            self.values = self.base.values
            self.invalidate()

    def discard_draft(self):
        """Discard only the in-memory draft; storage availability is irrelevant."""
        if self.close_held:
            return False
        self.values = self.base.values
        self.plan = None
        self.message = "Draft discarded. Last observed shared values restored; Refresh checks saved values."
        return True

    def restore_approved_plan(self, value):
        """Host supplies only its original durably approved intent, never an import."""
        if self.close_held or self.closed:
            return False
        plan = BatchPlan.from_json(value)
        bindings = {c.binding for c in self.capabilities}
        if (plan.suite != self.read_suite() or
                any(p.capability.binding not in bindings for p in plan.participants)):
            self.message = "Pending intent superseded or recipient changed. Fresh preview required."
            self.plan = None
            return False
        self.base = plan.suite
        self.values = plan.suite.values
        self.selected = {p.capability.binding for p in plan.participants}
        self.plan = plan
        self.message = "Original approved batch restored. Refresh to observe recipients; Apply retries this exact intent."
        return True

    def preview(self, replace_groups=()):
        if self.busy or self.closed:
            return None
        current = self.read_suite()
        if current.full_fingerprint != self.base.full_fingerprint:
            self.plan = None
            self.message = "Shared revision changed. Use current shared values, then preview again."
            return None
        suite = self.base if self.values == self.base.values else SuiteRevision.create(
            self.base.suite_id, self.base.revision + 1, self.values)
        caps = tuple(c for c in self.capabilities if c.binding in self.selected)
        self.plan = preview_batch(suite, caps, adopt_groups=tuple(replace_groups),
                                  observations=tuple(self.observations.values()))
        self.outcomes = ()
        self.delivery_unknown = False
        self.message = (f"Review {len(self.plan.participants)} settings files. "
                        "Apply preserves independent values unless replacement is selected. "
                        "Location changes repoint configuration only; no files move.")
        return self.plan

    @property
    def can_apply(self):
        return (not self.busy and not self.closed and self.plan is not None and bool(self.plan.participants)
                and self.contract_ready
                and self.save_approved_plan is not None
                and self.save_outcomes is not None
                and (self.plan.suite == self.base or self.publish_suite is not None))

    def apply(self):
        if not self.can_apply:
            self.message = "Apply held: core review and canonical Manager persistence must be connected."
            return ()
        self.busy = True
        self.cancel_requested = False
        return self._apply()

    def start_apply(self, submit):
        """Use Manager's retained executor; entry must drain close_held."""
        if not self.can_apply:
            return False
        self.busy = True
        self.cancel_requested = False
        try:
            self.future = submit(self._apply)
        except Exception:
            self.busy = False
            self.message = "Worker unavailable. Review retained; no recipients written."
            return False
        return True

    @property
    def close_held(self):
        return self.busy or self.future is not None

    def poll(self):
        if self.future is None or not self.future.done():
            return False
        try:
            self.future.result()
        except Exception:
            self.message = "Outcome delivery failed. Refresh original receipts before retrying."
            self.delivery_unknown = True
        self.future = None
        self.busy = False
        self.refresh()
        return True

    def _apply(self):
        try:
            self.delivery_unknown = False
            current = self.read_suite()
            if current.full_fingerprint != self.plan.suite.full_fingerprint:
                if current.full_fingerprint != self.base.full_fingerprint:
                    self.message = "Shared revision changed. Refresh and review; no recipients written."
                    self.plan = None
                    return ()
                self.publish_suite(self.base, self.plan.suite)
            if self.read_suite().full_fingerprint != self.plan.suite.full_fingerprint:
                self.message = "Shared revision was not saved. No recipients written."
                return ()
            # Approval is durable before any recipient transaction starts.
            self.save_approved_plan(self.plan.to_json())
            self.base = self.plan.suite
            self.outcomes = apply_batch(self.plan, read_suite=self.read_suite,
                                        receivers=self.receivers,
                                        cancelled=lambda: self.cancel_requested)
            counts = Counter(o.status for o in self.outcomes)
            self.message = "; ".join(f"{STATUS_LABELS.get(s, s)}: {n}" for s, n in counts.items())
            if self.save_outcomes is not None:
                try:
                    self.save_outcomes(self.outcomes)
                except Exception:
                    self.delivery_unknown = True
                    self.message = "Recipient receipts retained; Manager status persistence unknown — recovery pending. Refresh and retry the exact approved intent."
            return self.outcomes
        except (OSError, ValueError):
            self.message = "Manager persistence held. No completion claim; retain review and original recovery."
            return ()
        finally:
            if self.future is None:
                self.busy = False
                self.refresh()

    def cancel(self):
        self.cancel_requested = True

    def rows(self):
        """Detached presentation rows; no private snapshots in durable status."""
        outcomes = {o.binding: o for o in self.outcomes}
        intents = {p.capability.owner_key: p for p in self.plan.participants} if self.plan else {}
        rows = []
        for cap in self.capabilities:
            obs = self.observations.get(cap.binding)
            intent = intents.get(cap.owner_key)
            outcome = outcomes.get(intent.capability.binding) if intent else None
            for group in self.values:
                supported = group in cap.groups
                payload = obs.payload if obs else None
                origin = payload.get(MANAGED_KEY, {}).get("groups", {}).get(group, {}) if payload else {}
                mode = origin.get("mode", "independent")
                if payload and payload.get(MANAGED_KEY, {}).get("detached"):
                    mode = "disconnected / independent"
                if cap.binding in self.observation_holds:
                    mode += " (last observed)" if obs else " (unavailable)"
                state = outcome.status if outcome else intent.state if intent else "observed"
                if self.delivery_unknown and intent:
                    state = "unknown"
                if not supported:
                    state = "unsupported"
                elif cap.binding in self.observation_holds and not outcome:
                    state = "pending"
                elif outcome and intent and group not in intent.changes:
                    state = "override_retained"
                rows.append({"binding": cap.binding, "recipient": cap.app_id,
                    "profile": cap.profile_id, "group": group, "supported": supported,
                    "effective": effective_group(payload, group) if payload and supported else None,
                    "desired": deepcopy(self.values[group]), "origin": mode,
                    "status": STATUS_LABELS.get(state, "Observed; preview required" if state == "observed" else state.replace("_", " ")),
                    "reason": outcome.reason if outcome else "",
                    "recovery": outcome.receipt.get("recovery", {}) if outcome else {},
                    "changes": group in intent.changes if intent else False})
        return rows
