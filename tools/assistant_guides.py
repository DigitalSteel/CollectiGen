"""Render the ASSIST-01 guide files from one exact package manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


CATALOG = Path(__file__).with_name("assistant_actions.json")
GUIDE_PATH = "help/assistant/actions.json"
MARKDOWN_PATH = "help/assistant/README.md"
ACTION_ID = re.compile(r"[a-z][a-z0-9]*(?:[.-][a-z0-9]+){1,7}\Z")


def load_catalog() -> dict:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def render_guides(manifest: dict, catalog: dict) -> dict[str, str]:
    component = manifest.get("module_id") or manifest.get("app_id")
    version = manifest.get("software_version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("The package manifest must declare a software version.")
    if manifest.get("module_id") and manifest.get("app_id") not in (None, component):
        raise ValueError("Conflicting package component identities.")
    modules = catalog["components"]
    entry = modules.get(component)
    if entry is None:
        raise ValueError(f"No assistant guide for component {component!r}.")
    actions = []
    seen = set()
    for action in entry["actions"]:
        identity = action["id"]
        if type(identity) is not str or len(identity) > 80 or not ACTION_ID.fullmatch(identity) or identity in seen:
            raise ValueError("Invalid or duplicate assistant action ID.")
        seen.add(identity)
        example = action["example"]
        if (type(example) is not dict or set(example) != {"request", "result"}
                or any(type(value) is not str or not value.strip() or len(value) > 1024
                       for value in (action["confirmation"], *example.values()))):
            raise ValueError(f"Invalid confirmation or example for {identity}.")
        actions.append({**action, "local_help": f"README.md#{identity}"})
    guide = {
        "schema_version": catalog["schema_version"],
        "component_id": component,
        "software_version": version,
        **entry,
        "actions": actions,
    }
    if component == "collectigen.suite-manager":
        guide["suite_index"] = {
            identity: {"name": module["name"], "guide_path": GUIDE_PATH}
            for identity, module in modules.items()
        }

    lines = [
        f"# {entry['name']} assistant guide",
        "",
        f"Component version `{version}`. Guide schema `{catalog['schema_version']}`.",
        "",
        "Check current readiness before offering an action. The guide describes the workflow.",
    ]
    if "suite_index" in guide:
        lines.extend(["", "## Suite components", ""])
        lines.extend(
            f"- {module['name']} (`{identity}`). Read `{GUIDE_PATH}` in its installed package."
            for identity, module in modules.items()
        )
    for action in actions:
        lines.extend([
            "", f'<a id="{action["id"]}"></a>', "", f"## {action['title']}", "", f"Action ID `{action['id']}`.",
            "", action["summary"], "", f"Requirements. {action['prerequisites']}",
            "", f"Effects. {action['effects']}", "", f"Approval. {action['confirmation']}",
            "", f"Example request. {action['example']['request']}",
            "", f"Example result. {action['example']['result']}",
            "", f"Recovery. {action['recovery']}",
        ])
    return {
        GUIDE_PATH: json.dumps(guide, indent=2, ensure_ascii=False) + "\n",
        MARKDOWN_PATH: "\n".join(lines) + "\n",
    }


def guide_readme(readme: bytes) -> bytes:
    links = (
        f"\n\n## Assistant guides\n\n"
        f"- [Action guide]({MARKDOWN_PATH})\n"
        f"- [Machine-readable actions]({GUIDE_PATH})\n"
    )
    return readme.rstrip() + links.encode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--package-root", type=Path, required=True)
    args = parser.parse_args()
    if not args.package_root.is_dir():
        parser.error("--package-root must name an existing staging directory.")
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
        catalog = load_catalog()
        guides = render_guides(manifest, catalog)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    for relative, content in guides.items():
        path = args.package_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
