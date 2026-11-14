"""Selection cleared by a settled Dataset filter must not expose stale actions."""
from pathlib import Path
from types import SimpleNamespace
import pytest
from qt_app.dataset_page import DatasetPage

class Pane:
    def __init__(self):self.visible=True
    def setVisible(self,value):self.visible=bool(value)
    def isVisible(self):return self.visible
    def show(self):self.visible=True

def page(width):
    identity=object();caption=object();current=object()
    return SimpleNamespace(width=lambda:width,_dataset_narrow_layout=width<1100,
        workflow_tabs=SimpleNamespace(currentWidget=lambda:current),
        image_identity_correction=identity,training_caption_projection=caption,
        current_path=Path("synthetic-selected.png"),_narrow_inspector_open=True,
        _summary_visible=True,_has_dataset_images=True,
        dataset=SimpleNamespace(roots=(Path("synthetic-library"),)),
        header=SimpleNamespace(set_compact_mode=lambda value:None),
        preparation_controls=SimpleNamespace(summary=SimpleNamespace(setWordWrap=lambda value:None)),
        _sync_gallery_toolbar_layout=lambda value:None,inspector=Pane(),browser=Pane(),
        inspector_back_button=Pane(),inspect_current_button=Pane())

@pytest.mark.parametrize("width",[1920,980])
def test_filter_removal_hides_inspector_action_after_layout_sync(width):
    view=page(width)
    DatasetPage._sync_adaptive_panes(view)
    assert view.inspector.visible
    # Saving can move the sole image out of the active preparation-stage filter.
    # The existing settled gallery owns clearing that selection.
    view.current_path=None
    DatasetPage._sync_adaptive_panes(view)
    assert not view.inspector.visible
    assert not view.inspector_back_button.visible
    assert not view.inspect_current_button.visible
    assert view.browser.visible
    # Choosing the saved image in its new stage restores the ordinary inspector.
    view.current_path=Path("synthetic-saved.png")
    DatasetPage._sync_adaptive_panes(view)
    assert view.inspector.visible

@pytest.mark.parametrize("width",[1920,980])
def test_selected_image_and_summary_preference_survive_layout_sync(width):
    view=page(width);selected=view.current_path
    view._summary_visible=False
    DatasetPage._sync_adaptive_panes(view)
    assert not view.inspector.visible and view.current_path==selected
    view._summary_visible=True
    DatasetPage._sync_adaptive_panes(view)
    assert view.inspector.visible and view.current_path==selected
