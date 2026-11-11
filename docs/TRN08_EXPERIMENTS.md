# TRN-08 Training Experiments draft handoff (unreleased source)

Manager Applications → Training Experiments saves experiment names, notes and
favorites, compares retained runs, and creates a separate editable draft from
an execution run. Export Saved Draft writes a new JSON file without overwriting
an existing file.

In Organizer, open Dataset → Dataset Plans → Import Training Draft. Import saves
a fresh Dataset-owned copy and opens the existing candidate editor with current
Image Knowledge, duplicate assessment and saved configuration. Save Draft
updates this copy. Reopen it with Dataset Plans → Saved Dataset Drafts. The
Manager experiment, transfer file and original images remain unchanged.
Collection drafts ask you to choose and review the current Collection before
applying the saved goal and training settings to its current images.

Dataset's existing deferred image-status checks pause while this handoff is
choosing its draft or Collection; an existing task finishes normally. Import
shows when its current Dataset is ready. Closing cancels at the existing safe
boundary and preserves any already committed copy. No manual acceptance hold
or new worker, updater, registry or training editor is introduced.

The real Organizer ribbon routes for ordinary and Collection drafts were
exercised with generated images: import, candidate role override with a reason,
Save Draft, reopening the same copy, original preservation and normal Close.
Manager export and import-readiness component checks also pass. These are source
checks, not installed-package or native training qualification.

This source is not a delivered update. Manager a.0.6.0 and an Organizer successor
still need their existing package/release routes. Download links must continue
to point to actual published assets until those updates are delivered.

## Implementation owners

| Boundary | Existing owner |
| --- | --- |
| Experiments entry and host context | `suite_core/manager/window.py::create_window` |
| Shared experiment browser routing | `qt_app/training_workflow_controller.py::TrainingWorkflowController.experiments` |
| Canonical transfer publication | `core/training_draft_transfer.py::export_training_draft` |
| Transfer review and Close | `qt_app/training_draft_transfer_dialog.py::TrainingDraftTransferDialog` |
| Focused component checks | `tests/test_training_draft_transfer.py` |
