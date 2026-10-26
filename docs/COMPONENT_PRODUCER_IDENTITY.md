# Explicit schema2 component preparation

`tools/build_component_package.py::build_package` and its CLI accept an optional
`identity_inputs` / `--identity-inputs` path. Without it, legacy schema1 behavior
is unchanged. With it, malformed or incomplete identity input refuses before
output creation; it never falls back to legacy. Outputs are unsigned static
preparation artifacts, not installed, launched or native-qualified applications.

The bounded JSON document has exactly schema_version=1,
purpose=COMPONENT_SCHEMA2_PRODUCER_INPUTS, component_id, version, maturity,
source_version, source_module, source_inventory, release_history, release_notes,
and baseline. Each source role is an explicit absolute path/sha256 object.
Baseline has manifest/signature/trust objects in the same form. Source inventory
is a pinned stock component-source public-source.json; version/module/history/
notes must lie in that cohort and match its size/hash entries. The staged module
must equal the selected source module bytes. Unsafe, redirected, multiply linked,
aliased, mutable-data and oversized paths refuse. No cohort tree is scanned.

Source version.py is parsed as literal assignments without importing/executing
it. Required unique string declarations are SOFTWARE_VERSION, NUMERIC_VERSION,
MATURITY, RELEASE_DATE and RELEASE_TIMEZONE. Computed statements and dynamic
targets refuse. Version/stage agree with the selected numeric schema2 identity,
module fields and runtime feature_version/maturity. Module date/timezone and one
matching release-history row must agree with the source. Existing runtime and
inventory schemas remain unchanged; missing explicit runtime maturity holds.

Published time requires an offset and must map to the declared date. UTC uses
Python's built-in UTC; other IANA names use the stock zoneinfo resolver and hold
when its database is unavailable. No timezone data is installed or downloaded.
The tested project environment lacks that database; no America/New_York release
preparation is qualified by the UTC synthetic tests.

Canonical notes are strict UTF-8 without BOM. Their first nonempty line is
`# <display-version> - <YYYY-MM-DD>` with no second level-one heading. The exact
decoded text, including CRLF, must equal the existing release_notes argument.
Notes do not derive from history scope or unsigned display labels.

The selected delivered baseline must pass the existing strict component signature
verifier, public trust and target platform/component binding. B1's numeric/stage
upgrade gate refuses equal/downgraded or unsupported identities and same-base
legacy transitions. Input pins are rechecked before output and completion.
Failures after output creation retain INCOMPLETE.json and generated evidence.
Receipts add logical input-role hashes and identity without local source paths;
signed/native-health/install flags remain false.

A local input document is not publication authority or proof that a baseline is
the latest delivered release. The release owner must establish baseline custody
and history completeness separately. Signing, version selection, actual artifact
health, feed/exact-offer/profile/installed-stage integration and rollback safety
remain separate gates. Tests use generated inert payloads and disposable fixture
signatures only. Recovery of this source patch is code-only; never restore catalog
or Data bytes. Repository licensing applies; no new dependency is bundled.
