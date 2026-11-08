"""Isolated CPU-only PyAV resize child for one exact owned stage."""
from __future__ import annotations

from fractions import Fraction
import json
import os
from pathlib import Path
import sys
from time import monotonic

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _encode(spec: dict) -> dict:
    from core.video_decoder_worker import _bound_memory
    memory_job = _bound_memory(1024 * 1024 * 1024)
    import av
    from PIL import Image
    from core.video_decoder import (
        _contained_regular, _container, _frame_geometry, _frame_pts,
        _fraction, _hash_descriptor, _unchanged, _video_stream,
    )
    from core.video_generation import VideoSourceAsset

    started = monotonic()
    source_asset = VideoSourceAsset.from_dict(spec["source_asset"])
    trim = spec.get("operation") == "trim"
    if spec.get("operation", "resize") not in {"resize", "trim"}:
        raise ValueError("Unknown isolated video processing operation.")
    source, descriptor, opened = _contained_regular(
        spec["source"], Path(spec["source"]).parent, spec["source_max_bytes"])
    try:
        digest = _hash_descriptor(descriptor, None, started, spec["timeout_seconds"])
        if digest != source_asset.sha256 or opened.st_size != source_asset.byte_length:
            raise ValueError("The reviewed source changed before encoding.")
        container, borrowed = _container(av, descriptor)
        try:
            stream, _format = _video_stream(container)
            base = _fraction(stream.time_base, "source time base")
            if (base != source_asset.timing.time_base.fraction
                    or str(getattr(stream, "id", getattr(stream, "index", 0))) != source_asset.stream_id):
                raise ValueError("The selected video stream changed before encoding.")
            points = source_asset.timing.frame_pts
            target_w, target_h = spec["width"], spec["height"]
            source_w, source_h = int(stream.width), int(stream.height)
            scale = min(Fraction(target_w, source_w), Fraction(target_h, source_h))
            fit_w = max(1, int(source_w * scale))
            fit_h = max(1, int(source_h * scale))
            if trim:
                first, stop = spec["first_frame"], spec["stop_frame"]
                if (type(first) is not int or type(stop) is not int
                        or not 0 <= first < stop <= len(points)
                        or (target_w, target_h) != (source_w, source_h)):
                    raise ValueError("The reviewed trim interval or geometry changed.")
            stage = Path(spec["stage"])
            if os.stat(stage, follow_symlinks=False).st_ino != spec["stage_id"]:
                raise ValueError("The owned stage changed identity.")
            with open(stage, "r+b") as file:
                if os.fstat(file.fileno()).st_ino != spec["stage_id"]:
                    raise ValueError("The owned stage changed identity.")
                file.truncate(0)
                file.seek(0)
                with av.open(file, "w", format="mp4") as output:
                    encoded = output.add_stream("libx264", rate=source_asset.timing.nominal_fps.fraction)
                    encoded.width, encoded.height = target_w, target_h
                    encoded.pix_fmt = "yuv420p"
                    encoded.options = {"preset": "ultrafast", "crf": "18", "bf": "0"}
                    encoded.time_base = base
                    encoded.codec_context.time_base = base
                    count = 0
                    for frame in container.decode(video=0):
                        if monotonic() - started > spec["timeout_seconds"]:
                            raise TimeoutError("Isolated encoding exceeded its time limit.")
                        _frame_geometry(frame, source_w, source_h, 128 * 1024 * 1024)
                        if count >= len(points) or _frame_pts(frame, base) != points[count]:
                            raise ValueError("The source frame timeline changed.")
                        if not trim or first <= count < stop:
                            image = frame.to_image().convert("RGB")
                            if trim:
                                converted = av.VideoFrame.from_image(image)
                                converted.pts = points[count] - points[first]
                            else:
                                fitted = image.resize((fit_w, fit_h), Image.Resampling.LANCZOS)
                                canvas = Image.new("RGB", (target_w, target_h), "black")
                                canvas.paste(fitted, ((target_w - fit_w) // 2, (target_h - fit_h) // 2))
                                converted = av.VideoFrame.from_image(canvas)
                                converted.pts = points[count] - points[0]
                            converted.time_base = base
                            for packet in encoded.encode(converted):
                                output.mux(packet)
                        count += 1
                        if file.tell() > spec["output_max_bytes"]:
                            raise ValueError("Resized video exceeded its byte budget.")
                    if count != len(points):
                        raise ValueError("The source ended before all reviewed frames.")
                    for packet in encoded.encode(None):
                        output.mux(packet)
                file.flush(); os.fsync(file.fileno())
                if os.fstat(file.fileno()).st_size > spec["output_max_bytes"]:
                    raise ValueError("Resized video exceeded its byte budget.")
        finally:
            container.close(); borrowed.close()
        _unchanged(source, descriptor, opened, digest, None, started, spec["timeout_seconds"])
        del memory_job
        return {"status": "ok", "source_sha256": digest,
            "fitted_width": fit_w, "fitted_height": fit_h,
            "selected_frames": stop - first if trim else len(points)}
    finally:
        os.close(descriptor)


def main() -> int:
    try:
        raw = sys.stdin.buffer.readline(8 * 1024 * 1024 + 1)
        if not raw.endswith(b"\n") or len(raw) > 8 * 1024 * 1024:
            raise ValueError("Video resize request exceeds its bound.")
        result = _encode(json.loads(raw))
    except BaseException as exc:
        result = {"status": "error", "error": str(exc)[:1000]}
    sys.stdout.buffer.write(json.dumps(result, separators=(",", ":")).encode() + b"\n")
    sys.stdout.buffer.flush()
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
