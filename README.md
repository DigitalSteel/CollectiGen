# CollectiGen

CollectiGen is a developing suite for collecting, identifying and organizing media, preserving metadata and provenance, and preparing training data. Its longer-term direction includes connected training and image-generation workflows, with ordinary work primarily performed locally.

The project is in alpha. Individual Windows components are available below; a complete installable suite is not yet published.

## Downloads

Choose a component from its release page, then download its Windows ZIP under **Assets**. Source-code and source-notices downloads are for development and licensing; they are not the runnable app.

| Component | Current public release | What it does |
| --- | --- | --- |
| **Media Organizer** | [a.0.4.6 for Windows x64](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.media-organizer/v0.4.6-alpha.1) | Organize local images, browse galleries and datasets, edit descriptions and tags, and save JSON/TXT sidecars. |
| **Tag Encyclopedia** | [a.0.2.2 for Windows x64](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.tag-encyclopedia/v0.2.2-alpha.1) | Set up the standalone Reader to browse an enrolled local Tag Database or an exported catalog. |

Both are evaluation prereleases. See [all releases](https://github.com/DigitalSteel/CollectiGen/releases) for downloads, release notes and source information. No Manager or complete-suite download is currently available.

## Download and run

Both applications bundle Python and Qt. Install **Microsoft Visual C++ v14 Redistributable x64, version 14.44.35211.0 or newer**, if needed. Extract the complete ZIP into a new folder and keep its contents together.

### Media Organizer

1. Download **`MediaOrganizer-a.0.4.6-windows-x64.zip`** from its release page.
2. Extract it, then run **`Start_MediaOrganizer.bat`** directly in the extraction folder. The launcher is at the ZIP root.
3. Follow Guided Setup to choose library locations and preferences. **Finish** saves your choices without starting a scan. Use copied media while evaluating the alpha.

### Tag Encyclopedia

1. Download **`TagEncyclopedia-a.0.2.2-windows-x64.zip`** from its release page.
2. Extract it, then run **`TagEncyclopedia.exe`** in the extraction folder. Keep it with the **`_internal`** folder.
3. Follow setup and review the matching Reader package before confirming installation. First setup needs access to that signed release. The separate **`TagEncyclopediaReader`** ZIP is the component payload, not a second app to unpack and launch directly.
4. Open the Reader and choose an existing enrolled local Tag Database or an exported catalog. Local browsing works without Manager or internet access after setup.

**No Tag Database or snapshot is included.** The optional default Tag Database is not publicly available. Use backups and test copies when evaluating local catalog workflows.

The a.0.2.2 package has been built and checked for artifact integrity. Packaged startup and Close, database loading, Reader handoff, and authenticated installation, update and recovery remain unverified. Update notifications do not establish safe executable replacement. Media Organizer has packaged offscreen startup and Close evidence; native desktop interaction remains unverified. See each release's notes for its exact limits.

Read each package's included guide and release notes. For a newer version, extract the complete package into a separate folder and retain your existing package and data backups.

## Development status

The repository also contains development work that is not included in these public downloads. Suite Manager, connected component workflows, optional Tag Database distribution, and broader training and generation workflows remain under development. Local unreleased changes do not describe the capabilities of the downloadable packages.

Media Organizer and Tag Encyclopedia can be evaluated independently. Manager integration and automatic updates are not available through these releases. Encyclopedia's reviewed Reader setup is separate from Manager-managed installation. Optional AI settings do not provide model execution. No models or supported Linux packages are currently supplied.

## Feedback

Search [existing issues](https://github.com/DigitalSteel/CollectiGen/issues) before reporting a problem. Include the component version, operating system, steps, expected result and actual result. A text-only report is welcome.

Check attachments for private or sensitive information before posting. Report security vulnerabilities privately.

See [Support](SUPPORT.md) and [Privacy](PRIVACY.md).

## Licensing

Original application code and scripts use [AGPL-3.0-or-later](LICENSE).

Original documentation and the original copyrightable selection, organization and additions in compiled System Knowledge use [CC-BY-SA-4.0](licenses/CC-BY-SA-4.0.txt).

These licenses do not relicense user data, facts, models or incorporated third-party material. See the complete [license scope](LICENSES.md) and [third-party notices](THIRD_PARTY_NOTICES.md).
