# CollectiGen Generate 0.1.1-alpha.1

Generate is the Suite's separate application for image and video requests. This
alpha lets you edit requests, save drafts and reopen your work. Suite Manager
handles application installation, updates, shared settings and connections.

This release preserves a saved request through reopening and normal Close.
Whole seed values supplied as decimal or exponent text keep their exact
integer in the metadata, display and saved record.
Generation still needs a compatible, configured backend and suitable resources.
Real model execution, output quality and video generation are not qualified by
this release's synthetic workflow checks.

This is a portable Windows 11 x64 alpha. Release metadata is signed, but the
Windows executables are not Authenticode-signed. Windows may show an unknown
publisher warning or block unsigned apps. Follow the
[Suite download verification guidance](https://github.com/DigitalSteel/CollectiGen#readme)
before first startup. Manager verifies module signatures and file hashes before
installing updates.

## Install and open

1. Use the complete Suite distribution with Manager **0.8.0-alpha.1** and
   launcher **0.1.1**, available from [Suite releases](https://github.com/DigitalSteel/CollectiGen/releases).
   A Manager-only update cannot upgrade an older launcher. If the matching
   complete Suite is not listed yet, keep your current installation until it is.
2. Extract the complete Suite into its own folder and start **CollectiGen.exe**.
3. In Manager, choose **Applications**, then Generate's **Manage install and
   updates…**. Choose **Find Generate**, review the release, and confirm
   **Install** or **Update**.
4. Restart Manager if requested, then choose **Open Generate** in Applications.
   Start with **New Image…** or reopen a saved draft.

Close Generate normally when finished. Manager waits for its original work to
finish before shared settings and resources become available again.

## Updates and recovery

Back up the Suite's **Data** folder before updating. Close the workflow, then
review updates through Manager. Installation and restart remain your choices.
If an installation needs recovery, use **Recover installation** in its management
window and review the proposed change. Recovery restores the previous app state
and connection when available; saved drafts, runs and images stay separate.

## Help

- [Suite overview and usage](https://github.com/DigitalSteel/CollectiGen#readme)
- [Release notes](WHATS_NEW.md) · [GitHub release](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.generate/v0.1.1-alpha.1)
- [Support](https://github.com/DigitalSteel/CollectiGen/blob/manager-a032-source-candidate-20261011/SUPPORT.md)
- [Privacy](https://github.com/DigitalSteel/CollectiGen/blob/manager-a032-source-candidate-20261011/PRIVACY.md)
