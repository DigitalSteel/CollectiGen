# CollectiGen

CollectiGen is a developing suite for collecting, identifying and organizing media, preserving metadata and provenance, and preparing training data. Its longer-term direction includes connected training and image-generation workflows, with ordinary work primarily performed locally.

## Downloads

To organize images, [download Media Organizer](#media-organizer). To look up
tags, [install Tag Encyclopedia Reader through Suite Manager](#install-apps-with-suite-manager).
These are Windows x64 alpha releases. Each release page includes matching
source, checksums, and release notes.

| Component | Current release | Download and purpose |
| --- | --- | --- |
| **Suite Manager** | [a.0.6.3](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.suite-manager/v0.6.3-alpha.1) | [Portable Windows ZIP](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.suite-manager/v0.6.3-alpha.1/SuiteManager-a.0.6.3-windows-x64.zip) · [SHA-256](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.suite-manager/v0.6.3-alpha.1/SuiteManager-a.0.6.3-windows-x64.zip.sha256). Install and manage the CollectiGen suite of apps on Windows. |
| **Media Organizer** | [a.0.5.0](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.media-organizer/v0.5.0-alpha.1) | [Portable Windows ZIP](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.media-organizer/v0.5.0-alpha.1/MediaOrganizer-a.0.5.0-windows-x64.zip) · [SHA-256](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.media-organizer/v0.5.0-alpha.1/MediaOrganizer-a.0.5.0-windows-x64.zip.sha256). Local image organization, Tags/Descriptions and JSON/TXT sidecars, plus Training dataset planning and offline image/video drafts. |
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

Manager a.0.6.3 includes dedicated **Generate** and **Training** workspaces for
image/video drafts, saved work, local setup, training preparation and retained
reviews. Models and optional runtimes are configured separately. Saved setups can
open a separate image draft while preserving the work already open. Use the
**Help** group or **F1** in Generate and Training for their workflow guides.

Use a short installation path: long paths can prevent the bundled Qt Windows
plugin from loading. Keep the folder together. Existing component records are
bound to their installation location; moving installations and migration from
older portable Manager releases are not qualified by this release. Release
metadata is signed; the Windows executable is not Authenticode-signed.

Use the full Windows ZIP in a new folder for a fresh installation. The
a.0.6.2 client can find and stage a.0.6.3; applying that update and rollback
have not yet been exercised. Earlier clients need the full ZIP because they
retain the old Apply timeout or predate the managed-update baseline.

The Reader component ZIP is supplied for Manager's reviewed installation path.
Read the release's included guide before using component ZIPs directly.

### Media Organizer

Extract its ZIP, then run **Start_MediaOrganizer.bat** at the ZIP root. Guided
Setup saves selected locations/preferences without starting a scan. Evaluate
with copied images and retain backups. See its [versioned notes](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.media-organizer/v0.5.0-alpha.1)
and included README for the changes and supported scope.

Organizer a.0.5.0 adds Dataset Training plans and offline image/video drafts.
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
