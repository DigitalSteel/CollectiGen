# Character and Outfit setups for Suite Manager

This source candidate connects Media Organizer a.0.5.0 with Suite Manager a.0.6.0. These versions still require their qualified package delivery; older published Manager builds do not provide this receiving workflow.

From an existing Character or Outfit, open its Generation Setup review. In **Image Suggestions**, explicitly check the eligible libraries to search, then **Find Suggested Details**. No image library is selected automatically for an Organizer transfer. Select the details to use, edit the setup, review it, and save the separate canonical setup.

Organizer then opens **Review Setup Export for Manager**. Prompt text starts excluded; include it only after reviewing what will leave this workflow. Cancel retains the saved setup. Choose a new CollectiGen JSON filename; an existing file is never overwritten. Source paths, image pixels, supporting-image review evidence and the in-session library receipt are not exported.

In Manager, choose **Applications > Use Saved Setup > Import CollectiGen File**, select the exported JSON, then open the imported setup. Review its changes, explicitly select prompt replacement when wanted, and **Apply Reviewed Setup**. This creates a separate image draft; no generation starts. Exact resources and a checked generator remain decisions for Manager.

Organizer rechecks the original eligible library identities and roles plus actually used source evidence before save and export. A changed scope refuses the transfer while preserving the saved setup and authored text. Local Only and unclassified image libraries are excluded from this transfer path. Ordinary all-in-one local authoring keeps its existing scope.

Owners: GEN-13 / GEN-08 / SUITE-10B, child `GEN-13/organizer-reviewed-setup-export`. Existing entry and callback: `qt_app/main_window.py::TagManagerWindow.open_character_outfit_setup`, `suite_core/media_organizer/entry.py::make_window_type`. Scope receipt and source recheck: `qt_app/generation_character_setup_dialog.py::CharacterOutfitSetupDialog`. Stock portable writer: `core/generation_saved_work.py`; receiving UI: `qt_app/generation_saved_work_dialog.py` and `qt_app/generation_recipe_dialog.py`. Focused public producer checks: `tests/test_organizer_character_setup_export.py`.
