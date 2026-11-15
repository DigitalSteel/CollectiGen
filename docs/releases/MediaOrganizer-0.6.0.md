# Media Organizer a.0.6.0 — Alpha

## New features

- In Setup > AI Tools & Models > LM Studio Connection, remember a local server address and preferred model key. Choose Refresh Models to review the reported model list, select a key, then Save Connection separately.
- Refresh reads model metadata only. It does not load a model, change the current AI provider or start image analysis. Cancel Refresh or Close stops the request and waits for cleanup. An unavailable server still allows manual preferences.

## Bug fixes

- Ordinary preference saves preserve independently saved LM Studio preferences. A settings file changed during publication is held for reload instead of replacing the newer state.

## Scope and startup

This update supports an explicit local HTTP address and port. Credential entry and image analysis through LM Studio are not included. Reported model capabilities do not establish inference readiness. Existing Library, Ingest, Dataset, metadata and recovery workflows remain available; this update does not start generation or training.

Extract the complete Windows x64 ZIP and run Start_MediaOrganizer.bat, or use the existing Suite Manager component update route. Keep backups and close active work before updating. The release metadata is signed using the existing update identity; the ZIP/launcher does not carry a Windows Authenticode publisher signature.
