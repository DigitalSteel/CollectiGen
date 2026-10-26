# Standalone root integrity verifier

SUITE-06D2A admits the existing `tools/verify_encyclopedia_root_release.py`
and `tests/test_encyclopedia_root_release_verification.py` into the accepted
component source cohort. It adds no new verifier or application capability.
The public 0.2.2 release already listed a dedicated root-verifier source asset;
that metadata establishes prior publication, not byte equality with this cohort.
The admitted helper/test are pinned by the stock `public-source.json` inventory.

Run the helper's `--help` for its required explicit local paths and expected
version, channel, basename and public trust SHA-256. There is no network fetch,
default trust selection, signing, application launch, installation or activation.
Inputs must be ordinary, unredirected local files. Output and report locations
must be fresh; retain a held report and any partial extraction as failure evidence.
Use only material authorized for inspection; this tool grants no private-data
access authority.

The helper reuses `release_contract` for signed root metadata and artifact
verification, the stock frozen payload policy and component staging extraction,
and exact inventory/directory readback. It checks Windows x64 PE headers
statically; it never loads executable code. Reader metadata and mutable data,
model/database files, aliases, unsafe paths and unexpected archive members are
refused. Main-application portable-package admission and Reader admission retain
their independent stricter contracts.

`ROOT_INTEGRITY_VERIFIED` always has `execution_qualified=false`. It proves
the selected bytes' bounded integrity only. It establishes neither native
startup/health nor a safe root/Reader rollback pair. A source revert restores
the prior code cohort; it must never restore user catalog or Data bytes.

The focused test module uses inert synthetic PE/archive bytes and the existing
disposable test-only signature fixture. Those signatures are not release signing.
Source admission does not qualify a real binary, packaged Undo, migration or
recovery. The helper and tests remain under the repository LICENSE, with no new
bundled dependency or notice obligation.
