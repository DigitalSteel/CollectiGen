# Media Organizer a.0.7.0 — Alpha

2026-11-16 (America/New_York)

Organize and review your images locally. This alpha preserves long generation
seeds exactly and adds explicit authentication for local model-list inspection.

## Improvements

- Import generation seeds without rounding them. Raw metadata, unknown fields
  and fractional settings remain intact. Previously rounded records are not
  rewritten automatically.
- In **LM Studio Connection**, open **Authentication** to enter a token for one
  **Refresh Models** request. It is masked, cleared on submission and excluded
  from saved connection preferences. Enter it again for another refresh.
- Review the reported models before separately choosing **Save Connection**.
  Refresh shows progress; Cancel and Close wait for the active request to settle.
  Changing the server address requires another explicit review and refresh.

## Limits and upgrade guidance

LM Studio support covers connection preferences and read-only model-list review
on this computer, using an explicit local HTTP address and port. Reported model
capabilities do not establish inference readiness. LM Studio image analysis,
model loading and training are unavailable through this connection.

This is alpha software. Generated images and local fixture responses cover the
bounded workflow checks; actual authenticated LM Studio installations, inference
and output quality remain unqualified. No AI models are bundled.
Metadata writing and optional-tool readiness remain outside this release's
generated-data qualification.

Keep a verified prior package and separate backups of your profile and images.
Close the application before updating. Restoring software does not undo image
edits. Review the supplied release notes and checksums when installing an update.
