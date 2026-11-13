# Manager installed update layout

SUITE-10B failure correction, November 13, 2026.

The stock installer consumes Squirrel.exe as Apps/Manager/Update.exe and
collectigen.suite-manager_ExecutionStub.exe as
Apps/Manager/collectigen.suite-manager.exe. Signed application members stay
inside Apps/Manager/current. Every inventory member retains its signed size
and SHA-256 check; SDK members are mapped, never omitted.

The fresh assembled ZIP retains its unused execution stub inside current.
The original compiled host knows whether it is using its initial baseline or
an authenticated ordinary successor. That lifecycle selects the stub location;
file existence does not select or fall back between layouts. Target admission,
explicit restored-prior admission and ordinary successor startup all use the
stock-applied layout. The next update verifies that same installed prior.

The original 0.6.5 Root reproduced a real failure applying signed 0.6.6: all
application files matched, but the two relocated SDK members were rejected.
It closed the old Manager normally, then offered neither Repair nor Close.
The disposable fixture was retained and its Root was scoped-stopped only after
helper grants and all retained actors were closed. This is failed update
and recovery evidence, not a normal-close or rollback pass.

This source repair does not change immutable released Roots. Product delivery
remains open: finish the original-owner prelaunch-rejection recovery path,
build the fixed Root and Manager, resolve older-Root update compatibility,
and exercise installed update, saved-state startup/Close and explicit rollback
in disposable private-desktop fixtures. Never retry the preserved failed
fixture, copy files into it to conceal failure, or claim a child-only package
repairs an already-running old Root. Reader/C3A, real libraries and model or
training execution are excluded. No manual-user-test hold is introduced.
