"""Regression for the actual recovery wrapper dropping inherited catalog commands."""
from types import SimpleNamespace
import pytest
from qt_app.command_bar import UiCommand
from suite_core.media_organizer.entry import make_window_type
from suite_core.media_organizer.organizer_recovery_entry import with_dataset_recovery


@pytest.mark.parametrize("index", [0,1,2,4,6])
def test_final_recovery_wrapper_preserves_prompt_catalog_and_existing_commands(index):
    calls=[]
    class Base:
        def workspace_extra_commands(self, selected):
            calls.append(selected)
            return [UiCommand("prior:command", "Existing action", "View")]
    selected=[]
    Window=with_dataset_recovery(make_window_type(Base,current_application=lambda:None))
    # Command-composition unit fixture; actual native startup is checked separately.
    window=object.__new__(Window)
    window.navigation=SimpleNamespace(setCurrentRow=selected.append)
    commands=window.workspace_extra_commands(index)
    keys=[item.key for item in commands]
    assert calls==[index] and keys.count("prior:command")==1
    catalog=next(item for item in commands if item.key=="organizer:prompt-catalog")
    assert catalog.available() and catalog.group=="Knowledge"
    catalog.invoke()
    assert selected==[4]  # Canonical internal workspace only; no Reader/session action.
    assert ("organizer:package-recovery" in keys)==(index==2)
