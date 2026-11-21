# Contributing to CollectiGen

Start with the GitHub issue or maintainer brief for your task. Confirm the target repository, target branch, starting commit SHA, and expected checks before you edit. Work on a fresh local branch for each task. A component-source checkout covers a specific set of modules. Ask the maintainer for the appropriate baseline when the code or tests you need are absent.

## Check a source change

Run checks from the root of your local clone. On Windows, create a local Python environment for focused source tests. For a task in the public-source verifier, the focused test file is `tests/test_public_component_source.py`:

```powershell
py -3.13 -m venv ..\collectigen-contributor-venv
..\collectigen-contributor-venv\Scripts\python.exe -m pip install 'pytest>=8,<9'
..\collectigen-contributor-venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider tests/test_public_component_source.py
git -c core.whitespace=cr-at-eol diff --check
```

For a public export, run the source check on a clean checkout:

```powershell
py -3.13 tools/Test-PublicSource.py . --allow-git-metadata
```

The source check verifies the inventory before running the privacy scan. If it reports unlisted, missing, or mismatched files, record the exact commit SHA and findings. The privacy scan did not run in that case. Report inventory changes to the maintainer so the export can be regenerated from its reviewed inputs.

## Submit a change

For a public contribution, reuse your fork and push the task branch there. For an approved shared repository, push to the task branch the maintainer assigned. Open a draft pull request against the specified base branch. Include the baseline SHA, a summary, checks and results, and any remaining limitations. Push review fixes to the same PR branch. The maintainer handles the merge decision.

Use synthetic fixtures. Keep private libraries, Local Only data, credentials, model weights, and live application state out of contributions. See [Privacy](PRIVACY.md) and [Licenses](LICENSES.md).
