"""Retained terminal identity must survive a process recovery notice."""
from types import SimpleNamespace
import hashlib
import pytest
from core import training_run_recovery as recovery
from core.training_process import TrainingProcessResult, TrainingProcessService, TrainingRunJournal, _bytes
from core.training_run_index import TrainingRunIndexError


def stopped_case(tmp_path, monkeypatch, code, message):
    destination = tmp_path / 'run'
    destination.mkdir()
    (destination / 'events').mkdir()
    plan = SimpleNamespace(destination=destination)
    receipt = {'generated': True}
    monkeypatch.setattr(TrainingProcessService, '_receipt', staticmethod(lambda _: receipt))
    journal = TrainingRunJournal(destination)
    tip = journal.append('reserved', {'plan': receipt, 'fingerprint': hashlib.sha256(_bytes(receipt)).hexdigest()}, '')
    tip = journal.append('starting', {}, tip)
    tip = journal.append('running', {}, tip)
    tip = journal.append('exited_unverified', {'tree_stopped': True, 'exit_code': code, 'message': message}, tip)
    calls = []
    def recover(path, expected):
        calls.append((path, expected))
        return TrainingProcessResult('exited_unverified', destination, 'Recovery retained all evidence.', tree_stopped=True, journal_tip=tip)
    return plan, SimpleNamespace(recover=recover), tip, calls


@pytest.mark.parametrize('code,message', [(0, 'Original successful exit.'), (2, 'Original failed exit.')])
def test_recovery_preserves_recorded_terminal_message_and_exit(tmp_path, monkeypatch, code, message):
    plan, process, tip, calls = stopped_case(tmp_path, monkeypatch, code, message)
    result = recovery.recover_plan_process(plan, process, tip)
    assert result.message == message and result.exit_code == code
    assert result.tree_stopped and result.journal_tip == tip
    assert calls == [(plan.destination, tip)]


def test_malformed_terminal_message_is_refused(tmp_path, monkeypatch):
    plan, process, tip, _ = stopped_case(tmp_path, monkeypatch, 0, {'invalid': True})
    with pytest.raises(TrainingRunIndexError, match='message'):
        recovery.recover_plan_process(plan, process, tip)


def test_changed_tip_is_refused_before_process_recovery(tmp_path, monkeypatch):
    plan, process, _, calls = stopped_case(tmp_path, monkeypatch, 0, 'Original successful exit.')
    with pytest.raises(TrainingRunIndexError, match='differs'):
        recovery.recover_plan_process(plan, process, 'f' * 64)
    assert not calls
