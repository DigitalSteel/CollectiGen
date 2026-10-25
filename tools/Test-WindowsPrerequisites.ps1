[CmdletBinding()]
param(
    [string]$MinimumVersion = "14.44.35211.0",
    [string]$SystemDirectory = "",
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
if (-not $SystemDirectory) { $SystemDirectory = [Environment]::GetFolderPath("System") }
$SystemDirectory = [System.IO.Path]::GetFullPath($SystemDirectory)
$RequiredFiles = @(
    "concrt140.dll",
    "msvcp140.dll",
    "msvcp140_1.dll",
    "msvcp140_2.dll",
    "msvcp140_codecvt_ids.dll",
    "vcamp140.dll",
    "vccorlib140.dll",
    "vcomp140.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll"
)

try { $Minimum = [version]$MinimumVersion }
catch { throw "The minimum Visual C++ runtime version is invalid: $MinimumVersion" }

$Issues = @()
foreach ($Name in $RequiredFiles) {
    $Path = Join-Path $SystemDirectory $Name
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        $Issues += "$Name is missing"
        continue
    }
    $RawVersion = (Get-Item -LiteralPath $Path).VersionInfo.FileVersion
    $VersionMatch = [regex]::Match([string]$RawVersion,'\d+\.\d+\.\d+\.\d+')
    if (-not $VersionMatch.Success -or [version]$VersionMatch.Value -lt $Minimum) {
        $Issues += "$Name is older than $MinimumVersion ($RawVersion)"
    }
}

if ($Issues.Count) {
    if (-not $Quiet) {
        Write-Host "CollectiGen requires the Microsoft Visual C++ v14 Redistributable (x64) $MinimumVersion or newer."
        foreach ($Issue in $Issues) { Write-Host "- $Issue" }
        Write-Host "Official instructions: https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-170"
    }
    exit 1
}

if (-not $Quiet) {
    Write-Host "Microsoft Visual C++ x64 runtime prerequisite is ready ($MinimumVersion or newer)."
}
exit 0
