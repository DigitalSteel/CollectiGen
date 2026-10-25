# Tag Database component hookup

This source component is independent of Manager startup, Ollama, settings and
Organizer. It adds no normal entry or production Apply enablement. The underlying
Tag transaction module remains alpha.4/interface 1; Manager version is unchanged.

Product fit: reviewed default Tag installation and merging reach the selected
Encyclopedia reader while preserving corrections, unknown native fields, original
image assignments and exact native recovery. Native services retain authority.

## Reuse and bounded ownership

Inspected 2026-10-09: `database_presentation.py` supplies the existing bounded,
plain-text versions/protected-edit/conflict/assignment projection; `window.py`
contains its widget assembly and unconditionally disables Apply.
`database_preview_route.py` is intentionally preview-only. Its cancellation,
host-executor lifetime and unknown-submission close holds inform this controller,
but it cannot perform native apply/recovery and must retain its present contract.

`tag_database_controller.py` adds only the missing serialized native operation
coordination. `tag_database_ui.py` reuses the presenter and `ui_cards.py` workspace
heading/layout and compact actions. It calls the stock
`EncyclopediaWindow.open_snapshot` after verified publication, retaining existing
browser search/selection on same-catalog refresh. No alternate native store,
Pack serializer, migration, restoration engine or Encyclopedia view is created.
The previously passed alpha.4 native batch proves the stock service path; this
batch adds offscreen visible widget evidence, not another simulated writer.

The closest mature workflow is Knowledge Pack selected-decision review, owned by
the `knowledge-pack-review-and-sharing` journey. Scope, versions and readiness
precede actions; optional details use the existing section selector and bounded
plain-text viewer. Preview/discard start on the left, explicit confirmation and
Apply finish on the right; exact receipt recovery stays beside its selector.
The departure is a reusable pane outside the prohibited `window.py` shell: that
lets the primary integrator hook up the existing Databases home without competing
ownership. Main-window command/ribbon placement and real-entry acceptance remain
the primary integration gate, not a claim made by component tests.

## Explicit host hookup

```python
from suite_core.manager.tag_database_controller import TagDatabaseChoice, TagDatabaseController
from suite_core.manager.tag_database_ui import TagDatabasePane

choice = TagDatabaseChoice(
    approved_catalog_label, native_tag_transactions,
    approved_prior_release, approved_next_release, approved_complete_native_pack,
    installed=explicit_installed_observation,
    assignments=explicit_occurrence_assignments,
    offers=explicit_reviewed_offers,
    native_adapter=exact_native_adapter_already_owning_the_service,
    approved_releases=explicit_approved_release_snapshots,
)
controller = TagDatabaseController(choices=(choice,), executor=host_serial_executor)
pane = TagDatabasePane(controller, encyclopedia=existing_encyclopedia_reader)
# The primary replaces/hooks the Databases review surface in window.py.
# apply_authorized defaults False. Live qualification is required before enabling it.
```

The host supplies already approved selected roots, the exact canonical native
adapter/service, complete prepared Pack records, provenance and immutable approved
release snapshots. The component observes only that selected root and its native
managed receipts; it performs no catalog/library discovery. This constructor starts neither an application nor an executor.
Selection or `replace_choice(index, approved_choice)` invalidates the review;
replacement refuses until admitted work drains. The native service checks the
current plan, selected binding, authority fingerprints and receipt epoch again
before mutation. Confirmation belongs only to the current review and is reset
by every new review, selection or operation attempt.

Executor submit must be nonblocking and return `concurrent.futures.Future`.
Native work executes away from the Qt owner. An inline executor is refused before
native work; submission failure or missing completion handles retains close
ownership because work may have started. The host must not bypass or concurrently
mutate the service/session while this controller owns it, and must retain/drain
the executor for the pane lifetime. No executor shutdown or blocking join occurs
on the Qt thread. Main-window close must call `begin_close()`, continue polling and
respect `close_held`; the pane itself rejects close while work is retained.

Cancellation revokes preview delivery immediately. For apply/recovery/reload it
can stop queued work before admission. After admission it waits for the existing
atomic native operation and retains its committed or interrupted outcome. The
progress display uses honest phases, not an invented percentage or mid-write
abort. Exceptions retain the reason, consume Apply review authority and offer
exact native receipt-bound recovery. They do not claim that nothing was saved.

After a committed publication failure, use Reload saved catalog. Never replay
Apply. The component refreshes the stock reader only after successful publication.
Selected recovery reuses native journal preflight/restoration and republishes the
verified native state. Completed-import Undo is outside this component.

## Change-to-check map and remaining qualification

Owned: the two new Manager files, this document and
`suite_core/tests/{test_tag_database_ui,native_tag_database_ui_fixture}.py`.
Direct consumers: the Tag service, existing review presenter/shared card layout,
stock Encyclopedia reader and future primary-owned Manager hookup. Routing is one
owner; no delegation or shared-file edits.

Selected checks: audited generated-native visible-widget install/merge journey;
stale-plan refusal; interrupted-write/selected recovery; queued/admitted safe
cancellation and close drain; default authority hold. The original native fault
matrix remains reusable alpha.4 evidence while its source/dependency pins match.
The planner's coarse backend module set does not require unrelated app tests;
the exact component/adjacent presenter checks and supported-size captures cover
the changed boundary. No migration, package or release gate is triggered because
there is no live enrollment, production registration or distribution change.

Required before production: approved root/privacy/Pack fidelity and installed
release observation; native live enrollment/restart/cancellation and shared
reader/writer exclusion qualification; visible Manager Databases entry through
review, explicit apply, persisted completion, Encyclopedia refresh and recovery;
1080p light/dark novice/expert review and natural-size keyboard/scroll acceptance;
primary reconciliation of affected maps and `docs/UI_JOURNEYS.json`.
Unsupported native conflicts remain held. Organizer's hold stays in place.
Offscreen visible-widget routes are component evidence; they do not certify the
normal Manager entry, live catalog, production Apply, packaged app or human review.

## Retained component evidence

`.tmp/tag-module-integration-batch-20261009/tag-ui-evidence.json` pins final sources,
JUnit reports, themed captures, the narrow final phase-display correction and
unchanged native alpha.4 evidence. The complete batch passes six real-native
visible-widget scenarios and 29 immediate pure DatabaseActions checks; after the
visual phase correction, only its two affected progress/cancellation scenarios
are rerun. Overlapping passes are not added to the distinct check count.

Captures use the existing Qt light/dark theme and one explicitly loaded Segoe UI
system font, at an actual 1100x840 pane size within a 1080p working area. They are
widget component captures, not a real 1920x1080 Manager-window or desktop journey.
The initial unreadable-font captures and the test-loop/checkbox-hit-area failures
are retained as superseded diagnostics. Tests use real widgets and native files;
only generated faults and explicit approved host inputs are injected.

Inspected start, review, protected-edit, admitted-write, completion and recovery
states show readable scope/versions, bounded details, fixed apply/recovery and
accessible keyboard review selection. The inherited presenter uses technical
field names in optional details; default action/status placement follows the
stock Manager/Knowledge Pack pattern. The pane does not prove mature shell entry,
reader-window selection retention, multi-catalog native switching, narrow-screen
geometry, user comprehension, real enrollment or packaged behavior. Those remain
explicit primary/live acceptance gates rather than hidden completion claims.

## Fresh native host binding - 2026-10-10

`TagDatabaseChoice.native_adapter` must be the exact adapter whose bound capture
owns its transaction service; a different selected root or substituted adapter
is refused. Construction remains passive. Refresh host observations is explicit
and runs on the same serialized worker as native transactions. Preview refreshes
its baseline before computing, and apply/recovery/reload refresh their observed
outcomes before delivery. No Qt worker reads, alternate persistence or release
registry is introduced.

Under existing native exclusion and pinned identity, observation reuses
`core.catalog_admission._receipts` and `knowledge_pack._read_pack_receipt` to
bind each pending managed path to its native hash. Restore preflight is deliberately
left to selected native recovery admission; observation does not grant that lease.
Recovery rechecks the exact observed hash under exclusion before invoking the
existing service. Only receipts with approved Tag-release provenance and Tag/ledger-only targets
are offered; unrelated or unclassified imports remain with their native owner.
A changed receipt holds without writing; multiple pending
receipts disable this component's recovery action and remain native-owner work.
Successful recovery refreshes state and removes restored entries from the pending
selector. Completed-import Undo/inverse is not enabled.

Installed release metadata comes from `NativeTagCatalogAdapter.default_content`
and its completed native receipt proof. It must exactly match a caller-supplied
approved `CatalogRelease` fingerprint; labels alone never reconstruct the prior
snapshot. `approved_releases` retains approved historical snapshots through a
restart/recovery. The current baseline and available release also participate in
that lookup. Unknown installed releases with no approved matching snapshot hold
preview/Apply. No version binding is not treated as an empty catalog: only an
explicit approved blank baseline plus a fresh empty native capture allows install.
After commit, the native recorded release becomes the next baseline automatically.
An already-installed exact release is held to prevent duplicate import/replay.

On interruption the completion retains the reason and automatically observes the
selected root's native pending receipts. Recovery therefore needs no fixture-only
`replace_choice` injection. Pending recovery withholds installed-version claims.
The existing explicit full Pack remains authoritative for user overrides and
unknown native fields; this component does not generate a new Pack or overwrite
corrections to make a plan clean. Publication failure retains the committed native
outcome and installed-release observation; Reload refreshes without another import.

The primary hookup artifact is
`.tmp/tag-module-integration-batch-20261009/tag-window-hookup-UNAPPLIED.patch`.
Its adjacent JSON pins the exact `window.py` before bytes; the preview parses and
`git apply --check` passes. No patch is applied here. It adds optional host controller/
reader arguments, replaces only the Databases page's visible body, routes existing
preview/render/discard hooks and retains pending Tag ownership before close teardown.
The primary must verify/rebase its hash after lifecycle edits and retain executor
ownership through drain. Default startup still supplies no controller and no
production Apply authority. No Manager version, shared manifest/map or actual
lifecycle source changes are made by this lane.

Current focused evidence is `tag-observation-evidence.json` in the same temporary
batch directory: eight real-native offscreen widget scenarios plus immediate
DatabaseActions regressions, unknown-release and stale-receipt binding guards,
light/dark captures and an unapplied patch check. Earlier evidence remains historical
and is not added to the new count. These source components are ready for the primary
hookup; actual host authorization/privacy, approved Pack acquisition and historical
snapshot availability, native restart/enrollment/cancellation qualification, shared
reader/writer admission, normal Manager entry, packaged behavior and human 1080p
review remain actual-host gates. The Organizer hold is unchanged.

New operation errors select the existing Operation reason detail section immediately.
The final focused guard run verifies the visible interrupted-write and changed-receipt
reasons before selected recovery; user-selected detail navigation remains available.
