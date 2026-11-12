# Bounded video runtime admission

VID-02P1, linked to VID-02, VID-03 and SUITE-10B. November 11, 2026.

The normal packaged Organizer copied a synthetic Manager clip and reopened its saved needs-review record through Ingest > View > Review Media. Opening playback remained in runtime-file checking beyond the test's 180-second wait. Retained stacks identified repeated ancestor traversal in `DependencyService._safe_component_file` and `unredirected_directory`.

The repair checks each existing directory once within a directory-admission call. For a canonical component root, the freshly checked parent already covers all root ancestors; noncanonical raw roots retain their separate validation so dot-dot paths cannot hide a redirect. Every file still gets its own regular-file/reparse test, exact resolved-path check, fresh hash and size. No file-integrity cache, installer or updater is introduced.

The focused regression run passed 52 cases: fresh hashes after a byte change, original raw dot-dot/reparse rejection, ancestor/site/package/file redirects, outside-root and missing/non-directory refusal, profile isolation, existing dependency admission and retained-environment recovery. A call-count regression prevents duplicate ancestor work from returning.

Using the actual shipped CPython 3.13.15 interpreter and retained isolated PyAV 18.1.0 installation, the repaired source admitted the runtime in 7.03 seconds, fully decoded the five-frame 64x64 synthetic MP4 in 2.38 seconds, and retrieved its first preview frame in 2.28 seconds. Early close drained the worker; the parent did not import PyAV. These are separate source-under-packaged-interpreter timings, not a claim that the final distributed GUI or arbitrary media have been qualified.

Baseline package: source `a3972b4107eee5f52a74d555d2bdf0a9709a393f`, ZIP SHA-256 `5b96ec5719e5abf18b4f0b11daf358f1e1153ab7e32a9b6bbdc7267440a67c3b`. Retained evidence: `vid02-packaged-codec-875x19s3/PATH_COST_TESTS.xml`, `PATH_COST_NATIVE.json`, `UI_COPY_RESULT.json`, `UI_RESULT_RESUME.json` and `UI_STACKS_resume.log`. Failed driver and pre-repair timeout evidence remains retained.

The existing unpublished Organizer a.0.5.0 delivery route owns the rebuilt artifact, real visible playback/Close, signed metadata, installed update/recovery and publication. Source checks alone are not delivered updates. The existing decoder profile remains proposed until its qualification boundary is reconciled. Manager encoding, generation models and full Video parity remain separate. No Root/Reader/C3A trial or manual-testing hold is added.
