"""Selected Collection versions and current member proofs for shared History."""
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path

from .concept_groups import ConceptGroupError, MAX_MEMBERS, _key, _member, prepare_rule_update, resolve_members
from .federated_dataset import LibraryImageIdentity
from .training_source_io import _assert_unredirected


@dataclass(frozen=True)
class CollectionMemberChoice:
    key: tuple[str, str]
    historical_json: str
    current_json: str


@dataclass(frozen=True)
class CollectionMemberChoices:
    rows: tuple[CollectionMemberChoice, ...]
    total: int
    next_offset: int | None


@dataclass(frozen=True)
class CollectionComparison:
    plan: object
    prepared_rules: object
    adapter: object
    historical_json: str


@dataclass(frozen=True)
class CollectionObservationComparison:
    plan: object
    adapter: object


@dataclass(frozen=True)
class CollectionHistoryOutcome:
    group_id: str
    status: str
    message: str
    group_json: str = ""


def _decisions(group):
    if group is None:
        return {}
    return {_key(row): {"status": status, "row": row}
        for status, values in (("active", group["members"]), ("ignored", group.get("ignored_members", ())))
        for row in values}


def _text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


@contextmanager
def pinned_collection_members(rows, dataset, *, scope_current, cancelled=lambda: False):
    """Hold exact Windows image bytes/paths while canonical membership is saved."""
    if not scope_current():
        raise ConceptGroupError("Dataset scope changed. Reopen this Collection history.")
    witnesses = {}
    for value in rows:
        row = _member(value)
        key = _key(row)
        if key in witnesses and witnesses[key]["image_sha256"] != row["image_sha256"]:
            raise ConceptGroupError("Selected historical and current members refer to different image bytes.")
        witnesses[key] = row
    if len(witnesses) > MAX_MEMBERS:
        raise ConceptGroupError("The exact membership proof exceeds the supported Collection scope.")
    if not witnesses:
        yield
        return
    if os.name != "nt":
        raise ConceptGroupError("Exact membership restoration currently requires qualified Windows file-sharing guards.")
    from .training_windows_file_identity import _api, _from_handle
    from .library_storage import LibraryStorageInspector
    ctypes, _, create, _, close = _api()
    handles = []
    paths = {}
    try:
        for row in witnesses.values():
            if cancelled():
                raise InterruptedError("Collection member verification cancelled.")
            identity = LibraryImageIdentity(row["library_id"], row["relative_path"])
            path = Path(dataset.path_for_identity(identity)).absolute()
            if dataset.identity_for_path(path) != identity:
                raise ConceptGroupError("A selected member no longer has its exact library identity.")
            paths[path] = row
        parents = {parent for path in paths for parent in path.parents}
        for parent in sorted(parents, key=lambda path: (len(path.parts), str(path).casefold())):
            if cancelled():
                raise InterruptedError("Collection member verification cancelled.")
            _assert_unredirected(parent)
            before = parent.stat(follow_symlinks=False)
            handle = create(str(parent), 0x80, 3, None, 3, 0x02200000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            handles.append(handle)
            _from_handle(handle, before)
        inspector = LibraryStorageInspector()
        for path in sorted(paths, key=lambda value: str(value).casefold()):
            if cancelled():
                raise InterruptedError("Collection member verification cancelled.")
            _assert_unredirected(path)
            inspector.assert_locally_readable(path)
            before = path.stat(follow_symlinks=False)
            handle = create(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            handles.append(handle)
            _from_handle(handle, before)
        result = resolve_members({"members": tuple(witnesses.values())}, dataset, should_cancel=cancelled)
        if result["issues"]:
            raise ConceptGroupError("Exact Collection member proof is unavailable: " + "; ".join(result["issues"][:5]))
        if not scope_current():
            raise ConceptGroupError("Dataset scope changed during member verification.")
        yield
    finally:
        for handle in reversed(handles):
            close(handle)


class CollectionHistoryBrowser:
    domain_key = "collections"
    label = "Training Collection"

    def __init__(self, configuration, canonical, group_id, dataset, *, scope_current, observation_only=False):
        self.configuration, self.canonical, self.group_id = configuration, canonical, group_id
        self.dataset, self.scope_current = dataset, scope_current
        self._versions = {}
        self._comparison = None
        self._observation = None
        self.observation_only = bool(observation_only)

    def _adapter(self, *, write=False):
        from .collection_history import CollectionHistoryAdapter
        descriptor = self.configuration.resolve("collections") if write else self.configuration.read_descriptor("collections")
        expected = "configured" if write else "retained"
        if descriptor.status != expected:
            raise ConceptGroupError(descriptor.reason)
        adapter = CollectionHistoryAdapter(self.configuration.store(descriptor), authority_path=self.canonical.path)
        binding = json.loads(descriptor.binding_json)
        declared = Path(binding["domains"]["collections"]["authorities"]["collections"])
        if declared != self.canonical.path:
            raise ConceptGroupError("The selected Collection store differs from its configured authority.")
        return adapter

    def invalidate(self):
        self._comparison = None
        self._observation = None

    def update_index(self, *, cancelled=lambda: False):
        ready = True
        for write in (False, True):
            if cancelled():
                raise InterruptedError("Collection history indexing cancelled.")
            if write and self.configuration.resolve("collections").status != "configured":
                continue
            ready = self._adapter(write=write).history.refresh_index(max_events=256) and ready
        return ready

    def page(self, cursor=None, *, cancelled=lambda: False):
        if cancelled():
            raise InterruptedError("Collection history loading cancelled.")
        adapter = self._adapter()
        page = adapter.page(self.group_id, cursor=cursor, limit=50)
        if cursor is None:
            self._versions.clear()
            self.invalidate()
        if len(self._versions) + len(page.versions) > 2000:
            raise ConceptGroupError("This Collection view reached 2,000 versions; narrow its recorded operation scope.")
        self._versions.update((version.version_id, version) for version in page.versions)
        return page

    def _historical(self, version, adapter):
        if self._versions.get(version.version_id) != version or version.entity.key != self.group_id:
            raise ConceptGroupError("Reload the exact selected Collection version.")
        return adapter._group(adapter.history.payload(version))

    def member_page(self, version, *, offset=0, limit=100):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ConceptGroupError("Choose a bounded membership page.")
        adapter = self._adapter()
        self._historical(version, adapter)
        page = adapter.member_page(version, canonical=self.canonical, offset=offset, limit=limit)
        return CollectionMemberChoices(tuple(CollectionMemberChoice(row.key, row.proposed_json, row.current_json)
            for row in page.rows), page.total, page.next_offset)

    def preview(self, version, *, fields=(), member_keys=(), overwrite=False, cancelled=lambda: False):
        self.invalidate()
        if self.observation_only:
            raise ConceptGroupError("This view records current saved state only; unsaved editor choices stay separate.")
        if cancelled():
            raise InterruptedError("Collection comparison cancelled.")
        if not self.scope_current():
            raise ConceptGroupError("Dataset scope changed. Reopen this Collection history.")
        adapter = self._adapter(write=self.configuration.resolve("collections").status == "configured")
        historical = self._historical(version, adapter)
        if historical is None:
            raise ConceptGroupError("This version records absence of the Collection; native deletion is not qualified.")
        current = self.canonical.snapshot()[0]["groups"].get(self.group_id)
        if current is None:
            raise ConceptGroupError("The selected Collection no longer exists.")
        rules = historical.get("rules") if "rules" in fields else current.get("rules") if member_keys else None
        prepared = prepare_rule_update(current, rules, self.dataset, should_cancel=cancelled) if rules is not None else None
        def proof(rows):
            return pinned_collection_members(rows, self.dataset, scope_current=self.scope_current, cancelled=cancelled)
        plan = adapter.plan_restore(self.canonical, version, fields=fields, member_keys=member_keys,
            overwrite=overwrite, proof=proof, prepared_rules=prepared, dataset=self.dataset)
        self._comparison = CollectionComparison(plan, prepared, adapter, _text(historical))
        return self._comparison

    def apply(self, comparison, *, cancelled=lambda: False):
        if self.observation_only:
            raise ConceptGroupError("Close current-state review before restoring saved Collection decisions.")
        if comparison is not self._comparison or comparison is None:
            raise ConceptGroupError("The exact Collection comparison is no longer current.")
        self.invalidate()
        if cancelled():
            return (CollectionHistoryOutcome(self.group_id, "cancelled", "Cancelled before saving."),)
        if not self.scope_current():
            raise ConceptGroupError("Dataset scope changed. Reopen this Collection history.")
        def proof(rows):
            return pinned_collection_members(rows, self.dataset, scope_current=self.scope_current, cancelled=cancelled)
        group = comparison.adapter.apply_restore(self.canonical, comparison.plan, proof=proof,
            prepared_rules=comparison.prepared_rules, dataset=self.dataset)
        return (CollectionHistoryOutcome(self.group_id, "complete", "Selected Collection decisions saved as a new version; frozen training artifacts are unchanged.", _text(group)),)

    def preview_observation(self, *, cancelled=lambda: False):
        self.invalidate()
        if cancelled():
            raise InterruptedError("Current-state review cancelled.")
        if not self.scope_current():
            raise ConceptGroupError("Dataset scope changed. Reopen this Collection history.")
        adapter = self._adapter(write=True)
        plan = adapter.plan_observation(self.canonical, self.group_id)
        if cancelled():
            raise InterruptedError("Current-state review cancelled.")
        self._observation = CollectionObservationComparison(plan, adapter)
        return self._observation

    def confirm_observation(self, comparison, *, cancelled=lambda: False):
        if comparison is None or comparison is not self._observation:
            raise ConceptGroupError("Review the exact current saved Collection before recording it.")
        if cancelled():
            raise InterruptedError("Cancelled before recording current state.")
        if not self.scope_current():
            raise ConceptGroupError("Dataset scope changed. Reopen this Collection history.")
        # Retain this exact operation on an uncertain append result. The adapter
        # rechecks the same plan and recognizes its already-recorded operation.
        result = comparison.adapter.confirm_observation(self.canonical, comparison.plan)
        self.invalidate()
        return result
