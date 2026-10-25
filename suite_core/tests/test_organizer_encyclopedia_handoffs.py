"""Receiver component evidence only: fake host/admission and exact native routes.

No QApplication, profile, filesystem media, model, scan, save or apply is opened.
"""
import ast
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.organizer_encyclopedia_handoffs import (
    AdmittedCatalog, AdmittedTag, CanonicalRenameRequest,
    HandoffHold, MatchingImagesRequest, admit_request, decode_request,
)
from suite_core.media_organizer.contracts import ExistingContext, SERVICE_NAMES
from suite_core.media_organizer.facade import MediaOrganizer
from suite_core.media_organizer.encyclopedia_handoff_entry import (
    OrganizerEncyclopediaHandoffReceiver, install_encyclopedia_handoff_receiver,
)


class Host:
    def __init__(self):
        self.settings = object()
        self.state = SimpleNamespace(settings=self.settings,
                                     **{name: object() for name in SERVICE_NAMES})
        self.startup_cancelled = False
        self.current = 0
        self.decline = False
        self.stopping = False
        self.calls = []
        self.pending = []
        self.navigation = SimpleNamespace(setCurrentRow=self.navigate)
        self.stack = SimpleNamespace(currentIndex=lambda: self.current)
        self.organizer = MediaOrganizer(ExistingContext(self.settings, self.state), self)
        self.catalog = AdmittedCatalog(self.state.knowledge, "catalog-a", "r2",
                                      (AdmittedTag("tag-a", "new name", ("old name",)),))
        self.reader = lambda state: self.catalog
        self.receiver = OrganizerEncyclopediaHandoffReceiver(self, read_admitted_catalog=self.reader)

    def navigate(self, index):
        if not self.decline:
            self.current = index

    def choose_library(self): pass
    def open_completed_dataset_image(self, path): pass
    def edit_image_data(self, path): pass
    def close(self): pass

    def _queue_dataset_page_action(self, action):
        if self.stopping:
            return False
        if not hasattr(self, "dataset_page"):
            self.pending.append(action)
            return True
        return action()

    def ready(self):
        def show_tag(display):
            self.calls.append(("display_name_search", display))
        def open_function(function, scope):
            self.calls.append((function, deepcopy(scope)))
            correction._matching_scope = deepcopy(scope)
            return True
        correction = SimpleNamespace(has_active_background_work=lambda: False, _matching_scope={})
        self.dataset_page = SimpleNamespace(show_tag=show_tag, open_function=open_function,
                                            image_identity_correction=correction)
        pending, self.pending = self.pending, []
        return [action() for action in pending]


# Execute original native command bodies with fake globals, retaining their real
# queue/name-search/payload-copy behavior without importing the application.
_source = ast.parse((Path(__file__).resolve().parents[2] / "qt_app/main_window.py")
                    .read_text(encoding="utf-8-sig"))
_owner = next(node for node in _source.body if isinstance(node, ast.ClassDef)
              and node.name == "TagManagerWindow")
_namespace = {"TagManagerWindow": Host, "deepcopy": deepcopy}
for _name in ("open_dataset_tag", "open_dataset_function"):
    _method = next(node for node in _owner.body if isinstance(node, ast.FunctionDef)
                   and node.name == _name)
    exec(compile(ast.Module(body=[_method], type_ignores=[]), "native-organizer-route", "exec"), _namespace)
    setattr(Host, _name, _namespace[_name])


def search(request_id="search-1", **changes):
    values = dict(request_id=request_id, catalog_id="catalog-a", revision="r2", stable_id="tag-a")
    return MatchingImagesRequest(**(values | changes))


def rename(request_id="rename-1", **changes):
    values = dict(request_id=request_id, catalog_id="catalog-a", before_revision="r1",
                  after_revision="r2", stable_id="tag-a", previous_display="old name",
                  new_display="new name")
    return CanonicalRenameRequest(**(values | changes))


@pytest.fixture
def host():
    return Host()


@pytest.mark.parametrize("factory", [search, rename])
def test_lazy_dispatch_reuses_native_routes_without_scanning_or_applying(host, factory):
    assert host.receiver.receive(factory())
    assert host.calls == [] and len(host.pending) == 1
    assert host.ready() == [True]
    assert len(host.calls) == 1
    if factory is search:
        assert host.calls[0] == ("display_name_search", "new name")
    else:
        function, scope = host.calls[0]
        assert function == "dataset.image_facts.update_matching"
        assert scope == {
            "kind": "canonical_rename", "origin": "tag_encyclopedia",
            "stable_ids": ("tag-a",), "source_names": ("old name",),
            "label": 'Review image Tag names for "new name"',
            "selected_kind": "tag", "selected_id": "tag-a",
        }
    # The fake services are plain objects: a receiver scan/apply/write call would
    # fail. Only the original search/review opening methods above were invoked.


@pytest.mark.parametrize("factory", [search, rename])
def test_declined_navigation_is_not_consumed_and_can_be_explicitly_retried(host, factory):
    host.decline = True
    assert host.receiver.receive(factory()) is False
    assert host.pending == [] and host.calls == [] and host.current == 0
    host.decline = False
    assert host.receiver.receive(factory())
    host.ready()
    assert len(host.calls) == 1


@pytest.mark.parametrize("factory", [search, rename])
def test_duplicate_request_is_held_while_queued_and_after_dispatch(host, factory):
    assert host.receiver.receive(factory())
    with pytest.raises(HandoffHold, match="duplicate"):
        host.receiver.receive(factory())
    host.ready()
    with pytest.raises(HandoffHold, match="duplicate"):
        host.receiver.receive(factory())
    assert len(host.calls) == 1


def test_same_rename_under_another_request_id_cannot_duplicate_review(host):
    host.receiver.receive(rename())
    with pytest.raises(HandoffHold, match="duplicate"):
        host.receiver.receive(rename("different-id"))


@pytest.mark.parametrize("descriptor", [search(catalog_id="foreign"), search(revision="old"),
    search(stable_id="unknown"), rename(catalog_id="foreign"),
    rename(after_revision="old"), rename(previous_display="invented"), rename(new_display="invented"),
    rename(stable_id="unknown")])
def test_invalid_catalog_and_identity_hold_before_navigation(host, descriptor):
    with pytest.raises(HandoffHold):
        host.receiver.receive(descriptor)
    assert host.current == 0 and host.pending == [] and host.calls == []


def test_sender_old_name_claim_cannot_create_a_native_alias(host):
    host.catalog = AdmittedCatalog(host.state.knowledge, "catalog-a", "r2",
                                  (AdmittedTag("tag-a", "new name"),))
    with pytest.raises(HandoffHold, match="recognized_old_alias"):
        host.receiver.receive(rename())


@pytest.mark.parametrize("factory", [search, rename])
def test_lazy_delivery_rechecks_revision_and_does_not_open_stale_workflow(host, factory):
    host.receiver.receive(factory())
    host.catalog = AdmittedCatalog(host.state.knowledge, "catalog-a", "r3", host.catalog.tags)
    assert host.ready() == [False]
    assert host.receiver.last_hold_reason == "catalog_admission_held"
    assert host.calls == []
    with pytest.raises(HandoffHold, match="duplicate"):
        host.receiver.receive(factory())


def test_lazy_delivery_does_not_act_in_another_workspace(host):
    host.receiver.receive(search())
    host.current = 1
    assert host.ready() == [False] and host.calls == []


def test_queue_shutdown_before_delivery_is_retryable(host):
    host.stopping = True
    assert host.receiver.receive(search()) is False
    host.stopping = False
    assert host.receiver.receive(search())


def test_foreign_knowledge_observation_and_authority_replacement_are_held(host):
    host.catalog = AdmittedCatalog(object(), "catalog-a", "r2", host.catalog.tags)
    with pytest.raises(HandoffHold, match="foreign"):
        host.receiver.receive(search())
    host.state.knowledge = object()
    with pytest.raises(ValueError):
        host.receiver.receive(search())
    assert host.calls == [] and host.pending == []


def test_immediate_native_show_tag_none_is_success_and_replay_is_held(host):
    host.ready()
    assert host.receiver.receive(search()) is True
    assert host.calls == [("display_name_search", "new name")]


def test_installation_starts_nothing_and_preserves_exact_enrollment(host):
    first = install_encyclopedia_handoff_receiver(host, read_admitted_catalog=host.reader)
    assert install_encyclopedia_handoff_receiver(host, read_admitted_catalog=host.reader) is first
    with pytest.raises(HandoffHold, match="enrollment"):
        install_encyclopedia_handoff_receiver(host, read_admitted_catalog=lambda state: host.catalog)
    assert host.current == 0 and host.pending == [] and host.calls == []


@pytest.mark.parametrize("value", [None, {}, {"kind": "merge"},
    dict(vars(search()), schema_version=True), dict(vars(search()), extra="unexpected"),
    dict(vars(search()), stable_id=""), {"kind": []}, dict(vars(search()), request_id="bad\nvalue"),
    dict(vars(rename()), before_revision=True), dict(vars(rename()), kind="split")])
def test_exact_versioned_descriptor_is_required(value):
    with pytest.raises(HandoffHold):
        decode_request(value)


@pytest.mark.parametrize("factory", [search, rename])
def test_wire_descriptor_roundtrip(host, factory):
    request = factory()
    assert decode_request(vars(request)) == request
    assert admit_request(vars(request), host.catalog, host.state.knowledge)


def test_held_deferred_rename_can_use_new_explicit_request_after_readmission(host):
    original = host.catalog
    host.receiver.receive(rename())
    host.catalog = AdmittedCatalog(host.state.knowledge, "catalog-a", "r3", host.catalog.tags)
    assert host.ready() == [False]
    host.catalog = original
    assert host.receiver.receive(rename("reviewed-retry"))
    assert len(host.calls) == 1 and host.receiver.last_hold_reason == ""


def test_missing_admission_never_navigates_or_queues(host):
    host.catalog = None
    with pytest.raises(HandoffHold, match="unavailable"):
        host.receiver.receive(search())
    assert host.current == 0 and host.pending == [] and host.calls == []


@pytest.mark.parametrize("before", ["", "untrusted-prior-revision", "r2"])
def test_before_revision_is_only_context_for_independent_current_alias_review(host, before):
    assert host.receiver.receive(rename(before_revision=before))
    host.ready()
    assert len(host.calls) == 1
    scope = host.calls[0][1]
    assert scope["label"] == 'Review image Tag names for "new name"'
    assert not {"start_audit", "before_revision", "after_revision"}.intersection(scope)


@pytest.mark.parametrize("collision", ["alias", "canonical"])
def test_ambiguous_alias_or_another_canonical_owner_cannot_admit_review(host, collision):
    other = AdmittedTag("tag-b", "other name", ("old name",)) if collision == "alias" else AdmittedTag("tag-b", "old name")
    host.catalog = AdmittedCatalog(host.state.knowledge, "catalog-a", "r2",
                                  (*host.catalog.tags, other))
    with pytest.raises(HandoffHold, match="uniquely_owned"):
        host.receiver.receive(rename())
    assert host.current == 0 and host.pending == [] and host.calls == []


def test_changed_before_context_cannot_evade_duplicate_review(host):
    host.receiver.receive(rename())
    with pytest.raises(HandoffHold, match="duplicate"):
        host.receiver.receive(rename("another-id", before_revision="invented"))


def test_deferred_removed_alias_holds_before_opening_review(host):
    host.receiver.receive(rename())
    host.catalog = AdmittedCatalog(host.state.knowledge, "catalog-a", "r2",
                                  (AdmittedTag("tag-a", "new name"),))
    assert host.ready() == [False]
    assert host.calls == [] and host.receiver.last_hold_reason == "catalog_admission_held"


def test_admitted_hint_opens_exact_native_scope_without_audit_preview_or_write(host):
    host.receiver.receive(rename(before_revision="untrusted"))
    host.ready()
    scope = host.calls[0][1]
    source = ast.parse((Path(__file__).resolve().parents[2] / "qt_app/image_identity_correction_widget.py")
                      .read_text(encoding="utf-8-sig"))
    owner = next(node for node in source.body if isinstance(node, ast.ClassDef)
                 and node.name == "ImageIdentityCorrectionWidget")
    method = next(node for node in owner.body if isinstance(node, ast.FunctionDef)
                  and node.name == "open_scope")
    namespace = {}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "native-correction-open-scope", "exec"), namespace)
    messages, labels = [], []
    def forbidden(*args, **kwargs):
        pytest.fail("Receiving a hint must not start audit, preview, apply or metadata writes")
    widget = SimpleNamespace(
        has_active_background_work=lambda: False,
        _matching_scope={}, _matching_preview=None,
        _render_matching_preview=lambda: None,
        matching_return_button=SimpleNamespace(setVisible=lambda value: None),
        matching_scope_label=SimpleNamespace(setText=labels.append),
        _set_matching_status=messages.append,
        audit_matching_images=forbidden, _queue_matching_audit=forbidden,
        apply_matching_corrections=forbidden, _matching_service=forbidden,
    )
    namespace["open_scope"](widget, scope)
    assert widget._matching_scope == scope and widget._matching_selected == set()
    assert widget._matching_preview is None
    assert labels and messages and "did not start a scan or write data" in messages[0]


class NativeCorrection:
    def __init__(self):
        self.busy = False
        self.busy_on_show = False
        self._matching_scope = {"label": "existing scope"}
        self._matching_preview = None
        self.labels, self.messages = [], []
        self.matching_return_button = SimpleNamespace(setVisible=lambda value: None)
        self.matching_scope_label = SimpleNamespace(setText=self.labels.append)
        self.open_tag_encyclopedia = SimpleNamespace(connect=lambda callback: None)
    def has_active_background_work(self): return self.busy
    def _render_matching_preview(self): pass
    def _set_matching_status(self, message): self.messages.append(message)
    def show(self):
        if self.busy_on_show: self.busy = True
    def _queue_matching_audit(self): pytest.fail("No automatic audit allowed")


class NativePage:
    def __init__(self, host):
        self.host = host
        self.image_identity_correction = NativeCorrection()
        self._image_tag_maintenance_dialog = SimpleNamespace(
            show=lambda: None, raise_=lambda: None, activateWindow=lambda: None)
    def window(self): return self.host
    def show_tag(self, display): self.host.calls.append(("display_name_search", display))


class NativeQueueHost(Host):
    def __init__(self):
        super().__init__()
        self._dataset_page_task = object()
        self._dataset_page_preparation = None
        self._dataset_preparation_shutdown = False
        self._pending_dataset_page_actions = []
        self.errors = []
        self.navigation.currentRow = lambda: self.current
    def _ensure_page(self, index): pass  # Async preparation remains pending.
    def _dataset_loading_placeholder(self):
        return SimpleNamespace(set_error=self.errors.append)
    def statusBar(self):
        return SimpleNamespace(showMessage=lambda *args: self.errors.append(args[0]))
    def ready(self):
        self.dataset_page = NativePage(self)
        pending, self._pending_dataset_page_actions = self._pending_dataset_page_actions, []
        return [self._deliver_pending_dataset_page_action(action) for action in pending]


def install_native_body(path, class_name, method_name, target, namespace):
    tree = ast.parse((Path(__file__).resolve().parents[2] / path).read_text(encoding="utf-8-sig"))
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    method = next(node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == method_name)
    exec(compile(ast.Module(body=[method], type_ignores=[]), path + "::" + method_name, "exec"), namespace)
    setattr(target, method_name, namespace[method_name])


for name in ("open_dataset_tag", "open_dataset_function", "_queue_dataset_page_action",
             "_deliver_pending_dataset_page_action", "_dataset_page_preparation_failed"):
    install_native_body("qt_app/main_window.py", "TagManagerWindow", name, NativeQueueHost,
                        {"TagManagerWindow": NativeQueueHost, "deepcopy": deepcopy})
for name in ("open_function", "_open_image_tag_maintenance"):
    install_native_body("qt_app/dataset_page.py", "DatasetPage", name, NativePage, {})
install_native_body("qt_app/image_identity_correction_widget.py", "ImageIdentityCorrectionWidget",
                    "open_scope", NativeCorrection, {})


def settled_native_host():
    from suite_core.media_organizer.encyclopedia_handoff_entry import with_encyclopedia_handoff_settlement
    cls = with_encyclopedia_handoff_settlement(NativeQueueHost)
    host = cls()
    host.receiver = install_encyclopedia_handoff_receiver(host, read_admitted_catalog=host.reader)
    return host


def test_actual_native_busy_opener_true_is_not_scope_acceptance_and_retry_works():
    host = settled_native_host()
    host.ready()
    correction = host.dataset_page.image_identity_correction
    correction.busy = True
    assert host.dataset_page._open_image_tag_maintenance({"label": "candidate"}) is True
    assert correction._matching_scope == {"label": "existing scope"}
    assert host.receiver.receive(rename()) is False
    assert host.receiver.last_hold_reason == "correction_review_busy"
    with pytest.raises(HandoffHold, match="duplicate"):
        host.receiver.receive(rename())
    correction.busy = False
    assert host.receiver.receive(rename("explicit-retry")) is True
    assert correction._matching_scope["label"] == 'Review image Tag names for "new name"'


def test_scope_not_accepted_during_native_dialog_show_is_retryable():
    host = settled_native_host()
    host.ready()
    correction = host.dataset_page.image_identity_correction
    correction.busy_on_show = True
    assert host.receiver.receive(rename()) is False
    assert host.receiver.last_hold_reason == "correction_scope_not_accepted"
    assert correction._matching_scope == {"label": "existing scope"}
    correction.busy = correction.busy_on_show = False
    assert host.receiver.receive(rename("explicit-retry")) is True


@pytest.mark.parametrize("factory", [rename, search])
def test_actual_native_preparation_failure_settles_exact_dropped_callbacks(factory):
    host = settled_native_host()
    unrelated = lambda: pytest.fail("Another owner's dropped action must not be executed")
    host._pending_dataset_page_actions.append(unrelated)
    assert host.receiver.receive(factory())
    dropped = host._pending_dataset_page_actions[-1]
    task = host._dataset_page_task
    host._dataset_page_preparation_failed(task, "synthetic failure")
    assert host._pending_dataset_page_actions == []
    assert host.receiver.last_hold_reason == "dataset_preparation_failed"
    assert host.receiver._pending_actions == {}
    with pytest.raises(HandoffHold, match="duplicate"):
        host.receiver.receive(factory())
    host.ready()
    assert dropped() is False  # A late callback cannot run after settlement.
    assert host.receiver.receive(factory("explicit-retry")) is True


def test_stale_native_preparation_failure_does_not_settle_current_request():
    host = settled_native_host()
    host.receiver.receive(rename())
    host._dataset_page_preparation_failed(object(), "stale task failure")
    assert len(host._pending_dataset_page_actions) == 1
    assert host.receiver.last_hold_reason == ""
    assert host.ready() == [True]


def test_unwrapped_native_failure_reproduction_can_be_explicitly_settled():
    host = NativeQueueHost()
    host.receiver.receive(rename())
    dropped = tuple(host._pending_dataset_page_actions)
    host._dataset_page_preparation_failed(host._dataset_page_task, "synthetic failure")
    assert host._pending_dataset_page_actions == [] and host.receiver.last_hold_reason == ""
    host.receiver.settle_dropped_actions(dropped)
    assert host.receiver.last_hold_reason == "dataset_preparation_failed"
    host.ready()
    assert host.receiver.receive(rename("explicit-retry")) is True


def test_synchronous_native_preparation_failure_returns_hold_and_keeps_request_id():
    from suite_core.media_organizer.encyclopedia_handoff_entry import with_encyclopedia_handoff_settlement
    class ImmediateFailureHost(NativeQueueHost):
        def _ensure_page(self, index):
            self._dataset_page_preparation_failed(self._dataset_page_task, "synthetic synchronous failure")
    host = with_encyclopedia_handoff_settlement(ImmediateFailureHost)()
    host.receiver = install_encyclopedia_handoff_receiver(host, read_admitted_catalog=host.reader)
    assert host.receiver.receive(rename()) is False
    assert host.receiver.last_hold_reason == "dataset_preparation_failed"
    with pytest.raises(HandoffHold, match="duplicate"):
        host.receiver.receive(rename())
    host.ready()
    assert host.receiver.receive(rename("explicit-retry")) is True


class CloseEvent:
    def __init__(self): self.accepted = True
    def accept(self): self.accepted = True
    def ignore(self): self.accepted = False
    def isAccepted(self): return self.accepted


class CloseBase(NativeQueueHost):
    def closeEvent(self, event): event.accept()


# Compile the full original method in a class, preserving zero-argument super.
_close_method = next(node for node in _owner.body
                     if isinstance(node, ast.FunctionDef) and node.name == "closeEvent")
_close_class = ast.ClassDef(name="NativeCloseHost", bases=[ast.Name(id="CloseBase", ctx=ast.Load())],
                           keywords=[], body=[deepcopy(_close_method)], decorator_list=[])
_close_namespace = {"CloseBase": CloseBase, "QCloseEvent": CloseEvent,
                    "QMessageBox": SimpleNamespace(warning=lambda *args: None)}
exec(compile(ast.fix_missing_locations(ast.Module(body=[_close_class], type_ignores=[])),
             "original-native-close", "exec"), _close_namespace)


def native_close_host(*, drained=False, unsaved=False, hosted_decline=False):
    from suite_core.media_organizer.organizer_recovery_entry import with_dataset_recovery
    host = with_dataset_recovery(_close_namespace["NativeCloseHost"])()
    host.receiver = install_encyclopedia_handoff_receiver(host, read_admitted_catalog=host.reader)
    host._scan_thread = None
    host.ui_test = True
    host._finish_knowledge_editor_wait = lambda: None
    host.cancelled = []
    host._dataset_page_task = SimpleNamespace(cancel=lambda: host.cancelled.append(True))
    host.state.shutdown_background_jobs = lambda **kwargs: drained
    if unsaved:
        host.knowledge_page = SimpleNamespace(confirm_discard_order_changes=lambda: False)
    host.finished = []
    host._hosted_encyclopedia_entry = SimpleNamespace(
        prepare_close=lambda owner: not hosted_decline,
        finish_host_close=host.finished.append)
    return host


@pytest.mark.parametrize("drained", [False, True])
def test_original_native_close_settles_removed_callbacks_even_when_retained(drained):
    host = native_close_host(drained=drained)
    unrelated = lambda: pytest.fail("Must not invoke unrelated action")
    host._pending_dataset_page_actions.append(unrelated)
    assert host.receiver.receive(rename())
    dropped = host._pending_dataset_page_actions[-1]
    task = host._dataset_page_task
    event = CloseEvent()
    host.closeEvent(event)
    assert event.isAccepted() is drained and host.finished == [drained]
    assert host.cancelled == [True] and host._dataset_page_task is None
    assert host._pending_dataset_page_actions == []
    assert host.receiver._pending_actions == {} and host.receiver._renames == set()
    assert host.receiver.last_hold_reason == "dataset_shutdown"
    with pytest.raises(HandoffHold, match="duplicate"):
        host.receiver.receive(rename())
    assert dropped() is False
    host._dataset_page_preparation_failed(task, "late cancellation")
    assert host.receiver.last_hold_reason == "dataset_shutdown"
    host.reader = lambda state: pytest.fail("No admission reads after shutdown")
    with pytest.raises(HandoffHold, match="dataset_shutdown"):
        host.receiver.receive(rename("new-request"))
    assert "new-request" not in host.receiver._requests
    assert host._dataset_preparation_shutdown and not host.calls


@pytest.mark.parametrize("guard", ["unsaved", "hosted"])
def test_original_early_close_decline_preserves_pending_replay_state(guard):
    host = native_close_host(unsaved=guard == "unsaved", hosted_decline=guard == "hosted")
    assert host.receiver.receive(rename())
    pending = tuple(host._pending_dataset_page_actions)
    requests, renames = host.receiver._requests.copy(), host.receiver._renames.copy()
    event = CloseEvent()
    host.closeEvent(event)
    assert not event.isAccepted() and not host._dataset_preparation_shutdown
    assert tuple(host._pending_dataset_page_actions) == pending
    assert host.receiver._requests == requests and host.receiver._renames == renames
    assert host.receiver.last_hold_reason == "" and not host.cancelled
    assert host.finished == ([] if guard == "hosted" else [False])
    assert host.ready() == [True]
