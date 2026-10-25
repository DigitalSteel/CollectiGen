"""Prepare bounded Organizer source inputs from an existing closure assessment.

Public preparation admits only registered source and omits private maintainer
leaves. Explicit development preparation retains the original assessed closure.
This does not discover a new dependency graph, admit defaults, freeze, launch or
publish. Current bytes are pinned separately from the retained assessment; stale
rows remain visible. A prepared source tree is never a runnable distribution.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import stat


EXPLICIT_STARTUP_LEAVES = (
    "runtime_contract.py", "release_contract.py", "core/startup_profile.py",
    "tools/test_profile_isolation.py", "suite_core/__init__.py",
    "suite_core/encyclopedia/__init__.py", "core/component_release_identity.py",
    "core/application_profile.py",
    "qt_app/startup_recovery.py",
    "suite_core/media_organizer/runtime_startup.py",
    "suite_core/discovery/component_payload_policy.py",
)
PRIVATE_RUNTIME_LEAVES = frozenset({
    "core/development_bug_inbox.py", "core/knowledge_promotion.py",
    "qt_app/knowledge_promotion_dialog.py",
})
SOURCE_PREFIXES = ("core/", "qt_app/", "suite_core/", "tools/")
TOP_LEVEL = {"qt_main.py", "version.py", "runtime_contract.py", "release_contract.py"}


def prepare(root, assessment, output, *, public_runtime=True):
    root, assessment, output = map(lambda p: Path(p).absolute(), (root, assessment, output))
    retained = json.loads(assessment.read_text(encoding="utf-8"))
    # Reuse a reviewed actual-collection receipt without inventing a new graph.
    # Public registration below, not historical row labels, admits current bytes.
    if "pins" not in retained:
        rows=retained.get("rows")
        if not isinstance(rows,list) or not rows:
            raise ValueError("Reviewed source assessment has no finite rows")
        pins={}
        for index,row in enumerate(rows):
            if not isinstance(row,dict) or not isinstance(row.get("path"),str):
                raise ValueError("Invalid reviewed source row")
            digest=row.get("sha256")
            if not isinstance(digest,str) or len(digest)!=64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("Invalid reviewed source pin")
            pins[str(index)]={"path":row["path"],"sha256":digest}
        retained={**retained,"pins":pins}
    paths = {}
    identities = {}
    for module, row in retained["pins"].items():
        name = row["path"]
        path = Path(name)
        if (path.is_absolute() or ".." in path.parts or "\\" in name
                or not name.endswith(".py")
                or not (name.startswith(SOURCE_PREFIXES) or name in TOP_LEVEL)):
            raise ValueError("Unadmitted source path in retained assessment")
        identity = name.casefold()
        if identity in identities and identities[identity] != (name, row["sha256"]):
            raise ValueError("Conflicting retained source path identity")
        identities[identity] = (name, row["sha256"])
        paths[name] = row["sha256"]
    for name in EXPLICIT_STARTUP_LEAVES:
        paths.setdefault(name, None)
    excluded = []
    public_manifest_sha256 = None
    if public_runtime:
        manifest = root / "tools/public-source-manifest.json"
        for item in (manifest, *manifest.parents):
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Redirected public source manifest")
        manifest_bytes = manifest.read_bytes()
        registered = json.loads(manifest_bytes).get("files")
        if not isinstance(registered, list) or not all(isinstance(name, str) for name in registered):
            raise ValueError("Invalid public source file list")
        registered = set(registered)
        if registered & PRIVATE_RUNTIME_LEAVES:
            raise ValueError("Private maintainer leaves cannot be public runtime inputs")
        excluded = sorted(paths.keys() & PRIVATE_RUNTIME_LEAVES)
        for name in excluded:
            del paths[name]
        unregistered = sorted(paths.keys() - registered)
        if unregistered:
            raise ValueError("Unregistered public runtime inputs: " + ", ".join(unregistered))
        public_manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    rows = []
    for name, prior in sorted(paths.items()):
        path = root / name
        for item in (path, *path.parents):
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Redirected source input")
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Source input escapes checkout")
        data = path.read_bytes()
        current = hashlib.sha256(data).hexdigest()
        rows.append(dict(path=name, sha256=current, size=len(data),
                         retained_sha256=prior, changed_since_assessment=prior != current))
    if output.exists():
        raise ValueError("Fresh output directory required")
    if output == root or root.is_relative_to(output):
        raise ValueError("Output would contain checkout")
    for item in output.parents:
        if item.exists():
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Redirected output parent")
    output.mkdir(parents=True)
    try:
        for row in rows:
            target = output / "source" / row["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / row["path"], target)
            if hashlib.sha256(target.read_bytes()).hexdigest() != row["sha256"]:
                raise RuntimeError("Source changed during preparation")
        receipt = dict(schema_version=1, source_rows=rows,
            runtime_scope="public" if public_runtime else "development",
            excluded_private_runtime_leaves=excluded,
            public_manifest_sha256=public_manifest_sha256,
            assessment_sha256=hashlib.sha256(assessment.read_bytes()).hexdigest(),
            retained_dynamic_imports=retained.get("unresolved_dynamic_imports",None),
            external_names=retained.get("external_or_unresolved_import_names",None),
            standalone_distribution_qualified=False, native_activation_qualified=False,
            blockers=["Admit Pillow in retained freezer environment",
                      "Admit exact synthetic defaults and required static assets",
                      "Classify dynamic imports and actual stock freezer graph",
                      "Qualify frozen synthetic startup and recovery"],
            scope="Source-only retained closure plus explicit startup leaves; no user data/assets/models")
        (output / "INPUTS.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        return receipt
    except Exception as exc:
        (output / "INCOMPLETE.json").write_text(json.dumps({"error": str(exc)}), encoding="utf-8")
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assessment", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--development", action="store_true",
                        help="Retain private assessed maintainer leaves; not public distribution inputs")
    args = parser.parse_args()
    receipt = prepare(Path(__file__).resolve().parents[1], args.assessment, args.output,
                      public_runtime=not args.development)
    print(json.dumps({"source_files": len(receipt["source_rows"]),
        "changed_rows": sum(r["changed_since_assessment"] for r in receipt["source_rows"]),
        "blockers": receipt["blockers"], "runnable": False}))


if __name__ == "__main__":
    main()
