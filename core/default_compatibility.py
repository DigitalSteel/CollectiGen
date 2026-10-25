"""Pure pinned native-default reconciliation. No writes or activation authority.

Native hosts must supply complete observations and independently pinned ownership,
closure and approved transition evidence. Compact EffectiveTag is not an input.
"""
from __future__ import annotations
from dataclasses import dataclass
from hashlib import sha256
import json

from .relation_rule_service import FrozenPayload, RuleAdmissionError, _parse_json
from .tag_collection import normalize_tag

INTERFACE_VERSION = 1
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_RECORDS = 100_000
ROLES = ('baseline', 'upstream', 'local', 'retained', 'ownership', 'migrations')


class CompatibilityAdmissionError(ValueError):
    pass


def _object(value, label):
    if type(value) is not dict:
        raise CompatibilityAdmissionError(label+' must be an object')
    return value


def _array(value, label):
    if type(value) is not list or len(value)>MAX_RECORDS:
        raise CompatibilityAdmissionError(label+' requires a bounded list')
    return value


def _text(value, label):
    if type(value) is not str or not value.strip():
        raise CompatibilityAdmissionError(label+' requires nonempty text')
    return value


@dataclass(frozen=True, order=True)
class RecordKey:
    kind: str
    id: str

    def __post_init__(self):
        _text(self.kind,'record kind');_text(self.id,'record ID')

    @property
    def token(self):
        return self.kind+':'+self.id

    @classmethod
    def read(cls, value):
        _object(value,'record key')
        return cls(value.get('kind'),value.get('id'))

    def to_dict(self):
        return dict(kind=self.kind,id=self.id)


@dataclass(frozen=True)
class NativeDefinition:
    key: RecordKey
    native: FrozenPayload
    dependencies: tuple[RecordKey, ...]
    wrapper: FrozenPayload

    @classmethod
    def read(cls, row):
        _object(row,'native definition');key=RecordKey.read(row)
        native=_object(row.get('native'),'complete native record')
        if native.get('id')!=key.id:
            raise CompatibilityAdmissionError('Native record identity disagrees with its key')
        dependencies=tuple(RecordKey.read(d) for d in _array(row.get('depends_on'),'dependency inventory'))
        if len(set(dependencies))!=len(dependencies):
            raise CompatibilityAdmissionError('Duplicate dependency references')
        if key.kind=='tag':
            _text(native.get('display'),'native Tag display')
            aliases=_array(native.get('aliases',[]),'native Tag aliases')
            for name in aliases:_text(name,'native alias')
            category=native.get('category','')
            if type(category) is not str:
                raise CompatibilityAdmissionError('Invalid native category reference')
            if category and RecordKey('category',category) not in dependencies:
                raise CompatibilityAdmissionError('Native category missing from dependency inventory')
        if key.kind=='category':
            parent=native.get('parent_id')
            if parent is not None:
                _text(parent,'native category parent')
                if RecordKey('category',parent) not in dependencies:
                    raise CompatibilityAdmissionError('Native category parent missing from dependency inventory')
        return cls(key,FrozenPayload.capture(native),dependencies,FrozenPayload.capture(row))


@dataclass(frozen=True)
class PinnedInput:
    role: str
    byte_sha256: str
    payload: FrozenPayload

    @classmethod
    def decode(cls, raw, *, role, expected_sha256):
        if role not in ROLES or type(raw) is not bytes or len(raw)>MAX_INPUT_BYTES:
            raise CompatibilityAdmissionError('Invalid role or bounded input bytes')
        if type(expected_sha256) is not str or sha256(raw).hexdigest()!=expected_sha256:
            raise CompatibilityAdmissionError('Separately pinned input hash mismatch')
        def pairs(rows):
            result={}
            for key,value in rows:
                if key in result:raise CompatibilityAdmissionError('Duplicate JSON field')
                result[key]=value
            return result
        try:
            doc=_object(_parse_json(raw,object_pairs_hook=pairs),'input')
            payload=FrozenPayload.capture(doc)
        except RuleAdmissionError as error:
            raise CompatibilityAdmissionError(str(error)) from error
        if type(doc.get('schema_revision')) is not int or doc['schema_revision']!=INTERFACE_VERSION or doc.get('role')!=role:
            raise CompatibilityAdmissionError('Unsupported input contract or role')
        if role in ROLES[:4]:
            if doc.get('completeness')!='complete':
                raise CompatibilityAdmissionError('Complete native inventory declaration required')
            _text(doc.get('pack_id'),'pack ID');_text(doc.get('release_fingerprint'),'release fingerprint')
            definitions=tuple(NativeDefinition.read(r) for r in _array(doc.get('records'),'native records'))
            if len({r.key for r in definitions})!=len(definitions):
                raise CompatibilityAdmissionError('Duplicate native record identities')
            lifecycle=_object(doc.get('lifecycle'),'native lifecycle')
            for field in ('removed','redirects','disabled_relation_ids'):_array(lifecycle.get(field),field)
            for key in lifecycle['removed']:RecordKey.read(key)
            seen=set()
            for redirect in lifecycle['redirects']:
                _object(redirect,'redirect')
                source=RecordKey.read(redirect.get('source'));RecordKey.read(redirect.get('target'))
                if source in seen:raise CompatibilityAdmissionError('Duplicate redirect source')
                seen.add(source)
            for identity in lifecycle['disabled_relation_ids']:_text(identity,'disabled relation ID')
        return cls(role,expected_sha256,payload)

    @property
    def definitions(self):
        return tuple(NativeDefinition.read(r) for r in self.payload.to_value()['records'])


@dataclass(frozen=True)
class Decision:
    key: RecordKey
    action: str
    explanation: str


@dataclass(frozen=True)
class ExceptionGroup:
    reason: str
    keys: tuple[RecordKey, ...]
    evidence: FrozenPayload


@dataclass(frozen=True)
class CompatibilityPlan:
    payload: FrozenPayload
    decisions: tuple[Decision, ...]
    exceptions: tuple[ExceptionGroup, ...]

    @property
    def seal(self):
        return self.payload.fingerprint

    @property
    def activation_authorized(self):
        return False

    @property
    def ready_for_native_review(self):
        return not self.exceptions

    @property
    def effective_records(self):
        return tuple(NativeDefinition.read(r) for r in self.payload.to_value()['effective_records'])

    def accepts_inputs(self, inputs):
        return self.payload.to_value()['input_pins']=={i.role:i.byte_sha256 for i in inputs}


def _life(doc):
    row=doc['lifecycle']
    return (set(RecordKey.read(k) for k in row['removed']),
            {RecordKey.read(r['source']):RecordKey.read(r['target']) for r in row['redirects']},
            set(row['disabled_relation_ids']) | {r['id'] for r in doc['records']
                if r['kind']=='relationship' and r['native'].get('disabled') is True})


def source_state_fingerprint(catalog, key):
    """Complete source state binding, including unknown lifecycle metadata."""
    doc=catalog.payload.to_value();records={r.key:r for r in catalog.definitions}
    return FrozenPayload.capture(dict(record=records[key].wrapper.to_value() if key in records else None,
        lifecycle=doc['lifecycle'],key=key.to_dict(),release_fingerprint=doc['release_fingerprint'])).fingerprint


def plan_compatibility(*, baseline, upstream, local, retained, ownership, migrations):
    inputs=(baseline,upstream,local,retained,ownership,migrations)
    if any(type(i) is not PinnedInput or i.role!=role for i,role in zip(inputs,ROLES)):
        raise CompatibilityAdmissionError('Six separately admitted native inputs required')
    docs={i.role:i.payload.to_value() for i in inputs};pins={i.role:i.byte_sha256 for i in inputs}
    if len({docs[r]['pack_id'] for r in ROLES[:4]})!=1:
        raise CompatibilityAdmissionError('Foreign pack lineage')
    for role in ('ownership','migrations'):
        if docs[role].get('input_pins')!={r:pins[r] for r in ROLES[:4]}:
            raise CompatibilityAdmissionError('Evidence belongs to different native inputs')
    if docs['upstream'].get('previous_release_fingerprint')!=docs['baseline']['release_fingerprint']:
        raise CompatibilityAdmissionError('Upstream does not continue the admitted baseline')
    catalogs={i.role:{r.key:r for r in i.definitions} for i in inputs[:4]}
    B,U,L,R=(catalogs[r] for r in ROLES[:4])
    O=docs['ownership'];M=docs['migrations']
    owned=set(RecordKey.read(k) for k in _array(O.get('stored_user_records'),'stored user ownership'))
    local_claim=set(RecordKey.read(k) for k in _array(docs['local'].get('stored_user_records'),'native stored user inventory'))
    if owned!=local_claim or not owned<=set(L):
        raise CompatibilityAdmissionError('Whole stored user inventory must match independent ownership')
    default_owned=set(RecordKey.read(k) for k in _array(O.get('default_owned_records'),'default ownership'))
    if owned & default_owned:
        raise CompatibilityAdmissionError('Contradictory record ownership')
    closure=_object(O.get('closure'),'native closure evidence')
    removed,redirects,disabled=_life(docs['local'])
    default_lifecycle=set(RecordKey.read(k) for k in _array(O.get('default_lifecycle_records'),'default lifecycle ownership'))
    base_removed,base_redirects,base_disabled=_life(docs['baseline'])
    for key in default_lifecycle:
        if not ((key in removed and key in base_removed) or
                (key in redirects and redirects[key]==base_redirects.get(key))):
            if not (key.kind=='relationship' and key.id in disabled and key.id in base_disabled):
                raise CompatibilityAdmissionError('Default lifecycle lacks exact baseline lineage')
    protected=owned | ((removed | set(redirects) | {RecordKey('relationship',i) for i in disabled})-default_lifecycle)
    # Stored relationships protect their referenced identities as well. A
    # default transition cannot silently retarget a user's native relationship.
    protected_dependencies={dep for key in owned if key.kind=='relationship'
                            for dep in L[key].dependencies}
    protected |= protected_dependencies
    decisions=[];groups=[];effective=dict(L);carry=dict(R);planned_redirects=dict(redirects)
    def issue(reason,keys,evidence=None):
        groups.append(ExceptionGroup(reason,tuple(sorted(set(keys))),FrozenPayload.capture(evidence or {})))
    # Validate every pinned lifecycle before assigning any user/default
    # precedence. Lifecycle-only identities are authority evidence too.
    contradictory_keys=set()
    observation_life={role:_life(docs[role]) for role in ROLES[:4]}
    for role,(retired,mapping,inactive_relations) in observation_life.items():
        inactive_keys={RecordKey('relationship',i) for i in inactive_relations}
        contradictions=(retired & set(mapping)) | (retired & inactive_keys) | (set(mapping) & inactive_keys)
        contradictory_keys |= contradictions
        for key in sorted(contradictions):
            issue('contradictory_native_lifecycle',(key,),dict(observation=role))
    retained_removed,retained_redirects,retained_disabled=_life(docs['retained'])
    for source,target in retained_redirects.items():
        if source in redirects and redirects[source]!=target:
            issue('retained_lifecycle_contradiction',(source,target,redirects[source]))
        if source in removed:
            issue('retained_lifecycle_contradiction',(source,target))
        planned_redirects.setdefault(source,target)
    for key in retained_removed:
        if key in redirects:issue('retained_lifecycle_contradiction',(key,redirects[key]))
    # Absence from the local lifecycle is not permission to reactivate prior
    # compatibility tombstones, redirects or disabled relationships.
    removed |= retained_removed
    disabled |= retained_disabled
    retained_authority=retained_removed | set(retained_redirects) | {RecordKey('relationship',i) for i in retained_disabled}
    protected |= retained_authority
    def qualified(role,definition):
        witness=_object(closure.get(role,{}),'closure role').get(definition.key.token,{})
        return (type(witness) is dict and witness.get('record_fingerprint')==definition.wrapper.fingerprint
                and witness.get('depends_on')==[k.to_dict() for k in definition.dependencies]
                and witness.get('supported') is True and type(witness.get('evidence_ref')) is str
                and bool(witness['evidence_ref']))
    # Restore all previous retained definitions, never prune on upstream absence.
    for key,row in R.items():
        if key not in protected and key not in contradictory_keys:
            if key in L and L[key].wrapper!=row.wrapper:issue('retained_local_definition_conflict',(key,))
            else:effective[key]=row
    for key in sorted(set(B)|set(U)|set(L)):
        before,old,new=L.get(key),B.get(key),U.get(key)
        if key in contradictory_keys:
            if before is not None:effective[key]=before
            else:effective.pop(key,None)
            decisions.append(Decision(key,'hold_ambiguous_lifecycle','Contradictory native state is not proven user/default authority'))
        elif key in protected:
            if before is not None:effective[key]=before
            else:effective.pop(key,None)
            decisions.append(Decision(key,'preserve_user_authority','Whole record/lifecycle is explicit user authority'))
        elif key in R:
            if new is not None and new.wrapper!=R[key].wrapper:
                issue('retained_identity_reused',(key,))
            decisions.append(Decision(key,'retain_prior_compatibility','Existing retained state remains intact'))
        elif old is None and before is None and new is not None:
            effective[key]=new;decisions.append(Decision(key,'add_default','No local identity or lifecycle displaced'))
        elif old is None:
            if before is not None:
                effective[key]=before
                issue('untracked_local_origin',(key,))
        elif before is None:
            issue('missing_local_baseline',(key,))
        elif key not in default_owned:
            effective[key]=before;issue('unqualified_default_origin',(key,))
        elif before.wrapper!=old.wrapper:
            effective[key]=before;issue('local_default_drift_requires_ownership',(key,))
        elif new is None:
            carry[key]=before;effective[key]=before
            decisions.append(Decision(key,'retain_omitted_default','Absence is not deletion permission'))
        else:
            effective[key]=new;decisions.append(Decision(key,'update_default' if new.native!=old.native else 'unchanged_default',
                'Proven unchanged default-owned baseline'))
    # Only independently admitted exact transition declarations can redirect.
    rules=_array(M.get('transitions'),'migration rules');seen={}
    source_targets={}
    for rule in rules:
        _object(rule,'migration rule')
        source=RecordKey.read(rule.get('source'));target=RecordKey.read(rule.get('target'))
        source_targets.setdefault(source,set()).add(target)
    contradictory_sources={source for source,targets in source_targets.items()
                           if len(targets)>1 or (source in planned_redirects
                              and planned_redirects[source] not in targets)}
    for source in sorted(contradictory_sources):
        issue('contradictory_redirect_source',{source} | source_targets[source]
              | ({planned_redirects[source]} if source in planned_redirects else set()))
    lifecycle_rules={}
    for rule in rules:
        identity=_text(rule.get('id'),'migration ID');frozen=FrozenPayload.capture(rule)
        if identity in seen and seen[identity]!=frozen:
            raise CompatibilityAdmissionError('Conflicting complete migration payloads')
        seen[identity]=frozen
        if rule.get('kind')=='reviewed_lifecycle':
            source=RecordKey.read(rule.get('source'))
            lifecycle_rules.setdefault(source,[]).append(rule)
    seen={}
    upstream_removed,upstream_redirects,upstream_disabled=_life(docs['upstream'])
    def lifecycle_state(doc,key):
        retired,mapping,inactive_relations=observation_life[doc['role']]
        states=[]
        if key in retired:states.append('removed')
        if key in mapping:states.append('redirected')
        if key.kind=='relationship' and key.id in inactive_relations:states.append('disabled')
        return states[0] if len(states)==1 else ('active' if not states else 'contradictory')
    lifecycle_keys=(base_removed | upstream_removed | set(base_redirects) | set(upstream_redirects)
                    | {RecordKey('relationship',i) for i in base_disabled | upstream_disabled}
                    | set(lifecycle_rules))
    for key in sorted(lifecycle_keys):
        if key in contradictory_keys:continue
        before_state=lifecycle_state(docs['baseline'],key)
        target_state=lifecycle_state(docs['upstream'],key)
        if 'contradictory' in {before_state,target_state}:
            issue('contradictory_native_lifecycle',(key,))
            if key in L:effective[key]=L[key]
            else:effective.pop(key,None)
            continue
        if before_state==target_state and key not in lifecycle_rules:continue
        # Exact redirects continue through their separately qualified identity
        # lane. No new lifecycle declaration can dissolve or replace a redirect.
        if 'redirected' in {before_state,target_state}:
            if key in lifecycle_rules or (before_state=='redirected' and target_state!='redirected'):
                if key not in owned:issue('lifecycle_redirect_requires_identity_review',(key,))
            continue
        if key in owned or key in protected_dependencies:
            decisions.append(Decision(key,'preserve_user_lifecycle','Explicit user authority overrides upstream default state'))
            continue
        if key in protected and key not in retained_authority:
            if lifecycle_state(docs['local'],key)==before_state:
                issue('local_lifecycle_origin_requires_review',(key,))
                decisions.append(Decision(key,'preserve_local_lifecycle','Baseline-identical lifecycle origin is unqualified'))
            else:
                decisions.append(Decision(key,'preserve_user_lifecycle','Local explicit lifecycle differs from the baseline'))
            continue
        if key in retained_authority or key in R:
            issue('lifecycle_retained_authority_requires_review',(key,))
            continue
        declarations={FrozenPayload.capture(rule):rule for rule in lifecycle_rules.get(key,[])}
        rule=next(iter(declarations.values())) if len(declarations)==1 else None
        valid=(rule is not None and key not in contradictory_sources and before_state!=target_state
               and 'contradictory' not in {before_state,target_state}
               and rule.get('authority')=='reviewed_default_lifecycle'
               and RecordKey.read(rule.get('target'))==key
               and rule.get('from_state')==before_state and rule.get('to_state')==target_state
               and rule.get('base_release_fingerprint')==docs['baseline']['release_fingerprint']
               and rule.get('target_release_fingerprint')==docs['upstream']['release_fingerprint']
               and all(rule.get(field)==source_state_fingerprint(item,key) for field,item in (
                   ('source_state_fingerprint',baseline),('target_state_fingerprint',upstream),
                   ('local_state_fingerprint',local),('retained_state_fingerprint',retained)))
               and key in B and key in L and key in (default_owned | default_lifecycle)
               and L[key].wrapper==B[key].wrapper
               and lifecycle_state(docs['local'],key)==before_state
               and (target_state!='active' or key in U))
        if not valid:
            issue('upstream_lifecycle_requires_review',(key,),dict(from_state=before_state,to_state=target_state,
                  declaration_count=len(declarations)))
            # A held proposal retains the current full native record and state.
            if key in L:effective[key]=L[key]
            else:effective.pop(key,None)
            continue
        if target_state=='removed':removed.add(key)
        else:removed.discard(key)
        if key.kind=='relationship':
            if target_state=='disabled':disabled.add(key.id)
            else:disabled.discard(key.id)
        if target_state=='active':effective[key]=U[key]
        elif key in L:effective[key]=L[key]
        decisions.append(Decision(key,'qualified_default_'+target_state,
            'Reviewed same-identity lifecycle transition; history retained and no image rewrite'))
    for rule in rules:
        _object(rule,'migration rule');identity=_text(rule.get('id'),'migration ID')
        frozen=FrozenPayload.capture(rule)
        if identity in seen:
            if seen[identity]!=frozen:raise CompatibilityAdmissionError('Conflicting complete migration payloads')
            continue
        seen[identity]=frozen
        if rule.get('kind')=='reviewed_lifecycle':continue
        source=RecordKey.read(rule.get('source'));target=RecordKey.read(rule.get('target'))
        if source in contradictory_keys or target in contradictory_keys:continue
        if source in contradictory_sources:continue
        if (rule.get('kind')!='exact_redirect' or rule.get('authority')!='reviewed_default_identity'
                or rule.get('base_release_fingerprint')!=docs['baseline']['release_fingerprint']
                or rule.get('target_release_fingerprint')!=docs['upstream']['release_fingerprint']
                or rule.get('source_state_fingerprint')!=source_state_fingerprint(baseline,source)
                or source not in B or target not in U
                or rule.get('target_record_fingerprint')!=U[target].wrapper.fingerprint):
            issue('transition_authority_or_target_unqualified',(source,target),rule);continue
        chain={source};current=target
        while current in planned_redirects and current not in chain:
            chain.add(current);current=planned_redirects[current]
        chain.add(current)
        if source.kind!=target.kind or current==source or current in planned_redirects:
            issue('redirect_cycle_or_kind_mismatch',chain);continue
        if chain & protected or source not in default_owned | default_lifecycle:
            issue('redirect_protected_owner',chain);continue
        if ((source in L and L[source].wrapper!=B[source].wrapper)
                or (source not in L and source not in default_lifecycle)):
            issue('redirect_local_source_drift',chain);continue
        if current not in effective or current in removed or (current.kind=='relationship' and current.id in disabled):
            issue('redirect_terminal_unavailable',chain);continue
        planned_redirects[source]=target
        if source in effective:carry[source]=effective.pop(source)
        decisions.append(Decision(source,'qualified_default_redirect','Exact declared identity transition, no image rewrite'))
    for source,target in upstream_redirects.items():
        if base_redirects.get(source)==target or source in owned or source in protected_dependencies:
            continue
        if planned_redirects.get(source)!=target:
            issue('upstream_redirect_requires_review',(source,target))
    inactive=removed | set(planned_redirects) | {key for key in effective
                    if key.kind=='relationship' and key.id in disabled}
    for key in inactive:
        if key in effective:carry.setdefault(key,effective.pop(key))
    # Close retained records against exact native dependencies, never label lookup.
    pending=list(carry);visited=set()
    while pending:
        key=pending.pop()
        if key in visited:continue
        visited.add(key);row=carry[key]
        origin='local' if key in owned else ('retained' if key in R else 'baseline')
        if not qualified(origin,row):issue('unqualified_retained_closure',(key,))
        for dependency in row.dependencies:
            if dependency in planned_redirects:
                # Exact transition cannot substitute an incompatible category or
                # policy by lexical matching; preserve the old dependency too.
                if dependency in B:carry.setdefault(dependency,B[dependency]);pending.append(dependency)
                issue('retained_dependency_redirect_requires_native_qualification',(key,dependency))
                continue
            fallback=R.get(dependency) or B.get(dependency)
            if fallback is None or dependency in removed:
                issue('missing_or_retired_dependency',(key,dependency));continue
            if dependency in effective:
                if effective[dependency].wrapper!=fallback.wrapper:
                    issue('retained_dependency_changed',(key,dependency))
                    continue
                carry.setdefault(dependency,fallback);pending.append(dependency)
                continue
            carry[dependency]=fallback;effective[dependency]=fallback;pending.append(dependency)
    # Inactive records remain recoverable in retained/native evidence, but are
    # never projected as active definitions or used to satisfy closure.
    inactive |= removed | set(planned_redirects) | {key for key in effective
                    if key.kind=='relationship' and key.id in disabled}
    for key in inactive:
        if key in effective:carry.setdefault(key,effective.pop(key))
    for source in planned_redirects:
        chain=set();current=source
        while current in planned_redirects and current not in chain:
            chain.add(current);current=planned_redirects[current]
        if current in chain:issue('existing_redirect_cycle',chain)
        elif current not in effective:issue('existing_redirect_terminal_unavailable',chain | {current})
    for key,row in effective.items():
        if row.dependencies and key.kind not in {'tag','category','relationship'}:
            issue('unsupported_native_dependency_semantics',(key,))
        if key in U and key not in owned and key not in carry and not qualified('upstream',row):
            issue('unqualified_upstream_closure',(key,))
        for dependency in row.dependencies:
            if dependency not in effective:
                issue('effective_dependency_unavailable',(key,dependency))
            elif dependency in carry and not qualified('retained' if dependency in R else 'baseline',carry[dependency]):
                issue('unqualified_retained_closure',(dependency,))
    # Category ancestry/base dependency contracts are acyclic. Validate the
    # complete declared category graph iteratively (no recursion depth limit).
    graph={key:tuple(dep for dep in row.dependencies if dep.kind=='category' and dep in effective)
           for key,row in effective.items() if key.kind=='category'}
    colors={};path=[];positions={}
    for start in sorted(graph):
        if colors.get(start):continue
        colors[start]=1;positions[start]=len(path);path.append(start)
        stack=[(start,iter(graph[start]))]
        while stack:
            key,children=stack[-1];child=next(children,None)
            if child is None:
                stack.pop();path.pop();positions.pop(key);colors[key]=2
            elif colors.get(child)==1:
                issue('category_dependency_cycle',path[positions[child]:])
            elif not colors.get(child):
                colors[child]=1;positions[child]=len(path);path.append(child)
                stack.append((child,iter(graph[child])))
    names={}
    for key,row in effective.items():
        native=row.native.to_value()
        if key.kind=='tag':
            for name in (native['display'],*native.get('aliases',[])):
                names.setdefault(normalize_tag(name),set()).add(key)
    # Group all spelling collisions into connected identity components.
    edges=[(name,keys) for name,keys in names.items() if name and len(keys)>1]
    parents={}
    def root(key):
        parents.setdefault(key,key)
        while parents[key]!=key:
            parents[key]=parents[parents[key]];key=parents[key]
        return key
    for _,keys in edges:
        ordered=sorted(keys);leader=root(ordered[0])
        for key in ordered[1:]:parents[root(key)]=leader
    clusters={};labels={}
    for key in parents:clusters.setdefault(root(key),set()).add(key)
    for name,keys in edges:labels.setdefault(root(next(iter(keys))),set()).add(name)
    for leader in sorted(clusters):
        issue('identity_name_collision',clusters[leader],dict(normalized_keys=sorted(labels[leader])))
    # Collapse repeated witnesses for the same exception into one decision row.
    groups=list(dict.fromkeys(groups))
    for key in owned:
        preserved=carry.get(key) if key in inactive else effective.get(key)
        if preserved!=L[key]:raise CompatibilityAdmissionError('Protected whole user record changed')
    # Retained wrappers include unknown origin/provenance fields; no fake user rows.
    payload=dict(interface_version=INTERFACE_VERSION,input_pins=pins,activation_authorized=False,
        complete_plan=True,defaults=[r.wrapper.to_value() for r in upstream.definitions],
        native_observations={role:docs[role] for role in ROLES[:4]},
        effective_records=[effective[k].wrapper.to_value() for k in sorted(effective)],
        retained_records=[carry[k].wrapper.to_value() for k in sorted(carry)],
        original_retained_state=docs['retained'],local_lifecycle=docs['local']['lifecycle'],
        reconciled_lifecycle=dict(removed=[k.to_dict() for k in sorted(removed)],
                                 disabled_relation_ids=sorted(disabled)),
        proposed_redirects=[dict(source=k.to_dict(),target=v.to_dict()) for k,v in sorted(planned_redirects.items())],
        decisions=[dict(key=d.key.to_dict(),action=d.action,explanation=d.explanation) for d in decisions],
        exceptions=[dict(reason=g.reason,keys=[k.to_dict() for k in g.keys],evidence=g.evidence.to_value()) for g in groups])
    return CompatibilityPlan(FrozenPayload.capture(payload),tuple(decisions),tuple(groups))
