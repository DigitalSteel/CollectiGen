"""Generated Manager video receipts enter the existing source-preserving Ingest owner.
No native codec, model, generator or live library is used by these fixtures.
"""
from copy import deepcopy
from pathlib import Path
from dataclasses import replace
import hashlib
import json
import pytest
from core.video_generation import ImageSourceReference, Rational, TemporalTiming, VideoGenerationRequest, VideoSourceAsset
from core.video_generation_execution import VideoExecutionService, _digest
from core.video_decoder import VideoVerification
from core.video_asset import VideoAssetRecord, companion_path
from core.video_ingest import ManagerVideoIngestHandoff, VideoIngestService
from core.edit_service import MoveHistoryService
from core.external_write_policy import ExternalWritePolicy
from core.library_profiles import LibraryProfile, LibraryProfileRegistry, LibraryRole
RAW = b"Generated video fixture bytes; not a playable movie"
SOURCE = ImageSourceReference("a" * 64, 24, "b" * 64, "c" * 64)

def prepared(*, image=False, review=False):
    request = VideoGenerationRequest(
        task="i2v" if image else "t2v", model_resource_id="sha256:" + "d" * 64,
        positive_prompt="A moving scene", negative_prompt="", goal="", motion="", camera="",
        width=64, height=64, frame_count=5, fps=Rational(24, 1), seed=7,
        source_image=SOURCE if image else None,
    )
    graph = {"7": {"class_type": "SaveVideo", "inputs": {}}}
    manifest = {"request": request.to_dict(), "resources": [{"role": "transformer", "sha256": "d" * 64}],
                "uploaded_image": ({"source_sha256": SOURCE.sha256,
                    "source_state_sha256": SOURCE.source_state_sha256,
                    "byte_length": SOURCE.byte_length, "transform_sha256": SOURCE.transform_sha256}
                    if image else None), "output_token": "ownedtoken"}
    plan = {"graph": graph, "manifest": manifest, "translation_report": {},
            "requires_review": review}
    plan["fingerprint"] = _digest({key: plan[key] for key in
                                   ("graph", "manifest", "translation_report")})
    return {"graph_plan": plan, "reviewed_loss": False}

class Job:
    cancel_requested = False

    def update(self, **kwargs):
        pass

class Jobs:
    def submit(self, title, work, **kwargs):
        self.submit_options = kwargs
        self.job = Job()
        try:
            self.result = work(self.job)
            self.error = None
        except Exception as exc:
            self.result = None
            self.error = exc
        return self.job

class Transport:
    def __init__(self):
        self.submissions = []
        self.histories = []
        self.queues = []
        self.cancelled = []
        self.on_submit = None
        self.on_cancel = None

    def submit(self, graph, *, prompt_id, client_id, manifest):
        from uuid import UUID
        assert prompt_id == str(UUID(prompt_id))
        assert UUID(prompt_id).hex == manifest['job_id']
        self.submissions.append((deepcopy(graph), prompt_id, client_id, deepcopy(manifest)))
        if self.on_submit:
            self.on_submit()

    def history(self, prompt_id):
        if self.histories:
            return self.histories.pop(0)
        return {}

    def queue(self):
        if self.queues:
            return self.queues.pop(0)
        return {"queue_running": [], "queue_pending": []}

    def cancel(self, prompt_id, *, running):
        self.cancelled.append((prompt_id, running))
        if self.on_cancel:
            self.on_cancel()

    def download_output(self, descriptor, *, output_token):
        return RAW

class Adapter:
    def __init__(self, transport):
        self.transport = transport
        self.revalidated = []
        self.uploaded = []

    def revalidate(self, prepared):
        self.revalidated.append(deepcopy(prepared))

    def upload_source(self, prepared):
        self.uploaded.append(deepcopy(prepared))
        return {"uploaded": True} if prepared["graph_plan"]["manifest"]["uploaded_image"] else None

def success_history(transport, *, mutation=None):
    graph, job_id, _, manifest = transport.submissions[0]
    entry = {"prompt": [0, job_id, graph, {"collectigen": manifest}],
             "status": {"completed": True, "status_str": "success"},
             "outputs": {"7": {"images": [{"filename": "collectigen_ownedtoken_00001.mp4",
                "subfolder": "video", "type": "output"}]}}}
    if mutation:
        mutation(entry)
    return {job_id: entry}

def generated_asset(path, request):
    raw = Path(path).read_bytes()
    return VideoSourceAsset("generated", hashlib.sha256(raw).hexdigest(), len(raw), "v:0",
        TemporalTiming(Rational(1, 24), 0, request.frame_count, Rational(24, 1),
                       duration_ticks=request.frame_count, frame_pts=tuple(range(request.frame_count))),
        {"width": 64, "height": 64}, {"decoder": "generated fixture", "width": 64, "height": 64}, "network_shared")


def generated_video(root, monkeypatch, *, trim=False):
    """Use canonical job/trim producers with generated transport and codec boundary."""
    def verify(path, request):
        return {"source_asset": generated_asset(path, request).to_dict(), "decoder_evidence": {"fixture": True}}
    jobs = Jobs()
    service = VideoExecutionService(root / "Manager/video_generation/jobs", jobs,
        verify_output=verify, poll_interval=0, timeout=.1)
    transport = Transport(); adapter = Adapter(transport)
    transport.on_submit = lambda: transport.histories.append(success_history(transport))
    service.submit(adapter, prepared(), confirmed=True)
    assert jobs.error is None, jobs.error
    value = jobs.result
    output, = service.verified_video_outputs(value)
    original = Path(output["output_receipt"]["path"])
    selected = original
    if trim:
        import core.video_finishing as finishing
        def decode(path, request, **kwargs):
            return VideoVerification(generated_asset(path, request), {"fixture": True})
        def encode(payload, **kwargs):
            Path(payload["stage"]).write_bytes(b"Generated trimmed fixture")
            return {"source_sha256": payload["source_asset"]["sha256"],
                    "selected_frames": payload["stop_frame"] - payload["first_frame"]}
        monkeypatch.setattr(finishing, "verify_video_output", decode)
        monkeypatch.setattr(finishing, "_encode_child", encode)
        target_root = root / "Manager/video_generation/finishing"; target_root.mkdir()
        result = finishing.trim_verified_video(Path(output["path"]),
            VideoSourceAsset.from_dict(output["source_asset"]),
            VideoGenerationRequest.from_dict(output["generation"]["request"]),
            1, 4, target_root / "reviewed-trim.mp4", allowed_root=target_root)
        selected = result.receipt_path
        output = finishing.verified_trimmed_output(result, output,
            VideoGenerationRequest.from_dict(output["generation"]["request"]))
    return selected, original, output


def ingest_service(root, handoff):
    incoming = root / "Incoming Videos"; incoming.mkdir(exist_ok=True)
    profile = LibraryProfile.create("Generated Library", root, LibraryRole.NETWORK_SHARED)
    return VideoIngestService(handoff.verified_outputs, MoveHistoryService(root / "moves.json", recover=False),
        library_registry=LibraryProfileRegistry([profile]), external_write_policy=ExternalWritePolicy()), incoming


@pytest.mark.parametrize("trim", [False, True], ids=["original", "trim"])
def test_original_and_trim_receipts_copy_through_canonical_ingest(tmp_path, monkeypatch, trim):
    selected, original, expected = generated_video(tmp_path, monkeypatch, trim=trim)
    assert ManagerVideoIngestHandoff.selection_kind(selected) == ("trim" if trim else "video")
    admissions = []
    handoff = ManagerVideoIngestHandoff.from_selected_receipts(selected, original,
        runtime_admission=lambda: admissions.append(True))
    service, incoming = ingest_service(tmp_path, handoff)
    before = {p: p.read_bytes() for p in {selected, original, handoff.source_path}}
    plan = service.preview(selected, handoff.source_path, incoming)
    assert plan.ready, plan.issues
    assert not companion_path(handoff.source_path).exists()
    assert all(p.read_bytes() == raw for p, raw in before.items())
    result = service.transfer(plan)
    assert result.status == "transferred", result
    record = VideoAssetRecord.read(result.destination)
    assert record.to_dict()["review_status"] == "needs_review"
    assert record.to_dict()["generation"] == expected["generation"]
    assert result.destination.read_bytes() == before[handoff.source_path]
    assert companion_path(result.destination).read_bytes() == companion_path(handoff.source_path).read_bytes()
    assert all(p.read_bytes() == raw for p, raw in before.items())
    assert service.transfer(plan).status == "already_present"
    assert bool(admissions) == trim


@pytest.mark.parametrize("changed", ["receipt", "original", "video"])
def test_changed_trim_or_original_cannot_reuse_preview(tmp_path, monkeypatch, changed):
    selected, original, expected = generated_video(tmp_path, monkeypatch, trim=True)
    handoff = ManagerVideoIngestHandoff.from_selected_receipts(selected, original)
    service, incoming = ingest_service(tmp_path, handoff)
    plan = service.preview(selected, handoff.source_path, incoming)
    assert plan.ready, plan.issues
    target = {"receipt": selected, "original": original, "video": handoff.source_path}[changed]
    target.write_bytes(target.read_bytes() + b" ")
    result = service.transfer(plan)
    assert result.status == "failed"
    assert not (incoming / handoff.source_path.name).exists()
    assert not companion_path(handoff.source_path).exists()


def test_explicit_original_workspace_cancel_and_collision(tmp_path, monkeypatch):
    selected, original, expected = generated_video(tmp_path, monkeypatch, trim=True)
    with pytest.raises(ValueError):
        ManagerVideoIngestHandoff.from_selected_receipts(selected)
    handoff = ManagerVideoIngestHandoff.from_selected_receipts(selected, original)
    service, incoming = ingest_service(tmp_path, handoff)
    before = handoff.source_path.read_bytes()
    with pytest.raises(InterruptedError):
        service.preview(selected, handoff.source_path, incoming, should_cancel=lambda: True)
    assert handoff.source_path.read_bytes() == before
    occupied = incoming / handoff.source_path.name; occupied.write_bytes(b"Existing separate video")
    plan = service.preview(selected, handoff.source_path, incoming)
    assert not plan.ready
    assert occupied.read_bytes() == b"Existing separate video"
    assert not companion_path(handoff.source_path).exists()


def test_incomplete_resize_cannot_be_repaired_during_receipt_review(tmp_path, monkeypatch):
    from core.video_generation import VideoSourceAsset, VideoGenerationRequest
    import core.video_output_processing as processing
    selected, original, output = generated_video(tmp_path, monkeypatch)
    source = Path(output["path"])
    asset = VideoSourceAsset.from_dict(output["source_asset"])
    request = VideoGenerationRequest.from_dict(output["generation"]["request"])
    target = source.with_name("unfinished-resize.mp4")
    def no_work(*args, **kwargs):
        pytest.fail("Read-only receipt review cannot decode, encode or repair an incomplete resize")
    monkeypatch.setattr(processing, "_encode_child", no_work)
    monkeypatch.setattr(processing, "verify_video_output", no_work)
    with pytest.raises(ValueError, match="incomplete resize"):
        processing.resize_verified_video(source, asset, request, target,
            allowed_root=source.parent, width=128, height=128, allow_recovery=False)
    assert not target.exists()
    assert source.read_bytes() == RAW
