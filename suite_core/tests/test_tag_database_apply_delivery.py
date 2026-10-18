"""Saved Apply delivery; original worker/receipt lifecycle with transaction doubles."""
from threading import Event
from time import monotonic,sleep
from types import SimpleNamespace
from suite_core.manager import tag_database_controller as module
from suite_core.manager.resource_executor import ResourceExecutorOwner
from suite_core.knowledge.tag_transactions import TagCatalogTransactions
from suite_core.database_actions import CatalogRelease


def stock_choice(tmp_path,apply):
    transaction=object.__new__(TagCatalogTransactions)
    transaction.session=SimpleNamespace(binding='original-binding')
    transaction.apply=apply
    release=object.__new__(CatalogRelease)
    return module.TagDatabaseChoice('Synthetic selected catalog',transaction,release,None,tmp_path/'inert.zip',False,
        capability_inventory=(('old','pin'),),capability_revision='old-revision')


def drain(controller,executor):
    end=monotonic()+3
    while executor.close_held and monotonic()<end:sleep(.002)
    assert not executor.close_held
    assert controller.poll()


def test_saved_apply_defers_optional_report_and_preserves_single_worker_close(tmp_path,monkeypatch):
    entered=Event();release=Event();calls=[]
    saved=SimpleNamespace(publication_error='',publication=None)
    def apply(review,*,confirmed_token):
        calls.append((review,confirmed_token));entered.set();assert release.wait(3);return saved
    choice=stock_choice(tmp_path,apply)
    monkeypatch.setattr(module,'_acquire_foundation_choice',lambda c:c)
    executor=ResourceExecutorOwner();controller=module.TagDatabaseController(choices=(choice,),executor=executor,apply_authorized=True)
    review=SimpleNamespace(can_apply=True,token='exact-review');controller.review=review
    try:
        assert controller.request('apply',confirmed=True) and entered.wait(1)
        assert not controller.request('apply',confirmed=True)
        assert controller.close_held and executor.close_held
        controller.begin_close();assert controller.close_held and executor.close_held
        release.set();drain(controller,executor)
        assert calls==[(review,'exact-review')]
        assert controller.result is saved
        assert controller.choice.capability_report is None
        assert controller.choice.capability_inventory==() and controller.choice.capability_revision==''
        assert 'refresh observations' in controller.choice.capability_error
        assert 'saved outcome is retained' in controller.reason
        assert not controller.close_held
    finally:release.set();executor.begin_close()


def test_post_save_observation_failure_retains_result_without_replay(tmp_path,monkeypatch):
    saved=SimpleNamespace(publication_error='',publication=None);calls=[];observations=[]
    def apply(review,*,confirmed_token):calls.append(confirmed_token);return saved
    choice=stock_choice(tmp_path,apply);monkeypatch.setattr(module,'_acquire_foundation_choice',lambda c:c)
    original=module._observe
    def observe(c,*,include_capabilities=True):
        observations.append(include_capabilities)
        if len(observations)==1:raise RuntimeError('synthetic observation failure after saved native result')
        return original(c,include_capabilities=include_capabilities)
    monkeypatch.setattr(module,'_observe',observe)
    executor=ResourceExecutorOwner();controller=module.TagDatabaseController(choices=(choice,),executor=executor,apply_authorized=True)
    controller.review=SimpleNamespace(can_apply=True,token='exact-review')
    try:
        assert controller.request('apply',confirmed=True);drain(controller,executor)
        assert observations==[False,False] and calls==['exact-review']
        assert controller.result is saved and controller.choice.capability_report is None
        assert 'do not apply again' in controller.reason
        assert not controller.request('apply',confirmed=True)
    finally:executor.begin_close()


def test_explicit_observation_still_requests_capability_verification(tmp_path,monkeypatch):
    calls=[];choice=stock_choice(tmp_path,lambda *a,**kw:None)
    original=module._observe
    def observe(c,*,include_capabilities=True):calls.append(include_capabilities);return original(c,include_capabilities=include_capabilities)
    monkeypatch.setattr(module,'_observe',observe)
    executor=ResourceExecutorOwner();controller=module.TagDatabaseController(choices=(choice,),executor=executor)
    try:
        assert controller.request('observe');drain(controller,executor)
        assert calls==[True]
    finally:executor.begin_close()


def test_failed_write_and_failed_observation_invalidate_cached_capability_evidence(tmp_path,monkeypatch):
    calls=[]
    def apply(review,*,confirmed_token):
        calls.append(confirmed_token);raise RuntimeError('synthetic write refused before commit')
    choice=stock_choice(tmp_path,apply)
    monkeypatch.setattr(module,'_acquire_foundation_choice',lambda c:c)
    def observe(c,*,include_capabilities=True):raise RuntimeError('synthetic observation hold')
    monkeypatch.setattr(module,'_observe',observe)
    executor=ResourceExecutorOwner();controller=module.TagDatabaseController(choices=(choice,),executor=executor,apply_authorized=True)
    controller.review=SimpleNamespace(can_apply=True,token='exact-review')
    try:
        assert controller.request('apply',confirmed=True);drain(controller,executor)
        assert calls==['exact-review'] and controller.result is None
        assert controller.choice.observation_status=='unknown'
        assert controller.choice.capability_inventory==() and controller.choice.capability_revision==''
        assert 'observation hold' in controller.error and not controller.can_apply
    finally:executor.begin_close()
