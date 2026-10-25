"""Parent-observed native loader paths matched to selected software inventories.

No observed file is opened here. External paths and process addresses are never
retained. A loader-path snapshot is not measurement of loaded memory or proof
of complete runtime identity, offline behavior, or training readiness.
"""
from __future__ import annotations

import hashlib
import ntpath
import re

from .training_native_protocol import Barrier, canonical

LIMITATIONS = ["outside_selected_software_unverified", "datafile_and_manual_mappings_unobserved",
               "loaded_memory_unverified", "drivers_and_later_loads_unobserved"]
SCOPE = "owned_parser_launcher_and_console_at_ready"


def sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def path_key(value):
    if type(value) is not str or not value or len(value) >= 32768 or "\0" in value:
        raise ValueError("native_path_invalid")
    if value.startswith("\\\\?\\") and re.match(r"[A-Za-z]:\\", value[4:]):
        value = value[4:]
    if not ntpath.isabs(value) or not ntpath.splitdrive(value)[0]:
        raise ValueError("native_path_not_absolute")
    return ntpath.normcase(ntpath.normpath(value))


class NativeModuleObserver:
    def __init__(self, request, software, *, barrier=None):
        self.request, self.request_hash = request, sha(request)
        self.software = dict(software)
        self.index, self.roots = {}, {}
        if tuple(self.software) != ("environment", "python_base"):
            raise ValueError("native_software_scope")
        for role, inventory in software:
            root = self.roots[role] = path_key(inventory.root)
            for relative, state, digest in inventory.files:
                key = path_key(ntpath.join(root, relative))
                if key in self.index:
                    raise ValueError("native_inventory_overlap")
                self.index[key] = (role, relative, digest)
        self.inventory_hashes = {role: item.fingerprint for role, item in software}
        self.barrier = barrier if barrier is not None else Barrier(request["native_observation"]["mapping"], create=True)
        self.receipt = self.ready = self.ack = None

    def close(self):
        self.barrier.close()

    def poll(self, plan, owner, cancel):
        from .training_process import TrainingProcessError, _write_new
        from .training_process_windows import windows_directory, NativeObservationError
        if self.receipt is not None:
            return
        ready = self.barrier.read(0)
        if ready is None:
            return
        expected = {"schema_version": 1, "phase": "parser_ready", "nonce": self.request["nonce"],
                    "request_sha256": self.request_hash}
        if set(ready) != set(expected) | {"pid", "checks_sha256"} or any(
            type(ready[key]) is not type(value) or ready[key] != value for key, value in expected.items()
        ) or type(ready["pid"]) is not int or not 0 < ready["pid"] <= 0xFFFFFFFF or type(ready["checks_sha256"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", ready["checks_sha256"]):
            raise TrainingProcessError("The native observation barrier is invalid or stale.")
        bindings = {path_key(item.root): item.manifest_sha256 for item in plan.inventories}
        if any(bindings.get(root) != self.inventory_hashes[role] for role, root in self.roots.items()) or self.request_hash not in {item.sha256 for item in plan.inputs}:
            raise TrainingProcessError("Native observation has no exact process input authority.")
        required_ids = frozenset((owner.pid, ready["pid"]))
        expected_ids = owner.process_ids()
        if owner.poll() is not None:
            raise TrainingProcessError("The native parser owner exited before observation.")
        if not required_ids <= expected_ids or len(expected_ids) > len(required_ids) + 1:
            raise TrainingProcessError("The native parser has unexpected process membership.")
        if self.barrier.read(1) is not None:
            raise TrainingProcessError("The native parser acknowledgement already exists.")
        processes = [("parser", ready["pid"])]
        if owner.pid != ready["pid"]:
            processes.append(("launcher", owner.pid))
        processes.extend(("console", pid) for pid in expected_ids - required_ids)
        matched, counts, outside, total = [], {role: 0 for role in self.roots}, 0, 0
        executables = {path_key(plan.command[0]), path_key(ntpath.join(self.roots["python_base"], "python.exe"))}
        for process_role, pid in processes:
            try:
                paths = owner.module_paths(pid, cancel)
            except NativeObservationError as exc:
                raise TrainingProcessError(str(exc)) from None
            if process_role == "console":
                if not paths or path_key(paths[0]) != path_key(ntpath.join(windows_directory(), "System32", "conhost.exe")):
                    raise TrainingProcessError("An unexpected process accompanies the native parser.")
            elif not paths or path_key(paths[0]) not in executables or path_key(paths[0]) not in self.index:
                raise TrainingProcessError("The observed interpreter differs from the selected software.")
            seen = set()
            for path in paths:
                key = path_key(path)
                if key in seen:
                    raise TrainingProcessError("The native module observation repeats a file.")
                seen.add(key)
                total += 1
                if key in self.index:
                    role, relative, digest = self.index[key]
                    matched.append((process_role, role, relative, digest))
                    counts[role] += 1
                elif any(key.startswith(root.rstrip("\\") + "\\") for root in self.roots.values()):
                    raise TrainingProcessError("An observed module is absent from its selected inventory.")
                else:
                    outside += 1  # Do not open, hash, persist or classify external paths.
        if cancel():
            raise InterruptedError("Native observation was cancelled.")
        if owner.poll() is not None or owner.process_ids() != expected_ids or self.barrier.read(0) != ready:
            raise TrainingProcessError("The parser changed during native observation.")
        observation = {"scope": SCOPE, "process_count": len(processes), "module_count": total,
            "console_host_count": len(expected_ids - required_ids),
            "selected_counts": counts, "outside_selected_count": outside,
            "selected_identity_sha256": sha(sorted(matched)), "inventory_sha256": self.inventory_hashes,
            "limitations": LIMITATIONS}
        receipt = {**expected, "process_fingerprint": plan.fingerprint, "ready_sha256": sha(ready),
            "checks_sha256": ready["checks_sha256"], "observation": observation}
        _write_new(plan.destination / "native-observation.json", canonical(receipt))
        ack = {"schema_version": 1, "nonce": ready["nonce"], "request_sha256": self.request_hash,
            "ready_sha256": sha(ready), "process_fingerprint": plan.fingerprint, "observation_sha256": sha(receipt)}
        self.barrier.write(1, ack)
        self.receipt, self.ready, self.ack = receipt, ready, ack

    def finish(self, plan):
        from .training_process import TrainingProcessError, _read
        if self.receipt is None or self.barrier.read(0) != self.ready or self.barrier.read(1) != self.ack or _read(plan.destination / "native-observation.json", 16384) != canonical(self.receipt):
            raise TrainingProcessError("The native observation is missing or changed before completion.")
        return sha(self.receipt)


def verify_observation(plan, request, checks, expected_hash):
    """Validate the parent artifact independently of the child result."""
    import json
    from .training_process import _read
    raw = _read(plan.destination / "native-observation.json", 16384)
    value = json.loads(raw)
    expected = {"schema_version": 1, "phase": "parser_ready", "nonce": request["nonce"],
        "request_sha256": sha(request), "process_fingerprint": plan.fingerprint, "checks_sha256": sha(checks)}
    if canonical(value) != raw or hashlib.sha256(raw).hexdigest() != expected_hash or set(value) != set(expected) | {"ready_sha256", "observation"} or any(type(value[key]) is not type(wanted) or value[key] != wanted for key, wanted in expected.items()) or type(value["ready_sha256"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", value["ready_sha256"]):
        raise ValueError("native_receipt_invalid")
    observed = value["observation"]
    roots = request["runtime_roots"]
    bound = {path_key(item.root): item.manifest_sha256 for item in plan.inventories}
    inventories = {role: bound[path_key(root)] for role, root in roots.items()}
    if type(observed) is not dict or set(observed) != {"scope", "process_count", "console_host_count", "module_count", "selected_counts", "outside_selected_count", "selected_identity_sha256", "inventory_sha256", "limitations"} or observed["scope"] != SCOPE or observed["inventory_sha256"] != inventories or observed["limitations"] != LIMITATIONS:
        raise ValueError("native_observation_scope")
    if any(type(observed[key]) is not int for key in ("process_count", "console_host_count", "module_count", "outside_selected_count")) or observed["console_host_count"] not in {0, 1} or observed["process_count"] - observed["console_host_count"] not in {1, 2} or not 1 <= observed["module_count"] <= 12288 or not 0 <= observed["outside_selected_count"] <= observed["module_count"]:
        raise ValueError("native_observation_counts")
    counts = observed["selected_counts"]
    if type(counts) is not dict or set(counts) != set(roots) or any(type(count) is not int or not 0 <= count <= 12288 for count in counts.values()) or not 0 < sum(counts.values()) == observed["module_count"] - observed["outside_selected_count"] or type(observed["selected_identity_sha256"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", observed["selected_identity_sha256"]):
        raise ValueError("native_observation_identity")
    return observed
