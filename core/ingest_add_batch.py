"""Durable, read-only-reconcilable intent for a reviewed Add Images batch.

The parent journal operation records the selection before the first transfer.
Child transfer receipts, never a UI counter or parent checkpoint, establish
which families completed. This module does not resume or mutate image files.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import re
from typing import Callable
from uuid import uuid4

from .ingest_import import IngestImportItem, IngestImportPlan
from .operation_journal import JournalOperation, OperationJournal


SCHEMA_VERSION = 2
MAX_ITEMS = 100_000
CHUNK_ITEMS = 128
CHUNK_BYTES = 1024 * 1024


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _item_payload(index: int, item: IngestImportItem, action: str,
                  should_cancel: Callable[[], bool] | None = None) -> dict:
    should_cancel = should_cancel or (lambda: False)
    if should_cancel():
        raise InterruptedError("Add Images review stopped before changing files.")
    transfer = item.transfer_for(action)
    if transfer is None or not transfer.ready:
        raise ValueError("Only reviewed, ready image families may enter Add batch intent.")
    if (not item.source.is_absolute() or not item.destination.is_absolute()
            or transfer.image != item.source or transfer.destination != item.destination
            or transfer.collision_policy != "error"):
        raise ValueError("Add batch intent needs exact absolute error-on-collision paths.")
    members = []
    for member in transfer.members:
        if should_cancel():
            raise InterruptedError("Add Images review stopped before changing files.")
        if not member.source.is_absolute() or not member.target.is_absolute():
            raise ValueError("Add batch intent needs an exact absolute file family.")
        try:
            before = member.source.stat()
            if (before.st_size, before.st_mtime_ns) != member.signature():
                raise ValueError("A selected image family changed after preview.")
            digest = hashlib.sha256()
            with member.source.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    if should_cancel():
                        raise InterruptedError("Add Images review stopped before changing files.")
                    digest.update(chunk)
            after = member.source.stat()
            if (after.st_size, after.st_mtime_ns) != member.signature():
                raise ValueError("A selected image family changed while sealing its identity.")
        except OSError as exc:
            raise ValueError(f"A selected image family is unavailable: {exc}") from exc
        members.append({"source": str(member.source), "target": str(member.target),
                        "size": member.size, "mtime_ns": member.mtime_ns,
                        "sha256": digest.hexdigest()})
    if not members:
        raise ValueError("Add batch intent needs an exact absolute file family.")
    value = {"item_id": f"item_{index}", "ordinal": index,
             "source": str(item.source), "destination": str(item.destination),
             "source_profile_id": item.source_profile.stable_id
             if item.source_profile is not None else "",
             "members": members, "naming_policy": transfer.naming_policy,
             "action": action}
    value["fingerprint"] = hashlib.sha256(_canonical(value)).hexdigest()
    return value


@dataclass(frozen=True)
class AddBatchItemState:
    item_id: str
    ordinal: int
    source: Path
    destination: Path
    fingerprint: str
    expected_hashes: tuple[tuple[str, str], ...]
    state: str  # remaining, completed, held
    operation_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class AddBatchState:
    operation_id: str
    schema_version: int
    group_id: str
    action: str
    ingest_root: Path
    source_root: Path | None
    preserves_subfolders: bool
    destination_profile_id: str
    items: tuple[AddBatchItemState, ...]

    @property
    def remaining(self) -> int:
        return sum(item.state == "remaining" for item in self.items)

    @property
    def completed(self) -> int:
        return sum(item.state == "completed" for item in self.items)

    @property
    def held(self) -> int:
        return sum(item.state == "held" for item in self.items)


@dataclass(frozen=True)
class AddBatchAttempt:
    parent_id: str
    item_id: str
    attempt_id: str
    fingerprint: str
    group_id: str
    source: Path
    destination: Path


class AddBatchIntentStore:
    """Use the existing transfer journal, with exact child receipts as authority."""

    def __init__(self, journal: OperationJournal) -> None:
        self.journal = journal

    def begin(self, plan: IngestImportPlan, action: str, group_id: str,
              *, should_cancel: Callable[[], bool] | None = None) -> str:
        if action not in {"copy", "move"} or not group_id.startswith("ingest-add-"):
            raise ValueError("Choose an exact Add Images action and batch identity.")
        if (not plan.ingest_root.is_absolute()
                or plan.source_root is not None and not plan.source_root.is_absolute()
                or plan.preserves_subfolders and plan.source_root is None
                or plan.errors or plan.action_errors(action)):
            raise ValueError("Only a ready, exact-destination Add Images review can be retained.")
        items = plan.ready(action)
        if not items or len(items) > MAX_ITEMS:
            raise ValueError("The reviewed Add Images batch is empty or exceeds its bounded limit.")
        # Existing active batches must be resolved before a new one is admitted.
        if self.active():
            raise ValueError("Finish or explicitly abandon the saved Add Images batch first.")
        payload = tuple(_item_payload(index, item, action, should_cancel)
                        for index, item in enumerate(items))
        chunks = []
        for start in range(0, len(payload), CHUNK_ITEMS):
            if should_cancel is not None and should_cancel():
                raise InterruptedError("Add Images review stopped before changing files.")
            values = list(payload[start:start + CHUNK_ITEMS])
            digest = self.journal.store_verified_snapshot(values, max_bytes=CHUNK_BYTES)
            if self.journal.load_verified_snapshot(digest, max_bytes=CHUNK_BYTES) != values:
                raise ValueError("The saved Add Images selection did not read back exactly.")
            chunks.append(digest)
        if should_cancel is not None and should_cancel():
            raise InterruptedError("Add Images review stopped before changing files.")
        operation_id = self.journal.begin(
            "ingest_add_batch", expected=len(payload), reason="Reviewed Add Images batch",
            metadata={"batch_schema": SCHEMA_VERSION, "group_id": group_id,
                      "action": action, "ingest_root": str(plan.ingest_root),
                      "source_root": str(plan.source_root) if plan.source_root is not None else "",
                      "preserves_subfolders": plan.preserves_subfolders,
                      "destination_profile_id": plan.destination_profile.stable_id
                      if plan.destination_profile is not None else "",
                      "item_count": len(payload), "chunks": chunks},
        )
        # A published parent begin is the seal; no transfer is permitted before
        # its complete bounded snapshot can be read back.
        self.read(operation_id)
        return operation_id

    def active(self) -> tuple[str, ...]:
        if not self.journal.path.exists():
            return ()
        self.journal.require_intact_events()
        return tuple(item.id for item in self.journal.operations()
                     if item.kind == "ingest_add_batch" and item.status == "applying")

    def reserve(self, operation_id: str, item_id: str) -> AddBatchAttempt:
        """Durably claim one unattempted item before any child transfer begins.

        A reservation without a child remains held on restart. This method
        neither checks current files nor authorizes replay by itself.
        """
        with self.journal._lock:
            state = self.read(operation_id)
            if state.held:
                raise ValueError("Resolve the held Add Images family before reserving more work.")
            item = next((value for value in state.items if value.item_id == item_id), None)
            if item is None or item.state != "remaining":
                raise ValueError("Choose one exact unattempted Add Images family.")
            attempt_id = "attempt_" + uuid4().hex
            self.journal.append_checkpoint(operation_id, item=item_id, metadata={
                "phase": "attempt_reserved", "attempt_id": attempt_id,
                "fingerprint": item.fingerprint,
            })
            # Read-back must observe the held reservation before the caller
            # passes these identities to the verified family-transfer service.
            held = self.read(operation_id)
            current = next(value for value in held.items if value.item_id == item_id)
            if current.state != "held" or not current.reason.startswith("An attempt was reserved"):
                raise ValueError("The Add Images attempt reservation did not read back exactly.")
            return AddBatchAttempt(operation_id, item_id, attempt_id,
                                   item.fingerprint, state.group_id,
                                   item.source, item.destination)

    def close_complete(self, operation_id: str) -> AddBatchState:
        """Close only after every exact child receipt is complete."""
        with self.journal._lock:
            state = self.read(operation_id)
            if state.remaining or state.held:
                raise ValueError("The Add Images batch still has remaining or held families.")
            self.journal.finish(operation_id, result={
                "group_id": state.group_id, "action": state.action,
                "completed": state.completed,
            })
            return state

    def discard_remaining(
        self, operation_id: str, *, expected_remaining: tuple[str, ...],
    ) -> AddBatchState:
        """Close only unattempted intent; completed transfer receipts remain intact.

        The caller must also exclude a concurrently running Ingest worker.
        """
        with self.journal._lock:
            state = self.read(operation_id)
            remaining = tuple(item.item_id for item in state.items
                              if item.state == "remaining")
            if state.held or not remaining or remaining != expected_remaining:
                raise ValueError("The saved Add Images remainder changed or needs exact recovery.")
            self.journal.finish(operation_id, "cancelled",
                message="User discarded only the unattempted Add Images selection.",
                result={"group_id": state.group_id, "action": state.action,
                        "completed": state.completed, "discarded_remaining": state.remaining})
            closed = next((item for item in self.journal.operations()
                           if item.id == operation_id), None)
            if (closed is None or closed.status != "cancelled"
                    or closed.result.get("discarded_remaining") != state.remaining):
                raise ValueError("The discarded Add Images selection did not read back exactly.")
            return state

    def matching_remaining_items(
        self, operation_id: str, plan: IngestImportPlan,
        *, should_cancel: Callable[[], bool] | None = None,
    ) -> tuple[IngestImportItem, ...]:
        """Match a fresh exact-subset review to every unattempted sealed family.

        This is read-only and does not authorize transfer or reuse a previous
        privacy confirmation. The caller must recheck after the user's Apply.
        """
        state = self.read(operation_id)
        if state.schema_version != SCHEMA_VERSION or state.held:
            raise ValueError("This saved Add Images batch needs exact recovery before continuation.")
        if (plan.ingest_root != state.ingest_root
                or plan.source_root != state.source_root
                or plan.preserves_subfolders != state.preserves_subfolders
                or (plan.destination_profile.stable_id if plan.destination_profile else "")
                    != state.destination_profile_id
                or plan.errors or plan.action_errors(state.action)
                or plan.unsupported or plan.duplicates):
            raise ValueError("The fresh Add Images review differs from the saved selection or destination.")
        remaining = [item for item in state.items if item.state == "remaining"]
        if len(plan.items) != len(remaining) or len(plan.ready(state.action)) != len(remaining):
            raise ValueError("The fresh Add Images review changed or omitted a remaining image family.")
        matched = []
        for sealed, fresh in zip(remaining, plan.items):
            if (_item_payload(sealed.ordinal, fresh, state.action, should_cancel)["fingerprint"]
                    != sealed.fingerprint):
                raise ValueError("A remaining Add Images image family changed; review it again.")
            transfer = fresh.transfer_for(state.action)
            if transfer is None or len(sealed.expected_hashes) != len(transfer.members):
                raise ValueError("A remaining Add Images image family has no sealed byte identity.")
            pinned = replace(transfer, expected_hashes=sealed.expected_hashes)
            matched.append(replace(fresh, **{
                "copy_plan" if state.action == "copy" else "move_plan": pinned,
            }))
        return tuple(matched)

    def read(self, operation_id: str) -> AddBatchState:
        self.journal.require_intact_events()
        operations = self.journal.operations()
        parent = next((item for item in operations if item.id == operation_id
                       and item.kind == "ingest_add_batch"), None)
        if parent is None or parent.status != "applying":
            raise ValueError("The selected Add Images batch is unavailable or already closed.")
        metadata = parent.metadata
        if (type(metadata.get("batch_schema")) is not int
                or metadata["batch_schema"] not in {1, SCHEMA_VERSION}
                or metadata.get("action") not in {"copy", "move"}
                or not isinstance(metadata.get("group_id"), str)
                or not metadata["group_id"].startswith("ingest-add-")
                or not isinstance(metadata.get("chunks"), list)
                or not Path(str(metadata.get("ingest_root") or "")).is_absolute()
                or not isinstance(metadata.get("source_root"), str)
                or (metadata["source_root"] and not Path(metadata["source_root"]).is_absolute())
                or type(metadata.get("preserves_subfolders")) is not bool
                or not isinstance(metadata.get("destination_profile_id"), str)):
            raise ValueError("The saved Add Images batch contract is unsupported or incomplete.")
        count = metadata.get("item_count")
        chunks = metadata["chunks"]
        if (not isinstance(count, int) or not 1 <= count <= MAX_ITEMS
                or len(chunks) != (count + CHUNK_ITEMS - 1) // CHUNK_ITEMS):
            raise ValueError("The saved Add Images selection count is inconsistent.")
        payload = []
        for digest in chunks:
            values = self.journal.load_verified_snapshot(digest, max_bytes=CHUNK_BYTES)
            if not isinstance(values, list) or not 1 <= len(values) <= CHUNK_ITEMS:
                raise ValueError("A saved Add Images selection chunk is invalid.")
            payload.extend(values)
        if len(payload) != count:
            raise ValueError("The saved Add Images selection is incomplete.")
        by_id = {}
        for index, value in enumerate(payload):
            if not isinstance(value, dict):
                raise ValueError("A saved Add Images item is invalid.")
            fingerprint = value.get("fingerprint")
            expected = dict(value)
            expected.pop("fingerprint", None)
            members = value.get("members")
            if (value.get("item_id") != f"item_{index}" or value.get("ordinal") != index
                    or value.get("action") != metadata["action"]
                    or not isinstance(fingerprint, str)
                    or hashlib.sha256(_canonical(expected)).hexdigest() != fingerprint
                    or not Path(str(value.get("source") or "")).is_absolute()
                    or not Path(str(value.get("destination") or "")).is_absolute()
                    or not isinstance(members, list) or not members
                    or any(not isinstance(member, dict)
                           or not Path(str(member.get("source") or "")).is_absolute()
                           or not Path(str(member.get("target") or "")).is_absolute()
                           or type(member.get("size")) is not int or member["size"] < 0
                           or type(member.get("mtime_ns")) is not int or member["mtime_ns"] < 0
                           or (metadata["batch_schema"] == SCHEMA_VERSION
                               and (not isinstance(member.get("sha256"), str)
                                    or re.fullmatch(r"[0-9a-f]{64}", member["sha256"]) is None))
                           for member in members)):
                raise ValueError("A saved Add Images item changed or lost its exact identity.")
            by_id[value["item_id"]] = value
        children: dict[str, list[JournalOperation]] = {item_id: [] for item_id in by_id}
        for child in operations:
            if child.kind not in {"file_copy", "file_move"}:
                continue
            if child.metadata.get("batch_parent_id") != operation_id:
                if child.metadata.get("group_id") == metadata["group_id"]:
                    raise ValueError("A transfer in this batch lacks its exact parent link.")
                continue
            item_id = child.metadata.get("batch_item_id")
            if item_id not in by_id:
                raise ValueError("A transfer refers to an unknown Add Images item.")
            children[item_id].append(child)
        reservations: dict[str, set[str]] = {item_id: set() for item_id in by_id}
        for checkpoint in parent.checkpoints:
            detail = checkpoint["metadata"]
            if detail.get("phase") != "attempt_reserved":
                continue
            item_id = checkpoint["item"]
            if (item_id not in by_id or detail.get("fingerprint") != by_id[item_id]["fingerprint"]
                    or not isinstance(detail.get("attempt_id"), str)):
                raise ValueError("A saved Add Images attempt reservation is invalid.")
            reservations[item_id].add(detail["attempt_id"])
        states = []
        for item_id, value in by_id.items():
            attempts = children[item_id]
            completed = []
            issue = ""
            for child in attempts:
                record = child.result.get("record")
                if (child.metadata.get("batch_item_fingerprint") != value["fingerprint"]
                        or child.metadata.get("batch_attempt_id") not in reservations[item_id]
                        or child.metadata.get("group_id") != metadata["group_id"]
                        or child.metadata.get("image") != value["source"]
                        or child.metadata.get("destination") != value["destination"]
                        or child.kind != ("file_copy" if metadata["action"] == "copy" else "file_move")):
                    issue = "A transfer attempt conflicts with the saved selection."
                elif child.status == "complete":
                    if (not isinstance(record, dict) or record.get("operation_id") != child.id
                            or record.get("group_id") != metadata["group_id"]):
                        issue = "A completed transfer has no matching saved receipt."
                    else:
                        saved_files = record.get("files")
                        expected_files = value["members"]
                        if (not isinstance(saved_files, list)
                                or len(saved_files) != len(expected_files)
                                or any(not isinstance(saved, dict)
                                       or saved.get("from") != expected["source"]
                                       or saved.get("to") != expected["target"]
                                       or not isinstance(saved.get("sha256"), str)
                                       or re.fullmatch(r"[0-9a-f]{64}", saved["sha256"]) is None
                                       or (metadata["batch_schema"] == SCHEMA_VERSION
                                           and saved["sha256"] != expected.get("sha256"))
                                       for saved, expected in zip(saved_files, expected_files))):
                            issue = "A completed transfer receipt differs from the saved file family."
                        else:
                            completed.append(child.id)
                else:
                    issue = "A transfer still needs exact recovery or fresh review."
            if len(completed) > 1:
                issue = "More than one completed transfer claims this image family."
            if issue:
                state, operation = "held", completed[0] if completed else ""
            elif completed:
                state, operation = "completed", completed[0]
            elif reservations[item_id]:
                state, operation = "held", ""
                issue = "An attempt was reserved but no verified transfer result is available."
            else:
                state, operation = "remaining", ""
            hashes = (tuple((member["source"], member["sha256"])
                            for member in value["members"])
                      if metadata["batch_schema"] == SCHEMA_VERSION else ())
            states.append(AddBatchItemState(item_id, value["ordinal"], Path(value["source"]),
                                            Path(value["destination"]), value["fingerprint"], hashes,
                                            state, operation, issue))
        return AddBatchState(operation_id, metadata["batch_schema"], metadata["group_id"], metadata["action"],
                             Path(metadata["ingest_root"]),
                             Path(metadata["source_root"]) if metadata["source_root"] else None,
                             metadata["preserves_subfolders"],
                             metadata["destination_profile_id"], tuple(states))
