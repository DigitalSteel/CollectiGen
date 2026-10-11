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

