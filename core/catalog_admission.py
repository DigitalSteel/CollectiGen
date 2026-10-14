"""Native enrolled admission; selected import recovery retains persisted identity.

The accepted identity codec is reused pending its native-owner placement decision.
No enrollment, registry, discovery, new catalog writer or automatic migration.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
import json
from pathlib import Path

from suite_core.catalog_identity_candidate import inverse_catalog_binding, require_inverse_catalog, decode_identity

ENROLLED_PACK_RECEIPT = 5  # Native identity-bound Pack import receipt; same physical schema.
ENROLLED_INVERSE_RECEIPT = 6  # Synthetic inverse remains unactivated.
HEADER = "data/user/catalog_identity.json"
_ACTIVE = ContextVar("catalog_native_admission_candidate", default=None)


def _receipts(root, native):
    rows = []
    for path in sorted((root / "backups/knowledge-pack-import").glob("*/recovery.json")):
        value, state = native._read_pack_receipt(path, root)
        rows.append((path, value, state[-1]))
    return rows


def _observed(root, native):
    rows = _receipts(root, native)
    state = native._export_path_state(root / HEADER)
    enrolled = state is not None
    for _path, value, _digest in rows:
        intent = value.get("intent")
        enrolled |= value.get("schema_version") in {ENROLLED_PACK_RECEIPT, ENROLLED_INVERSE_RECEIPT}
        enrolled |= "catalog_identity_binding" in value
        if isinstance(intent, dict):
            files = intent.get("files")
            enrolled |= isinstance(files, list) and any(isinstance(row, dict) and row.get("relative") == HEADER for row in files)
            enrolled |= HEADER in intent.get("unchanged", {}) if isinstance(intent.get("unchanged"), dict) else False
    raw = None if state is None else native._recovery_bytes(root / HEADER)[0]
    if enrolled:
        binding = inverse_catalog_binding(raw)  # Missing/malformed header holds.
    else:
        binding = None  # Explicit legacy path; marker alone is never enrollment.
    return binding, rows


def _pins(rows, exclude=None):
    return tuple((str(path), digest) for path, _value, digest in rows if path != exclude)


def _intent_seal(value):
    stable = {k: v for k, v in value.items() if k not in {"status", "completed_at", "restored_at"}}
    return sha256(json.dumps(stable, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class CatalogAdmissionState:
    def __init__(self, root, native):
        self.root, self.native = root, native
        self.binding, rows = _observed(root, native)
        self.pins = _pins(rows)
        self.receipt_epoch = sha256(json.dumps(self.pins, separators=(",", ":")).encode()).hexdigest() if self.binding else ""
        self.owned_receipt = None
        self.owned_seal = None
        self.owned_published = False

    @property
    def enrolled(self):
        return self.binding is not None

    @property
    def catalog_id(self):
        if not self.enrolled:
            return None
        return decode_identity(self.native._recovery_bytes(self.root / HEADER)[0]).publication_catalog_id

    def checkpoint(self, *, ordinary=False):
        if not self.enrolled:
            # Enrollment appearing during a legacy operation must not silently
            # pass into a different authority mode.
            if _observed(self.root, self.native)[0] is not None:
                raise RuntimeError("Catalog was enrolled during a legacy operation")
            return
        if ordinary and self.owned_receipt is not None:
            raise RuntimeError("Native mutation is in progress; ordinary admission held")
        binding, rows = _observed(self.root, self.native)
        if binding != self.binding:
            raise RuntimeError("Catalog header changed during native admission")
        if _pins(rows, self.owned_receipt) != tuple(row for row in self.pins if row[0] != str(self.owned_receipt)):
            raise RuntimeError("Catalog operation history changed during native admission")
        own_seen = False
        for path, value, _digest in rows:
            if path == self.owned_receipt:
                own_seen = True
                self.owned_published = True
                if _intent_seal(value) != self.owned_seal:
                    raise RuntimeError("Native operation receipt intent changed")
                require_inverse_catalog(self.native._recovery_bytes(self.root / HEADER)[0], value.get("catalog_identity_binding"))
            if value["status"] == "pending":
                if ordinary or path != self.owned_receipt:
                    raise RuntimeError("Pending catalog operation holds native admission")
                require_inverse_catalog(self.native._recovery_bytes(self.root / HEADER)[0], value.get("catalog_identity_binding"))
        if self.owned_published and not own_seen:
            raise RuntimeError("Native operation receipt disappeared")

    def validate_plan(self, plan):
        self.checkpoint(ordinary=True)
        bound = tuple(sorted(self.binding.items())) if self.enrolled else ()
        if getattr(plan, "catalog_identity_binding", ()) != bound or getattr(plan, "catalog_receipt_epoch", "") != self.receipt_epoch:
            raise RuntimeError("Pack review belongs to a stale catalog identity or operation history")

    def begin(self, receipt, value):
        self.checkpoint(ordinary=True)
        self._own_receipt(receipt, value)

    def _own_receipt(self, receipt, value):
        if not self.enrolled:
            return
        receipt = Path(receipt).absolute()
        if receipt.name != "recovery.json" or receipt.parent.parent != self.root / "backups/knowledge-pack-import":
            raise RuntimeError("Native receipt is outside its managed operation zone")
        require_inverse_catalog(self.native._recovery_bytes(self.root / HEADER)[0], value.get("catalog_identity_binding"))
        self.owned_receipt = receipt
        self.owned_seal = _intent_seal(value)


@contextmanager
def catalog_admission(root, native, *, role):
    root = Path(root).absolute()
    active = _ACTIVE.get()
    if active is not None:
        if active.root != root:
            raise RuntimeError("Cross-catalog nested native admission is unsupported")
        active.checkpoint(ordinary=True)  # Nested public calls gain no pending exemption.
        if role == "import":
            raise RuntimeError("Recursive Pack import is unsupported")
        yield active
        active.checkpoint(ordinary=True)
        return
    with native.RulePairAdmission(root / "data/user").admit():
        state = CatalogAdmissionState(root, native)
        state.checkpoint(ordinary=True)
        token = _ACTIVE.set(state)
        try:
            yield state
            state.checkpoint()
        finally:
            _ACTIVE.reset(token)


def require_catalog_admission(root, native, *, ordinary=True, receipt=None):
    root = Path(root).absolute()
    state = _ACTIVE.get()
    if state is None or state.root != root:
        if _observed(root, native)[0] is not None:
            raise RuntimeError("Enrolled private Pack path requires canonical public admission")
        return None  # Unchanged legacy private paths remain legacy only.
    if isinstance(state, _SelectedIdentityAdmission):
        state.require_restore(native, receipt)
    if isinstance(state, _SelectedPackAdmission):
        state.authorize(native, receipt)
    state.checkpoint(ordinary=ordinary)
    return state


def checkpoint_catalog_admission():
    state = _ACTIVE.get()
    if state is not None:
        state.checkpoint()


def _resolved_snapshot_path(tags):
    path = getattr(tags, "path", None)
    if path is None:
        return None  # Explicit detached in-memory legacy capture, with no I/O.
    path = Path(path).absolute()
    # Check ORIGINAL components before platform canonicalization can erase a
    # redirect/reparse component. Preserve the canonical native refusal policy.
    from . import knowledge_pack as native
    native._check_export_path(path)
    resolved = path.resolve(strict=False)
    native._check_export_path(resolved)
    if path.exists() and not path.samefile(resolved):
        raise RuntimeError("Tag path changed during filesystem classification")
    return resolved


def snapshot_catalog_root(tags):
    path = _resolved_snapshot_path(tags)
    if path is None:
        return None
    if path.parent.name.casefold() != "user" or path.parent.parent.name.casefold() != "data":
        return None  # Custom/detached legacy paths have no canonical catalog root.
    return path.parent.parent.parent


def _verify_snapshot_tag_path(tags, state):
    original = Path(tags.path).absolute()
    canonical = state.root / "data/user/tags.json"
    state.native._check_export_path(original)
    state.native._check_export_path(canonical)
    resolved = _resolved_snapshot_path(tags)
    if resolved != canonical.resolve(strict=True) or not original.samefile(canonical):
        raise RuntimeError("Enrolled snapshot requires the canonical Tag participant")
    return canonical


@contextmanager
def snapshot_catalog_admission(tags, *, role):
    root = snapshot_catalog_root(tags)
    if root is None:
        yield None
        return
    # Lazy leaf import; never initialize AppState, services or an overlay.
    from . import knowledge_pack as native
    with catalog_admission(root, native, role=role) as state:
        if state.enrolled:
            canonical = _verify_snapshot_tag_path(tags, state)
            # A loaded TagDatabase must still represent these persisted bytes.
            raw = native._recovery_bytes(canonical)[0]
            if getattr(tags, "_loaded_bytes", None) != raw:
                raise RuntimeError("Loaded Tag authority is stale; reload before capture")
        yield state
        if state.enrolled:
            _verify_snapshot_tag_path(tags, state)


def verify_snapshot_participants(state, categories, aliases, relationships):
    if state is None or not state.enrolled:
        return
    expected = (state.root / "data/defaults/categories.json", state.root / "data/user/categories.json")
    if (Path(categories.default_path).absolute(), Path(categories.user_path).absolute()) != expected:
        raise RuntimeError("Category authority belongs to another catalog")
    loaded = categories._loaded_source_fingerprints
    for path in expected:
        current = state.native._export_path_state(path)
        digest = () if current is None else bytes.fromhex(current[-1])
        if loaded.get(path) != digest:
            raise RuntimeError("Loaded Category authority is stale")
    workspace = aliases.workspace
    for store, relative in ((workspace.default_store, "data/defaults/relationships.json"),
                            (workspace.user_store, "data/user/relationships.json")):
        if Path(store.path).absolute() != state.root / relative:
            raise RuntimeError("Relationship authority belongs to another catalog")
        current = state.native._export_path_state(store.path)
        raw = None if current is None else state.native._recovery_bytes(store.path)[0]
        if store._loaded_bytes != raw:
            raise RuntimeError("Loaded Relationship authority is stale")
    if tuple(relationships) != tuple(workspace.effective):
        raise RuntimeError("Snapshot relationships differ from effective native authority")


def require_snapshot_catalog_admission(tags):
    root = snapshot_catalog_root(tags)
    if root is not None:
        from . import knowledge_pack as native
        require_catalog_admission(root, native)


@contextmanager
def native_store_admission(paths, *, role):
    """Supported persisted stores only; cached DTO lookups remain detached.

    Canonical roots are discovered from the actual resolved participant paths.
    Custom paths have no host enrollment inference. Legacy stores retain their
    previous lock/write behavior, including detached import staging stores.
    """
    import os
    from types import SimpleNamespace
    def native_name(value):
        # Win32 accepts trailing-dot/space directory and filename spellings.
        # Preserve original-location enrollment even if full resolution leaves
        # that root through a redirect. POSIX literal names remain literal.
        return (value.rstrip(" .") if os.name == "nt" else value).casefold()
    originals, resolved, roots = [], [], set()
    for value in paths:
        original = Path(value).absolute()
        originals.append(original)
        path = original.resolve(strict=False)
        resolved.append(path)
        # Include the original catalog root as well as a resolved participant's
        # root, so a redirected file cannot escape its enrolled source catalog.
        lexical = Path(os.path.normpath(original))
        for candidate in (lexical, path):
            if (native_name(candidate.name) in {"tags.json", "categories.json", "relationships.json"}
                    and native_name(candidate.parent.name) in {"user", "defaults"}
                    and native_name(candidate.parent.parent.name) == "data"):
                roots.add(candidate.parent.parent.parent.resolve(strict=False))
    if not roots:
        yield None
        return
    from . import knowledge_pack as native
    enrolled = [root for root in roots if _observed(root, native)[0] is not None]
    if not enrolled:
        yield None
        return
    # Keep legacy redirect behavior unchanged. The existing strict native path
    # policy applies only after enrollment is known, before a public body runs.
    for original in originals:
        _resolved_snapshot_path(SimpleNamespace(path=original))
    if len(roots) != 1:
        raise RuntimeError("Store participants belong to different catalog roots")
    root = enrolled[0]
    for path in resolved:
        relative = path.relative_to(root).as_posix().casefold() if path.is_relative_to(root) else ""
        if relative not in {"data/user/tags.json", "data/user/categories.json", "data/defaults/categories.json",
                            "data/user/relationships.json", "data/defaults/relationships.json"}:
            raise RuntimeError("Enrolled store requires canonical persisted participants")
    with catalog_admission(root, native, role=role) as state:
        yield state


def native_store_api(*attributes):
    """Keep each supported load/save body intact under catalog admission."""
    from functools import wraps
    def decorate(method):
        @wraps(method)
        def admitted(self, *args, **kwargs):
            with native_store_admission(tuple(getattr(self, name) for name in attributes),
                                        role="store:" + method.__name__):
                return method(self, *args, **kwargs)
        return admitted
    return decorate


class _SelectedIdentityAdmission:
    """Synthetic-only selected schema-4 forward recovery, never ordinary access."""
    enrolled = True

    def __init__(self, transaction, reference):
        self.transaction, self.reference = transaction, reference
        self.root, self.native = transaction.root, transaction.native
        self.receipt, _manifest, _state, _prepared = transaction._prepare(reference)
        self.receipt_state = _state
        self.other_receipts = _pins(_receipts(self.root, self.native), self.receipt)
        self.restore_entered = False

    def checkpoint(self, *, ordinary=False):
        if ordinary:
            raise RuntimeError("Selected identity transaction holds ordinary native admission")
        path, _manifest, _state, _prepared = self.transaction._prepare(self.reference)
        if path != self.receipt or _pins(_receipts(self.root, self.native), path) != self.other_receipts:
            raise RuntimeError("Selected identity transaction history changed")

    def require_restore(self, native, receipt):
        if self.restore_entered or native is not self.native or receipt is None or Path(receipt).absolute() != self.receipt:
            raise RuntimeError("Identity admission permits only its selected native restore")
        self.restore_entered = True

    def validate_restore(self, manifest, preflight):
        _path, expected, state, prepared = self.transaction._prepare(self.reference)
        # Never iterate or execute a caller-owned mutable checkpoint object.
        if preflight is not None or type(manifest) is not dict or manifest != expected or state != self.receipt_state:
            raise RuntimeError("Selected identity restore requires admission-owned participants")
        return expected, state, prepared


@contextmanager
def selected_identity_admission(transaction, reference):
    """Candidate sandbox only; reuse its exact selected-intent/participant checks."""
    from suite_core.catalog_identity_transactions_candidate import SyntheticIdentityTransactions
    from suite_core.completed_pack_undo_candidate import SyntheticPackUndoSandbox
    if type(transaction) is not SyntheticIdentityTransactions or type(transaction.sandbox) is not SyntheticPackUndoSandbox:
        raise RuntimeError("Selected identity admission requires an explicit synthetic transaction")
    if _ACTIVE.get() is not None:
        raise RuntimeError("Nested selected identity admission is unsupported")
    with transaction.sandbox._guard():
        state = _SelectedIdentityAdmission(transaction, reference)
        state.checkpoint()
        token = _ACTIVE.set(state)
        try:
            yield state
            state.checkpoint()
        finally:
            _ACTIVE.reset(token)


class _SelectedPackAdmission(CatalogAdmissionState):
    """One exact selected Pack operation; canonical engine owns participants."""
    def __init__(self, sandbox, native, selection, receipt, operation, *, root=None):
        self.sandbox, self.selection, self.operation = sandbox, selection, operation
        self.native = native
        self.marker = sandbox._marker() if sandbox is not None else None
        super().__init__(sandbox.root if sandbox is not None else Path(root).absolute(), native)
        if not self.enrolled:
            raise RuntimeError("Selected Pack recovery requires an enrolled catalog")
        self.restore_entered = False
        self.receipt_stamp = None
        if operation == "undo":
            _source, value = native._selected_pack_source(sandbox, selection)
            self.baseline = value
        elif operation in {"import_recovery", "inverse_recovery"}:
            self.owned_receipt = Path(receipt).absolute()
            value, self.receipt_stamp = native._read_pack_receipt(self.owned_receipt, self.root)
            self.owned_seal, self.owned_published = _intent_seal(value), True
            self.baseline = value
            self._validate_owned(value)
        else:
            raise RuntimeError("Unsupported selected Pack operation")
        native._require_default_baseline(self.root, self.baseline)

    def _validate_owned(self, value):
        version = value.get("schema_version")
        if type(version) is not int:
            raise RuntimeError("Selected Pack requires an exact physical receipt version")
        if type(version) is not int:
            raise RuntimeError("Selected Pack receipt version must be exact")
        if value.get("status") not in {"pending", "restored"}:
            raise RuntimeError("Select a pending or completed restore receipt")
        if self.operation == "import_recovery":
            if version != ENROLLED_PACK_RECEIPT or value.get("identity_bound_operation") != "pack_import":
                raise RuntimeError("Select an identity-bound pending import")
        else:
            from dataclasses import asdict
            _source, original = self.native._selected_pack_source(self.sandbox, self.selection)
            if (version != ENROLLED_INVERSE_RECEIPT or value.get("identity_bound_operation") != "pack_undo"
                    or value.get("operation_kind") != "synthetic_pack_undo"
                    or value.get("operation_id") != self.owned_receipt.parent.name
                    or value.get("undo_of") != asdict(self.selection)
                    or value.get("files") != original.get("files")
                    or value.get("default_authority_fingerprints") != original.get("default_authority_fingerprints")):
                raise RuntimeError("Inverse does not match its exact selected import")
        require_inverse_catalog(self.native._recovery_bytes(self.root / HEADER)[0], value.get("catalog_identity_binding"))

    def checkpoint(self, *, ordinary=False):
        if ordinary:
            raise RuntimeError("Selected Pack operation holds ordinary admission")
        CatalogAdmissionState.checkpoint(self, ordinary=False)
        if self.sandbox is not None and self.sandbox._marker() != self.marker:
            raise RuntimeError("Selected synthetic catalog marker changed")
        self.native._require_default_baseline(self.root, self.baseline)
        if "foundation_transition" in self.baseline:
            others = _pins(_receipts(self.root, self.native), self.owned_receipt)
            epoch = sha256(json.dumps(others, separators=(",", ":")).encode()).hexdigest()
            if epoch != self.baseline.get("catalog_receipt_epoch"):
                raise RuntimeError("Foundation review receipt epoch changed")
        if self.selection is not None:
            self.native._selected_pack_source(self.sandbox, self.selection)
        if self.owned_receipt is not None and self.owned_receipt.exists():
            value, stamp = self.native._read_pack_receipt(self.owned_receipt, self.root)
            self._validate_owned(value)
            if value["status"] == "pending":
                if self.receipt_stamp is not None and stamp != self.receipt_stamp:
                    raise RuntimeError("Selected pending receipt changed")
                self.receipt_stamp = stamp
            else:
                rows = self.native._prepare_pack_restore_verified(self.owned_receipt, dict(value, schema_version=2), self.root)
                if any((row[4][-1] if row[4] is not None else "missing") != row[1] for row in rows):
                    raise RuntimeError("Completed selected restore differs from its declared result")

    def begin(self, receipt, value):
        if self.operation != "undo" or self.owned_receipt is not None:
            raise RuntimeError("Selected Pack operation already owns its receipt")
        self.checkpoint()
        receipt = Path(receipt).absolute()
        if receipt.exists():
            raise RuntimeError("Selected inverse receipt already exists")
        # Reuse the exact ordinary owned-receipt publication checks, without
        # giving nested public calls an ordinary-admission exception.
        self._own_receipt(receipt, value)
        self._validate_owned(value)

    def authorize(self, native, receipt):
        if native is not self.native:
            raise RuntimeError("Selected Pack requires its exact canonical native engine")
        if receipt is not None:
            if self.restore_entered or self.owned_receipt is None or Path(receipt).absolute() != self.owned_receipt:
                raise RuntimeError("Selected Pack permits one exact owned restore invocation")
            self.restore_entered = True

    def admit_manifest(self, path, value):
        path = Path(path).absolute()
        durable, _stamp = self.native._read_pack_receipt(path, self.root)
        if type(value) is not dict or value != durable:
            raise RuntimeError("Selected restore requires its physical durable receipt")
        if path == self.owned_receipt:
            self._validate_owned(durable)
        elif self.operation == "undo":
            source, original = self.native._selected_pack_source(self.sandbox, self.selection)
            if path != source or durable != original:
                raise RuntimeError("Prepare belongs to a different selected source")
        else:
            raise RuntimeError("Prepare belongs to a nonselected receipt")
        return dict(durable, schema_version=2)

    def validate_restore(self, manifest, preflight):
        if preflight is not None:
            raise RuntimeError("Selected Pack restore rejects caller-owned preflight")
        durable, stamp = self.native._read_pack_receipt(self.owned_receipt, self.root)
        if type(manifest) is not dict or manifest != durable or stamp != self.receipt_stamp:
            raise RuntimeError("Selected Pack receipt changed before restoration")
        prepared = self.native._prepare_pack_restore(self.owned_receipt, durable, self.root)
        return durable, stamp, prepared


@contextmanager
def selected_pack_admission(sandbox, native, *, selection=None, receipt=None, operation):
    from suite_core.completed_pack_undo_candidate import SyntheticPackUndoSandbox
    if type(sandbox) is not SyntheticPackUndoSandbox or sandbox.native is not native:
        raise RuntimeError("Selected Pack admission requires an explicit canonical synthetic sandbox")
    if _ACTIVE.get() is not None:
        raise RuntimeError("Nested selected Pack admission is unsupported")
    with sandbox._guard():
        state = _SelectedPackAdmission(sandbox, native, selection, receipt, operation)
        state.checkpoint()
        token = _ACTIVE.set(state)
        try:
            yield state
            state.checkpoint()
        finally:
            _ACTIVE.reset(token)


@contextmanager
def selected_native_pack_recovery(root, native, *, receipt, catalog_identity_binding):
    """Select one native enrolled import receipt without synthetic marker authority.

    Existing selected admission owns receipt seals, all other pending operations,
    durable default baselines, stale preflight and exact native restoration.
    No enrollment, header creation, inverse or completed-import Undo is authorized.
    """
    root = Path(root)
    if not root.is_absolute() or ".." in root.parts or _ACTIVE.get() is not None:
        raise RuntimeError("Select an exact non-nested native catalog recovery root")
    with native.RulePairAdmission(root / "data/user").admit():
        native._check_export_path(root)
        state = _SelectedPackAdmission(None, native, None, receipt, "import_recovery", root=root)
        if type(catalog_identity_binding) is not tuple or tuple(sorted(state.binding.items())) != catalog_identity_binding:
            raise RuntimeError("Selected native recovery belongs to a stale catalog identity")
        state.checkpoint()
        token = _ACTIVE.set(state)
        try:
            yield state
            state.checkpoint()
        finally:
            _ACTIVE.reset(token)
