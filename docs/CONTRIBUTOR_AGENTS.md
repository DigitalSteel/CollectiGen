# CollectiGen contributor instructions

Use the task brief and its approved starting commit. Confirm the target repository, base branch, assigned scope, and completion checks. Create a new local branch for each task and keep unrelated changes outside the diff.

Use synthetic fixtures. Keep private libraries, credentials, model weights, and application state outside the checkout and any contribution. Follow the setup and focused checks in the root `CONTRIBUTING.md`. Report checks you ran and any acceptance criteria you could not verify on your machine.

Push only to the fork or task branch authorized in the brief. Open a draft pull request against the specified base branch and include the starting SHA, summary, checks and results, and blockers. Push revisions to the same PR branch. Maintainers handle merges, tags, and releases.

This file is also published at the repository root as `AGENTS.md`. Propose edits to `docs/CONTRIBUTOR_AGENTS.md` so the export can regenerate the root copy.
