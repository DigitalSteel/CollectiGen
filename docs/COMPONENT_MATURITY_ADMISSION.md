# Signed component maturity admission

SUITE-06D2B1 implements an explicit component-library boundary. Discovery,
packaging, profiles, installed-stage observations and native updates do not yet
use it. It grants no installation authority. Historical schema-1 bytes, tags and
SemVer semantics stay unchanged; default root/main-app admission stays schema 1.

`verify_component_release` accepts schema 2 only with caller-supplied
`expected_maturity` matching signed `maturity`. Allowed values are alpha, beta
and stable. Version is canonical numeric major.minor.patch without a display
prefix, prerelease suffix, build metadata, whitespace or Unicode digits.
Alpha/Beta use the existing beta feed channel; Stable uses stable. Product
maturity is distinct from the prerelease feed name.

The exact schema-2 component manifest fields are schema_version, product,
component_id, version, maturity, channel, published_at, supported_from,
release_notes, platform and artifacts. Unknown fields/schemas are refused.
`supported_from` is canonical numeric, no greater than version, and represents a
numeric floor including all maturities of that number. Actual catalog/data
compatibility remains a separate gate. Signature envelope, algorithm, public
trust and component/platform/artifact pins retain the existing owners. The shared
private crypto helper verifies the original bytes; it does not rewrite schema 2
to schema 1. Results retain schema_version and maturity. Schema-1 callers retain
their existing semantics and get schema_version=1, maturity=None; missing maturity
expectations refuse schema 2, and maturity expectations refuse schema 1.

`require_component_maturity_upgrade` is a pure ordering check. Supply an explicit
current_schema and numeric supported_from. Schema-2 observations also require
current_maturity. Compare numeric version first, then alpha < beta < stable;
equal identities and downgrades refuse, regardless of build or artifact bytes.
Schema-1 transitions require a strictly higher numeric target; same-base legacy
transitions refuse. Legacy display mappings never authorize transitions. This
function does not authenticate its inputs or select a feed/artifact. The caller
must bind signed offers, installed identity, compatibility and recovery separately.

Focused checks use the existing disposable RSA fixture and inert archives only.
Neither fixture signatures nor static root checks qualify release signing, native
execution, packaged functionality, migration or an actual rollback pair. Recovery
for this patch is a scoped source revert to its accepted parent; it never restores
user catalog/Data bytes. Existing repository licensing applies; no dependency or
new notice is bundled. No new runnable application checkpoint is delivered.

## Schema2 staging floor alignment (F)

The explicit schema2 transaction path now calls the same
require_component_maturity_upgrade helper after signed target/current admission.
Numeric supported_from includes legacy prereleases at that number, while a
legacy-to-schema2 transition still requires a strictly higher numeric target.
Below-floor, equal/downgrade and unsupported same-base transitions remain held.
Schema1 continues through its unchanged SemVer/ensure_supported_upgrade branch.
Canonical failures retain StagingHold with the original helper cause; no separate
ordering/floor implementation remains in staging. Source checks retain real fixture
signatures, immutable prior journal recovery and synthetic Data sentinels; they
do not qualify actual native, binary rollback, profile or package execution.
