# CollectiGen

CollectiGen is a developing suite for collecting, identifying and organizing media, preserving metadata and provenance, and preparing training data. Suite Manager includes connected Generate and Training workspaces for configured local tools, with ordinary work primarily performed locally.

## Downloads

To organize images, [download Media Organizer](#media-organizer). To look up
tags, [install Tag Encyclopedia Reader through Suite Manager](#install-apps-with-suite-manager).
These are Windows x64 alpha releases. Each release page includes matching
source, checksums, and release notes.

| Component | Current release | Download and purpose |
| --- | --- | --- |
| **Suite Manager** | [a.0.7.11](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.suite-manager/v0.7.11-alpha.1) | [Portable Windows ZIP](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.suite-manager/v0.7.11-alpha.1/SuiteManager-a.0.7.11-windows-x64.zip) · [SHA-256](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.suite-manager/v0.7.11-alpha.1/SuiteManager-a.0.7.11-windows-x64.zip.sha256). Install and manage the CollectiGen suite of apps on Windows. |
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

Use Suite Manager to install and update Media Organizer and Tag Encyclopedia Reader. Generate and Training are built into Manager; models and optional runtimes are configured separately.

1. Extract the complete Manager ZIP into a new, short writable folder, such as **C:\CollectiGen**, and start **CollectiGen.exe**.
2. In **Applications**, choose **Get or update Media Organizer**, or **Tag Encyclopedia > Find reader**.
3. Review the offered version, choose **Install**, and confirm.
4. Restart Manager when requested, then open the installed app from its workspace.

Use **Generate** for image/video drafts, saved work and configured local generation tools. Saved setups and Remix can open a separate image draft while retaining drafts already open. Use **Training** for dataset preparation, trainer setup, saved runs and reviews. Open the **Help** group or press **F1** in either workspace for its guide.

In **Generate > Use Organizer Prompt Catalog**, import Organizer’s export to start an image draft with searchable Tags and Character/Outfit prompt choices. Saving and reopening the draft retains that catalog. **Create Generation Setup** offers manual text or a selected image; local description settings return to the unfinished setup. For optional image suggestions, **Review Local Description Setup** can save the model choices and open **Set Up Components**. Check readiness, review any required caption-component or model download, then return and choose Suggest. Opening or returning starts no download or analysis; manual authoring stays available.

Manager saves your theme, color-blind colors and tooltip choices in **Settings**, and restores them at the next launch. **Current build** identifies the bundled Python/Qt runtime and package version.

**Training > Set Up Trainer** can install the reviewed trainer software or verify an existing installation. **Other Setup Options** includes offline packs, repair, activation and rollback. Saved operations appear when work is available to review, resume or cancel. Installing software, activating it and starting training require separate reviews.

Find interrupted Training packages from **Training > Review Interrupted Training Package**. Use **Choose Package Location** to list packages by name, or **Browse Exact Receipt** for advanced selection. Search shows elapsed activity and **Cancel Safely**;review leaves package files unchanged.

Manager a.0.7.11 checks GitHub for newer compatible signed Reader and Organizer releases. Ordinary module updates no longer require a new Manager build. Review and confirm each installation in Applications. Generate and Training remain in Manager.

Training Package Review shows the target, image counts and next preparation
action. It distinguishes package verification from unchecked trainer readiness
and returns to **Training**. History management remains included.

Training history and saved SDXL preparations offer **Forget From This List**
and **Recover Saved List**. Forget keeps reviews, checkpoints and outputs intact;
Recover finishes an interrupted saved-list update.

Use a short installation path: long paths can prevent the bundled Qt Windows
plugin from loading. Keep the folder together. Existing component records are
bound to their installation location; moving installations and migration from
older portable Manager releases are not qualified by this release. Release
metadata is signed; the Windows executable is not Authenticode-signed.

For Manager a.0.7.11, an existing a.0.7.9 or later installation can use the reviewed component update. Component-only updates retain the existing launcher. Use the complete ZIP for a fresh installation or to replace an older launcher; a launcher already updated to a.0.7.10 needs no replacement for a.0.7.11. Close Manager first, back up Suite and preserve Data. If an update is stopped in recovery, keep that folder intact and extract the new ZIP separately. The exact a.0.7.11 ZIP passed startup, the Training page and normal Close. Signed prior a.0.7.10 was authenticated; unchanged failed-start recovery evidence is retained. Model/trainer execution and a new forced rollback were not run. Keep the previous folder and separate data backups. See the [Manager release notes](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.suite-manager/v0.7.11-alpha.1) for version-specific changes.

The Reader component ZIP is supplied for Manager's reviewed installation path.
Read the release's included guide before using component ZIPs directly.

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
Review transferred Training drafts in **Suite Manager > Training > Review a Dataset Draft**. The original Organizer plan remains separate.

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
