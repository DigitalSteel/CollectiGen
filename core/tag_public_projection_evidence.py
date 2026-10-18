"""Pure hash-bound projection replay. Private inputs never become output members."""
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
import re

FILES = frozenset('data/defaults/' + name + '.json' for name in (
    'categories', 'characters', 'community_tags', 'concepts', 'consumer_policies',
    'foundation_tags', 'modifiers', 'outfits', 'patterns', 'promoted_tags',
    'relationships', 'series', 'tag_aliases', 'tag_appearance'))
MEMBERS = frozenset({'PROJECTION_PROPOSAL.json', 'DISPOSITIONS.jsonl', 'AMBIGUITIES.json',
                     'INTERNAL_PROJECTION_ACCEPTANCE.json', 'SEMANTIC_PRESERVATION.json'})
MAX_BYTES = 64 * 1024 * 1024
MAX_DISPOSITIONS = 200000
ANNOTATIONS = (('promoted_tags.json', 'tags', 'two-tone', ('relations', 'semantic_provenance')),
               ('relationships.json', 'relationships', 'hair.length.contrary_branches', ('details', 'members_are')),
               ('relationships.json', 'relationships', 'hair.allowed.length', ('details', 'value_policy')))
OPERATOR_PATHS = frozenset({
    '$.tags[].seen_count', '$.tags[].first_seen', '$.tags[].last_seen',
    '$.merge_rules[].promotion.reviewed_at', '$.tags[].provenance[].reviewed_at',
    '$.tags[].promotion.reviewed_at', '$.tags[].content_sensitivity.provenance[].reviewed_at',
    '$.tags[].ordering.provenance[].reviewed_at', '$.categories[].promotion.reviewed_at',
    '$.categories[].ordering.provenance[].reviewed_at', '$.categories[].ordering.provenance[].before_id',
    '$.categories[].ordering.provenance[].after_id', '$.categories[].ordering.provenance[].batch_size',
    '$.patterns[].promotion.reviewed_at', '$.series[].promotion.reviewed_at', '$.characters[].promotion.reviewed_at',
    '$.outfits[].relationship_review.reviewed_at', '$.outfits[].promotion.reviewed_at',
    '$.relationships[].details.reviewed_at', '$.relationships[].promotion.reviewed_at',
    '$.relationships[].details.retired_at', '$.relationships[].compatibility_projection_payload.reviewed_at',
    '$.policies[].reviewed_at'})
RECOVERY_PATHS = frozenset({'$.merge_rules[].intent_migration', '$.merge_rules[].overlap_repair',
    '$.outfits[].legacy_character_fields', '$.relationships[].allowed_dimension_history',
    '$.relationships[].compatibility_projection_payload.identity_merge'})
ASSOCIATION_FIELDS = frozenset({'observations', 'sample_count', 'rate', 'lift', 'confidence'})


def _need(condition, reason):
    if not condition:
        raise ValueError(reason)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _need(key not in result, 'duplicate_evidence_key')
        result[key] = value
    return result


def decode(raw):
    _need(type(raw) is bytes and len(raw) <= MAX_BYTES, 'bounded_evidence_bytes_required')
    def constant(_value):
        raise ValueError('nonfinite_evidence_number')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique, parse_constant=constant)
    def depth(item, level=0):
        _need(level <= 64, 'evidence_depth')
        if isinstance(item, dict):
            for child in item.values(): depth(child, level + 1)
        elif isinstance(item, list):
            for child in item: depth(child, level + 1)
    depth(value)
    return value


def canonical_hash(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                             ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def _inventory(rows, names):
    _need(type(rows) is list and len(rows) == len(names), 'exact_authority_inventory_required')
    result = {}
    for row in rows:
        _need(type(row) is list and len(row) == 2 and type(row[0]) is str
              and row[0] in names and row[0] not in result
              and type(row[1]) is str and re.fullmatch('[0-9a-f]{64}', row[1]), 'invalid_authority_inventory')
        result[row[0]] = row[1]
    _need(set(result) == names, 'complete_authority_inventory_required')
    return result


@dataclass(frozen=True)
class ProjectionAuthority:
    """Caller supplies separately reviewed pins, never evidence's own assertions."""
    inputs: tuple
    outputs: tuple
    members: tuple
    policy_sha256: str
    pack_sha256: str
    source_sha256: str
    release_fingerprint: str
    snapshot_fingerprint: str

    @classmethod
    def from_document(cls, value):
        keys = {'contract', 'inputs', 'outputs', 'members', 'policy_sha256', 'pack_sha256',
                'source_sha256', 'release_fingerprint', 'snapshot_fingerprint'}
        _need(type(value) is dict and set(value) == keys
              and value['contract'] == 'tag_projection_authority/1', 'separate_projection_authority_required')
        inventories = [_inventory(value[key], names) for key, names in
                       (('inputs', FILES), ('outputs', FILES), ('members', MEMBERS))]
        scalar = [value[key] for key in ('policy_sha256', 'pack_sha256', 'source_sha256',
                                       'release_fingerprint', 'snapshot_fingerprint')]
        _need(all(type(x) is str and re.fullmatch('[0-9a-f]{64}', x) for x in scalar), 'exact_authority_pins_required')
        return cls(*(tuple(sorted(x.items())) for x in inventories), *scalar)


def validate_projection(inputs, outputs, evidence, *, authority):
    _need(type(authority) is ProjectionAuthority, 'separate_projection_authority_required')
    for values, expected, names in ((inputs, authority.inputs, FILES), (outputs, authority.outputs, FILES),
                                     (evidence, authority.members, MEMBERS)):
        _need(type(values) is dict and set(values) == names, 'exact_projection_members_required')
        _need(all(type(x) is bytes for x in values.values()) and sum(map(len, values.values())) <= MAX_BYTES,
              'bounded_projection_members_required')
        _need({n: sha256(b).hexdigest() for n, b in values.items()} == dict(expected), 'projection_authority_mismatch')
    proposal = decode(evidence['PROJECTION_PROPOSAL.json'])
    acceptance = decode(evidence['INTERNAL_PROJECTION_ACCEPTANCE.json'])
    _need(type(proposal) is dict and {'inputs', 'outputs', 'basis_prior_projection_policy_sha256'} <= set(proposal)
          and type(acceptance) is dict and {'accepted_input_count', 'accepted_disposition_count',
              'release_approval', 'signing_or_publication', 'user_publication_approval'} <= set(acceptance),
          'typed_projection_review_required')
    _need(proposal['inputs'] == [list(x) for x in authority.inputs]
          and proposal['outputs'] == [list(x) for x in authority.outputs]
          and proposal['basis_prior_projection_policy_sha256'] == authority.policy_sha256,
          'projection_policy_inventory_mismatch')
    _need(acceptance['accepted_input_count'] == 14 and acceptance['release_approval'] is False
          and acceptance['signing_or_publication'] is False and acceptance['user_publication_approval'] is False,
          'bounded_internal_review_required')
    replay = {n: decode(raw) for n, raw in inputs.items()}
    seen = set(); counts = {}
    ledger = evidence['DISPOSITIONS.jsonl'].splitlines()
    _need(len(ledger) <= MAX_DISPOSITIONS and len(ledger) == acceptance['accepted_disposition_count'],
          'exact_disposition_count_required')
    for raw in ledger:
        row = decode(raw)
        _need(type(row) is dict and set(row) == {'file', 'json_pointer', 'normalized_path', 'disposition'}
              and all(type(x) is str for x in row.values()), 'typed_disposition_required')
        name = 'data/defaults/' + row['file']
        pointer = row['json_pointer']
        _need(name in FILES and type(pointer) is str and pointer.startswith('/'), 'invalid_disposition_path')
        identity = (name, pointer)
        _need(identity not in seen, 'duplicate_disposition')
        seen.add(identity)
        parts = pointer[1:].split('/')
        _need(all(not re.search(r'~(?![01])', part) for part in parts), 'invalid_pointer_escape')
        parts = [part.replace('~1', '/').replace('~0', '~') for part in parts]
        parent = replay[name]
        normalized = '$'
        for part in parts[:-1]:
            if type(parent) is list:
                _need(part.isdecimal() and str(int(part)) == part and int(part) < len(parent), 'invalid_pointer_index')
                parent = parent[int(part)]; normalized += '[]'
            else:
                _need(type(parent) is dict and part in parent, 'invalid_pointer_member')
                parent = parent[part]; normalized += '.' + part
        _need(type(parent) is dict and parts[-1] in parent, 'disposition_must_remove_dictionary_field')
        normalized += '.' + parts[-1]
        _need(normalized == row['normalized_path'], 'normalized_disposition_path_mismatch')
        action = row['disposition']
        _need(action in {'omit_typed_recovery_container_keep_live_definition', 'omit_operator_or_usage_metadata',
                        'omit_local_library_association_statistic_keep_link_state_source'}, 'unsupported_disposition')
        _need((action == 'omit_operator_or_usage_metadata' and normalized in OPERATOR_PATHS)
              or (action == 'omit_typed_recovery_container_keep_live_definition' and normalized in RECOVERY_PATHS)
              or (action == 'omit_local_library_association_statistic_keep_link_state_source'
                  and normalized == '$.characters[].tag_associations[].' + parts[-1]
                  and parts[-1] in ASSOCIATION_FIELDS and parent.get('source') == 'aggregate local library cache'),
              'unapproved_disposition_scope')
        del parent[parts[-1]]
        counts[action] = counts.get(action, 0) + 1
    projected = {n: decode(raw) for n, raw in outputs.items()}
    _need(all(canonical_hash(replay[n]) == canonical_hash(projected[n]) for n in FILES), 'exact_projection_replay_required')
    ambiguities = decode(evidence['AMBIGUITIES.json'])
    _need(type(ambiguities) is list and len(ambiguities) == 3, 'exact_retained_annotations_required')
    expected = {(f, identity, '.'.join(path)) for f, _collection, identity, path in ANNOTATIONS}
    _need(all(type(x) is dict and {'file', 'record_id', 'field', 'decision'} <= set(x) for x in ambiguities)
          and {(x['file'], x['record_id'], x['field']) for x in ambiguities} == expected
          and all(x['decision'] == 'retain_exact_legacy_annotation_pending_role_review' for x in ambiguities),
          'exact_retained_annotations_required')
    subtrees = []
    for filename, collection, identity, path in ANNOTATIONS:
        name = 'data/defaults/' + filename
        def subtree(document):
            matches = [x for x in document[collection] if x['id'] == identity]
            _need(len(matches) == 1, 'unique_annotation_owner_required')
            value = matches[0]
            for key in path: value = value[key]
            return value
        before, after = subtree(decode(inputs[name])), subtree(projected[name])
        _need(canonical_hash(before) == canonical_hash(after), 'retained_annotation_changed')
        subtrees.append({'file': name, 'record_id': identity, 'field': '.'.join(path), 'sha256': canonical_hash(after)})
    # Explicit public allowlist: no source documents, ledger values, receipt text,
    # internal review material, operator paths or arbitrary evidence fields.
    return {'contract': 'tag_public_projection_summary/1', 'scope': 'fresh_install_only',
            'publication_clearance': False, 'source_inventory': [list(x) for x in authority.outputs],
            'policy_sha256': authority.policy_sha256, 'disposition_count': len(ledger),
            'omission_counts': counts, 'retained_annotation_fingerprints': subtrees}
