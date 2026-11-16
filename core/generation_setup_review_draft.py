"""Bounded unfinished image-setup choices; no pixels, services or execution authority."""
from copy import deepcopy
import json
import math

from core.generation_planning import _check_bounds, MAX_PLAN_BYTES, MAX_COLLECTION_ITEMS

SCHEMA_VERSION = 1
KIND = 'image-setup-unfinished-review'
MAX_SNAPSHOT_BYTES = MAX_PLAN_BYTES // 2
PART_KINDS = ('character', 'scene', 'pose', 'style', 'other')
INCLUSIONS = ('model', 'sampling', 'prompt', 'negative')
DISCLOSURES = ('lora', 'resources', 'manual_parts', 'suggestion_source', 'exact')


class ReviewSnapshotError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise ReviewSnapshotError(message)


def _strict_json(value, depth=0):
    _require(depth <= 64, 'review_snapshot_depth')
    _require(type(value) in (dict, list, str, int, float, bool, type(None)),
             'review_snapshot_json_only')
    if type(value) is dict:
        _require(len(value) <= MAX_COLLECTION_ITEMS, 'review_snapshot_collection')
        for key, nested in value.items():
            _require(type(key) is str, 'review_snapshot_string_keys')
            _strict_json(nested, depth + 1)
    elif type(value) is list:
        _require(len(value) <= MAX_COLLECTION_ITEMS, 'review_snapshot_collection')
        for nested in value:
            _strict_json(nested, depth + 1)
    elif type(value) is float:
        _require(math.isfinite(value), 'review_snapshot_finite_numbers')


def _object(value, required):
    _require(type(value) is dict and set(required) <= set(value), 'review_snapshot_fields')


def _index(value, size):
    _require(type(value) is int and -1 <= value < size, 'review_snapshot_selection')


def _bool(value):
    _require(type(value) is bool, 'review_snapshot_boolean')


def _text(value, maximum=None):
    _require(type(value) is str and (maximum is None or len(value.encode('utf-16-le')) // 2 <= maximum),
             'review_snapshot_text')


def _weight(value):
    _require(type(value) in (int, float) and -10 <= value <= 10 and math.isfinite(value)
             and round(value, 6) == value,
             'review_snapshot_authored_weight')


def validate_review_snapshot(value):
    """Validate without coercion; preserve every bounded unknown JSON field.

    The caller must use the original guarded GenerationPlan save/readback and
    reconstruct current providers/services from its admitted host. JSON data
    here never authenticates a source locator or grants filesystem access.
    """
    _strict_json(value)
    _check_bounds(value)
    _require(len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'))
             <= MAX_SNAPSHOT_BYTES, 'review_snapshot_size')
    _object(value, ('schema_version', 'kind', 'records', 'fragments', 'source_identity',
                    'suggestion_source', 'state'))
    _require(type(value['schema_version']) is int and value['schema_version'] == SCHEMA_VERSION
             and value['kind'] == KIND, 'review_snapshot_version')
    records, fragments, state = value['records'], value['fragments'], value['state']
    _require(type(records) is list and all(type(row) is dict for row in records),
             'review_snapshot_records')
    for row in records:
        _require(type(row.get('resources', [])) is list
                 and all(type(resource) is dict for resource in row.get('resources', [])),
                 'review_snapshot_record_resources')
    _require(type(fragments) is list and len(fragments) <= 200
             and all(type(row) is dict and row.get('reviewed')
                     and type(row.get('text')) is str and row['text'].strip() for row in fragments),
             'review_snapshot_fragments')
    source = value['suggestion_source']
    _require(source is None or type(source) is str and 0 < len(source) <= 4096 and '\0' not in source,
             'review_snapshot_source_locator')
    _object(state, ('name', 'record_index', 'text_rows', 'text_index', 'parts', 'include',
        'lora_rows', 'recorded_rows', 'chosen_resources', 'suggestions', 'suggestion_index',
        'suggestion_goal', 'tab', 'disclosures', 'scrolls', 'tree_selection',
        'saved_text_status', 'saved_text_status_visible', 'suggestion_status',
        'reviewed_proposal', 'notes', 'exact_text', 'strength_text'))
    _text(state['name'], 32767)
    _index(state['record_index'], len(records))
    _require(type(state['text_rows']) is list and len(state['text_rows']) == len(fragments),
             'review_snapshot_text_rows')
    for row in state['text_rows']:
        _object(row, ('text', 'source', 'data', 'checked'))
        _text(row['text']); _text(row['source']); _text(row['data']); _bool(row['checked'])
    _index(state['text_index'], len(fragments))
    _object(state['parts'], PART_KINDS)
    for kind in PART_KINDS:
        row = state['parts'][kind]
        _object(row, ('text', 'checked'))
        _text(row['text'], 4096 if kind != 'other' else None); _bool(row['checked'])
    _object(state['include'], INCLUSIONS)
    for field in INCLUSIONS:
        _bool(state['include'][field])
    resources = records[state['record_index']].get('resources', []) if state['record_index'] >= 0 else []
    for name, roles in (('lora_rows', ('lora',)), ('recorded_rows', ('checkpoint', 'vae'))):
        rows = state[name]
        _require(type(rows) is list, 'review_snapshot_resource_rows')
        seen = set()
        for row in rows:
            _object(row, ('resource_index', 'checked'))
            index = row['resource_index']
            _require(type(index) is int and 0 <= index < len(resources) and index not in seen
                     and resources[index].get('role') in roles, 'review_snapshot_record_resource')
            seen.add(index); _bool(row['checked'])
            if name == 'lora_rows':
                _object(row, ('strengths',))
                _object(row['strengths'], ('model', 'text_encoder'))
                for strength in ('model', 'text_encoder'):
                    entry = row['strengths'][strength]
                    _object(entry, ('checked', 'value'))
                    _bool(entry['checked']); _weight(entry['value'])
    _require(type(state['chosen_resources']) is list, 'review_snapshot_chosen_resources')
    for row in state['chosen_resources']:
        _object(row, ('resource', 'checked', 'weights'))
        resource = row['resource']
        _object(resource, ('id', 'role', 'name'))
        _text(resource['id']); _text(resource['name'])
        if 'display_label' in resource:
            _text(resource['display_label'])
        _require(resource['role'] in ('checkpoint', 'lora', 'vae'), 'review_snapshot_resource_role')
        _bool(row['checked']); _require(type(row['weights']) is dict, 'review_snapshot_weights')
        if resource['role'] == 'lora':
            _object(resource, ('weights',))
            _require(type(resource['weights']) is dict, 'review_snapshot_weights')
            _object(row['weights'], ('model', 'text_encoder'))
            for key in ('model', 'text_encoder'):
                _weight(row['weights'][key])
    _require(type(state['suggestions']) is list and len(state['suggestions']) <= 32,
             'review_snapshot_suggestions')
    for row in state['suggestions']:
        _object(row, ('kind', 'target', 'text', 'source_label', 'model_id'))
        _require(row['kind'] in PART_KINDS and row['target'] in PART_KINDS,
                 'review_snapshot_suggestion_target')
        for key in ('text', 'source_label', 'model_id'):
            _text(row[key])
        if 'choice_label' in row:
            _text(row['choice_label'])
    _index(state['suggestion_index'], len(state['suggestions']))
    goal = state['suggestion_goal']
    _require(type(goal) is list and 0 < len(goal) <= len(PART_KINDS)
             and all(type(kind) is str and kind in PART_KINDS for kind in goal)
             and len(set(goal)) == len(goal), 'review_snapshot_suggestion_goal')
    _require(type(state['tab']) is int and 0 <= state['tab'] <= 2, 'review_snapshot_tab')
    _object(state['disclosures'], DISCLOSURES)
    for key in DISCLOSURES:
        _bool(state['disclosures'][key])
    _require(type(state['scrolls']) is list and len(state['scrolls']) == 3
             and all(type(item) is int and 0 <= item <= 2**31 - 1 for item in state['scrolls']),
             'review_snapshot_scrolls')
    _object(state['tree_selection'], ('lora', 'recorded', 'chosen'))
    for key, rows in (('lora', 'lora_rows'), ('recorded', 'recorded_rows'), ('chosen', 'chosen_resources')):
        _index(state['tree_selection'][key], len(state[rows]))
    _bool(state['saved_text_status_visible'])
    for key in ('strength_visible', 'exact_toggle_visible'):
        if key in state:
            _bool(state[key])
    for key in ('saved_text_status', 'suggestion_status', 'exact_text', 'strength_text'):
        _text(state[key])
    _require(state['reviewed_proposal'] is None or type(state['reviewed_proposal']) is dict,
             'review_snapshot_proposal')
    _require(type(state['notes']) is list, 'review_snapshot_notes')
    return deepcopy(value)
