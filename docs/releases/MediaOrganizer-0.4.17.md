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
