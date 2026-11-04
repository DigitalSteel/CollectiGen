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
