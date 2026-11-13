# CollectiGen

CollectiGen is a developing suite for collecting, identifying and organizing media, preserving metadata and provenance, and preparing training data. Suite Manager includes connected Generate and Training workspaces for configured local tools, with ordinary work primarily performed locally.

## Downloads

To organize images, [download Media Organizer](#media-organizer). To look up
tags, [install Tag Encyclopedia Reader through Suite Manager](#install-apps-with-suite-manager).
These are Windows x64 alpha releases. Each release page includes matching
source, checksums, and release notes.

| Component | Current release | Download and purpose |
| --- | --- | --- |
| **Suite Manager** | [a.0.7.1](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.suite-manager/v0.7.1-alpha.1) | [Portable Windows ZIP](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.suite-manager/v0.7.1-alpha.1/SuiteManager-a.0.7.1-windows-x64.zip) · [SHA-256](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.suite-manager/v0.7.1-alpha.1/SuiteManager-a.0.7.1-windows-x64.zip.sha256). Install and manage the CollectiGen suite of apps on Windows. |
| **Media Organizer** | [a.0.5.2](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.media-organizer/v0.5.2-alpha.1) | [Portable Windows ZIP](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.media-organizer/v0.5.2-alpha.1/MediaOrganizer-a.0.5.2-windows-x64.zip) · [SHA-256](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.media-organizer/v0.5.2-alpha.1/MediaOrganizer-a.0.5.2-windows-x64.zip.sha256). Local image organization, Tags/Descriptions and JSON/TXT sidecars, plus Training dataset planning and offline image/video drafts. |
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

1. Extract the complete Manager ZIP into a new, short writable folder, such as **C:\CollectiGen**, and start **CollectiGen.exe**.
2. In **Applications**, choose **Get or update Media Organizer**, or **Tag Encyclopedia > Find reader**.
3. Review the offered version, choose **Install**, and confirm.
4. Restart Manager when requested, then open the installed app from its workspace.

Manager a.0.7.1 includes dedicated **Generate** and **Training** workspaces for
image/video drafts, saved work, local setup, training preparation and retained
reviews. Models and optional runtimes are configured separately. Saved setups can
open a separate image draft while preserving the work already open. Both Remix
entries now do the same, retaining existing image and video drafts. Use the
**Help** group or **F1** in Generate and Training for their workflow guides.
Generate and update buttons fit smaller windows, and Tab moves out of update notes.

Find interrupted Training packages from **Training > Review Interrupted Training Package**. Use **Choose Package Location** to list packages by name, or **Browse Exact Receipt** for advanced selection. Search shows elapsed activity and **Cancel Safely**;review leaves package files unchanged.

Manager a.0.7.1 fixes opening an installed Organizer when its signed-package startup review takes longer. Existing Generate and Training workspaces remain available.

Training Package Review now shows the target, image counts and next preparation
action. It distinguishes package verification from unchecked trainer readiness
and returns to **Training**. History management and the a.0.6.7 launcher fixes remain included.

Training history and saved SDXL preparations now offer **Forget From This List**
and **Recover Saved List**. Forget keeps reviews, checkpoints and outputs intact;
Recover finishes an interrupted saved-list update.
This Manager release offers signed Reader a.0.3.1 and Organizer a.0.5.2.

Use a short installation path: long paths can prevent the bundled Qt Windows
plugin from loading. Keep the folder together. Existing component records are
bound to their installation location; moving installations and migration from
older portable Manager releases are not qualified by this release. Release
metadata is signed; the Windows executable is not Authenticode-signed.

For a.0.6.6 and earlier, use the full Windows ZIP in a new folder to receive
the fixed launcher. The built-in update in those versions cannot replace it.
The a.0.6.7 → a.0.6.8 update and reopening passed in an isolated installation.
Rollback testing remains incomplete. Keep your previous folder and separate
data backups.

The Reader component ZIP is supplied for Manager's reviewed installation path.
Read the release's included guide before using component ZIPs directly.

### Media Organizer

Extract its ZIP, then run **Start_MediaOrganizer.bat** at the ZIP root. Guided
Setup saves selected locations/preferences without starting a scan. Evaluate
with copied images and retain backups. See its [versioned notes](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.media-organizer/v0.5.2-alpha.1)
and included README for the changes and supported scope.

Organizer a.0.5.2 includes Dataset Training plans and offline image/video drafts. Manager-result copying now retains an explicit Retry after busy file work and clearly explains copy-only Undo through Recycle.
If a generated image or sidecar changes after copy preview, Ingest explains the
refusal and directs you to review the current Manager result again.
Review transferred Training drafts in **Suite Manager a.0.6.0 or later > Training >
Review a Dataset Draft**. The original Organizer plan remains separate.

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

Generate and Training have dedicated Manager workspaces; the release notes
identify the supported runtime routes and qualification limits. No models or
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
