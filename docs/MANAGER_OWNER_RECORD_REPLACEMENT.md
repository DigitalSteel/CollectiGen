# Manager owner record replacement

SUITE-10B2 repairs the existing original launcher persistence boundary. Published
0.7.7 found and staged signed0.7.8, but two isolated Apply attempts failed with
Windows error5 replacing owner.json before the stock SDK or successor started.
The exact external source of that denial remains unproved.

For an existing marker only, DeploymentOwner._persist retries only PermissionError
Windows5/32 from os.replace: the same flushed temporary file, original lease and
original marker-byte snapshot, at most8retries, with a250ms monotonic budget and
25ms maximum backoff. Each retry verifies unredirected paths, unchanged lease and
old marker, and exact candidate bytes. Deadline is rechecked after those reads.
No acquisition, write/fsync, successful-replace readback or maintenance action is
replayed. RAM authority changes only after exact readback. Any changed state,
other error or exhausted budget takes the existing permanent fault latch. There
is no destination deletion, in-place write, new lease or implicit dispatch.

Product effect: tolerate a bounded Windows atomic-replacement denial while
preserving original ownership and recovery. This does not establish its original
Windows cause or prove the full update/recovery workflow fixed.

The source regressions cover transient/permanent denial, error classification,
initial acquisition, changed disk/RAM/candidate/lease, deadline consumed by reads,
flush failure and uncertain post-replace readback. Native final qualification
remains open until the corrected original Root completes the existing signed
update and explicit authenticated rollback, with all resources settled.

Delivery uses collectigen.suite-manager and its full Windows ZIP. A component-only
update retains the installed Root, so it cannot repair this pre-target failure.
Do not alter historical signed releases or reuse failed fixtures. No Reader/C3A,
real-library, model or trainer execution is authorized by this source change.
