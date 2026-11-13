# CollectiGen Media Organizer

**Media Organizer a.0.5.4** organizes images, preserves Image Knowledge and generation
lineage, and prepares Dataset captions and Training plans. Generate and Training
execution belong to Suite Manager.

[Release and notes](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.media-organizer/v0.5.4-alpha.1) ·
[Windows ZIP](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.media-organizer/v0.5.4-alpha.1/MediaOrganizer-a.0.5.4-windows-x64.zip) ·
[SHA-256](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.media-organizer/v0.5.4-alpha.1/MediaOrganizer-a.0.5.4-windows-x64.zip.sha256)

If a generated image or sidecar changes after the copy preview, Ingest now explains
why it stopped and directs you to **Review Manager Result** with the current
receipt. The Manager originals remain untouched; the changed family is not copied.

## Start

Extract the complete ZIP into a new writable folder, keep its contents together,
and run **Start_MediaOrganizer.bat**. Retain your previous package and separate
profile, image and catalog backups. Windows may report an unknown publisher:
CollectiGen signs update metadata, while the executable has no Authenticode signature.

Alternatively, use **Suite Manager a.0.7.3 > Applications > Get or update Media Organizer**, review the offered version and confirm Install or Update. Restart Manager if prompted before opening the newly installed app.

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

This alpha passed 48 focused source checks and four bundled recovery scenarios, plus isolated Manager update, native Open/Copy/Undo/Restore and normal Close. Status-check cancellation also passed the focused source tests; native busy-state timing was not observed. Original Manager files remain unchanged. Actual prior-version rollback, model execution, live-library behavior and full-suite parity remain separate unless named in an exact release receipt.
