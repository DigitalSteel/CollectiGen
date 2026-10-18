"""Synthetic-only journaled identity enrollment/restore; no native imports."""
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

from suite_core.catalog_identity_candidate import (
    decode_identity, encode_identity, prepare_legacy_enrollment, transition_identity,
)
from suite_core.completed_pack_undo_candidate import SyntheticPackUndoSandbox, _CheckedParticipants


@dataclass(frozen=True)
class CatalogBackup:
    rows: tuple
    default_pins: tuple


@dataclass(frozen=True)
class IdentityOperationReference:
    receipt_relative: str
    intent_sha256: str


def _digest(value):
    return "missing" if value is None else sha256(value).hexdigest()


def _seal(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode()).hexdigest()


class SyntheticIdentityTransactions:
    HEADER = "data/user/catalog_identity.json"

    def __init__(self, sandbox):
        if type(sandbox) is not SyntheticPackUndoSandbox:
            raise ValueError("Use an explicitly created synthetic sandbox")
        self.sandbox, self.root, self.native = sandbox, sandbox.root, sandbox.native
        self.sandbox._marker()

    def _paths(self):
        return tuple(sorted({p.relative_to(self.root).as_posix() for p in self.native._pack_target_paths(self.root)}
                            | {self.HEADER, "data/user/default_knowledge_state.json"}))

    def _read(self, relative):
        path = self.root / relative
        state = self.native._export_path_state(path)
        return None if state is None else self.native._recovery_bytes(path)[0]

    def _defaults(self):
        paths = self.native._pack_authority_paths(self.root)
        return tuple((p.relative_to(self.root).as_posix(), _digest(self._read(p.relative_to(self.root))))
                     for p in paths if p.is_relative_to(self.root / "data/defaults"))

    def _other_pending(self, chosen=None):
        self.sandbox._other_pending(chosen)

    def capture_backup(self):
        with self.sandbox._guard():
            self._other_pending()
            rows = tuple((p, self._read(p)) for p in self._paths())
            decode_identity(dict(rows)[self.HEADER])
            return CatalogBackup(rows, self._defaults())

    def enroll(self, *, decision):
        with self.sandbox._guard():
            self._other_pending()
            raw = self._read(self.HEADER)
            if raw is not None:
                return decode_identity(raw), None
            identity = prepare_legacy_enrollment(None, decision=decision)
            reference = self._begin("enroll", {self.HEADER: encode_identity(identity)})
            return identity, reference

    def restore_backup(self, backup, *, operation):
        with self.sandbox._guard():
            self._other_pending()
            if (type(backup) is not CatalogBackup or type(backup.rows) is not tuple
                    or tuple(p for p, _ in backup.rows) != self._paths()
                    or any(raw is not None and type(raw) is not bytes for _, raw in backup.rows)
                    or backup.default_pins != self._defaults()):
                raise RuntimeError("Backup membership or approved shipped baseline differs")
            desired = dict(backup.rows)
            source = decode_identity(desired[self.HEADER])
            current_raw = self._read(self.HEADER)
            if operation == "full_backup_restore":
                if current_raw is None or decode_identity(current_raw).catalog_id != source.catalog_id:
                    raise RuntimeError("Full restore requires the selected logical catalog")
            elif operation == "independent_copy":
                if current_raw is not None:
                    raise RuntimeError("Independent copy requires an unenrolled synthetic destination")
            else:
                raise RuntimeError("Choose full restore or independent copy")
            identity = transition_identity(source, operation=operation)
            desired[self.HEADER] = encode_identity(identity)
            reference = self._begin(operation, desired)
            return identity, reference

    def _begin(self, action, desired):
        marker = self.sandbox._marker()
        initial = {p: self._read(p) for p in self._paths()}
        defaults = self._defaults()
        changed = {p: raw for p, raw in desired.items() if raw != initial[p]}
        unchanged = {p: _digest(raw) for p, raw in initial.items() if p not in changed}
        directory = self.root / "backups/knowledge-pack-import" / ("synthetic-identity-" + uuid4().hex)
        directory.mkdir(exist_ok=False, parents=True)
        entries = []
        for number, (relative, raw) in enumerate(sorted(changed.items())):
            name = f"target-{number}.json"
            before_name = f"before-{number}.json"
            if raw is not None:
                self.native._atomic_bytes(directory / name, raw)
                if self.native._recovery_bytes(directory / name)[0] != raw:
                    raise RuntimeError("Target staging readback failed")
            if initial[relative] is not None:
                self.native._atomic_bytes(directory / before_name, initial[relative])
                if self.native._recovery_bytes(directory / before_name)[0] != initial[relative]:
                    raise RuntimeError("Before backup readback failed")
            entries.append({"relative": relative, "initial": _digest(initial[relative]),
                            "target": _digest(raw), "payload": name, "backup": before_name})
        if (self.sandbox._marker() != marker or self._defaults() != defaults
                or any(self._read(p) != raw for p, raw in initial.items())):
            raise RuntimeError("Catalog changed before identity intent publication")
        intent = {"schema_version": 4, "operation_kind": "synthetic_identity_transaction",
                  "operation_id": directory.name, "action": action, "marker_sha256": marker,
                  "files": entries, "unchanged": unchanged, "defaults": list(defaults)}
        reference = IdentityOperationReference((directory / "recovery.json").relative_to(self.root).as_posix(), _seal(intent))
        self.native._atomic_json(directory / "recovery.json", {
            "schema_version": 4, "operation_kind": "synthetic_identity_transaction",
            "status": "pending", "intent": intent})
        self._resume(reference)
        return reference

    def select_operation(self, receipt):
        """Explicit selected receipt, never infer a latest pending operation."""
        with self.sandbox._guard():
            path = Path(receipt).absolute()
            manifest, _ = self.native._read_pack_receipt(path, self.root)
            reference = IdentityOperationReference(path.relative_to(self.root).as_posix(), _seal(manifest.get("intent")))
            self._prepare(reference)
            return reference

    def _prepare(self, reference):
        if type(reference) is not IdentityOperationReference:
            raise RuntimeError("An explicit identity operation reference is required")
        relative = Path(reference.receipt_relative)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("Unsafe identity operation reference")
        path = self.root / relative
        manifest, state = self.native._read_pack_receipt(path, self.root)
        intent = manifest.get("intent")
        if (not isinstance(intent, dict) or _seal(intent) != reference.intent_sha256
                or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 4
                or manifest.get("operation_kind") != "synthetic_identity_transaction"
                or type(intent.get("schema_version")) is not int or intent["schema_version"] != 4
                or intent.get("operation_kind") != "synthetic_identity_transaction"
                or intent.get("operation_id") != path.parent.name
                or intent.get("marker_sha256") != self.sandbox._marker()
                or intent.get("action") not in {"enroll", "full_backup_restore", "independent_copy"}
                or manifest["status"] not in {"pending", "restored"}):
            raise RuntimeError("Identity transaction does not match its selected intent")
        self._other_pending(path)
        files, unchanged = intent.get("files"), intent.get("unchanged")
        if not isinstance(files, list) or not files or not isinstance(unchanged, dict):
            raise RuntimeError("Incomplete identity transaction membership")
        names = [e.get("relative") for e in files if isinstance(e, dict)]
        if (len(names) != len(files) or len(set(names)) != len(names)
                or set(names) & set(unchanged) or set(names) | set(unchanged) != set(self._paths())):
            raise RuntimeError("Unsafe or incomplete identity transaction membership")
        def checkpoint():
            if self.sandbox._marker() != intent["marker_sha256"] or list(self._defaults()) != [tuple(p) for p in intent["defaults"]]:
                raise RuntimeError("Synthetic catalog or shipped baseline changed")
            if any(_digest(self._read(p)) != digest for p, digest in unchanged.items()):
                raise RuntimeError("Unchanged catalog participant changed")
        checkpoint()
        prepared = []
        for entry in files:
            for field in ("payload", "backup"):
                name = entry.get(field)
                if type(name) is not str or Path(name).name != name or name in {".", "..", "recovery.json"}:
                    raise RuntimeError("Unsafe staged identity payload")
            for field in ("initial", "target"):
                value = entry.get(field)
                if type(value) is not str or (value != "missing" and
                    (len(value) != 64 or any(c not in "0123456789abcdef" for c in value))):
                    raise RuntimeError("Invalid identity transaction digest")
            target = self.root / entry["relative"]
            current = self.native._export_path_state(target)
            digest = "missing" if current is None else current[-1]
            if digest not in {entry["initial"], entry["target"]}:
                raise RuntimeError("Newer catalog changes block identity recovery")
            payload = None if entry["target"] == "missing" else self.native._recovery_bytes(path.parent / entry["payload"])[0]
            before = None if entry["initial"] == "missing" else self.native._recovery_bytes(path.parent / entry["backup"])[0]
            if _digest(payload) != entry["target"] or _digest(before) != entry["initial"]:
                raise RuntimeError("Identity transaction backup/staging is corrupt")
            if entry["relative"] == self.HEADER:
                decode_identity(payload)
            if manifest["status"] == "restored" and digest != entry["target"]:
                raise RuntimeError("Completed identity operation no longer matches")
            # Canonical restore's 'before' slot is the sealed target of this
            # forward-recovery candidate. Original before bytes remain backed up.
            prepared.append((target, entry["target"], entry["initial"], payload, current))
        return path, manifest, state, _CheckedParticipants(prepared, checkpoint)

    def _resume(self, reference):
        path, manifest, state, prepared = self._prepare(reference)
        if manifest["status"] == "restored":
            return False
        from core.catalog_admission import selected_identity_admission
        with selected_identity_admission(self, reference):
            return self.native._restore_pack_transaction(path, manifest, self.root)

    def resume(self, reference):
        with self.sandbox._guard():
            return self._resume(reference)
