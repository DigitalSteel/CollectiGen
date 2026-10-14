"""Durable, explicit native-video submission and verified output handoff."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time
from uuid import UUID, uuid4

from .comfy_video_transport import MAX_RESULT_BYTES, output_descriptor
from .external_write_transaction import atomic_verified_bytes, write_verified_json
from .generation_resource_guard import assess_memory, classify_memory_failure
from .generation_transport import strict_json
from .model_usage import ModelUsageService, UsageAdmission, UsageAdmissionFacts, UsageAttestation
from .training_source_io import _assert_unredirected, read_stable_training_source
from .video_generation import VideoGenerationRequest, VideoSourceAsset

_LOCK = threading.RLock()
_ID = re.compile(r"[a-f0-9]{32}\Z")
_USAGE_OWNER = "collectigen.video-generation"
_USAGE_VERSION = "1"
_USAGE_STATES = {"attempting", "admitted", "disabled", "unavailable", "stale", "history_full",
                 "recorded", "already_recorded", "retry_pending", "unverified", "cancelled"}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


class VideoExecutionService:
    """The adapter owns exact runtime/resources; the verifier owns full decoding."""

    def __init__(self, storage_root, jobs, *, verify_output, poll_interval=1.0, timeout=1800,
                 registry=None, protected_roots=()):
        self.root = Path(storage_root).absolute()
        self.jobs = jobs
        self.verify_output = verify_output
        self.poll_interval = poll_interval
        self.timeout = timeout
        self._usage_admissions, self._usage_proofs = {}, {}
        self.model_usage = None if registry is None else ModelUsageService(registry,
            protected_roots=protected_roots,
            admission_verifier=lambda execution_id: self._usage_admissions.get(execution_id),
            success_verifier=lambda proof: self._usage_proofs.get(proof))

    def _directory(self, job_id):
        if not isinstance(job_id, str) or not _ID.fullmatch(job_id):
            raise ValueError("Invalid retained video job identity.")
        path = self.root / job_id
        _assert_unredirected(path)
        return path

    def _load(self, job_id):
        path = self._directory(job_id) / "receipt.json"
        if path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("The retained video receipt exceeds the supported size.")
        raw, _ = read_stable_training_source(path)
        if len(raw) > 32 * 1024 * 1024:
            raise ValueError("The retained video receipt exceeds the supported size.")
        receipt = strict_json(raw)
        if (not isinstance(receipt, dict) or receipt.get("schema_id") != "collectigen.video-execution.v1"
                or receipt.get("job_id") != job_id):
            raise ValueError("The retained video receipt is invalid.")
        self._validate_receipt(receipt)
        return receipt

    def _save(self, receipt, *, expected=None):
        with _LOCK:
            self._validate_receipt(receipt)
            if len(json.dumps(receipt, ensure_ascii=False, allow_nan=False).encode()) > 32 * 1024 * 1024:
                raise ValueError("The video receipt exceeds the supported size.")
            path = self._directory(receipt["job_id"]) / "receipt.json"
            _assert_unredirected(path)
            if path.exists():
                if expected is None or self._load(receipt["job_id"]) != expected:
                    raise ValueError("Video job changed; recover the latest retained state.")
                original = expected.get("model_usage")
                if original is not None:
                    updated = receipt.get("model_usage")
                    if (updated is None or (original["state"] != "attempting"
                            and updated["admission"] != original["admission"])
                            or original["admission"] is not None and updated["admission"] != original["admission"]):
                        raise ValueError("Keep this video's original successful-use admission.")
                if ("verified_completed_at" in expected
                        and receipt.get("verified_completed_at") != expected["verified_completed_at"]):
                    raise ValueError("Keep this video's original verified completion time.")
            elif expected is not None:
                raise ValueError("The retained video job disappeared.")
            path.parent.mkdir(parents=True, exist_ok=True)
            write_verified_json(path, receipt, label="Video generation receipt")

    @staticmethod
    def _validate_prepared(prepared):
        if not isinstance(prepared, dict):
            raise ValueError("A reviewed video graph is required.")
        graph = prepared.get("graph_plan", {})
        if (not isinstance(graph, dict)
                or any(key not in graph for key in ("graph", "manifest", "translation_report"))
                or graph.get("fingerprint") != _digest({key: graph[key]
                    for key in ("graph", "manifest", "translation_report")})):
            raise ValueError("The reviewed video graph identity changed.")
        if not isinstance(graph["graph"], dict) or not isinstance(graph["manifest"], dict):
            raise ValueError("The reviewed video graph or manifest is malformed.")
        if graph.get("requires_review") and prepared.get("reviewed_loss") is not True:
            raise ValueError("Review the video source transform and translation differences before submission.")
        if not isinstance(graph["manifest"].get("request"), dict):
            raise ValueError("The video manifest has no typed request.")
        request = VideoGenerationRequest.from_dict(graph["manifest"]["request"])
        from .video_output_goal import validate_video_output_goal
        validate_video_output_goal(request)
        if request.expected_output_bytes is not None and request.expected_output_bytes > MAX_RESULT_BYTES:
            raise ValueError("The video output cap exceeds the 256 MiB service limit.")
        if not request.schema_supported:
            raise ValueError("This video request version is not executable.")
        upload = graph["manifest"].get("uploaded_image")
        if request.task == "i2v":
            source = request.source_image
            if not isinstance(upload, dict) or any(
                    upload.get(key) != expected for key, expected in {
                        "source_sha256": source.sha256,
                        "source_state_sha256": source.source_state_sha256,
                        "byte_length": source.byte_length,
                        "transform_sha256": source.transform_sha256,
                    }.items()):
                raise ValueError("The I2V graph upload differs from its exact source lineage.")
        elif upload is not None:
            raise ValueError("A T2V request cannot carry an image upload.")
        return graph, request

    @staticmethod
    def _native_identity(receipt):
        graph = receipt["prepared"]["graph_plan"]
        return {"job_id": receipt["job_id"], "attempt_id": receipt["attempt_id"],
                "request_fingerprint": receipt["request_fingerprint"],
                "graph_fingerprint": graph["fingerprint"],
                "source_image": receipt["prepared"]["execution_identity"]["source_image"]}

    @classmethod
    def _validate_receipt(cls, receipt):
        if (not isinstance(receipt, dict) or not isinstance(receipt.get("prepared"), dict)
                or not isinstance(receipt.get("job_id"), str)
                or not _ID.fullmatch(receipt["job_id"])
                or receipt.get("prompt_id") not in {receipt["job_id"], str(UUID(receipt["job_id"]))}):
            raise ValueError("The retained video receipt identity is invalid.")
        _, request = cls._validate_prepared(receipt["prepared"])
        identity = receipt["prepared"].get("execution_identity")
        if (not isinstance(identity, dict) or identity.get("job_id") != receipt["job_id"]
                or not isinstance(identity.get("attempt_id"), str)
                or not _ID.fullmatch(identity["attempt_id"])
                or identity["attempt_id"] != receipt.get("attempt_id")
                or identity.get("source_image") != (
                    request.source_image.to_dict() if request.source_image else None)
                or identity.get("source_state_sha256") != (
                    request.source_image.source_state_sha256 if request.source_image else None)
                or receipt.get("request_fingerprint") != _digest(receipt["prepared"])):
            raise ValueError("The retained video attempt or source lineage changed.")
        if "memory_at_execution" in receipt and (
                cls._memory_snapshot(receipt["memory_at_execution"])
                != receipt["memory_at_execution"]):
            raise ValueError("The retained video memory observation changed.")
        if "protected_usage_context" in receipt["prepared"]:
            from .comfy_video_adapter import validate_protected_usage_context
            validate_protected_usage_context(receipt["prepared"]["protected_usage_context"], receipt["prepared"])
        if "model_usage" in receipt:
            usage = receipt["model_usage"]
            if (not isinstance(usage, dict) or set(usage) != {"attempted", "admission", "state"}
                    or usage["attempted"] is not True or usage["state"] not in _USAGE_STATES
                    or "protected_usage_context" not in receipt["prepared"]):
                raise ValueError("The video's retained successful-use admission is invalid.")
            if usage["admission"] is not None:
                UsageAdmission.from_dict(usage["admission"])
        if "verified_completed_at" in receipt:
            completed = receipt["verified_completed_at"]
            if (not isinstance(completed, str) or len(completed) > 40
                    or datetime.fromisoformat(completed).utcoffset() != timezone.utc.utcoffset(None)
                    or receipt.get("state") != "verified"):
                raise ValueError("The video's verified completion time is invalid.")
        if "failure_class" in receipt:
            failure_class = receipt["failure_class"]
            native_oom = cls._native_failure_class(receipt.get("raw_runtime_history"))
            zero_preflight = (failure_class == "insufficient_vram"
                and "raw_runtime_history" not in receipt
                and isinstance(receipt.get("memory_at_execution"), dict)
                and assess_memory(receipt["memory_at_execution"])["status"] == "limited"
                and receipt["memory_at_execution"].get("available_vram_gib") == 0)
            if receipt.get("state") != "failed" or not (
                    failure_class == native_oom == "out_of_memory" or zero_preflight):
                raise ValueError("The retained video failure classification changed.")

    @staticmethod
    def _memory_snapshot(observation):
        if not isinstance(observation, dict):
            raise ValueError("The video memory observation is invalid.")
        status = observation.get("status")
        available = observation.get("available_vram_gib")
        total = observation.get("total_vram_gib")
        observed_at = observation.get("observed_at")
        if (not isinstance(observed_at, str) or len(observed_at) > 64
                or set(observation) != {"status", "available_vram_gib",
                                       "total_vram_gib", "observed_at"}):
            raise ValueError("The video memory observation is invalid.")
        try:
            if datetime.fromisoformat(observed_at).tzinfo is None:
                raise ValueError
        except ValueError as exc:
            raise ValueError("The video memory observation time is invalid.") from exc
        if status == "unknown" and available is None and total is None:
            return dict(observation)
        if (status != "measured" or type(available) not in (int, float)
                or type(total) not in (int, float) or not math.isfinite(available)
                or not math.isfinite(total) or total <= 0 or not 0 <= available <= total):
            raise ValueError("The video memory observation is invalid.")
        return dict(observation)

    @staticmethod
    def _native_failure_class(history):
        status = history.get("status") if isinstance(history, dict) else None
        if not isinstance(status, dict) or status.get("status_str") != "error":
            return None
        messages = status.get("messages")
        if not isinstance(messages, list):
            return None
        for entry in messages[:128]:
            if (isinstance(entry, (list, tuple)) and len(entry) == 2
                    and entry[0] == "execution_error" and isinstance(entry[1], dict)
                    and classify_memory_failure({
                        "type": entry[1].get("exception_type"),
                        "message": entry[1].get("exception_message")})):
                return "out_of_memory"
        return None

    def submit(self, adapter, prepared, *, confirmed=False):
        if confirmed is not True:
            raise ValueError("Explicit reviewed video submission is required.")
        frozen = deepcopy(prepared)
        _, request = self._validate_prepared(frozen)
        if "execution_identity" in frozen or "protected_usage_context" in frozen:
            raise ValueError("A new video submission cannot reuse an earlier attempt identity.")
        job_id = uuid4().hex
        prompt_id = str(UUID(job_id))  # Same job identity in native Comfy's canonical UUID form.
        attempt_id = uuid4().hex
        frozen["execution_identity"] = {"job_id": job_id, "attempt_id": attempt_id,
            "source_image": request.source_image.to_dict() if request.source_image else None,
            "source_state_sha256": request.source_image.source_state_sha256 if request.source_image else None}
        adapter.revalidate(frozen)
        if self.model_usage is not None:
            reader = getattr(adapter, "protected_usage_context", None)
            context = reader(frozen) if callable(reader) else None
            if context is not None:
                from .comfy_video_adapter import validate_protected_usage_context
                frozen["protected_usage_context"] = validate_protected_usage_context(context, frozen)
        receipt = {"schema_id": "collectigen.video-execution.v1", "job_id": job_id,
            "prompt_id": prompt_id, "attempt_id": attempt_id,
            "client_id": "collectigen-video", "created_at": _now(),
            "request_fingerprint": _digest(frozen), "prepared": frozen,
            "state": "reviewed", "outputs": [], "cancel_requested": False}
        self._save(receipt)

        def work(job):
            current = deepcopy(receipt)
            try:
                if job.cancel_requested:
                    return self._transition(current, "cancelled")
                observation = adapter.revalidate(frozen)
                if observation is not None:
                    current = self._transition(current, "reviewed",
                        memory_at_execution=self._memory_snapshot({key: observation.get(key)
                            for key in ("status", "available_vram_gib",
                                        "total_vram_gib", "observed_at")}))
                    if assess_memory(current["memory_at_execution"])["status"] == "limited":
                        return self._transition(current, "failed",
                                                failure_class="insufficient_vram")
                current = self._admit_usage(current, adapter)
                current = self._transition(current, "uploading")
                upload_receipt = adapter.upload_source(frozen)  # No-op for T2V; explicit owned upload for I2V.
                current = self._transition(current, "uploaded", uploaded_source=upload_receipt)
                if job.cancel_requested:
                    return self._transition(current, "cancelled")
                current = self._transition(current, "submitting")
                adapter.transport.submit(frozen["graph_plan"]["graph"], prompt_id=prompt_id,
                    client_id=receipt["client_id"], manifest=self._native_identity(receipt))
                current = self._transition(current, "submitted")
                deadline = time.monotonic() + self.timeout
                while time.monotonic() < deadline:
                    history = adapter.transport.history(prompt_id)
                    if not isinstance(history, dict):
                        raise ValueError("Native video history is malformed.")
                    if prompt_id in history:
                        return self._accept_history(current, history[prompt_id], adapter,
                            should_cancel=lambda: job.cancel_requested)
                    if job.cancel_requested and not current["cancel_requested"]:
                        queue = adapter.transport.queue()
                        if not isinstance(queue, dict):
                            raise ValueError("Native video queue is malformed.")
                        running = any(isinstance(row, list) and len(row) > 1 and row[1] == prompt_id
                                      for row in queue.get("queue_running", []))
                        pending = any(isinstance(row, list) and len(row) > 1 and row[1] == prompt_id
                                      for row in queue.get("queue_pending", []))
                        if not running and not pending:
                            current = self._transition(current, "cancelling", cancel_requested=True)
                            continue
                        adapter.transport.cancel(prompt_id, running=running)
                        current = self._transition(current, "cancelling", cancel_requested=True)
                        if pending and not running:
                            checked = adapter.transport.history(prompt_id)
                            if not isinstance(checked, dict):
                                raise ValueError("Native video history is malformed.")
                            if prompt_id in checked:
                                return self._accept_history(current, checked[prompt_id], adapter)
                            after = adapter.transport.queue()
                            if not isinstance(after, dict):
                                raise ValueError("Native video queue is malformed.")
                            if any(isinstance(row, list) and len(row) > 1 and row[1] == prompt_id
                                   for row in after.get("queue_running", [])):
                                continue
                            if not any(isinstance(row, list) and len(row) > 1 and row[1] == prompt_id
                                       for row in after.get("queue_pending", [])):
                                checked = adapter.transport.history(prompt_id)
                                if not isinstance(checked, dict):
                                    raise ValueError("Native video history is malformed.")
                                if prompt_id in checked:
                                    return self._accept_history(current, checked[prompt_id], adapter)
                                return self._transition(current, "cancelled")
                    job.update(message="Waiting for the exact native video job")
                    time.sleep(self.poll_interval)
                raise TimeoutError("Video outcome is not yet known. Recover this exact job before creating another submission.")
            except Exception:
                latest = self._load(job_id)
                if latest["state"] not in {"verified", "cancelled", "failed"}:
                    self._transition(latest, "outcome_unknown")
                raise
        return self.jobs.submit("Generate local video", work, resource="gpu",
                                reclaim_ai_runtime=True, recover_on_shutdown=True)

    def _transition(self, receipt, state, **changes):
        updated = deepcopy(receipt)
        updated.update(state=state, updated_at=_now(), **changes)
        self._save(updated, expected=receipt)
        return updated

    @staticmethod
    def _usage_facts(receipt):
        from .comfy_video_adapter import validate_protected_usage_context
        prepared = receipt["prepared"]
        context = validate_protected_usage_context(prepared["protected_usage_context"], prepared)
        return UsageAdmissionFacts(_USAGE_OWNER, _USAGE_VERSION,
            "video." + receipt["job_id"] + "." + receipt["attempt_id"], tuple(context["resource_ids"]),
            _digest({"request_fingerprint": receipt["request_fingerprint"], "runtime_owner": context}))

    def _admit_usage(self, receipt, adapter):
        if self.model_usage is None or "protected_usage_context" not in receipt["prepared"]:
            return receipt
        if "model_usage" in receipt:
            return receipt  # Never re-admit, including a crash before token persistence.
        reader = getattr(adapter, "protected_usage_context", None)
        context = reader(receipt["prepared"]) if callable(reader) else None
        if context != receipt["prepared"]["protected_usage_context"]:
            raise ValueError("The admitted video runtime ownership changed before execution.")
        facts = self._usage_facts(receipt)
        marker = {"attempted": True, "admission": None, "state": "attempting"}
        receipt = self._transition(receipt, receipt["state"], model_usage=marker)
        self._usage_admissions[facts.execution_id] = facts
        try:
            admission = self.model_usage.admit(facts.owner_id, facts.owner_version, facts.execution_id,
                facts.resource_ids, facts.required_set_digest)
            state = self.model_usage.last_admission_state
        except (OSError, RuntimeError, ValueError):
            # Optional counts do not block generation. The durable attempted
            # marker prevents an old job acquiring a later reset/enable epoch.
            admission, state = None, "unavailable"
        finally:
            self._usage_admissions.pop(facts.execution_id, None)
        return self._transition(receipt, receipt["state"], model_usage={**marker,
            "admission": admission.to_dict() if admission else None, "state": state})

    @staticmethod
    def _matching_success_history(receipt):
        history = receipt.get("raw_runtime_history")
        prompt = history.get("prompt") if isinstance(history, dict) else None
        return bool(isinstance(prompt, list) and len(prompt) >= 4
            and prompt[1] == receipt["prompt_id"] and prompt[2] == receipt["prepared"]["graph_plan"]["graph"]
            and isinstance(prompt[3], dict) and prompt[3].get("collectigen") == VideoExecutionService._native_identity(receipt)
            and history.get("status", {}).get("status_str") == "success"
            and history.get("status", {}).get("completed") is True)

    def _record_usage(self, receipt, *, just_decoded=False):
        usage = receipt.get("model_usage")
        if (self.model_usage is None or not usage or usage["admission"] is None
                or usage["state"] in {"recorded", "already_recorded", "stale", "disabled", "cancelled"}
                or receipt["state"] != "verified" or receipt.get("cancel_requested")
                or not receipt.get("verified_completed_at") or not self._matching_success_history(receipt)):
            return receipt
        # Read-back, exact lineage and output identity remain mandatory even on
        # reconciliation. A historical receipt does not adopt a running process.
        outputs = self.verified_video_outputs(receipt)
        if not just_decoded:
            request = VideoGenerationRequest.from_dict(receipt["prepared"]["graph_plan"]["manifest"]["request"])
            for output in outputs:
                # Final-size recipes retain both native generation and the
                # verified deterministic resize; decode with the native request.
                native = output.get("generation", {}).get("native_output") or output
                evidence = self.verify_output(Path(native["path"]), request)
                asset = evidence["source_asset"] if isinstance(evidence, dict) else evidence.asset
                asset = asset if isinstance(asset, VideoSourceAsset) else VideoSourceAsset.from_dict(asset)
                if asset.to_dict() != native["source_asset"]:
                    raise ValueError("The retained video no longer matches its complete decoder evidence.")
            self.verified_video_outputs(receipt)  # Recheck after the decoder read.
        facts = self._usage_facts(receipt)
        admission = UsageAdmission.from_dict(usage["admission"])
        proof = uuid4().hex
        self._usage_proofs[proof] = UsageAttestation(facts.owner_id, facts.owner_version, facts.execution_id,
            facts.resource_ids, facts.required_set_digest, receipt["verified_completed_at"], admission)
        try:
            result = self.model_usage.record_success(admission, proof)
            state = result["state"]
        except (OSError, RuntimeError, ValueError):
            state = "retry_pending"
        finally:
            self._usage_proofs.pop(proof, None)
        return self._transition(receipt, "verified", model_usage={**usage, "state": state})

    def _accept_history(self, receipt, history, adapter, *, should_cancel=None):
        if not isinstance(history, dict):
            raise ValueError("Native video history is malformed.")
        prompt = history.get("prompt")
        graph_plan = receipt["prepared"]["graph_plan"]
        if (not isinstance(prompt, list) or len(prompt) < 4 or prompt[1] != receipt["prompt_id"]
                or prompt[2] != graph_plan["graph"] or not isinstance(prompt[3], dict)
                or prompt[3].get("collectigen") != self._native_identity(receipt)):
            raise ValueError("Video history does not match the retained graph and exact request.")
        status = history.get("status", {})
        if status.get("status_str") == "error":
            failure_class = self._native_failure_class(history)
            changes = {"raw_runtime_history": history}
            if failure_class is not None:
                changes["failure_class"] = failure_class
            return self._transition(receipt, "failed", **changes)
        if status.get("completed") is not True or status.get("status_str") != "success":
            raise ValueError("The video job has no verified completion acknowledgement.")
        output_nodes = [key for key, value in graph_plan["graph"].items() if value.get("class_type") == "SaveVideo"]
        outputs = history.get("outputs", {})
        if len(output_nodes) != 1 or not isinstance(outputs, dict) or set(outputs) != set(output_nodes):
            raise ValueError("Unexpected or foreign video outputs were returned.")
        images = outputs[output_nodes[0]].get("images", [])
        if not isinstance(images, list) or len(images) != 1:
            raise ValueError("Expected exactly one retained video output.")
        token = graph_plan["manifest"]["output_token"]
        descriptor = output_descriptor(images[0], output_token=token)
        if self._load(receipt["job_id"]) != receipt:
            raise ValueError("The video job changed before output download.")
        raw = adapter.transport.download_output(descriptor, output_token=token)
        if not isinstance(raw, bytes) or not raw or len(raw) > MAX_RESULT_BYTES:
            raise ValueError("The downloaded video exceeds its bounded output contract.")
        request = VideoGenerationRequest.from_dict(graph_plan["manifest"]["request"])
        output_cap = min(MAX_RESULT_BYTES, request.expected_output_bytes or MAX_RESULT_BYTES)
        if len(raw) > output_cap:
            raise ValueError("The native video exceeds the reviewed output byte cap.")
        from .video_output_goal import validate_video_output_goal
        output_goal = validate_video_output_goal(request)
        path = self._directory(receipt["job_id"]) / ("native-result.mp4" if output_goal else "result.mp4")
        _assert_unredirected(path)
        if path.exists():
            if path.stat().st_size > MAX_RESULT_BYTES:
                raise ValueError("Retained video output exceeds the supported size.")
            existing, _ = read_stable_training_source(path)
            if existing != raw:
                raise ValueError("Retained video output collision; previous bytes remain unchanged.")
        atomic_verified_bytes(path, raw, label="Generated video")
        evidence = self.verify_output(path, request)
        asset_value = (evidence["source_asset"] if isinstance(evidence, dict) else evidence.asset)
        asset = asset_value if isinstance(asset_value, VideoSourceAsset) else VideoSourceAsset.from_dict(asset_value)
        decoder_evidence = (evidence.get("decoder_evidence", {}) if isinstance(evidence, dict)
                            else evidence.decoder_evidence)
        sha = hashlib.sha256(raw).hexdigest()
        if asset.sha256 != sha or asset.byte_length != len(raw):
            raise ValueError("Video decoder evidence differs from the retained exact output.")
        if path.stat().st_size > MAX_RESULT_BYTES:
            raise ValueError("Retained video output exceeds the supported size.")
        verified_raw, verified_identity = read_stable_training_source(path)
        if verified_identity["sha256"] != sha or verified_raw != raw:
            raise ValueError("The decoded video changed before verification completed.")
        native_output = None
        if output_goal is not None:
            from .video_output_processing import resize_verified_video
            resized = resize_verified_video(path, asset, request,
                self._directory(receipt["job_id"]) / "result.mp4", allowed_root=self.root,
                width=output_goal["final_width"], height=output_goal["final_height"],
                should_cancel=should_cancel)
            native_output = {"path": str(path), "sha256": sha, "byte_length": len(raw),
                "source_asset": asset.to_dict(), "recipe": resized.recipe,
                "timeline": resized.lineage.to_dict()}
            path = resized.output_path
            asset = resized.verification.asset
            decoder_evidence = resized.verification.decoder_evidence
            raw, identity = read_stable_training_source(path)
            sha = hashlib.sha256(raw).hexdigest()
            if sha != resized.output_sha256 or sha != asset.sha256 or len(raw) != asset.byte_length or len(raw) > output_cap:
                raise ValueError("The final video changed before verified handoff.")
        result = {"media_type": "video", "path": str(path), "sha256": sha, "byte_length": len(raw),
            "source_asset": asset.to_dict(), "decoder_evidence": decoder_evidence,
            "review_status": "needs_review", "generation": {"job_id": receipt["job_id"],
                "prompt_id": receipt["prompt_id"], "attempt_id": receipt["attempt_id"],
                "request_fingerprint": receipt["request_fingerprint"],
                "graph_fingerprint": graph_plan["fingerprint"], "request": request.to_dict(),
                "resource_bindings": graph_plan["manifest"]["resources"],
                "source_image": request.source_image.to_dict() if request.source_image else None,
                "raw_runtime_history": history}}
        if native_output is not None:
            result["generation"]["native_output"] = native_output
        changes = {}
        if receipt.get("model_usage") and should_cancel and should_cancel():
            changes["model_usage"] = {**receipt["model_usage"], "state": "cancelled"}
        verified = self._transition(receipt, "verified", outputs=[result], raw_runtime_history=history,
            verified_completed_at=_now(), **changes)
        return self._record_usage(verified, just_decoded=True)

    def recover(self, job_id, adapter):
        receipt = self._load(job_id)
        if receipt["state"] == "verified":
            self.verified_video_outputs(receipt)
            return self._record_usage(receipt)
        if receipt["state"] in {"cancelled", "failed"}:
            return receipt
        history = adapter.transport.history(receipt["prompt_id"])
        if not isinstance(history, dict):
            raise ValueError("Native video history is malformed.")
        if receipt["prompt_id"] not in history:
            return receipt  # Missing history never authorizes resubmission.
        return self._accept_history(receipt, history[receipt["prompt_id"]], adapter)

    def verified_video_outputs(self, expected_receipt):
        if not isinstance(expected_receipt, dict):
            raise ValueError("A retained verified video receipt is required.")
        current = self._load(expected_receipt["job_id"])
        if current != expected_receipt or current["state"] != "verified" or len(current["outputs"]) != 1:
            raise ValueError("Recheck the current verified video job before Ingest.")
        output = current["outputs"][0]
        generation = output.get("generation", {})
        if (generation.get("job_id") != current["job_id"]
                or generation.get("attempt_id") != current["attempt_id"]
                or generation.get("source_image") != current["prepared"]["execution_identity"]["source_image"]
                or generation.get("request_fingerprint") != current["request_fingerprint"]):
            raise ValueError("The verified video lineage differs from the retained attempt.")
        if Path(output["path"]).absolute() != self._directory(current["job_id"]) / "result.mp4":
            raise ValueError("Video result path is outside the retained job.")
        if Path(output["path"]).stat().st_size > MAX_RESULT_BYTES:
            raise ValueError("The retained video exceeds the supported size.")
        raw, identity = read_stable_training_source(Path(output["path"]))
        if identity["sha256"] != output["sha256"] or len(raw) != output["byte_length"]:
            raise ValueError("The retained video changed before handoff.")
        native = generation.get("native_output")
        from .video_output_goal import validate_video_output_goal
        request = VideoGenerationRequest.from_dict(current["prepared"]["graph_plan"]["manifest"]["request"])
        goal = validate_video_output_goal(request)
        if bool(native) != bool(goal):
            raise ValueError("The final-video recipe differs from the reviewed request.")
        if native:
            expected_native = self._directory(current["job_id"]) / "native-result.mp4"
            if Path(native["path"]).absolute() != expected_native or expected_native.stat().st_size > MAX_RESULT_BYTES:
                raise ValueError("The retained native clip is outside its bounded job.")
            original, original_identity = read_stable_training_source(expected_native)
            if original_identity["sha256"] != native["sha256"] or len(original) != native["byte_length"]:
                raise ValueError("The retained native clip changed before final-video handoff.")
            from .video_output_processing import resize_verified_video
            from .video_temporal import DerivedClipMapping
            original_asset = VideoSourceAsset.from_dict(native["source_asset"])
            mapping = DerivedClipMapping.from_dict(native["timeline"])
            checked = resize_verified_video(expected_native, original_asset, request, Path(output["path"]),
                allowed_root=self.root, width=goal["final_width"], height=goal["final_height"])
            if (checked.source_sha256 != native["sha256"] or checked.output_sha256 != output["sha256"]
                    or checked.recipe != native["recipe"] or checked.lineage.to_dict() != mapping.to_dict()
                    or checked.verification.asset.to_dict() != output["source_asset"]):
                raise ValueError("The retained final-video timeline or recipe differs from its exact resize receipt.")
        receipt_path = self._directory(current["job_id"]) / "receipt.json"
        if receipt_path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("The retained video receipt exceeds the supported size.")
        receipt_raw, _ = read_stable_training_source(receipt_path)
        if strict_json(receipt_raw) != current:
            raise ValueError("The video receipt changed before Ingest handoff.")
        result = deepcopy(current["outputs"])
        result[0]["output_receipt"] = {"path": str(receipt_path),
            "sha256": hashlib.sha256(receipt_raw).hexdigest()}
        return result
