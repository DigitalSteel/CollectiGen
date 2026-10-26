# Saved Reader selection identity

ReaderConnection preserves its original three positional fields. Optional
release_schema=2, maturity and release_sha256 carry a canonical numeric release,
explicit alpha/beta/stable stage and lowercase SHA256 digests. Legacy defaults
remain release_schema=1 with no separate stage/digest. New selections require an
absolute, bounded local path without traversal or NUL; ordinary package review
retains the existing filesystem/reparse/inventory checks.

ReaderConnectionStore serializes new selections with exactly schema_version=2,
release_schema=2, directory, inventory_sha256, version, maturity and
release_sha256. Unknown/missing/mistyped fields or unsupported schemas hold.
Legacy schema1 mappings and unknown selection fields remain compatible. An
explicit format promotion refuses unknown legacy corrections instead of pruning
them. Explicit selection rollback to schema1 removes only the known new identity
fields; unrelated top-level settings remain intact. This changes selection only,
not installed code or Data. The existing writer owns backups, atomic publication,
readback and stale settings guards. Reopening retains the complete identity.

review_directory with an explicit new-format expected selection retains that
identity while rechecking exact inventory/version/payload/platform. It cannot
authenticate the stage or release digest from inventory bytes. Without an expected
selection it returns the legacy inventory-only result, never an inferred stage.
These DTOs and saved settings grant no publisher trust or installation authority;
the future original-host adapter must compare independently signed journal state.

RootReaderSelection.context now retains the full selected identity through the
existing strict Manager helper adapter; its inventory/manual selection authority
is separate from publisher authentication. [Original-host identity](READER_HOST_IDENTITY.md)
describes explicit signed-state binding and the remaining acquisition/activation
holds. Legacy helper context remains exact. Fixture saved-state evidence does not
establish native health, visible entry acceptance or package qualification.

Existing licensing applies. Scoped source revert to the accepted preimages is
code recovery; do not use it to relabel saved schema2 documents or overwrite Data.
An older host refusing a new selection requires original-owner review/recovery,
not silently stripping its stage. No runnable app version or release tag is added.
