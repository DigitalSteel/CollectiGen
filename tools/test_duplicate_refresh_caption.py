"""Generated refresh error/result recovery; no physical images or store writes."""
import pytest
from qt_app.theme import apply_theme
from test_duplicate_review_write_custody_ui import make
import test_duplicate_recycle_accounting_ui as accounting
from duplicate_custody_test_support import qt_test_scope,wait_for,capture


@pytest.mark.parametrize('theme',['light','dark'])
@pytest.mark.parametrize('mode',['','preflight'])
def test_refresh_instruction_matches_actual_action(qt_test_scope,monkeypatch,theme,mode):
    from PySide6.QtWidgets import QApplication
    apply_theme(QApplication.instance(),theme)
    monkeypatch.setattr(accounting,'make',lambda scope,patch,ignored:make(scope,patch,mode))
    o=accounting.setup(qt_test_scope,monkeypatch,'refresh')
    try:
        expected='Refresh Frozen Scope' if mode else 'Find Duplicates'
        assert o.d.search.text()==expected
        accounting.submit(o);wait_for(o.boundary.is_set)
        original=o.d.state.duplicates.regroup
        def failure(*args):raise OSError('Generated result refresh failure')
        o.d.state.duplicates.regroup=failure
        accounting.settle(o)
        result=o.d._recycle_outcome
        assert len(result.moved)==2 and not result.unconfirmed and not result.unattempted
        assert o.d._recycle_refresh_errors and not o.d.resolve.isEnabled()
        assert f'choose {expected} before resolving more.' in o.d.search_status.text()
        assert 'Confirmed moved: 2' in o.d.search_status.text()
        capture(o.d,'refresh-caption',f'{mode or "normal"}-{theme}')
        o.d.state.duplicates.regroup=original
        o.d._search_complete(o.report)
        assert not o.d._recycle_refresh_errors
        assert o.d._recycle_outcome is result
        assert len(o.calls)==2 and len(o.changed)==1
    finally:accounting.close(o)


# Reuse existing failure-stage coverage and successful-render guard, without
# repeating the unchanged full accepted-result/accounting campaign.
from test_duplicate_recycle_accounting_ui import (
    test_refresh_failure_retains_and_notifies_known_receipts_once,
    test_refresh_hold_clears_only_after_successful_fresh_render,
)
