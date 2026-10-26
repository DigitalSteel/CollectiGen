# Suite frozen-payload packaging

The default Tag Database data candidate uses the existing KnowledgePack container,
not this runnable-payload builder. Its current synthetic-only qualification route is:

```powershell
.\.venv\Scripts\python.exe tools/build_tag_database_pack.py --manifest SYNTHETIC/manifest.json --input-root SYNTHETIC --output-root ISOLATED_DIST --synthetic
```

The input manifest is the explicit schema-10 Foundation manifest documented in
`docs/KNOWLEDGE_PACKS.md`; only its named default files are read. The output is
`ISOLATED_DIST/TagDatabase/<display-version>/data/TagDatabase.zip` plus a local
`BUILD_RECEIPT.json` marked `synthetic_qualification_only` with no publication
clearance. Bad input is preflighted before creating the output tree. An existing
candidate destination is held. The CLI does not approve public classification,
allocate a released version, rename legacy content revision, sign, install or
publish. Reviewed real projection integration remains with its classifier owner.

Organizer closure preparation is a separate source-only step:
`python -B tools/organizer_freeze_inputs.py --assessment RETAINED_CLOSURE.json --output FRESH_DIRECTORY`.
It validates the retained source paths, pins and read-backs current copied bytes,
and retains changed rows and unresolved import classifications. It does not scan
user data, supply a catalog, freeze or certify a runnable payload. The stock
freezer still needs admitted Pillow/runtime assets and an actual graph check.
Optional model/video libraries are not prerequisites for local organization.
The explicit closure additions include package initializers and the release
presentation formatter needed by the current component version modules. Missing
ModuleGraph identifiers can include literal quote characters; normalize these
before classifying local missing modules, and verify lazy exports against their
actual compiled owner rather than ignoring an entire namespace.

For synthetic native startup, bind the existing gated user/cache paths to an
owned `ROOT/data/user` and `ROOT/data/cache` layout before importing core. Shared
startup uses `core/startup_profile.py::startup_write_root` for recovery, logs,
migrations and update markers; ambiguous or overlapping layouts fail closed.
Normal startup retains its existing deployment root and settings identity.
This is qualification isolation, not a new production profile-selection route.
Frozen Organizer startup uses the stock importer directly; only source startup
temporarily owns a source-path bridge. A disposable offscreen qualification
executable has exercised verified settings-history recovery, actual shared
startup/AppState, visible Dataset entry and accepted close with an unchanged
payload. This does not qualify ordinary deployment launch, Dataset package
recovery or clean-machine distribution; the source manifest flags remain false.

`build_component_package.py` adds the packaging step missing from Windows
alpha4: a root `BOOTSTRAP_INVENTORY.json` and canonical unsigned release-envelope
input. This tool does not freeze source, build a runtime, sign/enroll keys, launch
apps, install, download or publish. Main-app `Build-PortablePackage.ps1` is a
different embedded-runtime path and remains untouched. No persistent Suite
freezer/alpha4 builder was found in the inspected repository tooling; wire this
explicit post-freezer command into its owner pipeline at the next authorized build.

Input is an explicit completed, fresh frozen-payload directory, containing the
full wrapped application directory and its actual RUNTIME_MANIFEST.json and
MODULE_MANIFEST.json. The runtime manifest must match every actual payload file,
size/hash and component/software/platform, excluding only itself. Mutable data,
models, catalogs, saves, logs, backups, Git, venv and bytecode-cache directories
are rejected at every depth. An existing bootstrap inventory is rejected rather
than overwritten or quietly excluded. Nothing is added to the input tree.

The tool presently accepts Windows x86_64 Manager, Tag Encyclopedia and Media
Organizer component identities. Each still requires an explicit completed frozen
payload and matching runtime closure; Organizer's current source-only inventory
is not such a payload. Linux/aarch64 need separate packaging qualification. Short runtime labels are
checked against the canonical module IDs. The expected software version is
explicit and cannot relabel an older payload. Packaging version remains in the
legacy runtime manifest and is not a substitute for newer software admission.
Declared platform checks do not prove binary/native compatibility.

Output must be a fresh, separate directory with an existing parent. It contains
a deterministic ZIP (sorted file names, fixed timestamps/permissions/compression),
the exact root bootstrap inventory inside the ZIP plus a matching sidecar,
`release-manifest.input.json`, and `packaging-receipt.json`. The envelope binds
exact archive basename/size/hash, internal-inventory hash, canonical component,
software version, platform, channel and supported_from. Dates and release notes
are explicit inputs; they are never invented from local time or app source.
The envelope is unsigned input for a separately approved signing step, not a
production release manifest. Receipts always deny installation/native-health
authority. No keys or trust are read by the tool.

No output is written until input identity/closure/pins and a stale-input guard
pass. Output readback verifies complete ZIP member bytes. A post-write failure
retains its fresh output with INCOMPLETE.json and never deletes input or accepted
artifacts; inspect that output before separately authorized cleanup. Do not use
incomplete outputs. A complete receipt has no incomplete marker. Reuse requires
a new output directory. These are cooperative staging guards, not hostile-race,
handle-based or crash/power-loss durability qualification.

Input/output paths are resolved before separation validation, retaining the
link/reparse guards; dot-dot aliases cannot place generated files inside input.
Release notes must be nonblank and their raw length must be at most 100000
characters, matching the canonical release contract even for padded text.

## Future command (not executed)

After the packaging owner has separately authorized and produced a fresh,
inventory-complete frozen alpha9 Manager payload at the exact input below and
provided its reviewed notes, this command emits a new packaging fixture. It does
not consume, extract or repack the accepted alpha4 ZIP. The input and notes do
not currently exist; do not create them by copying a runtime wholesale.

```powershell
& D:\CollectiGen-Dev\.venv\Scripts\python.exe -B D:\CollectiGen-Dev\tools\build_component_package.py `
  --payload-root D:\CollectiGen-Dev\.tmp\future-manager-alpha9-frozen-payload `
  --output-root D:\CollectiGen-Dev\.tmp\future-manager-alpha9-inventory-fixture `
  --runtime-manifest CollectiGenManager/RUNTIME_MANIFEST.json `
  --module-manifest CollectiGenManager/_internal/MODULE_MANIFEST.json `
  --component-id collectigen.suite-manager --software-version 0.1.0-alpha.9 `
  --os-name windows --architecture x86_64 `
  --artifact-name collectigen.suite-manager-0.1.0-alpha.9-inventory-fixture-windows-x86_64.zip `
  --supported-from 0.1.0-alpha.9 --published-at 2026-10-06T00:00:00Z `
  --release-notes-file D:\CollectiGen-Dev\.tmp\future-manager-alpha9-fixture-notes.txt
```

Replace publication date with the explicit approved envelope date when actually
running; its shown fixture value is not a publication claim. For another payload,
use its actual module-manifest path, canonical component/version and reviewed
notes. Do not substitute source-current Manager alpha17 for a built runtime.
A future same-component target must have a newer software version, distinct
archive bytes and supported_from admitting the prior; changing only packaging
version cannot satisfy qualify_pair. The synthetic tests already exercise two
inert versions through existing disposable RSA signing and canonical qualify_pair.
The packaging tool itself never signs.

## Verification and remaining gates

Use project Python with plugin autoload/cache disabled and the explicit canonical
test-helper path:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
& .\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider `
  -o pythonpath=D:/CollectiGen-Dev/suite_core/tests `
  --confcutdir D:/CollectiGen-Dev/suite_core/tests `
  suite_core/tests/test_component_package_builder.py
```

Product fit: reuse the canonical offline producer, release crypto/admission and
synthetic-root qualification to make a future offline update package measurable
while preserving component identity, source evidence and operator trust decisions.
This isolated source change closes no roadmap parent or release gate. Actual
freezer/export/allowlist/license closure, actual two-version artifacts, signature
custody/production trust, authenticated installed origin, native preflight/host
and GUI/normal-close qualification, hostile filesystem/races, crash durability,
installed activation, data-schema/backup/old-reader rollback and Linux/Bazzite
remain separate owner gates. Windows/Manager remains HELD. There is no new
user-facing distribution or What's New claim.

## Linux format dependency: output disabled

Accepted component staging (`suite_core/discovery/component_staging.py`,
stage_and_activate) and package-pair qualification
(`suite_core/discovery/offline_component_producer.py`, _package) both open archives
with zipfile.ZipFile and canonical ZIP-member checks. Neither dispatches tar.gz
or applies recorded executable modes after extraction. The bootstrap inventory's
exact file-row schema is path/size_bytes/sha256; it has no POSIX mode field.
Component signature platform admission accepting linux does not provide tar
extraction or Linux permission/loader qualification. Linux-declared ZIP fixture
tests establish byte/platform binding only, not portable Linux parity.

This builder therefore rejects Linux x86_64, Linux aarch64 and tar.gz output
before creating an output directory, including otherwise matching Linux payload
metadata. It also rejects Windows tar.gz. No Linux archive, deterministic tar
producer or synthetic claim of updater compatibility is introduced here.

Before Linux packaging can be enabled, the updater/staging owner must separately
review and implement:

1. Authenticated archive-format dispatch and bounded tar.gz member admission:
   canonical paths, duplicates/case aliases, decompressed/member counts and bytes,
   and rejection of symlinks, hardlinks, special nodes and unreviewed metadata.
2. An explicit authenticated POSIX permission policy, including how intended
   executable bits are bound and verified under versioned inventory/reader
   compatibility. Current ZIP type checks and byte hashes do not supply it.
3. Matching qualification/activation/recovery behavior for that format and mode
   policy before a packaging producer emits it. Producer metadata would then need
   deterministic sorted members, gzip time, tar timestamps/owner/group metadata,
   reviewed file modes and link/path policy, exercised with inert fixtures first.

No runtime validator, staging source or accepted archive is edited by this task.
Native Linux host/loader/GUI and Bazzite qualification remain subsequent gates.
The regression suite explicitly exercises the disabled paired-platform boundary;
Windows ZIP is the only produced format.


## Default-free component source checkpoint

The existing `tools/Build-PublicSource.py` accepts an explicit source manifest
with `export_scope: component-source`, `component_ids` naming Manager and/or
Encyclopedia, their module manifests, and empty reviewed-default/provenance lists.
It rejects catalog/default content paths; the sole permitted data input is the
existing strictly public release-trust JSON. This metadata does not activate trust.
The generated empty public-defaults audit file is not a Tag Database or snapshot.

This route exports only explicitly eligible component code and support files.
Its stock privacy scan and exact inventory remain mandatory; ordinary full-app
exports retain the native reviewed-default completeness gate. Keep the component
source manifest with its byte pins and disclose differences from frozen binaries.
A source checkpoint does not authenticate, install or publish a package. Checks:
`tests/test_public_component_source.py` and the existing exporter gate tests.


## Encyclopedia update-data declaration preflight

For a future update-capable Encyclopedia payload, use the existing producer with
`--require-update-data-contract`. It requires the existing updater's exact
`update_data_contract` schema in the actual inventoried
`TagEncyclopedia/MODULE_MANIFEST.json`, checks its snapshot sets against that
module's supported/max interface declarations, and holds before output on missing,
malformed or contradictory declarations. No module bytes are synthesized or
rewritten. A declared contract is also checked without the flag; older undeclared
packages remain packageable and retain no update compatibility claim.

The producer reuses `core/encyclopedia_update_binding.py::_data_contract` as its
sole update-data schema parser. That updater-owned module must be available in the
coherent source checkpoint. The current contract has exactly `schema: 1`,
`database_schema_versions` and `snapshot_interface_versions`; no new settings or
migration field is introduced. The module's actual bytes bind through the existing
inventory hash, archive hash and unsigned release-envelope input. Signing remains
separate. `update_data_contract_schema_validated` in the unsigned packaging receipt
reports only this preflight; installation/native health remain false.

Current reader source supports snapshot interfaces 1 and 2. Its persistent stores
have independent Tag/Category/lifecycle/relation identifiers, and current settings
preserve unknown fields through the existing settings transaction. These facts do
not demonstrate a single production database compatibility set or a settings
migration promise. Synthetic updater fixtures using database version 10 are not
production evidence. The source module remains undeclared until its owner supplies
a reviewed compatibility meaning and acceptance. Do not infer versions from the
software version or manufacture declarations to unblock a package.

## Organizer interpreter assembly

`assemble_organizer_interpreter` in `tools/build_component_package.py` prepares
fresh versioned candidate bytes from an explicit stock image runtime root and
the existing public source preparation receipt. Its required caller-selected
`runtime_kind="interpreter-portable"` consumes the shared
`select_payload_policy(ComponentId(...), "windows", "x86_64", runtime_kind)`;
it does not define or enlarge its own ceilings. The existing `build_package`
and CLI accept `runtime_kind`/`--runtime-kind`, defaulting to `frozen-portable`.
Release dates/notes, signing and installation remain separate authorities.

Assembly requires exact expected runtime-manifest and source-receipt hashes,
explicit software version, a fresh output directory named for that version,
and explicit asset tuples `(source_path, package_relative_path, expected_sha256)`.
The source preparer also accepts the reviewed actual-collection `rows` receipt;
missing graph classifications remain unknown. Current public registration still
governs every source path. Explicit startup leaves include runtime_startup.py
and component_payload_policy.py; their registration is coordinator-owned.

The immutable tree puts source at package root beside `Runtime/python` and
`Runtime/exiftool`. It keeps headers, plugins, codecs, licenses and notices.
Only cache files with an existing corresponding source are omitted; orphan
bytecode is retained. Original builder output and wheel RECORDs are untouched.
The final pinned python313._pth is:

```text
python313.zip
.
Lib\site-packages
..\..
import site
```

No checkout, environment or user-site path is admitted. Runtime manifest records
the explicit cache projection and final complete file hashes. Candidate receipt
denies Manager admission and native health. Existing paths are never overwritten;
failed output is retained with INCOMPLETE.json. Raw data/defaults and retained
public-seed-inputs cannot be assets. Only approved public projections may be
supplied by their resource owner; labels or historical assessments do not approve
raw seeds. Mutable profiles, optional components and models stay outside.

Installed same-version/damaged-package repair is unsupported. Do not run the
builder or repair script against pinned Runtime; replacement uses the existing
separately admitted newer-version component transaction. This preparation tool
does not select, launch, activate, download, sign or publish a package.
## Versioned fourteen-default projection evidence

`build_tag_database_pack --public-projection` is a separate fresh-install-only
candidate route. Legacy synthetic and seventeen-document reviewed-local routes
retain their contracts. Supply independent `--projection-authority` and its
`--projection-authority-sha256`, explicit private original-defaults/evidence
inputs, pinned source publication and exact six-check local qualification.
Authority pins are separate from the evidence's own claims; inventory, policy,
disposition replay, retained annotation subtrees and native wire/source binding
must all match before an output directory is created.

Original pre-projection documents, dispositions and internal reviews are private
validation inputs and are never copied into the candidate. The public projection
summary has an explicit hash/count/fingerprint allowlist. Typed v2 classification
recursively examines policy selectors/conditions, pattern regex/examples/extras
and appearance maps/rules without executing expressions or loading stores.
Unknown or private values remain in memory and hold export. Eleven-family v1
classification stays unchanged; the native Pattern record category distinction
does not exempt nested operator/provenance fields.

The three approved legacy annotations retain exact subtree fingerprints and
pending interpretation status. This route clears no rights, signature,
publication or existing-catalog upgrade gate; the retained 277 definitions and
608 collision keys remain a separate compatibility task. Bound native recovery
and no-op evidence may be reused while payloads and native behavior are unchanged.


### Explicit Organizer shipping-input admission

`assemble_organizer_interpreter(..., shipping_descriptor=descriptor)` optionally
adds shipping-input preflight to the same stock tuple/guard/readback path.
Qualification calls omit it and retain historical acceptance. The descriptor is
a reviewed Python/JSON object, not a runtime policy or release authorization.
Preflight runs before any assembly output creation. Only the descriptor hash is
recorded in the assembly receipt; absolute source paths are not copied into the
runtime/bootstrap inventories by this gate.

Exact schema (no additional fields): `schema_version: 1`,
`component_id: "collectigen.media-organizer"`, `software_version` matching the
assembly, `runtime_kind: "interpreter-portable"`,
`runtime_profile: "organizer-image"`, `entrypoint: "Start_MediaOrganizer.bat"`,
and `assets`, a list of rows with exactly `source` (path string), `destination`
(package-relative canonical path), `size_bytes` (nonnegative integer) and
`sha256` (lowercase 64-hex digest). The rows bind the **complete** `asset_inputs`
list, including unchanged resources/notices and individually approved document
link targets. Duplicate/case-alias/escaping destinations, missing mandatory
inputs, mismatched bytes/sizes/pins, and unapproved extra tuples refuse.

Mandatory destinations are `Start_MediaOrganizer.bat`,
`tools/Test-WindowsPrerequisites.ps1`, `README.md`, `TESTING.md`,
`RELEASE_NOTES.md`, `docs/USER_GUIDE.md`, `WINDOWS_PREREQUISITES.md`,
`SUPPORT.md`, `PRIVACY.md`, `SECURITY.md`, `LICENSE`, `LICENSES.md`,
`THIRD_PARTY_NOTICES.md`, and
`suite_core/media_organizer/MODULE_MANIFEST.json`. The module must declare matching
`module_id`, `software_version`, integer `interface_version: 1`, exact
`entrypoint: "Start_MediaOrganizer.bat"`, `runtime_kind: "interpreter-portable"`
and `runtime_profile: "organizer-image"`. Optional expert profile arguments belong
to the launcher grammar/direct module documentation, not the metadata filename.

The primary reviews and pins actual launcher/document bytes, resolved local link
closure, release-note consistency and source/notices correspondence. This
preflight does not execute or semantically certify a BAT/PowerShell script,
follow links, grant redistribution rights or prove native shipping readiness.
Launcher argument/prerequisite/error-retention tests stay with that input owner;
actual shipping-entry qualification follows one authorized fresh build. Receipt
fields `shipping_inputs_admitted` and `shipping_descriptor_sha256` describe only
this finite check. Existing installation/native-health/Manager authority remains
false, and current frozen qualification artifacts are never rewritten.


Common Organizer destination admission also rejects case-insensitive file/parent
conflicts across the complete runtime, prepared-source and asset closure before
creating output. A file `README.md` cannot coexist with
`readme.md/extra.txt`; either admission order refuses. This uses the existing
canonical path guard plus tracked file/parent names and applies to qualification
as well as descriptor-based shipping inputs. Valid historical closures remain
accepted; no output is created for an impossible Windows file/directory layout.
