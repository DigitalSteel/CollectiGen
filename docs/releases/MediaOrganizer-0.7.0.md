# Media Organizer a.0.7.0 — Alpha candidate

2026-11-15 (America/New_York; unpublished build candidate)

## New features

- In LM Studio Connection, open Authentication to enter a token for one explicit Refresh Models request. The token is masked, clears when submitted and is never saved with connection preferences. Enter it again for a later refresh.
- Run AI can inspect the saved local LM Studio connection and its reported image-capable models. Optional authentication is separate from your local AI model configuration. Image analysis through LM Studio remains unavailable.

## Improvements

- Model-list refresh shows activity and elapsed time while waiting. Cancel or Close waits for client cleanup. An address change requires review before a token can be sent to the new address.
- An unusable response cannot become a selected model. Preferences remain a separate Save decision; refreshing does not load a model or start analysis.

## Scope and qualification

Only an explicit local HTTP address and port on this computer are supported. Model capabilities are server declarations, not inference qualification. This update does not load, unload or configure a model, run training or enable LM Studio image analysis.

This is an unpublished, unsigned build candidate. Source checks use generated data; actual authenticated-server behavior, packaged startup and saved-state/recovery acceptance have not been qualified for this candidate. It is not an admitted Suite Manager update. Existing recovery and release gates remain open.
