# CollectiGen

CollectiGen is a developing suite for collecting, understanding and organizing images, preserving metadata and provenance, and preparing training data. Its longer-term direction includes connected training and image-generation workflows, with ordinary work primarily performed locally.

The project is in alpha. This repository contains a source checkpoint, not a complete installable suite.

## Available now

**Tag Encyclopedia a.0.1.8 for Windows x64** is published as an evaluation prerelease. It is a read-only reader for an explicitly selected Tag snapshot: search names and aliases, filter categories, and inspect published Tag details.

**The reader starts empty.** No Tag Database, snapshot, image library or model is included. The optional default Tag Database and a compatible Manager executable have not been publicly supplied. You need an approved compatible snapshot to browse content; the reader does not import a Tag Database pack.

[Download Tag Encyclopedia a.0.1.8](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.tag-encyclopedia/v0.1.0-alpha.8)

## Download and run

1. Open the release above and download **`TagEncyclopedia-a.0.1.8-windows-x64.zip`** from Assets.
2. Extract the entire archive into a new folder. Keep the complete **`TagEncyclopedia`** folder together.
3. Ensure **Microsoft Visual C++ v14 Redistributable x64, version 14.44.35211.0 or newer**, is installed. Python and Qt are bundled; no separate Python installation is needed. Do not substitute DLLs.
4. Open the extracted **`TagEncyclopedia`** folder and run **`TagEncyclopedia.exe`**.
5. If you have an approved compatible publication, choose **Open Snapshot**. Otherwise, the empty reader is expected.

Read the included `README.md`, `USER_GUIDE.md` and release notes. Preserve older packages and existing data while evaluating an alpha.

## Development status and limits

- **Suite Manager:** under development; no Manager executable is supplied by the current public release. Default acquisition and update profiles remain inactive.
- **Tag Database:** optional content, separate from the reader; not currently published.
- **Media Organizer and broader image, training and generation workflows:** ongoing development, not supplied as a complete public suite by this release.
- **Linux:** no supported Linux package is currently published.

This reader does not edit catalogs, import a Tag Database, run inference or include models. Hosted image examples require a separately admitted host.

Clean-machine acceptance and native installation/update workflows are not yet qualified. The release manifest has a detached signature; Windows Authenticode signing is not claimed. See the release notes for limits and source-to-binary correspondence.

## Feedback

Search [existing issues](https://github.com/DigitalSteel/CollectiGen/issues) before reporting a problem. Include the component version, operating system, steps, expected result and actual result. A text-only report is welcome.

Review attachments before posting. Do not upload private logs, credentials, catalogs, image libraries, Local Only content or private filesystem paths. Do not post security vulnerability details in a public issue.

See [Support](SUPPORT.md) and [Privacy](PRIVACY.md).

## Licensing

Original application code and scripts use [AGPL-3.0-or-later](LICENSE).

Original documentation and the original copyrightable selection, organization and additions in compiled System Knowledge use [CC-BY-SA-4.0](licenses/CC-BY-SA-4.0.txt).

These licenses do not relicense user data, facts, models or incorporated third-party material. See the complete [license scope](LICENSES.md) and [third-party notices](THIRD_PARTY_NOTICES.md).
