# CollectiGen

CollectiGen is a developing suite for collecting, identifying and organizing media, preserving metadata and provenance, and preparing training data. Suite Manager handles installation, updates, shared settings and connections. Generate and Train run in separate application windows launched from Applications, with ordinary work primarily performed locally.

## Downloads

To organize images, [download Media Organizer](#media-organizer). To look up
tags, [install Tag Encyclopedia Reader through Suite Manager](#install-apps-with-suite-manager).
These are Windows x64 alpha releases. Each release page includes matching
source, checksums, and release notes.

| Component | Current release | Download and purpose |
| --- | --- | --- |
| **Suite Manager** | [a.0.8.0](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.suite-manager/v0.8.0-alpha.1) | [Complete Windows Suite ZIP](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.suite-manager/v0.8.0-alpha.1/CollectiGen-Suite-a.0.8.0-windows-x64.zip) · [SHA-256](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.suite-manager/v0.8.0-alpha.1/CollectiGen-Suite-a.0.8.0-windows-x64.zip.sha256). Install, update and launch independently versioned workflow apps. Includes launcher 0.1.1. |
| **Generate** | [a.0.1.0](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.generate/v0.1.0-alpha.1) | [Module ZIP](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.generate/v0.1.0-alpha.1/CollectiGen-Generate-a.0.1.0-windows-x64.zip) · [SHA-256](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.generate/v0.1.0-alpha.1/CollectiGen-Generate-a.0.1.0-windows-x64.zip.sha256). Image/video request drafts and configured generation workflows. Install and open through Manager 0.8.0 / launcher 0.1.1. |
| **Train** | [a.0.1.0](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.train/v0.1.0-alpha.1) | [Module ZIP](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.train/v0.1.0-alpha.1/CollectiGen-Train-a.0.1.0-windows-x64.zip) · [SHA-256](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.train/v0.1.0-alpha.1/CollectiGen-Train-a.0.1.0-windows-x64.zip.sha256). Training-package review, SDXL dataset preparation and saved preparation history. Install and open through Manager 0.8.0 / launcher 0.1.1. |
| **Media Organizer** | [a.0.7.0](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.media-organizer/v0.7.0-alpha.1) | [Portable Windows ZIP](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.media-organizer/v0.7.0-alpha.1/MediaOrganizer-a.0.7.0-windows-x64.zip) · [SHA-256](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.media-organizer/v0.7.0-alpha.1/MediaOrganizer-a.0.7.0-windows-x64.zip.sha256). Local image organization, Tags/Descriptions and JSON/TXT sidecars, plus Training dataset planning and offline image/video drafts. |
| **Tag Encyclopedia** | [a.0.3.1](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.tag-encyclopedia/v0.3.1-alpha.1) | [Reader component ZIP](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.tag-encyclopedia/v0.3.1-alpha.1/TagEncyclopediaReader-0.3.1-alpha.1-windows-x64.zip) · [SHA-256](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.tag-encyclopedia/v0.3.1-alpha.1/TagEncyclopediaReader-0.3.1-alpha.1-windows-x64.zip.sha256). View and edit tags. |
| **Tag Database** | [a.0.2.1](https://github.com/DigitalSteel/CollectiGen/releases/tag/TagDatabase/v0.2.1-alpha.1) | [Data-package ZIP](https://github.com/DigitalSteel/CollectiGen/releases/download/TagDatabase/v0.2.1-alpha.1/TagDatabase-0.2.1-alpha.1.zip) · [SHA-256](https://github.com/DigitalSteel/CollectiGen/releases/download/TagDatabase/v0.2.1-alpha.1/TagDatabase-0.2.1-alpha.1.zip.sha256). Catalog tag data for use with the Tag Encyclopedia. |

Use the application ZIPs above to run the apps. Source and notices ZIPs are
listed on each release page. [Browse all releases](https://github.com/DigitalSteel/CollectiGen/releases).

## Download and run

Applications bundle Python and Qt. Microsoft Visual C++ v14 Redistributable x64,
version 14.44.35211.0 or newer, is required separately. Extract the **complete**
application ZIP into a new writable folder and keep its contents together.
Retain your previous package and separate profile/image/catalog backups.

### Install apps with Suite Manager

Use the **complete Suite a.0.8.0 ZIP** for Manager 0.8.0-alpha.1 and launcher
0.1.1. A Manager-only update cannot replace an older launcher. Keep your earlier
installation and separate Data/image/catalog backups. Extract the new ZIP into
its own short writable folder; do not overwrite a running installation or an
interrupted recovery folder. Moving older profiles into this Suite is not
qualified by this release.

1. Start **CollectiGen.exe** from the extracted Suite folder.
2. In **Applications > Generate and Train**, choose the app's **Manage install and updates…**.
3. Choose **Find Generate** or **Find Train**, review the offered version, then confirm **Install**.
4. Restart Manager when requested, then choose **Open Generate** or **Open Train**.

Use **App catalog** for other applications; Media Organizer keeps its **Get or
update Media Organizer** review. Reader and Organizer retain their signed
installation routes. Installations, updates and restarts remain explicit choices.

**Generate** creates image/video requests and saves drafts for reopening.
**More** keeps setup, remix and prompt-catalog tools accessible. Compatible
generation backends and models are required separately.

**Train** opens training packages, prepares SDXL datasets and reopens **Saved
Preparations…**. Saved training runs remain accessible under **More**.
Dataset preparation does not start training. Review compatible trainer software
and resources before proceeding.

Manager keeps shared settings, resources and connections together. Close an open
workflow normally before changing shared resources or updating its installation.
During preparation, Cancel requests a safe stop; wait for completion before
leaving. Installation recovery preserves saved work separately from app files.
Keep the prepared workspace with its receipt.

This release's generated-profile checks cover signed module installation,
restart, Generate Close → Train Open, fresh native health, exact draft reopening,
synthetic preparation/history/cancellation, and supported Train rollback.
The non-elevated checks used a filtered same-user medium-integrity token.
Separate clean Windows profiles, public-origin Mark of the Web and Smart App
Control behavior are not claimed. Real generation, video output, training,
model quality and hardware suitability remain unqualified.

Release metadata is RSA-signed; Windows executables are **not
Authenticode-signed** and may show publisher warnings. Do not disable Windows
security policy. See the [Manager release notes](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.suite-manager/v0.8.0-alpha.1)
and each module's validation asset for exact scope.

### Media Organizer

Use Media Organizer to organize local images, edit Tags and Descriptions, and prepare datasets. Related Rule Repairs shows proposed changes before you confirm; Restore Last Rule Repair restores the original rules.

Extract its ZIP, then run **Start_MediaOrganizer.bat** at the ZIP root. Guided
Setup saves selected locations/preferences without starting a scan. Evaluate
with copied images and retain backups. See its [versioned notes](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.media-organizer/v0.7.0-alpha.1)
and included README for the changes and supported scope.

Organizer a.0.7.0 exports reusable Tag names/aliases, Characters and reviewed Outfit prompt choices for Manager. Choose **Knowledge > Prompt Catalog**, then **Knowledge > Prompt Choices > Export Prompt Catalog for Generate**. Review and save the export, then import it in Manager. Existing rule repair/Undo recovery, Dataset Training plans and offline drafts remain included.

Organizer a.0.7.0 keeps imported generation seeds exact, including long
integer values. Previously rounded records are not rewritten automatically.

In **Setup > AI Tools & Models > LM Studio Connection**, review the reported
model list, then choose **Save Connection** separately. Use **Authentication**
for an optional token for one **Refresh Models** request; it is masked, cleared
on submission and not saved. This connection does not load models, change
your AI provider or analyze images.

Generated-profile qualification covers startup/Close, saved preferences, the
signed 0.6 to 0.7 update and software rollback. When using Manager, restart
Manager after installation, update or restoring an earlier Organizer version.
Actual authenticated LM Studio installations, inference, metadata writing
and optional-tool readiness remain unqualified.
If a generated image or sidecar changes after copy preview, Ingest explains the
refusal and directs you to review the current Manager result again.
Launch **Train** from Manager's **Applications** to review transferred Training drafts. The original Organizer plan remains separate.

### Optional catalog data

No Tag Database, snapshot, image library or model is bundled into these app
downloads. The optional Tag Database is a separate download, and
shared-consumer integration is experimental. Use a new empty test catalog;
existing-catalog updates/merges remain unqualified. Downloading or verifying a
data package does not apply it or authorize changing your normal catalog.

## Development and qualification status

The download links identify actual published versions. The default branch is a
project landing/source checkpoint; use each component's tagged source or matching
source asset to reproduce that component. Unreleased code and roadmap plans do
not describe the capabilities of the downloads.

Install Media Organizer through Manager or use its standalone download. Each
release's notes and validation receipt describe its executed startup, update and
recovery checks. Use the supplied checksums to verify downloaded files.

Generate and Train are independently versioned modules launched by Manager;
the release notes identify their supported runtime routes and qualification limits. No models or
supported Linux builds are supplied. Your existing review, update-channel,
installation and restart choices are not changed by downloading an update.

## Feedback

Search [existing issues](https://github.com/DigitalSteel/CollectiGen/issues) before reporting a problem. Include the component version, operating system, steps, expected result and actual result. A text-only report is welcome.

Check attachments for private or sensitive information before posting. Report security vulnerabilities privately.

See [Support](SUPPORT.md) and [Privacy](PRIVACY.md).

## Licensing

Original application code and scripts use [AGPL-3.0-or-later](LICENSE).

Original documentation and the original copyrightable selection, organization and additions in compiled System Knowledge use [CC-BY-SA-4.0](licenses/CC-BY-SA-4.0.txt).

These licenses do not relicense user data, facts, models or incorporated third-party material. See the complete [license scope](LICENSES.md) and [third-party notices](THIRD_PARTY_NOTICES.md).

