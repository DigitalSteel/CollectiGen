# Explicit stage-aware component transactions

The existing component_staging owner accepts release_schema=2 and explicit
maturity on stage_and_activate/stage_and_activate_admitted. Defaults remain
release_schema=1, maturity=None, retaining legacy paths, journals and verified
retained-code reuse after rollback. New-mode targets always require an absent
Apps/component/schema2/numeric/maturity destination; no overwrite or reuse.

Partial schema/maturity inputs refuse before root enrollment. New-mode release
metadata is signature-verified before entering the original root scope and again
before staging, with exact component/version/channel/maturity/platform/schema.
The verified original manifest and artifact inventory digests form the reference.
I1 DTO helpers do not replace signature verification or ComponentStageAdmission.
The fixture entry still requires its disposable marker; the admitted entry still
requires the existing root hold and fresh original-owner checks at handoffs.

Journal3 retains the existing phase machine, raw manifest/signature and retained
prior-journal custody. Legacy journal1/2 callers keep their defaults. Recovery,
journal validation and prior-journal reads require explicit allow_schema2=True
to accept the new format. This flag selects a format, not recovery confirmation,
native health, ownership or execution authority. Existing confirmations and
preflight remain mandatory. No profile or host enables this mode automatically.

New-mode active and prior references resolve through the original independently
signed history with a 32-record bound. Missing/tampered/foreign history refuses;
the new target's signature cannot authenticate its prior. Synthetic legacy seeds
without signed origin remain available only through the original legacy fixture
route. They cannot be promoted through the new mode. Recovery restores exact
prior reference and journal bytes while retaining code directories and all Data.

New-schema payload/active reads require a matching independently verified release
DTO passed as release=; current callers without it refuse the new reference.
Payload inventory/platform checks remain in force. This parameter validates
consistency, not provenance: callers must authenticate the original signed journal
and root first. Same-number promotion uses ComponentReleaseIdentity ordering;
numeric supported_from floors and schema regression hold remain explicit.

Saved selection persistence is described separately in
[Reader selection identity](READER_SELECTION_IDENTITY.md). Explicit
[original-host binding](READER_HOST_IDENTITY.md) retains snapshot/notification and
publication/Manager identity. Acquisition and normal profile activation remain held. Schema2 artifact acquire still
refuses before network/cache/transfer. Tests use inert archives/disposable signing
keys and synthetic original-owner scopes; they are source evidence, not native
health or real package qualification. Scoped source revert and retained exact
preimages provide code recovery. Existing licensing applies; no runnable app
version, migration or public release tag is added.
