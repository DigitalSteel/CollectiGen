# CollectiGen Media Organizer

**Media Organizer a.0.5.2** organizes images, preserves Image Knowledge and generation
lineage, and prepares Dataset captions and Training plans. Generate and Training
execution belong to Suite Manager.

[Release and notes](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.media-organizer/v0.5.2-alpha.1) ·
[Windows ZIP](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.media-organizer/v0.5.2-alpha.1/MediaOrganizer-a.0.5.2-windows-x64.zip) ·
[SHA-256](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.media-organizer/v0.5.2-alpha.1/MediaOrganizer-a.0.5.2-windows-x64.zip.sha256)

If a generated image or sidecar changes after the copy preview, Ingest now explains
why it stopped and directs you to **Review Manager Result** with the current
receipt. The Manager originals remain untouched; the changed family is not copied.

## Start

Extract the complete ZIP into a new writable folder, keep its contents together,
and run **Start_MediaOrganizer.bat**. Retain your previous package and separate
profile, image and catalog backups. Windows may report an unknown publisher:
CollectiGen signs update metadata, while the executable has no Authenticode signature.

Alternatively, use **Suite Manager a.0.7.0 > Applications > Get or update Media
Organizer**, review the offered version and confirm Install. Older Manager
releases can retain an older exact Organizer offer.

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

This update keeps a reviewed result available for explicit Retry after busy file work,
limits verified result previews to Copy, and names the copied-family Recycle action.

This alpha passed synthetic Copy/Retry/Undo and changed-copy refusal, exact package/source readback and
bundled offscreen startup, saved-state reopen and normal Close. These checks do not establish model execution, live-library behavior,
installed update/rollback or full-suite parity.
