# Pure installed component references

`component_reference_from_release(release)` and
`validate_component_reference(reference, component, *, release)` live with the
existing component staging owner. They are pure future-contract helpers. Current
payload, installation, journal, saved connection and recovery entrypoints do not
call them. Schema2 artifact acquisition remains unconditionally blocked.

Both helpers require an exact `VerifiedComponentRelease` DTO. Callers must obtain
it by verifying the original manifest/signature with independently admitted trust
and exact component/version/channel/platform/maturity expectations. A constructed
DTO is not proof of signature provenance, installed ownership or admission. These
helpers grant no filesystem, transfer, installation or recovery capability.

Legacy output retains exactly component_id, version, directory and
inventory_sha256, including the original Apps/component/exact-legacy-version
directory and canonical serialization. Legacy maturity must be None.

New output has the exact keys schema_version=2, release_schema=2, component_id,
version, maturity, directory, inventory_sha256 and release_sha256. Numeric version
and explicit alpha/beta/stable reuse ComponentReleaseIdentity validation. The
canonical relative directory is Apps/component/schema2/numeric/maturity, so
same-number stages are distinct from each other and from legacy destinations.
Channel and platform must agree with the verified identity. Digests are lowercase
64-character SHA256: inventory from the signed artifact and release from the
original verified manifest. No inventory-only inference of maturity is possible.

Validation requires exact component, key sets, field types and mapping equality
against that verified DTO. It returns a fresh mapping and does not mutate the
input. Unknown fields/schemas, booleans as integers, mixed legacy/new identities,
changed stage/digests/component and noncanonical paths are refused. Construction
uses the existing relative-path validator without resolving or reading a path.

No helper checks whether a directory exists, authenticates a saved connection,
settles a journal or launches a process. The existing immutable destination guard
and independently signed prior-history recovery remain mandatory at integration.
Journal3, connection2 and original-host freshness adapters are future work under
the installed-identity integration plan. No runnable app or schema migration is
published by this source contract; existing licensing applies.
