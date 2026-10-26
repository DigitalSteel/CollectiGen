"""Contained real stock-transport calls over inert fake responses only."""
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import socket
import subprocess
from urllib.parse import quote

import pytest
from core.application_update import UpdateServiceError
from core.component_acquisition import ComponentAcquisition, ExactComponentOffer, ExactOfferHold, InstalledOfferContext, ExactOfferAsset
from suite_core.discovery.github_component_releases import discover_releases, discover_component_ids, DiscoveryHold
from suite_core.discovery import component_staging as staging
from suite_core.installation import ComponentId
from suite_core.tests.test_component_release_signature import raw, signature, trust

ID = 'collectigen.tag-encyclopedia'
VERSION = '0.1.0-alpha.8'
API = 'https://api.github.com/repos/DigitalSteel/CollectiGen/releases?per_page=50&page='
PAYLOAD = b'INERT SYNTHETIC PACKAGE; NOT EXECUTABLE'


@pytest.fixture(autouse=True)
def no_executors(monkeypatch):
    calls = []
    def deny(*args, **kwargs):
        calls.append(args)
        raise AssertionError('real network/runtime/install invoked')
    for owner, name in [(socket, 'socket'), (subprocess, 'run'), (subprocess, 'Popen'),
                        (staging, 'stage_and_activate'), (staging, 'finish_or_recover')]:
        monkeypatch.setattr(owner, name, deny)
    import urllib.request
    monkeypatch.setattr(urllib.request, 'urlopen', deny)
    yield
    assert calls == []


class Response(BytesIO):
    status = 200
    def getcode(self):
        return self.status


class FakeFeed:
    def __init__(self):
        self.resources, self.rows, self.calls = {}, [], []
        self.failure, self.responses = None, []

    def add(self, component=ID, version=VERSION, payload=PAYLOAD, **changes):
        tag = component + '/v' + version
        base = 'https://github.com/DigitalSteel/CollectiGen/releases/download/' + quote(tag, safe='') + '/'
        name = component + '-' + version + '.zip'
        message = dict(schema_version=1, product='collectigen', component_id=component,
            version=version, channel='beta' if '-' in version else 'stable', published_at='2026-10-08T00:00:00Z',
            supported_from='0.1.0-alpha.1', release_notes='SIGNED: Read an explicit published snapshot.',
            platform=dict(os='windows', architecture='x86_64'), artifacts=[dict(name=name, kind='component-package',
            size_bytes=len(payload), sha256=sha256(payload).hexdigest(), portable_manifest_sha256='a'*64)])
        message.update(changes)
        m = raw(message)
        self.resources[base+'collectigen-release.json'] = m
        self.resources[base+'collectigen-release.json.sig'] = raw(signature(m))
        self.resources[base+name] = payload
        self.rows.append(dict(tag_name=tag, prerelease='-' in version, draft=False, name='UNTRUSTED TITLE',
            body='UNTRUSTED NOTES', assets=[dict(name=n, size=len(self.resources[base+n]), browser_download_url=base+n)
            for n in ['collectigen-release.json','collectigen-release.json.sig',name]]))
        return base, name

    def __call__(self, request, timeout):
        url = request.full_url
        self.calls.append((url, timeout, request.get_header('Range')))
        if self.failure and self.failure(url):
            raise OSError('injected interrupted transfer')
        if url.startswith(API):
            page = int(url[len(API):])
            data = raw(self.rows[(page-1)*50:page*50])
        else:
            data = self.resources[url]
        response = Response(data)
        self.responses.append(response)
        return response


def service(tmp_path, feed=None, **kwargs):
    root = tmp_path / ('cache-' + str(len(list(tmp_path.iterdir()))))
    root.mkdir()
    t = raw(trust())
    values = dict(cache_root=root, cache_owner='synthetic-fixture-only', opener=feed or FakeFeed(),
                  host_version='0.2.1-alpha.4', trust_bytes=t, trust_sha256=sha256(t).hexdigest())
    values.update(kwargs)
    return ComponentAcquisition(**values)


def offers(service, installed=None):
    return service.discover(channel='beta', installed_versions={} if installed is None else installed)


def test_scoped_discovery_skips_unrelated_platform_but_default_all_keeps_hold(tmp_path):
    f = FakeFeed(); f.add()
    f.add(component='collectigen.unrelated', platform=dict(os='linux', architecture='x86_64'))
    s = service(tmp_path, f)
    scoped = s.discover(channel='beta', installed_versions={}, component_id=ComponentId(ID))
    assert len(scoped) == 1 and scoped[0].release.component_id == ComponentId(ID)
    assert not any('collectigen.unrelated' in url for url in f.calls)
    with pytest.raises(ValueError, match='platform binding'):
        offers(s)


@pytest.mark.parametrize('fault', ['platform', 'signature'])
def test_scoped_discovery_still_rejects_selected_invalid_offer(tmp_path, fault):
    f = FakeFeed(); base, _name = f.add(**({'platform':dict(os='linux',architecture='x86_64')} if fault=='platform' else {}))
    if fault == 'signature': f.resources[base+'collectigen-release.json.sig'] = b'{}'
    with pytest.raises(ValueError):
        service(tmp_path, f).discover(channel='beta', installed_versions={}, component_id=ComponentId(ID))


def test_scoped_discovery_requires_typed_component_before_feed_access(tmp_path):
    f = FakeFeed(); f.add()
    with pytest.raises(ValueError, match='explicit_component_scope'):
        service(tmp_path,f).discover(channel='beta',installed_versions={},component_id=ID)
    assert not f.calls


def test_scoped_discovery_cannot_replace_explicit_exact_component(tmp_path):
    f = FakeFeed(); f.add()
    f.rows[0]['id'] = 51
    for index, asset in enumerate(f.rows[0]['assets']): asset['id'] = 61 + index
    reviewed = offers(service(tmp_path, f))[0]
    expected = ExactComponentOffer.from_reviewed_offer(reviewed, repository_id=123)
    f.calls.clear()
    with pytest.raises(ExactOfferHold):
        service(tmp_path,f,expected_offer=expected).discover(channel='beta',installed_versions={},
            component_id=ComponentId('collectigen.unrelated'))
    assert not f.calls
    f.resources['https://api.github.com/repos/DigitalSteel/CollectiGen'] = raw(
        dict(id=123,full_name='DigitalSteel/CollectiGen'))
    selected = service(tmp_path,f,expected_offer=expected).discover(channel='beta',installed_versions={},
        component_id=ComponentId(ID))
    assert len(selected)==1 and selected[0].release==reviewed.release


def test_first_install_then_real_stock_transport(tmp_path):
    feed = FakeFeed(); feed.add()
    s = service(tmp_path, feed)
    o, = offers(s)
    assert o.operation == 'install' and o.current_version is None
    assert o.release_notes.startswith('SIGNED:') and 'UNTRUSTED' not in repr(o)
    progress = []
    acquired = s.acquire(o, progress=lambda *args: progress.append(args))
    assert acquired.artifact.read_bytes() == PAYLOAD
    assert acquired.artifact.is_relative_to(s.root) and acquired.production_install_authorized is False
    assert progress[-1][:2] == (len(PAYLOAD), len(PAYLOAD))
    assert len(feed.calls) == 6 and all(r.closed for r in feed.responses)
    assert not (s.root / 'data').exists()


@pytest.mark.parametrize('current,expected', [('0.1.0-alpha.7','update'), (VERSION,None), ('0.1.0-alpha.9',None)])
def test_update_equal_and_downgrade(tmp_path, current, expected):
    f = FakeFeed(); f.add(); s = service(tmp_path,f)
    result = offers(s,{ID:current})
    assert ([o.operation for o in result] or [None]) == [expected]


def test_supported_from_update_hold_but_first_install(tmp_path):
    f = FakeFeed(); f.add(supported_from='0.1.0-alpha.7')
    s = service(tmp_path,f)
    with pytest.raises(ValueError): offers(s,{ID:'0.1.0-alpha.2'})
    assert offers(s)[0].operation == 'install'


def test_new_id_from_feed_only_and_old_offer_invalidated(tmp_path):
    f = FakeFeed(); f.add(); s = service(tmp_path,f); original, = offers(s)
    f.add('collectigen.new-reader')
    result = offers(s)
    assert {o.release.component_id.value for o in result} == {ID,'collectigen.new-reader'}
    with pytest.raises(ValueError,match='current_signed_offer'): s.acquire(original)


@pytest.mark.parametrize('change', ['signature','component','platform','size','missing','unsupported','trust','unknown_key','inactive_key'])
def test_untrusted_or_wrong_offer_never_displayed(tmp_path,change):
    f=FakeFeed(); base,name=f.add(); kwargs={}
    if change=='signature': f.resources[base+'collectigen-release.json.sig']=raw(dict(signature(f.resources[base+'collectigen-release.json']),signature='AA=='))
    elif change in ('component','platform','unsupported'):
        m=json.loads(f.resources[base+'collectigen-release.json'])
        if change=='component': m['component_id']='collectigen.other'
        elif change=='platform': m['platform']['os']='linux'
        else: m['schema_version']=999
        f.resources[base+'collectigen-release.json']=raw(m)
        f.resources[base+'collectigen-release.json.sig']=raw(signature(raw(m)))
    elif change=='size': f.rows[0]['assets'][-1]['size']+=1
    elif change=='missing': f.rows[0]['assets']=f.rows[0]['assets'][1:]
    elif change=='unknown_key':
        signed=json.loads(f.resources[base+'collectigen-release.json.sig']);signed['key_id']='unenrolled'
        f.resources[base+'collectigen-release.json.sig']=raw(signed)
    elif change=='inactive_key':
        t=trust();t['keys'][0]['status']='revoked';t=raw(t)
        kwargs=dict(trust_bytes=t,trust_sha256=sha256(t).hexdigest())
    else: kwargs=dict(trust_bytes=None,trust_sha256=None)
    s=service(tmp_path,f,**kwargs)
    with pytest.raises(ValueError): offers(s)
    assert not s._offers and list(s.root.iterdir())==[s._marker]


@pytest.mark.parametrize('kind',['short','hash','oversize','interrupted','progress_failure'])
def test_failed_transfer_cleans_only_attempt(tmp_path,kind):
    f=FakeFeed(); base,name=f.add(); s=service(tmp_path,f); o,=offers(s)
    if kind=='short': f.resources[base+name]=PAYLOAD[:-1]
    elif kind=='hash': f.resources[base+name]=b'X'*len(PAYLOAD)
    elif kind=='oversize': f.resources[base+name]=PAYLOAD+b'X'
    elif kind=='interrupted': f.failure=lambda url:url==base+name
    progress=None
    if kind=='progress_failure':
        def progress(*args): raise RuntimeError('injected progress callback failure')
    with pytest.raises((UpdateServiceError,RuntimeError)):
        s.acquire(o,progress=progress)
    assert list(s.root.iterdir())==[s._marker]
    f.failure=None; f.resources[base+name]=PAYLOAD
    assert s.acquire(o).artifact.read_bytes()==PAYLOAD


@pytest.mark.parametrize('point',['before','metadata','after_block'])
def test_cancel_cleans_no_completed_transfer(tmp_path,point):
    f=FakeFeed(); f.add(); s=service(tmp_path,f); o,=offers(s)
    cancelled=point=='before'
    def progress(*args):
        nonlocal cancelled
        cancelled=True
    if point=='metadata':
        original=f.__call__
        def opener(*args,**kwargs):
            nonlocal cancelled
            response=original(*args,**kwargs); cancelled=True; return response
        s.transport.opener=opener
    with pytest.raises(InterruptedError):
        s.acquire(o,should_cancel=lambda:cancelled,progress=progress if point=='after_block' else None)
    assert list(s.root.iterdir())==[s._marker]


@pytest.mark.parametrize('which',['manifest','signature','trust_pin','forged_offer'])
def test_changed_reviewed_metadata(tmp_path,which):
    f=FakeFeed(); base,name=f.add(); s=service(tmp_path,f); o,=offers(s)
    if which=='manifest': f.resources[base+'collectigen-release.json']+=b' '
    elif which=='signature': f.resources[base+'collectigen-release.json.sig']+=b' '
    elif which=='trust_pin': s.trust_pin='0'*64
    else: o=replace(o,release_notes='forged')
    with pytest.raises(ValueError): s.acquire(o)
    assert list(s.root.iterdir())==[s._marker]


def test_explicit_artifact_bound(tmp_path):
    f=FakeFeed(); f.add(); s=service(tmp_path,f,max_artifact_bytes=1)
    with pytest.raises(ValueError,match='artifact_bound'): offers(s)
    assert all(not url.endswith('.zip') for url,_,_ in f.calls)


@pytest.mark.parametrize('what',['opener','relative','occupied','owner_changed','trust_changed'])
def test_explicit_cache_and_trust(tmp_path,what):
    f=FakeFeed(); f.add()
    if what=='opener':
        with pytest.raises(ValueError): service(tmp_path,f,opener=None)
    elif what=='relative':
        with pytest.raises(ValueError): service(tmp_path,f,cache_root=Path('relative'))
    elif what=='occupied':
        root=tmp_path/'occupied';root.mkdir();(root/'foreign').write_bytes(b'preserve')
        with pytest.raises(ValueError): service(tmp_path,f,cache_root=root)
        assert (root/'foreign').read_bytes()==b'preserve'
    else:
        s=service(tmp_path,f);o,=offers(s)
        if what=='owner_changed': s._marker.write_bytes(b'changed')
        else: s.trust+=b' '
        with pytest.raises(ValueError):s.acquire(o)


def test_two_full_pages_require_empty_completion(tmp_path):
    f=FakeFeed()
    for i in range(100): f.rows.append(dict(tag_name='v0.1.'+str(i),draft=False,prerelease=False))
    s=service(tmp_path,f)
    assert offers(s)==()
    assert [url for url,_,_ in f.calls]==[API+'1',API+'2',API+'3']


@pytest.mark.parametrize('bad',['overbound','incomplete','duplicate','json','nonfinite','flags','origin'])
def test_bounded_feed_hold(tmp_path,bad):
    f=FakeFeed();f.add()
    if bad=='overbound': f.rows=f.rows*101
    elif bad=='duplicate': f.rows=f.rows*2
    elif bad=='flags': f.rows[0]['draft']='false'
    elif bad=='origin': f.rows[0]['assets'][0]['browser_download_url']='http://localhost/untrusted'
    s=service(tmp_path,f)
    if bad in ('incomplete','json','nonfinite'):
        original=f.__call__
        def opener(request,timeout):
            if request.full_url==API+'1':
                if bad=='incomplete': return Response(raw([dict(tag_name='v1.0.0')]*50))
                return Response(b'not json' if bad=='json' else b'[NaN]')
            if bad=='incomplete': raise OSError('pagination failed')
            return original(request,timeout)
        s.transport.opener=opener
    with pytest.raises((ValueError,UpdateServiceError)): offers(s)
    assert not s._offers


def test_pure_first_install_and_stable_catchup():
    f=FakeFeed();f.add()
    data=raw(f.rows)
    assert discover_component_ids(data,complete=True)==(ComponentId(ID),)
    assert discover_releases(data,component_id=ComponentId(ID),channel='beta',current_version=None,complete=True).candidate
    result=discover_releases(data,component_id=ComponentId(ID),channel='stable',current_version='0.1.0-alpha.7',complete=True)
    assert result.state=='waiting_for_stable_catch_up' and result.candidate is None


@pytest.mark.parametrize('what',['trust','selection'])
def test_inflight_authority_change_cleans_transfer(tmp_path,what):
    f=FakeFeed();f.add();s=service(tmp_path,f);o,=offers(s)
    def changed(*args):
        if what=='trust':s.trust+=b' '
        else:s._offers.clear()
    with pytest.raises(ValueError):s.acquire(o,progress=changed)
    assert list(s.root.iterdir())==[s._marker]


@pytest.mark.parametrize('limit',[0,-1,True,1048577])
def test_public_metadata_seam_bound(tmp_path,limit):
    f=FakeFeed();s=service(tmp_path,f)
    with pytest.raises(UpdateServiceError):s.transport.fetch_metadata(API+'1',limit=limit)
    assert not f.calls


def test_partial_read_failure_and_previous_cache_preserved(tmp_path):
    f=FakeFeed();base,name=f.add();s=service(tmp_path,f);o,=offers(s)
    first=s.acquire(o)
    f.add('collectigen.second-reader',payload=b'A'*(1024*1024+20))
    second=next(x for x in offers(s) if x.release.component_id.value=='collectigen.second-reader')
    original=f.__call__
    class Fault(Response):
        read_count=0
        def read(self,size=-1):
            self.read_count+=1
            if self.read_count==2:raise OSError('interrupted after partial block')
            return super().read(size)
    def opener(request,timeout):
        if request.full_url==second.artifact_source.url:
            return Fault(b'A'*(1024*1024+20))
        return original(request,timeout)
    s.transport.opener=opener
    with pytest.raises(UpdateServiceError):s.acquire(second)
    assert first.artifact.read_bytes()==PAYLOAD
    assert len(list(s.root.iterdir()))==2  # marker and prior successful attempt only


def test_cache_quota_and_exact_reuse(tmp_path):
    f=FakeFeed();f.add();s=service(tmp_path,f,max_cache_bytes=len(PAYLOAD));o,=offers(s)
    first=s.acquire(o);before=len(f.calls)
    assert s.acquire(o).artifact==first.artifact
    assert len(f.calls)==before+2  # fresh metadata, no second payload transfer
    f.add('collectigen.second-reader')
    second=next(x for x in offers(s) if x.release.component_id.value=='collectigen.second-reader')
    with pytest.raises(ValueError,match='cache_bound'):s.acquire(second)
    assert first.artifact.read_bytes()==PAYLOAD


def test_changed_cached_artifact_not_overwritten(tmp_path):
    f=FakeFeed();f.add();s=service(tmp_path,f);o,=offers(s)
    first=s.acquire(o);first.artifact.write_bytes(b'foreign-changed-cache')
    with pytest.raises(ValueError):s.acquire(o)
    assert first.artifact.read_bytes()==b'foreign-changed-cache'


def test_metadata_response_bound_before_parse(tmp_path):
    f=FakeFeed();s=service(tmp_path,f)
    s.transport.opener=lambda request,timeout:Response(b' '*(1024*1024+1))
    with pytest.raises(UpdateServiceError,match='safety limit'):offers(s)
    assert list(s.root.iterdir())==[s._marker]


REPO_API='https://api.github.com/repos/DigitalSteel/CollectiGen'
METADATA_NAMES=('collectigen-release.json','collectigen-release.json.sig')


class MaturityFeed(FakeFeed):
    def __init__(self):
        super().__init__();self.resources[REPO_API]=raw(dict(id=4321,full_name='DigitalSteel/CollectiGen'))

    def add_maturity(self,version='0.4.0',stage='alpha',**changes):
        tag=ID+'/releases/schema2/'+version+'/'+stage
        base='https://github.com/DigitalSteel/CollectiGen/releases/download/'+quote(tag,safe='')+'/'
        name='reader-'+version+'-'+stage+'.zip'
        m=dict(schema_version=2,product='collectigen',component_id=ID,version=version,maturity=stage,
            channel='stable' if stage=='stable' else 'beta',published_at='2026-10-26T00:00:00Z',
            supported_from='0.1.0',release_notes='SIGNED: static maturity fixture',platform=dict(os='windows',architecture='x86_64'),
            artifacts=[dict(name=name,kind='component-package',size_bytes=len(PAYLOAD),sha256=sha256(PAYLOAD).hexdigest(),portable_manifest_sha256='a'*64)])
        m.update(changes);message=raw(m)
        self.resources[base+METADATA_NAMES[0]]=message;self.resources[base+METADATA_NAMES[1]]=raw(signature(message))
        self.resources[base+name]=PAYLOAD
        rid=int(sha256(tag.encode()).hexdigest()[:10],16)
        self.rows.append(dict(id=rid,tag_name=tag,prerelease=stage!='stable',draft=False,
            assets=[dict(id=rid*100+i+1,name=n,size=len(self.resources[base+n]),browser_download_url=base+n)
                for i,n in enumerate((*METADATA_NAMES,name))]))
        return base,name


def maturity_service(tmp_path,feed,context=None,**kwargs):
    from types import SimpleNamespace
    from core.component_release_transport import ComponentReleaseTransport,ReleaseTransportPolicy
    state=[context or InstalledOfferContext(ComponentId(ID),None,None,None,'original-root-observation')]
    transport=ComponentReleaseTransport(ReleaseTransportPolicy(schema2_metadata_assets=METADATA_NAMES))
    transport._opener=SimpleNamespace(open=feed)
    values=dict(schema2_metadata=True,observe_installed=lambda:state[0],expected_repository_id=4321,opener=transport)
    values.update(kwargs)
    return service(tmp_path,feed,**values),state


def maturity_offers(s,context,channel='beta',**kwargs):
    return s.discover(channel=channel,component_id=ComponentId(ID),
        installed_versions={} if context.version is None else {ID:context.version},**kwargs)


@pytest.mark.parametrize('stage',['alpha','beta','stable'])
def test_signed_maturity_metadata_has_no_artifact_authority(tmp_path,stage):
    f=MaturityFeed();base,name=f.add_maturity(stage=stage);s,state=maturity_service(tmp_path,f)
    o,=maturity_offers(s,state[0],channel='stable' if stage=='stable' else 'beta')
    assert (o.release.schema_version,o.release.maturity,o.installed_context,o.discovery_epoch)==(2,stage,state[0],1)
    assert not o.production_install_authorized
    assert all(url!=base+name for url,_,_ in f.calls)
    before=tuple(f.calls);tree=set(s.root.iterdir())
    with pytest.raises(ValueError,match='schema2_installation_not_admitted'):s.acquire(o)
    assert tuple(f.calls)==before and set(s.root.iterdir())==tree
    s._acquired[o.release.manifest_sha256]=tmp_path/'fabricated-cache'
    with pytest.raises(ValueError,match='schema2_installation_not_admitted'):s.acquire(o)
    assert tuple(f.calls)==before and set(s.root.iterdir())==tree


def test_same_number_promotion_authenticates_every_stage_then_selects(tmp_path):
    f=MaturityFeed();f.add_maturity(stage='alpha');f.add_maturity(stage='beta')
    c=InstalledOfferContext(ComponentId(ID),'0.4.0',2,'alpha','original-selected-identity')
    s,state=maturity_service(tmp_path,f,c);o,=maturity_offers(s,c)
    assert o.release.maturity=='beta' and o.operation=='update'
    assert sum(url.endswith('.sig') for url,_,_ in f.calls)==2
    f.rows.reverse();s.revalidate_offer(o)


@pytest.mark.parametrize('schema,version,stage,allowed',[(1,'0.3.1-alpha.1',None,True),(1,'0.4.0-alpha.1',None,False),
    (2,'0.4.0','alpha',False),(2,'0.4.0','beta',False),(2,'0.0.9','stable',False)])
def test_maturity_upgrade_and_floor_boundary(tmp_path,schema,version,stage,allowed):
    f=MaturityFeed();f.add_maturity();c=InstalledOfferContext(ComponentId(ID),version,schema,stage,'original-context')
    s,state=maturity_service(tmp_path,f,c)
    if allowed:assert maturity_offers(s,c)[0].release.maturity=='alpha'
    else:
        with pytest.raises(ValueError):maturity_offers(s,c)
        assert not s._offers


@pytest.mark.parametrize('failure',['signature','key','channel','platform','stage','artifact','size','repository','missing_ids'])
def test_wrong_signed_or_exact_metadata_cannot_publish_offer(tmp_path,failure):
    f=MaturityFeed();changes={}
    if failure=='channel':changes['channel']='stable'
    if failure=='platform':changes['platform']=dict(os='linux',architecture='x86_64')
    if failure=='stage':changes['maturity']='beta'
    base,name=f.add_maturity(**changes)
    if failure in ('signature','key'):
        signed=json.loads(f.resources[base+METADATA_NAMES[1]])
        signed['signature' if failure=='signature' else 'key_id']='invalid'
        f.resources[base+METADATA_NAMES[1]]=raw(signed)
        f.rows[0]['assets'][1]['size']=len(f.resources[base+METADATA_NAMES[1]])
    if failure=='artifact':f.rows[0]['assets'][2]['size']+=1
    if failure=='size':f.rows[0]['assets'][0]['size']+=1
    if failure=='repository':f.resources[REPO_API]=raw(dict(id=999,full_name='DigitalSteel/CollectiGen'))
    if failure=='missing_ids':f.rows[0]['assets'][0].pop('id')
    s,state=maturity_service(tmp_path,f)
    with pytest.raises(ValueError):maturity_offers(s,state[0])
    assert s._offers=={} and all(url!=base+name for url,_,_ in f.calls)


def test_bad_highest_numeric_group_never_falls_back(tmp_path):
    f=MaturityFeed();lower,_=f.add_maturity('0.3.0');higher,_=f.add_maturity('0.4.0')
    f.resources[higher+METADATA_NAMES[1]]=b'{}';f.rows[1]['assets'][1]['size']=2
    s,state=maturity_service(tmp_path,f)
    with pytest.raises(ValueError):maturity_offers(s,state[0])
    assert not s._offers and not any(url.startswith(lower) for url,_,_ in f.calls)


def test_revalidation_without_exact_expectation_is_fresh_and_failure_clears_epoch(tmp_path):
    f=MaturityFeed();base,_=f.add_maturity();s,state=maturity_service(tmp_path,f);o,=maturity_offers(s,state[0])
    before=len(f.calls);s.revalidate_offer(o);assert len(f.calls)>before
    state[0]=replace(state[0],observation_id='changed-original-identity')
    with pytest.raises(ValueError):s.revalidate_offer(o)
    assert not s._offers


def test_exact_reviewed_schema_stage_and_asset_pins_revalidate(tmp_path):
    f=MaturityFeed();f.add_maturity();s,state=maturity_service(tmp_path,f);o,=maturity_offers(s,state[0])
    expected=ExactComponentOffer.from_reviewed_offer(o,repository_id=4321)
    assert expected.schema_version==2 and all(a.schema_version==2 for a in (expected.manifest,expected.signature,expected.artifact))
    other,context=maturity_service(tmp_path,f,expected_offer=expected);approved,=maturity_offers(other,context[0]);other.revalidate_offer(approved)
    with pytest.raises(ValueError):replace(expected,maturity='beta')
    with pytest.raises(ValueError):replace(expected.artifact,schema_version=1)
    with pytest.raises(ValueError):replace(expected,release_id=expected.release_id+1).check_offer(approved)


def test_new_epoch_rejects_old_offer_object_without_reusing_it(tmp_path):
    f=MaturityFeed();f.add_maturity();s,state=maturity_service(tmp_path,f);old,=maturity_offers(s,state[0]);new,=maturity_offers(s,state[0])
    assert new.discovery_epoch>old.discovery_epoch and new is not old
    with pytest.raises(ValueError):s.revalidate_offer(old)
    s.revalidate_offer(new)


@pytest.mark.parametrize('point',list(range(1,13)))
def test_opted_in_cancellation_never_publishes_prior_or_partial_offer(tmp_path,point):
    f=MaturityFeed();f.add_maturity();s,state=maturity_service(tmp_path,f);maturity_offers(s,state[0])
    count=0
    def cancel():
        nonlocal count
        count+=1;return count==point
    with pytest.raises(InterruptedError):maturity_offers(s,state[0],should_cancel=cancel)
    assert not s._offers and not any(p.name.startswith('attempt-') for p in s.root.iterdir())


def test_observer_change_during_fetch_cannot_publish(tmp_path):
    f=MaturityFeed();f.add_maturity();s,state=maturity_service(tmp_path,f)
    def mutate(url):
        if url.endswith('.sig'):state[0]=replace(state[0],observation_id='changed-during-io')
        return False
    f.failure=mutate
    with pytest.raises(ValueError):maturity_offers(s,state[0])
    assert not s._offers


def test_final_publication_check_cannot_miss_changed_context(tmp_path):
    f=MaturityFeed();f.add_maturity();s,state=maturity_service(tmp_path,f);original=state[0];calls=0
    def check():
        nonlocal calls
        calls+=1
        if calls==12:state[0]=replace(state[0],observation_id='changed-before-publish')
        return False
    with pytest.raises(ValueError):maturity_offers(s,original,should_cancel=check)
    assert not s._offers


@pytest.mark.parametrize('bad',[None,'missing',True])
def test_missing_typed_context_holds_before_fetch(tmp_path,bad):
    f=MaturityFeed();f.add_maturity();s,state=maturity_service(tmp_path,f);state[0]=bad
    with pytest.raises(ValueError):s.discover(channel='beta',component_id=ComponentId(ID),installed_versions={})
    assert f.calls==[] and not s._offers


def test_pending_and_wrong_component_context_hold_before_fetch(tmp_path):
    f=MaturityFeed();f.add_maturity();s,state=maturity_service(tmp_path,f)
    for change in (dict(pending=True),dict(component_id=ComponentId('collectigen.suite-manager'))):
        state[0]=replace(InstalledOfferContext(ComponentId(ID),None,None,None,'ctx'),**change)
        with pytest.raises(ValueError):s.discover(channel='beta',component_id=ComponentId(ID),installed_versions={})
    assert f.calls==[]


@pytest.mark.parametrize('config',[dict(schema2_metadata=1),dict(schema2_metadata=True),dict(observe_installed=lambda:None),
    dict(expected_repository_id=4321),dict(schema2_metadata=True,observe_installed=lambda:None,expected_repository_id=True),
    dict(schema2_metadata=True,observe_installed=lambda:None,expected_repository_id=4321,manifest_name='arbitrary.json')])
def test_invalid_metadata_constructor_does_not_create_owner_marker(tmp_path,config):
    root=tmp_path/'empty';root.mkdir()
    with pytest.raises(ValueError):ComponentAcquisition(cache_root=root,cache_owner='test',opener=FakeFeed(),host_version='0.1.0',**config)
    assert list(root.iterdir())==[]


def test_metadata_only_policy_never_admits_package_or_unqualified_redirect(tmp_path):
    from core.component_release_transport import ReleaseTransportPolicy,TransportHold
    f=MaturityFeed();base,name=f.add_maturity();policy=ReleaseTransportPolicy(schema2_metadata_assets=METADATA_NAMES)
    assert all(policy.admit_initial(base+n)=='asset' for n in METADATA_NAMES)
    with pytest.raises(TransportHold):policy.admit_initial(base+name)
    with pytest.raises(TransportHold):policy.admit_redirect(base+METADATA_NAMES[0],'https://evil.example/file',1)
    qualified=ReleaseTransportPolicy(asset_redirect_origins=('https://release-assets.githubusercontent.com',),schema2_metadata_assets=METADATA_NAMES)
    qualified.admit_redirect(base+METADATA_NAMES[0],'https://release-assets.githubusercontent.com/file',1)
    with pytest.raises(TransportHold):qualified.admit_redirect(base+METADATA_NAMES[0],'https://release-assets.githubusercontent.com/file',5)
    for names in (list(METADATA_NAMES),('*.json','*.sig'),(name,),tuple(reversed(METADATA_NAMES))):
        with pytest.raises(TransportHold):ReleaseTransportPolicy(schema2_metadata_assets=names)


def test_beta_to_stable_same_number_promotion(tmp_path):
    f=MaturityFeed();f.add_maturity(stage='stable');c=InstalledOfferContext(ComponentId(ID),'0.4.0',2,'beta','original-context')
    s,state=maturity_service(tmp_path,f,c);o,=maturity_offers(s,c,channel='stable');assert o.release.maturity=='stable'


@pytest.mark.parametrize('change',[dict(version='0.4.0',schema_version=None,maturity='alpha'),
    dict(version='0.4.0',schema_version=True,maturity='alpha'),dict(version='0.4.0',schema_version=2,maturity=None),
    dict(version='0.4.0-alpha.1',schema_version=2,maturity='alpha'),dict(version=None,schema_version=1),
    dict(observation_id=''),dict(observation_id='x'*1025),dict(observation_id='x\0y'),dict(pending=1)])
def test_installed_context_cannot_guess_schema_or_stage(change):
    values=dict(component_id=ComponentId(ID),version=None,schema_version=None,maturity=None,observation_id='ctx')
    values.update(change)
    with pytest.raises(ValueError):InstalledOfferContext(**values)


def add_identified_legacy(feed,version):
    base,name=feed.add(version=version);row=feed.rows[-1];row['id']=8765
    for i,a in enumerate(row['assets']):a['id']=876500+i
    return base,name


def test_mixed_same_base_and_schema_regression_hold(tmp_path):
    mixed=MaturityFeed();mixed.add_maturity();add_identified_legacy(mixed,'0.4.0-alpha.1')
    s,state=maturity_service(tmp_path,mixed)
    with pytest.raises(ValueError,match='mixed_schema'):maturity_offers(s,state[0])
    old=MaturityFeed();add_identified_legacy(old,'0.5.0-alpha.1')
    c=InstalledOfferContext(ComponentId(ID),'0.4.0',2,'alpha','ctx');other,state=maturity_service(tmp_path,old,c)
    with pytest.raises(ValueError,match='schema_regression'):maturity_offers(other,c)


def test_same_group_bad_stage_clears_previous_offer_not_partial_success(tmp_path):
    f=MaturityFeed();f.add_maturity();s,state=maturity_service(tmp_path,f);maturity_offers(s,state[0])
    base,_=f.add_maturity(stage='beta');f.resources[base+METADATA_NAMES[1]]=b'{}';f.rows[-1]['assets'][1]['size']=2
    with pytest.raises(ValueError):maturity_offers(s,state[0])
    assert not s._offers


@pytest.mark.parametrize('change',['signed_notes','asset_id','new_highest','trust','installed_stage'])
def test_revalidation_detects_fresh_metadata_or_authority_change(tmp_path,change):
    f=MaturityFeed();base,_=f.add_maturity();s,state=maturity_service(tmp_path,f);o,=maturity_offers(s,state[0])
    if change=='signed_notes':
        m=json.loads(f.resources[base+METADATA_NAMES[0]]);m['release_notes']='SIGNED: changed';message=raw(m)
        f.resources[base+METADATA_NAMES[0]]=message;f.resources[base+METADATA_NAMES[1]]=raw(signature(message))
        for a in f.rows[0]['assets']:a['size']=len(f.resources[a['browser_download_url']])
    elif change=='asset_id':f.rows[0]['assets'][2]['id']+=1
    elif change=='new_highest':f.add_maturity('0.5.0')
    elif change=='trust':s.trust=b'{}'
    else:state[0]=InstalledOfferContext(ComponentId(ID),'0.4.0',2,'beta','changed-inst')
    with pytest.raises(ValueError):s.revalidate_offer(o)
    assert not s._offers


@pytest.mark.parametrize('point',[1,7,12])
def test_cancelled_fresh_revalidation_discards_offer(tmp_path,point):
    f=MaturityFeed();f.add_maturity();s,state=maturity_service(tmp_path,f);o,=maturity_offers(s,state[0]);count=0
    def cancel():
        nonlocal count
        count+=1;return count==point
    with pytest.raises(InterruptedError):s.revalidate_offer(o,should_cancel=cancel)
    assert not s._offers


def test_opted_in_pagination_requires_empty_third_page(tmp_path):
    f=MaturityFeed();f.rows=[{} for _ in range(100)];s,state=maturity_service(tmp_path,f)
    f.failure=lambda url:url==API+'3'
    with pytest.raises(UpdateServiceError):maturity_offers(s,state[0])
    assert not s._offers
    f.failure=None;assert maturity_offers(s,state[0])==()
    assert all(r.closed for r in f.responses)


def test_metadata_scope_and_repository_expectation_cannot_silently_change(tmp_path):
    f=MaturityFeed();f.add_maturity();s,state=maturity_service(tmp_path,f)
    with pytest.raises(ValueError):s.discover(channel='beta',installed_versions={})
    assert not f.calls
    with pytest.raises(ValueError):s.discover(channel='beta',installed_versions={ID:'0.3.0'},component_id=ComponentId(ID))
    assert not f.calls
    o,=maturity_offers(s,state[0]);expected=ExactComponentOffer.from_reviewed_offer(o,repository_id=4321)
    root=tmp_path/'wrong-repository';root.mkdir()
    with pytest.raises(ValueError):ComponentAcquisition(cache_root=root,cache_owner='ctx',opener=f,host_version='0.1.0',
        schema2_metadata=True,observe_installed=lambda:state[0],expected_repository_id=999,expected_offer=expected)
    assert list(root.iterdir())==[]


def test_changed_cache_owner_at_publication_does_not_publish_metadata(tmp_path):
    f=MaturityFeed();f.add_maturity();s,state=maturity_service(tmp_path,f);calls=0
    def change_owner():
        nonlocal calls
        calls+=1
        if calls==12:s._marker.write_bytes(b'foreign owner')
        return False
    with pytest.raises(ValueError):maturity_offers(s,state[0],should_cancel=change_owner)
    assert not s._offers and s._marker.read_bytes()==b'foreign owner'
