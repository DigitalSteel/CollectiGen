"""Memory-only stock controller before-mutation ordering."""
from concurrent.futures import Future
from types import SimpleNamespace
from suite_core.manager.tag_database_controller import TagDatabaseController,TagDatabaseChoice


def test_selection_away_back_and_replacement_invalidate_before_mutation():
    seen=[]
    controller=TagDatabaseController(before_baseline_change=lambda:seen.append(
        (controller.index,controller.choices)))
    first,second=object(),object()
    controller.choices=(first,second);controller.index=0
    assert controller.select(1) and controller.select(0)
    replacement=object.__new__(TagDatabaseChoice)  # exact-type inert choice; no native service called
    controller.replace_choice(0,replacement)
    assert [row[0] for row in seen[:2]]==[0,1]
    assert seen[2][1]==(first,second)
    assert controller.choices[0] is replacement
    assert len(seen)==4  # replacement invalidates before both its mutation and select


def test_connection_request_and_completion_invalidate_before_delivery(tmp_path):
    events=[];futures=[]
    class Executor:
        def submit(self,*args):
            events.append('submit');future=Future();futures.append(future);return future
    controller=TagDatabaseController(executor=Executor(),settings_path=tmp_path/'settings.json',
        before_baseline_change=lambda:events.append('invalidate'))
    assert controller.connect('connection_open')
    assert events==['invalidate','submit']
    choice=SimpleNamespace(transactions=SimpleNamespace(session=SimpleNamespace(binding=object())),installed=False,
        observation_status="supplied",recovery_receipts=(),native_adapter=None)
    futures[-1].set_result(SimpleNamespace(choice=choice))
    assert controller.poll()
    assert events==['invalidate','submit','invalidate']
    controller.discard_connection()
    assert controller.request('observe')
    assert events[-2:]==['invalidate','submit']
    futures[-1].set_result(None)
    assert controller.poll() and events[-1]=='invalidate'
