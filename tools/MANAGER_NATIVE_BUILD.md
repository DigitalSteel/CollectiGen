# Manager native producer

`build_manager_distribution.py` produces an unsigned Windows x64 Suite through
the existing stock PyInstaller recipes. It never launches CollectiGen or Manager,
activates a deployment profile, signs an artifact, installs tools, or downloads
dependencies. A successful result is `BUILD_ONLY_NOT_EXECUTED`; it does not prove
native acceptance, UI acceptance, deployment trust, or release readiness.

## Accepted inputs

First obtain an accepted public export with its `public-source.json` inventory.
The finite frozen subset is independent of the runtime manifest's full catalog:
only explicitly reviewed rows are staged. Python rows must also be runtime
declared. Generated root code is separate from this source subset.

`manager_build/source-manifest.json` contains 316 public source/resource rows:
296 Python files and 20 resource, metadata, notice and documentation files.
They were reconciled against the retained a.0.3.1 frozen closure and accepted
public source checkpoint; the generated root wrapper is a separate 317th row.
All 31 omissions in the historical 285-row descriptor were added, including the
current acquisition factory and original-root connection owners. These pins are
**not blanket approval of a changing checkout or another export**. Verify them
against the accepted current export before freezing. Do not replace
the subset with every runtime-declared source to avoid resolving a dependency.
Missing analysis dependencies cause a hold and require explicit reconciliation.

Stage using the canonical public adapter (all paths are `pathlib.Path` values):

```python
from tools.prepare_manager_production_stage import prepare_public_stage

stage = prepare_public_stage(
    source_root, new_stage_output,
    public_inventory_sha256=accepted_inventory_sha256,
    frozen_sources=frozen_subset_path,
    frozen_sources_sha256=accepted_subset_sha256,
    manifest_sha256=accepted_runtime_manifest_sha256,
)
```

Subset JSON is exactly `{ "schema": 1, "source_rows": [...] }`. Each row has
`path`, `bytes`, and lowercase SHA256 `sha256`. The returned stage holds source
bytes, `STAGE.json`, and an intentionally refusing, unbound canonical root.
Keep `public-source.json` as a separately pinned input; the adapter does not put
that inventory inside the stage. Its receipt's absolute origin labels are
inspection evidence, not paths used by the producer or distributed payload.

`manager_build/tool-inputs.json` describes Python 3.14.7, Windows x64 cp314,
distribution versions, the Velopack 1.2.161 SDK file hashes, license inputs,
the four recipe/guard hashes, and the canonical root renderer AST hash. Supply
these dependencies explicitly; this descriptor performs no acquisition. It
includes upstream and license references without workstation or signing paths.
Installed distributions are checked by version; the descriptor does not hash
their complete installed trees or claim a reproducible wheel environment.
The recipes enforce approved binary origins and the existing system MSVC
prerequisite (x64, at least 14.44.35211.0); the producer does not install or
qualify that prerequisite on an end-user machine.

## Build-only command

After source inventory acceptance and native-build coordination, use the pinned
native interpreter. Run from the checkout; choose a new output directory:

```powershell
& $NativePython -B tools/build_manager_distribution.py `
  --source-stage $AcceptedStage --stage-sha256 $AcceptedStageReceiptSha256 `
  --public-inventory $AcceptedPublicInventory `
  --interpreter $NativePython --sdk-root $AcceptedSdkRoot `
  --notice-root $PublicExport --root-renderer tools/prepare_manager_production_stage.py `
  --output $NewBuildOutput
```

The Python API also accepts explicit `source_manifest` and `tool_inputs` paths
for a reviewed, pinned input set. The CLI uses the checked-in descriptors.
The notice root contains the descriptor's `licenses/...` inputs. SDK paths are
relative to the supplied SDK root. No historical `.tmp` stage is required.

The producer admits exact stage membership and pins, seals input bytes into a
separate working stage, then freezes `CollectiGenManager`. It verifies PE x64,
analysis closure, bundled sources, SDK metadata/extension, and license bytes.
It creates the existing `MANAGER_PACKAGE.json`, hashes those exact bytes, and
calls the AST-pinned canonical `root_entry(package_pin)` function. It executes
only that function definition, not the renderer module's top-level code.
The second freeze produces CollectiGen with that exact package pin. Source and
working-stage drift abort the build; a failed child prevents the root freeze.
Root failure retains the child evidence and bound working wrapper but does not
claim completion. Input staging remains untouched.

The resulting layout is:

```text
Suite/CollectiGen.exe
Suite/_bootstrap/...
Suite/MANAGER_PACKAGE.json
Suite/Apps/Manager/current/CollectiGenManager.exe
Suite/Apps/Manager/current/_internal/...
```

There is no mutable Suite `Data` directory. Evidence beside Suite includes
sealed-input hashes, freezer logs/commands, closure reports, and either
`BUILD_RESULT.json` or `BUILD_FAILURE.json`. A failed output is retained for
diagnosis; reruns require a new output. Evidence may contain build-machine
paths and is not part of the distributable Suite.

## Verification and remaining gates

Run project Python's focused synthetic tests:

```powershell
& ./.venv/Scripts/python.exe -B -m pytest tests/test_manager_distribution_builder.py -q
```

These tests connect to the actual public adapter and canonical renderer while
substituting both freezer passes and tool inspection. Fake PE/SDK/interpreter
bytes are never executed. They prove producer sequencing, exact pin/layout
propagation, drift/refusal handling, and held-state reporting. They do not prove
PyInstaller's actual closure, a runnable native package, or visual behavior.
Current-export reconciliation and an authorized native freeze remain required;
signing, startup, recovery, update and distribution acceptance remain separate
Manager-owned gates.

This reuses the existing stage, renderer, recipes and binary guards to make the
Manager build inspectable while preserving source authority and failed-build
recovery. Navigation and public-export enrollment of these new tooling entry
points are integration-owner work.
