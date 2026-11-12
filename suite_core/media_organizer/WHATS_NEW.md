# Media Organizer a.0.5.0

- Saved Dataset Drafts can export a selected plan for Suite Manager → Applications → Review a Dataset Draft, retaining its exact settings and original sources without building a training package.

- Saved Dataset Drafts keeps its Organizer label when Dataset finishes loading in the background.

- Dataset Plans now includes Plan Training Dataset for current selected or matching images; resume saved plans through Saved Dataset Drafts.

- Closing Dataset while its initial conflict checks are queued no longer starts background work after shutdown.

- Training Dataset Models & Guidance includes its offline model catalog again, so reviewed model-specific planning profiles can return to the selected image scope.

- Media review explains Refresh after a busy scan, shows ready feedback after playback closes, and stops cleanly when closed while opening.

- Existing Video playback and verification setup is available for the qualified Windows runtime. Optional dependency cache refresh is serialized so concurrent startup checks cannot interrupt Dataset/Ingest preparation.

- Video runtime checking avoids repeated ancestor walks while retaining fresh file-integrity checks. Exact packaged playback qualification remains part of candidate delivery.

- Review Manager Result now accepts original and trimmed video receipts.
  Review and copy through Video Ingest while retaining the original and lineage.
- Reviewed Character and Outfit generation setups can be exported for Manager after explicit eligible-library selection. Prompt sharing is opt-in; cancellation and changed-source refusal retain the saved setup. Manager imports and reviews the setup in a separate image draft.

- Dataset image actions keep Remix and image-request editing in an offline planner. Save or export the request, then import it with Suite Manager Plan an Image. Generator access stays in Manager. Animate This Image points to Manager Plan a Video.

2026-11-11 (America/New_York)

## Generation results

- In Ingest > Add Images > Review Manager Result, choose the verified receipt
  copied from Suite Manager. Review and confirm a copy through the normal Ingest
  preview. Source images, metadata and receipt remain in Manager; imported images
  keep generation lineage and remain unreviewed.

## Training draft handoff

Import a Training experiment draft from Suite Manager in Dataset > Dataset Plans >
Import Training Draft. Organizer creates a separate editable Dataset draft, checks
its current images and Image Knowledge, and opens the existing candidate editor.
Save your changes with Save Draft; reopen them through Saved Dataset Drafts.
Collection drafts let you choose and review the current Collection first.

The original Manager experiment, transfer file and images remain unchanged.
Import waits for an existing Dataset check to finish; background status checks
resume after the handoff. Generation and training execution belong to Suite
Manager. This update adds Dataset planning and exchange, not a training runtime.

The bundled Python, Qt, Pillow and ExifTool runtime is unchanged from a.0.4.23.
Earlier Organizer fixes remain included. Package and installed-workflow evidence
are recorded separately; this source selection alone is not a published release.

# Media Organizer a.0.4.23

2026-11-04 (America/New_York)

## Bug fixes

Feedback Archive creates its local ZIP and records optional export history through
existing background tasks. Reviewed scope stays fixed while phase, elapsed time
and unknown-total activity remain visible. Close waits for accepted work to settle.
A verified ZIP is retained before history starts and is never recreated in the
same review. Optional history failures remain separate warnings on that result.

A task-start error does not prove a write never began. The review denies effects
that have not entered, or retains an entered task until its outcome settles.
Unconfirmed creation asks you to review the destination and start a new review;
it does not automatically retry or claim that no file exists. Report originals,
canonical writers, the earlier read-only loading path and parent completion stay
unchanged. No upload, private Learning integration, unattended install or restart.

Qualification covers generated physical ZIP/history and finite ownership/fault
cases, focused source-component UI states, signed source/inventory, isolated idle
saved-state startup/Close and authenticated update/rollback. Actual/private reports,
ordinary full parent/native/DPI/AT/novice, scale and private reading remain open.

# Media Organizer a.0.4.22

2026-11-04 (America/New_York)

## Bug fixes

Feedback Archive opens its review controls before loading saved-report metadata.
Loading runs through the existing read-only task service while the interface
continues responding. Unknown-total activity and elapsed time remain visible;
report selection and archive creation wait for a current successful result.
If loading fails, restore access and choose Retry Loading Reports. Your chosen
archive destination is retained. Closing ignores the read result; an already
running read may finish in the background without changing reports.

The metadata parser, worker engine, report bytes, archive/history writes,
theme and parent completion are unchanged. The earlier known-archive result
guard remains included. This does not make archive creation/history writes
asynchronous or add private Learning integration. No automatic retry, upload,
unattended installation or restart is enabled.

Qualification uses generated metadata and stock Qt task/lifetime cases, focused
loading/result/failure/retry states, signed inventory, isolated idle saved-state
startup/Close and update/rollback. Actual/private reports, full ordinary parent,
native/DPI/AT/novice, scale and remaining Archive write/reading acceptance stay open.

# Media Organizer a.0.4.21

2026-11-04 (America/New_York)

## Bug fixes

A verified Feedback Archive now reaches the existing completion message even
when optional per-report export-history recording raises an unexpected ordinary
exception. The verified ZIP path is retained, history warnings identify affected
reports, and remaining selected reports can record history. The ZIP is not
created again. Existing success and expected-error behavior remain unchanged.

This patch changes only that optional-history exception guard. Archive creation,
writers, report formats, theme, layout, parent completion and sharing actions
are unchanged. System-level BaseException signals are not swallowed. Earlier
fixes remain included. No automatic upload, install or restart is enabled.

Qualification covers generated paired old/new archive and history faults,
immutable report/ZIP preservation, original Qt controls and completion-warning
carrier at component level, signed inventory, isolated idle saved-state startup/
Close and update/rollback. Actual/private reports, ordinary full parent entry,
clipboard/folder actions, native/DPI/AT/novice and private Learning acceptance
remain open. This does not deliver responsive discovery/export or Learning.

# Media Organizer a.0.4.20

2026-11-04 (America/New_York)

## Bug fixes

Report Review now says a report was prepared locally, rather than claiming it
was already saved. The readable report and Privacy Manifest use wording that
is true before and after Save. Copy and Save retain the exact reviewed text.

Existing saved reports are not rewritten or migrated. Their stored report and
privacy hashes remain authoritative. New reports hash the new reviewed wording
through the existing algorithms. The schema, loader, writer, archive service,
UI layout and sharing authority are unchanged. Earlier fixes remain included.

Qualification covers generated old/new report compatibility, hashes, tamper
refusal, duplicate tracking and archive readback; generated Qt review states
and inert clipboard effects; isolated idle startup/save/reopen/Close and signed
update/rollback. Actual/private reports, real clipboard, full parent/native/
DPI/AT and independent novice acceptance remain open. No automatic upload,
unattended install or restart is enabled.

# Media Organizer a.0.4.19

2026-11-04 (America/New_York)

## Bug fixes

Collected Feedback ZIP export now refuses destinations inside any selected
report folder and refuses existing output files before loading or writing.
Choose a new ZIP name outside the selected folders and retry. Normal new
archives preserve original reports and retain verified archive readback.

This patch ports the completed destination preflight only. The shared ZIP
writer, report format, UI and providers are unchanged. It adds no upload,
migration or unattended installation/restart. Earlier fixes remain included.

Generated report preservation and ordinary new ZIP output are qualified.
Native aliases, path races and atomic concurrent no-clobber are not qualified;
do not infer those guarantees from this preflight. Actual/private reports,
full UI/novice workflows and private Learning integration remain unqualified.
Signed inventory, isolated idle saved-state startup/Close and scoped update/
rollback qualification are recorded separately from those open boundaries.

# Media Organizer a.0.4.18

2026-11-04 (America/New_York)

## Bug fixes

Copy Report and Save Reviewed Report now stop if the current privacy review
fails validation. A previous valid preview is kept for review but is never
silently copied or saved as the current report. Correct the review and retry;
valid copying, saving and the existing draft/validation feedback are preserved.

This compatible patch changes only the two original action readiness checks.
It adds no report provider, writer, upload, Archive change or data migration.
Earlier fixes remain included. No unattended install or restart is enabled.

Qualification separates generated Qt review/validation/retry with fake clipboard
and writer effects, reviewed light/dark component frames, isolated idle-host
startup/save/reopen/Close and signed update/rollback from actual report/private
data, clipboard/files/inbox/provider behavior, ordinary parent/native/AT/DPI and
independent novice acceptance. Those broader criteria remain open.

# Media Organizer a.0.4.17

2026-11-04 (America/New_York)

## Bug fixes

Keep relationship review results bound to the Knowledge source that opened the
review. If that source is replaced while a dialog or completion callback runs,
stop stale refresh and navigation instead of sending the old result to another
owner. Stable-source review, saved outcomes and original navigation are preserved.

This compatible patch changes only the original relationship caller's identity
checks, including its existing secondary Character review. It adds no writer,
service, dialog, data migration, native/runtime change or automatic retry.
Earlier fixes remain included. No unattended installation or restart is enabled.

Qualification separates generated callback/Qt-return component checks and
isolated idle-host startup/save/reopen/Close plus signed update/rollback from
actual relationship Apply/Undo/transactions/recovery, ordinary Knowledge caller
assembly, parent/native retirement, full Learning, live Manager, DPI/AT and
independent novice acceptance. Those broader criteria remain open.

# Media Organizer a.0.4.16

2026-11-04 (America/New_York)

Remove-everywhere Tag review now retains its original preview/apply return and
History identity until the shared coordinator confirms scoped Python work has
drained. Cancellation keeps saved facts while refusing a new global removal
decision. Missing or undisplayed returns remain held without automatic retries.
Result delivery and the background-finished notification retain their ordering;
the original Category caller supplies its existing manager.

This compatible patch reuses the shipped lifetime helper and task API. The
original Category accepted-and-completed gate, parent tracking/finally logic,
global Tag service, image/history writers, native/runtime and schemas are
unchanged. Busy work names its phase; held Close is unavailable. Earlier fixes
remain included.

VALIDATION.json distinguishes generated in-memory task/callback component checks,
light/dark fixture review and isolated idle-host/update/recovery from actual
global Tag/image/history operations, parent/native retirement, complete Learning,
live Manager, DPI/AT and independent novice acceptance. Those remain open.
No unattended install/restart or broader native/Root/Reader qualification is added.

# Media Organizer a.0.4.15

2026-11-04 (America/New_York)

Selected-image Tag review now keeps its original preview/apply return and History
operation identity until the shared coordinator confirms that the scoped Python
work has drained. Cancel and direct dialog completion retain that ownership;
missing, interrupted or undisplayed results stay held for review rather than
automatically retrying. Both Dataset and Ingest supply their existing coordinator.

This compatible patch reuses the stock lifetime helper and existing shared task
API. Service preview/apply algorithms, image/history writers, native/runtime
libraries and data schemas are unchanged. Earlier fixes remain included.

VALIDATION.json distinguishes generated in-memory task/callback component checks,
light/dark fixture review and isolated idle-host lifecycle/update/recovery from
actual user-image/history mutations, live Manager, native/Root/Reader, complete
Learning integration, DPI/AT and independent novice acceptance. Those remain open.
This release does not enable unattended installs/restarts or claim those states.

# Media Organizer a.0.4.14

2026-11-03 (America/New_York)

Category Details now checks its original save-readiness callback for every result
route,including accept/done,Close and Escape. A pending or refused save keeps the
dialog open. Reentrant completion cannot bypass readiness or deliver twice;
changed owners/callbacks and callback errors retain a deliberate retry boundary.
The Qt Close event is refused before readiness runs,so an exception cannot leave
closure admitted.

This compatible patch changes only the small dialog completion class. The actual
editor,autosave,flush,discard,store and recovery authorities are unchanged. All
earlier fixes remain included. The public host has no Learning reader integration;
that separate source work is not shipped. VALIDATION.json distinguishes generated
callback-only component evidence and isolated idle host lifecycle/update recovery
from actual Category editor flush/save/discard,private data,original modal/native,
DPI/AT,clean-machine/independent novice and live Manager acceptance. Those remain
open. No data schema,native library or unattended-update change.

# Media Organizer a.0.4.13

2026-11-03 (America/New_York)

Duplicate preflight now waits when its nested Duplicate Review refuses completion.
Close, Escape and result actions retain the outer owner, freshness timer and exact
accepted review result until a deliberate later safe completion. Ingest remains
busy while that owner is open and rechecks exit readiness once it finishes.
Ordinary read-only cancellation still detaches, and replacement owners survive.

This compatible patch reuses the existing dialog, jobs, writers and completion
notifier. All earlier fixes remain included. No image-fact, schema, native library
or unattended-update change. The public host has no Learning reader integration;
that separate source work is not shipped or qualified here. VALIDATION.json keeps
generated component/method checks and isolated idle host/recovery separate from
actual full Ingest/global active shutdown, images/stores/journals, native, DPI/AT,
clean-machine/independent novice and live Manager acceptance. Those gates remain open.

# Media Organizer a.0.4.12

2026-11-03 (America/New_York)

When Duplicate Review needs a fresh result after a refresh error, its recovery
instruction now names the button actually shown: Find Duplicates in normal mode
or Refresh Frozen Scope in frozen review. Known outcomes and the resolve hold
remain intact until the existing successful fresh-render path completes.

This compatible clarity fix changes one status string. All earlier safety fixes
remain included. No writer, task, data schema, native library or update policy
changes. VALIDATION.json separates generated component evidence and isolated idle
host lifecycle/recovery from physical image/store operations, actual parent/Ingest,
native, clean-machine/accessibility/independent novice and live Manager acceptance.
Those broader criteria remain open.

# Media Organizer a.0.4.11

2026-11-03 (America/New_York)

Duplicate Review retains accepted Mark Distinct and recycle results while Close,
Escape and cancellation controls wait. Ordinary read-only searches still detach
deliberately. Each accepted recycle batch keeps exact known moved, unconfirmed
and unattempted families after cancellation, failure or changed scope. Known
paths are notified once; refresh errors retain the review hold until a successful
fresh render. A cancelled acknowledgement never claims rollback or no change.

This compatible alpha patch reuses existing writers, verification/recovery,
task coordination and progress controls. It adds no image-fact, data-schema,
native-runtime or unattended-install change. Earlier patches remain included.
VALIDATION.json distinguishes generated dialog/task/accounting evidence and idle
startup/save/reopen/Close from real images, physical recycle/journals, original
parent/Ingest/native interruption, clean-machine/accessibility/novice and live
Manager acceptance. Those broader criteria remain open.

# Media Organizer a.0.4.10

2026-11-03 (America/New_York)

Reviewed Distinct History keeps accepted mark-removal work observed while Close,
Escape or dialog completion waits. The exact returned result stays visible;
failure or cancellation does not claim that nothing changed. Remaining selection,
reason and Remove Mark action stay synchronized after a removal. The original
default-Cancel confirmation and existing store/task authorities remain.

The shared progress row can describe an owner-wired waiting Close accurately,
then restores that control's original hint. Existing callers retain their default
behavior. No image writer, decision-store format, task coordinator, data schema
or native runtime changes. Earlier 0.4.9/0.4.8/0.4.7 fixes remain included.

VALIDATION.json separates generated dialog/task evidence and isolated idle
startup/save/reopen/Close from actual user histories/images, parent cache/regroup,
native active jobs, clean-machine/accessibility/novice and live Manager acceptance.
No unattended install/restart or new training/generation operation is enabled.

# Media Organizer a.0.4.9

2026-11-03 (America/New_York)

Tag Details keeps late example-search errors attached to the original Tag and
generation. An error for a changed or removed Tag no longer clears the current
gallery task or replaces its feedback. Both existing task transports use the
same guard. Provenance summaries display source strings literally, including
markup and entities, matching the read-only source presentation contract.

This compatible alpha patch retains a.0.4.8's Close-custody and a.0.4.7's
selection-contrast improvements. Runtime libraries, data interfaces and save
authority are unchanged. It adds no model/video/training operation or unattended
installation. VALIDATION.json distinguishes generated callback/widget proof and
isolated startup/reopen from real library/index/transaction, native editor/reader,
clean-machine/non-elevated/DPI/AT and independent novice acceptance.

# Media Organizer a.0.4.8

2026-11-03 (America/New_York)

Fixes final Close ordering: when background shutdown is incomplete, the window
stays open and retains its pending callbacks, tasks and queued actions. A
deliberate later Close cleans up once after shutdown succeeds. Existing page
vetoes and image/data authority remain unchanged; Close is not replayed for you.

This compatible alpha patch retains a.0.4.7's indicator contrast improvement,
image workflow, runtime and data interfaces. It adds no model execution, video,
Training/Generation or unattended installation. Qualification details are in
VALIDATION.json. Active native editor/reader jobs, non-elevated/clean-machine,
assistive-technology and independent novice acceptance are not claimed.

# Media Organizer a.0.4.7

2026-11-03 (America/New_York)

Fixes hard-to-distinguish checkbox, radio and list selection states in Light,
Dark and Midnight Blue themes. Checked, partial, focused and disabled states
use explicit marks and outlines. Follow Windows keeps its native appearance.

This compatible patch retains the a.0.4.6 image organization workflow, native
runtime and data interfaces. It adds no model execution, video/audio support,
Training/Generation workflow or automatic resource installation.

Qualification and update/recovery results are supplied in the release validation
receipt. Clean-machine, assistive-technology and independent novice acceptance
are not claimed. Application-update signatures establish package integrity;
Windows Authenticode/SmartScreen publisher trust is not claimed.

# Media Organizer a.0.4.6 - Unreleased development (compatibility wire 0.4.6-alpha.1)

2026-10-14 (America/New_York)

## Bug fixes

Tag Encyclopedia cannot open once Organizer's background work is shutting down.

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
