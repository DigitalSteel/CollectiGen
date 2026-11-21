"""Read-only assistant discovery from canonical Manager connection records."""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath
from time import monotonic

from core.backend_connection_settings import BackendConnectionSettings
from core.encyclopedia_connection import ReaderConnectionStore
from core.generation_transport import strict_json
from core.organizer_connection_settings import OrganizerConnectionStore
from core.trainer_environment import TrainerEnvironmentService, TrainerProfile, _read_json
from core.training_setup_resources import RUNTIME_PROFILE
from core.training_setup_selection import (
    TrainingSetupSelectionError,
    TrainingSetupSelectionRecoveryRequired,
    TrainingSetupSelectionService,
)
from core.workflow_connection_settings import WorkflowConnectionStore
from suite_core.discovery.component_payload_policy import (
    ORGANIZER_INTERPRETER_PAYLOAD_POLICY,
    select_payload_policy,
)
from suite_core.discovery.component_staging import guard, verify_directory
from suite_core.installation import ComponentId


ORGANIZER = "collectigen.media-organizer"
TRAIN = "collectigen.train"
GUIDE = "help/assistant/actions.json"
MAX_GUIDE_BYTES = 64 * 1024
ACTION_ID = re.compile(r"[a-z][a-z0-9]*(?:[.-][a-z0-9]+){1,7}\Z")
ACTION_TEXT_FIELDS = ("title", "summary", "prerequisites", "effects", "recovery")
ACTION_LIST_FIELDS = ("inputs", "outputs")
EXAMPLE_FIELDS = ("request", "result")
POWERSHELL_QUOTES = str.maketrans({"'": "''", "‘": "‘‘", "’": "’’"})
EXPORT_NEXT_STEPS = {
    "documented": "Review the collection export guide in Media Organizer.",
    "not_configured": "Review Media Organizer setup in Suite Manager.",
    "missing": "Review the missing Media Organizer package in Suite Manager.",
    "guide_missing": "Check for an Organizer update with a collection export guide in Suite Manager.",
    "guide_invalid": "Review the installed Organizer package and guide in Suite Manager.",
    "guide_incompatible": "Check for compatible Manager and Organizer updates in Suite Manager.",
    "action_not_listed": "Check the installed Organizer guide for the available collection actions.",
    "unknown": "Check the Media Organizer connection in Suite Manager.",
}


def read_guide(selected, component):
    identity = ComponentId(component)
    policy = select_payload_policy(identity, "windows", "x86_64")
    # Organizer also has a stock interpreter-portable package route.
    try:
        profile = verify_directory(selected.directory, selected.inventory_sha256,
                                   identity, selected.version, payload_policy=policy)
    except (OSError, ValueError):
        if component != ORGANIZER:
            raise
        profile = verify_directory(selected.directory, selected.inventory_sha256,
                                   identity, selected.version,
                                   payload_policy=ORGANIZER_INTERPRETER_PAYLOAD_POLICY)
    matches = [row for row in profile["files"]
               if PurePosixPath(row["path"]).parts[-3:] == tuple(GUIDE.split("/"))]
    if not matches:
        return "guide_missing", []
    if len(matches) != 1 or matches[0]["size_bytes"] > MAX_GUIDE_BYTES:
        return "guide_invalid", []
    row = matches[0]
    path = selected.directory / row["path"]
    guard(path)
    with path.open("rb") as stream:
        raw = stream.read(MAX_GUIDE_BYTES + 1)
    if len(raw) != row["size_bytes"] or hashlib.sha256(raw).hexdigest() != row["sha256"]:
        return "guide_invalid", []
    try:
        guide = strict_json(raw)
    except (ValueError, RecursionError):
        return "guide_invalid", []
    if not isinstance(guide, dict):
        return "guide_invalid", []
    if type(guide.get("schema_version")) is not int or guide["schema_version"] != 1:
        return "guide_incompatible", []
    if (guide.get("component_id"), guide.get("software_version")) != (component, selected.version):
        return "guide_invalid", []
    try:
        actions = public_actions(guide.get("actions"))
    except ValueError:
        return "guide_invalid", []
    return "available", actions


def public_actions(actions):
    """Project schema 1 action fields. Future fields remain outside the CLI."""
    if type(actions) is not list or len(actions) > 64:
        raise ValueError("invalid action list")
    results = {}
    for row in actions:
        if type(row) is not dict:
            raise ValueError("invalid action")
        identity = row.get("id")
        if (type(identity) is not str or len(identity) > 80
                or not ACTION_ID.fullmatch(identity) or identity in results):
            raise ValueError("invalid or duplicate action ID")
        action = {"id": identity}
        for field in ACTION_TEXT_FIELDS:
            value = row.get(field)
            if type(value) is not str or not value.strip() or len(value) > 2048:
                raise ValueError("invalid action text")
            action[field] = value
        for field in ACTION_LIST_FIELDS:
            values = row.get(field)
            if type(values) is not list or len(values) > 16 or any(
                    type(value) is not str or not value.strip() or len(value) > 256 for value in values):
                raise ValueError("invalid action parameters")
            action[field] = values
        progress = row.get("support_status")
        action["support_status"] = progress if progress in (
            "qualification_pending", "supported", "unavailable", "planned"
        ) else "unknown"
        action["invocation"] = "application_ui" if row.get("invocation") == "application_ui" else "unknown"
        if is_optional_action_text(row.get("confirmation")):
            action["confirmation"] = row["confirmation"]
        example = row.get("example")
        if type(example) is dict and all(is_optional_action_text(example.get(field)) for field in EXAMPLE_FIELDS):
            action["example"] = {field: example[field] for field in EXAMPLE_FIELDS}
        local_help = f"README.md#{identity}"
        if row.get("local_help") == local_help:
            action["local_help"] = local_help
        results[identity] = action
    return list(results.values())


def is_optional_action_text(value):
    return type(value) is str and bool(value.strip()) and len(value) <= 1024


def describe_action(report, action_id):
    if report["manager"] != "settings_available":
        return {"state": "unavailable", "id": action_id}
    matches = []
    for component, row in report["components"].items():
        matches.extend((component, row, action) for action in row["actions"] if action["id"] == action_id)
    if len(matches) != 1:
        return {"state": "ambiguous" if matches else "not_found", "id": action_id}
    component, row, action = matches[0]
    return {"state": "described", "id": action_id, "component_id": component,
            "component_version": row["version"], "resource_readiness": row["resource_readiness"],
            "action": action}


def _settings_snapshot(settings_path, code_root):
    try:
        if ".." in code_root.parts or not code_root.is_absolute() or ".." in settings_path.parts:
            return None
        guard(code_root)
        if not code_root.is_dir():
            return None
        snapshot = BackendConnectionSettings(settings_path).observe()
    except (OSError, ValueError):
        return None
    return snapshot if snapshot.digest is not None else None


def discover(settings_path, code_root, *, trainer_software_root=None):
    report = {"schema_version": 1, "manager": "unavailable", "components": {}}
    settings_path, code_root = Path(settings_path), Path(code_root)
    snapshot = _settings_snapshot(settings_path, code_root)
    if snapshot is None:
        return report
    report["manager"] = "settings_available"
    stores = {
        ORGANIZER: OrganizerConnectionStore(settings_path),
        "collectigen.tag-encyclopedia": ReaderConnectionStore(settings_path),
        "collectigen.generate": WorkflowConnectionStore(settings_path, "collectigen.generate"),
        TRAIN: WorkflowConnectionStore(settings_path, TRAIN),
    }
    for component, store in stores.items():
        result = {"state": "unknown", "actions": []}
        report["components"][component] = result
        try:
            selected = store.selected(snapshot)
            if selected is None:
                result["state"] = "not_configured"
                continue
            directory = selected.directory
            if (".." in directory.parts or not directory.is_absolute()
                    or not directory.is_relative_to(code_root)):
                continue
            guard(directory)
            if not directory.is_dir():
                result["state"] = "missing"
                continue
            state, actions = read_guide(selected, component)
            result.update(state="package_verified", version=selected.version,
                          guide_state=state, actions=actions, resource_readiness="unknown")
        except (OSError, ValueError):
            continue
    train = report["components"][TRAIN]
    if train["state"] == "package_verified":
        train.update(observe_trainer_setup(settings_path, trainer_software_root))
    try:
        if BackendConnectionSettings(settings_path).observe().digest == snapshot.digest:
            return report
    except (OSError, ValueError):
        pass
    return {"schema_version": 1, "manager": "settings_changed", "components": {}}


def observe_trainer_setup(settings_path, trainer_software_root=None):
    """Observe the selection and optionally recheck explicitly scoped software."""
    result = {"trainer_setup": "unknown", "trainer_software": "unknown"}
    try:
        store = TrainingSetupSelectionService(settings_path.parent / "training-setup.json")
        selected, revision = store.snapshot()
        result["trainer_setup"] = "not_configured" if selected is None else "configured"
        if selected is not None and trainer_software_root is not None:
            result["trainer_software"] = observe_managed_software(selected, trainer_software_root)
        if store.snapshot()[1] != revision:
            return {"trainer_setup": "unknown", "trainer_software": "unknown"}
    except TrainingSetupSelectionRecoveryRequired:
        return {"trainer_setup": "recovery_required", "trainer_software": "unknown"}
    except (OSError, TrainingSetupSelectionError):
        return {"trainer_setup": "unknown", "trainer_software": "unknown"}
    return result


def observe_managed_software(selected, root):
    managed = selected.managed
    root = Path(root)
    if (managed is None or not root.is_absolute() or ".." in root.parts
            or root != Path(managed.root)):
        return "unknown"
    try:
        guard(root)
        generation = root / "generations" / managed.generation_id
        guard(generation)
        generation.stat()
    except FileNotFoundError:
        return "missing"
    except (OSError, ValueError):
        return "unverified"
    try:
        profile = TrainerProfile.from_dict(_read_json(RUNTIME_PROFILE))
        if profile.sha256 != selected.profile_sha256:
            return "unverified"
        deadline = monotonic() + 15
        TrainerEnvironmentService(root, profile).verify_generation(
            managed.generation_id, cancel=lambda: monotonic() >= deadline
        )
    except InterruptedError:
        return "unknown"
    except (OSError, ValueError, RuntimeError):
        return "unverified"
    return "verified"


def explain_training_collection(report):
    """Explain the initial ASSIST-02 journey without guessing live readiness."""
    components = report["components"]
    organizer = components.get(ORGANIZER, {})
    train = components.get(TRAIN, {})
    actions = {action["id"]: action for action in organizer.get("actions", [])}
    export = actions.get("organizer.collection.export")
    export_state = organizer.get("state", "unknown")
    if export_state == "package_verified":
        export_state = organizer.get("guide_state", "unknown")
        if export_state == "available":
            export_state = "documented" if export else "action_not_listed"
    if export_state not in EXPORT_NEXT_STEPS:
        export_state = "unknown"
    training = train.get("state", "unknown")
    setup = train.get("trainer_setup", "unknown")
    software = train.get("trainer_software", "unknown")
    if training in {"missing", "not_configured"}:
        next_step = "Review Train setup in Suite Manager."
    elif setup == "not_configured":
        next_step = "Review Trainer Setup in Suite Manager before attempting a run."
    elif setup == "recovery_required":
        next_step = "Recover the saved Trainer Setup in Suite Manager before attempting a run."
    elif software in {"missing", "unverified"}:
        next_step = "Review the saved trainer software in Suite Manager before attempting a run."
    elif training == "package_verified":
        next_step = "Review the collection in Train and check the trainer and model requirements."
    else:
        next_step = "Check the Train connection in Suite Manager."
    return {
        "collection_create": actions.get("organizer.collection.create"),
        "collection_export": export,
        "collection_export_state": export_state,
        "collection_next_step": EXPORT_NEXT_STEPS[export_state],
        "collection_can_be_prepared_without_train": True,
        "train_package": training,
        "trainer_setup": setup,
        "trainer_software": software,
        "train_execution_readiness": (
            "setup_required" if training in {"missing", "not_configured"}
            or setup == "not_configured" or software == "missing" else "unknown"
        ),
        "next_step": next_step,
    }


def connection_recipe(console, arguments, mcp_python):
    """Render trusted local startup configuration without editing any client."""
    argv = [str(console), *arguments, "discover"]
    powershell = "& " + " ".join("'" + arg.translate(POWERSHELL_QUOTES) + "'" for arg in argv)
    result = {"cli_check": {"argv": argv, "powershell": powershell}, "mcp": {"state": "python_required"}}
    if mcp_python is None:
        return result
    try:
        guard(mcp_python)
        if not mcp_python.is_file():
            raise FileNotFoundError
    except (OSError, ValueError):
        result["mcp"] = {"state": "python_unavailable"}
        return result
    adapter = console.parent / "help/assistant/assistant_mcp.py"
    try:
        guard(adapter)
        if not adapter.is_file():
            raise FileNotFoundError
    except (OSError, ValueError):
        result["mcp"] = {"state": "adapter_unavailable"}
        return result
    startup = {"command": str(mcp_python), "args": ["-I", "-B", str(adapter), "--console", str(console), *arguments]}
    entries = [f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in startup.items()]
    result["mcp"] = {
        "state": "recipe_available",
        "codex_toml": "[mcp_servers.collectigen]\n" + "\n".join(entries) + "\n",
        "hermes_yaml": "mcp_servers:\n  collectigen:\n" + "".join(
            f"    {key}: {json.dumps(value, ensure_ascii=False)}\n" for key, value in startup.items()),
    }
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manager-settings", required=True, type=Path)
    parser.add_argument("--code-root", required=True, type=Path)
    parser.add_argument("--trainer-software-root", type=Path,
                        help="Optionally verify the exact saved managed trainer software root.")
    parser.add_argument("--mcp-python", type=Path,
                        help="For connection-info, select a Python executable with the official MCP SDK installed.")
    parser.add_argument("command", choices=("discover", "explain-training-collection", "describe-action", "connection-info"),
                        nargs="?", default="discover")
    parser.add_argument("action_id", nargs="?")
    args = parser.parse_args(argv)
    if args.command == "describe-action":
        if args.action_id is None or len(args.action_id) > 80 or not ACTION_ID.fullmatch(args.action_id):
            parser.error("describe-action requires one stable action ID.")
    elif args.action_id is not None:
        parser.error("Only describe-action accepts an action ID.")
    if args.mcp_python is not None and args.command != "connection-info":
        parser.error("--mcp-python is only for connection-info.")
    if args.command == "connection-info":
        console = Path(sys.executable)
        if not getattr(sys, "frozen", False) or console.name.casefold() != "collectigenassist.exe":
            parser.error("Run connection-info from the installed CollectiGenAssist.exe.")
        paths = (args.manager_settings, args.code_root, args.trainer_software_root, args.mcp_python)
        if any(not path.is_absolute() or ".." in path.parts for path in paths if path is not None):
            parser.error("Use absolute installation and Python paths without parent traversal.")
        arguments = ["--manager-settings", str(args.manager_settings), "--code-root", str(args.code_root)]
        if args.trainer_software_root is not None:
            arguments.extend(("--trainer-software-root", str(args.trainer_software_root)))
        available = _settings_snapshot(args.manager_settings, args.code_root) is not None
        result = {"schema_version": 1, "manager": "settings_available" if available else "unavailable",
                  **connection_recipe(console, arguments, args.mcp_python)}
        print(json.dumps(result, ensure_ascii=True))
        return 0 if available else 1
    result = discover(args.manager_settings, args.code_root,
                      trainer_software_root=args.trainer_software_root)
    if args.command == "explain-training-collection":
        result["training_collection"] = explain_training_collection(result)
    elif args.command == "describe-action":
        described = describe_action(result, args.action_id)
        result = {"schema_version": result["schema_version"], "manager": result["manager"],
                  "description": described}
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["manager"] == "settings_available" else 1


if __name__ == "__main__":
    raise SystemExit(main())
