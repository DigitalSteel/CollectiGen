# Untrusted component maturity locators

SUITE-06D2B3A adds pure opt-in parsing to the existing discovery owner. It does
not authenticate a release, choose a winner, fetch an artifact or enable a feed.
ReleaseLocator's appended schema_version/maturity fields are untrusted hints;
legacy construction defaults to schema_version=1 and maturity=None.
production_install_authorized remains false.

`discover_candidates(raw, component_id=..., channel=..., complete=...,
include_schema2=True)` returns a bounded tuple of candidates in listing order.
It retains all same-number stages and applies no installed-version filter or
ranking. Later authenticated acquisition owns highest-numeric grouping, signed
stage ordering, full installed context and exact-offer checks. Without explicit
opt-in it collects legacy candidates only. Existing discover_releases remains
the default legacy winner selector, and discover_component_ids ignores the new
tag grammar. Existing callers do not gain schema2 capability.

The new exact tag is `<component>/releases/schema2/<numeric>/<maturity>`, with
canonical numeric version and alpha/beta/stable. The rejected unshipped v2 marker
is not adopted. Alpha/Beta select the beta feed; Stable selects stable. Drafts
are excluded. Selected reserved namespaces reject malformed or unknown schema
segments; unrelated component scopes are ignored. Schema2 requires positive
release/asset IDs and refuses duplicate tags, release IDs and cross-release asset
IDs. Repository identity remains the existing fixed source owner and later service
binding; these unsigned hints never establish publisher trust.

`release_asset_identity(url, include_schema2=True)` recognizes new tags only at
the existing first-party HTTPS origin with the full tag encoded into one path
segment. It decodes once and requires exact canonical reconstruction. Literal
tag slashes, alternate percent spelling, double encoding, basename separators,
extra paths, query/fragment and wrong origins refuse. The default legacy helper
retains its original validated slash variants. Stock transport still calls the
default helper and rejects schema2 URLs even after an opted-in pure parse.

The shared listing/asset guards retain the 1 MiB JSON, 100 release, 32 asset,
64 component-ID, 255-character basename and 2048-character URL bounds and exact
types. complete must be explicitly true; it is a caller pagination claim, not
network qualification. No network, filesystem, signature, transport policy or
installation side effects occur in the new parsing seam. Tests use inert metadata
and existing contained fake-transport fixtures. The previously omitted pure legacy
test module is enrolled from reviewed source with its preimage retained.

B3B owns authenticated acquisition/revalidation; B3I owns stage-aware installed
references and collision-free destinations/recovery. Actual schema2 acquire,
profile activation, native functionality and rollback qualification remain held.
Code-only source recovery uses the accepted parent/preimages, never user Data.
No dependency, license obligation or runnable app version is added.
