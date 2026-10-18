# Optional catalog transition preview candidate

**Combined source checkpoint, 2026-10-06:** this catalog-alpha2 component is now
assembled with independently accepted shared DTO admission alpha5. That candidate
closes the raw collection-shape prerequisite noted in the historical audit below.
The catalog runtime/version/history are preserved; native review/writer, recovery
and distribution prerequisites remain open. See CATALOG_INTEGRATION_CHECKPOINT.md.

2026-10-06. Preview software `0.1.0-alpha.2`; interface `1`. Application source
checkpoint remains `0.102.2-alpha.1`; pack content versions are caller-supplied
independent identifiers. This is an isolated candidate, not an integrated release.

`catalog_migration_preview.py::preview_catalog_transition` accepts prior default,
next default and effective user `Snapshot` values, plus detached scoped assignment
references. It reuses `TagLookup` admission/identity/alias decisions. No core import,
store constructor, file operation, database, installation, or application activation
is present. Synthetic tests live in `suite_core/tests/test_catalog_migration_preview.py`.

Choose `blank`, `own`, `default` or `merged` explicitly. These modes select proposed
catalog records only; they never clear or reassign image facts. Own retains the
supplied effective local view; deriving a user-only export is the host's existing
layered export responsibility. Default-only adoption holds when user edits/removals
would be lost. Merged compares fields against the explicit prior-default baseline,
keeps edited fields and updates untouched fields. If an old default is absent
locally without a portable tombstone, it holds rather than resurrecting it. New
same-ID records with no known baseline preserve local authority and hold on drift.
Removed defaults retain local records and their before values. Different IDs with
colliding normalized names/aliases remain separate and hold for native review.

The Foundation bridge may supply explicit, native-plan-fingerprinted ownership
evidence without replacing either comparison Snapshot or the final native equality
check. An unchanged default donor may leave assignable records only when the target
declares a reviewed redirect to a live survivor and native user Tags/lifecycle and
relationship participants prove no local ownership. Identical-looking stored or
restored records remain protected, including native notes/properties/provenance
outside the DTO. Missing targets, cycles and retirement without a live redirect
remain held; ordinary absent defaults continue to retain their before records.

Combined effective lifecycle is not itself proof of user retirement. The bridge
may qualify a redirect/tombstone as shipped only when it exactly matches the prior
source lifecycle, the target ID is live, and fingerprint-bound native participants
show no user record, lifecycle or relationship ownership (including an owned
redirect target). Other local retirement takes precedence. Whole stored user
records retain their complete effective DTO row for Foundation proposals, even
when a field happens to equal an older default; generic previews still use the
field comparison above. Tracking freshness remains part of this native evidence.
These inputs authorize comparison proposals only, never a writer or reassignment.

Each assignment retains its exact occurrence ID, stable ID, original value and
evidence reference. Unchanged IDs never change when displays change. Missing-ID
exact alias and reviewed redirect matches offer an explicit target choice. User
alias suppression/ownership precedes incoming pack aliases. Ambiguous aliases
list their exact targets. Explicit native refinement offers list specific choices
alongside Keep Original and Skip Review; no subtype, owner, object, modifier or
identity is inferred. The host must produce offers from its reviewed native
refinement planner and carry full relationship/modifier evidence separately.
Offer references alone do not certify historical reconstruction or replacement.

`CatalogPreview` retains the base/target pack identity, independent content version,
source revisions and fingerprints, full before records and original detached
local snapshot. `rollback_snapshot` is comparison evidence, not a native recovery
receipt. A downgrade can be previewed without overwriting newer user edits.
`matches_catalog_inputs` checks catalog freshness only; assignment, offer, image,
policy and writer freshness must be revalidated by the host.

Returned records are proposals, not an effective lookup Snapshot or an apply plan.
Category/relationship changes, disabled defaults beyond the supplied effective
projection, native field-override ledgers and unknown fields remain native owners'
responsibility. Conservatively protecting a whole field can retain a value equal
to a newer default; the native ledger remains the authority for clearing overrides.
Complete scoped snapshot admission is required; partial/out-of-scope inputs fail.
Assignments, offers and refinement target IDs accept only explicit built-in lists
or tuples. Shape, element types and cardinality are validated before immutable
tuple conversion. Strings/bytes, mappings, sets, nulls and lazy/custom iterables
are rejected without consuming arbitrary iterables. Assignment input is bounded
to 10,000 occurrences and offers to 1,000, each with 1..64 distinct nonblank
explicit target IDs. Snapshot admission keeps its existing contract.

Adjacent boundary audit: the unchanged shared DTO constructors also normalize
collections unconditionally. Scalar `"ab"` becomes `("a", "b")` in `Source.scope`,
`EffectiveTag.aliases`, `Lookup.owner_ids` and `Snapshot.removed_ids`. A preview
receiving those normalized DTOs cannot recover their original input shape. These
require separate shared-contract hardening; this candidate does not edit those
accepted supporting modules or certify their malformed-input boundaries. Native
producers must continue supplying explicit, reviewed snapshot collections.

Integration prerequisite: bind these DTOs under native revision/lock and privacy
authority, render the proposed changes/holds and exact occurrence choices in the
existing review controls, and feed only separately reviewed decisions into the
existing guarded writer/history/Undo flow. Qualify stale review, cancellation,
native unknown-field retention, backup/read-back and exact rollback before any
live migration. A standalone immutable default pack manifest/history and optional
pack selection/setup UI still require their own integration milestone. No current
roadmap parent is marked complete; this extends the KM-04/family planning boundary.

Product fit: review optional vocabulary updates without losing image facts or
personal edits, reusing snapshot/identity and scoped refinement authority.

## What's New — 0.1.0-alpha.2 — 2026-10-06

- Reject scalar/mapping refinement targets before conversion, fixing the reviewed
  P2 case where `target_ids="ab"` invented endpoints `a` and `b`.
- Apply the same explicit list/tuple admission to assignments and offers;
  validate types/bounds and distinct nonblank targets before freezing inputs.
- Retain legitimate two-target choices, evidence references, Keep/Skip and
  original assignment provenance; add 40 malformed/valid boundary regressions.

## Previous candidate — 0.1.0-alpha.1 — 2026-10-06

- Add detached four-mode catalog transition proposals with conservative user
  field precedence, removal provenance and collision holds.
- Add exact alias and one-to-many specific-choice review rows while preserving
  all original image assignment references.
- Retain independent prior/next content versions, snapshot fingerprints and
  original-state comparison evidence for future native recovery integration.
