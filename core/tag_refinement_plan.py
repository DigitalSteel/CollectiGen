"""Pure reviewed-proposal planner used by the scoped active-image review. No database, image or UI operations."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .knowledge_relationships import ADVISORY_ASSOCIATION, IDENTITY_ALIAS, SPECIFICITY, KnowledgeRelationship
from .modifier_composition import EffectiveModifierComposition
from .tag_collection import normalize_tag

SOFTWARE_VERSION = '0.1.0-alpha.2'
PLAN_INTERFACE_VERSION = 1


@dataclass(frozen=True)
class RefinementChoice:
    requested_id: str
    target_id: str | None
    target_display: str | None
    reason: str
    relationship_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RefinementPlan:
    source_value: str
    source_id: str | None
    scope_ref: str
    decision: str
    before: tuple[str, ...]
    after: tuple[str, ...]
    choices: tuple[RefinementChoice, ...]
    holds: tuple[str, ...] = ()

    @property
    def changed(self):
        return self.before != self.after

    @property
    def undo_values(self):
        """Preview only; the existing image owner must validate/stage/save/undo."""
        return self.before


def _modifiers(composition, display=None):
    if composition is None or composition.compatibility_values:
        return None
    rows = composition.canonical
    if any(not r.is_set or not r.reviewed_at or not r.record_fingerprint
           or r.tag_id != composition.tag_id or not r.base_tag_id
           or display is not None and normalize_tag(r.tag_name) != normalize_tag(display) for r in rows):
        return None
    if len({r.base_tag_id for r in rows}) > 1:
        return None
    values = tuple(sorted((r.family_id, r.value_id) for r in rows))
    if len({family for family, _ in values}) != len(values):
        return None
    if set(composition.values) != {value for _, value in values}:
        return None
    return values


def plan_refinement(source_value: str, source_id: str | None, before: tuple[str, ...],
                    displays: Mapping[str, str], relationships: tuple[KnowledgeRelationship, ...],
                    compositions: tuple[EffectiveModifierComposition, ...], candidate_ids: tuple[str, ...],
                    *, scope_ref: str, decision='review', selected_id=None, reviewed=False,
                    retain_source=False, relationship_scope=None) -> RefinementPlan:
    """Caller supplies effective native authority and one exact image/evidence scope.

    No source text, token overlap, category or popularity establishes a subtype.
    ``keep`` preserves a confirmed fact or pending finding; ``skip`` defers it.
    Neither records rejection/absence. No persistence or application authority.
    """
    if (not isinstance(source_value, str) or not normalize_tag(source_value)
            or type(scope_ref) is not str or not scope_ref.strip()
            or source_id is not None and type(source_id) is not str
            or selected_id is not None and type(selected_id) is not str
            or type(reviewed) is not bool or type(retain_source) is not bool
            or relationship_scope is not None and type(relationship_scope) is not str
            or type(before) is not tuple or any(not isinstance(v, str) for v in before)
            or type(candidate_ids) is not tuple or any(type(v) is not str or not v for v in candidate_ids)
            or type(relationships) is not tuple or any(type(r) is not KnowledgeRelationship for r in relationships)
            or type(compositions) is not tuple or any(type(c) is not EffectiveModifierComposition for c in compositions)
            or decision not in {'review', 'keep', 'skip', 'add_specific', 'correct', 'alias'}):
        raise ValueError('invalid_refinement_input')
    if (len(displays) > 256 or len(relationships) > 1024 or len(compositions) > 256
            or len(candidate_ids) > 64 or len(before) > 512):
        raise ValueError('refinement_input_bound')
    if any(not isinstance(k, str) or not k or not isinstance(v, str) or not normalize_tag(v)
           for k, v in displays.items()):
        raise ValueError('invalid_vocabulary_projection')
    if len({c.tag_id for c in compositions}) != len(compositions):
        raise ValueError('duplicate_composition')
    by_id = {c.tag_id: c for c in compositions}
    active = tuple(r for r in relationships if r.reviewed is True and not r.disabled and r.version == 1
                   and r.scope in ('any', scope_ref, relationship_scope))
    alias_targets = {r.endpoints[1].id for r in active if r.kind == IDENTITY_ALIAS and r.directed
                     and len(r.endpoints) == 2 and r.endpoints[0].kind == 'alias_text'
                     and normalize_tag(r.endpoints[0].id) == normalize_tag(source_value)
                     and r.endpoints[1].kind == 'tag' and r.endpoints[1].id in displays}
    alias_conflict = source_id is not None and any(t != source_id for t in alias_targets)
    if not alias_conflict and source_id in displays and normalize_tag(displays[source_id]) == normalize_tag(source_value):
        alias_targets.add(source_id)
    # A saved stable identity can follow a display rename. Never merge another ID.
    if not alias_conflict and source_id in displays and decision == 'alias':
        alias_targets.add(source_id)
    alias_targets = set() if alias_conflict else {t for t in alias_targets if source_id is None or t == source_id}
    required = _modifiers(by_id.get(source_id), displays.get(source_id))
    # An empty native resolution does not prove that an original compound has
    # no meaningful details. Do not silently treat missing slots as no modifiers.
    if source_id not in by_id or not by_id[source_id].canonical:
        required = None
    bases = {r.base_tag_id for r in by_id[source_id].canonical} if source_id in by_id else set()
    bases.discard('')
    source_bases = {source_id} | (bases if len(bases) == 1 else set())
    choices = []
    for requested in dict.fromkeys(candidate_ids):
        links = tuple(sorted(r.id for r in active if r.directed and len(r.endpoints) == 2
            and all(e.kind == 'tag' for e in r.endpoints)
            and ((r.kind == ADVISORY_ASSOCIATION and r.endpoints[0].id in source_bases and r.endpoints[1].id == requested)
                 or (r.kind == SPECIFICITY and r.endpoints[0].id == requested and r.endpoints[1].id in source_bases))))
        target = None; reason = 'missing_candidate'
        if requested in displays:
            reason = 'unreviewed_refinement_link'
            if links:
                reason = 'unknown_source_modifiers'
                source_composition = by_id.get(source_id)
                if (retain_source and decision != 'correct' and source_id in displays
                        and source_composition is not None and not source_composition.canonical
                        and not source_composition.compatibility_values):
                    # Retain unknown details on the original. This establishes no
                    # absence of modifiers and never authorizes replacement.
                    target = requested; reason = 'explicit_detail_with_original_retained'
                elif required is not None:
                    matches = []
                    if _modifiers(by_id.get(requested), displays.get(requested)) == required:
                        matches.append(requested)
                    for candidate, composition in by_id.items():
                        if candidate == requested or candidate not in displays or not composition.canonical:
                            continue
                        if (_modifiers(composition, displays[candidate]) == required
                                and all(r.base_tag_id == requested for r in composition.canonical)):
                            matches.append(candidate)
                    reason = 'modifier_preserving_candidate_missing' if not matches else 'ambiguous_modifier_preserving_candidates'
                    if len(matches) == 1:
                        target = matches[0]; reason = 'existing_modifier_preserving_candidate'
        choices.append(RefinementChoice(requested, target, displays.get(target), reason, links))
    after = before; holds = []
    if decision == 'alias':
        if len(alias_targets) != 1:
            holds.append('identity_equivalence_not_established')
        else:
            target = next(iter(alias_targets))
            canonical = displays[target]
            if normalize_tag(source_value) != normalize_tag(canonical):
                has_canonical = any(normalize_tag(v) == normalize_tag(canonical) for v in before)
                values = []
                for value in before:
                    if normalize_tag(value) != normalize_tag(source_value):
                        values.append(value)
                    elif not has_canonical:
                        values.append(canonical); has_canonical = True
                after = tuple(values)
    elif decision in {'add_specific', 'correct'}:
        if not reviewed:
            holds.append('explicit_image_review_required')
        else:
            matches = [c for c in choices if c.requested_id == selected_id]
            if len(matches) != 1 or matches[0].target_id is None:
                holds.append(matches[0].reason if len(matches) == 1 else 'select_one_candidate')
            elif not any(normalize_tag(v) == normalize_tag(source_value) for v in before):
                holds.append('source_occurrence_missing')
            elif source_id not in displays:
                holds.append('source_identity_missing')
            elif matches[0].target_id == source_id:
                holds.append('source_is_target')
            else:
                target = matches[0].target_display
                values = tuple(v for v in before if decision != 'correct' or normalize_tag(v) != normalize_tag(source_value))
                # Keep all pre-existing image-local values/order; add only the reviewed target.
                after = values if any(normalize_tag(v) == normalize_tag(target) for v in values) else (*values, target)
    return RefinementPlan(source_value, source_id, scope_ref, decision, before, after, tuple(choices), tuple(holds))
