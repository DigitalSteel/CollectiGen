# Tag Encyclopedia source candidate - 2026-10-14

Current development source: **a.0.2.2** (compatibility wire 0.2.2-alpha.1), maximum snapshot interface **2**, preserving readers for interfaces 1 and 2.

This candidate contains only the accepted setup presentation and pending-Close retry fixes on the previous candidate. Its next unsigned package and ordinary native startup/Close acceptance are still pending.

Home opens a current enrolled local Tag Database without Manager or internet access. Opening only observes its saved data. Optional default Tags use a separate explicit review, import and recovery workflow through the existing native transaction owner; retained personal records and corrections stay intact. Refresh follows the accepted database or export source. Unknown, stale, legacy or incomplete authority holds the operation.

For an accepted native database, Edit Tags and Categories prepares the original
Tag and Category forms for that exact catalog identity, directory and revision.
New SDXL Tag, Edit Selected Tag, New Category and Edit Selected Category use the
original verified writers. Finish their Save/Cancel controls before changing the
catalog, reviewing imports or closing. Failed preparation/composition leaves
browsing available without adopting a replacement database or partial editor.

When an explicitly admitted content acquisition service is supplied, the existing
default-content dialog offers release refresh, confirmed download and downloaded
source review before separate Connect, Preview and Apply decisions. Ordinary
startup supplies no live feed or public trust. A corrupt cached package reports
verification failure; this is not evidence of actual network-transfer failure.

An explicitly enrolled Organizer browser can offer Find Matching Images in
Organizer and Review Image Tag Names. The original receiver and selected native
adapter are required. The first opens display-name search; the second asks for a
recognized alias and opens original correction review. Neither automatically
writes image metadata. The original Organizer caller/settings/reopen source route
is integrated; actual Dataset completion and native/packaged acceptance remain
separate gates. Standalone startup discovers no recipient.

Help supplies dated notes, About and an optional notification-only update check. No authentic standalone updater is enabled by this source entry. Organizer may explicitly supply its original selected library and query owner for examples; standalone startup has no implicit library access.

Source and visible synthetic checks cover the database/shell flow. Packaged startup, clean-machine acceptance and authenticated executable updates remain pending. The published **a.0.1.8** Windows evaluation package is unchanged and supports its older read-only exported-snapshot workflow. No new package is released by this source candidate. See [What's New](WHATS_NEW.md) and the project source landing record for evidence and remaining gates.

From the repository root, using the existing development environment:

```powershell
& .\.venv\Scripts\python.exe -B -m suite_core.encyclopedia
```

Use this checkout's qualified source environment: Python 3.13.15 and
PySide6_Essentials/shiboken6 6.11.2. No AI dependencies,
downloads or installer are required by this entry point. Missing Qt is a developer
dependency error; this module never installs it. Run from source. [Module inventory](MODULE_MANIFEST.json) explicitly lists the
listed Python files and public support documents for a separate module. The main
portable builder still copies only its six shared Knowledge dependencies; it
does not deliver or activate this browser.

Create an explicitly synthetic publication in an existing developer directory,
then open it with Open exported tag catalog or the optional argument:

```powershell
& .\.venv\Scripts\python.exe -B -m suite_core.encyclopedia.demo C:\path\to\developer\demo.json
& .\.venv\Scripts\python.exe -B -m suite_core.encyclopedia --snapshot C:\path\to\developer\demo.json
```

Demo refuses to overwrite an existing publication. The exported-catalog route only
reads its immutable publication and grants no native editing/import authority.
Native editing and optional import use the separate accepted local database route
described above. No inference, Linux package or application activation is supplied.
Pack preview remains separately available through the existing
`suite_core.knowledge.pack_preview.inspect_pack_preview` API; it is not wired into
this browser and cannot replace the effective snapshot.

UI: substring search covers ID/label/aliases/category path; category selection uses
exact published IDs. Exact resolution uses the existing TagLookup semantics.
Load More starts at 200 rows. Details expose published fields, not full native
Tag Details or inferred inherited policy. Refresh preserves query/category and
stable selection, refuses identity changes, generation rollback and reused
generation content; failure keeps the previous snapshot visibly STALE. Explicit
Open is required to intentionally change catalog identity. No background watcher.
Freshness means the last successful explicit read, not proof of current native
authority. No lifetime or heartbeat is inferred from revision text.

## Publication and native writer boundary

`publication.Publication(catalog_id, generation, snapshot)` wraps the existing
immutable effective DTO. Readers support wire interfaces 1 and 2. Compatible
publications retain exact v1 bytes; nonassignable retirement/unavailability
diagnostics require v2. Empty diagnostic tuples are omitted, while nonempty tuples
and relationship references survive roundtrip. Strict v1 rejects diagnostic fields;
unsupported versions reject clearly. JSON accepts only recognized public DTO fields;
duplicate keys, unsupported versions, malformed rows and checksum corruption
hold. SHA256 detects corruption and binds receipts; it does not authenticate a
publisher. The user/host approves the source and destination separately.

Original native writers own defaults/user precedence, stable identities, recovery
and publication. Immutable reader projections may be discarded; selected editors
retain their original native session and models rather than a second catalog store.
Unknown/private native fields remain in native storage and are
never copied by the codec. Category paths are display labels, not an inferred
hierarchy. Content generation is a positive monotonic integer independent of
software version; a new generation requires a new combined native revision.

`publish(..., expected_token, revision, guard)` requires an existing destination
directory, approved source revision and caller-supplied exclusive mutation guard.
First publication requires expected_token=None and an absent file. Subsequent
publication binds the exact prior token. The sibling temporary file is flushed,
fsynced and read back before atomic replacement; pre-replace errors leave the old
publication intact and clean the temporary file. This is atomic file visibility,
not a qualified power-loss transaction or cross-process lock implementation.

`native_writer.capture_and_publish` accepts the existing
`core.shared_tag_snapshot.capture_tag_snapshot` callable explicitly from an already
initialized host, plus tags/categories/configured aliases/effective relationships.
It captures and publishes under the SAME reentrant guard. This adapter alone does
not import core or initialize native stores. Host must provide an approved whole-scope Source and a
combined revision covering tags, categories, redirects/removals and relationship/
alias changes. It must serialize EVERY relevant writer with that guard, including
other processes, before enabling publication. Generation allocation and persisted
catalog identity remain publisher-owned. App events alone are not a combined guard.

**Hook gap:** no `native_writer.capture_and_publish` post-commit hook is activated. Existing AppState startup runs
recovery/default publication and cannot serve as this browser's startup. Qualify
the native mutation guard, combined revision and post-commit capture hook before
real publication. Capture now uses the native read-only effective identity projection and
availability-aware alias diagnostics; saved lifecycle histories remain unchanged. Everday migration,
legacy mapping and its anatomy/category policies remain its owner's scope.

## Checks

```powershell
& .\.venv\Scripts\python.exe -B -m pytest suite_core\tests\test_encyclopedia.py -q --confcutdir=suite_core -p no:cacheprovider
```

Synthetic Qt workflow, two consumers, 20k scope, pure imports, strict codec,
stale native/publication revisions, atomic replace failure and explicit host
adapter are component evidence. No real catalog or full native recovery acceptance
is claimed. The focused `standalone-encyclopedia` suite maps browser/publication and distribution
tests. The current finite source inventory has 445 unique runtime files, including
the original editor dependencies and explicit sender/receiver seams. All are
present locally and included in the guarded public/component source allowlists.
The original editor treats private promotion as an optional capability unavailable
to standalone editing, so neither private promotion module is required or exported.
This membership is not full public-export or frozen-binary qualification.
Root/control/custody/shared-hold entries are inventoried as
source only with PROFILE=None. Full release export, licensing/runtime artifact and
native gates remain separate.

Codec limits: 32 MiB encoded envelope, 100,000 rows per collection, 300,000 total rows, 1,000,000 associations, 262,144 characters per string, JSON depth 16, 8,000,000 nodes and 4,000,000 entries. Encoder and decoder share these bounds; no larger browser capacity is implied. Existing alpha.2 archives remain unchanged.

Alpha.4 rejects JSON floating-point overflow during parsing, including numeric values in unknown/private input fields. Exporter alpha.5 shares this validation in detached admission. The detached 136.43 MiB fixture has 7,502 tags and 15,006 lookups, with size supplied by private notes and override metadata; it establishes byte-scale behavior only. No dense real-catalog or every-admitted-shape qualification is implied.

Exporter alpha.6 (2026-10-05) qualifies a separate generated dense input shape with 15,002 tags, 51,120 lookups, 21,117 public relationship evidence rows and 277 categories. Its finite input node/entry/category changes do not change this component version, interface 1, 32 MiB public output limits or browser qualification. Diagnostic copies remain nonauthoritative and cannot supply publication approval.

Alpha.5 details show nonassignable retired/unavailable IDs and relationship
attribution in published order. Diagnostic validation uses one tombstone set and
iterative memoization for shared redirect chains. Presentation component evidence
does not substitute for visible GUI or archive/builder acceptance. Preserved older
archives do not gain v2 support; a new versioned package/builder closure is required.

### Historical alpha2 engineering checkpoint

0.1.0-alpha.2, 2026-10-05, snapshot interface 1. Engineering-only inventory/check
notes moved from What's New; browser and publication behavior were unchanged.

- Explicitly inventory the standalone browser's 14 Python runtime files, separate
  from the main application's six shared Knowledge dependencies.
- Register focused browser/publication and distribution checks.
- Restore public-source completeness for the shared Qt result model extraction.

## Hosted examples source integration

Media Organizer owns explicit browser opening/adoption and attachment before show.
Call its `open_hosted_encyclopedia_browser` with the existing opening route, exact
host-admitted Publication, coordinated task factory and GUI dispatcher. The browser
uses the borrowed native session through `attach_hosted_library_session`; it creates
no stores or workers. Rebind after a catalog/scope change. Selection changes and
close invalidate outstanding delivery; native job drain remains host-owned.
Standalone browsing reports unavailable examples without an admitted session.
Native entry-to-results and teardown remain unqualified.

## Maintainer history details relocated from source release notes - 2026-10-09

The following original engineering details are historical contributor evidence, not newly available user features or package/native qualification. Versions and dates retain their original scope.

# 0.1.0-alpha.8 - 2026-10-08 (America/New_York; unpublished source)

Include the existing catalog-preview dependency in the standalone source inventory,
so an isolated source copy can open Tag Encyclopedia. Snapshot reader interfaces
1/2 and saved catalog data are unchanged. Existing archives remain unchanged;
no new package is released or native catalog workflow activated.

# 0.1.0-alpha.7 - 2026-10-06 (America/New_York; unpublished source)

Find example references for a selected published Tag through an explicitly hosted
Media Organizer session. Existing library scope, image identity and evidence query
services remain authoritative. Cancel or change selection/scope to discard old
results. With no admitted host session, examples report unavailable while read-only
Tag browsing remains available. Snapshot reader interfaces 1/2 are unchanged.

Native browser opening, host admission, GUI delivery and shutdown still require
qualification; no alpha.6 archive is changed and no new package is released.

# 0.1.0-alpha.6 - 2026-10-06 (America/New_York; unpublished source integration)

Native snapshot capture/publication holds during incomplete enrolled catalog work
and requires coherent actual participants. Read-only browser and codec wire formats
remain unchanged: snapshot interface maximum2 with minimum lossless v1/v2 encoding.
Native source integration does not publish data or qualify production host startup,
service transactions, real redirects, concurrency, visible UI or activation.

# 0.1.0-alpha.5 - 2026-10-05

Read legacy wire v1 and diagnostic wire v2. Compatible publications preserve exact
v1 bytes; retired/unavailable aliases retain nonassignable IDs and relationship
evidence. Native capture uses admitted effective identity projection without
rewriting saved lifecycle history. Diagnostic admission shares a tombstone set and
memoized redirect classification. Browser details present the retained IDs and
attribution. Pack-preview, native identity-context and exporter-input interfaces
are unchanged. Source component qualification does not activate publication,
qualify visible GUI behavior or update preserved older archives.

# 0.1.0-alpha.4 — 2026-10-05

Reject JSON numeric overflow (`1e9999` and `-1e9999`) as well as literal NaN/Infinity during float parsing. Finite floats and existing accepted interface 1 bytes remain unchanged. Native readers, row/byte limits and browser behavior are unchanged.

# 0.1.0-alpha.3 — 2026-10-05

Public codec now admits structure before parsing, validates the same DTO row/association/text bounds on encode and decode, and hashes canonical JSON incrementally. Interface 1 bytes are preserved for accepted publications. Output remains 32 MiB and 100,000 rows per collection; larger native inputs do not expand consumer capacity. Existing alpha.2 developer archives do not gain these changes. No browser/Qt scale qualification or live capture is claimed.

