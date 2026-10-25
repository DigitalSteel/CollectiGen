# Manager 0.6.1-alpha.1 source checkpoint

This default-free source snapshot builds on the qualified Manager source commit
1a4e4b2b617005567d4dfb799242b4c9b9c62601. It changes only failed-operation
guidance, direct regression checks and matching Manager patch-version notes.
Stale or unknown observations request refresh and a fresh review. A currently
observed pending receipt permits selected recovery guidance. Saved writes retain
their do-not-apply-again warning. No transaction or admission service is replaced.

This is a source-only checkpoint, not a new binary release, installer or tag.
No compiled package or broad novice/keyboard/DPI journey acceptance is claimed.
The previously published binaries retain their original identities and limits.

Catalog/default resources and runtime binaries are omitted. This tree is not
runnable by itself or a complete binary corresponding-source companion. Separate
matching companions are required; SOURCE_REQUIREMENTS.json records their scope.
Preserve LICENSE, LICENSES.md and third-party notices. No private profiles,
libraries, model weights or user data are included.


Developer harness addition: docs/MANAGER_CATALOG_RECIPE.md documents one guarded,
opt-in original Manager source-entry stale-review recipe. Run its explicit tests
with pytest tests/test_manager_catalog_recipe.py. The native case skips without
pinned generated inputs and an admitted Windows context. This is routing/recipe
infrastructure, not all-aspects or packaged acceptance; application code/version
is unchanged. The full-workspace suite registry/UI evidence validator is not
included as a runnable subsystem here. Its exact integration deltas are retained
under docs/patches with preimage hashes; do not apply them to a different baseline.
