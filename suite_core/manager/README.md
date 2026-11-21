# CollectiGen Suite Manager a.0.8.2

Suite Manager installs, updates and launches independently versioned applications.
It keeps shared settings, resources and connections together. Generate and Train
open in their own workflow windows from **Applications**.

## Download and start

- [Complete Windows Suite ZIP](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.suite-manager/v0.8.2-alpha.1/CollectiGen-Suite-a.0.8.2-windows-x64.zip) · [SHA-256](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.suite-manager/v0.8.2-alpha.1/CollectiGen-Suite-a.0.8.2-windows-x64.zip.sha256)
- [Release notes and matching source](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.suite-manager/v0.8.2-alpha.1)

Extract the **complete** ZIP into a new, short writable folder, such as
**C:\\CollectiGen**, keep the folder together and open **CollectiGen.exe**.
This ZIP includes Manager 0.8.2-alpha.1 and launcher 0.1.2. Use the complete ZIP
when you need the newer launcher.
Keep the previous installation and separate Data/image/catalog backups. Do not
overwrite a running installation or interrupted recovery folder. Older-profile
migration is not qualified by this release.

On Manager 0.8.2, open **Settings > Updates** and choose **Check for updates**.
The application shows published, compatible packages for review before download.

## Install and open a workflow

1. Choose **Applications > Generate and Train**.
2. Open the app's **Manage install and updates…** window.
3. Choose **Find Generate** or **Find Train**, review the release and confirm **Install**.
4. Restart Manager when requested, then choose **Open Generate** or **Open Train**.

Use **App catalog** for other applications and **Connections** for shared
software and connection setup. The existing signed Organizer and Reader routes
remain available. Opening an installation review starts no download. Installation,
updates and restarts remain explicit choices.

[Generate](../generate/README.md) creates image/video requests and saves drafts.
[Train](../train/README.md) reviews training packages, prepares SDXL datasets and
reopens saved preparation receipts. Preparing a dataset does not start training.
Close workflows normally before shared resource or installation changes. Cancel
requests a safe stop; wait for the result before leaving. Keep prepared workspaces
with their receipts and retain separate backups before recovery or updates.

## Tested scope

Generated-profile checks cover signed module installation/restart, Generate Close
to Train Open, normal Close, fresh native health, exact draft save/reopen,
synthetic preparation/history/cancellation and supported Train rollback.
Non-elevated execution used a filtered same-user medium-integrity token.
Real generation, video output, training, model quality, hardware suitability,
separate clean Windows profiles, Mark of the Web and Smart App Control behavior
are not claimed. See each release's **VALIDATION.json** for exact identities.

Release metadata is RSA-signed. Windows executables are **not Authenticode-signed**
and may show publisher warnings; do not disable Windows security policy. Microsoft
Visual C++ v14 Redistributable x64 14.44.35211.0 or newer is required separately.

[Suite overview](https://github.com/DigitalSteel/CollectiGen#readme) ·
[Support](https://github.com/DigitalSteel/CollectiGen/blob/manager-a032-source-candidate-20261011/SUPPORT.md) ·
[Privacy](https://github.com/DigitalSteel/CollectiGen/blob/manager-a032-source-candidate-20261011/PRIVACY.md)

