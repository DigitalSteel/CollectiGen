# Original Manager catalog source-entry recipe

Owner: `SUITE-05B1` under [the catalog acceptance contract](CATALOG_TRANSACTION_ACCEPTANCE.md).
`tools/acceptance/manager_catalog_recipe.py` provides one finite, opt-in Windows
stale-review route. Its default action checks admission without creating output.
It uses the original non-demo main, catalog host, controller, executor and Close;
it supplies only the existing observer hook and process-local Qt events.

## Admission

Supply a SHA256-pinned JSON recipe with exactly these fields:

| Field | Requirement |
| --- | --- |
| `schema_version` | `1` |
| `scenario` | `stale`; other workflows are not implicit options |
| `source_root` | Existing default-free Manager public export under this workspace's `.tmp` |
| `source_inventory_sha256` | Exact `public-source.json` SHA256 |
| `manager_version` | Exact version/manifest identity in that source |
| `input_receipt` | Existing generated-input `ADMISSION.json`, under `.tmp` |
| `input_receipt_sha256` | Exact generated receipt SHA256 |
| `fixture_root` | A new disjoint `.tmp` directory; never an existing profile |
| `interpreter_sha256` | Exact already admitted interpreter SHA256 |

The generated receipt has `schema_version: 1`, `synthetic: true`, an explicit
`source` folder within its own zone, and `initial_files` rows containing `path`
and `sha256`. Every selected packet input, including `TagDatabase.zip`, must
match those pins. Reuse the stock synthetic Foundation producer and existing
generated receipt; do not relabel live content as synthetic. The retained B/C
receipts are examples of actual generated input admission, not permission to
inspect any live catalog or private content.

Before creating a fixture the runner rejects changed/extra source files,
source/metadata mismatches, missing pins, redirects, protected paths, workspace
escapes, occupied targets and overlapping source/input/output zones. It reuses
the existing stdlib workspace guard and public inventory validator. Admission
does not execute source code or claim native acceptance. No dependency is installed.

## Run and retain

Use the project interpreter and explicit recipe hash:

```powershell
.\.venv\Scripts\python.exe -B tools\acceptance\manager_catalog_recipe.py --recipe ABSOLUTE_RECIPE_JSON --sha256 RECIPE_SHA256
```

Add `--run` only for the admitted Windows execution. It creates a fresh `-I -B`
child, selects the declared source root, binds an isolated profile/temp tree,
uses Windows Qt with the admitted system Segoe UI font, and denies network,
additional processes, redirects, production data reads and external writes.
Neither the original source code nor the accessibility fixture boundary is widened.

The canonical pytest entry is:

```powershell
$env:COLLECTIGEN_MANAGER_CATALOG_RECIPE = 'ABSOLUTE_RECIPE_JSON'
$env:COLLECTIGEN_MANAGER_CATALOG_RECIPE_SHA256 = 'RECIPE_SHA256'
.\.venv\Scripts\python.exe -B -m pytest -q tests/test_manager_catalog_recipe.py::test_original_manager_stale_recipe --junitxml=NEW_RESULT_XML
```

Without both explicit opt-in values the native test skips; a skip is never
application acceptance. Ordinary admission/oracle tests run with disposable
inert fixtures and do not launch an application. Run serially: each native
recipe owns its new source/profile/catalog/process boundary.

The route navigates through visible Databases controls, reviews and confirms
the exact source/catalog/create choice, previews a Pack, injects one labelled
fixture-only later correction, and attempts the reviewed Apply once. It requires
stale rejection with that correction preserved, no import receipt, truthful
refresh/new-review guidance, unknown settings preservation and ordinary Close.
It does not implement a normal Undo or silently recover/retry an uncertain write.

`RESULT.json` records source/recipe identities, original PID, input hashes,
capture hashes, selected application module origins and original host/controller/
executor drain. The parent retains the original child until completion, records
its actual exit separately from retirement and checks the receipt identity and
acceptance criteria. A deadline requests guarded Close; it never kills original
work or replays an action. Preserve a held process/receipt for diagnosis.
Worker output and all generated files remain inside the new fixture.

Pin the canonical runner, test source, actual JUnit, captures and execution record
in the existing UI review receipt. Do not alter JUnit to satisfy a parser. The
review validator compares the declared path's JUnit spelling, preserving literal
dot directories and rejecting another module's matching basename.

## Evidence limits

This recipe proves one original source-entry branch. It does not qualify compiled
packages, live user data, recovery/Undo, model execution, a locked desktop or the
complete `manager-tag-database-review` novice/expert/keyboard/scaling journey.
Existing native saved/reopen/recovery results retain their exact identities and
are not invalidated merely because a canonical recipe was added. The registry is
routing evidence, not proof that every suite aspect has executed successfully.
Broader UI work remains with `UI-06`; no new application version is implied by
this developer harness checkpoint.
