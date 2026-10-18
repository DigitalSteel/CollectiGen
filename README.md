# CollectiGen

CollectiGen is a developing suite for collecting, identifying and organizing media, preserving metadata and provenance, and preparing training data. Its longer-term direction includes connected training and image-generation workflows, with ordinary work primarily performed locally.

The project is in alpha. Individual Windows components are available below; a complete installable suite is not yet published.

## Downloads

Choose a component from its release page, then download its Windows ZIP under **Assets**. Source-code and source-notices downloads are for development and licensing; they are not the runnable app.

| Component | Current public release | What it does |
| --- | --- | --- |
| **Media Organizer** | [a.0.4.5 for Windows x64](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.media-organizer/v0.4.5-alpha.1) | Organize local images, browse galleries and datasets, edit descriptions and tags, and save JSON/TXT sidecars. |
| **Tag Encyclopedia** | [a.0.1.8 for Windows x64](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.tag-encyclopedia/v0.1.0-alpha.8) | Read a compatible Tag snapshot, search names and aliases, filter categories and view Tag details. |

Both are evaluation prereleases. See [all releases](https://github.com/DigitalSteel/CollectiGen/releases) for downloads, release notes and source information. No Manager or complete-suite download is currently available.

## Download and run

Both applications bundle Python and Qt. Install **Microsoft Visual C++ v14 Redistributable x64, version 14.44.35211.0 or newer**, if needed. Extract the complete ZIP into a new folder and keep its contents together.

### Media Organizer

1. Download **`MediaOrganizer-a.0.4.5-windows-x64.zip`** from its release page.
2. Extract it, then run **`Start_MediaOrganizer.bat`** directly in the extraction folder. The launcher is at the ZIP root.
3. Follow Guided Setup to choose library locations and preferences. **Finish** saves your choices without starting a scan. Use copied media while evaluating the alpha.

### Tag Encyclopedia

1. Download **`TagEncyclopedia-a.0.1.8-windows-x64.zip`** from its release page.
2. Extract it, open the **`TagEncyclopedia`** folder and run **`TagEncyclopedia.exe`**.
3. Choose **Open Snapshot** to select an approved, compatible Tag snapshot file.

**The reader starts empty.** No Tag Database or snapshot is included, and the optional default Tag Database is not publicly available. This release reads snapshots; it does not edit catalogs or import a Tag Database pack.

Read each package's included guide and release notes. For a newer version, extract the complete package into a separate folder and retain your existing package and data backups.

## Development status

The repository also contains development work that is not included in these public downloads. Suite Manager, connected component workflows, optional Tag Database distribution, and broader training and generation workflows remain under development. Local unreleased changes do not describe the capabilities of the downloadable packages.

Media Organizer and Tag Encyclopedia can be evaluated independently. Manager integration and automatic updates are not available through these releases. Optional AI settings do not provide model execution. No models or supported Linux packages are currently supplied.

## Feedback

Search [existing issues](https://github.com/DigitalSteel/CollectiGen/issues) before reporting a problem. Include the component version, operating system, steps, expected result and actual result. A text-only report is welcome.

Check attachments for private or sensitive information before posting. Report security vulnerabilities privately.

See [Support](SUPPORT.md) and [Privacy](PRIVACY.md).

## Licensing

Original application code and scripts use [AGPL-3.0-or-later](LICENSE).

Original documentation and the original copyrightable selection, organization and additions in compiled System Knowledge use [CC-BY-SA-4.0](licenses/CC-BY-SA-4.0.txt).

These licenses do not relicense user data, facts, models or incorporated third-party material. See the complete [license scope](LICENSES.md) and [third-party notices](THIRD_PARTY_NOTICES.md).
