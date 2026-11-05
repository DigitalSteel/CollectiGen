# Krea 2 package compatibility report

`core.krea_training_compatibility.KreaTrainingCompatibilityService.report`
produces an in-memory loss report from an explicitly verified training package
and an explicitly supplied model registry. It is a source prerequisite without
a connected UI, executable export, trainer recipe or runner. Execution readiness
is always false; it never creates staging, output or trainer configuration files.

The service independently verifies the package and compares the complete manifest
and fingerprint with the expected snapshot. It preserves exact item roles,
captions, hashes, exposures, exclusions and manifest evidence, and accounts for
every recorded configuration field. Stale packages, unsupported package schemas,
wrong target families and cancellation produce no report. Unknown setting evidence
is retained without interpretation; the canonical verifier still decides which
package schemas can be accepted.

The current source comparison pins Krea code
`db3984fbc6e13b34c0064990fc2d95ac64d00058` and the Diffusers example
`2a9417c30ae556b8d8864ad2d5a6068d221dc871`. The latter's folder branch opens
caption sidecars as images and uses a shared instance prompt. A future explicit
dataset-column translation must preserve per-image captions and split decisions.
No folder is silently declared compatible, and no captions are rewritten.

An exact registered base hash establishes local byte identity only. RAW training,
separate Turbo validation, companion revisions, tokenizer/geometry/weighting
semantics, unused output custody, strict checkpoint resume and retention,
acquisition/tracking/transfer choices, runtime, hardware and produced LoRA quality
remain explicit requirements. The SDXL preview and its guard are unchanged.

Focused synthetic tests cover these source behaviors. They do not qualify a
real model, installed trainer, native workflow or distributed application update.
User-facing adoption and signed host packages remain separate delivery gates.
