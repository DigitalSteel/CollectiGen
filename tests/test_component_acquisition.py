"""Contained real stock-transport calls over inert fake responses only."""
from dataclasses import replace
from email.message import Message
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import socket
import subprocess
from urllib.parse import quote

import pytest
from core.application_update import ApplicationUpdateService, ReleaseAssetSource, UpdateServiceError
from core.component_acquisition import ComponentAcquisition, ExactComponentOffer, ExactOfferHold
from release_contract import ReleaseArtifact
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

    def add(self, component=ID, version=VERSION, payload=PAYLOAD, *, asset_name=None, **changes):
        tag = component + '/v' + version
        base = 'https://github.com/DigitalSteel/CollectiGen/releases/download/' + quote(tag, safe='') + '/'
        name = asset_name or (component + '-' + version + '.zip')
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


def test_cache_claim_preserves_owner_created_after_empty_check(tmp_path, monkeypatch):
    from pathlib import Path
    from suite_core.discovery.component_staging import StagingHold

    root = tmp_path / "cache"
    root.mkdir()
    marker = root / ".component-acquisition-owner.json"
    original = Path.iterdir

    def claim_after_listing(path):
        entries = list(original(path))
        if path == root:
            assert not entries
            marker.write_bytes(b"other owner")
        return iter(entries)

    monkeypatch.setattr(Path, "iterdir", claim_after_listing)
    with pytest.raises(StagingHold, match="empty_owned_cache_required"):
        ComponentAcquisition(cache_root=root, cache_owner="second", opener=FakeFeed(),
                             host_version="0.2.1-alpha.4")
    assert marker.read_bytes() == b"other owner"


def test_replaced_cache_root_with_copied_marker_is_refused(tmp_path):
    from suite_core.discovery.component_staging import StagingHold

    feed = FakeFeed()
    acquisition = service(tmp_path, feed)
    root = acquisition.root
    moved = root.with_name("original-cache")
    root.rename(moved)
    root.mkdir()
    marker_name = ".component-acquisition-owner.json"
    marker = (moved / marker_name).read_bytes()
    (root / marker_name).write_bytes(marker)
    with pytest.raises(StagingHold, match="cache_root_changed"):
        offers(acquisition)
    assert not feed.calls
    assert (root / marker_name).read_bytes() == marker
    assert (moved / marker_name).read_bytes() == marker


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
    assert len(feed.calls) == 7 and all(r.closed for r in feed.responses)
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


@pytest.mark.parametrize("incomplete_http_body", [False, True])
def test_partial_read_failure_and_previous_cache_preserved(tmp_path, incomplete_http_body):
    from http.client import IncompleteRead

    f=FakeFeed();base,name=f.add();s=service(tmp_path,f);o,=offers(s)
    first=s.acquire(o)
    f.add('collectigen.second-reader',payload=b'A'*(1024*1024+20))
    second=next(x for x in offers(s) if x.release.component_id.value=='collectigen.second-reader')
    original=f.__call__
    class Fault(Response):
        read_count=0
        def read(self,size=-1):
            self.read_count+=1
            if self.read_count == 2:
                if incomplete_http_body:
                    raise IncompleteRead(b"partial", 20)
                raise OSError("interrupted after partial block")
            return super().read(size)
    def opener(request,timeout):
        if request.full_url==second.artifact_source.url:
            return Fault(b'A'*(1024*1024+20))
        return original(request,timeout)
    s.transport.opener=opener
    with pytest.raises(UpdateServiceError, match="download was interrupted"):
        s.acquire(second)
    assert first.artifact.read_bytes()==PAYLOAD
    assert len(list(s.root.iterdir()))==2  # marker and prior successful attempt only
    s.transport.opener = original
    assert s.acquire(second).artifact.read_bytes() == b'A'*(1024*1024+20)


def test_replaced_download_attempt_is_preserved(tmp_path, monkeypatch):
    from suite_core.discovery.component_staging import StagingHold

    feed = FakeFeed()
    feed.add()
    acquisition = service(tmp_path, feed)
    offer, = offers(acquisition)
    original = acquisition.transport.download_artifact
    displaced = acquisition.root / "displaced"
    replacement = []

    def replace_attempt(*args, **kwargs):
        artifact = original(*args, **kwargs)
        artifact.parent.rename(displaced)
        artifact.parent.mkdir()
        artifact.write_bytes(b"foreign")
        replacement.append(artifact)
        return artifact

    monkeypatch.setattr(acquisition.transport, "download_artifact", replace_attempt)
    with pytest.raises(StagingHold, match="cache_attempt_changed"):
        acquisition.acquire(offer)
    assert replacement[0].read_bytes() == b"foreign"
    assert (displaced / offer.release.artifact.name).read_bytes() == PAYLOAD
    assert not acquisition._acquired


def test_overlapping_discovery_refuses_before_contacting_feed(tmp_path):
    feed = FakeFeed()
    feed.add()
    acquisition = service(tmp_path, feed)
    original = acquisition.transport.opener
    calls = []

    def opener(request, timeout):
        if not calls:
            calls.append(request.full_url)
            assert not feed.calls
            with pytest.raises(ValueError, match="release check is already in progress"):
                offers(acquisition)
            assert not feed.calls
        return original(request, timeout)

    acquisition.transport.opener = opener
    offer, = offers(acquisition)
    assert offer.release.component_id == ComponentId(ID)
    assert len(calls) == 1
    refreshed, = offers(acquisition)
    assert refreshed.release == offer.release


def test_overlapping_acquisition_refuses_before_contacting_feed(tmp_path):
    feed = FakeFeed()
    feed.add()
    acquisition = service(tmp_path, feed, max_cache_bytes=len(PAYLOAD))
    offer, = offers(acquisition)
    callbacks = []

    def progress(*_args):
        before = len(feed.calls)
        with pytest.raises(ValueError, match="already in progress"):
            acquisition.acquire(offer)
        assert len(feed.calls) == before
        callbacks.append(True)

    completed = acquisition.acquire(offer, progress=progress)
    assert callbacks
    assert completed.artifact.read_bytes() == PAYLOAD
    assert acquisition.acquire(offer).artifact == completed.artifact


def test_stop_during_cached_component_verification_keeps_verified_retry(tmp_path, monkeypatch):
    from core import component_acquisition

    feed = FakeFeed()
    feed.add()
    acquisition = service(tmp_path, feed, max_cache_bytes=len(PAYLOAD))
    offer, = offers(acquisition)
    cached = acquisition.acquire(offer).artifact
    verify = component_acquisition.verify_component_artifact
    stopped = False

    def stop_while_verifying(path, release, *, should_cancel):
        nonlocal stopped
        assert path == cached
        stopped = True
        return verify(path, release, should_cancel=should_cancel)

    with monkeypatch.context() as patch:
        patch.setattr(component_acquisition, "verify_component_artifact", stop_while_verifying)
        with pytest.raises(InterruptedError, match="verification was cancelled"):
            acquisition.acquire(offer, should_cancel=lambda: stopped)
    assert cached.read_bytes() == PAYLOAD
    assert acquisition.acquire(offer).artifact == cached


def test_cache_quota_and_exact_reuse(tmp_path):
    f=FakeFeed();f.add();s=service(tmp_path,f,max_cache_bytes=len(PAYLOAD));o,=offers(s)
    first=s.acquire(o);before=len(f.calls)
    assert s.acquire(o).artifact==first.artifact
    assert len(f.calls)==before+3  # fresh listing and signed metadata, no second payload transfer
    f.add('collectigen.second-reader')
    second=next(x for x in offers(s) if x.release.component_id.value=='collectigen.second-reader')
    with pytest.raises(ValueError,match='cache_bound'):s.acquire(second)
    assert first.artifact.read_bytes()==PAYLOAD


@pytest.mark.parametrize("same_offer", [True, False])
def test_missing_cached_artifact_allows_a_fresh_signed_download(tmp_path, same_offer):
    feed = FakeFeed()
    feed.add()
    acquisition = service(tmp_path, feed, max_cache_bytes=len(PAYLOAD))
    first_offer, = offers(acquisition)
    first = acquisition.acquire(first_offer)
    first.artifact.unlink()
    if same_offer:
        next_offer = first_offer
    else:
        feed.add('collectigen.second-reader')
        next_offer = next(offer for offer in offers(acquisition)
                          if offer.release.component_id.value == 'collectigen.second-reader')

    replacement = acquisition.acquire(next_offer)

    assert replacement.artifact.parent != first.artifact.parent
    assert replacement.artifact.read_bytes() == PAYLOAD
    assert not first.artifact.exists()


@pytest.mark.parametrize("enough_cache", [True, False])
def test_changed_cached_artifact_not_overwritten(tmp_path, enough_cache):
    feed = FakeFeed()
    feed.add()
    acquisition = service(tmp_path, feed, max_cache_bytes=len(PAYLOAD) * (2 if enough_cache else 1))
    offer, = offers(acquisition)
    first = acquisition.acquire(offer)
    altered = b"X" * len(PAYLOAD)
    first.artifact.write_bytes(altered)

    with pytest.raises(ValueError, match="SHA-256"):
        acquisition.acquire(offer)

    if enough_cache:
        replacement = acquisition.acquire(offer)
        assert replacement.artifact != first.artifact
        assert replacement.artifact.read_bytes() == PAYLOAD
    else:
        with pytest.raises(ValueError, match="cache_bound"):
            acquisition.acquire(offer)
    assert first.artifact.read_bytes() == altered


def test_metadata_response_bound_before_parse(tmp_path):
    f=FakeFeed();s=service(tmp_path,f)
    s.transport.opener=lambda request,timeout:Response(b' '*(1024*1024+1))
    with pytest.raises(UpdateServiceError,match='safety limit'):offers(s)
    assert list(s.root.iterdir())==[s._marker]


def test_self_update_can_stop_during_verification_and_retry_without_downloading(tmp_path):
    payload = b"a" * (1024 * 1024) + b"b"
    name = "inert.zip"
    artifact = ReleaseArtifact(name, "portable", sha256(payload).hexdigest(), len(payload), "")
    source = ReleaseAssetSource(name, "https://example.invalid/inert.zip", len(payload))

    def unexpected_download(*_args, **_kwargs):
        pytest.fail("Complete retained bytes must be verified without another request")

    service = ApplicationUpdateService(tmp_path, "1.0.0", opener=unexpected_download)
    cache = service.state_root / "downloads"
    cache.mkdir(parents=True)
    part = cache / (name + ".part")
    part.write_bytes(payload)
    checks = 0

    def stop_after_first_hash_block():
        nonlocal checks
        checks += 1
        return checks == 4

    with pytest.raises(InterruptedError, match="verification was cancelled"):
        service._download(source, artifact, should_cancel=stop_after_first_hash_block)
    assert part.read_bytes() == payload
    assert not (cache / name).exists()
    assert service._download(source, artifact).read_bytes() == payload
    assert not part.exists()


@pytest.mark.parametrize("retry", ["range", "ignored", "missing_header", "wrong_hash"])
def test_self_update_short_download_retries_with_signed_integrity(tmp_path, retry):
    name = "inert.zip"
    artifact = ReleaseArtifact(name, "portable", sha256(PAYLOAD).hexdigest(), len(PAYLOAD), "")
    source = ReleaseAssetSource(name, "https://example.invalid/inert.zip", len(PAYLOAD))
    offset = 9
    calls = []

    def opener(request, *, timeout):
        calls.append(request.get_header("Range"))
        if len(calls) == 1:
            return Response(PAYLOAD[:offset])
        if retry == "ignored":
            return Response(PAYLOAD)
        remaining = PAYLOAD[offset:]
        if retry == "wrong_hash":
            remaining = b"X" * len(remaining)
        response = Response(remaining)
        response.status = 206
        response.headers = Message()
        if retry != "missing_header":
            response.headers["Content-Range"] = f"bytes {offset}-{len(PAYLOAD)-1}/{len(PAYLOAD)}"
        return response

    service = ApplicationUpdateService(tmp_path, "1.0.0", opener=opener)
    cache = service.state_root / "downloads"
    cache.mkdir(parents=True)
    complete = cache / name
    complete.write_bytes(b"previous verified archive")
    partial = cache / (name + ".part")

    with pytest.raises(UpdateServiceError, match="Retry to resume"):
        service._download(source, artifact)
    assert partial.read_bytes() == PAYLOAD[:offset]
    assert complete.read_bytes() == b"previous verified archive"

    if retry == "missing_header":
        with pytest.raises(UpdateServiceError, match="signed byte range"):
            service._download(source, artifact)
        assert partial.read_bytes() == PAYLOAD[:offset]
        assert complete.read_bytes() == b"previous verified archive"
    elif retry == "wrong_hash":
        with pytest.raises(UpdateServiceError, match="quarantined.*SHA-256"):
            service._download(source, artifact)
        assert (cache / (name + ".part.rejected")).read_bytes() == PAYLOAD[:offset] + b"X" * (len(PAYLOAD) - offset)
        assert complete.read_bytes() == b"previous verified archive"
    else:
        assert service._download(source, artifact).read_bytes() == PAYLOAD
        assert not partial.exists()

    assert calls == [None, f"bytes={offset}-"]


def test_scoped_discovery_checks_assets_of_the_selected_release_only(tmp_path):
    feed = FakeFeed()
    feed.add(version="0.1.0-alpha.7")
    feed.add(version=VERSION)
    feed.rows[0]["assets"] = []
    acquisition = service(tmp_path, feed)
    offer, = acquisition.discover(channel="beta", installed_versions={}, component_id=ComponentId(ID))
    assert offer.release.software_version == VERSION

    feed.rows[1]["assets"] = []
    with pytest.raises(DiscoveryHold, match="release_assets_missing"):
        acquisition.discover(channel="beta", installed_versions={}, component_id=ComponentId(ID))


def test_invalidated_selected_assets_require_a_new_review_before_download(tmp_path):
    feed = FakeFeed()
    feed.add()
    acquisition = service(tmp_path, feed)
    offer, = offers(acquisition)
    original_assets = feed.rows[0]["assets"]
    feed.rows[0]["assets"] = []

    with pytest.raises(ExactOfferHold, match="reviewed release changed"):
        acquisition.acquire(offer)
    assert all(url != offer.artifact_source.url for url, _, _ in feed.calls)

    feed.rows[0]["assets"] = original_assets
    with pytest.raises(ValueError, match="current_signed_offer"):
        acquisition.acquire(offer)
    refreshed, = offers(acquisition)
    assert acquisition.acquire(refreshed).artifact.read_bytes() == PAYLOAD


def test_scoped_refresh_retires_only_offers_for_the_selected_component(tmp_path):
    feed = FakeFeed()
    feed.add()
    other_id = ComponentId("collectigen.second-reader")
    feed.add(other_id.value)
    acquisition = service(tmp_path, feed)
    reviewed = {offer.release.component_id: offer for offer in offers(acquisition)}
    selected = ComponentId(ID)

    assert acquisition.discover(
        channel="beta", installed_versions={ID: VERSION}, component_id=selected,
    ) == ()

    with pytest.raises(ValueError, match="current_signed_offer"):
        acquisition.acquire(reviewed[selected])
    assert acquisition.acquire(reviewed[other_id]).artifact.read_bytes() == PAYLOAD


@pytest.mark.parametrize("fault", ["assets", "signature"])
def test_scoped_invalid_release_retires_only_its_previous_review(tmp_path, fault):
    feed = FakeFeed()
    feed.add()
    other = ComponentId("collectigen.second-reader")
    feed.add(other.value)
    acquisition = service(tmp_path, feed)
    selected = ComponentId(ID)
    reviewed = {offer.release.component_id: offer for offer in offers(acquisition)}
    assets = feed.rows[0]["assets"]
    signature_url = reviewed[selected].signature_url
    signature = feed.resources[signature_url]
    if fault == "assets":
        feed.rows[0]["assets"] = []
    else:
        feed.resources[signature_url] = b"{}"

    with pytest.raises(ValueError):
        acquisition.discover(channel="beta", installed_versions={}, component_id=selected)
    feed.rows[0]["assets"] = assets
    feed.resources[signature_url] = signature
    with pytest.raises(ValueError, match="current_signed_offer"):
        acquisition.acquire(reviewed[selected])
    assert acquisition.acquire(reviewed[other]).artifact.read_bytes() == PAYLOAD
    fresh, = acquisition.discover(channel="beta", installed_versions={}, component_id=selected)
    assert acquisition.acquire(fresh).artifact.read_bytes() == PAYLOAD


def test_manager_stage_rechecks_only_its_signed_release_before_publication(tmp_path, monkeypatch):
    from core import manager_update_service

    feed = FakeFeed()
    feed.add(manager_update_service.MANAGER.value)
    feed.add(component="collectigen.unrelated", platform={"os": "linux", "architecture": "x86_64"})
    acquisition = service(tmp_path, feed)
    updater = manager_update_service.ManagerUpdateService(
        acquisition, current_version="0.1.0-alpha.7", channel="beta",
    )
    offer = updater.discover()
    assert offer.release.component_id == manager_update_service.MANAGER

    monkeypatch.setattr(manager_update_service, "produce_detached_inventory", lambda *_a, **_kw: b"inert")
    def withdraw_during_admission(*_args, **_kwargs):
        feed.rows.pop(0)
        return object()
    monkeypatch.setattr(updater, "_admit", withdraw_during_admission)

    with pytest.raises(ExactOfferHold, match="reviewed release changed"):
        updater.prepare(offer)
    assert updater._prepared is None
    assert not any("collectigen.unrelated" in url for url, _, _ in feed.calls)


def test_manager_stage_refuses_archive_replaced_during_final_offer_check(tmp_path, monkeypatch):
    from core import manager_update_service

    feed = FakeFeed()
    feed.add(manager_update_service.MANAGER.value)
    acquisition = service(tmp_path, feed)
    updater = manager_update_service.ManagerUpdateService(
        acquisition, current_version="0.1.0-alpha.7", channel="beta",
    )
    offer = updater.discover()
    monkeypatch.setattr(manager_update_service, "produce_detached_inventory", lambda *_a, **_kw: b"inert")
    original_opener = acquisition.transport.opener

    def replace_cached_archive(request, timeout):
        cached = acquisition._acquired.get(offer.release.manifest_sha256)
        if cached is not None:
            cached.write_bytes(b"X" * len(PAYLOAD))
        return original_opener(request, timeout)

    def simulate_admission(*_args, **_kwargs):
        acquisition.transport.opener = replace_cached_archive
        return object()

    monkeypatch.setattr(updater, "_admit", simulate_admission)
    with pytest.raises(ValueError, match="SHA-256"):
        updater.prepare(offer)
    assert updater._prepared is None


def test_signed_offer_larger_than_empty_cache_is_not_reviewable(tmp_path):
    feed = FakeFeed()
    feed.add()
    acquisition = service(tmp_path, feed, max_cache_bytes=len(PAYLOAD) - 1)

    with pytest.raises(ValueError, match="artifact_exceeds_cache_capacity"):
        offers(acquisition)

    assert not acquisition._offers
    assert list(acquisition.root.iterdir()) == [acquisition._marker]
    assert not any(url.endswith(".zip") for url, _, _ in feed.calls)


@pytest.mark.parametrize("hold", [None, "archive_member_count", "detached_inventory_pin"])
def test_manager_stages_only_packages_that_pass_stock_admission(tmp_path, hold):
    import zipfile
    from core.manager_update_service import ManagerUpdateService, reviewed_stage_identity
    from suite_core.discovery.manager_runtime_detached_admission import (
        MANAGER, PACKAGE_STUB, PAYLOAD_ENTRY, PROFILE, produce_detached_inventory,
    )
    from suite_core.discovery.manager_runtime_admission_support import NS, PREFIX

    fields = dict(id=MANAGER.value, version=VERSION, channel="beta-win-x64", os="win",
                  rid="win-x64", machineArchitecture="x64", mainExe=PAYLOAD_ENTRY)
    contents = "".join(f"<{key}>{value}</{key}>" for key, value in fields.items())
    nuspec = f'<package xmlns="{NS[1:-1]}"><metadata>{contents}</metadata></package>'.encode()
    archive_path = tmp_path / f"{MANAGER.value}-{VERSION}-full.nupkg"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
        archive.writestr("_rels/.rels", b"<Relationships/>")
        archive.writestr(MANAGER.value + ".nuspec", nuspec)
        archive.writestr(PREFIX + "sq.version", nuspec)
        for name in (PAYLOAD_ENTRY, PACKAGE_STUB, "Squirrel.exe"):
            archive.writestr(PREFIX + name, b"INERT NOT EXECUTABLE")

        if hold == "archive_member_count":
            for index in range(509):
                archive.writestr(PREFIX + f"inert-{index}.bin", b"INERT")

    proof = None
    if hold != "archive_member_count":
        proof = produce_detached_inventory(archive_path, inventory_profile=PROFILE,
                                           expected_version=VERSION, expected_channel="beta")
    signed_proof = sha256(proof).hexdigest() if hold is None else "0" * 64
    payload = archive_path.read_bytes()
    feed = FakeFeed()
    feed.add(MANAGER.value, payload=payload, asset_name=archive_path.name, artifacts=[dict(
        name=archive_path.name, kind="component-package", size_bytes=len(payload),
        sha256=sha256(payload).hexdigest(), portable_manifest_sha256=signed_proof,
    )])
    updater = ManagerUpdateService(service(tmp_path, feed),
                                   current_version="0.1.0-alpha.7", channel="beta")

    offer = updater.discover()
    if hold:
        with pytest.raises(ValueError, match=hold):
            updater.prepare(offer)
        assert updater._prepared is None
        return

    prepared = updater.prepare(offer)
    assert prepared.artifact.read_bytes() == payload
    assert prepared.proof == proof
    assert updater.revalidate(prepared) == prepared.admission
    assert reviewed_stage_identity(prepared)["inventory_sha256"] == sha256(proof).hexdigest()
    assert not prepared.production_install_authorized


def test_grown_cache_marker_is_read_with_a_bound(tmp_path, monkeypatch):
    feed = FakeFeed()
    feed.add()
    owner = service(tmp_path, feed)
    offer, = offers(owner)
    original = owner._marker.read_bytes()
    open_path = Path.open
    count = len(feed.calls)

    class Observed(BytesIO):
        def read(self, size=-1):
            assert size == len(original) + 1
            return super().read(size)

    def grow(path, mode="r", *args, **kwargs):
        if path == owner._marker and mode == "rb":
            grown = original + b"x" * 4096
            with open_path(path, "wb") as marker:
                marker.write(grown)
            return Observed(grown)
        return open_path(path, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", grow)
        with pytest.raises(ValueError, match="cache_owner_changed"):
            owner.acquire(offer)
    assert len(feed.calls) == count
    assert owner._marker.read_bytes() == original + b"x" * 4096
