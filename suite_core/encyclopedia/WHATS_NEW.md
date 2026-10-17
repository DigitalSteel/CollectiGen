# Tag Encyclopedia a.0.3.0 - Alpha candidate

2026-10-14 (America/New_York)

Documentation reconciled 2026-10-17.

- Create a separate local database from Home, with confirmation before writing.
- Browse the selected catalog and use its native Tag and Category editing tools.
- Includes the corrected category and reload workflow in this alpha candidate.
- Review optional defaults before applying changes; retain your own records and corrections.

Keep a verified backup and use copied catalogs for alpha testing. This
alpha requires a matching verified package and release signature. Public
updates and clean-machine use remain unverified. Standalone startup does not discover an Organizer library.

# Tag Encyclopedia a.0.2.2 - Alpha candidate

2026-10-14 (America/New_York)

## Improvements

- Setup starts in a smaller window sized for its content. Optional notes and details can expand while the actions remain reachable.
- Check Again has a clear button outline. Setup uses plain wording and still distinguishes an unchecked installation from an observed or out-of-date status.

## Bug fixes

- Fix a case where a temporary Windows status-check failure could leave normal Close waiting. The pending Close request is retained while the same process-scope check retries.

These changes have source tests and remain a candidate for an unsigned build. Packaged startup/Close and authenticated installation, update and recovery still require qualification. The previous a.0.2.1 candidate and published a.0.1.8 package remain unchanged.

# Tag Encyclopedia a.0.2.1 - Alpha candidate

2026-10-14 (America/New_York)

- Install or Update uses one confirmation showing the exact reader, package size and destination. Downloads require that confirmation.
- Standalone setup checks the downloaded package for compatibility before installation. A failed check keeps your existing reader, catalogs and settings.
- Installation status distinguishes an unchecked reader from an observed absence or installed version; unsuccessful refreshes label retained observations as out of date.
- After installation, Open the reader and close it normally to finish setup before another Update.

This source candidate awaits frozen/native installation, Open, normal Close, update and recovery qualification. The published a.0.1.8 package is unchanged.

# Tag Encyclopedia a.0.2.0 - Alpha source candidate



2026-10-12 (America/New_York)



Development changes:



- Home offers Open Tag Database for a current personal database, without Manager or an internet connection. Opening it does not change your saved data.

- Use optional default tags opens a separate review of local Tag content and its destination. Import, merge and recovery remain explicit; your local records and corrections are retained, including when the database already has personal Tags but no defaults.

- Browse categories on the left, find Tags by name or alias, and copy the displayed Tag name. Technical IDs and exact lookup controls stay in advanced details.

- Help offers these dated notes, version information and an optional update check. Checks only report availability; they do not download or replace this application.

- Organizer can supply its selected local libraries for image examples and open accepted results in Dataset. Example cards show the library name, with its exact ID in a tooltip. Closing waits for that work to finish safely.

- Edit Tags and Categories opens the original forms for the selected native database. Save and Cancel retain the same catalog authority; changing the catalog or closing waits for those original decisions and work to finish.

- Optional downloaded Tag content can be reviewed before separate connection, Preview and Apply decisions when an approved acquisition service is supplied. Ordinary startup does not supply a live feed or trust.

- An explicitly connected Organizer browser can request existing image search or reviewed Tag-name correction. Requests use current native identity and revision and never automatically change image metadata. Its caller, saved settings and reopen route are integrated; native/package and image-writing acceptance remain separate.



- Explicit original-root development configurations now show reviewed first-install controls, followed by an explicit Open installed reader action.

- Enrolled readers can review and confirm their original update, inspect status, cancel a pending update or confirm retained recovery; normal Close still precedes code replacement.

- Saved Tag imports return their verified reader promptly; optional behavior observations are cleared for explicit refresh, and post-save observation holds remain visible.



Default-content preservation fixes (source, 2026-10-13):



- Default-content review keeps your complete stored Tag definitions, including values that also matched older defaults. Reviewed default redirects and removals remain separate from your own retirement decisions.

- Earlier definitions and ambiguous names that cannot yet be reconciled keep Apply unavailable. Existing-catalog upgrades remain under compatibility review; fresh catalogs have a separate qualification path.



This is a development source update. The previously published a.0.1.8 package is unchanged; a new tested package has not yet been released. Packaged/native startup, process-restart persistence, recovery, scale, richer published policy details and executable updating still need qualification. No personal images or models are included.



# a.0.1.8 (embedded 0.1.0-alpha.8) - 2026-10-08 (America/New_York; unpublished source)

Fix a missing dependency that could prevent Tag Encyclopedia from opening. Existing snapshots and saved catalog data are unchanged. This source update does not release a new package or enable live catalog publishing.

# a.0.1.7 (embedded 0.1.0-alpha.7) - 2026-10-06 (America/New_York; unpublished source)

Find example images for a selected Tag when an approved Media Organizer library connection is supplied. Cancel or change the selected Tag or library scope to discard older results. Without that connection, examples report unavailable while read-only Tag browsing remains available.

This source update does not activate examples in normal startup or change existing packages. Opening examples and closing their work still require native acceptance.

# a.0.1.6 (embedded 0.1.0-alpha.6) - 2026-10-06 (America/New_York; unpublished source integration)

Snapshot refresh refuses catalogs with unfinished changes, preserving the current read-only snapshot. Existing browsing and saved data are unchanged. Publishing from a live catalog is not enabled by this source checkpoint.

# a.0.1.5 (embedded 0.1.0-alpha.5) - 2026-10-05

Read older and newer snapshots without rewriting saved catalog history. Tag details retain unavailable or retired identities, relationship information and attribution without making those identities assignable.

This source update does not enable live catalog publishing or update existing packages. Native browser acceptance remains open.

# a.0.1.4 (embedded 0.1.0-alpha.4) — 2026-10-05

Reject malformed snapshot numbers, including overflowing and non-finite values. Valid snapshots and existing browsing behavior are unchanged.

# a.0.1.3 (embedded 0.1.0-alpha.3) — 2026-10-05

Reject oversized or excessively complex snapshots consistently when reading and saving publications. The existing limits remain 32 MiB per snapshot and 100,000 rows per collection.

This source update does not expand browsing capacity, update existing packages or enable live catalog capture.

# What's New in Tag Encyclopedia a.0.1.2 (embedded 0.1.0-alpha.2)

2026-10-05

No user-visible changes in this version.

Editing, applying imports, installers and Linux builds remain unavailable. Live catalog publishing is not enabled.

---

# What's New in Tag Encyclopedia a.0.1.1 (embedded 0.1.0-alpha.1)

2026-10-05

- Open a local read-only Tag Encyclopedia independently of the main application.
- Open an approved snapshot, search labels, aliases and IDs, filter categories, load more results and inspect identity details.
- Refresh explicitly. An older or invalid snapshot keeps the previous one visibly stale rather than silently replacing it.

The runnable demo uses sample data. Live catalog publishing, editing, applying imports, Everday integration, installers and Linux builds remain unavailable. Existing main-application packages are unchanged.

