"""Decode the Tag collection without retaining a second full record tree."""
from __future__ import annotations

import json
import re

from .knowledge_lifecycle import KnowledgeLifecycleError, apply_transitions, parse_envelope

_SPACE = re.compile(r"[ \t\n\r]*")


class KnowledgeLifecycleCodec:
    """Preserve explicitly loaded lifecycle authority without enrolling a writer.

    Raw lifecycle/schema fragments survive even when their typed interpretation
    is held. The containing store retains the complete original document bytes.
    No filesystem access, token generation or canonical schema rollout occurs.
    This is codec/identity preservation, not plain-save stale or interprocess
    admission. All canonical participants and native recovery still require
    the shared writer rollout before enrolled live stores may be produced.
    """

    def __init__(self, entity_kind, legacy_schema, enrolled_schema):
        self.entity_kind = entity_kind
        self.legacy_schema = legacy_schema
        self.enrolled_schema = enrolled_schema
        self.raw_fields = []
        self._seen_fields = set()
        self._duplicate_fields = False
        self._seen_records = set()
        self._duplicate_records = False
        self._duplicate_json = False
        self._inventory_seen = False
        self._invalid_records = False
        self._envelope = None
        self._hold = ""

    @property
    def envelope(self):
        return self._envelope

    @property
    def write_hold(self):
        return self._hold

    def observe_field(self, key, raw=None):
        self._duplicate_fields |= key in self._seen_fields
        self._seen_fields.add(key)
        if key in {"schema_version", "knowledge_lifecycle"}:
            self.raw_fields.append((key, raw))

    def decode_pairs(self, pairs):
        value = {}
        for key, item in pairs:
            self._duplicate_json |= key in value
            value[key] = item
        return value

    def observe_inventory(self, is_list):
        self._inventory_seen = True
        self._invalid_records |= not is_list

    def observe_record(self, record_id, declared_id):
        self._invalid_records |= not isinstance(declared_id, str) or record_id != declared_id
        self._duplicate_records |= record_id in self._seen_records
        self._seen_records.add(record_id)

    def finish(self):
        schemas = [raw for key, raw in self.raw_fields if key == "schema_version"]
        envelopes = [raw for key, raw in self.raw_fields if key == "knowledge_lifecycle"]
        try:
            declared_schemas = [json.loads(raw) for raw in schemas] or [1]
            schema = declared_schemas[-1]
            if any(type(item) is not int or item < 1 or item > self.enrolled_schema for item in declared_schemas):
                raise KnowledgeLifecycleError("Canonical Knowledge schema is unsupported; saving would downgrade authority.")
            if len(schemas) > 1 and (envelopes or self.enrolled_schema in declared_schemas):
                raise KnowledgeLifecycleError("Duplicate canonical schema declarations make enrolled authority ambiguous.")
            if not envelopes:
                if schema == self.enrolled_schema:
                    raise KnowledgeLifecycleError("Enrolled Knowledge schema is missing its lifecycle envelope.")
                return
            if schema != self.enrolled_schema:
                raise KnowledgeLifecycleError("Lifecycle envelope and canonical Knowledge schema disagree.")
            if self._duplicate_fields or self._duplicate_records or self._duplicate_json:
                raise KnowledgeLifecycleError("Enrolled Knowledge contains duplicate authority fields or record identities.")
            if not self._inventory_seen or self._invalid_records:
                raise KnowledgeLifecycleError("Enrolled Knowledge needs an exact canonical record inventory.")
            envelope = parse_envelope(envelopes[0].encode("utf-8"))
            if envelope.entity_kind != self.entity_kind:
                raise KnowledgeLifecycleError("Lifecycle namespace has the wrong entity kind for this document.")
            self._envelope = envelope
        except (ValueError, UnicodeError, TypeError) as exc:
            self._hold = str(exc)

    def _validate_current(self, envelope, record_ids, *, removed_ids=(), redirects=None):
        records = tuple(record_ids)
        if len(set(records)) != len(records):
            raise KnowledgeLifecycleError("Duplicate current Knowledge identities cannot be saved.")
        active = {entry.reference.record_id for entry in envelope.entries if entry.state == "active"}
        if active != set(records):
            raise KnowledgeLifecycleError("Effective Knowledge identities changed without a typed lifecycle transition.")
        if self.entity_kind != "tag":
            return
        removed = set(removed_ids)
        redirects = dict(redirects or {})
        if active.intersection(removed) or active.intersection(redirects):
            raise KnowledgeLifecycleError("Active Tag lifecycle conflicts with native tombstones or redirects.")
        by_token = {entry.reference.incarnation_id: entry for entry in envelope.entries}
        for entry in envelope.entries:
            record_id = entry.reference.record_id
            if entry.state == "retired" and record_id not in active and record_id not in removed and record_id not in redirects:
                raise KnowledgeLifecycleError("Retired Tag lifecycle is missing its native tombstone.")
            if entry.state != "redirected":
                continue
            target = by_token[entry.successor.incarnation_id]
            while target.successor is not None:
                target = by_token[target.successor.incarnation_id]
            current, seen = record_id, set()
            while current in redirects and current not in seen:
                seen.add(current)
                current = redirects[current]
            if current in seen or current != target.reference.record_id or record_id not in removed:
                raise KnowledgeLifecycleError("Tag redirect disagrees with its retained lifecycle survivor.")

    def bind_current(self, record_ids, **kwargs):
        """At load only, retain semantic readability while recording a write hold."""
        if self._hold or self._envelope is None:
            return
        try:
            self._validate_current(self._envelope, record_ids, **kwargs)
        except KnowledgeLifecycleError as exc:
            self._hold = str(exc)

    def fields_for_write(self, record_ids, **kwargs):
        if self._hold:
            raise KnowledgeLifecycleError(self._hold)
        if self._envelope is None:
            return {"schema_version": self.legacy_schema}
        self._validate_current(self._envelope, record_ids, **kwargs)
        return {"schema_version": self.enrolled_schema,
                "knowledge_lifecycle": json.loads(self._envelope.to_bytes())}

    def stage(self, transitions, *, operation_id, expected_fingerprint, record_ids, **kwargs):
        if self._hold:
            raise KnowledgeLifecycleError(self._hold)
        if self._envelope is None:
            raise KnowledgeLifecycleError("Legacy Knowledge is not enrolled; a save cannot invent lifecycle identity.")
        before = tuple(entry.reference.record_id for entry in self._envelope.entries if entry.state == "active")
        result = apply_transitions(self._envelope, transitions, operation_id=operation_id,
                                   expected_fingerprint=expected_fingerprint,
                                   effective_before=before, effective_after=tuple(record_ids))
        self._validate_current(result, record_ids, **kwargs)
        self._envelope = result


def decode_tag_storage(text, build_record, *, lifecycle_codec=None):
    """Return root fields and typed Tags, preserving ordinary JSON semantics.

    Only the top-level ``tags`` array is streamed. Keys and every individual
    value use the standard decoder, including nested unknown fields. Storage
    remains one ordinary JSON document. No caller-owned state changes here.
    ``None`` for records retains the legacy handling of a non-array tags value.
    """
    decoder = json.JSONDecoder(object_pairs_hook=lifecycle_codec.decode_pairs) if lifecycle_codec is not None else json.JSONDecoder()
    position = _SPACE.match(text, 0).end()
    if text[position:position + 1] != "{":
        return decoder.decode(text), None
    position = _SPACE.match(text, position + 1).end()
    payload = {}
    records = None
    conversion_error = None
    if text[position:position + 1] == "}":
        position += 1
    else:
        while True:
            if text[position:position + 1] != '"':
                raise json.JSONDecodeError("Expecting property name enclosed in double quotes", text, position)
            key, position = decoder.raw_decode(text, position)
            position = _SPACE.match(text, position).end()
            if text[position:position + 1] != ":":
                raise json.JSONDecodeError("Expecting ':' delimiter", text, position)
            position = _SPACE.match(text, position + 1).end()
            value_start = position
            if lifecycle_codec is not None and key not in {"schema_version", "knowledge_lifecycle"}:
                lifecycle_codec.observe_field(key)
            if key == "tags":
                # The last root key wins. Release a superseded typed graph
                # before decoding the replacement value, even a scalar.
                records = None
                current = None
                conversion_error = None
                payload.pop(key, None)
            if key == "tags" and build_record is not None and text[position:position + 1] == "[":
                if lifecycle_codec is not None:
                    lifecycle_codec.observe_inventory(True)
                current = {}
                conversion_error = None
                position = _SPACE.match(text, position + 1).end()
                if text[position:position + 1] != "]":
                    while True:
                        item, position = decoder.raw_decode(text, position)
                        if isinstance(item, dict) and conversion_error is None:
                            try:
                                record = build_record(item)
                                if lifecycle_codec is not None:
                                    lifecycle_codec.observe_record(record.id, item.get("id"))
                                current[record.id] = record
                                del record
                            except Exception as error:
                                # Like json.loads, the last duplicate root key
                                # wins; an earlier superseded array is not used.
                                current.clear()
                                conversion_error = error
                        elif not isinstance(item, dict) and lifecycle_codec is not None:
                            lifecycle_codec._invalid_records = True
                        del item
                        position = _SPACE.match(text, position).end()
                        delimiter = text[position:position + 1]
                        if delimiter == "]":
                            break
                        if delimiter != ",":
                            raise json.JSONDecodeError("Expecting ',' delimiter", text, position)
                        position = _SPACE.match(text, position + 1).end()
                position += 1
                payload[key] = []
                records = current
            else:
                value, position = decoder.raw_decode(text, position)
                payload[key] = value
                if lifecycle_codec is not None and key == (
                    "tags" if lifecycle_codec.entity_kind == "tag" else "categories"
                ):
                    lifecycle_codec.observe_inventory(isinstance(value, list))
                    if isinstance(value, list):
                        for item in value:
                            if isinstance(item, dict):
                                lifecycle_codec.observe_record(str(item.get("id") or "other"), item.get("id"))
                            else:
                                lifecycle_codec._invalid_records = True
                del value
                if key == "tags":
                    records = None
                    conversion_error = None
            if lifecycle_codec is not None and key in {"schema_version", "knowledge_lifecycle"}:
                lifecycle_codec.observe_field(key, text[value_start:position])
            position = _SPACE.match(text, position).end()
            delimiter = text[position:position + 1]
            if delimiter == "}":
                position += 1
                break
            if delimiter != ",":
                raise json.JSONDecodeError("Expecting ',' delimiter", text, position)
            position = _SPACE.match(text, position + 1).end()
    position = _SPACE.match(text, position).end()
    if position != len(text):
        raise json.JSONDecodeError("Extra data", text, position)
    if conversion_error is not None:
        raise conversion_error
    if lifecycle_codec is not None:
        lifecycle_codec.finish()
    return payload, records
