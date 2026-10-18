"""Finite shared-settings domain contract; immutable JSON values, no I/O."""
from dataclasses import dataclass
from .managed_settings_protection import (SCHEMA, MANAGED_KEY, GROUPS,
    MAX_CONTRACT_BYTES, SharedSettingsHold, require, _pairs, decode, encode, fingerprint)


def validate_groups(value):
    require(type(value) is dict and bool(value), "groups_required")
    for group, fields in value.items():
        require(group in GROUPS, "unsupported_setting_group")
        require(type(fields) is dict and set(fields) == set(GROUPS[group]), "coherent_group_required")
        if group == "appearance.theme":
            require(type(fields["ui_theme"]) is str and fields["ui_theme"] in {"system", "light", "dark", "midnight"}, "unsupported_theme")
        elif group == "appearance.accessibility":
            require(type(fields["show_tooltips"]) is bool and type(fields["color_blind_mode"]) is bool,
                    "invalid_accessibility_value")
        else:
            require(type(fields["library_profiles"]) is list and type(fields["library_scope_ids"]) is list,
                    "invalid_library_group")
            require(all(type(fields[key]) is str for key in
                        ("active_library_id", "dataset_directory", "working_directory", "ingest_directory")),
                    "invalid_library_location")


@dataclass(frozen=True)
class SuiteRevision:
    suite_id: str
    revision: int
    values_json: str

    def __post_init__(self):
        require(type(self.suite_id) is str and 0 < len(self.suite_id) <= 128, "suite_identity_required")
        require(type(self.revision) is int and self.revision > 0, "suite_revision_required")
        value = decode(self.values_json)
        validate_groups(value)
        object.__setattr__(self, "values_json", encode(value))

    @classmethod
    def create(cls, suite_id, revision, values):
        return cls(suite_id, revision, encode(values))

    @property
    def values(self):
        return decode(self.values_json)  # Always detached; retained state is immutable.

    @property
    def full_fingerprint(self):
        return fingerprint(encode({"schema": SCHEMA, "suite_id": self.suite_id,
                                   "revision": self.revision, "values": self.values}))


@dataclass(frozen=True)
class ReceiverCapability:
    app_id: str
    profile_id: str
    host_id: str
    settings_path: str
    profile_token: tuple
    groups: tuple
    version: int = SCHEMA

    def __post_init__(self):
        require(all(type(v) is str and 0 < len(v) <= 4096 for v in
                    (self.app_id, self.profile_id, self.host_id, self.settings_path)), "receiver_identity_required")
        require(self.version == SCHEMA and type(self.version) is int, "unsupported_receiver_version")
        require(type(self.groups) is tuple and len(set(self.groups)) == len(self.groups)
                and all(g in GROUPS for g in self.groups), "invalid_receiver_groups")
        require(type(self.profile_token) is tuple and len(self.profile_token) == 2
                and all(type(v) is int for v in self.profile_token), "profile_binding_required")

    @property
    def owner_key(self):
        import os
        return os.path.normcase(self.settings_path)

    @property
    def binding_json(self):
        return encode({"app_id": self.app_id, "profile_id": self.profile_id, "host_id": self.host_id,
                       "settings_path": self.settings_path, "profile_token": self.profile_token,
                       "groups": self.groups, "version": self.version})

    @property
    def binding(self):
        return fingerprint(self.binding_json)


@dataclass(frozen=True)
class SettingsObservation:
    capability: ReceiverCapability
    digest: str
    payload_json: str

    def __post_init__(self):
        require(type(self.capability) is ReceiverCapability, "receiver_capability_required")
        require(type(self.digest) is str and len(self.digest) == 64, "settings_digest_required")
        object.__setattr__(self, "payload_json", encode(decode(self.payload_json)))

    @property
    def payload(self):
        return decode(self.payload_json)


@dataclass(frozen=True)
class ParticipantIntent:
    capability: ReceiverCapability
    aliases: tuple
    expected_digest: str | None
    changes_json: str
    suite: SuiteRevision
    state: str

    def __post_init__(self):
        require(type(self.capability) is ReceiverCapability and type(self.suite) is SuiteRevision,
                "typed_intent_required")
        require(type(self.aliases) is tuple and all(type(v) is str for v in self.aliases), "intent_aliases_required")
        require(self.state in {"ready", "pending", "unsupported", "override_retained", "conflict"}, "invalid_intent_state")
        require(self.expected_digest is None or type(self.expected_digest) is str and len(self.expected_digest) == 64,
                "intent_digest_required")
        changes = decode(self.changes_json)
        require(type(changes) is dict, "invalid_intent_changes")
        if changes:
            validate_groups(changes)
        require(set(changes) <= set(self.capability.groups), "receiver_group_unsupported")
        object.__setattr__(self, "changes_json", encode(changes))

    @property
    def changes(self):
        return decode(self.changes_json)

    @property
    def intent_id(self):
        return fingerprint(encode({"binding": self.capability.binding, "digest": self.expected_digest,
                                   "changes": self.changes, "suite": self.suite.full_fingerprint}))


@dataclass(frozen=True)
class BatchPlan:
    suite: SuiteRevision
    participants: tuple

    def __post_init__(self):
        require(type(self.suite) is SuiteRevision and type(self.participants) is tuple,
                "immutable_batch_required")
        require(len(self.participants) <= 128, "batch_participant_limit")
        require(all(type(p) is ParticipantIntent and p.suite == self.suite for p in self.participants),
                "batch_suite_mismatch")
        require(len({p.capability.owner_key for p in self.participants}) == len(self.participants),
                "duplicate_physical_participant")


    @property
    def full_fingerprint(self):
        return fingerprint(encode(self._body()))

    def _body(self):
        return {"schema": SCHEMA, "suite": {"suite_id": self.suite.suite_id,
            "revision": self.suite.revision, "values": self.suite.values}, "participants": [
            {"capability": decode(p.capability.binding_json), "aliases": p.aliases,
             "expected_digest": p.expected_digest, "changes": p.changes, "state": p.state}
            for p in self.participants]}

    def to_json(self):
        """Bounded durable intent only, not raw recipient settings snapshots."""
        return encode({"body": self._body(), "fingerprint": self.full_fingerprint})

    @classmethod
    def from_json(cls, value):
        document = decode(value)
        require(type(document) is dict and set(document) == {"body", "fingerprint"}, "invalid_batch_envelope")
        body = document["body"]
        require(type(body) is dict and set(body) == {"schema", "suite", "participants"}
                and type(body["schema"]) is int and body["schema"] == SCHEMA, "invalid_batch_schema")
        require(fingerprint(encode(body)) == document["fingerprint"], "batch_fingerprint_mismatch")
        suite_value = body["suite"]
        require(type(suite_value) is dict and set(suite_value) == {"suite_id", "revision", "values"}, "invalid_batch_suite")
        suite = SuiteRevision.create(**{ "suite_id": suite_value["suite_id"],
            "revision": suite_value["revision"], "values": suite_value["values"]})
        require(type(body["participants"]) is list and len(body["participants"]) <= 128, "batch_participant_limit")
        participants = []
        for row in body["participants"]:
            require(type(row) is dict and set(row) == {"capability", "aliases", "expected_digest", "changes", "state"},
                    "invalid_batch_participant")
            binding = row["capability"]
            require(type(binding) is dict and set(binding) == {"app_id", "profile_id", "host_id", "settings_path",
                    "profile_token", "groups", "version"}, "invalid_batch_binding")
            require(type(binding["profile_token"]) is list and type(binding["groups"]) is list
                    and type(row["aliases"]) is list, "invalid_batch_binding")
            cap = ReceiverCapability(**{**binding, "profile_token": tuple(binding["profile_token"]),
                                        "groups": tuple(binding["groups"])})
            participants.append(ParticipantIntent(cap, tuple(row["aliases"]), row["expected_digest"],
                                                  encode(row["changes"]), suite, row["state"]))
        return cls(suite, tuple(participants))


@dataclass(frozen=True)
class ParticipantOutcome:
    binding: str
    aliases: tuple
    intent_id: str
    status: str
    reason: str = ""
    receipt_json: str = "{}"

    @property
    def receipt(self):
        return decode(self.receipt_json)

