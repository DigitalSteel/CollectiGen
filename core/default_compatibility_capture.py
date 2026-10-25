"""Bounded read-only capture from existing guarded native Foundation readers.

The host supplies selected admission/guard and approved release observations.
No enrollment, synchronization, staging, image access or activation occurs here.
"""
from dataclasses import dataclass, field
from hashlib import sha256
from base64 import b64encode
import json
from pathlib import Path

from .default_compatibility import PinnedInput, NativeDefinition, CompatibilityAdmissionError
from .default_catalog_release import ApprovedDefaultPackRelease, require_previous
from .default_sync import resolve_effective_tag_defaults
from .effective_tag_snapshot_reader import EffectiveTagSnapshotReader, EffectiveTagReaderChanged
from .knowledge_pack import FoundationProposal, FoundationPack, _foundation_json
from .knowledge_relationships import KnowledgeRelationship
from .tag_database import TagRecord
from .category_registry import CategoryRecord
from .relation_rule_service import FrozenPayload
from .relationship_store import validate_relationships

TAG_FILES=('foundation_tags.json','community_tags.json','promoted_tags.json')
REQUIRED=TAG_FILES+('categories.json','relationships.json','tag_aliases.json')
SUPPORTED_RELATIONS={'specificity','entailment','category_base'}


class NativeCompatibilityCaptureHold(CompatibilityAdmissionError):
    """Native shape/semantics cannot yet be captured completely; preserve it."""


def _pin(doc):
    raw=json.dumps(doc,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
    return PinnedInput.decode(raw,role=doc['role'],expected_sha256=sha256(raw).hexdigest())


def _rows(doc,field):
    values=doc.get(field,[])
    if type(values) is not list or any(type(row) is not dict for row in values):
        raise NativeCompatibilityCaptureHold('Malformed native '+field)
    return values


def _validate_relationship_set(rows):
    issues=validate_relationships(tuple(KnowledgeRelationship.from_dict(row) for row in rows))
    if issues:
        codes=sorted({issue.code for issue in issues})
        raise NativeCompatibilityCaptureHold('Canonical relationship validation held: '+', '.join(codes))


def _record(kind,native):
    # Native parsers validate known shapes, while the exact raw object (not their
    # normalized serializer) remains the authoritative planned definition.
    dependencies=set()
    def dep(kind,identity):
        if type(identity) is not str or not identity.strip():
            raise NativeCompatibilityCaptureHold('Malformed native dependency')
        dependencies.add((kind,identity))
    if kind=='tag':
        parsed=TagRecord.from_dict(native)
        if parsed.id!=native.get('id') or parsed.display!=native.get('display'):
            raise NativeCompatibilityCaptureHold('Native Tag requires normalization')
        if native.get('category'):dep('category',native['category'])
        if native.get('concept_id'):
            raise NativeCompatibilityCaptureHold('Concept dependency capture is not qualified')
        relations=native.get('relations',{})
        if type(relations) is not dict:raise NativeCompatibilityCaptureHold('Malformed native Tag relations')
        if set(relations)-{'parent','attributes','implies','conflicts','allowed_modifier_groups'}:
            raise NativeCompatibilityCaptureHold('Unknown native Tag relation semantics')
        if relations.get('parent'):dep('tag',relations['parent'])
        for field in ('attributes','implies','conflicts','allowed_modifier_groups'):
            values=relations.get(field,[])
            if type(values) is not list:raise NativeCompatibilityCaptureHold('Malformed native relation inventory')
            for identity in values:dep('category' if field=='allowed_modifier_groups' else 'tag',identity)
    elif kind=='category':
        if native.get('parent_id'):dep('category',native['parent_id'])
        if native.get('base_tag_id'):dep('tag',native['base_tag_id'])
    elif kind=='relationship':
        _validate_relationship_set((native,))
        parsed=KnowledgeRelationship.from_dict(native)
        if parsed.kind not in SUPPORTED_RELATIONS or len(parsed.endpoints)!=len(native.get('endpoints',())):
            raise NativeCompatibilityCaptureHold('Relationship family capture is not qualified')
        for endpoint in parsed.endpoints:
            if endpoint.kind not in {'tag','category'}:
                raise NativeCompatibilityCaptureHold('Relationship endpoint family capture is not qualified')
            dep(endpoint.kind,endpoint.id)
    row=dict(kind=kind,id=native.get('id'),native=native,
             depends_on=[dict(kind=k,id=i) for k,i in sorted(dependencies)])
    NativeDefinition.read(row)
    return row


def _default_records(payloads):
    docs={Path(name).name:_foundation_json(raw) for name,raw in payloads}
    if not set(REQUIRED)<=set(docs):
        raise NativeCompatibilityCaptureHold('Complete bounded native default participants required')
    for name,doc in docs.items():
        if name not in REQUIRED and any(isinstance(value,(list,dict)) and value for value in doc.values()):
            raise NativeCompatibilityCaptureHold('Nonempty native family is not qualified: '+name)
    if _rows(docs['tag_aliases.json'],'tags'):
        raise NativeCompatibilityCaptureHold('External alias projection capture is not qualified')
    layers=[_rows(docs[name],'tags') for name in TAG_FILES]
    lifecycle=docs['promoted_tags.json']
    compiled=resolve_effective_tag_defaults(layers,removed_tag_ids=lifecycle.get('removed_tag_ids',[]),
                                          merged_tag_ids=lifecycle.get('merged_tag_ids',{}))
    raw={}
    for layer in layers:
        for row in layer:raw[row['id']]=row
    if compiled.findings or any(row!=raw.get(row['id']) for row in compiled.tags) or any(k!=v for k,v in compiled.id_remap.items()):
        raise NativeCompatibilityCaptureHold('Stock compiler identity/relation rewrite requires explicit qualification')
    records=[_record('tag',row) for row in raw.values()]
    records += [_record('category',row) for row in _rows(docs['categories.json'],'categories')]
    relationship_rows=_rows(docs['relationships.json'],'relationships')
    _validate_relationship_set(relationship_rows)
    records += [_record('relationship',row) for row in relationship_rows]
    life=dict(removed=[dict(kind='tag',id=i) for i in lifecycle.get('removed_tag_ids',[])],
              redirects=[dict(source=dict(kind='tag',id=k),target=dict(kind='tag',id=v))
                         for k,v in dict(lifecycle.get('merged_tag_ids',{})).items()],
              disabled_relation_ids=sorted(set(docs['relationships.json'].get('disabled_default_ids',[]))
                                          | {row['id'] for row in relationship_rows if row.get('disabled') is True}))
    return records,life


@dataclass(frozen=True)
class NativeCompatibilityCapture:
    catalogs: tuple[PinnedInput,...]
    participant_pins: tuple[tuple[Path,str | None],...]
    memory_fingerprint: str
    _reader: object=field(repr=False,compare=False)
    _categories: object=field(repr=False,compare=False)
    _aliases: object=field(repr=False,compare=False)
    _revision: object=field(repr=False,compare=False)

    def verify(self):
        self._reader.verify(self._revision)
        self._reader.verify_participants(self._categories,self._aliases)
        if _memory_fingerprint(self._categories,self._aliases)!=self.memory_fingerprint:
            raise EffectiveTagReaderChanged('Native Category/relationship memory changed')
        for path,expected in self.participant_pins:
            for participant in (path,*path.parents):
                if participant.is_symlink() or getattr(participant,'is_junction',lambda:False)():
                    raise EffectiveTagReaderChanged('Redirected native compatibility participant')
            actual=sha256(path.read_bytes()).hexdigest() if path.exists() else None
            if actual!=expected:raise EffectiveTagReaderChanged('Native compatibility participant changed')

    def preview(self, *, ownership, migrations):
        """Evidence must already pin these exact captures; no evidence synthesis."""
        from suite_core.knowledge.catalog_migration_preview import preview_native_compatibility
        self.verify()
        inputs={item.role:item for item in self.catalogs}
        result=preview_native_compatibility(**inputs,ownership=ownership,migrations=migrations)
        relationship_rows=[row.native.to_value() for row in result.plan.effective_records
                           if row.key.kind=='relationship']
        _validate_relationship_set(relationship_rows)
        upstream_disabled=set(inputs['upstream'].payload.to_value()['lifecycle']['disabled_relation_ids'])
        stored_users={tuple((key['kind'],key['id'])) for key in
                      inputs['local'].payload.to_value()['stored_user_records']}
        if result.plan.ready_for_native_review and any(row['id'] in upstream_disabled and ('relationship',row['id']) not in stored_users
               for row in relationship_rows):
            raise NativeCompatibilityCaptureHold('Upstream disabled relationship requires planner lifecycle qualification')
        self.verify()
        return result


def _memory_fingerprint(categories,aliases):
    return FrozenPayload.capture(dict(categories={key:row.to_dict() for key,row in categories.categories.items()},
               relationships=[row.to_dict() for row in aliases.workspace.effective])).fingerprint


def capture_native_compatibility(*, baseline,upstream,
                                 expected_baseline_inventory,expected_upstream_inventory,
                                 reader,categories,aliases,revision,retained,
                                 baseline_source_snapshot,upstream_source_snapshot):
    """Host retains its selected admission throughout this bounded read.

    Baseline is exact current default bytes, upstream is an independently pinned
    FoundationProposal. Retained/ownership/migration authority is never inferred
    from names, tracking hashes or absent rows. Unsupported native families hold.
    """
    if type(reader) is not EffectiveTagSnapshotReader or type(retained) is not PinnedInput or retained.role!='retained':
        raise NativeCompatibilityCaptureHold('Exact guarded native reader and retained observation required')
    if type(baseline) is not FoundationPack or type(upstream) is not FoundationPack:
        raise NativeCompatibilityCaptureHold('Exact decoded Foundation Pack DTOs required')
    baseline_decoded,upstream_decoded=baseline,upstream
    baseline_release,upstream_release=baseline.approved_release,upstream.approved_release
    baseline,upstream=baseline.proposal,upstream.proposal
    for proposal,release,inventory in ((baseline,baseline_release,expected_baseline_inventory),
                                       (upstream,upstream_release,expected_upstream_inventory)):
        if type(proposal) is not FoundationProposal or type(release) is not ApprovedDefaultPackRelease:
            raise NativeCompatibilityCaptureHold('Existing approved Foundation DTOs required')
        if proposal.source_inventory!=inventory:
            raise NativeCompatibilityCaptureHold('Separately pinned Foundation inventory changed')
    if baseline_release.pack_id!=upstream_release.pack_id or upstream_release.previous_release_fingerprint!=baseline_release.release_fingerprint:
        raise NativeCompatibilityCaptureHold('Approved native release lineage mismatch')
    from suite_core.knowledge.native_tag_catalog_host import bind_foundation_source_release
    for decoded,snapshot in ((baseline_decoded,baseline_source_snapshot),
                             (upstream_decoded,upstream_source_snapshot)):
        # Existing binding validates independently approved source DTO/release;
        # it expressly grants no installation or complete-closure authority.
        bind_foundation_source_release(decoded,snapshot)
    reader.verify(revision);reader.verify_participants(categories,aliases)
    user=reader.raw_tags.path.absolute().parent;root=user.parent.parent
    baseline_rows,baseline_life=_default_records(baseline.payloads)
    upstream_rows,upstream_life=_default_records(upstream.payloads)
    pins={};native_bytes={}
    for path,digest in reader._pins:
        raw=path.read_bytes() if path.exists() else None
        if (sha256(raw).digest() if raw is not None else None)!=digest:
            raise EffectiveTagReaderChanged('Native compatibility capture is stale')
        pins[path]=None if raw is None else sha256(raw).hexdigest()
        native_bytes[path.relative_to(root).as_posix()]=raw
    for name,raw in baseline.payloads:
        path=root/name
        for participant in (path,*path.parents):
            if participant.is_symlink() or getattr(participant,'is_junction',lambda:False)():
                raise EffectiveTagReaderChanged('Redirected native compatibility participant')
        current=path.read_bytes() if path.exists() else None
        if current!=raw:raise EffectiveTagReaderChanged('Baseline is not exact installed native defaults')
        pins[path]=sha256(raw).hexdigest();native_bytes[name]=raw
    user_docs={name:_foundation_json(native_bytes.get('data/user/'+name) or b'{}')
               for name in ('tags.json','categories.json','relationships.json','default_knowledge_state.json')}
    require_previous(user_docs['default_knowledge_state.json'],upstream_release)
    stored=[];composed={(r['kind'],r['id']):r for r in baseline_rows}
    for kind,name,field in [('tag','tags.json','tags'),('category','categories.json','categories'),
                            ('relationship','relationships.json','relationships')]:
        for native in _rows(user_docs[name],field):
            row=_record(kind,native);key=(kind,row['id']);stored.append(dict(kind=kind,id=row['id']));composed[key]=row
    tags=user_docs['tags.json'];relations=user_docs['relationships.json']
    local_life=dict(removed=[dict(kind='tag',id=i) for i in tags.get('removed_tag_ids',[])],
                    redirects=[dict(source=dict(kind='tag',id=k),target=dict(kind='tag',id=v))
                               for k,v in dict(tags.get('merged_tag_ids',{})).items()],
                    disabled_relation_ids=relations.get('disabled_default_ids',[]))
    local_life['removed'] += [dict(kind='category',id=i) for i in user_docs['categories.json'].get('disabled_default_ids',[])]
    local_life['disabled_relation_ids'] += [row['id'] for row in _rows(relations,'relationships') if row.get('disabled') is True]
    local_life['disabled_relation_ids']=sorted(set(local_life['disabled_relation_ids']) | {
        key[1] for key,row in composed.items() if key[0]=='relationship' and row['native'].get('disabled') is True})
    # The existing admitted read recipe owns effective default/user lifecycle.
    # Capture that exact composition, rather than reviving default tombstones
    # merely because a user lifecycle file does not repeat them.
    local_life['removed']=[dict(kind='tag',id=i) for i in sorted(reader.tags.removed_tag_ids)] + [
        row for row in local_life['removed'] if row['kind']!='tag']
    local_life['redirects']=[dict(source=dict(kind='tag',id=k),target=dict(kind='tag',id=v))
                           for k,v in sorted(reader.tags.merged_tag_ids.items())]
    # Require agreement with actual stock native read surface; no flattened DTO
    # or manufactured new identity can masquerade as the local observation.
    active={key[1]:row['native'] for key,row in composed.items() if key[0]=='tag'}
    inactive={r['id'] for r in local_life['removed'] if r['kind']=='tag'} | {r['source']['id'] for r in local_life['redirects']}
    inactive |= set(reader.tags.removed_tag_ids) | set(reader.tags.merged_tag_ids)
    active={key:row for key,row in active.items() if key not in inactive}
    def non_alias_payload(record):
        value=record.to_dict();value.pop('aliases',None)
        return value
    if set(active)!=set(reader.tags.tags) or any(non_alias_payload(TagRecord.from_dict(row))!=non_alias_payload(reader.tags.tags[key]) for key,row in active.items()):
        raise NativeCompatibilityCaptureHold('Complete local native Tag composition disagrees with stock reader')
    for identity,native in active.items():
        for spelling in native.get('aliases',[]):
            if aliases.resolve(spelling).tag_id!=identity:
                raise NativeCompatibilityCaptureHold('Inline alias disagrees with stock native authority')
    category_rows={key[1]:row['native'] for key,row in composed.items() if key[0]=='category'}
    disabled_categories=set(user_docs['categories.json'].get('disabled_default_ids',[]))
    category_rows={key:row for key,row in category_rows.items() if key not in disabled_categories}
    user_category_ids={row['id'] for row in _rows(user_docs['categories.json'],'categories')}
    if set(category_rows)!=set(categories.categories) or any(
            CategoryRecord.from_dict(row,source='user' if key in user_category_ids else 'default').to_dict()
            !=categories.categories[key].to_dict() for key,row in category_rows.items()):
        raise NativeCompatibilityCaptureHold('Complete local Category composition disagrees with stock reader')
    relationship_rows={key[1]:row['native'] for key,row in composed.items() if key[0]=='relationship'}
    _validate_relationship_set(relationship_rows.values())
    actual_relations={row.id:row.to_dict() for row in aliases.workspace.effective}
    if set(relationship_rows)!=set(actual_relations) or any(
            KnowledgeRelationship.from_dict(row).to_dict()!=actual_relations[key]
            for key,row in relationship_rows.items()):
        raise NativeCompatibilityCaptureHold('Complete native relationship composition disagrees with stock reader')
    captures=[]
    for role,records,life,release in [('baseline',baseline_rows,baseline_life,baseline_release),
                                    ('upstream',upstream_rows,upstream_life,upstream_release),
                                    ('local',list(composed.values()),local_life,baseline_release)]:
        payloads=baseline.payloads if role=='baseline' else upstream.payloads if role=='upstream' else tuple(native_bytes.items())
        doc=dict(schema_revision=1,role=role,pack_id=release.pack_id,release_fingerprint=release.release_fingerprint,
                 completeness='complete',records=records,lifecycle=life,
                 native_payloads={name:None if raw is None else b64encode(raw).decode() for name,raw in payloads})
        if role=='upstream':doc['previous_release_fingerprint']=release.previous_release_fingerprint
        if role=='local':doc['stored_user_records']=stored
        captures.append(_pin(doc))
    captures.append(retained)
    result=NativeCompatibilityCapture(tuple(captures),tuple(sorted(pins.items())),
                                     _memory_fingerprint(categories,aliases),reader,categories,aliases,revision)
    result.verify()
    return result
