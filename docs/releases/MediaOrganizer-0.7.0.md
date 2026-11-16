# Media Organizer a.0.7.0 — Alpha candidate

2026-11-15 (America/New_York; unpublished build candidate)

## New features

- In LM Studio Connection, open Authentication to enter a token for one explicit Refresh Models request. The token is masked, clears when submitted and is never saved with connection preferences. Enter it again for a later refresh.
- Run AI can inspect the saved local LM Studio connection and its reported image-capable models. Optional authentication is separate from your local AI model configuration. Image analysis through LM Studio remains unavailable.

## Improvements

- Generation metadata parsing preserves integer seed values and text before floating-point conversion. Raw evidence, unknown fields and fractional sampling values stay intact; already-rounded metadata is not rewritten.
- Model-list refresh shows activity and elapsed time while waiting. Cancel or Close waits for client cleanup. An address change requires review before a token can be sent to the new address.
- An unusable response cannot become a selected model. Preferences remain a separate Save decision; refreshing does not load a model or start analysis.

## Scope and qualification

Only an explicit local HTTP address and port on this computer are supported. Model capabilities are server declarations, not inference qualification. This update does not load, unload or configure a model, run training or enable LM Studio image analysis.

This is an unpublished, unsigned build candidate, not an admitted Suite Manager update. The exact earlier 0.7.0 candidate ZIP (SHA-256 `96bb4b9a9593f101ac3991faf400445379158f4f0831fe0374c2f1a982ab9b3e`, source `80ba73e76058c4d282e5fb316d49e75704abdb42`) passed an automated offscreen check of original startup, generated-token model-list refresh, separate Save, fresh-process preference reopen, cancellation cleanup, safe failure with explicit retry, and normal Close. Fifteen default-system/light states were inspected. Responses were generated; no actual server or model was used.

That check does not qualify actual authenticated-server behavior, image inference, manual Run AI interaction, other themes or installed update/rollback. The existing missing-ExifTool installer prompt was declined; metadata writing and required-component readiness were not qualified. Signing, exact signed admission/recovery and publication remain open.

This source now also contains the exact-integer seed correction. The earlier tested ZIP and final-presentation ZIP2620e191 remain unchanged and do not contain that correction. A later package must bind its exact source and artifact identity and record applicable evidence reuse or additional qualification before publication. Organizer a.0.6.0 remains the shipped version.
