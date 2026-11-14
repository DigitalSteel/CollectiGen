# Manager image-derived Setup and description settings

Unreleased GEN-06 / SUITE-10B source; not in the Manager a.0.7.5 download.

In Manager **Generate > Create Generation Setup**, choose manual authoring or
**Start from one image**. The image choice uses the existing one-image draft
workflow; it preserves other open drafts and never starts analysis automatically.
Review recorded settings, saved text and optional image suggestions in the
retained setup review.

If optional image suggestions fail, **Review Local Description Setup** opens
Manager's local caption settings. Choose the existing JoyCaption or Florence-2
adapter and the models parent folder. Advanced controls match the original
Setup page's description settings. This dialog does not inspect model contents,
install components, benchmark, start inference or change a generator connection.
Existing component installation remains in Organizer Setup; the same installed
models folder can be reused here.

**Save and Return** uses the canonical settings writer and updates the same
in-memory Settings object used by the retained review. Unknown configuration
fields, shared settings authority, backups and readback/rollback stay with that
writer. A changed file requires Reload Saved Choices before saving. **Cancel**
leaves settings unchanged. The settings dialog holds normal Manager Close.

Returning preserves the setup name, prompt-part edits, selected suggestion goal
when supported and original image draft. The original source recheck runs before
suggestions continue. Changed source bytes disable Suggest and Save, retaining
unsaved choices so the user can reselect the image. Returning never automatically
retries failed analysis. Ended/replaced sessions cannot save or resume a review.

Owners: `core/manager_description_settings.py` validates and merges only caption
fields through AppSettings; `suite_core/manager/description_setup_ui.py` presents
those choices. `suite_core/manager/window.py` owns the modal lifetime and current
session; `qt_app/generation_setup_source_dialog.py` supplies the existing source
choice; `qt_app/generation_workflow.py` connects the host-specific return.
`qt_app/generation_image_setup_dialog.py::ImageGenerationSetupDialog.resume_description_setup`
is the shared source-recheck/return behavior for Manager and the original shell.

Final source checks passed: eight persistence cases and eight offscreen UI cases. Native packaged
acceptance, paired package delivery and full-suite parity remain separate/open.
Reader/C3A, model/trainer execution and retained fixture restrictions are unchanged.

The selected-image reference uses the shared protected viewer even when Manager
has no library content-policy service. Its rating stays unknown and follows the
explicit unknown-rating blur preference. Reveal Temporarily affects only this
preview; Hide protects it again. No System Knowledge or rating is invented.
Without both settings and a policy the reference remains unavailable.
