[CmdletBinding()]
param(
    [ValidateSet("collectigen-core","organizer-image")]
    [string]$CapabilityProfile = "collectigen-core",
    [string]$RuntimeRoot = "",
    [string]$Destination = ""
)

$ErrorActionPreference = "Stop"
Write-Host "CollectiGen will download verified core components and replace the private runtime only after validation succeeds."
& (Join-Path $PSScriptRoot "Build-PortableRuntime.ps1") -Force -SystemVCRuntime -CapabilityProfile $CapabilityProfile -RuntimeRoot $RuntimeRoot -Destination $Destination
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
