"""Bounded native profile preview and independent suite-settings transactions.

No discovery, profile initialization, application launch, transfer or network I/O.
A supplied current-suite reader is required before each recipient transaction.
"""
from copy import deepcopy
from .managed_settings_protection import metadata, effective_group, group_fingerprint
from pathlib import Path
import hashlib
from .suite_settings_policy import (SCHEMA, MANAGED_KEY, GROUPS, SuiteRevision,
    ReceiverCapability, SettingsObservation, ParticipantIntent, BatchPlan,
    ParticipantOutcome, SharedSettingsHold, require, encode, decode, fingerprint)


def bind_capability(app_id, profile_id, host_id, settings_path, groups):
    """Explicit caller admission to an initialized canonical data/user profile."""
    from .training_source_io import _assert_unredirected
    path = Path(settings_path)
    require(path.is_absolute() and ".." not in path.parts and path.name == "settings.json"
            and path.parent.name == "user" and path.parent.parent.name == "data", "canonical_profile_required")
    _assert_unredirected(path)
    root = path.parent.parent.parent
    require(root.is_dir() and path.is_file(), "initialized_profile_required")
    stat = root.stat()
    cap = ReceiverCapability(app_id, profile_id, host_id, str(path), (stat.st_dev, stat.st_ino),
                             tuple(groups))
    observe(cap)
    return cap


def _check_path(cap):
    from .training_source_io import _assert_unredirected
    path = Path(cap.settings_path)
    require(path.is_absolute() and ".." not in path.parts and path.name == "settings.json"
            and path.parent.name == "user" and path.parent.parent.name == "data", "canonical_profile_required")
    _assert_unredirected(path)
    stat = path.parent.parent.parent.stat()
    require((stat.st_dev, stat.st_ino) == cap.profile_token, "profile_identity_changed")
    return path


def raw_payload(path):
    """Read bounded JSON only; never invoke recovery-capable AppSettings.load."""
    from .history_configuration import MAX_SETTINGS_BYTES
    from .suite_settings_policy import _pairs
    import json
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_SETTINGS_BYTES + 1)
    require(len(raw) <= MAX_SETTINGS_BYTES, "settings_size_limit")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                             parse_constant=lambda _: (_ for _ in ()).throw(SharedSettingsHold("nonfinite_json")))
    except (UnicodeError, ValueError, RecursionError) as error:
        raise SharedSettingsHold("invalid_settings_json") from error
    require(type(payload) is dict and "ui_theme" in payload and "library_profiles" in payload,
            "initialized_settings_required")
    from .settings import AppSettings
    try:
        AppSettings._from_payload(payload)  # schema validation, no load/recovery I/O
    except (ValueError, TypeError, AttributeError, OverflowError) as error:
        raise SharedSettingsHold("invalid_settings_schema") from error
    metadata(payload)
    return raw, payload


def observe(capability):
    require(type(capability) is ReceiverCapability, "receiver_capability_required")
    path = _check_path(capability)
    raw, payload = raw_payload(path)
    meta = metadata(payload)
    if meta.get("binding"):
        require(meta["binding"] == capability.binding, "receiver_binding_changed")
    require(raw_payload(path)[0] == raw, "settings_changed_during_observation")
    return SettingsObservation(capability, hashlib.sha256(raw).hexdigest(), encode(payload))


def validate_patch(payload, changes):
    """Validate complete proposed native groups before normalization can hide drift."""
    from .settings import AppSettings
    from .history_configuration import validate_configuration
    from .library_profiles import LibraryProfileRegistry
    from .suite_settings_policy import validate_groups
    validate_groups(changes)
    proposed = deepcopy(payload)
    for fields in changes.values():
        proposed.update(deepcopy(fields))
    if "library.locations" in changes:
        value = changes["library.locations"]
        rows = value["library_profiles"]
        require(all(type(row) is dict and type(row.get("stable_id")) is str and row["stable_id"]
                    for row in rows), "explicit_library_identity_required")
        ids = [row["stable_id"] for row in rows]
        require(len(set(ids)) == len(ids), "duplicate_library_identity")
        registry = LibraryProfileRegistry.from_dicts(rows)
        require(all(not registry.validate(p, replacing_id=p.stable_id) for p in registry.profiles),
                "invalid_library_registry")
        scope = value["library_scope_ids"]
        require(all(type(v) is str and v in ids for v in scope) and len(set(scope)) == len(scope),
                "unresolved_library_scope")
        active = value["active_library_id"]
        require((active in ids) if ids else active == "", "unresolved_active_library")
        root = registry.get(active).root if active else ""
        require(value["working_directory"] == root and value["dataset_directory"] == root,
                "incoherent_active_location")
        require(not value["ingest_directory"] or Path(value["ingest_directory"]).is_absolute(),
                "absolute_ingest_location_required")
        previous = {row.get("stable_id"): row for row in payload.get("library_profiles", [])}
        for row in rows:
            old = previous.get(row["stable_id"])
            if old:
                # A suite location preference never changes privacy or per-library policy.
                require(row.get("role") == old.get("role") and
                        {k: v for k, v in row.items() if k not in {"name", "root"}} ==
                        {k: v for k, v in old.items() if k not in {"name", "root"}},
                        "library_policy_change_requires_separate_review")
    settings = AppSettings._from_payload(proposed)
    for group, values in changes.items():
        require({key: getattr(settings, key) for key in GROUPS[group]} == values, "settings_normalization_hold")
    validate_configuration(proposed.get("history_storage", {}), settings)
    # Native writer also protects structured history roots; raw patches must too.
    from .structured_history_configuration import structured_history_roots
    from .history_configuration import role_roots, overlaps
    require(not any(overlaps(a, b) for a in role_roots(settings)
                    for b in structured_history_roots(proposed.get("structured_history_storage", {}))),
            "history_location_overlap")
    return proposed


def preview_batch(suite, capabilities, *, adopt_groups=(), observations=()):
    """Explicit adoption groups; otherwise update only already inherited groups.

    A retained observation can preview a currently offline participant. No full
    settings payload is copied into the resulting persistent batch plan.
    """
    require(type(suite) is SuiteRevision, "suite_revision_required")
    require(set(adopt_groups) <= set(suite.values), "adoption_group_unavailable")
    cached = {item.capability.binding: item for item in observations}
    by_owner = {}
    for cap in capabilities:
        require(type(cap) is ReceiverCapability, "receiver_capability_required")
        by_owner.setdefault(cap.owner_key, []).append(cap)
    intents = []
    for peers in by_owner.values():
        cap = peers[0]
        require(all((p.profile_id, p.host_id, p.profile_token, p.groups, p.version) ==
                    (cap.profile_id, cap.host_id, cap.profile_token, cap.groups, cap.version) for p in peers),
                "incompatible_shared_file_capabilities")
        aliases = tuple(sorted({p.app_id for p in peers}))
        obs = cached.get(cap.binding)
        offline = False
        try:
            obs = observe(cap)
        except OSError:
            offline = True
        except SharedSettingsHold:
            intents.append(ParticipantIntent(cap, aliases, None, "{}", suite, "conflict"))
            continue
        if obs is None:
            intents.append(ParticipantIntent(cap, aliases, None, "{}", suite, "pending"))
            continue
        meta = metadata(obs.payload)
        if meta.get("suite_id") and not meta.get("detached"):
            if (meta["suite_id"] != suite.suite_id or meta.get("revision", 0) > suite.revision
                    or meta.get("revision") == suite.revision and
                    meta.get("suite_fingerprint") != suite.full_fingerprint):
                intents.append(ParticipantIntent(cap, aliases, obs.digest, "{}", suite, "conflict"))
                continue
        changes = {}
        origin_drift = False
        supported = set(cap.groups) & set(suite.values)
        for group in supported:
            origin = meta["groups"].get(group, {})
            if group in adopt_groups or (not meta.get("detached") and origin.get("mode") == "inherited"):
                if origin and origin.get("value_fingerprint") != group_fingerprint(obs.payload, group):
                    # External/local edits never silently regain inheritance.
                    origin_drift = True
                    continue
                changes[group] = suite.values[group]
        state = "ready" if changes else "override_retained" if supported else "unsupported"
        if origin_drift:
            state, changes = "conflict", {}
        if changes:
            try:
                validate_patch(obs.payload, changes)
            except (ValueError, TypeError):
                state, changes = "conflict", {}
        if offline and state == "ready":
            state = "pending"
        intents.append(ParticipantIntent(cap, aliases, obs.digest, encode(changes), suite, state))
    return BatchPlan(suite, tuple(intents))


def _outcome(intent, status, reason="", receipt=None):
    return ParticipantOutcome(intent.capability.binding, intent.aliases, intent.intent_id,
                              status, reason, encode(receipt or {}))


def _apply(intent, receiver, read_suite):
    from .settings import AppSettings, _settings_writer
    require(receiver.capability == intent.capability, "explicit_receiver_binding_required")
    path = _check_path(intent.capability)
    with _settings_writer(path):
        obs = observe(intent.capability)
        payload, meta = obs.payload, metadata(obs.payload)
        prior = meta["receipts"].get(intent.intent_id)
        if prior is not None:
            require(prior.get("suite_fingerprint") == intent.suite.full_fingerprint
                    and prior.get("changes_fingerprint") == fingerprint(intent.changes_json),
                    "receipt_intent_mismatch")
            require(all(meta["groups"].get(g, {}).get("intent_id") == intent.intent_id and
                        meta["groups"][g]["value_fingerprint"] == group_fingerprint(payload, g)
                        for g in intent.changes), "applied_intent_changed_locally")
            return _outcome(intent, "applied" if prior.get("activated") else "applied_activation_pending",
                            receipt={**prior, "readback_digest": obs.digest})
        require(intent.expected_digest is not None and obs.digest == intent.expected_digest,
                "recipient_revision_conflict")
        require(bool(intent.changes), "reviewed_changes_required")
        proposed = validate_patch(payload, intent.changes)
        receipt = {"intent_id": intent.intent_id, "binding": intent.capability.binding,
                   "suite_id": intent.suite.suite_id, "suite_revision": intent.suite.revision,
                   "suite_fingerprint": intent.suite.full_fingerprint,
                   "changes_fingerprint": fingerprint(intent.changes_json),
                   "previous_digest": obs.digest, "activated": False, "groups": list(intent.changes),
                   "recovery": {"owner": "AppSettings", "history_directory": "settings-history",
                                "previous_sha256": obs.digest}}
        meta.update(binding=intent.capability.binding, suite_id=intent.suite.suite_id,
                    revision=intent.suite.revision, suite_fingerprint=intent.suite.full_fingerprint,
                    detached=False)
        for group in intent.changes:
            meta["groups"][group] = {**deepcopy(meta["groups"].get(group, {})),
                                      "mode": "inherited", "value_fingerprint": group_fingerprint(proposed, group),
                                      "intent_id": intent.intent_id, "activation_pending": True, "suite_revision": intent.suite.revision,
                                      "suite_fingerprint": intent.suite.full_fingerprint}
        meta["receipts"][intent.intent_id] = receipt
        # Bound recovery receipts without discarding any currently referenced intent.
        # A pruned superseded plan still fails its old recipient digest on replay.
        active = {origin.get("intent_id") for origin in meta["groups"].values()}
        for old_id in tuple(meta["receipts"]):
            if len(meta["receipts"]) <= 128:
                break
            if old_id not in active:
                del meta["receipts"][old_id]
        require(len(meta["receipts"]) <= 128, "receipt_capacity_hold")
        proposed[MANAGED_KEY] = meta
        require(read_suite() == intent.suite, "suite_revision_conflict")
        AppSettings._from_payload(payload)._save_locked(path, _patch_payload=proposed, _suite_managed_intent=True)
        saved = observe(intent.capability)
        require(saved.payload.get(MANAGED_KEY) == meta, "recipient_readback_mismatch")
        return _outcome(intent, "applied_activation_pending", receipt={**receipt, "readback_digest": saved.digest})


def apply_batch(plan, *, read_suite, receivers, cancelled=lambda: False):
    """One reviewed batch; never claim global atomicity or runtime activation.

    Receipts are saved with values/origins in each original settings transaction.
    A lost GUI acknowledgement is recovered by idempotent readback of that receipt.
    """
    require(type(plan) is BatchPlan and callable(read_suite), "reviewed_batch_required")
    from .suite_settings_receiver import SettingsReceiver
    receivers = tuple(receivers)
    require(all(type(receiver) is SettingsReceiver for receiver in receivers), "typed_receivers_required")
    bound = {receiver.capability.binding: receiver for receiver in receivers}
    require(len(bound) == len(receivers), "duplicate_bound_receiver")
    outcomes = []
    for intent in plan.participants:
        if cancelled():
            outcomes.append(_outcome(intent, "cancelled", "not_attempted"))
            continue
        if intent.state not in {"ready", "pending"}:
            outcomes.append(_outcome(intent, intent.state))
            continue
        try:
            current = read_suite()
            require(type(current) is SuiteRevision and current == plan.suite, "suite_revision_conflict")
            receiver = bound.get(intent.capability.binding)
            require(receiver is not None, "explicit_receiver_binding_required")
            if intent.expected_digest is None:
                outcomes.append(_outcome(intent, "pending", "fresh_review_required"))
            else:
                outcomes.append(_apply(intent, receiver, read_suite))
        except FileNotFoundError:
            outcomes.append(_outcome(intent, "pending", "recipient_offline"))
        except SharedSettingsHold as error:
            outcomes.append(_outcome(intent, "conflict", str(error)))
        except (OSError, ValueError, TypeError):
            # Original writer may require recovery; never label uncertain write applied.
            outcomes.append(_outcome(intent, "failed", "native_transaction_or_recovery_required"))
    return tuple(outcomes)
