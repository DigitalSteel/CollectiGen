"""Native live-refinement read adapter; no writer or historical rule execution."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path

from .relation_rule_service import FrozenPayload, RuleAdmissionError


def detach_live_inputs(inputs):
    """Freeze the existing planner's complete typed input representation."""
    return dict(displays=inputs['displays'], candidate_ids=list(inputs['candidate_ids']),
        relationships=[row.to_dict() for row in inputs['relationships']],
        compositions=json.loads(json.dumps([asdict(row) for row in inputs['compositions']])))


@dataclass(frozen=True)
class NativeRefinementSnapshot:
    payload: FrozenPayload

    @property
    def fingerprint(self):
        return self.payload.fingerprint

    @property
    def occurrences(self):
        from .confirmed_tag_identity import ImageTagOccurrence
        return tuple(ImageTagOccurrence(row['owner_kind'],row['owner_id'],row['owner_group'],
            row['label'],tuple(row['values'])) for row in self.payload.to_value()['occurrences'])

    def inputs(self):
        from .knowledge_relationships import KnowledgeRelationship
        from .modifier_composition import EffectiveModifierComposition,ModifierCompositionResolution
        row=self.payload.to_value()['inputs']
        return dict(displays=row['displays'],candidate_ids=tuple(row['candidate_ids']),
            relationships=tuple(KnowledgeRelationship.from_dict(value) for value in row['relationships']),
            compositions=tuple(EffectiveModifierComposition(value['tag_id'],tuple(value['values']),
                tuple(ModifierCompositionResolution(**slot) for slot in value['canonical']),
                tuple(value['compatibility_values'])) for value in row['compositions']))

    def plan(self, occurrence, *, selected_id=None, reviewed=False):
        from .tag_refinement_plan import plan_refinement
        if occurrence not in self.occurrences:
            raise RuleAdmissionError('The exact occurrence is not in this native observation')
        row=self.payload.to_value()
        return plan_refinement(row['source_value'],row['source_id'],occurrence.values,**self.inputs(),
            scope_ref=occurrence.scope_ref,relationship_scope=occurrence.owner_kind,retain_source=True,
            decision='add_specific' if reviewed else 'review',selected_id=selected_id,reviewed=reviewed)


def _participant(path, *, expected_bytes=None, expected_digest=None):
    path=Path(path)
    raw=path.read_bytes() if path.exists() else None
    if expected_digest is not None:
        actual=sha256(raw).digest() if raw is not None else ()
        if actual!=expected_digest:
            raise RuleAdmissionError('Native participant changed; reload before refinement')
    elif raw!=expected_bytes:
        raise RuleAdmissionError('Native participant changed; reload before refinement')
    parent=path.parent.stat()
    return dict(path=str(path.absolute()),sha256=sha256(raw).hexdigest() if raw is not None else 'missing',
                parent_identity=[parent.st_dev,parent.st_ino])


def capture_live_refinement(knowledge,categories,semantic,policies,annotation,*,source_id,source_value,
                            extra_dependencies):
    """Observe explicitly supplied native participants and the exact current draft.

    No native object/store is constructed here. Caller repeats observation before
    staging; source/catalog writes and image Save remain with original owners.
    Legacy incarnation absence is explicit, never made into historical authority.
    """
    from .tag_database import TagDatabase
    from .category_registry import CategoryRegistry
    from .tag_semantic_relationships import TagSemanticRelationshipService
    from .consumer_policies import ConsumerPolicyResolver
    from .annotation import ImageAnnotation
    if not (isinstance(knowledge,TagDatabase) and isinstance(categories,CategoryRegistry)
            and isinstance(semantic,TagSemanticRelationshipService) and semantic.tags is knowledge
            and isinstance(policies,ConsumerPolicyResolver) and isinstance(annotation,ImageAnnotation)):
        raise RuleAdmissionError('Original native refinement participants are required')
    if (categories.user_path.parent.absolute()!=knowledge.path.parent.absolute()
            or semantic.workspace.user_store.path.parent.absolute()!=knowledge.path.parent.absolute()):
        raise RuleAdmissionError('Refinement participants belong to different native profiles')
    with semantic._admission():
        return _capture_admitted_live_refinement(knowledge,categories,semantic,policies,annotation,
            source_id=source_id,source_value=source_value,extra_dependencies=extra_dependencies)


def _capture_admitted_live_refinement(knowledge,categories,semantic,policies,annotation,*,source_id,source_value,
                                     extra_dependencies):
    from .confirmed_tag_identity import active_refinement_inputs,image_tag_occurrences
    semantic._verify_authority()
    participants={'tags':_participant(knowledge.path,expected_bytes=knowledge._loaded_bytes)}
    context=getattr(knowledge,'_default_identity_context',None)
    if context is not None:
        from .default_tag_identity import document_revision,pair_revision
        tracking=Path(context.tracking_identity)
        if (context.tags_identity!=str(knowledge.path.absolute())
                or tracking.parent.absolute()!=knowledge.path.parent.absolute()
                or context.tags_revision!=document_revision(knowledge._loaded_bytes)
                or context.generation!=knowledge.default_identity_generation):
            raise RuleAdmissionError('Native default identity participant binding changed')
        tracking_raw=tracking.read_bytes() if tracking.exists() else None
        if pair_revision(knowledge._loaded_bytes,tracking_raw,context.tags_identity,context.tracking_identity)!=context.revision:
            raise RuleAdmissionError('Native default identity pair changed; reload before refinement')
        participants['default_tracking']=_participant(tracking,expected_bytes=tracking_raw)
    for index,(path,digest) in enumerate(categories._loaded_source_fingerprints.items()):
        participants['categories.'+str(index)]=_participant(path,expected_digest=digest)
    for label,store in (('relationships.default',semantic.workspace.default_store),
                        ('relationships.user',semantic.workspace.user_store)):
        participants[label]=_participant(store.path,expected_bytes=store._loaded_bytes)
    for index,store in enumerate(policies.stores):
        participants['policy.'+str(index)]=_participant(store.path,expected_bytes=store._loaded_bytes)
    if policies.authority_error:
        raise RuleAdmissionError('Consumer policy authority is unavailable')
    graph=semantic.effective_relationships
    inputs=active_refinement_inputs(knowledge,categories,graph,source_id)
    occurrences=image_tag_occurrences(annotation,source_value)
    if not occurrences:
        raise RuleAdmissionError('No confirmed structured source occurrence is available')
    envelope=knowledge.lifecycle_envelope
    source_ref=envelope.active_reference(source_id) if envelope is not None else None
    if envelope is not None and source_ref is None:
        raise RuleAdmissionError('Source has no active native incarnation')
    category_envelope=categories.lifecycle_envelope
    if envelope is not None and category_envelope is not None:
        from .knowledge_lifecycle import validate_namespace_pair
        validate_namespace_pair(envelope,category_envelope)
    return NativeRefinementSnapshot(FrozenPayload.capture(dict(
        interface_version=1,mode='native_live_refinement',source_id=source_id,source_value=source_value,
        participants=participants,inputs=detach_live_inputs(inputs),
        records={tid:knowledge.tags[tid].to_dict() for tid in inputs['displays']},
        graph=[row.to_dict() for row in graph],policies=[row.to_dict() for row in policies.records],
        tag_lifecycle=json.loads(envelope.to_bytes()) if envelope is not None else None,
        source_incarnation=source_ref.payload() if source_ref is not None else None,
        lifecycle_status='enrolled' if envelope is not None else 'legacy_incarnation_unavailable',
        category_lifecycle=json.loads(category_envelope.to_bytes()) if category_envelope is not None else None,
        categories={cid:record.to_dict() for cid,record in categories.categories.items()},
        native_identity=dict(removed_ids=sorted(knowledge.removed_tag_ids),redirects=knowledge.merged_tag_ids,
            default_generation=knowledge.default_identity_generation,
            default_context_revision=getattr(getattr(knowledge,'_default_identity_context',None),'revision',None),
            default_context_mappings=dict(getattr(getattr(knowledge,'_default_identity_context',None),'mappings',{}))),
        image=annotation.to_dict(),occurrences=json.loads(json.dumps([asdict(row) for row in occurrences])),
        extra_dependencies=extra_dependencies)))
