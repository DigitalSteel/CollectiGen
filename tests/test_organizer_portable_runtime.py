from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import runtime_contract as contract

ROOT = Path(__file__).resolve().parents[1]


def test_image_lock_reuses_exact_existing_official_pins():
    full = (ROOT / "requirements-runtime.lock").read_text(encoding="utf-8")
    image = (ROOT / "requirements-organizer-image-runtime.lock").read_text(encoding="utf-8")
    selected = [line for line in full.splitlines() if line.startswith(("Pillow==", "PySide6_Essentials==", "shiboken6=="))]
    assert [line for line in image.splitlines() if line and not line.startswith("#")] == selected
    assert "av==" not in image
    image_contract = contract.load_expected_runtime_contract(
        ROOT / "tools/runtime-pins.json", ROOT / "requirements-organizer-image-runtime.lock",
        capability_profile="organizer-image")
    assert image_contract["python_version"] == "3.13.15"
    assert set(image_contract["core_packages"]) == {"Pillow", "PySide6"}
    assert "PyAV" in contract.load_expected_runtime_contract(
        ROOT / "tools/runtime-pins.json", ROOT / "requirements-runtime.lock")["core_packages"]


def test_profiles_are_explicit_and_full_defaults_preserved(monkeypatch):
    imported = []
    def load(name):
        imported.append(name)
        if name == "av": raise ImportError("synthetic unavailable Video")
        return SimpleNamespace(__version__="synthetic")
    monkeypatch.setattr(contract.importlib, "import_module", load)
    assert not contract.core_import_issues(capability_profile="organizer-image")
    assert "av" not in imported
    assert any("PyAV" in issue for issue in contract.core_import_issues())
    assert contract.runtime_profile_modules()[0] is contract.CORE_IMPORTS
    with pytest.raises(ValueError): contract.runtime_profile_modules("unknown")


def manifest_fixture(tmp_path, monkeypatch, *, profile="organizer-image"):
    versions = {"PySide6": "6.11.2", "Pillow": "12.3.0"}
    def load(name):
        if name == "av": raise ImportError("synthetic unavailable Video")
        return SimpleNamespace(__version__=versions.get(name, versions.get("Pillow", "")))
    monkeypatch.setattr(contract.importlib, "import_module", load)
    binary = tmp_path / "python" / "python.exe"
    binary.parent.mkdir()
    binary.write_bytes(b"synthetic interpreter")
    manifest = {
        "schema_version": 2, "profile": profile, "architecture": "amd64",
        "python_version": platform.python_version(), "exiftool_version": "13.59",
        "core_packages": versions, "resolved_packages": [{"name": "Pillow", "version": "12.3.0"}],
        "critical_files": {"python/python.exe": hashlib.sha256(binary.read_bytes()).hexdigest()},
    }
    path = binary.parent / "collectigen-runtime.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path, manifest


def test_manifest_cannot_select_its_own_admission_profile(tmp_path, monkeypatch):
    path, _ = manifest_fixture(tmp_path, monkeypatch)
    assert contract.validate_runtime(path, application_version="synthetic", runtime_root=tmp_path,
                                     capability_profile="organizer-image").ready
    assert not contract.validate_runtime(path, application_version="synthetic", runtime_root=tmp_path).ready


@pytest.mark.parametrize("defect", ["profile", "av", "schema", "hash"])
def test_image_runtime_refuses_wrong_identity_or_payload(tmp_path, monkeypatch, defect):
    path, manifest = manifest_fixture(tmp_path, monkeypatch)
    if defect == "profile": manifest["profile"] = "collectigen-core"
    elif defect == "av": manifest["resolved_packages"].append({"name": "av", "version": "18.1.0"})
    elif defect == "schema": manifest["schema_version"] = 1
    else: manifest["critical_files"]["python/python.exe"] = "0" * 64
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert not contract.validate_runtime(path, application_version="synthetic", runtime_root=tmp_path,
                                         capability_profile="organizer-image").ready


def _powershell():
    return shutil.which("pwsh") or shutil.which("powershell")


def _synthetic_builder_fixture(tmp_path, *, profile, failure="", repair=False):
    """Execute original stock PS transaction with explicit external-call substitutions."""
    tools = tmp_path / "tools"
    tools.mkdir()
    text = (ROOT / "tools/Build-PortableRuntime.ps1").read_text(encoding="utf-8")
    # Only native execution sites are substituted; acquisition cmdlets are scoped functions.
    text = text.replace("& $Python ", "Invoke-SyntheticPython ").replace("& $BundledExifTool ", "Invoke-SyntheticExifTool ")
    (tools / "Build-PortableRuntime.ps1").write_text(text, encoding="utf-8")
    shutil.copyfile(ROOT / "tools/Repair-PortableRuntime.ps1", tools / "Repair-PortableRuntime.ps1")
    (tools / "Test-WindowsPrerequisites.ps1").write_text('param($MinimumVersion,[switch]$Quiet)\nexit 0\n', encoding="utf-8")
    (tmp_path / "version.py").write_text('VERSION = "0.99.0"\n', encoding="utf-8")
    for name in ("requirements-runtime.lock", "requirements-organizer-image-runtime.lock"):
        shutil.copyfile(ROOT / name, tmp_path / name)
    artifacts = tmp_path / "synthetic-inputs"
    artifacts.mkdir()
    python_zip = artifacts / "python.zip"
    with zipfile.ZipFile(python_zip, "w") as archive:
        for name in ("python.exe", "pythonw.exe", "python313.dll", "vcruntime140.dll"):
            archive.writestr(name, b"synthetic interpreter bytes")
        archive.writestr("python313._pth", "python313.zip\n.\n#import site\n")
        archive.writestr("LICENSE.txt", "synthetic interpreter license")
    exif_zip = artifacts / "exif.zip"
    with zipfile.ZipFile(exif_zip, "w") as archive:
        for name in ("exiftool(-k).exe", "exiftool_files/perl.exe", "exiftool_files/lib/Image/ExifTool.pm", "exiftool_files/LICENSE"):
            archive.writestr(name, b"synthetic ExifTool bytes")
    pip = artifacts / "pip.whl"
    pip.write_bytes(b"synthetic pip archive")
    pins = json.loads((ROOT / "tools/runtime-pins.json").read_text(encoding="utf-8"))
    for key, file in (("python", python_zip), ("exiftool", exif_zip), ("pip_wheel", pip)):
        pins[key]["url"] = str(file)
        pins[key]["sha256"] = hashlib.sha256(file.read_bytes()).hexdigest()
    (tools / "runtime-pins.json").write_text(json.dumps(pins), encoding="utf-8")
    runtime = tmp_path / "data" / "runtime"
    for folder in (runtime / "python", runtime / "exiftool"):
        folder.mkdir(parents=True)
        (folder / "old-marker").write_bytes(b"original base runtime")
    optional = tmp_path / "data" / "optional-components"
    optional.mkdir()
    (optional / "installed-components.json").write_bytes(b"synthetic retained inventory")
    (optional / "retained.bin").write_bytes(b"synthetic retained component")
    harness = r'''
param($Fixture,$Profile,$Failure,[switch]$Repair)
$ErrorActionPreference="Stop"
$global:LASTEXITCODE=0
$env:TEMP=Join-Path $Fixture "synthetic-temp"
$env:TMP=$env:TEMP
New-Item -ItemType Directory -Path $env:TEMP -Force | Out-Null
$script:FailedSwap=$false
function Invoke-WebRequest { param($Uri,$OutFile) Copy-Item -LiteralPath $Uri -Destination $OutFile }
function Get-Command { param($Name,$ErrorAction) if ($Name -eq "curl.exe") { return @{Source="Invoke-SyntheticCurl"} }; Microsoft.PowerShell.Core\Get-Command $Name }
function Invoke-SyntheticCurl { $Arguments=$args; Copy-Item -LiteralPath $Arguments[-1] -Destination $Arguments[5]; $global:LASTEXITCODE=0 }
function Invoke-SyntheticExifTool { $Arguments=$args; $global:LASTEXITCODE=0; "13.59" }
function Move-Item {
    param($LiteralPath,$Destination)
    if ($Failure -eq "swap" -and -not $script:FailedSwap -and $LiteralPath -match '\.build-[^\\]+[\\/]exiftool$') {
        $script:FailedSwap=$true; throw "synthetic late swap failure"
    }
    Microsoft.PowerShell.Management\Move-Item -LiteralPath $LiteralPath -Destination $Destination
}
function Invoke-SyntheticPython {
    $Arguments=$args;
    $global:LASTEXITCODE=0
    $flat=$Arguments -join " "
    if ($flat -match "pip.__version__" -and $flat -notmatch "PySide6") { return "26.2.1" }
    if ($Arguments -contains "install") {
        if ($env:PIP_EXTRA_INDEX_URL -or $env:PIP_FIND_LINKS -or $env:PIP_CONFIG_FILE -ne "nul") { throw "pip configuration isolation failed" }
        if ($Arguments -notcontains "--isolated" -or $Arguments -notcontains "--no-deps" -or $Arguments -notcontains "https://pypi.org/simple") { throw "source policy missing" }
        $lock=$Arguments[[Array]::IndexOf($Arguments,"--requirement")+1]
        $withVideo=(Get-Content -LiteralPath $lock -Raw) -match '(?m)^av=='
        $stage=Get-ChildItem -LiteralPath (Join-Path $Fixture "data/runtime") -Directory -Filter ".build-*" | Select-Object -First 1
        $site=Join-Path $stage.FullName "python/Lib/site-packages"
        $files=@("PySide6/QtCore.pyd","PySide6/QtGui.pyd","PySide6/QtWidgets.pyd","PIL/_imaging.pyd","PySide6/LICENSE.txt","PIL/LICENSE.txt")
        if ($withVideo) { $files+=@("av/_core.pyd","av.libs/codec.dll") }
        foreach ($name in $files) { $path=Join-Path $site $name; New-Item -ItemType Directory -Path (Split-Path -Parent $path) -Force | Out-Null; Set-Content -LiteralPath $path -Value "synthetic wheel payload" }
        return
    }
    if ($Arguments -contains "list") {
        if ($Profile -eq "organizer-image") { return '[{"name":"Pillow","version":"12.3.0"}]' }
        return '[{"name":"Pillow","version":"12.3.0"},{"name":"av","version":"18.1.0"}]'
    }
    if ($flat -match "load_expected_runtime_contract") {
        if ($Failure -eq "validation") { $global:LASTEXITCODE=13 }
        return
    }
    if ($flat -match "import json, PIL") {
        if ($Profile -eq "organizer-image") { return '{"PySide6":"6.11.2","Pillow":"12.3.0","pip":"26.2.1"}' }
        return '{"PySide6":"6.11.2","Pillow":"12.3.0","PyAV":"18.1.0","pip":"26.2.1"}'
    }
}
$env:PIP_EXTRA_INDEX_URL="https://synthetic.invalid/simple"
$env:PIP_FIND_LINKS="synthetic-untrusted"
try {
    if ($Repair) { & (Join-Path $Fixture "tools/Repair-PortableRuntime.ps1") -CapabilityProfile $Profile }
    else { & (Join-Path $Fixture "tools/Build-PortableRuntime.ps1") -Force -CapabilityProfile $Profile }
    if ($Failure) { throw "expected failure did not occur" }
}
catch { if (-not $Failure) { throw }; Set-Content -LiteralPath (Join-Path $Fixture "observed-failure.txt") -Value $_ }
if ($env:PIP_EXTRA_INDEX_URL -ne "https://synthetic.invalid/simple" -or $env:PIP_FIND_LINKS -ne "synthetic-untrusted") { throw "environment restoration failed" }
'''.replace(r"\\Get-Command", r"\Get-Command").replace(r"\\Move-Item", r"\Move-Item")
    driver = tmp_path / "run-synthetic.ps1"
    driver.write_text(harness, encoding="utf-8")
    args = [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
            str(driver), "-Fixture", str(tmp_path), "-Profile", profile]
    if failure: args += ["-Failure", failure]
    if repair: args += ["-Repair"]
    return args, runtime, optional


@pytest.mark.skipif(not _powershell(), reason="Windows PowerShell unavailable")
@pytest.mark.parametrize("profile,repair", [("collectigen-core", False), ("organizer-image", False), ("organizer-image", True)])
def test_actual_stock_powershell_transaction_with_synthetic_external_calls(tmp_path, profile, repair):
    args, runtime, optional = _synthetic_builder_fixture(tmp_path, profile=profile, repair=repair)
    before = {file.name: file.read_bytes() for file in optional.iterdir()}
    result = subprocess.run(args, check=False, capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads((runtime / "python/collectigen-runtime.json").read_text(encoding="utf-8-sig"))
    assert manifest["profile"] == profile
    assert ("PyAV" in manifest["core_packages"]) == (profile == "collectigen-core")
    assert (runtime / "python/Lib/site-packages/av").exists() == (profile == "collectigen-core")
    assert before == {file.name: file.read_bytes() for file in optional.iterdir()}
    assert not list(runtime.glob("*.previous")) and not list(runtime.glob(".build-*"))
    assert (runtime / "python/Lib/site-packages/PIL/LICENSE.txt").exists()


@pytest.mark.skipif(not _powershell(), reason="Windows PowerShell unavailable")
@pytest.mark.parametrize("failure", ["validation", "swap"])
def test_original_builder_failure_preserves_old_runtime_and_optional_store(tmp_path, failure):
    args, runtime, optional = _synthetic_builder_fixture(tmp_path, profile="organizer-image", failure=failure)
    before = {file.name: file.read_bytes() for file in optional.iterdir()}
    result = subprocess.run(args, check=False, capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
    observed = (tmp_path / "observed-failure.txt").read_text(encoding="utf-8-sig")
    assert ("manifest read-back" if failure == "validation" else "late swap failure") in observed
    assert (runtime / "python/old-marker").read_bytes() == b"original base runtime"
    assert (runtime / "exiftool/old-marker").read_bytes() == b"original base runtime"
    assert before == {file.name: file.read_bytes() for file in optional.iterdir()}
    assert not list(runtime.glob("*.previous")) and not list(runtime.glob(".build-*"))
