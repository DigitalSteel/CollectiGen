[CmdletBinding()]
param(
    [string]$Destination = "",
    [string]$RuntimeRoot = "",
    [ValidateSet("collectigen-core","organizer-image")]
    [string]$CapabilityProfile = "collectigen-core",
    [switch]$SystemVCRuntime,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$RepositoryRoot = Split-Path -Parent $PSScriptRoot
$PinsPath = Join-Path $PSScriptRoot "runtime-pins.json"
$RuntimeLockPath = Join-Path $RepositoryRoot "requirements-runtime.lock"
if ($CapabilityProfile -eq "organizer-image") {
    $RuntimeLockPath = Join-Path $RepositoryRoot "requirements-organizer-image-runtime.lock"
}
$Pins = Get-Content -LiteralPath $PinsPath -Raw | ConvertFrom-Json
if ($Pins.schema_version -ne 2) { throw "The runtime pin schema is unsupported." }
$PythonVersion = [string]$Pins.python.version
$PythonArchiveSha256 = [string]$Pins.python.sha256
$ExifToolVersion = [string]$Pins.exiftool.version
$ExifToolArchiveSha256 = [string]$Pins.exiftool.sha256
$VersionText = Get-Content -LiteralPath (Join-Path $RepositoryRoot "version.py") -Raw
$VersionMatch = [regex]::Match($VersionText, '(?m)^\s*VERSION\s*=\s*"([^"]+)"\s*$')
if (-not $VersionMatch.Success) { throw "The CollectiGen application version could not be read from version.py." }
$AppVersion = $VersionMatch.Groups[1].Value

function Assert-PlainDirectoryPath([string]$Path) {
    $Current = [System.IO.Path]::GetFullPath($Path)
    while ($Current) {
        if (Test-Path -LiteralPath $Current) {
            $Item = Get-Item -LiteralPath $Current -Force
            if (-not $Item.PSIsContainer -or ($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
                throw "Runtime directory must not contain a redirected or non-directory ancestor: $Current"
            }
        }
        $Parent = Split-Path -Parent $Current
        if ($Parent -eq $Current) { break }
        $Current = $Parent
    }
}

$AllowedRuntimeRoot = [System.IO.Path]::GetFullPath((Join-Path $RepositoryRoot "data\runtime"))
if (-not $RuntimeRoot) { $RuntimeRoot = $AllowedRuntimeRoot }
$RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
$AllowedPrefix = $AllowedRuntimeRoot.TrimEnd('\') + '\'
if ($RuntimeRoot -ne $AllowedRuntimeRoot -and -not $RuntimeRoot.StartsWith($AllowedPrefix,[System.StringComparison]::OrdinalIgnoreCase)) {
    throw "The portable runtime root must remain inside $AllowedRuntimeRoot"
}
if (-not $Destination) { $Destination = Join-Path $RuntimeRoot "python" }
$Destination = [System.IO.Path]::GetFullPath($Destination)
$RuntimePrefix = $RuntimeRoot.TrimEnd('\') + '\'
if (-not $Destination.StartsWith($RuntimePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "The portable runtime destination must remain inside $RuntimeRoot"
}
$ExifToolDestination = Join-Path $RuntimeRoot "exiftool"
$PythonBackup = $Destination + ".previous"
$ExifToolBackup = $ExifToolDestination + ".previous"
foreach ($OwnedDirectory in @($RuntimeRoot,$Destination,$ExifToolDestination,$PythonBackup,$ExifToolBackup)) {
    Assert-PlainDirectoryPath $OwnedDirectory
}
if ($Destination -eq $ExifToolDestination -or $Destination.StartsWith($ExifToolDestination + '\',[System.StringComparison]::OrdinalIgnoreCase) -or $ExifToolDestination.StartsWith($Destination + '\',[System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Python and ExifTool runtime destinations must be disjoint."
}
if (-not (Test-Path -LiteralPath $Destination) -and (Test-Path -LiteralPath $PythonBackup)) {
    Move-Item -LiteralPath $PythonBackup -Destination $Destination
}
if (-not (Test-Path -LiteralPath $ExifToolDestination) -and (Test-Path -LiteralPath $ExifToolBackup)) {
    Move-Item -LiteralPath $ExifToolBackup -Destination $ExifToolDestination
}
if ((Test-Path -LiteralPath $Destination) -or (Test-Path -LiteralPath $ExifToolDestination)) {
    if (-not $Force) { throw "A runtime already exists. Use -Force to replace it after a verified build succeeds." }
}

$BuildRoot = Join-Path $RuntimeRoot (".build-" + [guid]::NewGuid().ToString("N"))
$StagePython = Join-Path $BuildRoot "python"
$StageExifTool = Join-Path $BuildRoot "exiftool"
$DownloadRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("collectigen-runtime-" + [guid]::NewGuid().ToString("N"))
if ($CapabilityProfile -eq "organizer-image") {
    $DownloadRoot = Join-Path $RuntimeRoot (".downloads-" + [guid]::NewGuid().ToString("N"))
}
$HadPython = Test-Path -LiteralPath $Destination
$HadExifTool = Test-Path -LiteralPath $ExifToolDestination
$SwappedPython = $false
$SwappedExifTool = $false
$InstalledPython = $false
$InstalledExifTool = $false

function Assert-FileHash([string]$Path, [string]$Expected, [string]$Label) {
    $Actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($Actual -ne $Expected.ToLowerInvariant()) {
        throw "$Label verification failed. Expected $Expected but received $Actual."
    }
}

function Add-CriticalFile([System.Collections.IDictionary]$Inventory, [string]$Root, [string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "A critical runtime file is missing: $Path" }
    $RootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    $PathFull = [System.IO.Path]::GetFullPath($Path)
    if (-not $PathFull.StartsWith($RootFull,[System.StringComparison]::OrdinalIgnoreCase)) {
        throw "A critical runtime file escaped its build root: $PathFull"
    }
    $Relative = $PathFull.Substring($RootFull.Length).Replace('\','/')
    $Inventory[$Relative] = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Remove-MicrosoftRuntimeCopies([string]$PythonRoot) {
    $Prefixes = @("concrt","msvcp","vcamp","vccorlib","vcomp","vcruntime")
    $Copies = @(Get-ChildItem -LiteralPath $PythonRoot -Recurse -File | Where-Object {
        $Name = $_.Name.ToLowerInvariant()
        $_.Extension -eq ".dll" -and ($Prefixes | Where-Object { $Name.StartsWith($_) })
    })
    if (-not $Copies.Count) { throw "The reviewed runtime contained no Microsoft runtime copies to replace with the system prerequisite." }
    foreach ($Copy in $Copies) { Remove-Item -LiteralPath $Copy.FullName -Force }
    return $Copies.Count
}

New-Item -ItemType Directory -Path $StagePython -Force | Out-Null
New-Item -ItemType Directory -Path $DownloadRoot -Force | Out-Null
try {
    $PythonArchive = Join-Path $DownloadRoot "python-embed.zip"
    $ExifToolArchive = Join-Path $DownloadRoot "exiftool.zip"
    $PipWheel = Join-Path $DownloadRoot ([string]$Pins.pip_wheel.filename)

    Write-Host "Downloading the pinned Python $PythonVersion runtime..."
    Invoke-WebRequest -Uri ([string]$Pins.python.url) -OutFile $PythonArchive
    Assert-FileHash $PythonArchive $PythonArchiveSha256 "Python archive"
    Expand-Archive -LiteralPath $PythonArchive -DestinationPath $StagePython

    $PathFile = Get-ChildItem -LiteralPath $StagePython -Filter "python*._pth" | Select-Object -First 1
    if (-not $PathFile) { throw "The embedded Python path configuration was not found." }
    $PathLines = Get-Content -LiteralPath $PathFile.FullName | Where-Object {
        $_ -ne "#import site" -and $_ -ne "import site" -and $_ -ne "..\..\.."
    }
    $PathLines += "..\..\.."
    $PathLines += "import site"
    Set-Content -LiteralPath $PathFile.FullName -Value $PathLines -Encoding ascii

    Write-Host "Installing the pinned pip wheel and CollectiGen's required interface packages..."
    Invoke-WebRequest -Uri ([string]$Pins.pip_wheel.url) -OutFile $PipWheel
    Assert-FileHash $PipWheel ([string]$Pins.pip_wheel.sha256) "pip wheel"
    $Python = Join-Path $StagePython "python.exe"
    $PipWheelInstaller = "import sys; sys.path.insert(0, sys.argv.pop(1)); from runtime_contract import install_pip_wheel; install_pip_wheel(sys.argv[1], expected_version=sys.argv[2], expected_sha256=sys.argv[3])"
    & $Python -c $PipWheelInstaller $RepositoryRoot $PipWheel ([string]$Pins.pip_wheel.version) ([string]$Pins.pip_wheel.sha256)
    if ($LASTEXITCODE -ne 0) { throw "pip wheel installation failed with exit code $LASTEXITCODE." }
    $InstalledPipVersion = (& $Python -c "import pip; print(pip.__version__)").Trim()
    if ($LASTEXITCODE -ne 0 -or $InstalledPipVersion -ne [string]$Pins.pip_wheel.version) {
        throw "pip validation failed. Expected $($Pins.pip_wheel.version) but received '$InstalledPipVersion'."
    }
    # Do not inherit user/global/site pip sources or installation options.
    $SavedPipEnvironment = @{}
    foreach ($Variable in @(Get-ChildItem Env: | Where-Object { $_.Name -like "PIP_*" })) {
        $SavedPipEnvironment[$Variable.Name] = $Variable.Value
        Remove-Item -LiteralPath ("Env:" + $Variable.Name)
    }
    $env:PIP_CONFIG_FILE = "nul"
    try {
        & $Python -I -m pip --isolated install --disable-pip-version-check --no-cache-dir --require-hashes --only-binary=:all: --no-deps --index-url https://pypi.org/simple --requirement $RuntimeLockPath
        if ($LASTEXITCODE -ne 0) { throw "Required package installation failed with exit code $LASTEXITCODE." }
    }
    finally {
        foreach ($Variable in @(Get-ChildItem Env: | Where-Object { $_.Name -like "PIP_*" })) {
            Remove-Item -LiteralPath ("Env:" + $Variable.Name)
        }
        foreach ($Name in $SavedPipEnvironment.Keys) { Set-Item -LiteralPath ("Env:" + $Name) -Value $SavedPipEnvironment[$Name] }
    }
    & $Python -m pip check
    if ($LASTEXITCODE -ne 0) { throw "The portable runtime contains incompatible packages." }

    Write-Host "Downloading and verifying ExifTool $ExifToolVersion..."
    $Curl = Get-Command "curl.exe" -ErrorAction SilentlyContinue
    if (-not $Curl) { throw "Windows curl.exe is required to follow the official ExifTool download mirrors." }
    & $Curl.Source --location --fail --silent --show-error --output $ExifToolArchive ([string]$Pins.exiftool.url)
    if ($LASTEXITCODE -ne 0) { throw "ExifTool download failed with exit code $LASTEXITCODE." }
    Assert-FileHash $ExifToolArchive $ExifToolArchiveSha256 "ExifTool archive"
    $ExifToolExtracted = Join-Path $DownloadRoot "exiftool-extracted"
    Expand-Archive -LiteralPath $ExifToolArchive -DestinationPath $ExifToolExtracted
    $ExifToolExecutable = Get-ChildItem -LiteralPath $ExifToolExtracted -Recurse -File | Where-Object {
        $_.Name -eq "exiftool(-k).exe" -or $_.Name -eq "exiftool.exe"
    } | Select-Object -First 1
    if (-not $ExifToolExecutable) { throw "The ExifTool archive did not contain its Windows executable." }
    New-Item -ItemType Directory -Path $StageExifTool -Force | Out-Null
    Copy-Item -Path (Join-Path $ExifToolExecutable.Directory.FullName "*") -Destination $StageExifTool -Recurse
    $BundledExifTool = Join-Path $StageExifTool "exiftool.exe"
    if ($ExifToolExecutable.Name -ne "exiftool.exe") {
        Rename-Item -LiteralPath (Join-Path $StageExifTool $ExifToolExecutable.Name) -NewName "exiftool.exe"
    }
    $ReportedExifToolVersion = (& $BundledExifTool -ver).Trim()
    if ($LASTEXITCODE -ne 0 -or $ReportedExifToolVersion -ne $ExifToolVersion) {
        throw "ExifTool validation failed. Expected $ExifToolVersion but received '$ReportedExifToolVersion'."
    }

    $RemovedMicrosoftRuntimeCopies = 0
    if ($SystemVCRuntime) {
        & (Join-Path $PSScriptRoot "Test-WindowsPrerequisites.ps1") -MinimumVersion "14.44.35211.0" -Quiet
        if ($LASTEXITCODE -ne 0) {
            throw "The system Visual C++ runtime prerequisite must be installed before building the public runtime."
        }
        $RemovedMicrosoftRuntimeCopies = Remove-MicrosoftRuntimeCopies $StagePython
    }

    $VersionProbe = "import json, PIL, PySide6, pip; print(json.dumps({'PySide6': PySide6.__version__, 'Pillow': PIL.__version__, 'pip': pip.__version__}))"
    if ($CapabilityProfile -eq "collectigen-core") {
        $VersionProbe = "import json, PIL, PySide6, av, pip; print(json.dumps({'PySide6': PySide6.__version__, 'Pillow': PIL.__version__, 'PyAV': av.__version__, 'pip': pip.__version__}))"
    }
    $ResolvedVersions = (& $Python -c $VersionProbe) | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw "The selected runtime profile packages could not load." }
    $CorePackages = [ordered]@{ PySide6=$ResolvedVersions.PySide6; Pillow=$ResolvedVersions.Pillow }
    if ($CapabilityProfile -eq "collectigen-core") { $CorePackages.PyAV=$ResolvedVersions.PyAV }
    $ResolvedPackagesJson = (& $Python -m pip list --disable-pip-version-check --format=json | Out-String)
    $ResolvedPackages = @()
    foreach ($Package in (ConvertFrom-Json -InputObject $ResolvedPackagesJson)) {
        $ResolvedPackages += [ordered]@{ name=[string]$Package.name; version=[string]$Package.version }
    }
    $Critical = [ordered]@{}
    Add-CriticalFile $Critical $BuildRoot (Join-Path $StagePython "python.exe")
    Add-CriticalFile $Critical $BuildRoot (Join-Path $StagePython "pythonw.exe")
    Add-CriticalFile $Critical $BuildRoot ((Get-ChildItem -LiteralPath $StagePython -Filter "python3*.dll" | Sort-Object Name | Select-Object -Last 1).FullName)
    Add-CriticalFile $Critical $BuildRoot (Join-Path $StagePython "Lib\site-packages\PySide6\QtCore.pyd")
    Add-CriticalFile $Critical $BuildRoot (Join-Path $StagePython "Lib\site-packages\PySide6\QtGui.pyd")
    Add-CriticalFile $Critical $BuildRoot (Join-Path $StagePython "Lib\site-packages\PySide6\QtWidgets.pyd")
    Add-CriticalFile $Critical $BuildRoot ((Get-ChildItem -LiteralPath (Join-Path $StagePython "Lib\site-packages\PIL") -Filter "_imaging*.pyd" | Select-Object -First 1).FullName)
    $VideoPackages = @()
    if ($CapabilityProfile -eq "collectigen-core") { $VideoPackages = @("av", "av.libs") }
    foreach ($VideoPackage in $VideoPackages) {
        $VideoRoot = Join-Path $StagePython "Lib\site-packages\$VideoPackage"
        foreach ($VideoBinary in (Get-ChildItem -LiteralPath $VideoRoot -Recurse -File | Where-Object { $_.Extension -in @(".dll", ".pyd") })) {
            Add-CriticalFile $Critical $BuildRoot $VideoBinary.FullName
        }
    }
    Add-CriticalFile $Critical $BuildRoot $BundledExifTool
    Add-CriticalFile $Critical $BuildRoot (Join-Path $StageExifTool "exiftool_files\perl.exe")
    Add-CriticalFile $Critical $BuildRoot (Join-Path $StageExifTool "exiftool_files\lib\Image\ExifTool.pm")
    $Files = @(Get-ChildItem -LiteralPath $BuildRoot -Recurse -File)

    $Manifest = [ordered]@{
        schema_version = 2
        profile = $CapabilityProfile
        application_version = $AppVersion
        runtime_pins_sha256 = (Get-FileHash -LiteralPath $PinsPath -Algorithm SHA256).Hash.ToLowerInvariant()
        runtime_lock_sha256 = (Get-FileHash -LiteralPath $RuntimeLockPath -Algorithm SHA256).Hash.ToLowerInvariant()
        python_version = $PythonVersion
        pip_version = $ResolvedVersions.pip
        core_packages = $CorePackages
        resolved_packages = $ResolvedPackages
        architecture = [string]$Pins.python.architecture
        python_source = [string]$Pins.python.url
        python_source_sha256 = $PythonArchiveSha256
        pip_wheel_source = [string]$Pins.pip_wheel.url
        pip_wheel_sha256 = [string]$Pins.pip_wheel.sha256
        exiftool_version = $ExifToolVersion
        exiftool_source = [string]$Pins.exiftool.url
        exiftool_source_sha256 = $ExifToolArchiveSha256
        exiftool_checksum_source = [string]$Pins.exiftool.checksum_url
        critical_files = $Critical
        payload_file_count = $Files.Count
        payload_size_bytes = ($Files | Measure-Object Length -Sum).Sum
        system_prerequisites = @(
            if ($SystemVCRuntime) {
                [ordered]@{
                    id = "microsoft-vc-runtime-x64"
                    minimum_version = "14.44.35211.0"
                    source_url = "https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-170"
                    redistributed = $false
                    removed_runtime_copy_count = $RemovedMicrosoftRuntimeCopies
                }
            }
        )
        built_at_utc = [DateTime]::UtcNow.ToString("o")
    }
    $Manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $StagePython "collectigen-runtime.json") -Encoding utf8

    & $Python -c "import PIL, PySide6, sys; print(f'Portable runtime ready: Python {sys.version.split()[0]}, PySide6 {PySide6.__version__}, Pillow {PIL.__version__}')"
    if ($LASTEXITCODE -ne 0) { throw "The portable runtime validation failed." }
    $ManifestValidator = "import sys; sys.path.insert(0, sys.argv.pop(1)); from runtime_contract import load_expected_runtime_contract, validate_runtime; contract=load_expected_runtime_contract(sys.argv[4],sys.argv[5],capability_profile=sys.argv[6]); result=validate_runtime(sys.argv[1],application_version=sys.argv[3],runtime_root=sys.argv[2],expected_contract=contract,capability_profile=sys.argv[6]); print('\n'.join(result.issues)); sys.exit(0 if result.ready else 1)"
    & $Python -I -B -c $ManifestValidator $RepositoryRoot (Join-Path $StagePython "collectigen-runtime.json") $BuildRoot $AppVersion $PinsPath $RuntimeLockPath $CapabilityProfile
    if ($LASTEXITCODE -ne 0) { throw "The portable runtime manifest read-back failed." }

    foreach ($OwnedDirectory in @($RuntimeRoot,$Destination,$ExifToolDestination,$PythonBackup,$ExifToolBackup,$StagePython,$StageExifTool)) {
        Assert-PlainDirectoryPath $OwnedDirectory
    }
    foreach ($Backup in @($PythonBackup,$ExifToolBackup)) {
        if (Test-Path -LiteralPath $Backup) { Assert-PlainDirectoryPath $Backup; Remove-Item -LiteralPath $Backup -Recurse -Force }
    }
    if ($HadPython) { Move-Item -LiteralPath $Destination -Destination $PythonBackup; $SwappedPython=$true }
    if ($HadExifTool) { Move-Item -LiteralPath $ExifToolDestination -Destination $ExifToolBackup; $SwappedExifTool=$true }
    Move-Item -LiteralPath $StagePython -Destination $Destination
    $InstalledPython=$true
    Move-Item -LiteralPath $StageExifTool -Destination $ExifToolDestination
    $InstalledExifTool=$true
    foreach ($Backup in @($PythonBackup,$ExifToolBackup)) {
        if (Test-Path -LiteralPath $Backup) { Assert-PlainDirectoryPath $Backup; Remove-Item -LiteralPath $Backup -Recurse -Force -ErrorAction SilentlyContinue }
    }
    Write-Host "Portable CollectiGen runtime created at $Destination"
    Write-Host "Run tools\Check-CoreDependencyUpdates.ps1 before publishing a release."
}
catch {
    if ($InstalledPython -and (Test-Path -LiteralPath $Destination)) {
        Assert-PlainDirectoryPath $Destination
        Remove-Item -LiteralPath $Destination -Recurse -Force
    }
    if ($InstalledExifTool -and (Test-Path -LiteralPath $ExifToolDestination)) {
        Assert-PlainDirectoryPath $ExifToolDestination
        Remove-Item -LiteralPath $ExifToolDestination -Recurse -Force
    }
    if ($SwappedPython -and (Test-Path -LiteralPath $PythonBackup)) {
        Move-Item -LiteralPath $PythonBackup -Destination $Destination
    }
    if ($SwappedExifTool -and (Test-Path -LiteralPath $ExifToolBackup)) {
        Move-Item -LiteralPath $ExifToolBackup -Destination $ExifToolDestination
    }
    throw
}
finally {
    foreach ($Folder in @($BuildRoot,$DownloadRoot)) {
        if (Test-Path -LiteralPath $Folder) { Assert-PlainDirectoryPath $Folder; Remove-Item -LiteralPath $Folder -Recurse -Force }
    }
}
