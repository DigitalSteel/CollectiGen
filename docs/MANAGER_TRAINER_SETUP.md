# Manager Trainer Setup lifecycle

Owner: TRN-04L / SUITE-10B. Source candidate; qualification and delivery tracked separately; Manager 0.7.7 remains the published release. This completes the trainer software lifecycle available in the original all-in-one; it does not add new training methods or execute a model.

## Existing stock workflow

Training > Set Up Trainer uses TrainingSetupDialog and TrainerSetupQueueController. Managed installation is available only with a valid reviewed runtime catalog. Existing installation verification, offline import/export, repair, activation and rollback use the original TrainerSetupExecutor, immutable generations, pinned software catalog, health checks and recovery receipts. Installation does not activate software or start training. Managed/import/repair results require explicit activation before saving local resources. The resulting selection reloads through Load Saved Trainer.

## Confirmation, continuity and recovery

The queue is Manager-local at training/setup/setup-queue.json. Opening Setup reads metadata only and starts no acquisition or saved operation. Review Saved Operation reconnects running/completed work, or prepares a new review of recovered waiting/failed/cancelled work; resuming requires confirmation. Cancel Saved Operation cancels only the selected original row. Original executor recovery records are never deleted or fabricated.

A dismissed setup dialog leaves confirmed work owned by the same Manager queue and jobs. Reopening exposes saved operation review. Manager Close requests cancellation and retains the original job, process and recovery ownership until settlement. Uncertain native owners continue to hold Close. The Manager observation subclass changes no stock process execution or qualified source module.

## Profile and data boundaries

Settings, queue, health and software generations use the admitted Manager data root. Protected paths are rechecked before preparation, dispatch, completion and resource save; resource saving checks the actual verified generation software root rather than a generation ID or an acquisition plan dictionary. Stale profile/action/review/session state cannot replace retained work. No Dataset library, model, tokenizer or output is silently adopted. Original settings revisions and resource-save readback remain authoritative.

## Owners and evidence boundaries

- core/manager_trainer_setup.py: Manager-local stock queue/executor binding, protected paths, session and Close observation.
- qt_app/training_setup_queue_controller.py::ManagerTrainerSetupController: stock action reviews, exact retained-row confirmation and completed result access.
- suite_core/manager/trainer_setup_ui.py: shared setup dialog, saved operation review/cancel, metadata-loading cancellation and modal resource-save return.
- suite_core/manager/window.py: normal Training entry and Manager Close integration.
- tests/test_manager_trainer_setup.py and tests/test_manager_trainer_setup_ui.py: synthetic boundary and UI cases. Actual outcomes are recorded with the source checkpoint and published release.

Final checks start only after this connected feature is complete. No acquisition, health/trainer/model execution, Reader/C3A or real libraries have been used during implementation. Source checks alone will not count as a delivered update; next delivery is the existing collectigen.suite-manager package route. Full legacy parity and actual failed-start recovery remain open until their final evidence is complete.
