"""Manager-only durable recovery candidate. No Qt, SDK import or default executor.

Gate adapters must supply authentic signed admission, native exclusive ownership
and startup evidence. A matching DTO is not production qualification.
"""
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from uuid import uuid4

COMPONENT = "collectigen.suite-manager"
MAX_DATA_BYTES = 16 * 1024 * 1024


class RecoveryHold(ValueError):
    """No authority to proceed; retain all evidence for explicit review."""


def require(condition, reason):
    if not condition:
        raise RecoveryHold(reason)


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest(value):
    return sha256(encoded(value)).hexdigest()


def pin(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value), "sha256_required")


def safe_path(path):
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            info = part.lstat()
            require(not stat.S_ISLNK(info.st_mode) and not
                    getattr(info, "st_file_attributes", 0) & 0x400, "link_or_reparse_path")
    return path


def within(path, root):
    return path == root or root in path.parents


def observe(path, *, bounded=False):
    path = safe_path(path)
    if not path.exists():
        return {"exists": False, "size": 0, "sha256": None}
    require(path.is_file(), "regular_file_required")
    require(not bounded or path.stat().st_size <= MAX_DATA_BYTES, "manager_data_bound")
    h = sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            h.update(chunk)
    return {"exists": True, "size": path.stat().st_size, "sha256": h.hexdigest()}


def atomic(path, raw):
    path = safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        require(path.read_bytes() == raw, "atomic_readback_failed")
        # Directory fsync is available on POSIX; Windows qualification must prove
        # its replace/write durability on the actual filesystem and power faults.
        if os.name != "nt":
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def state(value):
    require(isinstance(value, dict) and set(value) == {"exists", "size", "sha256"}, "data_state_schema")
    require(type(value["exists"]) is bool and type(value["size"]) is int and value["size"] >= 0, "data_state")
    if value["exists"]:
        pin(value["sha256"])
        require(value["size"] <= MAX_DATA_BYTES, "manager_data_bound")
    else:
        require(value["size"] == 0 and value["sha256"] is None, "absent_data_state")
    return value


def relative(value):
    require(isinstance(value, str) and "\\" not in value and ":" not in value, "data_path")
    p = PurePosixPath(value)
    require(value == p.as_posix() and not p.is_absolute() and value != "."
            and all(part not in (".", "..") and not part.endswith((" ", "."))
                    and re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in p.parts), "data_path")
    require(all(part.split(".")[0].upper() not in
                {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))} for part in p.parts), "data_path_alias")
    return value


class ManagerRecovery:
    """Single exact transaction; caller retains original approved binding digest.

    deployment: explicit absolute root/app_root/current/packages/update_exe/manifest/data/
    recovery paths plus portable boolean. Dedicated data/recovery directories are
    qualified by ownership gate; shared catalogs/libraries/models never qualify.
    gates(name, journal) -> exact correlated receipt (see CONTRACT.md).
    """
    def __init__(self, deployment, transaction_id, *, gates):
        require(callable(gates), "production_gates_required")
        require(set(deployment) == {"root", "app_root", "current", "packages", "update_exe", "manifest", "data", "recovery", "portable"}, "deployment_schema")
        require(type(deployment["portable"]) is bool, "portable_binding")
        self.paths = {k: safe_path(v) for k, v in deployment.items() if k != "portable"}
        root = self.paths["root"]
        require(all(within(p, root) and p != root for k, p in self.paths.items() if k != "root"), "exact_deployment_root")
        app_root = self.paths["app_root"]
        require(all(within(self.paths[k], app_root) and self.paths[k] != app_root
                    for k in ("current", "packages", "update_exe", "manifest")), "exact_stock_app_root")
        dirs = [self.paths[k] for k in ("current", "packages", "data", "recovery")]
        require(all(not within(a, b) and not within(b, a) for i, a in enumerate(dirs) for b in dirs[i+1:]), "overlapping_authority")
        require(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", transaction_id or ""), "transaction_id")
        self.binding = {**{k: str(v) for k, v in self.paths.items()}, "portable": deployment["portable"]}
        self.id = transaction_id
        self.home = self.paths["recovery"] / transaction_id
        self.journal_path = self.home / "transaction.json"
        self.active_path = self.paths["recovery"] / "active.json"
        self.gates = gates

    @contextmanager
    def owned(self):
        safe_path(self.home).mkdir(parents=True, exist_ok=True)
        lock = self.paths["recovery"] / "owner.lock"
        lock_bytes = encoded({"transaction": self.id, "nonce": uuid4().hex})
        try:
            with lock.open("xb") as stream:
                stream.write(lock_bytes)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError as exc:
            raise RecoveryHold("busy_or_unknown_owner") from exc
        try:
            yield
        finally:
            # Never automatically reclaim a lock left by an abruptly dead host.
            require(safe_path(lock).read_bytes() == lock_bytes, "owner_lock_changed")
            lock.unlink()

    def gate(self, name, journal):
        result = self.gates(name, json.loads(encoded(journal)))
        require(isinstance(result, dict) and result.get("transaction") == self.id
                and result.get("binding_sha256") == digest(journal["binding"])
                and result.get("status") == name, "unqualified_" + name)
        return result

    def load(self):
        require(safe_path(self.journal_path).stat().st_size <= 1024 * 1024, "journal_bound")
        j = json.loads(safe_path(self.journal_path).read_bytes())
        require(j.get("schema") == 1 and j.get("transaction") == self.id
                and j["binding"]["deployment"] == self.binding, "foreign_or_stale_transaction")
        # Adapter validates this binding against the retained original approval,
        # signed admission and data-ownership declaration; JSON is not authority.
        self.gate("transaction_authority", j)
        # Reject legacy/mutated migration journals too, before any data operation.
        require(j["binding"].get("migration") is False and
                all(row["expected_b"] == row["before"] for row in j["binding"]["data"].values()),
                "migration_recovery_unqualified")
        if self.active_path.exists():
            require(json.loads(safe_path(self.active_path).read_bytes()) ==
                    {"transaction": self.id, "binding_sha256": digest(j["binding"])}, "another_active_transaction")
        else:
            require(j["phase"] in ("healthy", "recovery_verified"), "active_transaction_missing")
        return j

    def save(self, j, phase, event, **detail):
        j["phase"] = phase
        j["events"].append({"sequence": len(j["events"]), "event": event, **detail})
        require(len(encoded(j)) <= 1024 * 1024, "journal_bound")
        atomic(self.journal_path, encoded(j))
        require(json.loads(self.journal_path.read_bytes()) == j, "journal_readback")
        return j

    def _owner_record_path(self, ref):
        require(type(ref) is dict and set(ref)=={'kind','transaction','owner','sha256'}
            and ref['transaction']==self.id and type(ref['kind']) is str
            and re.fullmatch('[a-z_]{1,64}',ref['kind']) and type(ref['owner']) is str
            and re.fullmatch('[0-9a-f]{32}',ref['owner']), 'exact_original_owner_record_reference')
        pin(ref['sha256'])
        return safe_path(self.home/'owner-records'/ref['owner']/(ref['kind']+'-'+ref['sha256']+'.json'))

    def commit(self, ref, raw):
        """Existing owner's immutable record backend inside this transaction."""
        require(type(raw) is bytes and len(raw)<=65536 and sha256(raw).hexdigest()==ref['sha256'],
            'immutable_owner_record_bytes')
        path=self._owner_record_path(ref)
        path.parent.mkdir(parents=True,exist_ok=True)
        try:
            with path.open('xb') as stream:
                stream.write(raw);stream.flush();os.fsync(stream.fileno())
        except FileExistsError:
            require(path.read_bytes()==raw,'immutable_owner_record_collision')
        require(path.read_bytes()==raw,'immutable_owner_record_readback')

    def read(self, ref):
        path=self._owner_record_path(ref)
        require(path.stat().st_size<=65536,'immutable_owner_record_bounds')
        raw=path.read_bytes()
        require(sha256(raw).hexdigest()==ref['sha256'],'immutable_owner_record_changed')
        return raw

    def release(self, release):
        require(set(release) == {"version", "channel", "platform", "files"}, "release_schema")
        require(isinstance(release["version"], str) and release["version"], "release_version")
        require(release["channel"] in ("stable", "beta"), "release_channel")
        require(release["platform"] == {"os": "windows", "architecture": "x86_64"}, "platform_unqualified")
        require(set(release["files"]) == {"archive", "manifest", "signature", "trust", "proof", "inventory", "executable"}, "release_pins")
        for spec in release["files"].values():
            require(set(spec) == {"path", "size", "sha256"}, "retained_file_pin")
            pin(spec["sha256"])
            p = safe_path(spec["path"])
            require(within(p, self.paths["recovery"]) and not within(p, self.home), "retained_payload_outside_transaction")
            require(observe(p) == {"exists": True, "size": spec["size"], "sha256": spec["sha256"]}, "retained_payload_missing_or_changed")

    def retained_context(self, j):
        self.release(j["binding"]["prior"])
        self.release(j["binding"]["target"])
        for key in ("healthy_receipt", "repair_entry"):
            spec = j["binding"][key]
            require(observe(spec["path"]) == {"exists": True, "size": spec["size"], "sha256": spec["sha256"]}, key + "_changed")

    def current_code(self, release):
        spec = release["files"]["executable"]
        require(observe(self.paths["current"] / "CollectiGenManager.exe") ==
                {"exists": True, "size": spec["size"], "sha256": spec["sha256"]}, "current_executable_pin_mismatch")

    def data_path(self, name):
        return safe_path(self.paths["data"] / relative(name))

    def check_data(self, j, key):
        for name, row in j["binding"]["data"].items():
            require(observe(self.data_path(name), bounded=True) == row[key], "changed_manager_data:" + name)

    def prepare(self, prior, target, *, healthy_receipt, repair_entry, declared_data, migration_expected=None):
        """Prepare manual schema-neutral repair only; migrations are unsupported."""
        # Reject even empty/equal-state migration declarations before acquiring a
        # lock, creating transaction state or reading any declared user data.
        require(migration_expected is None, "migration_recovery_unqualified")
        with self.owned():
            require(not self.journal_path.exists(), "transaction_already_exists")
            require(not self.active_path.exists(), "another_active_transaction")
            self.release(prior)
            self.release(target)
            require(prior["version"] != target["version"] and prior["platform"] == target["platform"]
                    and prior["channel"] == target["channel"], "release_pair")
            require(isinstance(declared_data, (list, tuple)) and len(declared_data) <= 64, "declared_data_bound")
            names = [relative(n) for n in declared_data]
            require(len({n.casefold() for n in names}) == len(names), "duplicate_data_alias")
            require(all(not within(Path(a), Path(b)) for a in names for b in names if a != b), "overlapping_data")
            require(migration_expected is None or set(migration_expected) == set(names), "migration_closure")
            # Qualify dedicated Manager ownership before opening any data file.
            # No shared/Local Only namespace can be admitted by this candidate.
            self.gate("manager_data_ownership", {"transaction": self.id,
                      "binding": {"deployment": self.binding, "declared_data": names}})
            data = {n: {"before": observe(self.data_path(n), bounded=True)} for n in names}
            for n, row in data.items():
                row["expected_b"] = state(migration_expected[n]) if migration_expected is not None else row["before"]
            for spec in (healthy_receipt, repair_entry):
                require(set(spec) == {"path", "size", "sha256"}, "entry_or_health_pin")
                pin(spec["sha256"])
                require(within(safe_path(spec["path"]), self.paths["recovery"]), "independent_retained_source_required")
                require(observe(spec["path"]) == {"exists": True, "size": spec["size"], "sha256": spec["sha256"]}, "entry_or_health_changed")
            j = {"schema": 1, "transaction": self.id, "binding": {"deployment": self.binding,
                 "prior": prior, "target": target, "healthy_receipt": healthy_receipt,
                 "repair_entry": repair_entry, "migration": migration_expected is not None, "data": data},
                 "phase": "preparing", "events": [], "restore_intents": [], "restored": []}
            self.gate("prepare_admission", j)
            self.gate("exclusive_owner", j)
            atomic(self.active_path, encoded({"transaction": self.id, "binding_sha256": digest(j["binding"])}))
            self.save(j, "preparing", "preparation_started")
            for n, row in data.items():
                if row["before"]["exists"] and j["binding"]["migration"]:
                    raw = self.data_path(n).read_bytes()
                    require(len(raw) <= MAX_DATA_BYTES, "manager_data_bound")
                    atomic(self.home / "preimages" / n, raw)
                    require(observe(self.home / "preimages" / n) == row["before"], "preimage_changed")
            self.check_data(j, "before")
            return self.save(j, "prepared", "pre_apply_readback_verified")

    def authorize_apply(self):
        """Call immediately before normal stock apply under retained exclusivity."""
        with self.owned():
            j = self.load()
            require(j["phase"] == "prepared", "apply_phase")
            self.retained_context(j)
            self.current_code(j["binding"]["prior"])
            self.check_data(j, "before")
            self.gate("prepare_admission", j)
            self.gate("exclusive_owner", j)
            return self.save(j, "launch_ready", "normal_apply_authorized")

    def starting(self, owner):
        with self.owned():
            j = self.load()
            require(j["phase"] == "launch_ready", "startup_phase")
            require(isinstance(owner, dict) and owner, "exact_native_owner_required")
            j["startup_owner"] = owner
            self.current_code(j["binding"]["target"])
            self.gate("target_starting", j)
            self.check_data(j, "expected_b")
            return self.save(j, "awaiting_health", "target_starting", owner=owner)

    def startup_failed(self):
        with self.owned():
            j = self.load()
            require(j["phase"] == "awaiting_health", "failure_phase")
            evidence = self.gate("target_failed_and_closed", j)
            return self.save(j, "startup_failed", "target_startup_failure", evidence=evidence)

    def healthy(self):
        with self.owned():
            j = self.load()
            require(j["phase"] == "awaiting_health", "health_phase")
            self.current_code(j["binding"]["target"])
            evidence = self.gate("target_healthy", j)
            return self.save(j, "healthy", "target_healthy", evidence=evidence)

    def repair(self, *, explicitly_requested, executor):
        """Manual repair only. Executor performs qualified stock exact downgrade.

        No retry after apply_requested: reconcile actual code/ownership first.
        """
        require(explicitly_requested is True and callable(executor), "explicit_repair_required")
        with self.owned():
            j = self.load()
            require(j["phase"] in ("startup_failed", "preserving_b", "restoring_data", "data_restored"), "repair_phase_requires_reconciliation")
            self.retained_context(j)
            self.current_code(j["binding"]["target"])
            self.gate("recovery_admission", j)
            self.gate("exclusive_owner", j)
            migration = j["binding"]["migration"]
            if j["phase"] in ("startup_failed", "preserving_b"):
                self.check_data(j, "expected_b")
                self.save(j, "preserving_b", "manual_repair_requested")
                # Preserve every B-era declared existence state before any restore.
                for n, row in j["binding"]["data"].items():
                    if row["expected_b"]["exists"] and migration:
                        backup = self.home / "b-era" / n
                        if backup.exists():
                            require(observe(backup) == row["expected_b"], "b_backup_changed")
                        else:
                            atomic(backup, self.data_path(n).read_bytes())
                        require(observe(backup) == row["expected_b"], "b_backup_readback")
                self.check_data(j, "expected_b")
                self.save(j, "restoring_data", "b_data_preserved", states={n: r["expected_b"] for n, r in j["binding"]["data"].items()})
            if migration and j["phase"] in ("restoring_data", "data_restored"):
                # Retained B evidence and all A preimages must still read back,
                # including already-restored files on an interrupted retry.
                for n, row in j["binding"]["data"].items():
                    for folder, key in (("b-era", "expected_b"), ("preimages", "before")):
                        require(observe(self.home / folder / n) == row[key], "snapshot_missing_or_changed:" + folder + ":" + n)
            if j["phase"] == "restoring_data":
                for n, row in j["binding"]["data"].items():
                    actual = observe(self.data_path(n), bounded=True)
                    if n in j["restored"]:
                        require(actual == row["before"], "changed_restored_data:" + n)
                        continue
                    if migration:
                        require(actual == row["expected_b"] or (n in j["restore_intents"] and actual == row["before"]), "changed_manager_data:" + n)
                        if row["before"]["exists"]:
                            backup = self.home / "preimages" / n
                            require(observe(backup) == row["before"], "preimage_missing_or_changed")
                        if n not in j["restore_intents"]:
                            j["restore_intents"].append(n)
                            self.save(j, "restoring_data", "restore_intent", path=n)
                        # Recheck immediately before mutation; native gate owns exclusion.
                        require(observe(self.data_path(n), bounded=True) == actual, "stale_data")
                        if actual != row["before"]:
                            if row["before"]["exists"]:
                                atomic(self.data_path(n), backup.read_bytes())
                            else:
                                self.data_path(n).unlink()
                    else:
                        require(actual == row["before"], "changed_manager_data:" + n)
                    require(observe(self.data_path(n), bounded=True) == row["before"], "restore_readback")
                    j["restored"].append(n)
                    self.save(j, "restoring_data", "data_restored_or_preserved", path=n)
                self.save(j, "data_restored", "manager_data_readback_verified")
            self.check_data(j, "before")
            self.release(j["binding"]["prior"])
            self.gate("exclusive_owner", j)
            self.gate("recovery_admission", j)
            request = {"transaction": self.id, "binding_sha256": digest(j["binding"]),
                       "deployment": self.binding, "prior": j["binding"]["prior"],
                       "observed_current_version": j["binding"]["target"]["version"],
                       "allow_version_downgrade": True, "full_only": True, "deltas": [],
                       "required_is_downgrade": True}
            # Must precede executor: an interrupted/uncertain SDK result is held.
            self.save(j, "apply_requested", "stock_exact_downgrade_requested", request=request)
            receipt = executor(request)
            require(isinstance(receipt, dict) and receipt.get("transaction") == self.id
                    and receipt.get("binding_sha256") == request["binding_sha256"]
                    and receipt.get("status") == "apply_requested", "stock_receipt_unqualified")
            return self.save(j, "awaiting_recovery_health", "stock_apply_request_returned", receipt=receipt)

    def verify_recovered(self):
        """Fresh A code/window/close/reopen proof; SDK return never means health."""
        with self.owned():
            j = self.load()
            require(j["phase"] in ("apply_requested", "awaiting_recovery_health"), "recovery_health_phase")
            self.retained_context(j)
            self.current_code(j["binding"]["prior"])
            self.check_data(j, "before")
            evidence = self.gate("prior_reopened_healthy_and_closed", j)
            return self.save(j, "recovery_verified", "manual_recovery_verified", evidence=evidence)


    def retire(self):
        """Release active slot only after durable healthy terminal evidence."""
        with self.owned():
            j = self.load()
            require(j["phase"] in ("healthy", "recovery_verified"), "pending_transaction_cannot_retire")
            self.gate("exclusive_owner", j)
            self.save(j, j["phase"], "transaction_retired")
            if self.active_path.exists():
                require(json.loads(self.active_path.read_bytes()) ==
                        {"transaction": self.id, "binding_sha256": digest(j["binding"])}, "another_active_transaction")
                self.active_path.unlink()
                require(not self.active_path.exists(), "active_retire_readback")
            return j
