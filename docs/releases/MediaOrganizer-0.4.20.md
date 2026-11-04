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
