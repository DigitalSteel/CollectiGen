# Inert app capability, availability and search handoff contracts

Contract schema 1, 2026-10-13. Source/synthetic lane only; no standalone endpoint or discovery UI activation,
credentials, app launch, discovery scans, new persistent registry or settings owner.
Existing installation/module manifests and settings are unchanged. The existing
in-process hosted sender now uses terminal receipts and exact recipient scope guards;
that source integration is described below and does not activate standalone transport.

Product fit: reuse current app identities, inventory and native admission to make
knowledge-to-library actions discoverable and recoverable while preserving explicit
scope, privacy, canonical Knowledge and image review authority.

## Stable Python API

`core.app_capabilities`:

- `AppBinding(component_id, installation_id, binding_revision)` uses existing
  `ComponentId`, `InstallationId` and `StableId` nominal identities.
- `CapabilityContract(capability_id, request_versions, response_versions, effect,
  transports, required_context)` is immutable. Versions are nonempty sorted unique
  tuples of positive integers; capabilities/transports/context are bounded.
- `AppCapabilityDeclaration(binding, software_version, declaration_revision,
  source_ref, capabilities, schema_version=1)` is immutable and validates the existing
  SemVer parser. `mapping()` returns detached JSON-compatible data; `digest` is a
  canonical SHA256 identity, **not** publisher authentication.
- `negotiate(declaration, capability_id, *, request_versions, response_versions,
  transport)` returns `NegotiatedCapability` with the highest exact intersecting
  request and response versions. Software ordering never implies schema compatibility.
- `TAG_SEARCH_CAPABILITY` is `collectigen.organizer.tag-search.open`. Its declaration
  requires `OPEN_SEARCH` effect and profile/library/privacy/scope_revision context.
  Generic settings effects describe support only; the shared-settings owner retains
  permissions, transaction, schema and authority decisions.

`core.app_availability`:

- `DimensionObservation(state, source_ref, observed_at, attempted_at, valid_for,
  evidence_level, reason='')` is immutable. TTL is at most 30 seconds; timestamps are
  aware. Freshness reuses `suite_core.registry.status_view.Freshness` and its predicate.
- `AppAvailability(binding, installed, configured, running, reachable, compatible,
  capability_supported, capability_id, endpoint=None, peer_proof=None)` requires the
  six **different** enum classes `InstalledState`, `ConfiguredState`, `RunningState`,
  `ReachableState`, `CompatibleState`, `SupportedState`. No dimension rewrites another.
- `AppEndpointBinding(app, connection_ref, instance_id, endpoint_ref,
  declaration_digest)` binds opaque identities, not executable paths or credentials.
- `SelectedPeerProof(endpoint, source_ref, validated_at, valid_for)` is an original
  validator assertion. Constructing this DTO proves nothing about a peer. The future
  producer must qualify exact selected installation, live instance and endpoint
  ownership; pipe name/PID/health response alone is insufficient.
- `project_availability(declaration, observation, capability_id, *, now,
  request_versions=(1,), response_versions=(1,), transport=LOCAL, relevant=True)`
  returns immutable `AvailabilityDecision(enabled, visible, reason, negotiation)`.
  Its `execution_authorized` is always false. All positive dimensions must be fresh;
  latest failed/unverified attempts, future/stale observations, configured-only
  runtime claims and missing/mismatched/expired peer proof cannot enable a handoff.
  Offline relevant actions remain visible; unsupported irrelevant actions may hide.

`core.organizer_encyclopedia_handoffs` additive APIs:

- Existing `MatchingImagesRequest`, `CanonicalRenameRequest`, `decode_request` and
  `admit_request` remain unchanged. Hosted rename review remains with its old owner.
- `RecipientSearchScope(profile_id, library_ids, privacy_scope, scope_revision)`
  contains original admitted opaque IDs, never paths or automatically selected
  profiles. Library IDs are a nonempty sorted unique tuple (maximum 64). Only
  `local_explicit` and `explicit_connection` are admitted privacy scope labels;
  Local Only cannot become an endpoint scope.
- `PreparedRecipientSearch(target, scope, prepared_at, valid_for)` is the retained
  recipient-owned preparation snapshot (TTL maximum 30 seconds), not incoming claims.
- `SearchHandoffEnvelope(request_id, source, source_generation, target, negotiation,
  scope, payload, issued_at, expires_at, schema_version=1)` requires exactly
  `MatchingImagesRequest` v1, matching envelope/payload request IDs, local transport,
  search request/response schema 1, matching endpoint declaration digest and deadline
  up to two minutes. Rename, another capability or transport, schema booleans, and
  altered request IDs hold. `fingerprint` includes all request/target/scope content
  using canonical UTC times.
- `admit_search_envelope(envelope, *, prepared, current_scope, current_target,
  current_source, current_source_generation, peer_proof, catalog, knowledge, now)` requires current
  scope to equal both prepared scope and envelope scope **exactly**, selected live
  target, selected source binding and generation to match, current peer proof, preparation/request freshness
  and all existing catalog revision/stable identity/borrowed Knowledge checks. It
  returns `AdmittedSearchHandoff(envelope, display_name, admitted_at)` and performs
  no dispatch. No raw mapping/bytes transport codec is admitted in this lane;
  untrusted mappings cannot bypass these exact typed inputs.
- `SearchSettlementLedger(maximum=128, retention=5 minutes)` is single-owner and
  in-memory, capped at 512 records and five minutes after request expiry. Caller
  serializes access; no locks, services or persistent queue are added.
- `queue(admission, *, now)` requires admission at the supplied current instant and
  returns a request-scoped `SearchHandoffReceipt`. Same source/request ID and same
  fingerprint returns the existing receipt; changed content holds without replacement.
- `record_delivery(admission, *, now)` records the owner's explicit assertion only
  after actual native opening and fresh admission at that instant. It rejects
  cancelled, held, expired, delivered or unknown requests. `lookup(envelope, *, now)`
  enables a future sender to observe final settlement independently of queued return.
- `hold(envelope, *, reason, now)`, `cancel(envelope, *, now)` and
  `connection_lost(envelope, *, now)` settle a queued request. Loss is terminal
  `UNKNOWN_OUTCOME`; it neither resends nor erases the original receipt. Expired queued
  requests become `EXPIRED`; old records age out without allowing an expired request
  to queue again. Clock rollback holds. Capacity never evicts active records.
- `invalidate_source_generation(source, generation, *, now)` is a trusted source
  observer notification; it holds queued requests for other generations only.

Receipt stages are `QUEUED`, `DELIVERED`, `HELD`, `CANCELLED`, `EXPIRED`,
`UNKNOWN_OUTCOME`. Delivered means the exact native search opened, not saved image
changes, exhaustive stable-ID search results or successful image Apply. Receipts
  carry identity, fingerprint, generation, stage/content-free reason code/times; no catalog payload or
image content is retained. Replay protection ends with this object's lifetime:
after process restart, ambiguous submissions must stay unknown until separately
qualified recovery, never auto-resend.

## Endpoint activation prerequisites

The APIs validate supplied assertions; they are not a working endpoint. All of the
following gates remain open before any UI, local transport or remote connection:

1. Coordinate exact Organizer entry and Encyclopedia browser ownership with the
   active integration/QA/native/settings owners. Retain existing revision guards.
2. Enforce exact prepared recipient profile, included library IDs, privacy and scope
   revision **inside the consumed Dataset operation**, including deferred opening.
   The current native `open_dataset_tag` takes only display text; validating an
   envelope then calling that unscoped opener is insufficient. The hosted receiver
   now consumes its original Dataset queue directly and revalidates the prepared
   in-process scope immediately before stock `show_tag`/review consumption. A future
   standalone adapter must bind independently validated endpoint scope to that
   original recipient authority; the hosted snapshot is not a portable peer proof. Native recipient must
   independently revalidate the same admitted scope at actual consumption.
3. Admit only search v1 on the initial endpoint. Apply strict bounded envelope framing
   and decode, matching payload/envelope IDs and qualified declaration versions. No
   rename dispatch, image reads/transfers or settings RPCs are activated by this code.
4. Review actual selected-peer/live-instance proof and establish it **before sending
   any Tag descriptor**, not merely at recipient consumption. No credentials/pairing
   are created by these contracts. Preserve explicit local/connection choices and
   no data transmission on inventory/health refresh.
5. Integrate request-scoped final receipts, bounded duplicates/conflicts, expiry,
   cancellation, source generation changes and ambiguous-loss lookup into sender and
   receiver. Exact same request retry is lookup-only after ambiguity. Prepare fresh
   recipient admission at deferred consumption and record actual route outcome.
6. Prove the stock local transport with explicit synthetic scope, response and cleanup
   before adding adapters. Manager is optional; direct standalone app use must work
   with Manager absent/offline. Review exact runtime/package closure and visible
   entry/result/recovery states separately. No launch from declaration endpoint paths.

Shared settings owns configuration and permission; runtime owns current health and
instance observations. A successful settings save does not create peer proof or
renew TTL; runtime failure does not overwrite saved choices. Manager can publish
verified detached inventory with original evidence/timestamps when connected, but
cannot restamp stale observations or become mandatory settings/runtime authority.

## Reuse and verification owners

Existing identity/resolution: `suite_core/installation.py`; dated projections:
`suite_core/inventory_view.py`, `suite_core/registry/status_view.py`; version evidence:
`core/connected_version_observation.py`; native search/review:
`core/organizer_encyclopedia_handoffs.py`, original admission/receiver owners.
The stock two-consumer example admits only its exact new `runtime_contract.py`
manifest member in addition to its established finite source allowlist.

Focused checks: `tests/test_app_capabilities.py`, `tests/test_app_availability.py`,
`tests/test_app_handoff_contract.py`, existing
`suite_core/tests/test_organizer_encyclopedia_handoffs.py` and
`suite_core/tests/test_installation_registry.py`. No automatic full-suite, UI,
packaging or native gate follows from these inert core declarations.

## Existing hosted terminal-feedback source integration

`core.organizer_handoff_scope.OrganizerHostedScopeReader` observes the already loaded
original profile token, exact ordered included library IDs, privacy role/share/review
policy, configured root tokens, original manager references and the original
`HostedLibraryBinding` epoch. It creates no event subscriber, registry, index or
filesystem reader. `HostedRecipientScope` retains local borrowed owner references;
no paths or scope contents enter receipts. Local Only is not exported or granted
network/hosted-provider permissions; the consumed action is original native local UI.

The current `OrganizerEncyclopediaHandoffReceiver` adds:

- `prepare_recipient_scope()` on the original GUI thread before sender work.
- `receive_tracked_handoff(value, *, prepared_scope, source_generation,
  current_generation, on_settled, lifetime=30.0)` for existing in-process search or
  correction review. `receive_tracked_search` narrows this to existing search v1 only.
  These are not endpoint receivers and do not permit cross-process rename.
- Exact prepared scope plus current native catalog revalidation at original queued
  consumption, and a final scope check immediately before the stock Dataset method.
  The tracked path avoids a second unscoped navigation/queue lambda. Stock search,
  original correction form and its accepted matching scope remain unchanged.
- Immutable `HostedHandoffReceipt(request_id, source_generation, stage, reason)`;
  final observer plus `receipt(request_id, source_generation)` lookup. Queued is
  separate from delivered/held/cancelled/expired/unknown. Identical duplicates are
  lookup-only; altered content/generation conflicts hold. Callback errors retain
  actual outcome and only a content-free failure count.
- `cancel_tracked`, `sender_unavailable`, `poll_settlements` retain original request
  identity. Deadline/source-generation checks stop late callbacks. Reentrant Cancel
  during native opening and operation exceptions produce unknown outcome, never
  falsely claim no opening or automatically resend. Clock rollback/nonfinite values
  hold. Tracking is capped at 512 receiver-lifetime records/tombstones; session
  capacity has explicit reopen guidance. Terminal records release prepared scope,
  generation reader and observer references.

The existing Encyclopedia sender prepares scope before worker submission, identifies
each callback by request ID and baseline generation, polls queued settlement using its
original GUI timer, and cancels pending delivery through existing Cancel/Close paths.
Its original command bar `refresh_state` disables handoff actions immediately while
queued; Cancel remains visible. Old-generation results cannot overwrite a new
catalog/Close status. Sender now reports actual opened search/review, late held cause,
explicit cancellation, expiry or unknown outcome. No image Apply is automatic.

Pre-existing shutdown settlement wrappers are unchanged. This lane did not read or
apply a prior denied proposal or alter native process/Close ownership. All changes
derive from preserved current source and the separately authorized hosted-feedback
scope. Frozen a.0.4.3 and its code/assets/metadata are untouched.

Tests: `suite_core/tests/test_hosted_handoff_settlement.py`, existing receiver tests,
`suite_core/tests/test_encyclopedia_organizer_sender.py`, and
`suite_core/tests/test_hosted_sender_settlement.py`. Reader commands/status/Cancel are
real Qt controls; recipient Dataset endpoints/scope in those Qt tests are doubles.
Original native queue/review method bodies have independent synthetic component
coverage. Light/dark 1080p source captures do not qualify actual native Dataset
results, image Apply, packaged startup or endpoint authentication. Source manifests
and package closure enrollment remain with the primary integration owner.
