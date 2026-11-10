# CollectiGen

CollectiGen is a developing suite for collecting, identifying and organizing media, preserving metadata and provenance, and preparing training data. Its longer-term direction includes connected training and image-generation workflows, with ordinary work primarily performed locally.

## Downloads

For organizing images, [start with Media Organizer](#media-organizer). For
looking up tags, [install Tag Encyclopedia Reader through Suite Manager](#install-tag-encyclopedia-reader-with-suite-manager).
The applications are separate Windows x64 alpha downloads. Each release page
contains its matching source, checksums, and supported scope.

| Component | Current release | Download and purpose |
| --- | --- | --- |
| **Suite Manager** | [a.0.5.1](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.suite-manager/v0.5.1-alpha.1) | [Portable Windows ZIP](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.suite-manager/v0.5.1-alpha.1/SuiteManager-0.5.1-alpha.1-windows-x64.zip) · [SHA-256](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.suite-manager/v0.5.1-alpha.1/SuiteManager-0.5.1-alpha.1-windows-x64.zip.sha256). Install and open Tag Encyclopedia Reader on Windows. For Media Organizer, use the standalone download in the next row. |
| **Media Organizer** | [a.0.4.23](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.media-organizer/v0.4.23-alpha.1) | [Portable Windows ZIP](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.media-organizer/v0.4.23-alpha.1/MediaOrganizer-a.0.4.23-windows-x64.zip) · [SHA-256](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.media-organizer/v0.4.23-alpha.1/MediaOrganizer-a.0.4.23-windows-x64.zip.sha256). Local image organization, Tags/Descriptions and JSON/TXT sidecars. |
| **Tag Encyclopedia Reader** | [a.0.3.1](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.tag-encyclopedia/v0.3.1-alpha.1) | [Reader component ZIP](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.tag-encyclopedia/v0.3.1-alpha.1/TagEncyclopediaReader-0.3.1-alpha.1-windows-x64.zip) · [SHA-256](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.tag-encyclopedia/v0.3.1-alpha.1/TagEncyclopediaReader-0.3.1-alpha.1-windows-x64.zip.sha256). Component package installed and launched through Suite Manager. |
| **Optional Tag Database** | [a.0.2.1](https://github.com/DigitalSteel/CollectiGen/releases/tag/TagDatabase/v0.2.1-alpha.1) | [Data-package ZIP](https://github.com/DigitalSteel/CollectiGen/releases/download/TagDatabase/v0.2.1-alpha.1/TagDatabase-0.2.1-alpha.1.zip) · [SHA-256](https://github.com/DigitalSteel/CollectiGen/releases/download/TagDatabase/v0.2.1-alpha.1/TagDatabase-0.2.1-alpha.1.zip.sha256). Experimental catalog data for a new, empty test catalog; no application or model is included. |

Use the application ZIPs above to run the apps. Source and notices ZIPs are
listed on each release page. [Browse all releases](https://github.com/DigitalSteel/CollectiGen/releases).

## Download and run

Applications bundle Python and Qt. Microsoft Visual C++ v14 Redistributable x64,
version 14.44.35211.0 or newer, is required separately. Extract the **complete**
application ZIP into a new writable folder and keep its contents together.
Retain your previous package and separate profile/image/catalog backups.

### Install Tag Encyclopedia Reader with Suite Manager

1. Extract the Manager ZIP and start **CollectiGen.exe**.
2. For a fresh installation, choose **Applications > Tag Encyclopedia > Find reader**.
3. Review Reader **a.0.3.1**, choose **Install**, and confirm.
4. Close Manager normally and reopen it before the first **Launch** of Reader.

Manager a.0.5.1 has checksum sidecars but no signed Manager self-update envelope
or Windows Authenticode signature. Its fresh-install Reader workflow does not
qualify a Manager self-update or an upgrade of an existing installation.
The Reader component ZIP is supplied for that reviewed installation path; do not
treat it as a standalone launcher ZIP. Read the release's included guide first.

### Media Organizer

Extract its ZIP, then run **Start_MediaOrganizer.bat** at the ZIP root. Guided
Setup saves selected locations/preferences without starting a scan. Evaluate
with copied images and retain backups. See its [versioned notes](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.media-organizer/v0.4.23-alpha.1)
and included README for the changes and supported scope.

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

Organizer's recent patches have signed package, generated component, isolated
offscreen host startup/reopen/Close and disposable update/recovery evidence.
Live Manager-managed Organizer installation, active native editor/reader work,
non-elevated/clean-machine/DPI/assistive-technology and independent novice
acceptance remain unqualified. See each release's validation receipt.
Reader/Manager have their own qualification boundaries in their release notes.
Signatures and checksums establish integrity, not Windows publisher trust.

Broader cross-app automatic updating, training, generation and model execution
remain under development or require their own qualified releases. No models or
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
