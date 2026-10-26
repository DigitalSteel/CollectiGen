"""Disposable identity transactions with native primitives and no host startup."""
from dataclasses import asdict
import importlib.abc
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType

root, fixture, scenario = Path(sys.argv[1]), Path(sys.argv[2]).resolve(), sys.argv[3]
sys.path.insert(0, str(root))
tempfile.tempdir = str(fixture)
archives = {Path(p).resolve() for p in sys.path if p.lower().endswith(".zip")}


class NoHost(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(("PySide6", "shiboken6", "qt_app", "core.app_state")):
            raise AssertionError("Host import forbidden")


def local(value):
    return isinstance(value, int) or Path(value).resolve().is_relative_to(fixture)


def audit(event, args):
    if event.startswith("socket.") or event in {"os.system", "subprocess.Popen"}:
        raise AssertionError("External operation forbidden")
    if event == "open" and not isinstance(args[0], int):
        mode, flags = args[1:3]
        writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT))
        if writes and not local(args[0]): raise AssertionError("Nonfixture write")
        if (Path(args[0]).suffix.lower() in {".json", ".zip", ".db", ".sqlite"}
                and not local(args[0]) and Path(args[0]).resolve() not in archives):
            raise AssertionError("Nonfixture data read")
    if event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod"} and not local(args[0]):
        raise AssertionError("Nonfixture mutation")
    if event in {"os.rename", "os.replace"} and not all(local(p) for p in args[:2]):
        raise AssertionError("Nonfixture relocation")


sys.meta_path.insert(0, NoHost())
sys.addaudithook(audit)
core = ModuleType("core")
core.__path__ = [str(root / "core")]
sys.modules["core"] = core
from core import knowledge_pack as native
from suite_core.completed_pack_undo_candidate import SyntheticPackUndoSandbox
from suite_core.catalog_identity_candidate import decode_identity, inverse_catalog_binding, require_inverse_catalog
from suite_core.catalog_identity_transactions_candidate import SyntheticIdentityTransactions, IdentityOperationReference

spec = importlib.util.spec_from_file_location("synthetic_roots", root / "tests/test_knowledge_pack_records.py")
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


def hold(action):
    try: action()
    except (RuntimeError, OSError, ValueError): return
    raise AssertionError("Expected safe hold")


if scenario == "resume":
    saved = helpers._read(fixture / "retry.json")
    sandbox = SyntheticPackUndoSandbox(fixture / "catalog", native=native,
                                      expected_incarnation=saved["sandbox_incarnation"])
    transaction = SyntheticIdentityTransactions(sandbox)
    reference = IdentityOperationReference(**saved["reference"])
    writes = []
    original = native._atomic_bytes
    def observe(path, payload):
        if str(path) in saved["already_targeted"]: writes.append(str(path))
        original(path, payload)
    native._atomic_bytes = observe
    assert transaction.resume(reference) is saved["pending"]
    assert writes == []
    for relative, value in saved["expected"].items():
        assert transaction._read(relative) == (None if value is None else bytes.fromhex(value))
    assert transaction.resume(reference) is False
else:
    sandbox = SyntheticPackUndoSandbox.create(fixture / "catalog", native=native)
    helpers._root(sandbox.root)
    transaction = SyntheticIdentityTransactions(sandbox)
    header = sandbox.root / transaction.HEADER
    tags = sandbox.root / "data/user/tags.json"
    initial = {p: transaction._read(p) for p in transaction._paths()}
    if scenario.startswith("restore") or scenario in {"copy", "newer_retry", "corrupt_stage", "wrong_catalog"}:
        first, _ = transaction.enroll(decision="enroll_selected_legacy_catalog")
        backup = transaction.capture_backup()
        original_identity_binding = inverse_catalog_binding(header.read_bytes())
        payload = helpers._read(tags)
        payload["future_changed"] = "newer content for restore qualification"
        helpers._write(tags, payload)
        extra = sandbox.root / "data/user/relationships.json"
        if dict(backup.rows)["data/user/relationships.json"] is None:
            helpers._write(extra, {"schema_version": 1, "relationships": []})
        action = lambda: transaction.restore_backup(backup, operation="full_backup_restore")
        expected = dict(backup.rows)
    else:
        first = None
        action = lambda: transaction.enroll(decision="enroll_selected_legacy_catalog")
        expected = initial.copy()
    if scenario == "future_header":
        helpers._write(header, {"schema_version": 2, "catalog_id": "unknown future identity"})
        before = header.read_bytes()
        hold(action)
        assert header.read_bytes() == before
        assert not (sandbox.root / "backups/knowledge-pack-import").exists()
    elif scenario == "stale_before_intent":
        original = native._atomic_bytes
        fired = False
        def changed(path, payload):
            global fired
            original(path, payload)
            if not fired and Path(path).name == "target-0.json":
                fired = True
                original(tags, tags.read_bytes() + b"\nnewer catalog bytes")
        native._atomic_bytes = changed
        hold(action)
        native._atomic_bytes = original
        assert fired and not header.exists()
        assert tags.read_bytes().endswith(b"newer catalog bytes")
        assert not list((sandbox.root / "backups/knowledge-pack-import").glob("*/recovery.json"))
    elif scenario == "retain":
        first, reference = action()
        before = header.read_bytes()
        second, reference = action()
        assert first == second and reference is None and header.read_bytes() == before
    elif scenario == "copy":
        other = SyntheticPackUndoSandbox.create(fixture / "copy-catalog", native=native)
        helpers._root(other.root)
        clone = SyntheticIdentityTransactions(other)
        copied, reference = clone.restore_backup(backup, operation="independent_copy")
        assert copied.catalog_id != first.catalog_id and copied.incarnation_id != first.incarnation_id
        for relative, raw in backup.rows:
            if relative != transaction.HEADER: assert clone._read(relative) == raw
        hold(lambda: require_inverse_catalog(clone._read(clone.HEADER), original_identity_binding))
    elif scenario in {"enroll", "restore"}:
        committed, reference = action()
        assert decode_identity(header.read_bytes()) == committed
        if scenario == "restore":
            assert committed.catalog_id == first.catalog_id and committed.incarnation_id != first.incarnation_id
            hold(lambda: require_inverse_catalog(header.read_bytes(), original_identity_binding))
        for relative, raw in expected.items():
            if relative != transaction.HEADER: assert transaction._read(relative) == raw
        assert transaction.resume(reference) is False
    elif scenario in {"enroll_before", "enroll_after", "enroll_terminal", "restore_before", "restore_partial",
                      "restore_terminal", "newer_retry", "corrupt_stage", "wrong_catalog"}:
        original = native._atomic_bytes
        fired = False
        def interrupt(path, payload):
            global fired
            path = Path(path)
            terminal = path.name == "recovery.json" and json.loads(payload).get("status") == "restored"
            before = scenario in {"enroll_before", "restore_before"} and path == header
            after = scenario == "enroll_after" and path == header
            partial = scenario in {"restore_partial", "newer_retry", "corrupt_stage", "wrong_catalog"} and path == tags
            if not fired and (before or partial):
                fired = True
                raise OSError("synthetic before/partial commit interruption")
            original(path, payload)
            if not fired and (after or terminal and scenario.endswith("terminal")):
                fired = True
                raise OSError("synthetic postreplace interruption")
        native._atomic_bytes = interrupt
        try:
            committed, reference = action()
            assert scenario.endswith("terminal")
        except OSError:
            assert not scenario.endswith("terminal")
        native._atomic_bytes = original
        assert fired
        receipt = next((sandbox.root / "backups/knowledge-pack-import").glob("synthetic-identity-*/recovery.json"))
        # Restoration scenarios already have a completed enrollment receipt.
        candidates = list((sandbox.root / "backups/knowledge-pack-import").glob("synthetic-identity-*/recovery.json"))
        receipt = next(p for p in candidates if helpers._read(p)["intent"]["action"] ==
                       ("enroll" if scenario.startswith("enroll") else "full_backup_restore"))
        reference = transaction.select_operation(receipt)
        manifest = helpers._read(receipt)
        sealed = dict(expected)
        for entry in manifest["intent"]["files"]:
            sealed[entry["relative"]] = None if entry["target"] == "missing" else (receipt.parent / entry["payload"]).read_bytes()
        if scenario in {"newer_retry", "corrupt_stage", "wrong_catalog"}:
            if scenario == "newer_retry": tags.write_bytes(tags.read_bytes() + b"\nnewer edit")
            elif scenario == "corrupt_stage":
                entry = next(e for e in manifest["intent"]["files"] if e["relative"] == "data/user/tags.json")
                (receipt.parent / entry["payload"]).write_bytes(b"bad staged bytes")
            else:
                other = SyntheticPackUndoSandbox.create(fixture / "wrong-catalog", native=native)
                wrong = SyntheticIdentityTransactions(other)
                hold(lambda: wrong.resume(reference))
            held = {p: transaction._read(p) for p in transaction._paths()}
            if scenario != "wrong_catalog": hold(lambda: transaction.resume(reference))
            assert {p: transaction._read(p) for p in transaction._paths()} == held
        else:
            if manifest["status"] == "pending":
                hold(lambda: native.recover_interrupted_knowledge_pack_imports(sandbox.root))
                hold(lambda: transaction.enroll(decision="enroll_selected_legacy_catalog"))
            already = [str(sandbox.root / e["relative"]) for e in manifest["intent"]["files"]
                       if transaction._read(e["relative"]) == sealed[e["relative"]]]
            helpers._write(fixture / "retry.json", {"sandbox_incarnation": sandbox.incarnation,
                "reference": asdict(reference), "expected": {p: None if v is None else v.hex() for p, v in sealed.items()},
                "already_targeted": already, "pending": manifest["status"] == "pending"})
    else:
        raise AssertionError("Unknown scenario")
assert "core.app_state" not in sys.modules
assert not any(n.startswith(("PySide6", "qt_app", "shiboken6")) for n in sys.modules)
print(json.dumps({"scenario": scenario, "passed": True}))
