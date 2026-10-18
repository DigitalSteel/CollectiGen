# Reader a.0.3.1 source and build proposal

This exact source includes the Manager-owned ordinary-entry correction to a.0.3.0.
It retains the stock finite build, source allowlist, public trust and isolated
toolchain. NORMAL_PROFILE.json is public build input, not a signing key.
GENERATED_SOURCE_PINS.json records the exact generated entry/profile bytes.
The source remains inert until compiled through the stock prepared recipes.

Use the stock tools.prepare_encyclopedia_qualification_stage command with this
export's public-source.json, finite source-manifest.json, MODULE_MANIFEST.json,
TOOLCHAIN.json and NORMAL_PROFILE.json SHA-256 values, --compiled-sources-only,
and a new sibling output directory. Compile its TagEncyclopediaReader.spec and
TagEncyclopediaRoot.spec using the exact admitted Python 3.14.7 toolchain.
Retain analysis closure and actual CArchive/PYZ source readback checks.

No signing, publication, native acceptance or byte-identical rebuilding is
implied. The admitted Reader test subset retains its documented scaffolding
and Organizer limitations; no complete passing public Reader suite is claimed.
Private operator and qualification inputs remain excluded.
