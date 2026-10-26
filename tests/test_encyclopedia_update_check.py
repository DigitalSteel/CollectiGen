"""Synthetic signed feed through the existing transport; no real updater."""
from dataclasses import replace
import json

import pytest

from core.encyclopedia_update_check import EncyclopediaUpdateCheck, EncyclopediaUpdateObservation
from tests.test_component_acquisition import FakeFeed, service, ID, no_executors


def make_check(tmp_path, owner='standalone', version='0.1.0-alpha.7'):
    feed = FakeFeed()
    feed.add()
    acquisition = service(tmp_path, feed)
    observation = EncyclopediaUpdateObservation(version, owner, 'synthetic-original-root')
    return EncyclopediaUpdateCheck(acquisition, lambda: observation, channel='beta'), feed, observation


@pytest.mark.parametrize('owner,route', [('standalone', 'manual_package_review'), ('manager', 'manager')])
def test_signed_notification_routes_without_package_transfer(tmp_path, owner, route):
    check, feed, _ = make_check(tmp_path, owner)
    notice = check.check()
    assert notice.status == 'available' and notice.route == route
    assert notice.offer.release.component_id.value == ID
    assert notice.offer.release_notes.startswith('SIGNED:')
    assert not notice.production_install_authorized
    assert not any(url.endswith('.zip') for url, *_ in feed.calls)
    assert list(check.acquisition.root.iterdir()) == [check.acquisition._marker]


@pytest.mark.parametrize('owner,pending', [('unknown', False), ('conflict', False), ('manager', True)])
def test_ownership_or_recovery_hold_before_network(tmp_path, owner, pending):
    check, feed, observation = make_check(tmp_path, owner)
    check.observe = lambda: replace(observation, pending=pending)
    assert check.check().status == 'held'
    assert feed.calls == []


def test_changed_owner_discards_signed_notice(tmp_path):
    check, _, observation = make_check(tmp_path)
    observations = iter([observation, replace(observation, owner='manager')])
    check.observe = lambda: next(observations)
    assert check.check().status == 'held'
    assert check.notice.offer is None


@pytest.mark.parametrize('version', ['0.1.0-alpha.8', '0.1.0-alpha.9'])
def test_same_or_newer_install_not_offered(tmp_path, version):
    check, _, _ = make_check(tmp_path, version=version)
    assert check.check().status == 'not_found'


def test_empty_listing_does_not_claim_installed_current(tmp_path):
    check, feed, _ = make_check(tmp_path)
    feed.rows.clear()
    notice = check.check()
    assert notice.status == 'not_found'
    assert notice.reason == 'no_newer_release_found_on_channel'


def test_missing_component_does_not_claim_installed_current(tmp_path):
    check, feed, _ = make_check(tmp_path)
    feed.rows.clear()
    feed.add(component='collectigen.other-reader')
    assert check.check().status == 'not_found'


def test_stable_channel_catch_up_does_not_claim_installed_current(tmp_path):
    check, feed, _ = make_check(tmp_path, version='0.2.0-alpha.1')
    feed.rows.clear()
    feed.add(version='0.1.0')
    check.channel = 'stable'
    assert check.check().status == 'not_found'


def test_network_failure_clears_prior_notice_and_retry_is_explicit(tmp_path):
    check, feed, _ = make_check(tmp_path)
    assert check.check().status == 'available'
    feed.failure = lambda _: True
    assert check.check().status == 'unavailable'
    assert check.notice.offer is None
    feed.failure = None
    assert check.check().status == 'available'


def test_bad_signature_held_and_unsigned_notes_not_used(tmp_path):
    check, feed, _ = make_check(tmp_path)
    url = next(u for u in feed.resources if u.endswith('.sig'))
    signature = json.loads(feed.resources[url])
    signature['signature'] = 'AA=='
    feed.resources[url] = json.dumps(signature).encode()
    assert check.check().status == 'held'
    assert check.notice.offer is None


def test_cancel_clears_previous_notice(tmp_path):
    check, _, _ = make_check(tmp_path)
    check.check()
    with pytest.raises(InterruptedError):
        check.check(should_cancel=lambda: True)
    assert check.notice is None


def test_explicit_pinned_trust_required(tmp_path):
    check, feed, _ = make_check(tmp_path)
    check.acquisition.trust_pin = '0' * 64
    assert check.check().status == 'held'
    assert feed.calls == []


def test_notification_scopes_before_unrelated_linux_platform_admission(tmp_path):
    check, feed, _ = make_check(tmp_path)
    feed.add(component='collectigen.unrelated', platform=dict(os='linux', architecture='x86_64'))
    notice = check.check()
    assert notice.status == 'available' and notice.offer.release.component_id.value == ID
    assert not any('collectigen.unrelated' in url for url, *_rest in feed.calls)
    assert not any(url.endswith('.zip') for url, *_rest in feed.calls)


@pytest.mark.parametrize('fault', ['platform', 'signature'])
def test_scoped_notification_still_holds_invalid_target(tmp_path, fault):
    check, feed, _ = make_check(tmp_path)
    feed.rows.clear(); feed.resources.clear()
    base, _name = feed.add(**({'platform':dict(os='linux', architecture='x86_64')} if fault == 'platform' else {}))
    feed.add(component='collectigen.unrelated')
    if fault == 'signature': feed.resources[base + 'collectigen-release.json.sig'] = b'{}'
    notice = check.check()
    assert notice.status == 'held' and notice.offer is None
    assert not any('collectigen.unrelated' in url for url, *_rest in feed.calls)
    assert not any(url.endswith('.zip') for url, *_rest in feed.calls)


@pytest.mark.parametrize('stage',['alpha','beta','stable'])
def test_full_identity_notification_observation_with_contained_metadata(tmp_path,stage):
    from core.encyclopedia_update_check import EncyclopediaUpdateObservation
    from tests.test_component_acquisition import MaturityFeed,maturity_service
    observation=EncyclopediaUpdateObservation('0.4.0','standalone','original-full-snapshot',False,2,stage)
    feed=MaturityFeed();feed.add_maturity(version='0.5.0',stage=stage)
    acquisition,state=maturity_service(tmp_path,feed,context=observation.installed_context())
    check=EncyclopediaUpdateCheck(acquisition,lambda:observation,channel='stable' if stage=='stable' else 'beta')
    notice=check.check();assert notice.status=='available' and notice.offer.installed_context==observation.installed_context()
    assert not any(url.endswith('.zip') for url,*_ in feed.calls)


def test_same_number_stage_promotion_notification_and_snapshot_drift(tmp_path):
    from core.encyclopedia_update_check import EncyclopediaUpdateObservation
    from tests.test_component_acquisition import MaturityFeed,maturity_service
    first=EncyclopediaUpdateObservation('0.4.0','standalone','snapshot-1',False,2,'alpha')
    feed=MaturityFeed();feed.add_maturity(version='0.4.0',stage='beta')
    acquisition,state=maturity_service(tmp_path,feed,context=first.installed_context())
    check=EncyclopediaUpdateCheck(acquisition,lambda:first,channel='beta');assert check.check().status=='available'
    observations=iter([first,replace(first,identity='snapshot-2')]);check.observe=lambda:next(observations)
    assert check.check().status=='held' and check.notice.offer is None
    assert not any(url.endswith('.zip') for url,*_ in feed.calls)
