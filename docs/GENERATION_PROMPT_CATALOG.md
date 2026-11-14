# Reuse Organizer prompt choices in Manager

Available in Manager a.0.7.6 and Organizer a.0.5.6. Broader GEN-06 / SUITE-10B
parity and update/recovery work remains open.

In Organizer, choose **Knowledge > Prompt Catalog**, then **Knowledge > Prompt
Choices > Export Prompt Catalog for Generate**. Review the counts and scope, then save to a new filename.
The export contains reusable Tag names/aliases, Character names and reviewed
Outfit prompt text and associations. It contains no image files, image paths,
model files or arbitrary native record fields. Nothing is uploaded.

In Manager, open **Generate > Use Organizer Prompt Catalog**. Choose **Import
File**, select that export, then open the imported draft. The existing importer
makes a new local copy. Other open drafts and the exported file remain unchanged.
Use Tag search on **Request**, or **Advanced > Reusable Prompt Text** to browse Outfits
associated with the selected Character, other Characters, or no Character.
Browsing does not change the request. Choose text and Apply deliberately.

The catalog is a read-only snapshot embedded in the normal portable image draft.
Save and reopen the draft to retain its prompt choices. Knowledge editing stays
in Organizer; export again to use later Knowledge changes. This route grants no
image, model, trainer, Reader or network execution authority.

Implementation owners: `core/generation_prompt_catalog.py` validates and projects
text; `qt_app/generation_prompt_catalog_actions.py` owns Organizer's reviewed
coordinated export; `qt_app/knowledge_page.py` exposes its contextual action.
`qt_app/generation_dialog.py` adopts read-only choices; the existing
`qt_app/generation_workflow.py` and `qt_app/generation_saved_work_dialog.py` own
Manager import/selection. `suite_core/manager/window.py` exposes the Generate entry.
The stock GenerationPlan serializer, exact import-copy service and guarded save
transaction retain persistence, stale refusal, backup/readback and recovery.

Final source checks passed: 12 Manager cases and 11 Organizer cases, including
shared generated core cases. Readable offscreen import/export, progress and error
states were reviewed. These are source/component results, not packaged acceptance.
Native packaged acceptance and full-suite parity are separate; no human-test hold.
