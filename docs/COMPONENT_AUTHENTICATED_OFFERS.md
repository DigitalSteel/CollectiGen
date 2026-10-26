# Metadata-only authenticated maturity offers

The existing ComponentAcquisition supports explicit `schema2_metadata=True`,
`observe_installed` and `expected_repository_id` constructor arguments. Default
legacy mode is unchanged. New-mode partial configuration, noncanonical metadata
names, mismatched repository expectations or wrong types hold before the existing
owner marker is created. A source-mode flag does not activate any app/profile.

InstalledOfferContext is a frozen adapter containing component_id, version,
schema_version, maturity, observation_id and pending=False. First install uses
explicit None version/schema/stage. Schema1 retains its exact legacy version and
None stage; schema2 requires canonical numeric version and explicit maturity.
The opaque nonempty observation token is at most 1024 UTF-8 bytes without NUL.
It comes from the original host observer; this DTO creates no lease or ownership
proof. Pending, missing, inconsistent or changed observations hold.

Opted-in discover requires one typed component scope and matching installed_versions.
It pins the actual repository response, reuses bounded B3A candidates, fetches
only the highest numeric group's metadata and authenticates each candidate's
original bytes before comparing signed stages. It binds exact asset IDs/sizes,
component/platform/channel/schema/version/maturity and public trust. Invalid
highest groups hold without fallback. Same-base mixed schemas, equal/downgraded
identity, unsupported floors and schema2-to-schema1 regression are refused.
This is metadata review only; no artifact integrity/native claim is inferred.

Each opted-in discovery starts a serial in-memory epoch with no published offer.
Only complete success commits the new view. Cancellation/failure leaves no
partial/prior-epoch result. Offers bind the full installed context and epoch.
Legacy behavior is retained. These additions do not make the service thread-safe.

ExactOfferAsset/ExactComponentOffer retain their old defaults and append explicit
schema/stage pins. from_reviewed_offer copies verified identity, never unsigned
locator hints. Revalidation always reruns the shared authenticated selection in
opted-in mode, including when no exact expectation was provided. It requires the
same raw metadata, selected locator, verified result, context, trust and live
offer/epoch. Stale successful-offer revalidation clears its view; replayed objects
are refused. No automatic retry is performed.

ReleaseTransportPolicy's default-empty schema2_metadata_assets accepts only the
canonical tuple (collectigen-release.json, collectigen-release.json.sig) when
explicitly configured. It uses the existing callable HTTPS opener, canonical
schema2 URLs and reviewed redirect/hop rules. Package URLs and wildcards remain
refused. The main application updater is unchanged and supplies only its existing
bounded metadata primitive. No new network or credential enrollment is implied.

ComponentAcquisition.acquire unconditionally refuses verified schema2 offers
before refresh, acquired-cache lookup, artifact requests or attempt/cache writes,
including purported existing cached artifacts. No flag or false install-authority
property replaces that executable guard. B3I/original-root work must qualify
installation identity before any later change to this hold.

Qualification uses existing contained fake HTTP responses, inert artifacts and
disposable fixture signatures. No real network, key, signing, profile, installer,
native execution or user catalog is involved. Source rollback is a scoped code
revert to the accepted parent/preimages, never Data restoration. Existing licensing
applies; no dependency or runnable app version is added.
