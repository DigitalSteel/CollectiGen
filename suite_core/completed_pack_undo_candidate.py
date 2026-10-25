"""Isolated synthetic completed-Pack Undo; no native imports or production wiring."""
from dataclasses import dataclass, asdict
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4


@dataclass(frozen=True)
class SelectedCompletedImport:
    incarnation: str
    marker_sha256: str
    receipt_relative: str
    receipt_sha256: str


class _CheckedParticipants(list):
    def __init__(self, rows, checkpoint):
        super().__init__(rows)
        self.checkpoint = checkpoint

    def __iter__(self):
        for row in super().__iter__():
            self.checkpoint()
            yield row
        self.checkpoint()


class SyntheticPackUndoSandbox:
    """Candidate-only store, admitted by creation in an absent disposable root.

    Inject the actual core.knowledge_pack leaf. This does not enroll existing
    catalogs or provide a production schema migration/host incarnation authority.
    """
    MARKER = "synthetic-pack-undo-incarnation.json"

    @classmethod
    def create(cls, root, *, native):
        root = Path(root).absolute()
        native._check_export_path(root.parent)
        root.mkdir(exist_ok=False)
        incarnation = uuid4().hex
        native._atomic_json(root / cls.MARKER, {
            "schema_version": 1, "synthetic_only": True,
            "root": str(root), "incarnation": incarnation,
        })
        return cls(root, native=native, expected_incarnation=incarnation)

    def __init__(self, root, *, native, expected_incarnation):
        self.root = Path(root).absolute()
        self.native = native
        self.incarnation = expected_incarnation
        self._marker()

    def _marker(self):
        raw, _ = self.native._recovery_bytes(self.root / self.MARKER)
        value = json.loads(raw)
        if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
                or type(value.get("synthetic_only")) is not bool
                or value != {"schema_version": 1, "synthetic_only": True,
                     "root": str(self.root), "incarnation": self.incarnation}):
            raise RuntimeError("Synthetic catalog incarnation mismatch")
        return sha256(raw).hexdigest()

    def _guard(self):
        return self.native.RulePairAdmission(self.root / "data/user").admit()

    def _source(self, selection):
        if (type(selection) is not SelectedCompletedImport
                or selection.incarnation != self.incarnation
                or selection.marker_sha256 != self._marker()):
            raise RuntimeError("Selected import belongs to a different catalog incarnation")
        relative = Path(selection.receipt_relative)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("Unsafe selected import reference")
        path = self.root / relative
        manifest, state = self.native._read_pack_receipt(path, self.root)
        if (state[-1] != selection.receipt_sha256
                or manifest.get("schema_version") not in {2, 5} or manifest["status"] != "complete"):
            raise RuntimeError("Selected completed import changed or is unsupported")
        if manifest.get("schema_version") == 5:
            return self.native._selected_pack_source(self, selection)
        return path, manifest

    def select_import(self, receipt):
        with self._guard():
            marker = self._marker()
            path = Path(receipt).absolute()
            manifest, state = self.native._read_pack_receipt(path, self.root)
            if manifest.get("schema_version") == 5:
                return self.native.select_enrolled_completed_pack(self, path)
            if manifest.get("schema_version") != 2 or manifest["status"] != "complete":
                raise RuntimeError("Select a supported completed import")
            return SelectedCompletedImport(self.incarnation, marker,
                str(path.relative_to(self.root)), state[-1])

    def _other_pending(self, selected=None):
        for path in (self.root / "backups/knowledge-pack-import").glob("*/recovery.json"):
            value, _ = self.native._read_pack_receipt(path, self.root)
            if value["status"] == "pending" and path != selected:
                raise RuntimeError("Another native or inverse operation is pending")

    def undo(self, selection):
        """Initial admission requires all imported after bytes, then seals intent."""
        with self._guard():
            source, original = self._source(selection)
            if original.get("schema_version") == 5:
                return self.native.undo_enrolled_completed_pack(self, selection)
            self._other_pending()
            prepared = self.native._prepare_pack_restore(source, original, self.root)
            for _target, _before, after, _payload, current in prepared:
                if current is None or current[-1] != after:
                    raise RuntimeError("Catalog changed after the selected import; Undo refused")
            self._source(selection)
            inverse = source.parent.parent / ("synthetic-undo-" + uuid4().hex) / "recovery.json"
            inverse.parent.mkdir(exist_ok=False)
            # Exact canonical backup payloads, copied only to the inverse's own
            # receipt zone so canonical prepare/restore requires no new writer.
            for entry, row in zip(original["files"], prepared):
                if entry["existed"]:
                    backup = inverse.parent / entry["backup"]
                    self.native._atomic_bytes(backup, row[3])
                    if self.native._recovery_bytes(backup)[0] != row[3]:
                        raise RuntimeError("Inverse backup readback failed")
            pending = {"schema_version": 3, "operation_kind": "synthetic_pack_undo",
                       "operation_id": inverse.parent.name, "status": "pending",
                       "undo_of": asdict(selection), "files": original["files"]}
            self.native._atomic_json(inverse, pending)
            # No file participant changes before the inverse receipt is durable.
            self._resume(selection, inverse)
            return inverse

    def _resume(self, selection, inverse):
        source, original = self._source(selection)
        if original.get("schema_version") == 5:
            return self.native.recover_enrolled_pack(self, inverse, selection=selection)
        inverse = Path(inverse).absolute()
        durable, state = self.native._read_pack_receipt(inverse, self.root)
        if (type(durable.get("schema_version")) is not int or durable["schema_version"] != 3
                or durable.get("operation_kind") != "synthetic_pack_undo"
                or durable.get("operation_id") != inverse.parent.name
                or durable.get("undo_of") != asdict(selection)
                or durable.get("files") != original["files"]
                or durable["status"] not in {"pending", "restored"}):
            raise RuntimeError("Inverse receipt does not match the selected import")
        self._other_pending(inverse)
        # Schema adaptation is private to this synthetic candidate, after exact
        # inverse admission. Legacy native recovery still holds schema 3 pending.
        prepared = self.native._prepare_pack_restore(inverse, dict(durable, schema_version=2), self.root)
        if durable["status"] == "restored":
            if any((row[4][-1] if row[4] is not None else "missing") != row[1] for row in prepared):
                raise RuntimeError("Completed Undo no longer matches its exact result")
            return False  # Canonical preflight above still rejects newer edits.
        checked = _CheckedParticipants(prepared, lambda: self._source(selection))
        return self.native._restore_pack_transaction(inverse, durable, self.root,
            preflight=(durable, state, checked))

    def recover_inverse(self, selection, inverse):
        """Selected retry only; never scans and adopts a different inverse."""
        with self._guard():
            return self._resume(selection, inverse)
