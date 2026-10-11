"""Pure explicit saved-default identity context; never reads or writes files."""
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
import json
from types import MappingProxyType

SOFTWARE_VERSION = "0.1.0-alpha.2"
CONTEXT_INTERFACE_VERSION = 1
RELEASE_DATE = "2026-10-05"
MAX_IDENTITY_DEPTH = 64


class DefaultTagIdentityHold(ValueError):
    pass


def document_revision(raw):
    return sha256(b"absent\0" if raw is None else b"present\0" + raw).hexdigest()


def pair_revision(tags_raw, tracking_raw, tags_identity, tracking_identity):
    payload = [str(tags_identity), document_revision(tags_raw),
               str(tracking_identity), document_revision(tracking_raw)]
    return sha256(b"collectigen-default-tag-identity-v1\0" +
                  json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DefaultTagIdentityHold("Duplicate identity-context JSON field")
        result[key] = value
    return result


def _payload(raw):
    value = {} if raw is None else json.loads(raw, object_pairs_hook=_pairs)
    if not isinstance(value, dict):
        raise DefaultTagIdentityHold("Identity context requires object documents")
    return value


@dataclass(frozen=True)
class DefaultTagIdentityContext:
    tags_identity: str
    tracking_identity: str
    tags_revision: str
    revision: str
    generation: int
    mappings: object


def admit_default_tag_identity(tags_raw, tracking_raw, *, tags_identity,
                              tracking_identity, required_ids, generation=0,
                              prospective_ids=(), equivalent_ids=()):
    """Validate the complete requested graph before collapsing records.

    Prospective IDs are for an explicitly planned synchronization graph only;
    runtime callers leave them empty. Dormant tracking is not traversed.
    """
    payload, state = _payload(tags_raw), _payload(tracking_raw)
    rows = payload.get("tags", [])
    if not isinstance(rows, list):
        raise DefaultTagIdentityHold("Invalid Tag records for identity admission")
    counts = Counter(row.get("id") for row in rows if isinstance(row, dict)
                     and isinstance(row.get("id"), str) and row["id"].strip())
    active = set(counts) | set(prospective_ids)
    removed = set(payload.get("removed_tag_ids", []))
    disabled = set(payload.get("disabled_default_ids", []))
    redirects = payload.get("merged_tag_ids", {})
    records = state.get("records", {})
    tracking = records.get("tags", {}) if isinstance(records, dict) else None
    if not isinstance(redirects, dict) or not isinstance(tracking, dict):
        raise DefaultTagIdentityHold("Invalid saved identity graph")
    mappings = {}
    def walk(identity):
        current, seen, used = identity, set(), []
        for _ in range(MAX_IDENTITY_DEPTH):
            if not isinstance(current, str) or not current.strip():
                raise DefaultTagIdentityHold("Invalid relevant identity target")
            if current in seen:
                raise DefaultTagIdentityHold("Relevant default identity cycle")
            seen.add(current)
            row = tracking.get(current)
            if current in active and (current in counts or not row or (isinstance(row, dict) and row.get("local_id") == current)):
                if counts.get(current, 1) != 1:
                    raise DefaultTagIdentityHold("Duplicate relevant local Tag ID")
                # Exact-active behavior is preserved; a mapped target conflict
                # is never used to resolve somebody else's missing identity.
                if used and current in removed | disabled:
                    raise DefaultTagIdentityHold("Mapped target has conflicting retirement")
                row = tracking.get(current)
                if current == identity and isinstance(row, dict):
                    target = row.get("local_id")
                    if target and target != current:
                        raise DefaultTagIdentityHold("Active source conflicts with saved mapping")
                for source in used:
                    mappings[source] = current
                return current
            if current in redirects:
                current = redirects[current]
                continue
            if current in removed | disabled:
                if used:
                    raise DefaultTagIdentityHold("Mapped target is deliberately retired")
                return None
            row = tracking.get(current)
            if not isinstance(row, dict) or not isinstance(row.get("local_id"), str) or not row["local_id"].strip():
                raise DefaultTagIdentityHold("Relevant saved identity target is missing")
            used.append(current)
            current = row["local_id"]
        raise DefaultTagIdentityHold("Relevant identity depth exceeds bound")
    for identity in sorted(set(required_ids)):
        walk(identity)
    groups = tuple(frozenset(group) for group in equivalent_ids)
    owners = {}
    for source, target in mappings.items():
        owners.setdefault(target, set()).add(source)
    for sources in owners.values():
        if len(sources) > 1 and not any(sources <= group for group in groups):
            raise DefaultTagIdentityHold("Multiple shipped identities have ambiguous local ownership")
    return DefaultTagIdentityContext(str(tags_identity), str(tracking_identity),
        document_revision(tags_raw), pair_revision(tags_raw, tracking_raw, tags_identity, tracking_identity),
        generation, MappingProxyType(mappings))


def resolve_tag_identity(stable_id, *, active_ids, removed_ids, redirects, context=None):
    current = str(stable_id or "").strip()
    seen = set()
    mapped = False
    while current and current not in seen:
        if current in active_ids:
            if mapped and current in removed_ids:
                raise DefaultTagIdentityHold("Mapped target retirement changed")
            return current
        seen.add(current)
        if current in redirects:
            current = str(redirects[current] or "").strip()
        elif current in removed_ids:
            return None
        elif context is not None and current in context.mappings:
            current = context.mappings[current]
            mapped = True
            if current not in active_ids or current in removed_ids:
                raise DefaultTagIdentityHold("Mapped target is no longer active")
        else:
            return None
    if mapped:
        raise DefaultTagIdentityHold("Identity graph changed to a cycle")
    return None
