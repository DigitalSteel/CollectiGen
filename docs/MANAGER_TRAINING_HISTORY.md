# Manager Training history management

Implementation contract for SUITE-10C. Runtime delivery uses independently
versioned signed Manager packages; source and package qualifications are distinct.

## User workflow

In Training, choose Review Saved Training Runs or Saved SDXL Preparations.
Select a row and choose Forget From This List to remove that index entry.
Reviews, checkpoints, output files and model registrations stay in place.
When a saved-list transaction was interrupted, Recover Saved List finishes the
original recorded transaction before the list can be used again.

Use the existing TrainingHistoryDialog, including its paging, explanation,
worker progress, retained results, cancellation and normal Close behavior.
Forget is a secondary pointer-management action. Recovery is the next action
when the list itself is held; it does not start or resume training.

## Scope and authority

ScopedTrainingHistory and ScopedPreparationHistory require an explicit
write_allowed callback for these two actions. The default remains read-only.
Manager binds the callback to its exact active generation/Training state.
An ended or closing session cannot start a list mutation.

Forget validates the complete scoped snapshot and the reviewed revision, then
delegates to TrainingRunIndex.forget. The original index transaction checks the
revision again under its lock and retains backup, read-back and recovery rules.
No linked run files are opened or deleted by Forget.

Recover delegates to the original TrainingRunIndex recovery transaction with
a validator for both retained index states. Every before/after pointer must
belong to the same host scope; preparation pointers retain their stricter
layout/kind rules. An invalid state, changed current index, ended session or
changed recovery evidence leaves the pending transaction held.

Existing receipt review, run execution, registration and model/library authority
stay with their original services. This change grants no training execution,
profile migration, Reader or C3A authority.

## Evidence and delivery boundary

tests/test_manager_training_history_management.py covers pointer preservation,
default/revoked capability, stale revisions, session ending during review,
pending recovery and foreign before/after states. Its visible-control cases
enter through the real Manager Training actions and exercise both index types.
Existing retained-review tests continue to cover handoff and closure ownership.

Synthetic offscreen evidence establishes source-component behavior only. It is
not a novice user study, live trainer run or installed package qualification.
The existing manager-retained-training-review journey remains partial for the
broader packaged workflow. Ship through the existing independently versioned,
signed Suite Manager package and update public download links after publication.

## Packaged list management and button fit

Published Manager a.0.6.7 exercised Recover Saved List and Forget From This List
for both run and preparation indexes through its original Windows launcher.
A fresh synthetic installation recovered sealed interrupted list writes,
removed the remaining pointers, preserved all review/output bytes, closed
normally and reopened both empty lists without reseeding. No trainer, model,
Reader, C3A or real library was exercised. This qualifies the bounded list
management delta; the broader retained-run review journey remains separate.

Native captures also exposed clipped Refresh/Forget labels with Manager fonts.
The host now reuses its existing compact_action sizing for the list controls
and Close, retaining the longest selected-review caption minimum. Index writes,
recovery, readiness and task ownership are unchanged. Delivery of this visual
fix uses the next real Manager patch; no version is bumped for evidence alone.
