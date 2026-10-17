# Reader source and compilation correspondence

This source archive contains the admitted product, build and license sources
for the a.0.3.0 Reader. Its Python application sources correspond to the retained
executable. Documentation resources have been reconciled separately. This is
source correspondence, not a claim of byte-identical executable rebuilding or
complete release qualification.

`TOOLCHAIN.json` records the qualified Windows x64 Python 3.14.7 build environment,
dependencies and stock build-input pins. `NORMAL_PROFILE.json` contains only the
portable layout, public HTTPS transport and release-verification public key.
It contains no signing key. Use an environment matching that toolchain; acquiring
or installing a new toolchain is a separate setup action.

From the source root, prepare a fresh sibling build-input directory using the
existing stock command. Set `$BuildPython` to the matching interpreter:

```powershell
$sourceRoot = (Get-Location).Path
$outputRoot = Join-Path (Split-Path $sourceRoot) 'reader-prepared-inputs'
$finite = Join-Path $sourceRoot 'tools/encyclopedia_build/source-manifest.json'
$tools = Join-Path $sourceRoot 'tools/encyclopedia_build/TOOLCHAIN.json'
$profile = Join-Path $sourceRoot 'tools/encyclopedia_build/NORMAL_PROFILE.json'
& $BuildPython -B -m tools.prepare_encyclopedia_qualification_stage `
  --source-root $sourceRoot --output $outputRoot `
  --public-inventory-sha256 (Get-FileHash 'public-source.json' -Algorithm SHA256).Hash.ToLower() `
  --frozen-sources $finite --frozen-sources-sha256 (Get-FileHash $finite -Algorithm SHA256).Hash.ToLower() `
  --module-sha256 (Get-FileHash 'suite_core/encyclopedia/MODULE_MANIFEST.json' -Algorithm SHA256).Hash.ToLower() `
  --toolchain $tools --toolchain-sha256 (Get-FileHash $tools -Algorithm SHA256).Hash.ToLower() `
  --normal-profile $profile --normal-profile-sha256 (Get-FileHash $profile -Algorithm SHA256).Hash.ToLower() `
  --compiled-sources-only
```

The command validates and prepares source inputs; it does not compile or launch
an application. `GENERATED_SOURCE_PINS.json` records the exact three generated
entry/profile sources used by the retained executable. Their stock regeneration
is checked during archive preparation. Together with the 406 unchanged Python
source inputs, they account for its 409 compiled modules. The finite descriptor
also retains the associated non-Python resources.

The prepared stage contains the normal `TagEncyclopediaReader.spec` and
`TagEncyclopediaRoot.spec` stock recipes. Subsequent compilation must retain
their toolchain isolation, analysis closure and compiled-source readback checks.
New binaries need their own package, native and release qualification.

Private Qt qualification recipes, operator tools and tests without public
admission are omitted from this archive and retained separately. The admitted
test subset still has incomplete scaffolding and Organizer dependencies; no
complete passing public Reader suite is claimed. Manager test results do not
qualify the different Reader runtime.
