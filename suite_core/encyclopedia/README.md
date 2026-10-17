# Tag Encyclopedia a.0.3.0 — Alpha candidate

Release identity: October 14, 2026. Documentation reconciled October 17, 2026.

Browse an explicitly selected local Tag Database, review Tags and Categories,
and use the available native editing tools for that catalog. Create a database
from Home for a new, separate catalog. Existing catalogs and images stay in
place. Optional default content requires review before Apply.

This candidate includes catalog creation and the corrected category/reload
workflow. Keep a verified backup and use copied catalogs for alpha testing.
Standalone startup has no implicit access to an Organizer library. Organizer
examples and correction review require an explicitly enrolled original caller.

Local synthetic installation, update, recovery and ordinary Close were exercised
with a separate signed test envelope. This is an alpha evaluation candidate.
Authenticated installation requires its own matching verified release package
and signature. Public updates and clean-machine use remain unverified. The
previous signed test package is retained separately.

## Run from this source

Use the qualified project environment (Python 3.13.15 and
PySide6_Essentials/shiboken6 6.11.2). From the repository root:

```powershell
& .\.venv\Scripts\python.exe -B -m suite_core.encyclopedia
```

The source entry installs no runtime, starts no AI service and enables no live
feed implicitly. Use the chosen catalog's own Save/Cancel controls before
switching catalogs or closing. An unknown, stale or incomplete authority keeps
writes held. Keep original recovery files when an operation needs attention.

See [What's New](WHATS_NEW.md) and [release history](RELEASE_HISTORY.json) for
versioned changes. [Module inventory](MODULE_MANIFEST.json) identifies the
separate component's exact source boundaries; it is not a release signature.
Historical release entries remain in the notes and history.

## Source and test delivery

This archive preserves the reviewed product source corresponding to the compiled
Reader. Private maintainer qualification tooling and tests without public
admission remain outside this archive. The admitted test files are not a
self-contained test distribution: some still need additional test scaffolding
and Organizer dependencies. A complete passing public Reader test suite has not
been established. Separate Manager results do not qualify these Reader tests.

See [Build reproduction](../../tools/encyclopedia_build/BUILD_REPRODUCTION.md)
for the stock stage command, pinned toolchain and generated source inputs.
