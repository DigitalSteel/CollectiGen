# Generate and Train source candidates

Generate and Train are separate applications, with independent component IDs,
versions, manifests, frozen entries and package inventories. Suite Manager owns
Applications, installation, updates, shared setup and connections. Workflow
controllers and their canonical saved data remain shared implementation services.

## Train preparation presentation

`suite_core/train/surface.py` owns the family-neutral entry. The existing
`suite_core/manager/krea_training_review.py::KreaTrainingReviewDialog` presents
the verified target/method, data scope, original preparation worker and detailed
evidence; its historical source-directory name does not make this an embedded
Manager workflow. `qt_app/training_history_dialog.py::TrainingHistoryDialog`
owns the shared saved-list presentation and exact selected-record identity.
`tests/test_train_preparation_clarity.py` covers the novice/expert disclosures,
unsupported model/method branches, delayed checks, retention, current readiness
and saved reopening with generated component fixtures. Original module admission,
native/package/update/rollback qualification remains a separate required gate.

Shared-source recipe pins track the current source tree. Updating such a pin
does not rebuild or republish an existing Manager/Generate version. Each actual
component release must bind its own new version and exact qualified artifact.

The existing `prepare_manager_production_stage.py`,
`build_manager_distribution.py --component generate|train`, and
`build_component_package.py` producers provide the finite build/package routes.
The two source manifests in this directory seal their respective closures; they
are source inventories, not freezer or native qualification receipts.

A matching full-suite launcher/bootstrap 0.1.1 and Manager 0.8 are required.
Updating Manager alone cannot add the new Root admission contract. Generate and
Train launch only through the original authenticated Manager/Root helper path;
running a source entry or passing workspace arguments does not grant access.

No qualified Generate or Train download is published by this source patch.
Compiled admission and acquisition profiles remain unavailable until the exact
native route, signed package metadata, update/recovery path and release assets
are qualified. Retain approved public Manager admission evidence using the
existing producer helper; do not add private signing keys, arbitrary peers,
unsigned feed entries or a second updater. Source checks and unsigned ZIPs do
not establish delivered updates.
