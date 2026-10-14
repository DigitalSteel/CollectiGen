from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from importlib import metadata
from pathlib import Path


def _json(path: Path) -> dict:
    payload=json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload,dict):
        raise ValueError(f"{path} must contain one JSON object.")
    return payload


def _normal(value: str) -> str:
    return str(value).casefold().replace("_","-").replace(".","-").replace(" ","-")


def _license_files(distribution,package_root: Path) -> list[dict]:
    values={}
    for relative in distribution.files or ():
        text=str(relative).replace("\\","/")
        if not any(token in Path(text).name.casefold() for token in ("license","copying","notice")):
            continue
        try:
            absolute=Path(distribution.locate_file(relative)).resolve()
            record=_binary_record(absolute,package_root)
            values[record["path"]]=record
        except (OSError,ValueError):
            continue
    return [values[path] for path in sorted(values,key=str.casefold)]


def _distribution_record(name: str,version: str,package_root: Path) -> dict:
    distribution=metadata.distribution(name)
    if distribution.version!=version:
        raise ValueError(f"{name} is {distribution.version}, not runtime-manifest version {version}.")
    values=distribution.metadata
    classifiers=[
        item for item in (values.get_all("Classifier") or [])
        if item.startswith("License ::")
    ]
    project_urls={}
    for value in values.get_all("Project-URL") or []:
        label,separator,url=value.partition(",")
        if separator: project_urls[label.strip()]=url.strip()
    return {
        "name":values.get("Name") or name,
        "version":version,
        "license_expression":values.get("License-Expression") or "",
        "license":values.get("License") or "",
        "license_classifiers":classifiers,
        "license_files":_license_files(distribution,package_root),
        "home_page":values.get("Home-page") or "",
        "project_urls":project_urls,
    }


def _binary_record(path: Path,package_root: Path) -> dict:
    return {
        "path":path.resolve().relative_to(package_root.resolve()).as_posix(),
        "size_bytes":path.stat().st_size,
        "sha256":hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _owned_binary_inventory(package_root: Path) -> tuple[list[dict],list[dict]]:
    qt=[]; microsoft=[]
    microsoft_prefixes=("concrt","msvcp","vcamp","vccorlib","vcomp","vcruntime")
    for distribution_name in ("PySide6_Essentials","shiboken6"):
        distribution=metadata.distribution(distribution_name)
        for relative in distribution.files or ():
            absolute=Path(distribution.locate_file(relative)).resolve()
            if not absolute.is_file() or absolute.suffix.casefold() not in {".dll",".exe",".pyd"}:
                continue
            record=_binary_record(absolute,package_root)
            record["distribution"]=distribution.metadata.get("Name") or distribution_name
            record["distribution_version"]=distribution.version
            if relative.hash is not None:
                record["record_hash"]={"algorithm":relative.hash.mode,"value":relative.hash.value}
                if relative.hash.mode=="sha256":
                    actual=base64.urlsafe_b64encode(bytes.fromhex(record["sha256"])).decode("ascii").rstrip("=")
                    if actual!=relative.hash.value:
                        raise ValueError(f"installed file differs from wheel RECORD: {record['path']}")
            target=microsoft if absolute.name.casefold().startswith(microsoft_prefixes) else qt
            target.append(record)
    python_root=package_root/"data/runtime/python"
    for path in sorted(python_root.glob("*.dll"),key=lambda value:value.name.casefold()):
        if path.name.casefold().startswith(microsoft_prefixes):
            record=_binary_record(path,package_root); record["distribution"]="Python"
            microsoft.append(record)
    return (
        sorted(qt,key=lambda value:value["path"].casefold()),
        sorted(microsoft,key=lambda value:value["path"].casefold()),
    )


def _bundled_binary_inventory(package_root: Path) -> tuple[list[dict],list[dict]]:
    python_root=package_root/"data/runtime/python"
    pyside_root=python_root/"Lib/site-packages/PySide6"
    pyside=[]; microsoft=[]
    microsoft_prefixes=("concrt","msvcp","vcamp","vccorlib","vcomp","vcruntime")
    for path in sorted(pyside_root.rglob("*"),key=lambda value:value.as_posix().casefold()):
        if not path.is_file() or path.suffix.casefold() not in {".dll",".exe",".pyd"}: continue
        target=microsoft if path.name.casefold().startswith(microsoft_prefixes) else pyside
        target.append(_binary_record(path,package_root))
    for path in sorted(python_root.glob("*.dll"),key=lambda value:value.name.casefold()):
        if path.name.casefold().startswith(microsoft_prefixes):
            microsoft.append(_binary_record(path,package_root))
    return pyside,microsoft


def build_package_inventory(package_root: Path,contract: dict) -> dict:
    runtime_manifest=_json(package_root/"data/runtime/python/collectigen-runtime.json")
    packages=[]
    for value in runtime_manifest.get("resolved_packages",[]):
        packages.append(_distribution_record(str(value["name"]),str(value["version"]),package_root))
    pyside_binaries,microsoft_binaries=_owned_binary_inventory(package_root)
    appendix_path=package_root/"licenses/Qt-6.11.2-ThirdParty-Attributions.json"
    appendix=_json(appendix_path)
    return {
        "schema_version":2,
        "application_version":runtime_manifest.get("application_version"),
        "python_version":runtime_manifest.get("python_version"),
        "bundled_runtime_contract":contract["bundled_runtime"],
        "system_prerequisite_contract":contract.get("system_prerequisites",[]),
        "system_prerequisites":runtime_manifest.get("system_prerequisites",[]),
        "resolved_python_distributions":sorted(packages,key=lambda item:_normal(item["name"])),
        "pyside_qt_binary_files":pyside_binaries,
        "microsoft_runtime_binary_files":microsoft_binaries,
        "qt_attribution":{
            "path":"licenses/Qt-6.11.2-ThirdParty-Attributions.json",
            "sha256":hashlib.sha256(appendix_path.read_bytes()).hexdigest(),
            "method":appendix.get("scope",{}).get("method"),
            "qt_source_sha256":appendix.get("qt_source",{}).get("sha256"),
            "pyside6_essentials_wheel_sha256":appendix.get("pyside6_essentials_wheel",{}).get("sha256"),
        },
        "qt_attribution_status":"complete_conservative_upstream_superset_with_exact_wheel_owned_binary_inventory",
        "optional_components_bundled":False,
        "optional_models_bundled":False,
    }


def repository_issues(root: Path,contract: dict,*,release: bool) -> tuple[list[str],list[str]]:
    issues=[]; pending=[]
    if contract.get("schema_version")!=1 or contract.get("roadmap_id")!="REL-03":
        issues.append("public component contract schema/roadmap ID is invalid")
    sys.path.insert(0,str(root))
    try:
        from core import MODEL_ACQUISITIONS
    except Exception as exc:
        issues.append(f"model acquisition contract could not load: {exc}")
    else:
        expected={value["model_id"]:value for value in contract.get("optional_models",[])}
        if set(expected)!=set(MODEL_ACQUISITIONS):
            issues.append("model IDs differ between core and public-components.json")
        for model_id,acquisition in MODEL_ACQUISITIONS.items():
            value=expected.get(model_id,{})
            if (
                value.get("revision")!=acquisition.revision
                or value.get("license")!=acquisition.license_name
                or value.get("license_url")!=acquisition.license_url
                or value.get("source_url")!=acquisition.source_url
            ):
                issues.append(f"model revision/license drift: {model_id}")
    requirements=(root/"requirements.txt").read_text(encoding="utf-8")
    runtime_lock_path=root/"requirements-runtime.lock"
    runtime_lock_bytes=runtime_lock_path.read_bytes()
    runtime_lock=runtime_lock_bytes.decode("utf-8")
    dependency_source=(root/"core/dependencies.py").read_text(encoding="utf-8")
    expected_runtime_lock_hash=str(contract.get("release_policy",{}).get("core_runtime_lock_sha256") or "")
    if hashlib.sha256(runtime_lock_bytes).hexdigest()!=expected_runtime_lock_hash:
        issues.append("core runtime lock hash does not match the reviewed component contract")
    for value in contract.get("optional_python_roots",[]):
        requirement=str(value.get("requirement") or "")
        if requirement and requirement not in requirements and requirement not in dependency_source:
            issues.append(f"optional requirement is not implemented: {requirement}")
    for value in contract.get("bundled_runtime",[]):
        normalized=_normal(value.get("name",""))
        if normalized in {"pillow","pyside6-essentials","shiboken6","av"}:
            expected=f"{value['name']}=={value['version']}"
            if expected.casefold() not in runtime_lock.casefold():
                issues.append(f"core runtime lock is missing {expected}")
        if value.get("redistribution_status") == "pending_ffmpeg_codec_attribution":
            pending.append("PyAV video runtime requires complete FFmpeg/codec attribution before redistribution")
    profiles_path=root/"tools/optional-ai-profiles.json"
    try:
        profiles=_json(profiles_path)
    except (OSError,ValueError,json.JSONDecodeError) as exc:
        issues.append(f"optional-AI profile contract is invalid: {exc}")
        profiles={}
    if profiles.get("schema_version")!=1 or profiles.get("roadmap_id")!="REL-03":
        issues.append("optional-AI profile schema/roadmap ID is invalid")
    for profile_id,profile in profiles.get("profiles",{}).items():
        names=set()
        for package in profile.get("packages",[]):
            name=_normal(package.get("name","")); digest=str(package.get("sha256") or "")
            if not name or name in names or not package.get("version") or len(digest)!=64:
                issues.append(f"optional-AI profile has an invalid package record: {profile_id}")
                break
            names.add(name)
        if not profile.get("roots") or not names:
            issues.append(f"optional-AI profile is incomplete: {profile_id}")
    appendix_path=root/"licenses/Qt-6.11.2-ThirdParty-Attributions.json"
    try:
        appendix=_json(appendix_path)
    except (OSError,ValueError,json.JSONDecodeError) as exc:
        issues.append(f"Qt attribution appendix is invalid: {exc}")
        appendix={}
    pyside=next((value for value in contract.get("bundled_runtime",[]) if _normal(value.get("name",""))=="pyside6-essentials"),{})
    scope=appendix.get("scope",{})
    if (
        appendix.get("schema_version")!=1
        or appendix.get("qt_version")!=pyside.get("version")
        or appendix.get("qt_source",{}).get("sha256")!=pyside.get("qt_source_sha256")
        or appendix.get("pyside6_essentials_wheel",{}).get("sha256")!=pyside.get("wheel_sha256")
        or scope.get("method")!="conservative_upstream_superset"
        or scope.get("attribution_file_count")!=147
        or scope.get("attribution_entry_count")!=203
        or scope.get("unique_source_license_count")!=89
    ):
        issues.append("Qt attribution appendix does not match the reviewed Qt/PySide evidence")
    for record in appendix.get("attributions",[]):
        license_file=record.get("license_file")
        if license_file and hashlib.sha256(str(license_file.get("text") or "").encode("utf-8")).hexdigest()!=license_file.get("text_sha256"):
            issues.append(f"Qt attribution license text hash drift: {license_file.get('path')}")
            break
    for license_file in appendix.get("source_licenses",[]):
        if hashlib.sha256(str(license_file.get("text") or "").encode("utf-8")).hexdigest()!=license_file.get("text_sha256"):
            issues.append(f"Qt source license text hash drift: {license_file.get('path')}")
            break
    runtime_pins=_json(root/"tools/runtime-pins.json")
    bundled={_normal(value["name"]):value for value in contract.get("bundled_runtime",[])}
    if bundled.get("python",{}).get("version")!=runtime_pins.get("python",{}).get("version"):
        issues.append("Python version differs between runtime pins and public component contract")
    pip_contract=bundled.get("pip",{})
    pip_pin=runtime_pins.get("pip_wheel",{})
    if pip_contract.get("version")!=pip_pin.get("version"):
        issues.append("pip version differs between runtime pins and public component contract")
    if pip_contract.get("wheel_filename")!=pip_pin.get("filename"):
        issues.append("pip wheel filename differs between runtime pins and public component contract")
    if pip_contract.get("wheel_sha256")!=pip_pin.get("sha256"):
        issues.append("pip wheel hash differs between runtime pins and public component contract")
    if bundled.get("exiftool-windows-package",{}).get("version")!=runtime_pins.get("exiftool",{}).get("version"):
        issues.append("ExifTool version differs between runtime pins and public component contract")
    for value in contract.get("bundled_runtime",[]):
        for relative in value.get("license_evidence",[]):
            if relative.startswith("data/runtime/") or relative=="installed distribution license files":
                continue
            if not (root/relative).is_file():
                issues.append(f"missing repository license evidence: {relative}")
    for relative,expected_hash in contract.get("release_policy",{}).get("repository_license_sha256",{}).items():
        path=root/relative
        if not path.is_file():
            issues.append(f"missing repository license file: {relative}")
        elif hashlib.sha256(path.read_bytes()).hexdigest()!=str(expected_hash).casefold():
            issues.append(f"repository license hash drift: {relative}")
    project=contract.get("project",{})
    for label,value in project.items():
        status=str(value.get("status") or "")
        if status not in {"approved","complete"}:
            pending.append(f"{label}: {status or 'status missing'}")
    if any(value.get("status")=="legal_review_required" for value in contract.get("optional_models",[])):
        pending.append("JoyCaption model acquisition: legal review required")
    for value in contract.get("release_policy",{}).get("pending_release_requirements",[]):
        if value.get("status") not in {"approved","complete"}:
            pending.append(f"{value.get('id')}: {value.get('status') or 'status missing'}")
    if release:
        public_manifest=_json(root/"tools/public-source-manifest.json")
        public_files=set(public_manifest.get("files",[]))
        for relative in contract.get("release_policy",{}).get("required_repository_files",[]):
            if not (root/relative).is_file():
                issues.append(f"public release is missing {relative}")
            if relative not in public_files:
                issues.append(f"public source allowlist is missing {relative}")
        for relative in ("CONTRIBUTING.md","SECURITY.md","SUPPORT.md"):
            path=root/relative
            if path.is_file() and "REL03-RELEASE-BLOCKER" in path.read_text(encoding="utf-8"):
                issues.append(f"public release still contains a pre-public placeholder: {relative}")
        issues.extend(f"unresolved release decision: {value}" for value in pending)
    return issues,pending


def package_issues(package_root: Path,contract: dict,*,release: bool=False) -> list[str]:
    issues=[]
    inventory_path=package_root/"THIRD_PARTY_COMPONENTS.json"
    if not inventory_path.is_file():
        return ["portable package is missing THIRD_PARTY_COMPONENTS.json"]
    try:
        inventory=_json(inventory_path)
        expected=build_package_inventory(package_root,contract)
    except (OSError,ValueError,metadata.PackageNotFoundError) as exc:
        return [f"portable dependency inventory is invalid: {exc}"]
    if inventory!=expected:
        issues.append("portable dependency inventory does not match the bundled runtime")
    for relative in ("data/runtime/python/LICENSE.txt","data/runtime/exiftool/exiftool_files/LICENSE","data/runtime/exiftool/exiftool_files/Licenses_Strawberry_Perl.zip"):
        if not (package_root/relative).is_file():
            issues.append(f"portable package is missing license evidence: {relative}")
    for value in inventory.get("resolved_python_distributions",[]):
        name=_normal(value.get("name",""))
        metadata_license=bool(value.get("license_expression") or value.get("license") or value.get("license_classifiers"))
        license_files=bool(value.get("license_files"))
        if not metadata_license:
            issues.append(f"bundled distribution has no license metadata: {value.get('name')}")
        if name not in {"pyside6-essentials","shiboken6"} and not license_files:
            issues.append(f"bundled distribution has no installed license/notice file: {value.get('name')}")
    if not inventory.get("pyside_qt_binary_files"):
        issues.append("portable package has no exact PySide/Qt binary inventory")
    if inventory.get("microsoft_runtime_binary_files"):
        issues.append("portable package redistributes Microsoft Visual C++ runtime DLL copies")
    prerequisite=next((
        value for value in inventory.get("system_prerequisites",[])
        if value.get("id")=="microsoft-vc-runtime-x64"
    ),{})
    prerequisite_contract=next((
        value for value in contract.get("system_prerequisites",[])
        if value.get("id")=="microsoft-vc-runtime-x64"
    ),{})
    if (
        not prerequisite_contract
        or prerequisite.get("minimum_version")!=prerequisite_contract.get("minimum_version")
        or prerequisite.get("redistributed") is not False
    ):
        issues.append("portable package does not declare the reviewed system Visual C++ runtime prerequisite")
    prerequisite_payloads=[
        value for value in (package_root/"Prerequisites").rglob("*")
        if value.is_file() and value.name.casefold()!="readme.md"
    ]
    if prerequisite_payloads:
        issues.append("portable package contains a prerequisite payload instead of instructions only")
    if inventory.get("qt_attribution_status")!="complete_conservative_upstream_superset_with_exact_wheel_owned_binary_inventory":
        issues.append("portable package does not carry the reviewed Qt attribution method")
    bundled_names={_normal(value.get("name","")) for value in inventory.get("resolved_python_distributions",[])}
    if bundled_names & {"pyside6","pyside6-addons"}:
        issues.append("portable package contains PySide6 Addons or the PySide6 metapackage outside the reviewed Essentials scope")
    if release:
        for relative in contract.get("release_policy",{}).get("required_repository_files",[]):
            if not (package_root/relative).is_file():
                issues.append(f"release package is missing {relative}")
    return issues


def main(argv: list[str] | None=None) -> int:
    parser=argparse.ArgumentParser(description="Audit REL-03 public component and license evidence.")
    parser.add_argument("--root",type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument("--package-root",type=Path)
    parser.add_argument("--write-package-inventory",action="store_true")
    parser.add_argument("--release",action="store_true",help="Fail on every unresolved public-release decision.")
    args=parser.parse_args(argv)
    root=args.root.resolve(); contract=_json(root/"tools/public-components.json")
    issues,pending=repository_issues(root,contract,release=args.release)
    if args.package_root:
        package_root=args.package_root.resolve()
        if args.write_package_inventory:
            output=package_root/"THIRD_PARTY_COMPONENTS.json"
            output.write_text(json.dumps(build_package_inventory(package_root,contract),indent=2,sort_keys=True)+"\n",encoding="utf-8")
        issues.extend(package_issues(package_root,contract,release=args.release))
    if issues:
        print("Public component audit failed:")
        for value in issues: print(f"- {value}")
        return 1
    if pending:
        print("Public component structure verified; public release remains blocked by:")
        for value in pending: print(f"- {value}")
    else:
        print("Public component audit passed with no pending release decisions.")
    return 0


if __name__=="__main__":
    raise SystemExit(main())
