# TRN-08 Training Experiments draft handoff (unreleased source)

Manager Applications → Training Experiments uses the existing Manager Training
history to save experiment names, notes and favorites, compare two retained runs,
and create a separate draft from an execution run. Export Saved Draft writes a
new JSON file. Existing files are preserved.

In Organizer, open a named library, then Dataset → Library Actions → Import
Training Draft. Import saves a fresh Dataset-owned copy and opens the existing
candidate editor with current image knowledge, duplicate assessment and saved
configuration. Save Draft updates this copy. The Manager experiment, transfer
file and original sources are preserved. Collection drafts first ask the user
to choose and review the current Collection; saved goal/settings are applied to
that reviewed membership. Cancellation retains any already committed copy.

Product fit: reuse the canonical experiment browser, draft store, source reader
and Dataset planner to make iteration editable across the modular suite. No new
trainer, updater, global registry or automatic installation is introduced.

This source change is not a delivered package. Manager a.0.6.0 and the Organizer
successor must be rebuilt and delivered through their existing component routes.
Do not change download links to unpublished artifacts. This does not certify
native training or Root/Reader behavior, and does not require manual acceptance
before independent feature work continues.
