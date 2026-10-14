# CollectiGen

CollectiGen is a developing suite for collecting, identifying and organizing media, preserving metadata and provenance, and preparing training data. Its longer-term direction includes connected training and image-generation workflows, with ordinary work primarily performed locally.

CollectiGen is still in early alpha. You can try the two Windows apps below on their own; Suite Manager is not needed to open them.

## Try the Windows apps

**Media Organizer a.0.4.5** lets you sort local images, edit Tags and Descriptions, save JSON/TXT sidecars, preview where images will go and move them into your library. You can then browse the results in Dataset.

[Download Media Organizer](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.media-organizer/v0.4.5-alpha.1). Under **Assets**, choose **`MediaOrganizer-a.0.4.5-windows-x64.zip`**.

**Tag Encyclopedia a.0.1.8** is a read-only browser for a compatible Tag snapshot. It can search names and aliases, filter categories and show Tag details. It starts empty: you'll need an approved compatible snapshot to see content. A default Tag Database is not available to download yet.

[Download Tag Encyclopedia](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.tag-encyclopedia/v0.1.0-alpha.8). Under **Assets**, choose **`TagEncyclopedia-a.0.1.8-windows-x64.zip`**.

Choose the **Windows ZIP** to run an app. The `source-notices.zip` and GitHub's “Source code” downloads are for people who want the source or licensing information. You don't need them to launch the app.

## Get started

1. Download your app's Windows ZIP and check its accompanying SHA-256 checksum. Extract the whole ZIP into a new folder.
2. If needed, install **Microsoft Visual C++ v14 Redistributable x64, version 14.44.35211.0 or newer**. Python and Qt are included, so you don't need to install Python separately. Keep the extracted files together.
3. For Organizer, run **`Start_MediaOrganizer.bat`** directly in the extracted folder. For Encyclopedia, open the **`TagEncyclopedia`** folder and run **`TagEncyclopedia.exe`**.
4. Organizer's Guided Setup walks you through library locations, optional AI settings and your preferences. **Finish** saves your choices; it doesn't start a scan. In Encyclopedia, use **Open Snapshot** when you have a compatible snapshot.

Start with copies of your images and keep backups. The included README and guides explain each app in more detail. To try a newer Organizer release, extract it into a separate folder and keep the old copy. Its launcher also accepts `--profile-root` if you want a separate evaluation profile.

## What is still being built?

Suite Manager is intended to help connect apps and manage their resources and updates. There is no public Manager executable yet. Manager-managed installation, launch, shared settings and update reconciliation are still being worked on.

These releases don't include automatic updates, Tag Database download/import/update, cross-app Encyclopedia integration, model execution, video/audio, training or image generation. No models are bundled. Optional AI settings in Organizer don't mean a model is installed or ready to use. There is no supported Linux package yet.

These are early releases, so keep backups and avoid testing on your only copy of important media. Automatic/in-place updates, moving profiles between versions, clean-machine installation and large-library behavior haven't been fully checked. The downloads have checksum files and detached release signatures; that is separate from Windows Authenticode signing.

## Feedback

Search [existing issues](https://github.com/DigitalSteel/CollectiGen/issues) before reporting a problem. Include the component version, operating system, steps, expected result and actual result. A text-only report is welcome.

Review attachments before posting. Do not upload private logs, credentials, catalogs, image libraries, Local Only content or private filesystem paths. Do not post security vulnerability details in a public issue.

See [Support](SUPPORT.md) and [Privacy](PRIVACY.md).

## Licensing

Original application code and scripts use [AGPL-3.0-or-later](LICENSE).

Original documentation and the original copyrightable selection, organization and additions in compiled System Knowledge use [CC-BY-SA-4.0](licenses/CC-BY-SA-4.0.txt).

These licenses do not relicense user data, facts, models or incorporated third-party material. See the complete [license scope](LICENSES.md) and [third-party notices](THIRD_PARTY_NOTICES.md).
