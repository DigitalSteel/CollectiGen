"""Explicit model collection operations over the shared private registry.

No constructor scans, network, model execution, or image knowledge access.
"""
from __future__ import annotations

from copy import deepcopy
from collections import deque
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
from itertools import islice
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
from urllib.parse import urlsplit, unquote, parse_qs
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from .model_resource_registry import (
    ModelResourceError, ModelResourceRegistry, RESOURCE_ROLES,
    ResourceInspection, inspect_model_resource, _cancel, _current,
)
from .training_source_io import _assert_unredirected
from .external_write_transaction import write_verified_json
from .model_organization import destination_path, normalize_model_family


def _path(value, protected_roots=()):
    if not isinstance(value, (str, Path)) or not str(value).strip() or not Path(value).is_absolute():
        raise ModelResourceError("Choose an explicit absolute model location.")
    path = Path(os.path.abspath(value))
    # Reject overlap before stat or enumeration, including a parent of a library.
    for root in protected_roots:
        root = Path(os.path.abspath(root))
        if path == root or root in path.parents or path in root.parents:
            raise ModelResourceError("Choose a model folder separate from image libraries and protected backups.")
    _assert_unredirected(path)
    return path


def _notify_operation_started(callback, reference):
    """Bind detached recovery metadata before effects; never reenter an owner.

    Callbacks run under the caller's existing locks/pins and must durably record
    only their own journal. A failed binding never authorizes a guessed retry.
    """
    if callback is not None:
        try:
            callback(deepcopy(reference))
        except Exception as exc:
            raise ModelResourceError("Operation binding failed; inspect the retained exact recovery reference.") from exc


def _immutable_operation_reference(owner, operation_id, locator, raw, **evidence):
    return {"schema_version": 1, "reference_kind": "immutable_receipt",
        "owner": owner, "operation_id": operation_id, "locator": str(locator),
        "fingerprint": hashlib.sha256(raw).hexdigest(), "evidence": evidence}


@dataclass(frozen=True)
class IndexPreview:
    root: Path
    inspections: tuple[ResourceInspection, ...]
    held: tuple[tuple[str, str], ...]
    registry_token: str
    header_suggestions: tuple = ()


@dataclass(frozen=True)
class IndexDeclaration:
    path: Path
    role: str
    family: str


@dataclass(frozen=True)
class IndexRegistration:
    count: int
    registry_token: str


@dataclass(frozen=True)
class ExactLocationPreview:
    resource_id: str
    inspection: ResourceInspection
    registry_token: str


@dataclass(frozen=True)
class ModelLineageRow:
    label: str
    identity: str
    detail: str
    target: str = ""
    training_reference: object = None


class ModelLineageIndex:
    """Read-only, bounded projection of one registry snapshot; never resolves files.

    Build once off the UI thread. All subsequent pages use indexed rows, including
    reverse edges. Unknown fields remain in the detached raw record, not a writer.
    """
    PAGE_SIZE = 50
    MAX_EDGES = 200_000
    MAX_CLAIMS = 64
    KINDS = ("operations", "parents", "children", "references", "locations")

    def __init__(self, snapshot, token, *, should_cancel=None, max_edges=MAX_EDGES):
        if len(snapshot["resources"]) > 20_000:
            raise ModelResourceError("Lineage inspection supports at most 20,000 registered resources.")
        self.token = token
        self._records = deepcopy(snapshot["resources"])
        self._rows = {key: {kind: [] for kind in self.KINDS} for key in self._records}
        self._warnings = {key: [] for key in self._records}
        self.complete = True
        self.edge_count = 0
        graph = {key: set() for key in self._records}
        reverse = {key: set() for key in self._records}
        for identity, record in self._records.items():
            _cancel(should_cancel)
            rows, warnings = self._rows[identity], self._warnings[identity]
            from .model_training_reference import training_package_reference
            package_reference = training_package_reference(record, identity, token)
            claims = record.get("lineage", [])
            if not isinstance(claims, list):
                warnings.append("Unreadable lineage structure; inspect Raw Details."); claims = []
            if len(claims) > 1:
                warnings.append("Multiple lineage claims; conflicting attribution is not reconciled here.")
            if len(claims) > self.MAX_CLAIMS:
                warnings.append("Claim limit reached; additional claims remain in Raw Details.")
                self.complete = False
            for claim_index, claim in enumerate(claims[:self.MAX_CLAIMS], 1):
                if not isinstance(claim, dict):
                    warnings.append(f"Claim {claim_index} is unreadable; retained in Raw Details."); continue
                scope = f"Claim {claim_index}"
                operation = self._text(claim.get("operation", "Unknown operation"))
                schema = claim.get("schema_version", 1)
                if type(schema) is not int or schema != 1:
                    warnings.append(f"{scope}: unsupported schema {self._text(schema)}; shown as recorded only.")
                if operation not in {"training", "fine_tune", "merge", "conversion", "quantization"}:
                    warnings.append(f"{scope}: unknown operation; no execution meaning inferred.")
                if claim.get("output_id", identity) != identity:
                    warnings.append(f"{scope}: output identity conflicts with this resource.")
                authority = claim.get("authority")
                receipt = claim.get("execution_receipt")
                if authority == "user_attributed":
                    evidence = "User attribution; does not prove execution"
                elif schema == 1 and type(schema) is int and authority == "collectigen_verified" and self._exact_identity(receipt):
                    evidence = "Recorded verified execution; receipt not revalidated in this view"
                else:
                    evidence = "Unknown or incomplete execution authority"
                tool = self._text(claim.get("tool", claim.get("trainer_id", "Unknown tool")))
                version = self._text(claim.get("tool_version", claim.get("trainer_version", "Unknown version")))
                rows["operations"].append(ModelLineageRow(f"{scope}: {operation}", identity,
                    f"{tool} · {version}\n{evidence}\nQuality: {self._text(claim.get('quality', 'unknown'))}. "
                    "No compatibility or quality assessment is made here. Exact settings are in Raw Details."))
                parents = claim.get("parent_ids", [])
                if not isinstance(parents, list):
                    warnings.append(f"{scope}: unreadable parent list."); parents = []
                if len(parents) > 64:
                    warnings.append(f"{scope}: parent limit reached; inspect Raw Details."); self.complete = False
                seen = set()
                for order, parent in enumerate(parents[:64], 1):
                    if self.edge_count >= max_edges:
                        self.complete = False; break
                    self.edge_count += 1
                    valid = self._exact_identity(parent)
                    registered = valid and parent in self._records
                    state = "Registered; location not rechecked" if registered else "Not registered" if valid else "Invalid exact identity"
                    if valid and parent in seen:
                        warnings.append(f"{scope}: repeated parent identity at position {order}.")
                    if valid: seen.add(parent)
                    row = ModelLineageRow(f"{scope} · Parent {order}: {self.name(parent)}", self._text(parent),
                        f"{operation} · {state}\n{evidence}", parent if registered else "")
                    rows["parents"].append(row)
                    if registered:
                        self._rows[parent]["children"].append(ModelLineageRow(
                            f"{self.name(identity)} · {scope}, parent {order}", identity,
                            f"Derived child · {operation}\n{evidence}", identity))
                        graph[identity].add(parent); reverse[parent].add(identity)
                self._references(rows, warnings, claim.get("references", []), scope)
                for key, label in (("package_fingerprint", "Training package fingerprint"),
                                   ("plan_fingerprint", "Training plan fingerprint"),
                                   ("execution_receipt", "Execution receipt"),
                                   ("settings_sha256", "Operation settings SHA-256")):
                    if key in claim:
                        rows["references"].append(ModelLineageRow(f"{scope} · {label}",
                            self._text(claim[key]),
                            "Choose the referenced public training package to verify and inspect its frozen data. No folder is searched automatically."
                            if key == "package_fingerprint" and package_reference else
                            "Recorded value only; no reference resolver is available here.",
                            training_reference=package_reference if key == "package_fingerprint" else None))
            self._references(rows, warnings, record.get("references", []), "Resource")
            locations = record.get("locations", [])
            if not isinstance(locations, list):
                locations = []; warnings.append("Unreadable location list; inspect Raw Details.")
            for index, location in enumerate(locations[:1000], 1):
                path = location.get("path") if isinstance(location, dict) else location
                rows["locations"].append(ModelLineageRow(f"Location {index}", self._text(path),
                    "Recorded path; availability and bytes have not been checked. This view does not open files."))
            if len(locations) > 1000:
                warnings.append("Location limit reached; remaining locations are in Raw Details.")
        # Iterative O(V+E) reduction: leftovers are cycles OR depend on a cycle.
        # Do not call every remaining vertex a cycle member.
        degrees = {key: len(parents) for key, parents in graph.items()}
        pending = deque(key for key, degree in degrees.items() if not degree)
        while pending:
            _cancel(should_cancel)
            parent = pending.popleft()
            for child in reverse[parent]:
                degrees[child] -= 1
                if not degrees[child]: pending.append(child)
        for identity, degree in degrees.items():
            if degree:
                self._warnings[identity].append("Cycle or dependency on a cycle in loaded relationships; inspect one step at a time.")
        for kinds in self._rows.values():
            for kind in kinds: kinds[kind] = tuple(kinds[kind])

    @staticmethod
    def _text(value):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return text if len(text)<=2048 else text[:2048]+"… [see Raw Details]"

    @staticmethod
    def _exact_identity(value):
        return isinstance(value, str) and re.fullmatch(r"(?:pack-)?sha256:[0-9a-f]{64}", value) is not None

    def name(self, identity):
        record = self._records.get(identity, {}) if isinstance(identity, str) else {}
        user = record.get("user", {})
        if isinstance(user, dict) and isinstance(user.get("display_name"), str) and user["display_name"]:
            return user["display_name"][:120]
        locations = record.get("locations", [])
        if isinstance(locations, list) and locations and isinstance(locations[0], dict):
            return Path(str(locations[0].get("path", ""))).name[:120] or "Unnamed resource"
        return "Registered resource" if record else "Unavailable resource"

    def _references(self, rows, warnings, references, scope):
        if not isinstance(references, list):
            warnings.append(f"{scope}: unreadable references; inspect Raw Details."); return
        if len(references) > 100:
            warnings.append(f"{scope}: reference limit reached; inspect Raw Details.")
        for reference in references[:100]:
            if not isinstance(reference, dict):
                rows["references"].append(ModelLineageRow(f"{scope} · Unknown reference", self._text(reference),
                    "Unreadable reference; retained in Raw Details.")); continue
            kind = reference.get("kind", "Unknown type")
            identity = reference.get("identity", "Missing identity")
            supported = kind in ("image", "generation_record", "benchmark", "dataset", "training_manifest", "evaluation")
            detail = "No reference resolver is available here; no library or path is searched."
            if not supported: detail = "Unknown reference type. " + detail
            if not self._exact_identity(identity): detail = "Invalid or unsupported exact identity. " + detail
            rows["references"].append(ModelLineageRow(f"{scope} · {self._text(kind)}", self._text(identity), detail))

    def page(self, identity, kind, page=0):
        if kind not in self.KINDS: raise ValueError("Unknown lineage view")
        rows = self._rows.get(identity, {}).get(kind, ())
        start = max(0, int(page)) * self.PAGE_SIZE
        return rows[start:start + self.PAGE_SIZE], len(rows)

    def summary(self, identity):
        if identity not in self._records: return "This exact resource is not in the loaded registry snapshot."
        rows = self._rows[identity]
        text = (f"{self.name(identity)}\n{len(rows['operations'])} recorded claim(s) · {len(rows['parents'])} parent link(s) · "
                f"{len(rows['children'])} child link(s) · {len(rows['locations'])} recorded location(s). "
                "Loaded snapshot; locations and evidence have not been revalidated.")
        if not self.complete: text += " Projection limit reached: relationship counts may be incomplete; inspect Raw Details."
        warnings = list(dict.fromkeys(self._warnings[identity]))
        if warnings: text += "\n" + "\n".join(warnings[:8])
        return text

    def raw_details(self, identity):
        """Explicit full-record inspection preserves every unknown field."""
        return json.dumps({"resource_id": identity, "registry_snapshot": self.token,
            "record": self._records.get(identity)}, indent=2, ensure_ascii=False)


class ModelWorkspace:
    def __init__(self, registry: ModelResourceRegistry, *, protected_roots=(), cache_references=None):
        self.registry = registry
        self.protected_roots = tuple(protected_roots)
        if cache_references is not None and cache_references.cache.registry is not registry:
            raise ModelResourceError("Cache references must use this exact registry owner.")
        self.cache_references = cache_references

    def preview_exact_location(self, resource_id, path, *, expected_registry, should_cancel=None, progress=None):
        """Read one deliberately chosen copy, without changing the selected record."""
        if not isinstance(resource_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", resource_id):
            raise ModelResourceError("Choose a registered single-file model. Managed packs use their existing pack registration flow.")
        snapshot, token = self.registry.snapshot()
        record = snapshot["resources"].get(resource_id)
        if token != expected_registry or record is None:
            raise ModelResourceError("The selected registry record changed or is missing; reload before locating its copy.")
        chosen = _path(path, self.protected_roots)
        inspection = inspect_model_resource(chosen, should_cancel=should_cancel, progress=progress)
        if inspection.resource_id != resource_id or inspection.stored_identity() != record["identity"]:
            raise ModelResourceError("This file is not an exact copy of the selected model. Nothing was registered; choose another file.")
        if self.registry.snapshot()[1] != token:
            raise ModelResourceError("The model registry changed during preview; reload before locating its copy.")
        return ExactLocationPreview(resource_id, inspection, token)

    def attach_exact_location(self, preview, *, should_cancel=None, progress=None):
        if not isinstance(preview, ExactLocationPreview):
            raise ModelResourceError("Preview an exact copy before adding its location.")
        _path(preview.inspection.path, self.protected_roots)
        return self.registry.attach_verified_location(preview.resource_id, preview.inspection,
            expected_registry=preview.registry_token, should_cancel=should_cancel, progress=progress)

    def preview_folder(self, root, *, recursive=True, should_cancel=None, progress=None, workflow_progress=None, collect_detection=False,
                       max_entries=100_000, max_files=20_000):
        """Discover a bounded inventory, then inspect it without registration.

        workflow_progress(completed_files, total_files, label) uses None for the
        unknown discovery total. The existing progress callback remains bytes
        for the current file; it never represents whole-folder completion.
        """
        root = _path(root, self.protected_roots)
        _, token = self.registry.snapshot()
        inspections, held, stack, candidates, suggestions = [], [], [root], [], {}
        count = 0
        def report(completed, total, label):
            if workflow_progress: workflow_progress(completed, total, label)
        report(0, None, "Discovering model files; total not yet known")
        while stack:
            _cancel(should_cancel)
            directory = _path(stack.pop(), self.protected_roots)
            state = directory.stat(follow_symlinks=False)
            if int(getattr(state, "st_file_attributes", 0)) & (0x1000 | 0x40000 | 0x400000):
                raise ModelResourceError("An indexed folder is offline or requires cloud recall.")
            with os.scandir(directory) as entries:
                for entry in entries:
                    _cancel(should_cancel)
                    count += 1
                    if count > max_entries:
                        raise ModelResourceError("Folder exceeds the scan limit; choose a smaller subfolder.")
                    if count % 128 == 0:
                        report(0, None, f"Discovering model files · {count:,} entries checked · {len(candidates):,} candidates found")
                    path = _path(entry.path, self.protected_roots)
                    state = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(state.st_mode):
                        if recursive:
                            stack.append(path)
                        continue
                    if path.suffix.casefold() not in (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf"):
                        continue
                    if len(candidates) >= max_files:
                        raise ModelResourceError("Folder exceeds the model limit; choose a smaller subfolder.")
                    candidates.append(path)
        total = len(candidates)
        _cancel(should_cancel)
        report(0, total, f"Discovered {total:,} model files; preparing review")
        for index, candidate in enumerate(candidates, 1):
            _cancel(should_cancel)
            path = _path(candidate, self.protected_roots)
            name = str(path.relative_to(root))
            phase = f"Inspecting model file {index:,} of {total:,}: {name}"
            report(index - 1, total, phase)
            _cancel(should_cancel)
            held_before = len(held)
            if path.suffix.casefold() != ".safetensors":
                held.append((name, "Unsupported container: use a safe tensor version; no model code was loaded."))
            else:
                def file_progress(done, size, label):
                    if progress: progress(done, size, label)
                    report(index - 1, total, f"{phase} · {label}: {done / 2**20:.1f} / {size / 2**20:.1f} MiB")
                try:
                    observed = []
                    def header(raw):
                        from .model_detection import inspect_header_suggestions
                        observed.append(inspect_header_suggestions(raw))
                    extra = {"header_observer": header} if collect_detection else {}
                    inspection = inspect_model_resource(path, should_cancel=should_cancel, progress=file_progress, **extra)
                    inspections.append(inspection)
                    if observed: suggestions[inspection.resource_id] = observed[0]
                except InterruptedError:
                    raise
                except (OSError, RuntimeError, ValueError):
                    held.append((name, "Unavailable, changed, redirected or invalid container; inspect an available safe copy."))
            report(index, total, f"{'Held' if len(held) > held_before else 'Reviewed'} model file {index:,} of {total:,}: {name}")
        _cancel(should_cancel)
        report(total, total, f"Folder review complete: {len(inspections):,} inspected, {len(held):,} held; nothing registered")
        return IndexPreview(root, tuple(inspections), tuple(held), token, tuple(suggestions.items()))

    def detect_preview(self, preview, *, should_cancel=None):
        from .model_detection import review_model_files
        _cancel(should_cancel)
        snapshot, token = self.registry.snapshot()
        if token != preview.registry_token:
            raise ModelResourceError("The model registry changed after this scan; scan again before reviewing detection.")
        return review_model_files(preview.inspections, snapshot, headers=dict(preview.header_suggestions), should_cancel=should_cancel)

    def apply_index(self, preview, *, role, family, should_cancel=None):
        return self.apply_index_selection(preview,
            (IndexDeclaration(item.path, role, family) for item in preview.inspections), should_cancel=should_cancel).count

    def apply_index_selection(self, preview, declarations, *, should_cancel=None, operation_started=None):
        """Register one reviewed mixed-family selection in a single transaction."""
        if not isinstance(preview, IndexPreview):
            raise ModelResourceError("Review the exact model files before registering them.")
        declarations = tuple(islice(iter(declarations), 20_001))
        if not declarations or len(declarations) > 20_000:
            raise ModelResourceError("Select between one and 20,000 reviewed model files.")
        available = {item.path: item for item in preview.inspections}
        selected, seen = [], set()
        for declaration in declarations:
            _cancel(should_cancel)
            if not isinstance(declaration, IndexDeclaration) or not isinstance(declaration.path, Path) or declaration.path not in available or declaration.path in seen:
                raise ModelResourceError("Choose each exact reviewed file once; unreviewed files cannot be registered.")
            if declaration.role not in RESOURCE_ROLES or not isinstance(declaration.family, str) or not declaration.family.strip() or len(declaration.family) > 160:
                raise ModelResourceError("Choose a role and model family for every selected file.")
            seen.add(declaration.path)
            selected.append((available[declaration.path], declaration))
        _path(preview.root, self.protected_roots)
        with self.registry._writer():
            value, token = self.registry.snapshot()
            if token != preview.registry_token:
                raise ModelResourceError("The registry changed after preview; preview the folder again.")
            for inspection, declaration in selected:
                _cancel(should_cancel)
                _path(inspection.path, self.protected_roots)
                _current(inspection)
            for inspection, declaration in selected:
                record = value["resources"].setdefault(inspection.resource_id, {
                    "identity": inspection.stored_identity(), "locations": [], "declarations": [], "lineage": [],
                })
                if record["identity"] != inspection.stored_identity():
                    raise ModelResourceError("Stored identity differs from inspection; review it before indexing.")
                match = next((r for r in record["locations"] if r["path"].casefold() == str(inspection.path).casefold()), None)
                if match is None:
                    match = {}; record["locations"].append(match)
                match.update(path=str(inspection.path), file_state=list(inspection.file_state))
                claim = {"role": declaration.role, "family": declaration.family.strip(), "authority": "user_declared", "compatibility": "unverified"}
                if claim not in record["declarations"]:
                    record["declarations"].append(claim)
            workspace = value.setdefault("workspace", {})
            roots = workspace.setdefault("indexed_folders", [])
            if str(preview.root) not in roots:
                roots.append(str(preview.root))
            _cancel(should_cancel)
            for inspection, _ in selected:
                _current(inspection)
            if operation_started is not None:
                from .model_resource_registry import _bytes, MAX_RESOURCES, MAX_STORE_BYTES
                after = _bytes(value)
                if len(value["resources"]) > MAX_RESOURCES or len(after) > MAX_STORE_BYTES:
                    raise ModelResourceError("The model registry exceeds its supported size limit.")
                selection = {"root": str(preview.root), "selected": [
                    {"path": str(inspection.path), "resource_id": inspection.resource_id,
                        "file_state": list(inspection.file_state), "identity": inspection.stored_identity(),
                        "role": declaration.role, "family": declaration.family.strip()}
                    for inspection, declaration in selected]}
                evidence = {"kind": "index_selection", "before_registry": token,
                    "expected_after_registry": hashlib.sha256(after).hexdigest(),
                    "selection_fingerprint": hashlib.sha256(_bytes(selection)).hexdigest()}
                _notify_operation_started(operation_started, {"schema_version": 1,
                    "reference_kind": "registry_transition", "owner": "model_registration",
                    "operation_id": uuid4().hex, "locator": str(self.registry.path),
                    "fingerprint": hashlib.sha256(_bytes(evidence)).hexdigest(), "evidence": evidence})
                _cancel(should_cancel)
                for inspection, _ in selected:
                    _current(inspection)
                if self.registry.snapshot()[1] != token:
                    raise ModelResourceError("The registry changed while binding registration; review again.")
            self.registry._save(value, token)
            saved, saved_token = self.registry.snapshot()
            if saved != value:
                raise ModelResourceError("Registration finished but the registry changed again; reload to review the saved result.")
        return IndexRegistration(len(selected), saved_token)

    def save_notes(self, resource_id, *, tags, notes, expected_registry):
        if not isinstance(tags, list) or len(tags) > 100 or any(not isinstance(t, str) or len(t) > 160 for t in tags) or len(notes) > 20_000:
            raise ModelResourceError("Use at most 100 short tags and 20,000 characters of notes.")
        with self.registry._writer():
            value, token = self.registry.snapshot()
            if token != expected_registry or resource_id not in value["resources"]:
                raise ModelResourceError("The model changed after preview; reload before saving notes.")
            user = value["resources"][resource_id].setdefault("user", {})
            user.update(tags=list(dict.fromkeys(t.strip() for t in tags if t.strip())), notes=notes)
            self.registry._save(value, token)

    def attach_provider_evidence(self, resource_id, evidence, *, expected_registry):
        if evidence.get("provider") not in ("civitai", "hugging_face") or evidence.get("state") != "available" or evidence.get("currentness") not in ("current", "stale"):
            raise ModelResourceError("Only identifiable available provider evidence can be attached.")
        if not isinstance(evidence.get("source"), dict) or not evidence["source"].get("retrieved_at"):
            raise ModelResourceError("Provider evidence must retain its dated source attribution.")
        matches = [item for item in evidence.get("items", []) if any(
            file.get("hash_scope") == "full_file" and "sha256:" + str(file.get("sha256")) == resource_id
            for file in item.get("files", []))]
        if len(matches) != 1:
            raise ModelResourceError("Choose an unambiguous provider version matching this exact full-file identity.")
        claim = {"provider": evidence["provider"], "source": deepcopy(evidence["source"]),
            "currentness": evidence.get("currentness", "unknown"), "item": deepcopy(matches[0]),
            "authority": "attributed_provider_evidence", "compatibility": "unverified"}
        if len(json.dumps(claim).encode("utf-8")) > 256 * 1024:
            raise ModelResourceError("Provider evidence is too large; choose a narrower version query.")
        with self.registry._writer():
            value, token = self.registry.snapshot()
            if token != expected_registry or resource_id not in value["resources"]:
                raise ModelResourceError("The model registry changed; reload before attaching evidence.")
            record = value["resources"][resource_id]
            claims = record.setdefault("provider_evidence", [])
            if claim not in claims: claims.append(claim)
            if len(claims) > 100:
                raise ModelResourceError("This model has 100 retained evidence versions; review its history before adding more.")
            self.registry._save(value, token)

    def save_destinations(self, default, overrides, *, expected_registry, family_destinations=None,
                          storage_policy=None, managed_roots=()):
        _path(default, self.protected_roots)
        if not isinstance(overrides, dict) or len(overrides) > 100:
            raise ModelResourceError("Use a mapping with at most 100 destination overrides.")
        for key, path in overrides.items():
            if not isinstance(key, str) or not key or len(key) > 200:
                raise ModelResourceError("Destination keys must be a role or role:family.")
            role, separator, family = key.partition(":")
            if role not in RESOURCE_ROLES or (separator and (
                not family.strip() or family != family.strip() or len(family) > 160
            )):
                raise ModelResourceError("Destination keys must use a supported role and an optional nonempty family.")
            _path(path, self.protected_roots)
        if family_destinations is not None:
            if not isinstance(family_destinations, dict) or len(family_destinations) > 100:
                raise ModelResourceError("Use at most 100 configured family destinations.")
            roots = []
            family_keys = set()
            for key, path in family_destinations.items():
                if (not isinstance(key, str) or not key or len(key) > 160
                        or key not in ("Shared", "Upscalers", "Analysis", "Unclassified")
                        and not (key.startswith(("Images:", "Video:"))
                                 and key.partition(":")[2]
                                 and not any(c in key.partition(":")[2] for c in "/\\"))):
                    raise ModelResourceError("Choose an Images or Video family, or a shared purpose destination.")
                if ":" in key:
                    media_type, family = key.split(":", 1)
                    identity = (media_type, normalize_model_family(family).casefold())
                    if identity in family_keys:
                        raise ModelResourceError("Configure one destination for each model media and family; alias labels cannot use separate folders.")
                    family_keys.add(identity)
                selected = _path(path, self.protected_roots)
                if any(selected == root or selected in root.parents or root in selected.parents for root in roots):
                    raise ModelResourceError("Keep configured family destinations separate so folder hints stay unambiguous.")
                roots.append(selected)
        from .model_use_leases import cache_lock
        from .model_storage_policy import prepare_policy_update
        with cache_lock(self.registry, protected_roots=self.protected_roots), self.registry._writer():
            value, token = self.registry.snapshot()
            if token != expected_registry:
                raise ModelResourceError("The model settings changed; reload and retry.")
            workspace = value.setdefault("workspace", {})
            workspace.update(default_destination=str(Path(default).absolute()),
                             destination_overrides={k: str(Path(v).absolute()) for k, v in overrides.items()})
            if family_destinations is not None:
                workspace["family_destinations"] = {k: str(Path(v).absolute()) for k, v in family_destinations.items()}
            # One publication for permanent destinations and tier policy. The
            # same validation also protects old Fast generations for legacy callers.
            value = prepare_policy_update(value, {} if storage_policy is None else storage_policy,
                protected_roots=self.protected_roots, managed_roots=managed_roots)
            self.registry._save(value, token)

    def attach_swarm_model_evidence(self, resource_id, claim, inspection, *,
                                    expected_registry, should_cancel=None):
        """Append one reviewed Swarm source claim without changing user authority.

        The importer holds the selected-file and ancestor pins through this
        registry publication. This writer still checks exact bytes and revision.
        """
        from .swarm_model_import import MAX_EVIDENCE_ROWS, validate_swarm_claim
        validate_swarm_claim(claim, resource_id=resource_id)
        if not isinstance(inspection, ResourceInspection) or inspection.resource_id != resource_id:
            raise ModelResourceError("The selected SwarmUI model inspection is missing or mismatched.")
        _path(inspection.path, self.protected_roots)
        return self._append_swarm_model_evidence(resource_id, claim, inspection,
            expected_registry=expected_registry, should_cancel=should_cancel)

    def attach_swarm_model_evidence_from_cache(self, cache_review, *, build_claim,
                                              expected_registry, should_cancel=None, progress=None):
        """Own the issued cache guard through source review and canonical publication."""
        from .model_cache_reference import CacheReferenceReview
        if (self.cache_references is None or not isinstance(cache_review, CacheReferenceReview)
                or cache_review.registry_token != expected_registry or not callable(build_claim)):
            raise ModelResourceError("Use an issued Fast source review with its trusted cache owner.")
        with self.cache_references.pinned(cache_review, should_cancel=should_cancel, progress=progress) as verified:
            claim = build_claim(deepcopy(verified.inspection))
            return self._append_swarm_model_evidence(cache_review.resource_id, claim, verified.inspection,
                expected_registry=expected_registry, should_cancel=should_cancel,
                cache_entry_id=cache_review.entry_id)

    def _append_swarm_model_evidence(self, resource_id, claim, inspection, *, expected_registry,
                                     should_cancel=None, cache_entry_id=None):
        from .swarm_model_import import MAX_EVIDENCE_ROWS, validate_swarm_claim
        validate_swarm_claim(claim, resource_id=resource_id)
        _cancel(should_cancel)
        _current(inspection)
        with self.registry._writer():
            value, token = self.registry.snapshot()
            if token != expected_registry:
                raise ModelResourceError("The model registry changed after SwarmUI metadata review.")
            record = value["resources"].get(resource_id)
            if record is None or record["identity"] != inspection.stored_identity():
                raise ModelResourceError("The selected SwarmUI model no longer matches its registered identity.")
            key = os.path.normcase(os.path.abspath(inspection.path))
            locations = [row for row in record["locations"] if os.path.normcase(os.path.abspath(row["path"])) == key]
            if len(locations) != 1:
                raise ModelResourceError("The selected SwarmUI model location is no longer registered.")
            if (cache_entry_id is None and "cache_entry_id" in locations[0]
                    or cache_entry_id is not None and locations[0] != {
                        "path": str(inspection.path), "file_state": list(inspection.file_state), "cache_entry_id": cache_entry_id}):
                raise ModelResourceError("A Fast location requires its exact issued cache publication guard.")
            claims = record.setdefault("swarm_model_evidence", [])
            if not isinstance(claims, list) or len(claims) > MAX_EVIDENCE_ROWS:
                raise ModelResourceError("Existing SwarmUI model evidence needs review.")
            for prior in claims:
                validate_swarm_claim(prior, resource_id=resource_id)
            if any(isinstance(row, dict) and row.get("source_digest") == claim["source_digest"] for row in claims):
                return token
            if len(claims) >= MAX_EVIDENCE_ROWS:
                raise ModelResourceError("This model has reached its retained SwarmUI evidence limit.")
            _cancel(should_cancel)
            _current(inspection)
            claims.append(deepcopy(claim))
            self.registry._save(value, token)
            current, saved_token = self.registry.snapshot()
            if current != value:
                raise ModelResourceError("SwarmUI evidence publication did not read back exactly.")
            return saved_token

    def destination(self, snapshot, *, role="", family="", media_type="Images"):
        return self.destination_details(snapshot, role=role, family=family, media_type=media_type)[0]

    def destination_details(self, snapshot, *, role="", family="", media_type="Images"):
        return destination_path(snapshot.get("workspace", {}), role=role,
                                family=family, media_type=media_type)

    def record_derivation(self, output_id, *, parent_ids, operation, tool, tool_version,
                          settings, references, expected_registry, should_cancel=None):
        """Record a reviewed attribution, never manufacture an execution receipt."""
        if operation not in ("fine_tune", "merge", "conversion", "quantization"):
            raise ModelResourceError("Choose a recognized derived-model operation.")
        if not 1 <= len(parent_ids) <= 64 or len(set(parent_ids)) != len(parent_ids) or output_id in parent_ids:
            raise ModelResourceError("Use 1–64 distinct exact parent identities, separate from the output.")
        if not tool.strip() or not tool_version.strip() or max(len(tool), len(tool_version)) > 200:
            raise ModelResourceError("Record the exact tool and runtime version.")
        raw = json.dumps(settings, sort_keys=True, allow_nan=False).encode("utf-8")
        if not isinstance(settings, dict) or len(raw) > 128 * 1024:
            raise ModelResourceError("Operation settings must be a bounded JSON object.")
        if not isinstance(references, list) or len(references) > 100:
            raise ModelResourceError("Use at most 100 explicit identity references.")
        allowed = ("image", "generation_record", "benchmark", "dataset", "training_manifest", "evaluation")
        for ref in references:
            if not isinstance(ref, dict) or ref.get("kind") not in allowed or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(ref.get("identity", ""))):
                raise ModelResourceError("References need a supported kind and exact content identity.")
        snapshot, token = self.registry.snapshot()
        if token != expected_registry:
            raise ModelResourceError("The registry changed after preview; reload and retry.")
        # Avoid cycles through existing attributed or verified lineage.
        def ancestors(identity, visited):
            pending = [identity]
            while pending:
                current = pending.pop()
                if current in visited: continue
                visited.add(current)
                for claim in snapshot["resources"].get(current, {}).get("lineage", []):
                    pending.extend(claim.get("parent_ids", []))
        for parent in parent_ids:
            found = set(); ancestors(parent, found)
            if output_id in found: raise ModelResourceError("This attribution would create a lineage cycle.")
        verified = []
        for identity in [output_id, *parent_ids]:
            for location in snapshot["resources"].get(identity, {}).get("locations", []):
                _path(location["path"], self.protected_roots)
            result = self.registry.resolve(identity, should_cancel=should_cancel)
            if result.status != "identity_verified": raise ModelResourceError(result.reason)
            _path(result.inspection.path, self.protected_roots)
            verified.append(result.inspection)
        with self.registry._writer():
            value, current = self.registry.snapshot()
            if current != token: raise ModelResourceError("The registry changed while verifying lineage.")
            record = value["resources"][output_id]
            if record["lineage"]:
                raise ModelResourceError("This output already has lineage; review its existing attribution instead of replacing it.")
            claim = {"operation": operation, "parent_ids": list(parent_ids), "tool": tool,
                     "tool_version": tool_version, "settings": deepcopy(settings),
                     "settings_sha256": hashlib.sha256(raw).hexdigest(), "output_id": output_id,
                     "authority": "user_attributed", "execution_receipt": "unavailable",
                     "quality": "unmeasured", "references": deepcopy(references)}
            record["lineage"].append(claim)
            record.setdefault("references", []).extend(deepcopy(references))
            _cancel(should_cancel)
            for inspection in verified: _current(inspection)
            self.registry._save(value, current)


def validate_download_url(url, *, provider=None):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    # Primary provider documentation, reviewed 2026-09-14. Unknown CDNs stay held.
    allowed = host in ("civitai.com", "b2.civitai.com", "huggingface.co",
        "cdn-lfs.huggingface.co", "cdn-lfs-us-1.huggingface.co", "cdn-lfs-eu-1.huggingface.co",
        "cdn-lfs-us-1.hf.co", "cdn-lfs-eu-1.hf.co", "cas-bridge.xethub.hf.co",
        "cas-server.xethub.hf.co", "cas-server.xethub-eu.hf.co", "transfer.xethub.hf.co",
        "transfer.xethub-eu.hf.co", "us.aws.cdn.hf.co", "us.gcp.cdn.hf.co")
    if parsed.scheme != "https" or not allowed or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ModelResourceError("Download URL is outside the supported HTTPS provider hosts.")
    if provider is not None:
        expected = "civitai" if host in ("civitai.com", "b2.civitai.com") else "hugging_face"
        if provider != expected:
            raise ModelResourceError("Download redirect differs from the reviewed provider.")
    return url


class _DownloadRedirect(HTTPRedirectHandler):
    max_repeats = 2
    max_redirections = 5
    def __init__(self, provider):
        super().__init__()
        self.provider = provider
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_download_url(newurl, provider=self.provider)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _stream(url, should_cancel):
    validate_download_url(url)
    provider = "civitai" if urlsplit(url).hostname == "civitai.com" else "hugging_face"
    with build_opener(_DownloadRedirect(provider)).open(Request(url, headers={"User-Agent": "CollectiGen-model-acquisition/1"}), timeout=20) as response:
        validate_download_url(response.geturl(), provider=provider)
        while True:
            _cancel(should_cancel)
            block = response.read(1024 * 1024)
            if not block:
                break
            yield block


@dataclass(frozen=True)
class AcquisitionPlan:
    provider: str
    version: str
    url: str
    sha256: str
    size: int
    destination: Path
    terms: str
    parent_state: tuple
    creation_root: Path | None = None

    def receipt(self):
        return {"schema_version": 1, "provider": self.provider, "version": self.version,
                "url": self.url, "sha256": self.sha256, "size": self.size,
                "destination": str(self.destination), "terms": self.terms}


def preview_acquisition(*, provider, version, url, sha256, size, destination, terms, protected_roots=()):
    validate_download_url(url)
    parsed = urlsplit(url)
    if parsed.fragment or (parsed.query and (provider != "civitai" or set(parse_qs(parsed.query)) - {"type", "format", "size"})):
        raise ModelResourceError("Use a public stable download link without credential or signed query parameters.")
    if provider not in ("civitai", "hugging_face") or not version or not terms.strip():
        raise ModelResourceError("Review the exact provider version and terms before acquisition.")
    if provider == "civitai":
        if parsed.hostname != "civitai.com" or parsed.path.rstrip("/") != f"/api/download/models/{version}" or not str(version).isdigit():
            raise ModelResourceError("The Civitai download URL does not identify the reviewed version.")
    else:
        parts = parsed.path.strip("/").split("/")
        if parsed.hostname != "huggingface.co" or len(parts) < 5 or parts[2] != "resolve" or unquote(parts[3]) != version or not re.fullmatch(r"[0-9a-f]{40}", version):
            raise ModelResourceError("Use the Hugging Face file URL at its exact reviewed commit revision.")
    if not isinstance(sha256, str) or not re.fullmatch("[0-9a-f]{64}", sha256) or type(size) is not int or not 0 < size <= 1024**4:
        raise ModelResourceError("Acquisition requires an exact full-file SHA-256 and byte size.")
    destination = _path(destination, protected_roots)
    if destination.suffix.casefold() != ".safetensors" or destination.exists():
        raise ModelResourceError("Choose a new .safetensors destination; existing files are never replaced.")
    creation_root = destination.parent
    while not creation_root.exists() and creation_root != creation_root.parent:
        creation_root = creation_root.parent
    _path(creation_root, protected_roots)
    if not creation_root.is_dir():
        raise ModelResourceError("Choose a model destination under an existing folder.")
    if shutil.disk_usage(creation_root).free < size + 16 * 1024**2:
        raise ModelResourceError("Insufficient storage; choose another model destination.")
    state = creation_root.stat(follow_symlinks=False)
    return AcquisitionPlan(provider, str(version), url, sha256, size, destination, terms,
                           (state.st_dev, state.st_ino), creation_root)


@contextmanager
def _pinned_parent(parent, protected_roots):
    """Hold Windows ancestor directory identities against rename/delete."""
    parent = _path(parent, protected_roots)
    handles = []
    try:
        if os.name == "nt":
            from .training_windows_file_identity import _api, _from_handle
            ctypes, _, create, _, close = _api()
            for directory in reversed((parent, *parent.parents)):
                _assert_unredirected(directory)
                before = directory.stat(follow_symlinks=False)
                handle = create(str(directory), 0x80, 3, None, 3, 0x02200000, None)
                if handle == ctypes.c_void_p(-1).value:
                    raise ctypes.WinError(ctypes.get_last_error())
                handles.append(handle)
                _from_handle(handle, before)
                _assert_unredirected(directory)
        yield
    finally:
        if os.name == "nt":
            for handle in reversed(handles): close(handle)


def acquire_model(plan, *, confirmed=False, should_cancel=None, progress=None, stream=None, protected_roots=(), operation_started=None):
    if not confirmed:
        raise ModelResourceError("Confirm the exact acquisition preview before downloading.")
    if os.name != "nt":
        raise ModelResourceError("Safe model publication currently requires the qualified Windows file-sharing boundary.")
    if plan.creation_root != plan.destination.parent:
        refreshed = preview_acquisition(provider=plan.provider, version=plan.version, url=plan.url,
            sha256=plan.sha256, size=plan.size, destination=plan.destination, terms=plan.terms,
            protected_roots=protected_roots)
        if refreshed != plan:
            raise ModelResourceError("The destination changed since preview; preview again.")
        with _pinned_parent(plan.creation_root, protected_roots):
            state = plan.creation_root.stat(follow_symlinks=False)
            if (state.st_dev, state.st_ino) != plan.parent_state:
                raise ModelResourceError("The model destination changed since preview; preview again.")
            pending = []
            cursor = plan.destination.parent
            while cursor != plan.creation_root:
                pending.append(cursor)
                cursor = cursor.parent
            for directory in reversed(pending):
                _cancel(should_cancel)
                _path(directory, protected_roots)
                directory.mkdir(exist_ok=True)
                _path(directory, protected_roots)
        plan = preview_acquisition(provider=plan.provider, version=plan.version, url=plan.url,
            sha256=plan.sha256, size=plan.size, destination=plan.destination, terms=plan.terms,
            protected_roots=protected_roots)
    with _pinned_parent(plan.destination.parent, protected_roots):
        return _acquire_model(plan, confirmed=confirmed, should_cancel=should_cancel, progress=progress,
            stream=stream, protected_roots=protected_roots, operation_started=operation_started)


@contextmanager
def _pinned_model_file(path, protected_roots):
    """Keep verified bytes and their name stable through Windows publication."""
    if os.name != "nt":
        raise ModelResourceError("Safe model publication currently requires the qualified Windows file-sharing boundary.")
    from .training_windows_file_identity import _api, _from_handle
    path = _path(path, protected_roots)
    with _pinned_parent(path.parent, protected_roots):
        before = path.stat(follow_symlinks=False)
        ctypes, _, create, _, close = _api()
        # GENERIC_READ, FILE_SHARE_READ only: deny existing/new writers and
        # deletion/rename through every hard link while allowing inspection.
        handle = create(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            _from_handle(handle, before)
            _path(path, protected_roots)
            _from_handle(handle, path.stat(follow_symlinks=False))
            yield
        finally:
            close(handle)


def _acquisition_operation_reference(attempt, protected_roots):
    from .model_resource_registry import _bounded_read
    attempt = _path(attempt, protected_roots)
    path = _path(attempt / "operation.json", protected_roots)
    raw = _bounded_read(path, 32 * 1024)
    if raw is None:
        raise ModelResourceError("This legacy acquisition has no immutable operation receipt; binding is unavailable.")
    try:
        baseline = json.loads(raw)
        operation = baseline["operation_id"]
        receipt = json.loads(_bounded_read(attempt / "receipt.json", 16 * 1024) or b"null")
        if (set(baseline) != {"schema_version", "operation_id", "plan"}
                or type(baseline["schema_version"]) is not int or baseline["schema_version"] != 1 or not isinstance(operation, str)
                or not re.fullmatch("[0-9a-f]{32}", operation)
                or attempt.name != ".collectigen-acquisition-" + operation
                or not isinstance(receipt, dict)
                or baseline["plan"] != {key: value for key, value in receipt.items() if key != "status"}):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ModelResourceError("Acquisition operation evidence is damaged or changed; preserve it for review.") from None
    return _immutable_operation_reference("model_acquisition", operation, path, raw, kind="acquire")


def _bind_acquisition_operation(attempt, protected_roots, callback):
    if callback is not None:
        reference = _acquisition_operation_reference(attempt, protected_roots)
        _notify_operation_started(callback, reference)
        if _acquisition_operation_reference(attempt, protected_roots) != reference:
            raise ModelResourceError("Acquisition operation evidence changed while binding; inspect recovery.")


def _acquire_model(plan, *, confirmed=False, should_cancel=None, progress=None, stream=None, protected_roots=(), operation_started=None):
    """Stage exact bytes, then publish with a no-clobber link; retain failed attempts.

    Interrupted/cancelled attempts remain receipt-addressed for inspection. Retry
    starts a fresh bounded attempt; it never appends unverified partial content.
    Registration and model execution require their own explicit actions.
    """
    if not confirmed:
        raise ModelResourceError("Confirm the exact acquisition preview before downloading.")
    refreshed = preview_acquisition(provider=plan.provider, version=plan.version, url=plan.url,
        sha256=plan.sha256, size=plan.size, destination=plan.destination, terms=plan.terms,
        protected_roots=protected_roots)
    if refreshed != plan:
        raise ModelResourceError("The destination changed since preview; preview again.")
    _cancel(should_cancel)
    operation = uuid4().hex
    attempt = plan.destination.parent / (".collectigen-acquisition-" + operation)
    _path(plan.destination.parent, protected_roots)
    state = plan.destination.parent.stat(follow_symlinks=False)
    if (state.st_dev, state.st_ino) != plan.parent_state:
        raise ModelResourceError("The model destination changed before staging.")
    attempt.mkdir()
    _path(attempt, protected_roots)
    state = plan.destination.parent.stat(follow_symlinks=False)
    if (state.st_dev, state.st_ino) != plan.parent_state:
        raise ModelResourceError("The model destination changed before any staged bytes were written.")
    staged = attempt / plan.destination.name
    receipt = dict(plan.receipt(), status="staging")
    # Destination/attempt directories can precede this immutable identity. They
    # are retained empty scaffolding, never inferred operation authority.
    write_verified_json(attempt / "operation.json", {
        "schema_version": 1, "operation_id": operation, "plan": plan.receipt()})
    write_verified_json(attempt / "receipt.json", receipt)
    try:
        _bind_acquisition_operation(attempt, protected_roots, operation_started)
        _cancel(should_cancel)
        total, digest = 0, hashlib.sha256()
        with staged.open("xb") as handle:
            for block in (stream or _stream)(plan.url, should_cancel):
                _cancel(should_cancel)
                total += len(block)
                if total > plan.size:
                    raise ModelResourceError("Provider bytes exceed the reviewed size; staged data retained.")
                handle.write(block); digest.update(block)
                if progress:
                    progress(total, plan.size, "Downloading selected model")
            handle.flush(); os.fsync(handle.fileno())
        if total != plan.size or digest.hexdigest() != plan.sha256:
            raise ModelResourceError("Download size or identity changed; staged data retained for review.")
        with _pinned_model_file(staged, protected_roots):
            inspection = inspect_model_resource(staged, should_cancel=should_cancel, progress=progress)
            if inspection.resource_id != "sha256:" + plan.sha256:
                raise ModelResourceError("Staged model identity changed.")
            _cancel(should_cancel)
            _path(plan.destination, protected_roots)
            state = plan.destination.parent.stat(follow_symlinks=False)
            if (state.st_dev, state.st_ino) != plan.parent_state:
                raise ModelResourceError("The model destination changed during download.")
            receipt["status"] = "verified_staged"
            write_verified_json(attempt / "receipt.json", receipt)
            _current(inspection)
            # Atomic, same-volume, no overwrite; retain the stable stage for recovery.
            os.link(staged, plan.destination)
            result = inspect_model_resource(plan.destination, should_cancel=None)
            if result.resource_id != inspection.resource_id:
                raise ModelResourceError("Published model verification failed; review the retained attempt.")
            receipt["status"] = "complete"
            write_verified_json(attempt / "receipt.json", receipt)
            return result
    except Exception:
        # Do not mask the original fault if recording it also fails.
        try:
            receipt["status"] = "held_for_review"
            write_verified_json(attempt / "receipt.json", receipt)
        except OSError:
            pass
        raise


@dataclass(frozen=True)
class AcquisitionRecovery:
    attempt: Path
    destination: Path
    staged: Path
    sha256: str
    size: int
    receipt_sha256: str
    destination_present: bool


def preview_acquisition_recovery(attempt, *, protected_roots=(), should_cancel=None):
    from .model_resource_registry import _bounded_read
    attempt = _path(attempt, protected_roots)
    raw = _bounded_read(attempt / "receipt.json", 16 * 1024)
    if raw is None: raise ModelResourceError("Choose a retained acquisition attempt containing its receipt.")
    try:
        receipt = json.loads(raw)
        if receipt.get("schema_version") != 1: raise ValueError()
        destination = _path(receipt["destination"], protected_roots)
        size, digest = receipt["size"], receipt["sha256"]
        if type(size) is not int or not 0 < size <= 1024**4 or not re.fullmatch("[0-9a-f]{64}", digest): raise ValueError()
        # Attempts can recover only their sibling destination, never a receipt-
        # supplied arbitrary external location.
        if destination.parent != attempt.parent or destination.suffix.casefold() != ".safetensors": raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ModelResourceError("Acquisition receipt is damaged or outside its original destination.") from None
    staged = _path(attempt / destination.name, protected_roots)
    inspection = inspect_model_resource(staged, should_cancel=should_cancel)
    if inspection.resource_id != "sha256:" + digest or inspection.size != size:
        raise ModelResourceError("The retained staged bytes are incomplete or changed. Retry from a fresh provider preview.")
    present = destination.exists()
    if present:
        output = inspect_model_resource(destination, should_cancel=should_cancel)
        if output.resource_id != inspection.resource_id:
            raise ModelResourceError("An unrelated destination exists; recovery will never overwrite it.")
    return AcquisitionRecovery(attempt, destination, staged, digest, size, hashlib.sha256(raw).hexdigest(), present)


def recover_acquisition(preview, *, confirmed=False, protected_roots=(), should_cancel=None, operation_started=None):
    if not confirmed: raise ModelResourceError("Confirm the exact recovery preview first.")
    with _pinned_parent(preview.destination.parent, protected_roots):
        return _recover_acquisition(preview, confirmed=True, protected_roots=protected_roots, should_cancel=should_cancel,
            operation_started=operation_started)


def _recover_acquisition(preview, *, confirmed=False, protected_roots=(), should_cancel=None, operation_started=None):
    if not confirmed: raise ModelResourceError("Confirm the exact recovery preview first.")
    with ExitStack() as pins:
        pins.enter_context(_pinned_model_file(preview.staged, protected_roots))
        if preview.destination_present:
            pins.enter_context(_pinned_model_file(preview.destination, protected_roots))
        return _publish_recovery(preview, protected_roots=protected_roots, should_cancel=should_cancel,
            operation_started=operation_started)


def _publish_recovery(preview, *, protected_roots=(), should_cancel=None, operation_started=None):
    fresh = preview_acquisition_recovery(preview.attempt, protected_roots=protected_roots, should_cancel=should_cancel)
    if fresh != preview: raise ModelResourceError("The acquisition changed after recovery preview; inspect it again.")
    _cancel(should_cancel)
    _bind_acquisition_operation(preview.attempt, protected_roots, operation_started)
    _cancel(should_cancel)
    if not preview.destination_present: os.link(preview.staged, preview.destination)
    inspection = inspect_model_resource(preview.destination)
    if inspection.resource_id != "sha256:" + preview.sha256:
        raise ModelResourceError("Recovered output identity differs; review the retained attempt.")
    # Preserve the original receipt as immutable history. Recovery is an
    # additional receipt and leaves unrelated files and attempts untouched.
    write_verified_json(preview.attempt / ("recovery-" + uuid4().hex + ".json"), {
        "schema_version": 1, "original_receipt_sha256": preview.receipt_sha256,
        "status": "complete", "resource_id": inspection.resource_id,
    })
    return inspection
