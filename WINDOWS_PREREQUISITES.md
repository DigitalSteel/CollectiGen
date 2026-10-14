# Windows prerequisite for Media Organizer a.0.4.5

This portable Windows x64 candidate requires the official **Microsoft Visual C++ v14 Redistributable (x64), 14.44.35211.0 or newer**. Microsoft runtime DLLs and its installer are not part of this download or the project's license grant.

The launcher runs the bundled read-only native x64 prerequisite checker before Python starts. It does not install or repair the prerequisite, download anything, elevate, change script-execution policy or use another Python installation.

Obtain the x64 installer from [Microsoft's official Visual C++ instructions](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-170). Run that Microsoft-signed installer yourself; it may request administrator approval. For an offline computer, transfer the official installer from a trusted connected computer. Do not use an installer from an unknown source. After it completes, start **Start_MediaOrganizer.bat** again.

If the checker is missing, damaged or blocked by Windows policy, the launcher stops with instructions. Ordinary startup does not invoke PowerShell. The optional PowerShell maintenance check remains subject to your device script policy. Follow your administrator/device policy; this launcher does not offer a policy bypass. A successful integrity check does not establish Windows publisher reputation. If startup still fails, keep the visible diagnostic and see [support](SUPPORT.md).
