from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from threading import RLock, get_ident
from typing import Any
import time
from .video_decoder import VideoDecoderLifetime

from .federated_dataset import FederatedDatasetManager
from .events import EventBus
from .ai_interrogation import AIInterrogationService,configured_runtime_specs
from .ai_runtime_residency import LocalAIRuntimeManager
from .edit_service import MoveHistoryService, TagEditService
from .recycle_bin import RecycleBinService
from .duplicate_finder import (
    DuplicateDecisionStore,DuplicateFinderService,DuplicateIndex,
)
from .paths import (
    CONSUMER_POLICIES_PATH,DEFAULTS_DIR,RELATIONSHIPS_PATH,SETUP_OPERATION_QUEUE_PATH,
    TAGS_PATH,TASKS_PATH,USER_DATA_DIR,
)
from .review_queue import ReviewTaskDatabase
from .settings import AppSettings
from .tag_database import TagDatabase
from .automation_rule_conversion import RulePairAdmission
from .background_jobs import BackgroundJobManager
from .derived_refresh import DerivedRefreshCoordinator
from .setup_operation_queue import SetupOperationQueue
from .evidence_query import EvidenceQueryService
from .ai_runtime_recovery import AIWorkLease
from .default_sync import sync_promoted_defaults
from .category_registry import CategoryRegistry
from .paths import AI_WORK_LEASE_PATH,APP_ROOT,CATEGORIES_PATH
from .paths import CACHE_DIR
from .dependencies import DependencyService,VideoRuntimeBinding


_OBSERVATION_COUNTER_KEYS=frozenset({"seen_count","first_seen","last_seen"})


def _without_observation_counters(value):
    """Return a canonical comparison value without non-semantic observations."""
    if isinstance(value,dict):
        return {
            str(key):_without_observation_counters(item)
            for key,item in value.items()
            if str(key) not in _OBSERVATION_COUNTER_KEYS
        }
    if isinstance(value,(list,tuple)):
        return [_without_observation_counters(item) for item in value]
    return value


def _persistent_semantic_authority_snapshot(
    knowledge: TagDatabase,categories: CategoryRegistry,*,workspace=None,
) -> str | None:
    """Capture effective reload authority, failing closed when it cannot be read."""
    try:
        tags=[]
        for record in sorted(knowledge.tags.values(),key=lambda item:str(item.id)):
            payload=record.to_dict()
            relations=dict(payload.get("relations") or {})
            parent,implies=knowledge.effective_semantic_ids(record)
            payload["aliases"]=list(knowledge.effective_aliases(record))
            relations["parent"]=parent or None
            relations["implies"]=list(implies)
            payload["relations"]=relations
            tags.append(_without_observation_counters(payload))
        category_values=[]
        for category_id,record in sorted(categories.categories.items()):
            payload=record.to_dict(include_category_base_projection=False)
            payload["effective_base"]=categories.effective_base_tag(category_id)
            payload["effective_ordering"]=categories.effective_ordering(category_id)
            payload["effective_role"]=categories.effective_semantic_role(category_id)
            category_values.append(_without_observation_counters(payload))
        merge_rules=[
            _without_observation_counters(rule.to_dict())
            for _rule_id,rule in sorted(knowledge.merge_rules.items())
        ]
        relationships=(
            [
                _without_observation_counters(relationship.to_dict())
                for relationship in sorted(
                    workspace.effective,key=lambda item:str(item.id),
                )
            ]
            if workspace is not None else None
        )
        payload={
            "tags":tags,
            "categories":category_values,
            "merge_rules":merge_rules,
            "relationships":relationships,
            "removed_tag_ids":sorted(knowledge.removed_tag_ids),
            "merged_tag_ids":sorted(knowledge.merged_tag_ids.items()),
        }
        return json.dumps(payload,sort_keys=True,separators=(",",":"),ensure_ascii=False)
    except (AttributeError,TypeError,ValueError):
        # A reload must never turn an unavailable comparison into permission to
        # retain dependent projections.
        return None


def _vocabulary_refresh_dependency(knowledge,index,names,before,after) -> dict:
    """Scope a verified vocabulary reload using cached evidence, never image reads.

    Empty input (global ignores), unrelated authority changes, or unavailable
    index coverage retain the conservative recovery event.
    """
    from .tag_collection import normalize_tag
    from .tag_record_save import _valid_impact_token
    try:
        names=tuple(dict.fromkeys(normalize_tag(name) for name in names if name))
        if not names or before is None or after is None:return {}
        records=[knowledge.find(name) for name in names]
        if any(record is None for record in records):return {}
        tag_ids=tuple(dict.fromkeys(record.id for record in records))
        old,new=json.loads(before),json.loads(after)
        identity_names=set(names)
        for snapshot in (old,new):
            for record in snapshot["tags"]:
                if record.get("id") in tag_ids:
                    identity_names.update(
                        normalize_tag(value) for value in
                        (record.get("display", ""),*record.get("aliases",())) if value
                    )
            snapshot["tags"]=[
                record for record in snapshot["tags"]
                if record.get("id") not in tag_ids
            ]
        if old!=new:return {}
        token=index.evidence_snapshot_token()
        if not _valid_impact_token(token):return {}
        # This indexed query must prove its coverage. Do not fall back to
        # materializing every saved image record on the interface thread.
        result=index.query_evidence_groups((tuple(sorted(identity_names)),),False)
        if result is None or token!=index.evidence_snapshot_token():return {}
        paths,_coverage=result
        return {"dependency_kind":"tag_identity","complete":True,
                "tag_ids":tag_ids,"images":tuple(paths)}
    except (AttributeError,KeyError,OSError,RuntimeError,TypeError,ValueError):
        return {}


def _configure_alias_authority(knowledge: TagDatabase,workspace=None) -> object:
    """Publish canonical Other Name ownership to application-wide lookups."""
    from .tag_alias_relationships import TagAliasRelationshipService
    return TagAliasRelationshipService(
        knowledge,default_path=DEFAULTS_DIR/"relationships.json",
        user_path=RELATIONSHIPS_PATH,workspace=workspace,
    )


def _configure_semantic_authority(knowledge: TagDatabase,workspace=None) -> object:
    """Publish canonical Broader Tag and Also Means application-wide."""
    from .tag_semantic_relationships import TagSemanticRelationshipService
    return TagSemanticRelationshipService(
        knowledge,default_path=DEFAULTS_DIR/"relationships.json",
        user_path=RELATIONSHIPS_PATH,workspace=workspace,
    )


def _configure_category_base_authority(
    categories: CategoryRegistry,
    knowledge: TagDatabase,
    workspace=None,
) -> None:
    """Publish canonical Category Base organization to inherited consumers."""
    from .relationship_workspace import RelationshipWorkspaceService

    workspace=workspace or RelationshipWorkspaceService(
        DEFAULTS_DIR/"relationships.json",RELATIONSHIPS_PATH,tags=knowledge,
    )
    categories.configure_category_base_authority(workspace.effective)


def _bind_default_identity_authority(knowledge, *, expected_revision=None):
    """Explicit host read of the exact admitted native Tags/tracking pair."""
    from .default_tag_identity import (
        DefaultTagIdentityHold, _payload, admit_default_tag_identity, pair_revision,
    )
    from .knowledge_write_admission import _read, MAX_PARTICIPANT_BYTES
    from .default_sync import load_effective_tag_defaults
    from .relationship_store import RelationshipStore
    binding=getattr(knowledge,"rule_pair_admission",None)
    if binding is None:
        return None
    if not binding.owns(knowledge.path,"knowledge"):
        raise DefaultTagIdentityHold("Default identity host must use its exact admitted Tags participant")
    knowledge.invalidate_default_identity_context()
    binding.assert_clear()
    tags_path=binding.knowledge_path
    tracking_path=binding.root/"default_knowledge_state.json"
    tags_raw=_read(tags_path,MAX_PARTICIPANT_BYTES)
    tracking_raw=_read(tracking_path,MAX_PARTICIPANT_BYTES)
    revision=pair_revision(tags_raw,tracking_raw,str(tags_path.absolute()),str(tracking_path.absolute()))
    if expected_revision is not None and revision!=expected_revision:
        knowledge.invalidate_default_identity_context()
        raise DefaultTagIdentityHold("Native Tags/tracking pair changed during dependent index refresh")
    state=_payload(tracking_raw)
    tracked=state.get("records",{}).get("tags",{})
    defaults=load_effective_tag_defaults(DEFAULTS_DIR)
    required={row["id"] for row in defaults.tags}
    required.update(defaults.merged_tag_ids)
    required.update(defaults.merged_tag_ids.values())
    required.update(target for target in knowledge.merged_tag_ids.values() if target in tracked)
    for path in (DEFAULTS_DIR/"relationships.json",RELATIONSHIPS_PATH):
        for row in RelationshipStore(path).relationships.values():
            required.update(endpoint.id for endpoint in row.endpoints
                if endpoint.kind=="tag" and endpoint.id in tracked)
    equivalent={}
    for identity in required:
        equivalent.setdefault(defaults.canonical_id(identity),set()).add(identity)
    context=admit_default_tag_identity(tags_raw,tracking_raw,
        tags_identity=str(tags_path.absolute()),tracking_identity=str(tracking_path.absolute()),
        required_ids=required,generation=knowledge.default_identity_generation,
        equivalent_ids=equivalent.values())
    knowledge.bind_default_identity_context(context,pair_revision=revision)
    return revision


def _require_default_identity_authority(knowledge, revision):
    """Check the admitted pair around a mapped host batch; never resolve with IO."""
    if revision is None:
        return
    from .default_tag_identity import pair_revision
    from .knowledge_write_admission import _read, MAX_PARTICIPANT_BYTES
    binding=knowledge.rule_pair_admission
    binding.assert_clear()
    tags_path=binding.knowledge_path;tracking_path=binding.root/"default_knowledge_state.json"
    current=pair_revision(_read(tags_path,MAX_PARTICIPANT_BYTES),
        _read(tracking_path,MAX_PARTICIPANT_BYTES),str(tags_path.absolute()),str(tracking_path.absolute()))
    knowledge.require_default_identity_revision(current)
    if current!=revision:
        # require_default_identity_revision already holds on a changed pair.
        raise RuntimeError("Default identity host pair revision changed")


def _discard_default_identity_authority(knowledge,categories=None):
    """Discard every mapped host projection after a failed bound batch."""
    if getattr(knowledge,"rule_pair_admission",None) is None:
        return
    knowledge.invalidate_default_identity_context()
    knowledge.clear_alias_authority()
    knowledge.clear_semantic_authority()
    if categories is not None:
        categories.configure_category_base_authority(())
    knowledge._app_relationship_workspace=None
    knowledge._app_tag_alias_service=None
    knowledge._app_tag_semantic_service=None
    # Priming publishes this module-level cache before the final pair check.
    # Its file/legacy signature does not carry the admitted tracking revision.
    from . import relationship_evaluator
    relationship_evaluator._ACTIVE_EVALUATOR_CACHE=None


def _refresh_knowledge_relationship_authority(knowledge,categories):
    """Build dependent authority only inside one explicitly checked native pair."""
    from .relationship_workspace import RelationshipWorkspaceService
    revision=None
    try:
        revision=_bind_default_identity_authority(knowledge)
        _require_default_identity_authority(knowledge,revision)
        workspace=RelationshipWorkspaceService(
            DEFAULTS_DIR/"relationships.json",RELATIONSHIPS_PATH,tags=knowledge,
        )
        _configure_category_base_authority(categories,knowledge,workspace)
        _require_default_identity_authority(knowledge,revision)
        knowledge.apply_category_ordering(categories)
        _bind_default_identity_authority(knowledge,expected_revision=revision)
        _require_default_identity_authority(knowledge,revision)
        alias_service=_configure_alias_authority(knowledge,workspace)
        _require_default_identity_authority(knowledge,revision)
        semantic_service=_configure_semantic_authority(knowledge,workspace)
        _require_default_identity_authority(knowledge,revision)
        if revision is not None:
            # A failed batch discards the global cache. Re-prime from this
            # canonical workspace rather than the raw-default lazy fallback.
            from .relationship_evaluator import prime_active_relationship_evaluator
            prime_active_relationship_evaluator(knowledge,workspace.effective)
            _require_default_identity_authority(knowledge,revision)
    except Exception:
        _discard_default_identity_authority(knowledge,categories)
        raise
    knowledge._app_relationship_workspace=workspace
    knowledge._app_tag_alias_service=alias_service
    knowledge._app_tag_semantic_service=semantic_service
    return workspace


def _load_knowledge() -> TagDatabase:
    # Recovery and default synchronization can publish tags.json directly.
    # Hold the canonical pair before either runs, even before TagDatabase exists.
    admission = RulePairAdmission(USER_DATA_DIR)
    with admission.admit():
        return _load_knowledge_admitted(admission)


def _load_knowledge_admitted(admission: RulePairAdmission) -> TagDatabase:
    from .allowed_dimension_policy import AllowedDimensionPolicyService
    from .consumer_policies import recover_interrupted_consumer_policy_writes
    from .default_overrides import recover_interrupted_default_override_restores
    from .knowledge_pack import recover_interrupted_knowledge_pack_imports
    from .tag_identity_merge import TagIdentityMergeService
    from .modifier_composition import ModifierCompositionService
    from .tag_semantic_relationships import TagSemanticRelationshipService
    from .tag_alias_relationships import TagAliasRelationshipService
    from .relationship_workspace import RelationshipWorkspaceService
    from .tag_order_workspace import CategoryWorkspaceSession
    from .tag_template_organization_apply import TagTemplateOrganizationApplyService
    from .category_retirement import preflight_category_retirements
    # A newer native retirement may be partially published. Other recovery or
    # default writers must not alter its participants before explicit review.
    preflight_category_retirements(USER_DATA_DIR)
    recovery_root = APP_ROOT
    # Native synthetic startup has already admitted this exact root/data layout.
    # Component fixtures and ordinary deployment recovery retain their owner.
    if (USER_DATA_DIR.parent.name == "data"
            and USER_DATA_DIR.parent.parent != APP_ROOT
            and os.environ.get("TAG_MANAGER_TEST_OWNED_ROOT") == str(USER_DATA_DIR.parent)):
        from .startup_profile import startup_write_root
        recovery_root = startup_write_root(APP_ROOT)
    recover_interrupted_knowledge_pack_imports(recovery_root)
    recover_interrupted_consumer_policy_writes(
        USER_DATA_DIR / "backups" / "consumer-policy",
        allowed_targets=(CONSUMER_POLICIES_PATH,),
    )
    recover_interrupted_default_override_restores(USER_DATA_DIR)
    TagIdentityMergeService.recover_interrupted(USER_DATA_DIR)
    CategoryWorkspaceSession.recover_interrupted(USER_DATA_DIR)
    TagTemplateOrganizationApplyService(
        tags_path=TAGS_PATH,
        rule_pair_admission=admission,
        category_default_path=DEFAULTS_DIR/"categories.json",
        category_user_path=CATEGORIES_PATH,
        modifier_path=DEFAULTS_DIR/"modifiers.json",
        concept_path=DEFAULTS_DIR/"concepts.json",
        default_relationship_path=DEFAULTS_DIR/"relationships.json",
        user_relationship_path=RELATIONSHIPS_PATH,
        journal_path=USER_DATA_DIR/"operation_journal.jsonl",
    ).recover_interrupted()
    RelationshipWorkspaceService.recover_interrupted_constraint_transactions(
        USER_DATA_DIR
    )
    ModifierCompositionService.recover_interrupted(USER_DATA_DIR)
    TagSemanticRelationshipService.recover_interrupted(USER_DATA_DIR)
    TagAliasRelationshipService.recover_interrupted(USER_DATA_DIR)
    AllowedDimensionPolicyService.recover_interrupted(USER_DATA_DIR)
    sync_promoted_defaults(DEFAULTS_DIR,USER_DATA_DIR)
    from .character_tag_bridge import CharacterTagBridge
    from .character_record_edit import CharacterRecordEditService
    CharacterTagBridge.recover_interrupted(USER_DATA_DIR)
    CharacterRecordEditService.recover_interrupted(USER_DATA_DIR)
    knowledge=TagDatabase(TAGS_PATH, rule_pair_admission=admission)
    categories=CategoryRegistry(DEFAULTS_DIR/"categories.json",CATEGORIES_PATH)
    try:
        workspace=_refresh_knowledge_relationship_authority(knowledge,categories)
    except Exception:
        _discard_default_identity_authority(knowledge,categories)
        raise
    return knowledge


@dataclass
class AppState:
    settings: AppSettings = field(default_factory=AppSettings.load)
    dependency_service_factory: Any = field(default=None,repr=False,kw_only=True)
    history_configuration: Any = field(init=False, repr=False)
    dataset: FederatedDatasetManager = field(init=False)
    events: EventBus = field(default_factory=EventBus)
    knowledge: TagDatabase = field(default_factory=_load_knowledge)
    reviews: ReviewTaskDatabase = field(default_factory=lambda: ReviewTaskDatabase(TASKS_PATH))
    edits: TagEditService = field(
        default_factory=lambda: TagEditService(USER_DATA_DIR / "change_history.json")
    )
    moves: MoveHistoryService = field(
        default_factory=lambda: MoveHistoryService(USER_DATA_DIR / "move_history.json", recover=False)
    )
    recycle: RecycleBinService = field(init=False)
    duplicates: DuplicateFinderService = field(init=False)
    ai: AIInterrogationService = field(default_factory=AIInterrogationService)
    jobs: BackgroundJobManager = field(default_factory=BackgroundJobManager)
    dataset_preparation_work: dict = field(default_factory=dict,repr=False)
    ingest_preparation_work: dict = field(default_factory=dict,repr=False)
    preparation_requests: Any = field(init=False,repr=False)
    ai_runtimes: LocalAIRuntimeManager = field(init=False,repr=False)
    derived_refresh: DerivedRefreshCoordinator = field(init=False,repr=False)
    setup_queue: SetupOperationQueue = field(init=False,repr=False)
    video_decoder_lifetime: Any = field(default_factory=VideoDecoderLifetime,init=False,repr=False)
    video_runtime_binding: Any = field(default=None,init=False,repr=False)
    video_component_hold: str = field(default="",init=False)
    video_recovery_required: bool = field(default=False,init=False,repr=False)
    evidence_queries: EvidenceQueryService = field(default_factory=EvidenceQueryService)
    ai_work_lease: AIWorkLease = field(
        default_factory=lambda: AIWorkLease(AI_WORK_LEASE_PATH,APP_ROOT)
    )
    selected_image: str = ""
    selected_library_id: str = ""
    selected_relative_image_path: str = ""
    busy: bool = False
    status: str = 'Ready'
    _operation_gate: dict[str,str] = field(default_factory=dict,init=False,repr=False)
    _ai_lease_owners: set[str] = field(default_factory=set,init=False,repr=False)
    _derived_yield_owners: set[str] = field(default_factory=set,init=False,repr=False)
    _operation_lock: RLock = field(default_factory=RLock,init=False,repr=False)
    _knowledge_authority_thread: int = field(default_factory=get_ident,init=False,repr=False)
    _knowledge_authority_thread_bound: bool = field(default=False,init=False,repr=False)

    def __post_init__(self) -> None:
        if self.dependency_service_factory is not None and not callable(self.dependency_service_factory):
            raise ValueError("The dependency service factory must be callable.")
        from .preparation_requests import PreparationRequestCoordinator
        self.preparation_requests = PreparationRequestCoordinator(
            self.jobs, ai_work_lease=self.ai_work_lease,
        )
        from .history_configuration import HistoryConfigurationService
        self.history_configuration = HistoryConfigurationService(self.settings)
        from .history_moves import HistoryMoveCoordinator
        self.moves.history_coordinator = HistoryMoveCoordinator(self.history_configuration)
        self.moves.recover_pending()
        # Construction deliberately has no model or hardware discovery.  The
        # manager resolves its fallback budget only on first worker work.
        self.ai_runtimes=LocalAIRuntimeManager(self.jobs,self.events)
        self.ai.runtime_manager=self.ai_runtimes
        from .inference_timing import InferenceTimingStore
        self.ai.timing_store=InferenceTimingStore(USER_DATA_DIR/"inference_timings.sqlite3")
        self.jobs.set_ai_runtime_reclaimer(self._release_ai_for_exclusive_work)
        self.configure_ai_runtimes()
        self.dataset=FederatedDatasetManager(
            self.settings.library_registry(),
            preferred_library_id=self.settings.active_library_id,
            scope_library_ids=self.settings.library_scope_ids,
            knowledge=self.knowledge,
        )
        self.edits.knowledge=self.knowledge
        self.recycle=RecycleBinService(
            self.settings,self.moves,USER_DATA_DIR / "recycle_records",
        )
        self.duplicates=DuplicateFinderService(
            DuplicateIndex(CACHE_DIR / "duplicate_fingerprints.sqlite3"),
            DuplicateDecisionStore(USER_DATA_DIR / "duplicate_decisions.json"),
        )
        from .trainer_setup_executor import TrainerSetupExecutor
        from .dependencies import OPTIONAL_COMPONENT_ROOT
        trainer_setup=TrainerSetupExecutor(self.jobs,OPTIONAL_COMPONENT_ROOT/'trainers'/'sdxl',
            profile_path=APP_ROOT/'tools/trainer_runtime_profile.json',
            catalog_path=APP_ROOT/'tools/trainer_runtime_catalog.json',
            source_root=APP_ROOT/'tools/trainer-sources/sd-scripts')
        self.setup_queue=SetupOperationQueue(
            self.settings,self.jobs,self.events,SETUP_OPERATION_QUEUE_PATH,
            trainer_executor=trainer_setup,
            dependency_service_factory=self.dependency_service_factory,
            video_recovery_needed=lambda:bool(self.video_component_hold or self.video_recovery_required),
            before_component_mutation=self._before_component_mutation,
            after_component_mutation=self._after_component_mutation,
        )
        self.derived_refresh=DerivedRefreshCoordinator(
            self.events,self.jobs,self.dataset,settings=self.settings,
            reviews=self.reviews,
        )

    def dependency_service(self):
        factory=self.dependency_service_factory if self.dependency_service_factory is not None else DependencyService
        return factory(self.settings)

    def admit_video_runtime(self,*,check_interrupt=None):
        """Worker-side fresh binding for an explicit decoder/preview entry.

        Callers pass the result and this state's exact video_decoder_lifetime;
        old dialogs never silently receive a replacement binding.
        """
        self.video_runtime_binding=None
        if check_interrupt is not None:check_interrupt()
        if self.video_component_hold:
            raise RuntimeError(self.video_component_hold)
        service=self.dependency_service()
        if service.capability_profile=="collectigen-core":
            return None  # Original full-runtime decoder route remains unchanged.
        binding=service.admit_video_runtime(check_interrupt=check_interrupt)
        if type(binding) is not VideoRuntimeBinding:
            raise RuntimeError("Video requires the immutable admitted runtime binding.")
        if check_interrupt is not None:check_interrupt()
        if self.video_component_hold:
            self.video_runtime_binding=None
            raise RuntimeError(self.video_component_hold)
        self.video_runtime_binding=binding
        self._video_last_admitted_binding=binding
        return binding

    def configure_ai_runtimes(self) -> None:
        """Apply saved residency preferences without probing the machine."""
        signature=tuple(str(getattr(self.settings,key,"")) for key in (
            "ai_runtime_mode","ai_models_directory","enabled_wd_taggers",
            "suggest_wd_for_missing_tags","suggest_joycaption_for_missing_descriptions",
            "auto_run_wd_on_managed_images","auto_run_joy_on_managed_images",
            "plain_language_model","wd_execution_provider","joycaption_gpu_index",
            "joycaption_quantization","ai_runtime_memory_budget_mb",
        ))
        if signature!=getattr(self,"_ai_runtime_configuration",None):
            self._ai_preload_pending=getattr(self.settings,"ai_runtime_mode","on_demand")=="preload"
            self._ai_runtime_configuration=signature
        self.ai_runtimes.configure(self.settings)

    def _release_ai_for_exclusive_work(self) -> None:
        self.ai_runtimes.release_all(
            "another GPU workflow needs resources",wait=True,
        )

    def prepare_ai_exclusive_work(self) -> bool:
        """Worker-only gate for native GPU work outside ``jobs.submit``."""
        return self.jobs.prepare_ai_exclusive_work()

    def _before_component_mutation(self,_operation) -> None:
        if getattr(_operation,"kind","") in {"setup_video","setup_wd","setup_joy","setup_application"}:
            self.video_component_hold="Waiting for Video decoder owners to drain."
            self._video_pre_mutation_binding=self.video_runtime_binding or getattr(self,"_video_last_admitted_binding",None)
            self.video_recovery_required=(getattr(self,"video_recovery_required",False)
                or self._video_pre_mutation_binding is not None or _operation.kind=="setup_video")
            ready=self.video_decoder_lifetime.begin_mutation()
            deadline=time.monotonic()+30
            while not ready:
                job=self.jobs.get(_operation.job_id)
                if self.jobs.shutting_down or (job is not None and job.cancel_requested):
                    raise InterruptedError("Video setup stopped while retaining its decoder hold.")
                if time.monotonic()>=deadline:
                    raise RuntimeError("Video decoder owners have not drained. Setup is held; retry after they stop.")
                time.sleep(.01)
                ready=self.video_decoder_lifetime.drained
        self._release_ai_for_exclusive_work()

    def _after_component_mutation(self,_operation) -> None:
        video_affected=getattr(_operation,"kind","") in {"setup_video","setup_wd","setup_joy","setup_application"}
        if video_affected:
            self.video_runtime_binding=None
            self.video_component_hold="Checking Video component recovery after attempted mutation."
        self.ai_runtimes.invalidate(reason="local AI components changed")
        self._ai_preload_pending=getattr(self.settings,"ai_runtime_mode","on_demand")=="preload"
        if video_affected:
            try:
                service=self.dependency_service()
                issue=service.optional_profile_runtime_issue()
                if issue:raise RuntimeError(issue)
                binding=None
                if _operation.kind=="setup_video" or getattr(self,"video_recovery_required",False):
                    job=self.jobs.get(_operation.job_id)
                    def check_interrupt():
                        if self.jobs.shutting_down or (job is not None and not job.checkpoint()):
                            raise InterruptedError("Video recovery interrupted; its hold remains supervised.")
                    binding=service.admit_video_runtime(check_interrupt=check_interrupt)
                    if type(binding) is not VideoRuntimeBinding:raise RuntimeError("Video recovery requires its immutable native admission binding.")
                    if _operation.kind=="setup_video":
                        reviewed=SetupOperationQueue.video_plan_from_params(_operation.params)
                        current=SetupOperationQueue.video_plan_params(binding)
                        if any(current[name]!=value for name,value in SetupOperationQueue.video_plan_params(reviewed).items() if name!="inventory_sha256"):
                            raise RuntimeError("Video recovery identity differs from the confirmed plan. Review again.")
                if not self.video_decoder_lifetime.drained:
                    raise RuntimeError("Video decoder owners have not physically drained.")
                self.video_runtime_binding=binding
                if binding is not None:self._video_last_admitted_binding=binding
            except Exception as exc:
                self.video_component_hold=f"Video component recovery held: {exc}"
                raise
            self.video_decoder_lifetime.end_mutation()
            self.video_recovery_required=False
            self.video_component_hold=""

    def _ai_runtime_worker(self,job,*,preload: bool=False) -> tuple:
        if not self.ai_work_lease.begin("Local AI runtime maintenance"):
            raise RuntimeError("Another local AI operation is using the GPU.")
        try:
            if not job.checkpoint():
                raise InterruptedError()
            released=self.ai_runtimes.reap_expired()
            if not preload or self.ai_runtimes.mode!="preload":
                return (),released
            # Preload once per startup or changed load configuration, never on
            # every timer tick. Idle expiry and explicit release must stay useful.
            self._ai_preload_pending=False
            service=self.dependency_service()
            capabilities=[]
            if self.settings.suggest_wd_for_missing_tags or self.settings.auto_run_wd_on_managed_images:
                capabilities.append("wd")
            if self.settings.suggest_joycaption_for_missing_descriptions or self.settings.auto_run_joy_on_managed_images:
                capabilities.append("joy")
            specs=configured_runtime_specs(
                self.settings,capabilities=capabilities,
                readiness=lambda capability,_model_id,_model_dir:service.capability_ready(capability),
            )
            if not specs:
                return (),released
            statuses=self.ai_runtimes.preload(
                specs,progress=lambda current,total,message:job.update(current,total,message),
                should_cancel=lambda:not job.checkpoint(),
            )
            return statuses,released
        finally:
            self.ai_work_lease.finish()

    def maintain_ai_runtimes(self,*,allow_preload: bool=False):
        """Queue idle reaping/preload only after foreground work has settled."""
        if self.jobs.shutting_down or self.jobs.active():
            return None
        with self._operation_lock:
            if self._ai_lease_owners:
                return None
        preload=bool(allow_preload and getattr(self,"_ai_preload_pending",False))
        needs=getattr(self.ai_runtimes,"needs_maintenance",lambda:False)
        if not preload and not needs():
            return None
        return self.jobs.submit(
            "Maintain local AI runtimes",
            lambda job:self._ai_runtime_worker(job,preload=preload),
            resource="gpu",key="ai-runtime-maintenance",
        )

    def free_ai_memory(self):
        """Queue a bounded manual release; safe for a Qt-button callback."""
        if self.jobs.shutting_down:
            return False,"Background work is shutting down."
        self._ai_preload_pending=False
        def worker(job):
            if not self.ai_work_lease.begin("Release local AI runtime memory"):
                raise RuntimeError("Another local AI operation is using the GPU.")
            try:
                if not job.checkpoint():
                    raise InterruptedError()
                return self.ai_runtimes.release_all("manual release",wait=True)
            finally:
                self.ai_work_lease.finish()
        job=self.jobs.submit(
            "Release local AI runtime memory",worker,resource="gpu",
            key="ai-runtime-release",
        )
        return True,job.name

    def set_status(self, status: str, *, busy: bool | None = None) -> None:
        self.status = status
        if busy is not None:
            self.busy = busy
        self.events.publish('status_changed', status=self.status, busy=self.busy)

    def begin_long_operation(
        self,owner: str,label: str,*,resource: str="io",dedupe_key: str="",
        exclusive: bool=True,
    ) -> tuple[bool,str]:
        """Reserve visible work through the shared resource coordinator.

        Most consequential operations retain the conservative application-wide
        exclusive boundary. Passive startup validation and small foreground
        checks may opt into ordinary resource limits so a remembered library
        does not monopolize otherwise independent work.
        """
        with self._operation_lock:
            if owner in self._operation_gate:
                current=self.jobs.get(self._operation_gate[owner])
                return False,current.name if current is not None else str(label)
            job,started=self.jobs.try_acquire(
                label,resource=resource,key=f"visible:{dedupe_key or owner}",
                exclusive=bool(exclusive),
            )
            yielded=False
            coordinator=getattr(self,"derived_refresh",None)
            defer=getattr(coordinator,"defer_for_visible_work",None)
            if (
                not started and callable(defer)
                and any(defer(candidate.id,label=str(label)) for candidate in self.jobs.active())
            ):
                yielded=True
                job,started=self.jobs.try_acquire(
                    label,resource=resource,
                    key=f"visible:{dedupe_key or owner}",
                    exclusive=bool(exclusive),
                )
            acquire_readers=getattr(self.jobs,"try_acquire_after_readers",None)
            if not started and exclusive and callable(acquire_readers):
                job,started=acquire_readers(
                    label,resource=resource,key=f"visible:{dedupe_key or owner}",timeout=.1,
                )
            if not started:
                if yielded:
                    coordinator.resume_after_visible_work()
                return False,job.name
            self._operation_gate[owner]=job.id
            if yielded:
                owners=getattr(self,"_derived_yield_owners",None)
                if owners is None:
                    owners=set(); self._derived_yield_owners=owners
                owners.add(owner)
            if resource=="gpu" and self.ai_work_lease.begin(label):
                self._ai_lease_owners.add(owner)
        self.events.publish(
            "long_operation_changed", active=True, owner=owner, label=label,
            current=0, total=0, message="",resource=resource,job_id=job.id,
        )
        return True,""

    def update_long_operation(
        self,owner: str,current: int,total: int=0,message: str="",
    ) -> None:
        """Publish progress for the active app-wide operation."""
        with self._operation_lock:
            job_id=self._operation_gate.get(owner)
        if job_id is None:
            return
        self.jobs.update(job_id,current,total,message)
        job=self.jobs.get(job_id)
        if job is None:
            return
        self.events.publish(
            "long_operation_changed", active=True, owner=owner, label=job.name,
            current=max(0,int(current)), total=max(0,int(total)), message=str(message),
            resource=job.resource,job_id=job.id,
        )

    def finish_long_operation(self,owner: str) -> None:
        with self._operation_lock:
            job_id=self._operation_gate.pop(owner,None)
            leased=owner in self._ai_lease_owners
            self._ai_lease_owners.discard(owner)
            yielded=owner in getattr(self,"_derived_yield_owners",set())
            if yielded:
                self._derived_yield_owners.discard(owner)
        if job_id is not None:
            job=self.jobs.get(job_id); self.jobs.release(job_id)
            if leased: self.ai_work_lease.finish()
            self.events.publish(
                "long_operation_changed", active=False, owner=owner,
                label=job.name if job is not None else "",
                current=0, total=0, message="",
                resource=job.resource if job is not None else "io",job_id=job_id,
            )
        if yielded:
            coordinator=getattr(self,"derived_refresh",None)
            resume=getattr(coordinator,"resume_after_visible_work",None)
            if callable(resume):
                resume()

    def request_long_operation_cancel(self,owner: str) -> bool:
        """Publish cooperative cancellation through the same shared job state."""
        with self._operation_lock:
            job_id=self._operation_gate.get(owner)
            leased=owner in self._ai_lease_owners
        if job_id is None:
            return False
        if leased: self.ai_work_lease.request_stop()
        self.jobs.cancel(job_id)
        return True

    def shutdown_background_jobs(self,*,wait: bool=False,timeout: float=2.0) -> bool:
        """Stop coordinator-owned work after page-owned jobs have drained."""
        coordinator=getattr(self,"derived_refresh",None)
        if coordinator is not None:
            coordinator.stop()
        drained=self.jobs.shutdown(wait=wait,timeout=timeout)
        # Page shutdown calls this with wait=True after its owned workers have
        # drained.  A nonblocking close never waits from the Qt thread.
        self.ai_runtimes.shutdown(wait=bool(wait and drained))
        if wait and drained:
            from .metadata_sync import shutdown_metadata_readers
            # Explicit app exit cancels owned read sessions only; writes use
            # separate bounded calls and coordinator jobs have already drained.
            drained=shutdown_metadata_readers(timeout=timeout,force=True)
        return drained

    def select_image(self, image: str) -> None:
        self.selected_image = image
        identity=self.dataset.identity_for_path(image) if image else None
        self.selected_library_id=(identity.library_id if identity is not None else "")
        self.selected_relative_image_path=(
            identity.relative_path if identity is not None else ""
        )
        self.events.publish("image_selected",image=image)
        self.events.publish(
            "image_identity_selected",image=image,
            library_id=self.selected_library_id,
            relative_path=self.selected_relative_image_path,
        )

    def _bind_knowledge_authority_thread(self):
        """Seal the shared authority owner when the GUI adopts startup state."""
        from .category_retirement import CategoryRetirementHold
        with self._operation_lock:
            if self._knowledge_authority_thread_bound:
                if get_ident() != self._knowledge_authority_thread:
                    raise CategoryRetirementHold("Shared Knowledge already belongs to another application thread.")
                return
            if self._operation_gate or getattr(self, "busy", False):
                raise CategoryRetirementHold("Finish visible work before binding application Knowledge ownership.")
            self._knowledge_authority_thread = get_ident()
            self._knowledge_authority_thread_bound = True

    def _complete_category_retirement(self, candidate, *, owner: str):
        """Private owning-thread handoff; the connected UI route is not enabled."""
        from .category_retirement import CategoryRetirementHold
        from .category_retirement_handoff import (
            CategoryRetirementNotificationError, adopt_completed_retirement,
        )
        if not self._knowledge_authority_thread_bound or get_ident() != self._knowledge_authority_thread:
            raise CategoryRetirementHold("Adopt retirement on the application authority thread.")
        with self._operation_lock:
            job_id = self._operation_gate.get(owner)
            predecessor = getattr(candidate, "_retirement_predecessor", None)
            if (job_id is None or not self.jobs.holds_exclusive_visible_operation(job_id)
                    or predecessor is None or predecessor.live_tags is not self.knowledge):
                raise CategoryRetirementHold("Keep the exact exclusive visible operation through retirement adoption.")
            session, result = adopt_completed_retirement(candidate)
        # No reads/recovery/review reload, and no events during installation or
        # native exclusion. A listener failure must not look like a failed save.
        try:
            self.events.publish("knowledge_changed")
        except Exception as failure:
            raise CategoryRetirementNotificationError(session, result) from failure
        return session, result

    def refresh_persistent_data(self, *, vocabulary_names=None) -> None:
        """Reload files that may have changed through an external maintenance or import workflow."""
        from contextlib import nullcontext
        binding=getattr(self.knowledge,"rule_pair_admission",None)
        categories=getattr(self.knowledge,"_categories",None)
        categories=categories if isinstance(categories,CategoryRegistry) else None
        try:
            with (binding.admit() if binding is not None else nullcontext()):
                if categories is None:
                    categories=CategoryRegistry(DEFAULTS_DIR/"categories.json",CATEGORIES_PATH)
                workspace=getattr(self.knowledge,"_app_relationship_workspace",None)
                before=(
                    _persistent_semantic_authority_snapshot(self.knowledge,categories,workspace=workspace)
                    if workspace is not None else None
                )
                self.knowledge.load()
                categories.load()
                workspace=_refresh_knowledge_relationship_authority(self.knowledge,categories)
                context=getattr(self.knowledge,"_default_identity_context",None)
                revision=context.revision if context is not None else None
                _require_default_identity_authority(self.knowledge,revision)
                after=_persistent_semantic_authority_snapshot(
                    self.knowledge,categories,workspace=workspace,
                )
                dependency=(
                    _vocabulary_refresh_dependency(self.knowledge,getattr(self.dataset,"index",None),
                        vocabulary_names,before,after)
                    if vocabulary_names is not None else None
                )
                _require_default_identity_authority(self.knowledge,revision)
        except Exception:
            _discard_default_identity_authority(self.knowledge,categories)
            raise
        self.reviews.load()
        if vocabulary_names is not None:
            self.events.publish("knowledge_changed",**dependency)
        elif before is None or after is None or before!=after:
            self.events.publish("knowledge_changed")
        self.events.publish("reviews_changed")
