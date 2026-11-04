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
