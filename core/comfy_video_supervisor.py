"""Standalone owner of one contained Comfy tree; no discovery or HTTP client.

The controller copies this stdlib-only module and the unchanged Windows Job
primitive into its reviewed runtime workspace. A durable terminal witness is
written only after that exact Job reports zero active members. Missing files,
PIDs or Job names are never terminal evidence.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import threading
import time

SCHEMA = "collectigen.comfy-supervisor.v1"
MAX_CONTRACT = 256 * 1024
MAX_LOG = 8 * 1024 * 1024


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def checked_path(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("Supervisor paths must be absolute.")
    for part in (path, *path.parents):
        if not os.path.lexists(part):
            continue
        state = part.lstat()
        if stat.S_ISLNK(state.st_mode) or getattr(state, "st_file_attributes", 0) & 0x400:
            raise ValueError("Redirected supervisor path is held.")
    return path


def read(path, maximum=MAX_CONTRACT):
    path = checked_path(path)
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
        raise ValueError("Supervisor evidence is not a bounded private file.")
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        raw = handle.read(maximum + 1)
        after = os.fstat(handle.fileno())
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
    if (len(raw) > maximum or identity(before) != identity(opened)
            or identity(opened) != identity(after) or identity(after) != identity(path.stat())):
        raise ValueError("Supervisor evidence changed during read.")
    checked_path(path)
    return raw


def publish(path, value):
    """No replacement: interrupted writes remain held, never repaired by guessing."""
    path = checked_path(path)
    raw = encoded(value)
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    if read(path) != raw:
        raise ValueError("Supervisor evidence failed read-back.")


def load_contract(path):
    if Path(path).name != "supervisor-launch.json":
        raise ValueError("Use the exact owned supervisor locator.")
    path = checked_path(path)
    raw = read(path)
    value = json.loads(raw)
    keys = {"schema", "run_id", "nonce", "runtime_fingerprint", "command", "cwd", "environment", "helper_sha256"}
    if (not isinstance(value, dict) or set(value) != keys or value.get("schema") != SCHEMA
            or any(not isinstance(value.get(k), str) or not re.fullmatch(r"[0-9a-f]{32}", value[k]) for k in ("run_id", "nonce"))
            or any(not isinstance(value.get(k), str) or not re.fullmatch(r"[0-9a-f]{64}", value[k]) for k in ("runtime_fingerprint", "helper_sha256"))
            or not isinstance(value.get("command"), list) or not 2 <= len(value["command"]) <= 64
            or any(not isinstance(v, str) or len(v) > 8192 or "\0" in v for v in value["command"])
            or not isinstance(value.get("environment"), dict) or len(value["environment"]) > 256
            or any(not isinstance(k, str) or not isinstance(v, str) or not k or "=" in k or "\0" in k + v
                   or len(k) + len(v) > 32768 for k, v in value["environment"].items())
            or raw != encoded(value)):
        raise ValueError("Unsupported supervisor launch contract.")
    if not isinstance(value["cwd"], str) or not Path(value["cwd"]).is_absolute() or not Path(value["command"][0]).is_absolute():
        raise ValueError("Supervisor executable and working directory must be absolute.")
    return value


def terminal_evidence(workspace, contract):
    """Validate a retained positive witness; absence remains uncertain."""
    path = checked_path(Path(workspace) / "supervisor-terminal.json")
    value = json.loads(read(path))
    keys = {"schema", "launch_digest", "nonce", "run_id", "supervisor_pid", "child_pid", "tree_empty", "reason"}
    if (not isinstance(value, dict) or set(value) != keys or value.get("schema") != SCHEMA
            or value.get("launch_digest") != digest(contract) or value.get("nonce") != contract["nonce"]
            or value.get("run_id") != contract["run_id"] or value.get("tree_empty") is not True
            or type(value.get("supervisor_pid")) is not int or value["supervisor_pid"] <= 0
            or type(value.get("child_pid")) is not int or value["child_pid"] < 0
            or value["child_pid"] == 0 and value.get("reason") != "not_started"
            or value.get("reason") not in {"exited", "stopped", "deadline", "failed", "not_started"}):
        raise ValueError("Contained-tree terminal evidence is unsupported or changed.")
    started = json.loads(read(Path(workspace) / "supervisor-started.json"))
    expected = {k: value[k] for k in ("schema", "launch_digest", "nonce", "run_id", "supervisor_pid", "child_pid")}
    if started != expected:
        raise ValueError("Terminal evidence differs from the original contained launch.")
    return value


def supervise(contract_path, *, expected_digest=None, backend=None, deadline_seconds=None, poll_interval=.05):
    """Own one fresh child; deadline is used by bounded synthetic qualification."""
    contract_path = checked_path(contract_path)
    contract = load_contract(contract_path)
    if (expected_digest is None and backend is None) or (expected_digest is not None and digest(contract) != expected_digest):
        raise ValueError("Supervisor launch differs from the controller's exact command-line identity.")
    # Only inspect command-owned paths after the immutable caller digest check.
    checked_path(contract["cwd"])
    checked_path(contract["command"][0])
    workspace = contract_path.parent
    if deadline_seconds is not None and (type(deadline_seconds) not in {int, float} or not 0 < deadline_seconds <= 90):
        raise ValueError("Synthetic supervisor deadline must be at most 90 seconds.")
    if any(os.path.lexists(workspace / name) for name in ("supervisor-started.json", "supervisor-terminal.json")):
        raise ValueError("This supervisor attempt already has evidence; never relaunch it.")
    if backend is None:
        helper = workspace / "windows-owner.py"
        if hashlib.sha256(read(helper)).hexdigest() != contract["helper_sha256"]:
            raise ValueError("Contained process helper changed before launch.")
        spec = importlib.util.spec_from_file_location("collectigen_owned_windows", helper)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        backend = module.WindowsTrainingJob
    # An exclusive lifetime lock prevents replay before a started receipt exists.
    lock = checked_path(workspace / "supervisor.claim")
    with lock.open("xb") as claim:
        claim.write(encoded({"launch_digest": digest(contract)})); claim.flush(); os.fsync(claim.fileno())
    owner = backend("Local\\CollectiGen.Comfy." + contract["run_id"])
    reason, started, reader = "failed", None, None
    try:
        early_stop = workspace / "supervisor-stop.json"
        if os.path.lexists(early_stop):
            if json.loads(read(early_stop)) != {"action": "stop", "nonce": contract["nonce"], "launch_digest": digest(contract)}:
                raise ValueError("The stop request does not match this owned tree.")
            reason = "not_started"
            return
        owner.spawn_suspended(tuple(contract["command"]), contract["cwd"], contract["environment"])
        if type(owner.pid) is not int or owner.pid <= 0 or owner.active() < 1:
            raise ValueError("The contained child has no exact process identity.")
        started = {"schema": SCHEMA, "launch_digest": digest(contract), "nonce": contract["nonce"],
                   "run_id": contract["run_id"], "supervisor_pid": os.getpid(), "child_pid": owner.pid}
        publish(workspace / "supervisor-started.json", started)
        def drain():
            with checked_path(workspace / "contained-runtime.log").open("xb") as log:
                retained = 0
                while True:
                    block = owner.stdout.read(65536)
                    if not block: break
                    part = block[:max(0, MAX_LOG - retained)]
                    log.write(part); retained += len(part)
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        owner.resume()
        began = time.monotonic()
        while True:
            if read(contract_path) != encoded(contract):
                raise ValueError("Runtime launch contract changed while running.")
            stop_path = workspace / "supervisor-stop.json"
            if os.path.lexists(stop_path):
                requested = json.loads(read(stop_path))
                if requested != {"action": "stop", "nonce": contract["nonce"], "launch_digest": digest(contract)}:
                    raise ValueError("The stop request does not match this owned tree.")
                reason = "stopped"; break
            if deadline_seconds is not None and time.monotonic() - began >= deadline_seconds:
                reason = "deadline"; break
            if owner.poll() is not None:
                reason = "exited"; break
            time.sleep(poll_interval)
    finally:
        try:
            if owner.active(): owner.terminate()
            until = time.monotonic() + 10
            while owner.active():
                if time.monotonic() >= until:
                    raise RuntimeError("Contained tree remains uncertain; retain all protections.")
                time.sleep(.01)
            # The held Job, not parent exit or name absence, proves this fact.
            if started is None:
                child_pid = owner.pid if type(owner.pid) is int and owner.pid > 0 else 0
                started = {"schema": SCHEMA, "launch_digest": digest(contract), "nonce": contract["nonce"],
                    "run_id": contract["run_id"], "supervisor_pid": os.getpid(), "child_pid": child_pid}
                if not child_pid: reason = "not_started"
                publish(workspace / "supervisor-started.json", started)
            publish(workspace / "supervisor-terminal.json", {**started, "tree_empty": True, "reason": reason})
        finally:
            if reader is not None: reader.join(1)
            owner.close()


if __name__ == "__main__":
    import sys
    if len(sys.argv) not in {3, 4}:
        raise SystemExit("Expected one exact supervisor launch locator.")
    supervise(Path(sys.argv[1]), expected_digest=sys.argv[2], deadline_seconds=float(sys.argv[3]) if len(sys.argv) == 4 else None)
