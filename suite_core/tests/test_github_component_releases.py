import json
from urllib.parse import quote

import pytest
from suite_core.installation import ComponentId
from suite_core.discovery.github_component_releases import (
    discover_releases, discover_candidates, release_asset_identity, discover_component_ids,
    DiscoveryHold, REPOSITORY, MAX_BYTES,
)

READER = ComponentId("collectigen.tag-encyclopedia")


def release(version, component=READER.value, *, draft=False, names=("reader-linux-x86_64.tar.gz",)):
    tag = component + "/v" + version
    return dict(tag_name=tag, draft=draft, prerelease="-" in version, assets=[dict(
        name=name, size=123, browser_download_url=
        f"https://github.com/{REPOSITORY}/releases/download/{quote(tag, safe='')}/{quote(name, safe='')}") for name in names])


def discover(rows, **options):
    return discover_releases(json.dumps(rows).encode(), component_id=READER,
        channel=options.pop("channel", "beta"), current_version=options.pop("current_version", "0.1.0-alpha.5"),
        complete=options.pop("complete", True), **options)


def test_scoped_prerelease_semver_and_draft():
    rows = [release("0.1.0-alpha.9"), release("0.1.0-alpha.10"),
            release("9.0.0-alpha.1", "collectigen.suite-manager"), release("1.0.0-alpha.1", draft=True),
            dict(tag_name="v9.0.0-alpha.1"), release("0.2.0")]
    result = discover(rows)
    assert result.candidate.software_version == "0.1.0-alpha.10"
    assert result.state == "verification_required"
    assert result.candidate.production_install_authorized is False
    assert discover(list(reversed(rows))) == result


def test_all_os_assets_preserved_without_platform_authority():
    names = ("reader-windows-x86_64.zip", "reader-linux-x86_64.tar.gz", "component-release.json.sig")
    candidate = discover([release("0.1.0-alpha.6", names=names)]).candidate
    assert tuple(a.name for a in candidate.assets) == tuple(sorted(names))
    assert candidate.production_install_authorized is False


@pytest.mark.parametrize("rows,current,state", [
    ([], "0.1.0-alpha.5", "waiting_for_stable_catch_up"),
    ([release("0.0.9")], "0.1.0-alpha.5", "waiting_for_stable_catch_up"),
    ([release("0.1.0-alpha.6")], "0.1.0-alpha.5", "waiting_for_stable_catch_up"),
    ([], "0.1.0", "no_newer_release"),
    ([release("0.1.0")], "0.1.0", "no_newer_release"),
])
def test_empty_stable_or_wait_without_downgrade(rows, current, state):
    result = discover(rows, channel="stable", current_version=current)
    assert result.candidate is None and result.state == state


def test_stable_catches_up():
    assert discover([release("0.1.0")], channel="stable").candidate.software_version == "0.1.0"


def test_channel_switch_changes_only_offers_without_io(monkeypatch):
    import builtins
    import socket
    from pathlib import Path
    def deny(*args, **kwargs):
        raise AssertionError("discovery cannot perform I/O or install")
    monkeypatch.setattr(builtins, "open", deny)
    monkeypatch.setattr(Path, "write_bytes", deny)
    monkeypatch.setattr(Path, "write_text", deny)
    monkeypatch.setattr(socket, "socket", deny)
    rows = [release("0.1.0-alpha.6"), release("0.0.9")]
    raw = json.dumps(rows).encode()
    before = raw
    beta = discover_releases(raw, component_id=READER, channel="beta", current_version="0.1.0-alpha.5", complete=True)
    stable = discover_releases(raw, component_id=READER, channel="stable", current_version="0.1.0-alpha.5", complete=True)
    assert beta.candidate.production_install_authorized is False
    assert stable.candidate is None and stable.state == "waiting_for_stable_catch_up"
    assert raw == before
    assert discover_releases(raw, component_id=READER, channel="beta", current_version="0.1.0-alpha.5", complete=True) == beta


@pytest.mark.parametrize("mutate,reason", [
    (lambda r: r.update(prerelease=False), "tag_channel_mismatch"),
    (lambda r: r.update(draft=0), "release_flags"),
    (lambda r: r.update(assets=[]), "release_assets_missing"),
    (lambda r: r["assets"][0].update(name="../escape"), "asset_basename"),
    (lambda r: r["assets"][0].update(size=True), "asset_size"),
    (lambda r: r["assets"][0].update(browser_download_url="https://github.com/other/repo/x"), "asset_origin_binding"),
    (lambda r: r["assets"].append(r["assets"][0].copy()), "duplicate_asset"),
])
def test_bad_metadata_holds(mutate, reason):
    row = release("0.1.0-alpha.6")
    mutate(row)
    with pytest.raises(DiscoveryHold, match=reason):
        discover([row])


def test_incomplete_and_duplicate_release_hold():
    with pytest.raises(DiscoveryHold, match="complete_release_listing_required"):
        discover([], complete=False)
    row = release("0.1.0-alpha.6")
    with pytest.raises(DiscoveryHold, match="duplicate_component_tag"):
        discover([row, row])


@pytest.mark.parametrize("raw", [b'[{"tag_name":"x","tag_name":"y"}]', b'[NaN]', b'{}', b'\xff', b'[' * 2000, b' ' * (MAX_BYTES + 1)],
                         ids=["duplicate-keys", "nonfinite", "wrong-root", "non-utf8", "deep-json", "oversized"])
def test_raw_input_holds(raw):
    with pytest.raises(DiscoveryHold):
        discover_releases(raw, component_id=READER, channel="beta", current_version="0.1.0-alpha.5", complete=True)


def test_equal_or_older_beta_not_offered_and_no_other_component():
    assert discover([release("0.1.0-alpha.5"), release("0.1.0-alpha.4")]).candidate is None
    assert discover([release("1.0.0-alpha.1", "collectigen.suite-manager")]).candidate is None


@pytest.mark.parametrize("version", ["0.1.0-alpha.06", " 0.1.0-alpha.6", "0.1.0-alpha.6+build"])
def test_noncanonical_version_holds(version):
    with pytest.raises(DiscoveryHold):
        discover([release(version)])


@pytest.mark.parametrize("version", ["0.1.0-alpha.\u0666", "0.1\u0660.0-alpha.6"])
def test_unicode_numeric_version_cannot_alias_ascii_or_depend_on_order(version):
    rows = [release("0.1.0-alpha.6"), release(version)]
    for order in (rows, list(reversed(rows))):
        with pytest.raises(DiscoveryHold, match="exact_version"):
            discover(order)
    with pytest.raises(DiscoveryHold, match="exact_version"):
        discover([], current_version=version)


def test_oversized_json_integer_is_a_discovery_hold():
    message = b'[{"ignored_number":' + b'9' * 5000 + b'}]'
    with pytest.raises(DiscoveryHold, match="metadata_syntax"):
        discover_releases(message, component_id=READER, channel="beta", current_version="0.1.0-alpha.5", complete=True)


@pytest.mark.parametrize("size", [1.0, "123", -1, 0, 2**40 + 1])
def test_adjacent_asset_numeric_admission(size):
    row = release("0.1.0-alpha.6")
    row["assets"][0]["size"] = size
    with pytest.raises(DiscoveryHold, match="asset_size"):
        discover([row])


def test_owner_rename_binds_new_canonical_url_and_rejects_old_origin():
    assert REPOSITORY == "DigitalSteel/CollectiGen"
    row = release("0.1.0-alpha.6")
    assert discover([row]).candidate.assets[0].url.startswith("https://github.com/DigitalSteel/CollectiGen/")
    row["assets"][0]["browser_download_url"] = row["assets"][0]["browser_download_url"].replace("/DigitalSteel/", "/DigitalSteel7/")
    with pytest.raises(DiscoveryHold, match="asset_origin_binding"):
        discover([row])


def test_actual_published_metadata_literal_tag_slash_is_preserved():
    # Observed anonymous metadata for release 410022292 on 2026-10-12.
    tag = "collectigen.tag-encyclopedia/v0.1.0-alpha.8"
    names = {"TagEncyclopedia-a.0.1.8-windows-x64.zip": 38380886,
             "collectigen-release.json": 1286, "collectigen-release.json.sig": 731}
    row = dict(tag_name=tag, draft=False, prerelease=True, assets=[dict(name=n, size=s,
        browser_download_url=f"https://github.com/{REPOSITORY}/releases/download/{tag}/{n}")
        for n, s in names.items()])
    candidate = discover([row]).candidate
    assert candidate.software_version == "0.1.0-alpha.8"
    assert {a.url for a in candidate.assets} == {a['browser_download_url'] for a in row['assets']}
    assert not candidate.production_install_authorized


@pytest.mark.parametrize("delimiter", ["/", "%2F", "%2f"])
def test_equivalent_validated_tag_delimiters_keep_exact_identity(delimiter):
    row = release("0.1.0-alpha.8", names=("reader.zip",))
    url = row['assets'][0]['browser_download_url'].replace('%2F', delimiter)
    row['assets'][0]['browser_download_url'] = url
    assert discover([row]).candidate.assets[0].url == url


@pytest.mark.parametrize("suffix", [
    "collectigen.tag-encyclopedia%252Fv0.1.0-alpha.8/reader.zip",
    "collectigen.tag-encyclopedia/v0.1.0-alpha.8/reader%2Fextra.zip",
    "collectigen.tag-encyclopedia/v0.1.0-alpha.8/reader%252Fextra.zip",
    "collectigen.tag-encyclopedia/v0.1.0-alpha.8/../reader.zip",
    "collectigen.tag-encyclopedia/v0.1.0-alpha.8/%2e%2e",
    "collectigen.tag-encyclopedia/v0.1.0-alpha.8/reader%5Cextra.zip",
    "collectigen.tag-encyclopedia/v0.1.0-alpha.8/reader%255Cextra.zip",
    "collectigen.tag-encyclopedia/v0.1.0-alpha.8//reader.zip",
    "collectigen.tag-encyclopedia%2Fv0.1.0-alpha.8/extra/reader.zip",
    "collectigen.tag-encyclopedia/v0.1.0-alpha.9/reader.zip",
    "collectigen.suite-manager/v0.1.0-alpha.8/reader.zip",
    "collectigen.tag-encyclopedia/v0.1.0-alpha.8/%72eader.zip",
    "collectigen.tag-encyclopedia/v0.1.0-alpha.8/reader.zip?other=1",
    "collectigen.tag-encyclopedia/v0.1.0-alpha.8/reader.zip#fragment",
])
def test_slash_support_does_not_admit_ambiguous_or_different_fields(suffix):
    row = release("0.1.0-alpha.8", names=("reader.zip",))
    row['assets'][0]['browser_download_url'] = f"https://github.com/{REPOSITORY}/releases/download/{suffix}"
    with pytest.raises(DiscoveryHold, match="asset_origin_binding"):
        discover([row])


def test_declared_percent_escape_basename_cannot_mask_double_encoding():
    row = release("0.1.0-alpha.8", names=("reader%2Fextra.zip",))
    with pytest.raises(DiscoveryHold, match="asset_basename"):
        discover([row])


def maturity_release(version='0.4.0', maturity='alpha', *, component=READER.value):
    from hashlib import sha256
    tag=f'{component}/releases/schema2/{version}/{maturity}'
    rid=int(sha256(tag.encode()).hexdigest()[:10],16)
    return dict(id=rid, tag_name=tag, draft=False, prerelease=maturity!='stable', assets=[dict(
        id=rid*100+1, name='reader.zip', size=123, browser_download_url=
        f'https://github.com/{REPOSITORY}/releases/download/{quote(tag,safe="")}/reader.zip')])


def candidates(rows, **options):
    defaults=dict(component_id=READER, channel='beta', complete=True, include_schema2=True)
    defaults.update(options)
    return discover_candidates(json.dumps(rows).encode(),**defaults)


def test_schema2_retains_all_same_number_stages_without_authentication_or_winner():
    rows=[maturity_release(maturity='beta'),maturity_release(),maturity_release('0.3.0'),release('0.2.0-alpha.1')]
    result=candidates(rows)
    assert [(r.software_version,r.maturity,r.schema_version) for r in result]==[
        ('0.4.0','beta',2),('0.4.0','alpha',2),('0.3.0','alpha',2),('0.2.0-alpha.1',None,1)]
    assert all(not r.production_install_authorized for r in result)
    assert set(candidates(list(reversed(rows))))==set(result)
    assert candidates([maturity_release(maturity='stable')],channel='stable')[0].maturity=='stable'


def test_schema2_default_skip_and_legacy_all_component_compatibility():
    row=maturity_release();old=release('0.3.1-alpha.1')
    assert discover_component_ids(json.dumps([row,old]).encode(),complete=True)==(READER,)
    assert discover([row,old]).candidate.software_version=='0.3.1-alpha.1'
    assert candidates([row,old],include_schema2=False)==(discover([old]).candidate,)
    legacy=discover([old]).candidate
    assert (legacy.schema_version,legacy.maturity)==(1,None)
    url=row['assets'][0]['browser_download_url']
    with pytest.raises(DiscoveryHold):release_asset_identity(url)
    assert release_asset_identity(url,include_schema2=True)==(row['tag_name'],'reader.zip')


@pytest.mark.parametrize('version,stage',[('0.4.0-alpha.1','alpha'),('a.0.4.0','alpha'),('0.4.0+build1','alpha'),
    (' 0.4.0','alpha'),('0.4.\u0660','alpha'),('00.4.0','alpha'),('0.4.0','Alpha'),('0.4.0','preview')])
def test_schema2_noncanonical_tag_fields_hold(version,stage):
    with pytest.raises(DiscoveryHold,match='schema2_tag_identity'):candidates([maturity_release(version,stage)])


@pytest.mark.parametrize('suffix',['releases/schema2/0.4.0/alpha/extra','releases/schema3/0.4.0/alpha',
    'releases/schema2//alpha','releases/schema2/0.4.0/../alpha','releases/schema2/0.4.0/%61lpha'])
def test_selected_reserved_namespace_is_not_silently_normalized(suffix):
    row=maturity_release();row['tag_name']=READER.value+'/'+suffix
    with pytest.raises(DiscoveryHold):candidates([row])


@pytest.mark.parametrize('change,reason',[
    (lambda r:r.update(prerelease=False),'tag_channel_mismatch'),
    (lambda r:r.update(draft=0),'release_flags'),
    (lambda r:r.update(id=None),'release_id'),(lambda r:r.update(id=True),'release_id'),
    (lambda r:r.update(id=2**63),'release_id'),(lambda r:r.update(assets=[]),'release_assets_missing'),
    (lambda r:r.update(assets=r['assets']*33),'asset_count'),
    (lambda r:r['assets'][0].update(id=None),'asset_id'),
    (lambda r:r['assets'][0].update(id=True),'asset_id'),
    (lambda r:r['assets'][0].update(id=2**63),'asset_id'),
    (lambda r:r['assets'][0].update(size=True),'asset_size'),
    (lambda r:r['assets'][0].update(name='../escape'),'asset_basename'),
    (lambda r:r['assets'][0].update(name='x'*256),'asset_basename'),
    (lambda r:r['assets'].append(dict(r['assets'][0])),'duplicate_asset'),
])
def test_schema2_metadata_bounds_and_ids(change,reason):
    row=maturity_release();change(row)
    with pytest.raises(DiscoveryHold,match=reason):candidates([row])


def test_schema2_duplicate_tag_release_and_cross_release_asset_ids_hold():
    row=maturity_release()
    with pytest.raises(DiscoveryHold,match='duplicate_component_tag'):candidates([row,row])
    for key in ('release','asset'):
        other=maturity_release(maturity='beta')
        if key=='release':other['id']=row['id']
        else:other['assets'][0]['id']=row['assets'][0]['id']
        with pytest.raises(DiscoveryHold,match='duplicate_'+('release_id' if key=='release' else 'asset_id')):
            candidates([row,other])


@pytest.mark.parametrize('mutation',['literal','lower_hex','double_tag','extra','basename_separator','double_basename',
    'host','repository','query','fragment','length','wrong_tag','wrong_name','http'])
def test_schema2_urls_require_one_exact_encoded_tag_segment(mutation):
    row=maturity_release();url=row['assets'][0]['browser_download_url']
    if mutation=='literal':url=url.replace('%2F','/')
    elif mutation=='lower_hex':url=url.replace('%2F','%2f')
    elif mutation=='double_tag':url=url.replace('%2F','%252F')
    elif mutation=='extra':url=url.replace('/reader.zip','/extra/reader.zip')
    elif mutation=='basename_separator':url=url.replace('reader.zip','reader%2Fextra.zip')
    elif mutation=='double_basename':url=url.replace('reader.zip','reader%252Fextra.zip')
    elif mutation=='host':url=url.replace('github.com','evil.example')
    elif mutation=='repository':url=url.replace('DigitalSteel','DigitalSteel7')
    elif mutation=='query':url+='?x=1'
    elif mutation=='fragment':url+='#x'
    elif mutation=='length':url+='x'*2049
    elif mutation=='wrong_tag':url=url.replace('0.4.0','0.4.1')
    elif mutation=='wrong_name':url=url.replace('reader.zip','other.zip')
    else:url=url.replace('https:','http:')
    row['assets'][0]['browser_download_url']=url
    with pytest.raises(DiscoveryHold,match='asset_origin_binding'):candidates([row])


def test_schema2_draft_other_scope_and_other_feed_do_not_become_candidates():
    draft=maturity_release();draft.update(draft=True,assets='not inspected')
    other=maturity_release(component='collectigen.suite-manager');other['assets']='not inspected'
    assert candidates([draft,other,maturity_release(maturity='stable')])==()


@pytest.mark.parametrize('option',[False,1,None])
def test_candidate_collection_requires_complete_listing(option):
    with pytest.raises(DiscoveryHold,match='complete_release_listing_required'):candidates([],complete=option)


def test_candidate_collection_metadata_release_and_component_bounds():
    with pytest.raises(DiscoveryHold,match='release_count'):candidates([{}]*101)
    with pytest.raises(DiscoveryHold,match='metadata_size'):
        discover_candidates(b' '*(MAX_BYTES+1),component_id=READER,channel='beta',complete=True,include_schema2=True)
    with pytest.raises(DiscoveryHold,match='component_count'):
        discover_component_ids(json.dumps([release('0.1.0-alpha.1',f'collectigen.component{i}') for i in range(65)]).encode(),complete=True)


@pytest.mark.parametrize('option',[1,'yes',None])
def test_schema2_optin_requires_bool(option):
    with pytest.raises(DiscoveryHold,match='explicit_schema2_optin'):candidates([],include_schema2=option)
    with pytest.raises(DiscoveryHold,match='explicit_schema2_optin'):
        release_asset_identity(maturity_release()['assets'][0]['browser_download_url'],include_schema2=option)


def test_schema2_candidate_collection_is_pure(monkeypatch):
    import builtins,socket
    from pathlib import Path
    message=json.dumps([maturity_release()]).encode()
    def deny(*a,**k):raise AssertionError('pure locator I/O forbidden')
    monkeypatch.setattr(builtins,'open',deny);monkeypatch.setattr(socket,'socket',deny)
    monkeypatch.setattr(Path,'read_bytes',deny);monkeypatch.setattr(Path,'write_bytes',deny)
    assert discover_candidates(message,component_id=READER,channel='beta',complete=True,include_schema2=True)


def test_optin_url_parsing_does_not_activate_stock_transport():
    from core.component_release_transport import ReleaseTransportPolicy,TransportHold
    policy=ReleaseTransportPolicy();row=maturity_release();url=row['assets'][0]['browser_download_url']
    assert release_asset_identity(url,include_schema2=True)==(row['tag_name'],'reader.zip')
    with pytest.raises(TransportHold,match='first_party'):policy.admit_initial(url)
    assert policy.admit_initial(release('0.3.1-alpha.1')['assets'][0]['browser_download_url'])=='asset'
