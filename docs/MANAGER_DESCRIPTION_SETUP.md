# Manager image-derived Setup and description settings

Manager a.0.7.7 candidate for GEN-06 / SUITE-10B; package delivery pending.

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
These choices do not install a caption runtime. Model files may be reused, but
Manager also needs compatible caption components. If suggestions remain
unavailable, use saved text or manual authoring.

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

## Caption worker and explicit component setup

Save Choices and Set Up Components returns through the same guarded settings writer,
then opens a focused component review. Opening it runs no checks or downloads.
Check Readiness observes the selected model and Manager's admitted worker. Install
Caption Components and Download Selected Model each require an explicit review;
the latter may also install missing caption components. Neither runs inference.
Cancel/Return retain the original coordinated operation until its process and all
contained download descendants stop; an unconfirmed stop retains the owner.

The original DependencyService owns profile hashes, wheel acquisition, staged
readback/swap/recovery and pinned model transactions inside that same Python.
Manager's Descriptions/data/optional-components store is separate from the existing
Video environment. bind_profile_root(initialize_defaults=False) explicitly selects
this component-only profile before storage imports; it creates no catalog defaults.
No test environment override or mutable global path replacement is used.

The finite child calls the whole suggest_image_setup_parts service without a
backend override, preserving purpose prompts, original image rechecks, caption
adapter release and uncertain proposal semantics. The shared image setup dialog
adopts a host provider only when supplied; the all-in-one application retains its
original in-process provider. Returning never automatically restarts suggestions.

Owners: core/manager_caption_runtime.py retains the admitted invocation/review and
contained lifetime; core/manager_caption_worker.py invokes canonical services;
suite_core/manager/entry_binding.py supplies signed source/worker/session identity;
suite_core/manager/caption_components_ui.py presents deliberate acquisition;
qt_app/generation_workflow.py and generation_dialog.py carry the provider to the
existing generation_image_setup_dialog.py. Caption settings remain in the same
Manager generation-settings.json; the child never loads or saves another settings
file. The next worker package must include tools/runtime-pins.json and the exact
already reviewed pip wheel; the retained 0.7.6 worker omitted that wheel.

These implementation descriptions do not prove model/GPU inference or packaged
acceptance. Final evidence distinguishes synthetic transport/UI cases, actual
contained generated child cancellation, and any executed package checks. Reader,
C3A, model/trainer and real-library restrictions remain unchanged.
