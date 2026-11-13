# CollectiGen Media Organizer

**Media Organizer a.0.5.5** organizes images, preserves Image Knowledge and generation
lineage, and prepares Dataset captions and Training plans. Generate and Training
execution belong to Suite Manager.

[Release and notes](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.media-organizer/v0.5.5-alpha.1) ·
[Windows ZIP](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.media-organizer/v0.5.5-alpha.1/MediaOrganizer-a.0.5.5-windows-x64.zip) ·
[SHA-256](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.media-organizer/v0.5.5-alpha.1/MediaOrganizer-a.0.5.5-windows-x64.zip.sha256)

Interrupted rule repair and Undo can now be reviewed and recovered from the Rules workspace. Repair also checks that the supporting data still matches what you reviewed. Existing Copy, Undo and reused-path recovery fixes remain included.

## Start

Extract the complete ZIP into a new writable folder, keep its contents together,
and run **Start_MediaOrganizer.bat**. Retain your previous package and separate
profile, image and catalog backups. Windows may report an unknown publisher:
CollectiGen signs update metadata, while the executable has no Authenticode signature.

Suite Manager a.0.7.3 offers the previous Organizer a.0.5.4. Use the ZIP above to run a.0.5.5.

## Work across components

- In **Ingest > Add Images > Review Manager Result**, select a verified image or
  video receipt and review the source-preserving copy. Imported evidence remains
  unreviewed. For a trimmed video, also select its original Manager video receipt.
- Prepare captions, collections and Training packages in Dataset. Export a saved
  Dataset draft for **Manager > Training > Review a Dataset Draft** when you want
  to review the plan before building data.
- Prepare offline image/video drafts or reviewed Character/Outfit setups in
  Organizer, then import them in Manager. Organizer does not start a generator
  or training runtime.

Native Copy/recovery can now return to a previously used path while preserving exact file identity and family bytes. This update preserves Copy Undo after the first automatic initialization of a copied Manager result. It keeps a reviewed result available for explicit Retry after busy file work,
limits verified result previews to Copy, and names the copied-family Recycle action.

This release passed 93 focused source cases, 15 affected offscreen UI cases, four bundled rule-recovery scenarios, and bundled startup, saved-state reopen and normal Close. These checks use synthetic data. Earlier isolated Manager prior-version recovery is retained separately. Enrolled/native concurrent recovery, real libraries, model execution and full suite parity remain outside this release evidence.
