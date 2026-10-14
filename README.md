# CollectiGen

CollectiGen is a developing suite for collecting, identifying and organizing media, preserving metadata and provenance, and preparing training data. Its longer-term direction includes connected training and image-generation workflows, with ordinary work primarily performed locally.

The suite is in early alpha. Its applications have separate releases and versions; the downloads below are evaluation packages with the limits described here.

## Downloadable now

| Application | What it does today | Windows x64 download |
| --- | --- | --- |
| **Media Organizer a.0.4.5** | Organize local images, manually edit Tags and Descriptions, save JSON/TXT sidecars, preview destinations, move ready images and browse the saved results in Dataset. | [Organizer release](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.media-organizer/v0.4.5-alpha.1) — choose **MediaOrganizer-a.0.4.5-windows-x64.zip**. |
| **Tag Encyclopedia a.0.1.8** | Read an explicitly selected compatible Tag snapshot: search names and aliases, filter categories and inspect published details. | [Encyclopedia release](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.tag-encyclopedia/v0.1.0-alpha.8) — choose **TagEncyclopedia-a.0.1.8-windows-x64.zip**. |

**For ordinary Windows use, choose the application ZIP named `windows-x64`.** GitHub's automatic “Source code” downloads and Organizer's `source-notices.zip` are for source/licensing access, not the runnable application. The source companion and `SOURCE_DELIVERY_SUPPLEMENT.md` provide corresponding source, notices and library-replacement guidance. Release checksums and detached signatures accompany the application; a detached signature is not Windows Authenticode signing.

The Encyclopedia starts empty. No Tag Database, snapshot, image library or model is included. You need an approved compatible snapshot to browse content. The reader does not import a Tag Database pack or edit catalogs. The optional default Tag Database is not currently published.

## Start on Windows

1. Download the application ZIP from its release's **Assets** and check the accompanying SHA-256 checksum. Extract the **entire ZIP into a new folder** and keep its folder structure intact.
2. Install **Microsoft Visual C++ v14 Redistributable x64, version 14.44.35211.0 or newer**, if needed. Python and Qt are bundled; no separate Python installation is needed. Do not substitute DLLs.
3. For Organizer, run **`Start_MediaOrganizer.bat`** in the extracted folder. For Encyclopedia, open the extracted **`TagEncyclopedia`** folder and run **`TagEncyclopedia.exe`**.
4. In Organizer's Guided Setup, choose **Library locations**, optional **AI setup**, **Preferences and defaults**, then **Review and finish**. Finish saves your choices without automatically opening a library or starting a scan. Evaluate with copied images and keep backups. Optional AI controls do not mean a model is installed or ready to run.
5. In Encyclopedia, choose **Open Snapshot** if you have an approved compatible snapshot. An empty reader is expected otherwise.

Read the application's included README and user guidance. Keep older packages and backups of profiles/data. For Organizer, update by extracting a complete new package into a separate folder; automatic/in-place updates and cross-version profile migration are not qualified. The Organizer launcher accepts `--profile-root` for a separate evaluation profile.

## Standalone apps and Suite Manager

These downloads run as standalone applications. **No Suite Manager executable is supplied by the current public releases.** You do not need Manager to launch them.

Manager is intended to help connect applications and manage their resources and updates. Manager-managed installation, launch, settings inheritance, update reconciliation and optional default acquisition are still in development; the published packages do not activate them.

## In development

- Tag Database download/import/update and cross-application Encyclopedia integration.
- AI/model execution, video/audio workflows, training and image generation.
- Automatic updates and broader connected suite workflows.
- Supported Linux packages; none are currently published.

The current downloads are limited evaluation releases, not a feature-complete suite. No models are bundled. Clean-machine installation, every profile type and large-library behavior are not claimed as qualified. Consult each release's known limits before using original media.

## Feedback

Search [existing issues](https://github.com/DigitalSteel/CollectiGen/issues) before reporting a problem. Include the component version, operating system, steps, expected result and actual result. A text-only report is welcome.

Review attachments before posting. Do not upload private logs, credentials, catalogs, image libraries, Local Only content or private filesystem paths. Do not post security vulnerability details in a public issue.

See [Support](SUPPORT.md) and [Privacy](PRIVACY.md).

## Licensing

Original application code and scripts use [AGPL-3.0-or-later](LICENSE).

Original documentation and the original copyrightable selection, organization and additions in compiled System Knowledge use [CC-BY-SA-4.0](licenses/CC-BY-SA-4.0.txt).

These licenses do not relicense user data, facts, models or incorporated third-party material. See the complete [license scope](LICENSES.md) and [third-party notices](THIRD_PARTY_NOTICES.md).
