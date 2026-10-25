# Media Organizer a.0.4.8 - Unreleased development (compatibility wire 0.4.8-alpha.1)

2026-10-25 (America/New_York)

## Bug fixes

- If selected runtime policy files cannot be read or are invalid, startup explains the selected profile and runtime location. It shows an explicit repair command when the tools are present, or explains that they are unavailable. Missing tools refuse a repair request before launching a process. Choose a profile supported by the complete matching package; no repair runs automatically.

This unpublished checkpoint does not add a runtime profile or qualify all Organizer workflows.

# Media Organizer a.0.4.7 - Unreleased development (compatibility wire 0.4.7-alpha.1)

2026-10-25 (America/New_York)

## Bug fixes

- Portable Organizer uses its selected runtime's ExifTool when no tool has been configured, instead of depending on another installation on the computer. Explicit configured tools retain precedence.

This unpublished checkpoint does not qualify metadata writes, optional models or all Organizer workflows.

# Media Organizer a.0.4.6 - Unreleased development (compatibility wire 0.4.6-alpha.1)

2026-10-14 (America/New_York)

## Bug fixes

- Requests from Tag Encyclopedia remain pending until the original Organizer action finishes; queueing a request does not report visible completion.
- Declining Close keeps queued requests available. When shutdown removes an original callback, its request receives a settled cancellation rather than waiting indefinitely.

This development checkpoint has not been packaged or released. Native and packaged acceptance remain pending. Video/audio, model execution and Generation/Training are not added to the supported release scope.

# Media Organizer a.0.4.5 - Published alpha (compatibility wire 0.4.5-alpha.1)

Published 2026-10-14; source checkpoint 2026-10-13 (America/New_York).

## Improvements

- Guided Setup follows Library locations, optional AI setup, Preferences and defaults, then Review and finish.
- Existing choices are prefilled, and Finish saves configuration without starting a media scan or source-evidence processing.

This entry describes the distinct published a.0.4.5 source and package, not later development changes. Optional AI choices do not establish installed or runnable model support. Manager-managed launch and updates, Video/audio and Generation/Training remain outside that release's supported scope.

# Media Organizer a.0.4.4 - Alpha source checkpoint (compatibility wire 0.4.4-alpha.1)

2026-10-13 (America/New_York)

## Improvements

- The portable image launcher keeps startup errors visible without automatic repair or a system-Python fallback.
- Version-specific guidance explains the manual image workflow, prerequisites, backups and alpha limits.

# Media Organizer a.0.4.3 - Alpha (compatibility wire 0.4.3-alpha.1)

2026-10-13 (America/New_York)

## Bug fixes

Opening Ingest no longer remains waiting when Dataset initialization has already
finished. Cancelling initialization, changing pages, and closing Organizer retain
their existing safeguards.

# Media Organizer a.0.4.2 - Alpha (compatibility wire 0.4.2-alpha.1)

2026-10-13 (America/New_York)

## Bug fixes

- Saving your first Image Knowledge record no longer fails during optional
  Metadata review. JSON/TXT are saved while image Metadata stays unchanged;
  you can review Metadata later.
- Closing Organizer no longer restarts queued content-safety work or fails
  because that work is already shutting down.
- Duplicate Tag names open collision review instead of failing. Cancelling
  the review keeps your saved Tags unchanged.
- Compound Tag recommendations no longer fail when saved relation details
  are reloaded, and they retain known qualifiers.

# Media Organizer a.0.4.1 - Alpha (compatibility wire 0.4.1-alpha.1)

2026-10-13 (America/New_York)

## Bug fixes

Help now shows Media Organizer's own About information and What's New.

Setup no longer offers Generation or Training controls that are unavailable in
Media Organizer. AI tagging, descriptions, memory settings, and Dataset
preparation remain available.

# Media Organizer a.0.4.0 - Alpha source candidate (compatibility wire 0.4.0-alpha.1)

2026-10-12 (America/New_York)

## New features

From Library or Dataset, choose **Knowledge > Tag Encyclopedia**, then select an
approved published snapshot. Choose a Tag and **Find Examples** to review matching
images from your currently included libraries. Select an example and choose
**Open in Dataset**; your images remain local and are not added to the catalog.

The existing catalog reader and native Tag identity checks are retained. A snapshot
that does not match the connected native knowledge cannot supply usable examples.
No library is scanned or connected just by starting Organizer.

## Bug fixes

Closing waits for original example-query workers and local thumbnail workers to
finish. Choosing another Tag while closing cannot start fresh example work. If
another workspace refuses Close, the connection remains available for an explicit
retry. Local corrections, Tag ordering and public catalog data stay unchanged.

## Candidate limits

Normal native entry, visible Dataset return, close/reopen and clean-package
acceptance are pending. No new package has been produced or published. Existing
archives retain their original identities. Generation and Training execution
remain unavailable; no model, snapshot or personal image is bundled by this change.

# Media Organizer a.0.3.1 - Alpha

2026-10-11 (America/New_York)

## Bug fixes

The public portable application excludes maintainer-only future-default teaching
and the private development inbox. Your local Knowledge editing, shipped-value
restoration and reviewed prompt authoring remain available. Startup recovery
assistance still lets you copy its diagnostic and close without changing recovery
records or backups.

## Available workflow and limits

Use Library, Dataset, Ingest and Setup in a separate local Organizer profile.
From an inspected Dataset image, choose **Image Actions > Suggest Prompt from
Image**, review saved text, choose writing guidance, then copy prompts or export
a new text file. Guides work without an installed checkpoint; no model is bundled.

Generation and Training execution remain unavailable. Drafts last only for the
open dialog session, and writing advice does not prove model support or output
quality. This unsigned, unpublished candidate has not yet passed ordinary desktop
interaction or clean-machine acceptance.

# Media Organizer a.0.3.0 - Alpha

2026-10-11 (America/New_York)

## New features

From an inspected Dataset image, choose **Image Actions > Suggest Prompt from Image** to create an editable prompt. Review saved image text or deliberately request local suggestions, choose model writing guidance, then copy positive or negative text or export a new plain-text file.

Writing guides work without an installed checkpoint. Switching guides preserves your edits without analysing the image again. Saved generation records stay read-only, and model or adapter uncertainties remain visible. Writing advice does not establish execution support or image quality.

## Bug fixes

Return and Enter while choosing a writing guide no longer start image analysis unexpectedly.

Prompt edits and pending text reviews remain available when you cancel closing. Drafts exist only while the dialog is open; copy or export what you need before discarding the session. Plain-text export transfers prompts, not their complete analysis history. Generation and Training execution remain unavailable.
