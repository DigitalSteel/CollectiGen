# Exact a.0.4.5 source delivery and private library replacement

This supplement resolves the older pending source-delivery note without changing the immutable software ZIP. Supply it with the matching source/notices companion when distribution is separately authorized.

Software ZIP SHA-256: `82c8e71ff1802da686ba376b25f4ac5c22cc5d9e69456f466af412bdfc648a40`. Matching source/notices SHA-256: `cff10bf497b539053f225ad4a3831799c3372e0e5047f96877fee35a96c66570`. The companion includes exact application source, generator/recipe/mappings, checker source/support notices, LGPL/GPL texts and official Qt6.11.2/PySide6.11.2 source archives. GENERATOR_RECIPE.md explains sanitized receipts and exact-byte reproduction limits. Older a043/a044 companions do not substitute for this pair.

Qt/PySide are dynamically loaded. Users may build compatible modified libraries from the supplied sources, following their included build instructions and matching Windows x86-64, Python3.13, Qt/PySide/shiboken6.11.2 ABI/dependencies. Application source is available; own modifications and debugging are not prohibited. Library notices/licenses remain applicable. See [Qt's stated LGPL obligations](https://www.qt.io/development/open-source-lgpl-obligations) and [LGPLv3](https://www.gnu.org/licenses/lgpl-3.0.en.html).

For a private replacement, first close the app and retain a complete software/profile backup. Work in a separate copy. Place compatible Qt/PySide libraries under Runtime/python/Lib/site-packages/PySide6, preserving the layout. Runtime/python/collectigen-runtime.json records critical-file SHA-256 values: refresh only entries for intentionally replaced files in your private copy, keeping the expected capability/version/lock contract intact. For example, the QtCore.pyd binding is critical; Qt6Core.dll is a dynamically loaded dependency and has no separate critical-file row in this candidate. Do not invent an extra row/policy or disable validation. The whole official package inventory/signature no longer describes a modified copy; do not claim official distribution or Manager admission for it.

Run the private copy's bundled interpreter with `-B -m suite_core.media_organizer --runtime-profile organizer-image --runtime-root <private-copy>/Runtime --validate-runtime`. This supported command exits before application/profile startup. Then import QtCore and check its version and loaded library location; use a separate profile if proceeding to your own application run. A failure means retain diagnostics and fix compatibility; it does not authorize weakening Windows policy, deleting profiles or blindly repairing over your modified libraries.

Technical witness qt-replacement/RESULT.json proves a byte-distinct ABI-equivalent binding/DLL pair can load from a private copy with normal validation enabled and only its applicable integrity hash refreshed. Inert PE overlays left executable sections/ABI unchanged; this is a replacement-mechanism witness, not a rebuilt-Qt-source or arbitrary-version compatibility guarantee. The exact sources/build instructions provide the route for substantive compatible modifications. No signing key, publisher permission or Windows configuration change was needed for this private witness. No native window was created.

This document records technical source-delivery/replacement evidence; it is not a blanket legal opinion, clean-machine test or publication approval. Official candidate bytes and user data remain untouched.

## Current Organizer LM Studio source integration

AI-05H carries the reviewed discovery/preferences services and canonical late-
publication/preference-preservation protections into the current0.5.8 source.
Original newer Organizer files are preserved outside finite Setup/settings
changes. This source branch is not the published0.5.8 build;current-host
packaged qualification and signed delivery remain open. No inference,credential
storage,model loading or default server discovery is enabled.

## Media Organizer a.0.6.0 source candidate

The current Organizer source integrates explicit LM Studio model-list review and
connection preferences. The declared0.6.0identity selects a new feature release;
this source checkpoint alone is not a qualified binary or publication. Existing
server/model ownership and image-analysis limits remain.


## AI-05J1 pure native image-description codec

The source-only codec constructs bounded native v1 image-description request bytes
and decodes unreviewed text with exact loaded-instance correlation. It does not
connect,read files,decode pixels,activate a provider,run a model or write evidence.
Requests ask for no conversation storage or streaming and include no tools,
integrations,load/context settings or TTL. MIME is caller-supplied;it is not image
validation. Model load time is retrospective observation,not execution authority.

This module is not wired into the current Organizer runtime or the shipped0.6.0
package. Future consumer and signed package delivery retain the existing Organizer
route. Actual server/JIT policy,model inference,review and resource qualification
remain open. No component/version/binary or model management is added here.


## AI-05K1 fixed native description POST source

The new source-only client builds the J1 request internally and uses one fresh
stock loopback connection and one fixed native POST. It never retries or follows
redirects. Failures after submission begins retain outcome_unknown even after
client cancellation or cleanup failure. Timers,watchers and owned handles settle
before success;client closure does not establish server generation stopped.

Only injected and generated ephemeral-loopback tests qualify this source child.
No real LM Studio,model,provider,profile or image-library operation is performed.
Existing GET and codec source remain unchanged. Actual server/JIT policy,review
consumer and signed Organizer runtime delivery remain open;no version is added.


## AI-05L1 read-only preparation and description backend source

The source adapter reads only the explicit image through the canonical stable
reader,prepares an oriented metadata-free RGB PNG in memory,verifies returned
request/instance correlation and rereads the source before returning the existing
unreviewed InterrogationResult. Original files and image families are not written.
Metadata encountered during decode is not copied into the payload or audit.

Dispatch must be injected;no default server,credentials,provider selection,local
model lifecycle,evidence persistence or UI activation is added. The existing
source reader/decoder,J1/K1 and evidence conversion classes remain unchanged.
This follows existing RGB/white-alpha conventions,not color-managed fidelity.
Actual server/JIT/model,visible/persisted review and signed Organizer delivery
remain open. No package or version is created for this source-only child.


## AI-05M1 original service and persisted review component qualification

The focused source witness binds a fresh generated profile before storage imports
and uses the original analysis service,record/evidence writers,review decision
and reload paths with the real injected LM Studio backend. Cases cover unreviewed
persistence,explicit acceptance,cache reuse,no automatic retry,cancellation and
late source/sidecar/review stale guards. No product service is replaced or activated.

This is generated component evidence,not visible UI or actual-server/model
qualification. Generic Loading/partial-cancel uncertainty remain integration
requirements. No existing profile,real library,Root/Reader/C3A trial,package or
version is introduced. Signed Organizer runtime delivery remains open.


## AI-05N external progress and cancellation source integration

External analysis now reports preparation, request waiting and source checking
through the existing progress callback without claiming a local model load.
Partial cancellation and failed requests retain validated immutable per-image
submission uncertainty and cleanup status. Default cancellation still raises the
original typed exception; local backend Loading/cancellation behavior is retained.

Generated component checks cover original persistence/review and local compatibility.
This source-only change activates no provider, model, server or Qt workflow. Visible
uncertainty/recovery, opt-in routing and actual-server qualification remain open.
Organizer 0.6.0 does not contain this change; signed runtime delivery remains open.


## AI-05O unified preparation request disposition

The original preparation description projection now retains the immutable
per-image external request outcome. Existing result detail and serialization
separate not-sent from unknown server outcome and retain client cleanup failure.
Local outcomes omit the new optional serialized field, preserving their shape.
Generated original-service checks cover cancellation, errors, explicit retry,
persistence and local compatibility. No provider, server or Qt route is activated;
source result text is not visible workflow qualification. Organizer 0.6.0 remains
discovery/preferences only; signed runtime delivery remains open.


## AI-05P1 manual external description source consumer

Run AI retains Local AI as its default and offers an explicit per-run LM Studio
choice. Connection preferences remain independently saved; model inspection is an
explicit metadata refresh and exact loaded vision instance choice. Local drafts,
factory settings and automatic analysis stay separate. The normal live dispatch
is absent: this build cannot execute LM Studio image analysis until the separate
exact-server execution qualification is bound by its owner.

The injected source consumer reuses the original job/history/preparation/evidence
and review path, with description-only controls, explicit retry and client cleanup.
It reports unknown server outcomes honestly and never loads/unloads a local model.
Generated source UI evidence does not establish actual model or packaged support.
Organizer 0.6.0 remains preferences/discovery only; no new package/version here.
