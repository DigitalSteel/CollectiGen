# Generate and Train source candidates

Generate and Train are separate applications, with independent component IDs,
versions, manifests, frozen entries and package inventories. Suite Manager owns
Applications, installation, updates, shared setup and connections. Workflow
controllers and their canonical saved data remain shared implementation services.

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
