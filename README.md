# CollectiGen Media Organizer

**Media Organizer a.0.5.6** organizes images, preserves Image Knowledge and generation
lineage, and prepares Dataset captions and Training plans. Generate and Training
execution belong to Suite Manager.

[Release and notes](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.media-organizer/v0.5.6-alpha.1) ·
[Windows ZIP](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.media-organizer/v0.5.6-alpha.1/MediaOrganizer-a.0.5.6-windows-x64.zip) ·
[SHA-256](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.media-organizer/v0.5.6-alpha.1/MediaOrganizer-a.0.5.6-windows-x64.zip.sha256)

Export reusable Tag names/aliases, Characters and reviewed Outfit prompt choices with **Knowledge > Prompt Catalog**, then **Knowledge > Prompt Choices > Export Prompt Catalog for Generate**. Review and save a new export; import it in **Suite Manager a.0.7.6 > Generate > Use Organizer Prompt Catalog**. The export excludes images, library/model paths and unreviewed Outfit text. Existing rule repair, Undo and Copy recovery remain included.

## Start

Extract the complete ZIP into a new writable folder, keep its contents together,
and run **Start_MediaOrganizer.bat**. Retain your previous package and separate
profile, image and catalog backups. Windows may report an unknown publisher:
CollectiGen signs update metadata, while the executable has no Authenticode signature.

Use **Suite Manager a.0.7.4 > Applications > Get or update Media Organizer** to find the latest compatible signed release. Review the version and confirm installation. Module updates do not require rebuilding Manager.

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

The packaged Ribbon catalog export, saved catalog readback, independent reopen and normal Close passed with a generated NetworkShared profile. Unchanged rule-recovery checks are retained from a.0.5.5; they were not repeated for the catalog update. Signed prior recovery artifacts remain authenticated. Actual installed update/rollback, real libraries, model execution and full suite parity are not claimed by this release.
