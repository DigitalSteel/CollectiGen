"""Explicit native settings receiver; no discovery, launch or automatic activation."""
from copy import deepcopy
from .managed_settings_protection import (capture_baseline, _save_metadata,
    preserve_managed_save, accept_saved_managed)
from .suite_settings_policy import (MANAGED_KEY, GROUPS, SuiteRevision, SettingsObservation,
    SharedSettingsHold, require, encode, fingerprint)
from .suite_settings_adoption import (observe, metadata, validate_patch, effective_group,
    group_fingerprint, _check_path)


class SettingsReceiver:
    """Caller explicitly binds an initialized recipient capability.

    Activation acknowledgement is supplied by an owning app after reload/restart;
    persistence alone never certifies current runtime state.
    """
    def __init__(self, capability):
        from .suite_settings_policy import ReceiverCapability
        require(type(capability) is ReceiverCapability, "receiver_capability_required")
        self.capability = capability

    def observe(self):
        return observe(self.capability)

    def _patch(self, expected, mutate):
        from .settings import AppSettings, _settings_writer
        require(type(expected) is SettingsObservation and expected.capability == self.capability,
                "expected_receiver_observation_required")
        path = _check_path(self.capability)
        with _settings_writer(path):
            current = self.observe()
            require(current.digest == expected.digest, "recipient_revision_conflict")
            payload = current.payload
            proposed = mutate(payload)
            AppSettings._from_payload(payload)._save_locked(path, _patch_payload=proposed,
                                                           _suite_managed_intent=True)
            return self.observe()

    def set_override(self, group, values, *, expected):
        """Explicit equal-value edits still become overrides, in the value transaction."""
        require(group in self.capability.groups, "receiver_group_unsupported")
        def mutate(payload):
            meta = metadata(payload)
            require(group in meta["groups"], "managed_group_not_enrolled")
            proposed = validate_patch(payload, {group: values})
            origin = deepcopy(meta["groups"][group])
            origin.update(mode="override", activation_pending=True, value_fingerprint=group_fingerprint(proposed, group))
            origin.pop("intent_id", None)
            meta["groups"][group] = origin
            proposed[MANAGED_KEY] = meta
            return proposed
        return self._patch(expected, mutate)

    def reset_to_suite(self, group, suite, *, expected):
        require(type(suite) is SuiteRevision and group in self.capability.groups and group in suite.values,
                "selected_suite_group_required")
        def mutate(payload):
            meta = metadata(payload)
            require(group in meta["groups"] and not meta.get("detached")
                    and meta.get("suite_id") == suite.suite_id, "suite_association_required")
            require(suite.revision >= meta.get("revision", 0), "suite_revision_conflict")
            require(suite.revision != meta.get("revision") or
                    suite.full_fingerprint == meta.get("suite_fingerprint"), "suite_revision_conflict")
            proposed = validate_patch(payload, {group: suite.values[group]})
            meta["groups"][group] = {**deepcopy(meta["groups"][group]),
                                      "mode": "inherited", "value_fingerprint": group_fingerprint(proposed, group),
                                      "activation_pending": True, "suite_revision": suite.revision, "suite_fingerprint": suite.full_fingerprint}
            meta["groups"][group].pop("intent_id", None)
            meta.update(revision=suite.revision, suite_fingerprint=suite.full_fingerprint)
            proposed[MANAGED_KEY] = meta
            return proposed
        return self._patch(expected, mutate)

    def disconnect(self, *, expected):
        def mutate(payload):
            meta = metadata(payload)
            require(meta.get("binding") == self.capability.binding, "managed_receiver_not_enrolled")
            meta["detached"] = True
            for group, origin in meta["groups"].items():
                origin.update(mode="override", value_fingerprint=group_fingerprint(payload, group))
                origin.pop("intent_id", None)
            payload[MANAGED_KEY] = meta
            return payload
        return self._patch(expected, mutate)

    def acknowledge_activation(self, intent_id, *, expected):
        def mutate(payload):
            meta = metadata(payload)
            receipt = meta["receipts"].get(intent_id)
            require(receipt is not None and receipt.get("binding") == self.capability.binding,
                    "activation_receipt_required")
            groups = [g for g, o in meta["groups"].items() if o.get("intent_id") == intent_id]
            require(set(groups) == set(receipt["groups"]) and groups and all(meta["groups"][g]["value_fingerprint"] == group_fingerprint(payload, g)
                                   for g in groups), "activation_revision_changed")
            receipt["activated"] = True
            for group in groups:
                meta["groups"][group]["activation_pending"] = False
            payload[MANAGED_KEY] = meta
            return payload
        return self._patch(expected, mutate)
